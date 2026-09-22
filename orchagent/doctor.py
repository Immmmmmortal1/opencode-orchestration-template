from __future__ import annotations

from pathlib import Path

from .config import validate_all
from .extensions import list_extensions
from .paths import DEFAULT_HOME


def run_doctor(home: Path = DEFAULT_HOME) -> tuple[bool, list[str]]:
    messages: list[str] = []
    ok = True
    if not home.exists():
        return False, [f"runtime home missing: {home}"]
    for dirname in ["state", "sessions", "locks", "leases", "logs", "backups"]:
        path = home / dirname
        if path.exists():
            messages.append(f"ok: {path}")
        else:
            ok = False
            messages.append(f"missing: {path}")
    errors = validate_all(home)
    if errors:
        ok = False
        messages.extend([f"config error: {e}" for e in errors])
    else:
        messages.append("ok: config validate")
    for row in list_extensions(home):
        messages.append(f"extension {row['type']}: {row['status']} adapters={row.get('adapters', 0)} items={row.get('items', 0)}")
    return ok, messages
