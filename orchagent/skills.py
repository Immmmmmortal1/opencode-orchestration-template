from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

from .config import extension_registry_paths, read_text_config
from .paths import DEFAULT_HOME


SUPPORTED_ADAPTER_TYPES = {"filesystem"}
REGISTRY_FIELDS = {"version", "adapters", "skills"}
ADAPTER_FIELDS = {"id", "type", "enabled", "root"}
SKILL_FIELDS = {"id", "adapter", "path", "enabled"}
SENSITIVE_PATH_PARTS = {"secrets", "api-keys", "mail", "accounts"}


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
        if "enabled" in skill and not isinstance(skill["enabled"], bool):
            return path, None, f"skill {skill_id} enabled must be boolean"
        skill_ids.add(skill_id)

    return path, registry, None


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
            with os.fdopen(fd, "r", encoding="utf-8") as stream:
                fd = -1
                lines = stream.read().splitlines()
        finally:
            if fd >= 0:
                os.close(fd)
    except (OSError, UnicodeError) as exc:
        return None, f"failed to read SKILL.md: {exc}"

    if not lines or lines[0].strip() != "---":
        return None, "SKILL.md must start with YAML frontmatter"
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


def list_skills(home: Path = DEFAULT_HOME) -> dict[str, Any]:
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
