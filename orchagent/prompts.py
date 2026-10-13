from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from .paths import DEFAULT_HOME


ROOT_FIELDS = {"version", "prompts", "targets"}
PROMPT_FIELDS = {"id", "description", "template", "extends"}
TARGET_FIELDS = {"kind", "id", "prompt", "override"}
SAFE_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
SENSITIVE_KEYS = frozenset({"apikey", "token", "secret", "password", "credential", "bearer", "env", "authorization"})
SENSITIVE_ASSIGNMENT_PATTERN = re.compile(
    r"(?im)(?:^|[\s`'\"(])(?:[A-Za-z0-9_-]*(?:api[_-]?key|token|secret|password|credential|authorization|bearer|env)[A-Za-z0-9_-]*)\s*[:=]\s*\S"
)
SENSITIVE_BEARER_PATTERN = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]{4,}")
SENSITIVE_REFERENCE_PATTERN = re.compile(
    r"(?i)\$\{\s*(?:[A-Za-z0-9_-]*(?:api[_-]?key|token|secret|password|credential|authorization|bearer|env)[A-Za-z0-9_-]*)\s*\}|\bprocess\.env\b"
)
SENSITIVE_NAME_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])(?:[A-Za-z0-9]+[._-])*?(?:api[._ -]?key|access[._ -]?token|client[._ -]?secret|api[._ -]?secret|authorization|token|secret|password|credential|bearer)(?:[A-Za-z0-9._-]*)(?![A-Za-z0-9])"
)


def prompt_registry_path(home: Path = DEFAULT_HOME) -> Path:
    return Path(home) / "prompts" / "prompts.json"


def _error(code: str, location: str, message: str) -> dict[str, Any]:
    return {
        "status": "error",
        "checks": [{"level": "error", "code": code, "location": location, "message": message}],
    }


def _sensitive_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    return normalized in SENSITIVE_KEYS or any(
        marker in normalized for marker in ("apikey", "token", "secret", "password", "credential", "authorization", "bearer")
    )


def _contains_sensitive(value: str) -> bool:
    return any(
        pattern.search(value) is not None
        for pattern in (
            SENSITIVE_ASSIGNMENT_PATTERN,
            SENSITIVE_BEARER_PATTERN,
            SENSITIVE_REFERENCE_PATTERN,
            SENSITIVE_NAME_PATTERN,
        )
    )


def _sensitive_location(value: Any, location: str) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            child_location = f"{location}.{key}" if location else str(key)
            if _sensitive_key(str(key)):
                return child_location
            found = _sensitive_location(child, child_location)
            if found:
                return found
    elif isinstance(value, list):
        for index, child in enumerate(value):
            found = _sensitive_location(child, f"{location}[{index}]")
            if found:
                return found
    elif isinstance(value, str) and _contains_sensitive(value):
        return location
    return None


def sensitive_data_location(value: Any, location: str = "draft") -> str | None:
    """返回首个凭据相关位置；只暴露结构位置，不暴露输入值。"""
    return _sensitive_location(value, location)


def _read_regular_file(path: Path, root: Path) -> str:
    if path.is_absolute() and path != root:
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValueError("path_outside_root") from exc
    relative = path.relative_to(root)
    if any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("path_outside_root")
    descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        parent = os.dup(descriptor)
        try:
            for part in relative.parts[:-1]:
                next_parent = os.open(part, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
                os.close(parent)
                parent = next_parent
            file_descriptor = os.open(relative.parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent)
            try:
                if not stat.S_ISREG(os.fstat(file_descriptor).st_mode):
                    raise ValueError("not_regular_file")
                chunks: list[bytes] = []
                while True:
                    chunk = os.read(file_descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8")
            finally:
                os.close(file_descriptor)
        finally:
            os.close(parent)
    except (OSError, UnicodeError) as exc:
        raise ValueError("file_unavailable") from exc
    finally:
        os.close(descriptor)


def _resolve_markdown(root: Path, raw: Any, file_contents: dict[str, str] | None = None) -> tuple[Path, str]:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("path_required")
    candidate = Path(raw)
    if candidate.is_absolute() or candidate.suffix != ".md" or any(part in {"", ".", ".."} for part in candidate.parts):
        raise ValueError("path_outside_root")
    path = root / candidate
    if file_contents is not None and candidate.as_posix() in file_contents:
        return path, file_contents[candidate.as_posix()]
    return path, _read_regular_file(path, root)


def load_prompt_registry(
    home: Path = DEFAULT_HOME,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    path = prompt_registry_path(home)
    try:
        text = _read_regular_file(path, path.parent)
        registry = json.loads(text)
    except (ValueError, json.JSONDecodeError):
        return None, _error("registry_read_error", "registry", "prompt registry could not be read")
    if not isinstance(registry, dict):
        return None, _error("invalid_type", "registry", "prompt registry root must be an object")
    return registry, None


def _unknown_fields(value: dict[str, Any], allowed: set[str], location: str) -> dict[str, Any] | None:
    if set(value) - allowed:
        return _error("unknown_field", location, "object contains an unknown field")
    return None


def _validate(home: Path, registry: dict[str, Any], file_contents: dict[str, str] | None = None) -> dict[str, Any]:
    sensitive = _sensitive_location(registry, "registry")
    if sensitive:
        return _error("sensitive_data", sensitive, "credential-related data is not allowed")
    unknown = _unknown_fields(registry, ROOT_FIELDS, "registry")
    if unknown:
        return unknown
    version = registry.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        return _error("invalid_version", "registry.version", "version must be integer 1")
    prompts = registry.get("prompts")
    targets = registry.get("targets")
    if not isinstance(prompts, list):
        return _error("invalid_type", "registry.prompts", "prompts must be a list")
    if not isinstance(targets, list):
        return _error("invalid_type", "registry.targets", "targets must be a list")

    root = prompt_registry_path(home).parent
    prompt_ids: set[str] = set()
    prompt_map: dict[str, dict[str, Any]] = {}
    for index, prompt in enumerate(prompts):
        location = f"registry.prompts[{index}]"
        if not isinstance(prompt, dict):
            return _error("invalid_type", location, "prompt must be an object")
        unknown = _unknown_fields(prompt, PROMPT_FIELDS, location)
        if unknown:
            return unknown
        prompt_id = prompt.get("id")
        if not isinstance(prompt_id, str) or SAFE_IDENTIFIER_PATTERN.fullmatch(prompt_id) is None:
            return _error("invalid_identifier", f"{location}.id", "id must use safe identifier syntax")
        if prompt_id in prompt_ids:
            return _error("duplicate_id", f"{location}.id", "prompt id must be unique")
        prompt_ids.add(prompt_id)
        prompt_map[prompt_id] = prompt
        if "description" in prompt and (not isinstance(prompt["description"], str) or not prompt["description"].strip()):
            return _error("invalid_type", f"{location}.description", "description must be a non-empty string")
        try:
            _, body = _resolve_markdown(root, prompt.get("template"), file_contents)
        except ValueError as exc:
            return _error(str(exc), f"{location}.template", "template must reference a regular Markdown file inside prompts root")
        if _contains_sensitive(body):
            return _error("sensitive_data", f"{location}.template", "credential-related data is not allowed")

    for index, prompt in enumerate(prompts):
        parent = prompt.get("extends")
        if parent is not None and parent not in prompt_ids:
            return _error("unknown_reference", f"registry.prompts[{index}].extends", "parent prompt does not exist")

    visiting: set[str] = set()
    visited: set[str] = set()
    def visit(prompt_id: str) -> bool:
        if prompt_id in visiting:
            return False
        if prompt_id in visited:
            return True
        visiting.add(prompt_id)
        parent = prompt_map[prompt_id].get("extends")
        if parent is not None and not visit(parent):
            return False
        visiting.remove(prompt_id)
        visited.add(prompt_id)
        return True
    if any(not visit(prompt_id) for prompt_id in prompt_ids):
        return _error("inheritance_cycle", "registry.prompts", "prompt inheritance must be acyclic")

    target_keys: set[tuple[str, str]] = set()
    for index, target in enumerate(targets):
        location = f"registry.targets[{index}]"
        if not isinstance(target, dict):
            return _error("invalid_type", location, "target must be an object")
        unknown = _unknown_fields(target, TARGET_FIELDS, location)
        if unknown:
            return unknown
        if target.get("kind") != "role":
            return _error("unsupported_kind", f"{location}.kind", "target kind must be role")
        target_id = target.get("id")
        if not isinstance(target_id, str) or SAFE_IDENTIFIER_PATTERN.fullmatch(target_id) is None:
            return _error("invalid_identifier", f"{location}.id", "id must use safe identifier syntax")
        key = ("role", target_id)
        if key in target_keys:
            return _error("duplicate_target", location, "target kind and id must be unique")
        target_keys.add(key)
        if target.get("prompt") not in prompt_ids:
            return _error("unknown_reference", f"{location}.prompt", "target prompt does not exist")
        if "override" in target:
            try:
                _, body = _resolve_markdown(root, target["override"], file_contents)
            except ValueError as exc:
                return _error(str(exc), f"{location}.override", "override must reference a regular Markdown file inside prompts root")
            if _contains_sensitive(body):
                return _error("sensitive_data", f"{location}.override", "credential-related data is not allowed")
    return {"status": "ok", "checks": [{"level": "ok", "message": "prompt registry is valid"}]}


def validate_prompt_registry(
    home: Path = DEFAULT_HOME,
    registry: dict[str, Any] | None = None,
    file_contents: dict[str, str] | None = None,
) -> dict[str, Any]:
    home = Path(home)
    if registry is None:
        registry, error = load_prompt_registry(home)
        if error:
            return error
        assert registry is not None
    if not isinstance(registry, dict):
        return _error("invalid_type", "registry", "prompt registry root must be an object")
    return _validate(home, registry, file_contents)


def prompt_markdown_paths(registry: dict[str, Any]) -> list[str]:
    paths = {item.get("template") for item in registry.get("prompts", []) if isinstance(item, dict)}
    paths.update(item.get("override") for item in registry.get("targets", []) if isinstance(item, dict) and "override" in item)
    return sorted(path for path in paths if isinstance(path, str))


def load_prompt_file_contents(home: Path, registry: dict[str, Any]) -> dict[str, str]:
    """一次性读取 registry 引用的 Markdown，供校验与渲染共享同一快照。"""
    root = prompt_registry_path(home).parent
    contents: dict[str, str] = {}
    for relative in prompt_markdown_paths(registry):
        _, body = _resolve_markdown(root, relative)
        if _contains_sensitive(body):
            raise ValueError("sensitive_data")
        contents[relative] = body
    return contents


def render_prompt_registry(
    home: Path,
    registry: dict[str, Any],
    prompt_id: str,
    target_override_path: str | None = None,
    file_contents: dict[str, str] | None = None,
) -> dict[str, Any]:
    home = Path(home)
    validation = _validate(home, registry, file_contents)
    if validation["status"] != "ok":
        return validation
    prompts = {prompt["id"]: prompt for prompt in registry["prompts"]}
    if prompt_id not in prompts:
        return _error("unknown_prompt", "prompt_id", "prompt does not exist")
    chain: list[dict[str, Any]] = []
    current = prompts[prompt_id]
    while True:
        chain.append(current)
        parent = current.get("extends")
        if parent is None:
            break
        current = prompts[parent]
    chain.reverse()
    root = prompt_registry_path(home).parent
    sources = [prompt["template"] for prompt in chain]
    bodies = [_resolve_markdown(root, source, file_contents)[1] for source in sources]
    if target_override_path is not None:
        try:
            _, body = _resolve_markdown(root, target_override_path, file_contents)
        except ValueError as exc:
            return _error(str(exc), "target_override_path", "override must reference a regular Markdown file inside prompts root")
        if _contains_sensitive(body):
            return _error("sensitive_data", "target_override_path", "credential-related data is not allowed")
        bodies.append(body)
        sources.append(target_override_path)
    return {"status": "ok", "markdown": "\n\n".join(bodies), "sources": sources}


def render_prompt(
    home: Path = DEFAULT_HOME,
    prompt_id: str = "",
    target_override_path: str | None = None,
) -> dict[str, Any]:
    home = Path(home)
    registry, error = load_prompt_registry(home)
    if error:
        return error
    assert registry is not None
    return render_prompt_registry(home, registry, prompt_id, target_override_path)


def list_prompt_targets(home: Path = DEFAULT_HOME) -> dict[str, Any]:
    home = Path(home)
    registry, error = load_prompt_registry(home)
    if error:
        return error
    assert registry is not None
    validation = _validate(home, registry)
    if validation["status"] != "ok":
        return validation
    return {"status": "ok", "targets": [dict(target) for target in registry["targets"]]}
