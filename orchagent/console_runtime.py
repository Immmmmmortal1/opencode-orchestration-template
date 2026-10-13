from __future__ import annotations

from pathlib import Path
from typing import Any, Protocol

from .paths import DEFAULT_HOME


RESOLUTION_STATUSES = frozenset({"resolved", "unresolved", "unknown"})


class HostResolutionAdapter(Protocol):
    def resolve(self, provider: str, model: str | None) -> str: ...


def resolve_host_status(
    home: Path = DEFAULT_HOME,
    provider: str = "",
    model: str | None = None,
    *,
    adapter: HostResolutionAdapter | None = None,
) -> dict[str, Any]:
    """通过显式只读 adapter 解析宿主状态；默认及异常均为 unknown。"""
    del home  # seam 保留 home 上下文，但默认实现不得读取本机配置。
    status = "unknown"
    if adapter is not None:
        try:
            candidate = adapter.resolve(provider, model)
            if candidate in RESOLUTION_STATUSES:
                status = candidate
        except Exception:  # noqa: BLE001
            status = "unknown"
    return {"provider": provider, "model": model, "resolvedStatus": status}


def list_host_resolution(
    home: Path = DEFAULT_HOME,
    roles: list[dict[str, Any]] | None = None,
    *,
    adapter: HostResolutionAdapter | None = None,
) -> list[dict[str, Any]]:
    return [
        resolve_host_status(
            home,
            str(role.get("provider", "")),
            role.get("model") if isinstance(role.get("model"), str) else None,
            adapter=adapter,
        )
        for role in (roles or [])
    ]
