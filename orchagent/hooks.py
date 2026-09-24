from __future__ import annotations

from pathlib import Path
from typing import Any

from .adapter_contract import adapter_status, diagnostic, hook_enabled_status, missing_adapter_status
from .config import extension_registry_paths, read_text_config
from .paths import DEFAULT_HOME


def hooks_registry_path(home: Path = DEFAULT_HOME) -> Path:
    return extension_registry_paths(home)["hooks"]


def load_hooks_registry(home: Path = DEFAULT_HOME) -> dict[str, Any]:
    return read_text_config(hooks_registry_path(home))


def adapters_by_id(registry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {str(adapter.get("id", "")): adapter for adapter in registry.get("adapters", [])}


def list_hooks(home: Path = DEFAULT_HOME) -> list[dict[str, Any]]:
    registry = load_hooks_registry(home)
    adapters = adapters_by_id(registry)
    rows: list[dict[str, Any]] = []
    for hook in registry.get("hooks", []):
        adapter_id = str(hook.get("adapter", ""))
        adapter = adapters.get(adapter_id, {})
        status = adapter_status(adapter) if adapter else missing_adapter_status(adapter_id)
        enabled, enabled_error = hook_enabled_status(hook)
        rows.append({
            "id": hook.get("id", ""),
            "adapter": adapter_id,
            "enabled": enabled,
            "events": hook.get("events", []),
            "runtime": "dryRunOnly",
            "adapterStatus": status["status"],
            "adapterReason": enabled_error or status["reason"],
        })
    return rows


def doctor_hooks(home: Path = DEFAULT_HOME) -> tuple[bool, dict[str, Any]]:
    registry = load_hooks_registry(home)
    adapters = registry.get("adapters", [])
    hooks = registry.get("hooks", [])
    checks: list[dict[str, Any]] = []
    ok = True

    if registry.get("version") != 1:
        ok = False
        checks.append(diagnostic("error", "hooks registry version must be 1"))

    adapter_map = adapters_by_id(registry)
    adapter_rows = []
    for adapter in adapters:
        status = adapter_status(adapter)
        adapter_rows.append(status)
        if status["status"] != "available":
            is_plain_disabled = status["reason"] == "disabled"
            ok = False if not is_plain_disabled else ok
            checks.append(diagnostic("warn" if is_plain_disabled else "error", status["reason"], adapter=status["id"]))

    for hook in hooks:
        hook_id = hook.get("id", "")
        adapter_id = str(hook.get("adapter", ""))
        events = hook.get("events", [])
        if not hook_id:
            ok = False
            checks.append(diagnostic("error", "hook missing id"))
        if adapter_id not in adapter_map:
            ok = False
            checks.append(diagnostic("error", "hook references missing adapter", hook=hook_id, adapter=adapter_id))
        _, enabled_error = hook_enabled_status(hook)
        if enabled_error:
            ok = False
            checks.append(diagnostic("error", enabled_error, hook=hook_id))
        if not isinstance(events, list) or not events:
            ok = False
            checks.append(diagnostic("error", "hook must declare non-empty events", hook=hook_id))

    checks.append(diagnostic("ok", f"{len(adapters)} adapters declared"))
    checks.append(diagnostic("ok", f"{len(hooks)} hooks declared"))
    return ok, {
        "registry": str(hooks_registry_path(home)),
        "adapters": adapter_rows,
        "checks": checks,
    }


def dry_run_hooks(event: str, home: Path = DEFAULT_HOME) -> dict[str, Any]:
    registry = load_hooks_registry(home)
    adapters = adapters_by_id(registry)
    planned: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for hook in registry.get("hooks", []):
        hook_id = str(hook.get("id", ""))
        adapter_id = str(hook.get("adapter", ""))
        adapter = adapters.get(adapter_id, {})
        status = adapter_status(adapter) if adapter else missing_adapter_status(adapter_id)
        enabled, enabled_error = hook_enabled_status(hook)
        events = hook.get("events", []) if isinstance(hook.get("events", []), list) else []
        item = {
            "id": hook_id,
            "adapter": adapter_id,
            "event": event,
            "wouldRun": False,
            "mode": "dry-run",
        }
        if enabled_error:
            item["reason"] = enabled_error
            skipped.append(item)
        elif status["status"] != "available":
            item["reason"] = status["reason"]
            skipped.append(item)
        elif not enabled:
            item["reason"] = "hook disabled"
            skipped.append(item)
        elif event not in events:
            item["reason"] = "event not declared for hook"
            skipped.append(item)
        else:
            item["wouldRun"] = True
            item["reason"] = "enabled hook matches event"
            planned.append(item)

    return {
        "event": event,
        "mode": "dry-run",
        "planned": planned,
        "skipped": skipped,
    }
