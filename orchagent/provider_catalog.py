from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SENSITIVE_NAME = re.compile(
    r"(?i)(?:api[._ -]?key|access[._ -]?token|client[._ -]?secret|authorization|token|secret|password|credential|bearer|env)"
)
_ENV_REFERENCE = re.compile(r"(?i)\$\{[^}]+\}|\$[A-Z_][A-Z0-9_]*|\bprocess\.env\b")
_URL = re.compile(r"(?i)\b(?:https?|wss?)://|\b[a-z0-9.-]+\.(?:com|dev|net|org)(?:/|$)")


def candidate_config_paths() -> list[Path]:
    configured = os.environ.get("OPENCODE_CONFIG")
    if configured:
        return [Path(os.path.expanduser(configured))]
    return [
        Path("~/.config/opencode/opencode.json").expanduser(),
        Path("~/.config/opencode.local.json").expanduser(),
    ]


def _safe_id(value: Any) -> str | None:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        return None
    if _SENSITIVE_NAME.search(value) or _ENV_REFERENCE.search(value) or _URL.search(value):
        return None
    return value


def _catalog_from_config(config: Any) -> list[dict[str, Any]]:
    if not isinstance(config, dict):
        return []
    providers = config.get("provider")
    if not isinstance(providers, dict):
        return []
    result: list[dict[str, Any]] = []
    for provider_key in sorted((key for key in providers if isinstance(key, str))):
        provider_id = _safe_id(provider_key)
        if provider_id is None:
            continue
        value = providers[provider_key]
        if not isinstance(value, dict):
            result.append({"id": provider_id, "name": provider_id, "models": []})
            continue
        models = value.get("models")
        model_items: list[dict[str, str]] = []
        if isinstance(models, dict):
            for model_key in sorted(key for key in models if isinstance(key, str)):
                model_id = _safe_id(model_key)
                if model_id is None:
                    continue
                model = models[model_key]
                if isinstance(model, dict):
                    model_items.append({"id": model_id, "name": model_id})
                else:
                    model_items.append({"id": model_id, "name": model_id})
        result.append({
            "id": provider_id,
            "name": provider_id,
            "models": model_items,
        })
    return result


def load_catalog() -> dict[str, Any]:
    """读取宿主配置的 provider/model 名录；不执行任何宿主动作。"""
    paths = candidate_config_paths()
    merged: dict[str, Any] = {}
    found = False
    for path in paths:
        try:
            if not path.is_file():
                continue
            config = json.loads(path.read_text(encoding="utf-8"))
            found = True
            if isinstance(config, dict) and isinstance(config.get("provider"), dict):
                merged.setdefault("provider", {}).update(config["provider"])
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {"status": "ok", "providers": [], "sourceStatus": "unavailable"}
    if not found:
        return {"status": "ok", "providers": [], "sourceStatus": "unavailable"}
    return {"status": "ok", "providers": _catalog_from_config(merged), "sourceStatus": "available"}
