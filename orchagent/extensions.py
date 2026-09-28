from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import extension_registry_paths, read_text_config
from .paths import DEFAULT_HOME


EXTENSION_TYPES = ["hooks", "skills", "mcp", "knowledge"]


def list_extensions(home: Path = DEFAULT_HOME) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    registry_paths = extension_registry_paths(home)
    for kind in EXTENSION_TYPES:
        path = registry_paths[kind]
        if not path.exists():
            rows.append({"type": kind, "status": "missing", "path": str(path)})
            continue
        data = read_text_config(path)
        if not isinstance(data, dict):
            # registry 根节点必须是对象；其它合法 JSON（[]/null/字符串等）fail-closed，
            # 不能让扩展列表因为畸形配置直接抛异常。
            rows.append({
                "type": kind,
                "status": "error",
                "path": str(path),
                "reason": "registry root must be an object",
            })
            continue
        item_key = {
            "hooks": "hooks",
            "skills": "skills",
            "mcp": "servers",
            "knowledge": "sources",
        }[kind]
        items = data.get(item_key, [])
        runtime = {
            "hooks": "dryRunOnly",
            "knowledge": "searchOnly",
        }.get(kind, "notImplemented")
        rows.append({
            "type": kind,
            "status": "declared",
            "path": str(path),
            "adapters": len(data.get("adapters", [])),
            "items": len(items),
            "runtime": runtime,
        })
    return rows
