from __future__ import annotations

from pathlib import Path
from typing import Any

from .config import extension_registry_paths, read_text_config
from .paths import DEFAULT_HOME


SUPPORTED_ADAPTER_TYPES = {"opencode"}
SUPPORTED_SERVER_TYPES = {"local", "remote"}
REGISTRY_FIELDS = {"version", "adapters", "servers"}
ADAPTER_FIELDS = {"id", "type", "enabled"}
COMMON_SERVER_FIELDS = {"id", "adapter", "type", "enabled"}
LOCAL_SERVER_FIELDS = COMMON_SERVER_FIELDS | {"command", "env"}
REMOTE_SERVER_FIELDS = COMMON_SERVER_FIELDS | {"url", "oauth", "headers"}


def mcp_registry_path(home: Path = DEFAULT_HOME) -> Path:
    return extension_registry_paths(home)["mcp"]


def _registry_fallback_path(home: Path) -> Path:
    return home / "extensions" / "mcp.yaml"


def _load_registry(home: Path) -> tuple[Path, dict[str, Any] | None, str | None]:
    try:
        path = mcp_registry_path(home)
    except Exception as exc:  # noqa: BLE001
        return _registry_fallback_path(home), None, f"failed to resolve MCP registry path: {exc}"

    try:
        registry = read_text_config(path)
    except Exception as exc:  # noqa: BLE001
        return path, None, f"failed to read MCP registry: {exc}"
    if not isinstance(registry, dict):
        return path, None, "MCP registry root must be an object"

    unknown_fields = sorted(set(registry) - REGISTRY_FIELDS)
    if unknown_fields:
        return path, None, f"MCP registry has unknown fields: {', '.join(unknown_fields)}"
    adapters = registry.get("adapters")
    servers = registry.get("servers")
    if registry.get("version") != 1:
        return path, None, "MCP registry version must be 1"
    if not isinstance(adapters, list):
        return path, None, "MCP adapters must be a list"
    if not isinstance(servers, list):
        return path, None, "MCP servers must be a list"

    adapter_ids: set[str] = set()
    for index, adapter in enumerate(adapters):
        if not isinstance(adapter, dict):
            return path, None, f"MCP adapter at index {index} must be an object"
        adapter_id = adapter.get("id")
        unknown_fields = sorted(set(adapter) - ADAPTER_FIELDS)
        if unknown_fields:
            return path, None, f"MCP adapter {adapter_id} has unknown fields: {', '.join(unknown_fields)}"
        adapter_type = adapter.get("type")
        enabled = adapter.get("enabled")
        if not isinstance(adapter_id, str) or not adapter_id.strip():
            return path, None, f"MCP adapter at index {index} id must be non-empty string"
        if adapter_id in adapter_ids:
            return path, None, f"duplicate MCP adapter id: {adapter_id}"
        if adapter_type not in SUPPORTED_ADAPTER_TYPES:
            return path, None, f"unsupported MCP adapter type: {adapter_type}"
        if not isinstance(enabled, bool):
            return path, None, f"MCP adapter {adapter_id} enabled must be boolean"
        adapter_ids.add(adapter_id)

    server_ids: set[str] = set()
    for index, server in enumerate(servers):
        if not isinstance(server, dict):
            return path, None, f"MCP server at index {index} must be an object"
        server_id = server.get("id")
        adapter_id = server.get("adapter")
        server_type = server.get("type")
        enabled = server.get("enabled")
        if not isinstance(server_id, str) or not server_id.strip():
            return path, None, f"MCP server at index {index} id must be non-empty string"
        if server_id in server_ids:
            return path, None, f"duplicate MCP server id: {server_id}"
        if not isinstance(adapter_id, str) or adapter_id not in adapter_ids:
            return path, None, f"MCP server {server_id} references missing adapter"
        if not isinstance(enabled, bool):
            return path, None, f"MCP server {server_id} enabled must be boolean"
        if server_type not in SUPPORTED_SERVER_TYPES:
            return path, None, f"unsupported MCP server type: {server_type}"

        allowed_fields = LOCAL_SERVER_FIELDS if server_type == "local" else REMOTE_SERVER_FIELDS
        unknown_fields = sorted(set(server) - allowed_fields)
        if unknown_fields:
            return path, None, f"MCP server {server_id} has unknown fields: {', '.join(unknown_fields)}"

        if server_type == "local":
            command = server.get("command")
            if (
                not isinstance(command, list)
                or not command
                or any(not isinstance(item, str) or not item.strip() for item in command)
            ):
                return path, None, f"MCP local server {server_id} command must be a non-empty list of non-empty strings"
            if "env" in server and not isinstance(server["env"], dict):
                return path, None, f"MCP local server {server_id} env must be an object"
        else:
            url = server.get("url")
            if not isinstance(url, str) or not url.strip():
                return path, None, f"MCP remote server {server_id} url must be non-empty string"
            if "oauth" in server and not isinstance(server["oauth"], bool):
                return path, None, f"MCP remote server {server_id} oauth must be boolean"
            if "headers" in server:
                headers = server["headers"]
                if not isinstance(headers, dict) or any(
                    not isinstance(key, str) or not isinstance(value, str)
                    for key, value in headers.items()
                ):
                    return path, None, f"MCP remote server {server_id} headers must be an object with string keys and values"
        server_ids.add(server_id)

    return path, registry, None


def _adapter_rows(registry: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "id": adapter["id"],
            "type": adapter["type"],
            "enabled": adapter["enabled"],
            "status": "available" if adapter["enabled"] else "disabled",
        }
        for adapter in registry["adapters"]
    ]


def _server_rows(registry: dict[str, Any]) -> list[dict[str, Any]]:
    adapters = {adapter["id"]: adapter for adapter in registry["adapters"]}
    rows: list[dict[str, Any]] = []
    for server in registry["servers"]:
        active = server["enabled"] and adapters[server["adapter"]]["enabled"]
        rows.append({
            "id": server["id"],
            "adapter": server["adapter"],
            "type": server["type"],
            "enabled": server["enabled"],
            "status": "configured" if active else "disabled",
        })
    return rows


def _error_payload(path: Path, message: str, *, include_status: bool) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "registry": str(path),
        "runtime": "notImplemented",
        "adapters": [],
        "servers": [],
        "checks": [{"level": "error", "message": message}],
    }
    if include_status:
        payload = {"status": "error", **payload}
    return payload


def list_mcp(home: Path = DEFAULT_HOME) -> dict[str, Any]:
    path, registry, error = _load_registry(home)
    if error:
        return _error_payload(path, error, include_status=True)
    assert registry is not None
    return {
        "status": "ok",
        "registry": str(path),
        "runtime": "notImplemented",
        "adapters": _adapter_rows(registry),
        "servers": _server_rows(registry),
        "checks": [{"level": "ok", "message": "MCP registry is valid"}],
    }


def doctor_mcp(home: Path = DEFAULT_HOME) -> tuple[bool, dict[str, Any]]:
    path, registry, error = _load_registry(home)
    if error:
        return False, _error_payload(path, error, include_status=False)
    assert registry is not None

    checks: list[dict[str, Any]] = [{"level": "ok", "message": "MCP registry is valid"}]
    for adapter in registry["adapters"]:
        if not adapter["enabled"]:
            checks.append({"level": "warn", "message": f"MCP adapter disabled: {adapter['id']}"})
    for server in registry["servers"]:
        if not server["enabled"]:
            checks.append({"level": "warn", "message": f"MCP server disabled: {server['id']}"})

    return True, {
        "registry": str(path),
        "runtime": "notImplemented",
        "adapters": _adapter_rows(registry),
        "servers": _server_rows(registry),
        "checks": checks,
    }
