from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .paths import DEFAULT_HOME, expand_path


REQUIRED_TOP_LEVEL = ["version", "core", "entrypoints", "extensions", "opencode", "state"]
EXTENSION_FILES = ["hooks", "skills", "mcp", "knowledge"]


def read_text_config(path: Path) -> dict[str, Any]:
    """Read JSON-or-YAML-lite config.

    The first version intentionally keeps configs JSON-compatible YAML so we can
    validate with Python standard library only. This avoids bootstrapping a YAML
    dependency during install.
    """
    text = path.read_text(encoding="utf-8")
    return json.loads(text)


def load_main_config(path: Path | None = None) -> dict[str, Any]:
    config_path = path or DEFAULT_HOME / "orchAgent.yaml"
    return read_text_config(expand_path(config_path))


def extension_registry_paths(home: Path = DEFAULT_HOME) -> dict[str, Path]:
    config = load_main_config(home / "orchAgent.yaml")
    extensions = config.get("extensions", {})
    paths: dict[str, Path] = {}
    for name in EXTENSION_FILES:
        raw = extensions.get(name)
        if raw:
            paths[name] = expand_path(raw)
        else:
            paths[name] = home / "extensions" / f"{name}.yaml"
    return paths


def validate_main_config(config: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for key in REQUIRED_TOP_LEVEL:
        if key not in config:
            errors.append(f"missing top-level key: {key}")
    if config.get("name") not in (None, "orchAgent"):
        errors.append("name must be orchAgent when present")
    extensions = config.get("extensions", {})
    if not isinstance(extensions, dict):
        errors.append("extensions must be object")
    else:
        for key in EXTENSION_FILES:
            if key not in extensions:
                errors.append(f"missing extensions.{key}")
    return errors


def validate_extension_registry(path: Path) -> list[str]:
    errors: list[str] = []
    data = read_text_config(path)
    if data.get("version") != 1:
        errors.append(f"{path}: version must be 1")
    if "adapters" not in data:
        errors.append(f"{path}: missing adapters")
    return errors


def validate_all(home: Path = DEFAULT_HOME) -> list[str]:
    errors: list[str] = []
    main_path = home / "orchAgent.yaml"
    if not main_path.exists():
        return [f"missing main config: {main_path}"]
    try:
        config = read_text_config(main_path)
        errors.extend(validate_main_config(config))
    except Exception as exc:  # noqa: BLE001
        return [f"failed to parse main config: {exc}"]

    try:
        registry_paths = extension_registry_paths(home)
    except Exception as exc:  # noqa: BLE001
        return [f"failed to resolve extension registry paths: {exc}"]

    for name in EXTENSION_FILES:
        ext_path = registry_paths[name]
        if not ext_path.exists():
            errors.append(f"missing extension registry: {ext_path}")
            continue
        try:
            errors.extend(validate_extension_registry(ext_path))
        except Exception as exc:  # noqa: BLE001
            errors.append(f"failed to parse {ext_path}: {exc}")
    return errors
