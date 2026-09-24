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
        item_key = {
            "hooks": "hooks",
            "skills": "skills",
            "mcp": "servers",
            "knowledge": "sources",
        }[kind]
        items = data.get(item_key, [])
        runtime = "dryRunOnly" if kind == "hooks" else "notImplemented"
        rows.append({
            "type": kind,
            "status": "declared",
            "path": str(path),
            "adapters": len(data.get("adapters", [])),
            "items": len(items),
            "runtime": runtime,
        })
    return rows
