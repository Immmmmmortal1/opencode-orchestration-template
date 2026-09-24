from __future__ import annotations

from typing import Any


SUPPORTED_HOOK_ADAPTER_TYPES = {"builtin"}


def diagnostic(level: str, message: str, **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {"level": level, "message": message}
    item.update(extra)
    return item


def adapter_status(adapter: dict[str, Any]) -> dict[str, Any]:
    adapter_id = adapter.get("id", "")
    adapter_type = adapter.get("type", "")
    enabled_value = adapter.get("enabled", False)
    if not isinstance(adapter_id, str) or not adapter_id:
        return {
            "id": str(adapter_id),
            "type": str(adapter_type),
            "enabled": False,
            "status": "unavailable",
            "reason": "adapter id must be non-empty string",
        }
    if not isinstance(adapter_type, str):
        return {
            "id": adapter_id,
            "type": str(adapter_type),
            "enabled": False,
            "status": "unavailable",
            "reason": "adapter type must be string",
        }
    if not isinstance(enabled_value, bool):
        return {
            "id": adapter_id,
            "type": adapter_type,
            "enabled": False,
            "status": "unavailable",
            "reason": "adapter enabled must be boolean",
        }
    enabled = enabled_value
    available = enabled and adapter_type in SUPPORTED_HOOK_ADAPTER_TYPES
    reason = "available" if available else "disabled" if not enabled else f"unsupported adapter type: {adapter_type}"
    return {
        "id": adapter_id,
        "type": adapter_type,
        "enabled": enabled,
        "status": "available" if available else "unavailable",
        "reason": reason,
    }


def hook_enabled_status(hook: dict[str, Any]) -> tuple[bool, str | None]:
    enabled = hook.get("enabled", False)
    if not isinstance(enabled, bool):
        return False, "hook enabled must be boolean"
    return enabled, None


def missing_adapter_status(adapter_id: str) -> dict[str, Any]:
    return {
        "id": adapter_id,
        "type": "missing",
        "enabled": False,
        "status": "unavailable",
        "reason": "adapter not found",
    }
