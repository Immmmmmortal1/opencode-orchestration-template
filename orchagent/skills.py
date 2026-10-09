from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from .config import extension_registry_paths, read_text_config
from .paths import DEFAULT_HOME
from .skill_seam import SkillRegistry


SUPPORTED_ADAPTER_TYPES = {"filesystem"}
REGISTRY_FIELDS = {"version", "adapters", "skills"}
ADAPTER_FIELDS = {"id", "type", "enabled", "root"}
SKILL_FIELDS = {"id", "adapter", "path", "enabled", "backend"}
V2_REGISTRY_FIELDS = {"version", "providers", "overrides"}
V2_PROVIDER_TYPES = {"builtin", "filesystem", "opencode-host", "codex-host"}
V2_PROVIDER_COMMON_FIELDS = {"id", "type", "enabled", "rank"}
V2_PROVIDER_PATH_FIELDS = {"roots", "allowedBases", "source"}
V2_PROVIDER_HOST_FIELDS = V2_PROVIDER_PATH_FIELDS | {"useDefaultRoots"}
SENSITIVE_PATH_PARTS = {"secrets", "api-keys", "mail", "accounts"}
MAX_FRONTMATTER_BYTES = 8192


def skills_registry_path(home: Path = DEFAULT_HOME) -> Path:
    return extension_registry_paths(home)["skills"]


def _registry_fallback_path(home: Path) -> Path:
    return home / "extensions" / "skills.yaml"


def _load_registry(home: Path) -> tuple[Path, dict[str, Any] | None, str | None]:
    try:
        path = skills_registry_path(home)
    except Exception as exc:  # noqa: BLE001
        return _registry_fallback_path(home), None, f"failed to resolve skills registry path: {exc}"

    try:
        registry = read_text_config(path)
    except Exception as exc:  # noqa: BLE001
        return path, None, f"failed to read skills registry: {exc}"
    if not isinstance(registry, dict):
        return path, None, "skills registry root must be an object"

    unknown_fields = sorted(set(registry) - REGISTRY_FIELDS)
    if unknown_fields:
        return path, None, f"skills registry has unknown fields: {', '.join(unknown_fields)}"
    adapters = registry.get("adapters")
    skills = registry.get("skills")
    if registry.get("version") != 1:
        return path, None, "skills registry version must be 1"
    if not isinstance(adapters, list):
        return path, None, "skills adapters must be a list"
    if not isinstance(skills, list):
        return path, None, "skills must be a list"

    adapter_ids: set[str] = set()
    for index, adapter in enumerate(adapters):
        if not isinstance(adapter, dict):
            return path, None, f"skills adapter at index {index} must be an object"
        adapter_id = adapter.get("id")
        if not isinstance(adapter_id, str) or not adapter_id.strip():
            return path, None, f"skills adapter at index {index} id must be non-empty string"
        if adapter_id in adapter_ids:
            return path, None, f"duplicate skills adapter id: {adapter_id}"
        unknown_fields = sorted(set(adapter) - ADAPTER_FIELDS)
        if unknown_fields:
            return path, None, f"skills adapter {adapter_id} has unknown fields: {', '.join(unknown_fields)}"
        if adapter.get("type") not in SUPPORTED_ADAPTER_TYPES:
            return path, None, f"unsupported skills adapter type: {adapter.get('type')}"
        if not isinstance(adapter.get("enabled"), bool):
            return path, None, f"skills adapter {adapter_id} enabled must be boolean"
        if not isinstance(adapter.get("root"), str) or not adapter["root"].strip():
            return path, None, f"skills adapter {adapter_id} root must be non-empty string"
        adapter_ids.add(adapter_id)

    skill_ids: set[str] = set()
    for index, skill in enumerate(skills):
        if not isinstance(skill, dict):
            return path, None, f"skill at index {index} must be an object"
        skill_id = skill.get("id")
        if not isinstance(skill_id, str) or not skill_id.strip():
            return path, None, f"skill at index {index} id must be non-empty string"
        if skill_id in skill_ids:
            return path, None, f"duplicate skill id: {skill_id}"
        unknown_fields = sorted(set(skill) - SKILL_FIELDS)
        if unknown_fields:
            return path, None, f"skill {skill_id} has unknown fields: {', '.join(unknown_fields)}"
        if not isinstance(skill.get("adapter"), str) or skill["adapter"] not in adapter_ids:
            return path, None, f"skill {skill_id} references missing adapter"
        if not isinstance(skill.get("path"), str) or not skill["path"].strip():
            return path, None, f"skill {skill_id} path must be non-empty string"
        backend = skill.get("backend")
        if backend == "agent":
            return path, None, f"skill {skill_id} backend agent is not supported until 3B"
        if not isinstance(backend, str):
            return path, None, f"skill {skill_id} backend is required and must be string"
        if backend != "builtin":
            return path, None, f"unsupported skill backend: {backend}"
        if "enabled" in skill and not isinstance(skill["enabled"], bool):
            return path, None, f"skill {skill_id} enabled must be boolean"
        skill_ids.add(skill_id)

    return path, registry, None


def _read_registry_root(home: Path) -> tuple[Path, dict[str, Any] | None, str | None]:
    """只读取 registry 根对象，不预先套用 v1 schema。"""
    try:
        path = skills_registry_path(home)
    except Exception as exc:  # noqa: BLE001
        return _registry_fallback_path(home), None, f"failed to resolve skills registry path: {exc}"
    try:
        registry = read_text_config(path)
    except Exception as exc:  # noqa: BLE001
        return path, None, f"failed to read skills registry: {exc}"
    if not isinstance(registry, dict):
        return path, None, "skills registry root must be an object"
    return path, registry, None


def _v2_string_list(value: Any, field: str, provider_id: str) -> tuple[list[str] | None, str | None]:
    if not isinstance(value, list) or not value:
        return None, f"provider_invalid: skills provider {provider_id} {field} must be a non-empty string list"
    if any(not isinstance(item, str) or not item.strip() for item in value):
        return None, f"provider_invalid: skills provider {provider_id} {field} must be a non-empty string list"
    return value, None


def _v2_optional_string_list(
    provider: dict[str, Any], field: str, provider_id: str
) -> tuple[list[str], str | None]:
    if field not in provider:
        return [], None
    value = provider[field]
    if not isinstance(value, list):
        return [], f"provider_invalid: skills provider {provider_id} {field} must be a string list"
    if any(not isinstance(item, str) or not item.strip() for item in value):
        return [], f"provider_invalid: skills provider {provider_id} {field} must be a string list"
    return value, None


def _v2_paths(values: list[str]) -> list[Path]:
    """v2 路径由 registry 显式授权，只做变量和用户目录展开。"""
    return [Path(os.path.expanduser(os.path.expandvars(value))) for value in values]


def load_skill_registry_v2(
    home: Path = DEFAULT_HOME,
) -> tuple[SkillRegistry | None, list[dict[str, Any]] | None, str | None]:
    """读取 v2 registry 并构建 SkillRegistry。"""
    _, data, error = _read_registry_root(home)
    if error:
        return None, None, error
    assert data is not None

    unknown_fields = sorted(set(data) - V2_REGISTRY_FIELDS)
    if unknown_fields:
        return None, None, f"registry_unknown_fields: {', '.join(unknown_fields)}"
    version = data.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version != 2:
        return None, None, "registry_version_invalid: skills registry version must be integer 2"
    providers = data.get("providers")
    if not isinstance(providers, list):
        return None, None, "providers_invalid: skills providers must be a list"
    overrides = data.get("overrides")
    if not isinstance(overrides, list):
        return None, None, "overrides_invalid: skills overrides must be a list"
    if overrides:
        return None, None, "overrides_not_supported: skills overrides must be empty in 3B-1"

    # 延迟导入，避免 skill_providers 复用本模块安全读取函数时形成循环导入。
    from .skill_providers import (  # noqa: PLC0415
        BuiltinSkillProvider,
        CodexHostSkillProvider,
        FilesystemSkillProvider,
        OpencodeHostSkillProvider,
    )

    registry = SkillRegistry()
    providers_meta: list[dict[str, Any]] = []
    provider_ids: set[str] = set()
    for index, provider in enumerate(providers):
        if not isinstance(provider, dict):
            return None, None, f"provider_invalid: skills provider at index {index} must be an object"
        provider_id = provider.get("id")
        if not isinstance(provider_id, str) or not provider_id.strip():
            return None, None, f"provider_invalid: skills provider at index {index} id must be non-empty string"
        if provider_id in provider_ids:
            return None, None, f"provider_id_duplicate: {provider_id}"
        provider_type = provider.get("type")
        if provider_type not in V2_PROVIDER_TYPES:
            return None, None, f"provider_type_invalid: skills provider {provider_id} has unsupported type"
        enabled = provider.get("enabled")
        if not isinstance(enabled, bool):
            return None, None, f"provider_invalid: skills provider {provider_id} enabled must be boolean"
        rank = provider.get("rank")
        if isinstance(rank, bool) or not isinstance(rank, int) or rank < 0:
            return None, None, f"provider_invalid: skills provider {provider_id} rank must be non-negative integer"

        allowed_fields = set(V2_PROVIDER_COMMON_FIELDS)
        if provider_type == "filesystem":
            allowed_fields |= V2_PROVIDER_PATH_FIELDS
        elif provider_type in {"opencode-host", "codex-host"}:
            allowed_fields |= V2_PROVIDER_HOST_FIELDS
        unknown_provider_fields = sorted(set(provider) - allowed_fields)
        if unknown_provider_fields:
            return None, None, (
                f"provider_unknown_fields: skills provider {provider_id}: "
                f"{', '.join(unknown_provider_fields)}"
            )

        source_value = provider.get("source")
        if "source" in provider and (
            not isinstance(source_value, str) or not source_value.strip()
        ):
            return None, None, f"provider_invalid: skills provider {provider_id} source must be non-empty string"

        try:
            if provider_type == "builtin":
                provider_instance = BuiltinSkillProvider()
                provider_instance.provider_id = provider_id
                provider_instance.rank = rank
                source = provider_instance.source
            elif provider_type == "filesystem":
                roots, field_error = _v2_string_list(provider.get("roots"), "roots", provider_id)
                if field_error:
                    return None, None, field_error
                allowed_bases, field_error = _v2_string_list(
                    provider.get("allowedBases"), "allowedBases", provider_id
                )
                if field_error:
                    return None, None, field_error
                assert roots is not None and allowed_bases is not None
                source = source_value or "filesystem"
                provider_instance = FilesystemSkillProvider(
                    provider_id=provider_id,
                    rank=rank,
                    roots=_v2_paths(roots),
                    allowed_bases=_v2_paths(allowed_bases),
                    source=source,
                )
            else:
                use_default_roots = provider.get("useDefaultRoots", False)
                if not isinstance(use_default_roots, bool):
                    return None, None, (
                        f"provider_invalid: skills provider {provider_id} useDefaultRoots must be boolean"
                    )
                roots, field_error = _v2_optional_string_list(provider, "roots", provider_id)
                if field_error:
                    return None, None, field_error
                allowed_bases, field_error = _v2_optional_string_list(
                    provider, "allowedBases", provider_id
                )
                if field_error:
                    return None, None, field_error
                if use_default_roots and not allowed_bases:
                    return None, None, (
                        f"provider_invalid: skills provider {provider_id} allowedBases must be non-empty "
                        "when useDefaultRoots is true"
                    )
                host_class = (
                    OpencodeHostSkillProvider
                    if provider_type == "opencode-host"
                    else CodexHostSkillProvider
                )
                selected_roots = list(host_class.default_roots) if use_default_roots else []
                explicit_roots = _v2_paths(roots)
                selected_roots.extend(explicit_roots)
                # 显式 roots 自身可作为最窄授权边界；默认 roots 仍必须显式给 allowedBases。
                effective_allowed_bases = _v2_paths(allowed_bases) or explicit_roots
                provider_instance = host_class(
                    rank=rank,
                    roots=selected_roots,
                    allowed_bases=effective_allowed_bases,
                )
                source = source_value or provider_type
                provider_instance.provider_id = provider_id
                provider_instance.source = source
                delegate = getattr(provider_instance, "_delegate", None)
                if delegate is not None:
                    delegate.provider_id = provider_id
                    delegate.source = source
            if enabled:
                registry.register(provider_instance)
        except ValueError as exc:
            return None, None, f"provider_invalid: skills provider {provider_id}: {exc}"

        providers_meta.append({
            "id": provider_id,
            "type": provider_type,
            "enabled": enabled,
            "rank": rank,
            "source": source_value or source,
            "status": "available" if enabled else "disabled",
            "warnings": [],
        })
        provider_ids.add(provider_id)

    return registry, providers_meta, None


def _has_sensitive_part(path: Path) -> bool:
    for part in path.parts:
        # 去掉前导点，避免 .secrets / .accounts.yaml 这类隐藏名绕过。
        name = part.casefold().lstrip(".")
        if not name:
            continue
        if {name, Path(name).stem, name.split(".", 1)[0]} & SENSITIVE_PATH_PARTS:
            return True
    return False


def _has_symlink_component(path: Path, home: Path, home_resolved: Path) -> bool:
    relative: Path | None = None
    base = home
    for candidate_base in (home, home_resolved):
        try:
            relative = path.relative_to(candidate_base)
            base = candidate_base
            break
        except ValueError:
            continue
    if relative is None:
        return False
    current = base
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _authorize_path(path: Path, home: Path) -> tuple[Path | None, str | None]:
    try:
        home_resolved = home.resolve()
        resolved = path.resolve()
    except OSError as exc:
        return None, f"path resolution failed: {exc}"
    if _has_sensitive_part(path) or _has_sensitive_part(resolved):
        return None, "path contains a sensitive name"
    if resolved != home_resolved and home_resolved not in resolved.parents:
        return None, "path resolves outside ORCHAGENT_HOME"
    if _has_symlink_component(path, home, home_resolved):
        return None, "path contains a symlink component"
    return resolved, None


def _expanded_path(raw_path: str, base: Path, home: Path) -> tuple[Path | None, str | None]:
    expanded = Path(os.path.expandvars(os.path.expanduser(raw_path)))
    candidate = expanded if expanded.is_absolute() else base / expanded
    return _authorize_path(candidate, home)


def _read_frontmatter(path: Path) -> tuple[dict[str, str] | None, str | None]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return None, "SKILL.md is not a regular file"
            chunks: list[bytes] = []
            consumed = 0
            with os.fdopen(fd, "rb") as stream:
                fd = -1
                while consumed < MAX_FRONTMATTER_BYTES:
                    line = stream.readline(MAX_FRONTMATTER_BYTES - consumed)
                    if not line:
                        break
                    chunks.append(line)
                    consumed += len(line)
                    if len(chunks) > 1 and line.strip() == b"---":
                        break
                else:
                    return None, "SKILL.md frontmatter exceeds 8192 bytes"
        finally:
            if fd >= 0:
                os.close(fd)
    except OSError as exc:
        return None, f"failed to read SKILL.md: {exc}"

    try:
        lines = b"".join(chunks).decode("utf-8").splitlines()
    except UnicodeError as exc:
        return None, f"failed to read SKILL.md: {exc}"

    if not lines or lines[0].strip() != "---":
        return None, "SKILL.md must start with YAML frontmatter"
    if len(chunks) < 2 or chunks[-1].strip() != b"---":
        if consumed >= MAX_FRONTMATTER_BYTES:
            return None, "SKILL.md frontmatter exceeds 8192 bytes"
        return None, "SKILL.md frontmatter is not closed"
    fields: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields, None
        key, separator, value = line.partition(":")
        key = key.strip()
        if not separator or key not in {"name", "description"}:
            continue
        value = _parse_scalar(value)
        if value is None:
            return None, f"SKILL.md frontmatter {key} must be a non-empty scalar value"
        fields[key] = value
    return None, "SKILL.md frontmatter is not closed"


def _is_escaped(text: str, index: int) -> bool:
    """判断 text[index] 处的引号是否被前面的反斜杠转义（按连续反斜杠奇偶性）。"""
    backslashes = 0
    cursor = index - 1
    while cursor >= 0 and text[cursor] == "\\":
        backslashes += 1
        cursor -= 1
    return backslashes % 2 == 1


def _parse_scalar(raw_value: str) -> str | None:
    """按受限 frontmatter 语法解析标量；返回 None 表示非法或语义为空。

    受限语法（只支持 name / description 这类简单键值）：
    - 允许 `key: value`，value 可被一对同类引号完整包裹
    - 空值 / YAML null（裸 null、~ 及各大小写变体）/ 纯注释 / block scalar 均视为非法
    - 无引号值会先剥离行内注释再判断，避免 `null # comment` 被当成普通字符串
    - 引号值必须恰好由一个**未被转义**的开引号和一个**未被转义**的闭引号包住，
      且内部不得再出现未转义的同类引号（末尾 `"foo\\"` 这类转义引号不算闭合）
    """
    value = raw_value.strip()
    if not value:
        return None
    if value.startswith(("|", ">")):
        # block scalar 一律拒绝
        return None
    if value.startswith(("\"", "'")):
        quote = value[0]
        if len(value) < 2:
            return None
        # 收集除开引号外所有"未被转义"的同类引号位置
        unescaped = [
            index
            for index in range(1, len(value))
            if value[index] == quote and not _is_escaped(value, index)
        ]
        # 必须恰好有一个未转义引号，且它就在末尾 -> 否则视为未正确闭合
        if unescaped != [len(value) - 1]:
            return None
        inner = value[1:-1]
        # 还原转义引号（去掉多余反斜杠）后剥空
        inner = inner.replace("\\" + quote, quote).strip()
        return inner or None
    # 无引号：先剥离行内注释，再判断是否为空/null
    uncommented = value.split(" #", 1)[0].strip()
    if not uncommented or uncommented.startswith("#"):
        # 纯注释（如 `name: # comment`）语义为空
        return None
    if uncommented.casefold() == "null" or uncommented == "~":
        return None
    return uncommented


def _inspect_registry(
    home: Path,
    registry: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    checks: list[dict[str, str]] = [{"level": "ok", "message": "skills registry is valid"}]
    adapter_rows: list[dict[str, Any]] = []
    skill_rows: list[dict[str, Any]] = []
    adapter_state: dict[str, tuple[dict[str, Any], Path | None, str | None]] = {}

    for adapter in registry["adapters"]:
        root, reason = _expanded_path(adapter["root"], home, home)
        adapter_state[adapter["id"]] = (adapter, root, reason)
        status = "disabled" if not adapter["enabled"] else "available"
        if reason:
            status = "error"
            checks.append({"level": "error", "message": f"skills adapter {adapter['id']} root {reason}"})
        adapter_rows.append({
            "id": adapter["id"],
            "type": adapter["type"],
            "enabled": adapter["enabled"],
            "root": adapter["root"],
            "status": status,
        })

    enabled_entries_by_adapter = {
        adapter["id"]: any(
            skill["adapter"] == adapter["id"] and skill.get("enabled", True)
            for skill in registry["skills"]
        )
        for adapter in registry["adapters"]
    }
    for adapter_id, (adapter, root, reason) in adapter_state.items():
        if reason or root is None:
            continue
        if not root.exists():
            level = "error" if adapter["enabled"] and enabled_entries_by_adapter[adapter_id] else "warn"
            checks.append({"level": level, "message": f"skills adapter root does not exist: {adapter_id}"})
        elif not root.is_dir():
            checks.append({"level": "error", "message": f"skills adapter root is not a directory: {adapter_id}"})
        if not adapter["enabled"]:
            checks.append({"level": "warn", "message": f"skills adapter disabled: {adapter_id}"})

    for skill in registry["skills"]:
        adapter, root, root_reason = adapter_state[skill["adapter"]]
        enabled = skill.get("enabled", True)
        status = "disabled" if not enabled or not adapter["enabled"] else "configured"
        row: dict[str, Any] = {
            "id": skill["id"],
            "adapter": skill["adapter"],
            "path": skill["path"],
            "backend": skill["backend"],
            "enabled": enabled,
            "status": status,
            "name": None,
            "descriptionPresent": False,
        }
        skill_rows.append(row)
        if root_reason or root is None:
            row["status"] = "error"
            continue

        skill_path, reason = _expanded_path(skill["path"], root, home)
        if reason:
            row["status"] = "error"
            checks.append({"level": "error", "message": f"skill {skill['id']} path {reason}"})
            continue
        assert skill_path is not None
        if not enabled or not adapter["enabled"]:
            checks.append({"level": "warn", "message": f"skill disabled: {skill['id']}"})
            continue
        if not skill_path.is_dir():
            row["status"] = "error"
            checks.append({"level": "error", "message": f"skill directory does not exist: {skill['id']}"})
            continue

        skill_file, reason = _authorize_path(skill_path / "SKILL.md", home)
        if reason:
            row["status"] = "error"
            checks.append({"level": "error", "message": f"skill {skill['id']} SKILL.md {reason}"})
            continue
        assert skill_file is not None
        if not skill_file.is_file():
            row["status"] = "error"
            checks.append({"level": "error", "message": f"skill {skill['id']} is missing SKILL.md"})
            continue
        frontmatter, error = _read_frontmatter(skill_file)
        if error:
            row["status"] = "error"
            checks.append({"level": "error", "message": f"skill {skill['id']} {error}"})
            continue
        assert frontmatter is not None
        name = frontmatter.get("name", "").strip()
        description_present = bool(frontmatter.get("description", "").strip())
        row["name"] = name or None
        row["descriptionPresent"] = description_present
        if not name or not description_present:
            row["status"] = "error"
            checks.append({
                "level": "error",
                "message": f"skill {skill['id']} frontmatter requires non-empty name and description",
            })
        else:
            checks.append({"level": "ok", "message": f"skill is valid: {skill['id']}"})

    return adapter_rows, skill_rows, checks


def _error_payload(path: Path, message: str, *, include_status: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "registry": str(path),
        "runtime": "notImplemented",
        "adapters": [],
        "skills": [],
        "checks": [{"level": "error", "message": message}],
    }
    if include_status:
        payload = {"status": "error", **payload}
    return payload


def _v2_error_payload(path: Path, message: str) -> dict[str, Any]:
    return {
        "status": "error",
        "registry": str(path),
        "version": 2,
        "runtime": "seamCatalog",
        "providers": [],
        "skills": [],
        "conflicts": [],
        "complete": False,
        "warnings": [],
        "checks": [{"level": "error", "message": message}],
    }


def _registry_version(home: Path) -> tuple[Path, Any, str | None]:
    path, registry, error = _read_registry_root(home)
    return path, None if registry is None else registry.get("version"), error


def _list_skills_v2(home: Path, path: Path) -> dict[str, Any]:
    registry, providers_meta, error = load_skill_registry_v2(home)
    if error:
        return _v2_error_payload(path, error)
    assert registry is not None and providers_meta is not None
    snapshot = registry.snapshot()
    skills = [
        {
            "id": summary.name,
            "name": summary.name,
            "description": summary.description,
            "provider": summary.provider,
            "source": summary.source,
            "rank": summary.rank,
            "backend": summary.backend,
            "modelInvocable": summary.invocation.model_invocable,
            "userInvocable": summary.invocation.user_invocable,
        }
        for summary in snapshot.skills
    ]
    conflicts = [
        {
            "name": conflict.name,
            "winnerProvider": conflict.winner_provider,
            "shadowed": [list(item) for item in conflict.shadowed],
        }
        for conflict in snapshot.conflicts
    ]
    checks: list[dict[str, str]] = [{"level": "ok", "message": "skills registry v2 is valid"}]
    checks.extend(
        {"level": "warning", "message": warning}
        for warning in snapshot.warnings
    )
    checks.extend(
        {"level": "warning", "message": f"skills provider disabled: {meta['id']}"}
        for meta in providers_meta
        if meta["status"] == "disabled"
    )
    return {
        "status": "ok",
        "registry": str(path),
        "version": 2,
        "runtime": "seamCatalog",
        "providers": providers_meta,
        "skills": skills,
        "conflicts": conflicts,
        "complete": snapshot.complete,
        "warnings": list(snapshot.warnings),
        "checks": checks,
    }


def list_skills(home: Path = DEFAULT_HOME) -> dict[str, Any]:
    version_path, version, version_error = _registry_version(home)
    if version_error:
        return _error_payload(version_path, version_error, include_status=True)
    if version == 2:
        return _list_skills_v2(home, version_path)

    # v1 继续走原校验和输出路径，保持既有消费者与逐字段输出兼容。
    path, registry, error = _load_registry(home)
    if error:
        return _error_payload(path, error, include_status=True)
    assert registry is not None
    adapters, skills, checks = _inspect_registry(home, registry)
    ok = not any(check["level"] == "error" for check in checks)
    return {
        "status": "ok" if ok else "error",
        "registry": str(path),
        "runtime": "notImplemented",
        "adapters": adapters,
        "skills": skills,
        "checks": checks,
    }


def doctor_skills(home: Path = DEFAULT_HOME) -> tuple[bool, dict[str, Any]]:
    version_path, version, version_error = _registry_version(home)
    if version_error:
        return False, _error_payload(version_path, version_error, include_status=False)
    if version == 2:
        result = _list_skills_v2(home, version_path)
        return result["status"] == "ok", result

    # v1 继续复用原 doctor payload，不增加 version 等新字段。
    path, registry, error = _load_registry(home)
    if error:
        return False, _error_payload(path, error, include_status=False)
    assert registry is not None
    adapters, skills, checks = _inspect_registry(home, registry)
    ok = not any(check["level"] == "error" for check in checks)
    return ok, {
        "registry": str(path),
        "runtime": "notImplemented",
        "adapters": adapters,
        "skills": skills,
        "checks": checks,
    }


def get_skill(name: str, home: Path = DEFAULT_HOME) -> dict[str, Any]:
    path, version, error = _registry_version(home)
    if error:
        return {"status": "error", "message": error}
    if version != 2:
        return {"status": "unsupported", "message": "skills get requires registry version 2"}
    registry, _, error = load_skill_registry_v2(home)
    if error:
        return {"status": "error", "registry": str(path), "message": error}
    assert registry is not None
    definition = registry.get(name)
    if definition is None:
        return {"status": "not_found", "id": name}
    candidate = definition.candidate
    return {
        "status": "ok",
        "id": candidate.name,
        "description": candidate.description,
        "backend": candidate.backend,
        "content": definition.content,
        "truncated": definition.truncated,
        "sizeBytes": definition.size_bytes,
    }


def list_skill_providers(home: Path = DEFAULT_HOME) -> dict[str, Any]:
    path, version, error = _registry_version(home)
    if error:
        return {"status": "error", "message": error}
    if version != 2:
        return {
            "status": "unsupported",
            "message": "skills providers requires registry version 2",
        }
    _, providers_meta, error = load_skill_registry_v2(home)
    if error:
        return {"status": "error", "registry": str(path), "message": error}
    assert providers_meta is not None
    return {"status": "ok", "version": 2, "providers": providers_meta}
