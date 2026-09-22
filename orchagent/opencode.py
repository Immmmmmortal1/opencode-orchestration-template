from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from .paths import DEFAULT_HOME


MANAGED_KEY = "orchAgent"
MANAGED_MARKER = "orchAgent-managed"


def agent_prompt(home: Path = DEFAULT_HOME) -> str:
    return f"Load {home / 'orchAgent.yaml'}, then use its extension registries for hooks, skills, mcp, and knowledge. Do not use any existing orchestrator config."


def candidate_config_paths() -> list[Path]:
    paths: list[Path] = []
    env_path = os.environ.get("OPENCODE_CONFIG")
    if env_path:
        return [Path(os.path.expanduser(env_path))]
    paths.append(Path("~/.config/opencode/opencode.json").expanduser())
    paths.append(Path("~/.config/opencode.local.json").expanduser())
    deduped: list[Path] = []
    seen = set()
    for path in paths:
        key = str(path)
        if key not in seen:
            seen.add(key)
            deduped.append(path)
    return deduped


def existing_config_paths() -> list[Path]:
    return [p for p in candidate_config_paths() if p.exists()]


def primary_config_path() -> Path:
    env_path = os.environ.get("OPENCODE_CONFIG")
    if env_path:
        return Path(os.path.expanduser(env_path))
    default = Path("~/.config/opencode/opencode.json").expanduser()
    default.parent.mkdir(parents=True, exist_ok=True)
    return default


def read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return {}
    return json.loads(text)


def write_json(path: Path, data: dict[str, Any]) -> None:
    write_path = Path(os.path.realpath(path)) if path.is_symlink() else path
    write_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = write_path.with_suffix(write_path.suffix + f".tmp-{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, write_path)


def backup_files(paths: list[Path], home: Path = DEFAULT_HOME) -> Path:
    root = home.parent / f"{home.name}.backups"
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    backup_dir = root / f"opencode-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    backup_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    files = []
    for path in paths:
        if path.is_symlink():
            link_target = os.readlink(path)
            resolved = Path(os.path.realpath(path))
            resolved_existed = resolved.exists()
            backup_path = backup_dir / str(resolved).strip("/").replace("/", "__")
            if resolved_existed:
                shutil.copyfile(resolved, backup_path)
                os.chmod(backup_path, 0o600)
            files.append({
                "type": "symlinkFile",
                "target": str(path),
                "linkTarget": link_target,
                "resolvedTarget": str(resolved),
                "resolvedExisted": resolved_existed,
                "backup": str(backup_path) if resolved_existed else "",
                "existed": True,
            })
            continue
        if not path.exists():
            files.append({"target": str(path), "backup": "", "existed": False})
            continue
        safe_name = str(path).strip("/").replace("/", "__")
        backup_path = backup_dir / safe_name
        shutil.copyfile(path, backup_path)
        os.chmod(backup_path, 0o600)
        files.append({"target": str(path), "backup": str(backup_path), "existed": True})
    manifest = {"version": 1, "kind": "opencode", "createdAt": int(time.time()), "files": files}
    manifest_path = backup_dir / "rollback-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(manifest_path, 0o600)
    return backup_dir


def doctor(home: Path = DEFAULT_HOME) -> dict[str, Any]:
    paths = candidate_config_paths()
    existing = existing_config_paths()
    linked = False
    linked_paths: list[str] = []
    for path in existing:
        try:
            data = read_json(path)
        except Exception:  # noqa: BLE001
            continue
        agent = data.get("agent", {}).get(MANAGED_KEY, {}) if isinstance(data.get("agent"), dict) else {}
        top = data.get(MANAGED_KEY, {}) if isinstance(data.get(MANAGED_KEY), dict) else {}
        entry_path = Path(str(top.get("entry", ""))).expanduser()
        if (
            top.get("enabled") is True
            and top.get("entry") == str(home / "orchAgent.yaml")
            and top.get("marker") == MANAGED_MARKER
            and entry_path.exists()
            and agent.get("prompt") == agent_prompt(home)
            and agent.get("marker") == MANAGED_MARKER
            and agent.get("mode") == "primary"
        ):
            linked = True
            linked_paths.append(str(path))
    return {
        "opencodeConfigEnv": os.environ.get("OPENCODE_CONFIG"),
        "candidatePaths": [str(p) for p in paths],
        "existingPaths": [str(p) for p in existing],
        "primaryPatchTarget": str(primary_config_path()),
        "linked": linked,
        "linkedPaths": linked_paths,
    }


def link(home: Path = DEFAULT_HOME) -> dict[str, Any]:
    target = primary_config_path()
    backup_dir = backup_files([target], home=home)
    data = read_json(target)
    existing_marker = data.get(MANAGED_KEY, {}).get("marker") if isinstance(data.get(MANAGED_KEY), dict) else None
    if MANAGED_KEY in data and existing_marker != MANAGED_MARKER:
        raise RuntimeError("opencode config already has unmanaged orchAgent key; refuse to overwrite")
    agents = data.setdefault("agent", {})
    if not isinstance(agents, dict):
        raise RuntimeError("opencode config field 'agent' is not an object")
    existing_agent_marker = agents.get(MANAGED_KEY, {}).get("marker") if isinstance(agents.get(MANAGED_KEY), dict) else None
    if MANAGED_KEY in agents and existing_agent_marker != MANAGED_MARKER:
        raise RuntimeError("opencode agent.orchAgent already exists without orchAgent-managed marker")
    agents[MANAGED_KEY] = {
        "mode": "primary",
        "description": f"orchAgent 独立入口：读取 {home / 'orchAgent.yaml'} 并加载扩展注册表",
        "prompt": agent_prompt(home),
        "marker": MANAGED_MARKER,
        "permission": {
            "read": "allow",
            "bash": "deny",
            "task": "deny"
        }
    }
    data.setdefault(MANAGED_KEY, {})
    data[MANAGED_KEY] = {
        "enabled": True,
        "entry": str(home / "orchAgent.yaml"),
        "marker": MANAGED_MARKER,
    }
    instructions = data.get("instructions")
    line = f"Load orchAgent entry from {home / 'orchAgent.yaml'} when orchAgent is requested. [{MANAGED_MARKER}]"
    if instructions is None:
        data["instructions"] = line
    elif isinstance(instructions, str) and MANAGED_MARKER not in instructions:
        data["instructions"] = instructions + "\n" + line
    elif not isinstance(instructions, str):
        data.setdefault("orchAgentNotes", {})["instructions"] = line
    write_json(target, data)
    return {"target": str(target), "backupDir": str(backup_dir), "entry": str(home / "orchAgent.yaml")}


def unlink(home: Path = DEFAULT_HOME) -> dict[str, Any]:
    planned: list[tuple[Path, dict[str, Any]]] = []
    expected_entry = str(home / "orchAgent.yaml")
    generated_line = f"Load orchAgent entry from {home / 'orchAgent.yaml'} when orchAgent is requested. [{MANAGED_MARKER}]"
    for path in existing_config_paths():
        data = read_json(path)
        top = data.get(MANAGED_KEY, {}) if isinstance(data.get(MANAGED_KEY), dict) else {}
        agents = data.get("agent")
        agent = agents.get(MANAGED_KEY, {}) if isinstance(agents, dict) and isinstance(agents.get(MANAGED_KEY), dict) else {}
        owns_file = (
            top.get("marker") == MANAGED_MARKER
            and top.get("entry") == expected_entry
        ) or (
            agent.get("marker") == MANAGED_MARKER
            and agent.get("prompt") == agent_prompt(home)
            and top.get("entry") == expected_entry
        )
        if owns_file:
            planned.append((path, data))
    if planned:
        backup_files([path for path, _ in planned], home=home)
    changed: list[str] = []
    for path, data in planned:
        touched = False
        top = data.get(MANAGED_KEY, {}) if isinstance(data.get(MANAGED_KEY), dict) else {}
        agents = data.get("agent")
        agent = agents.get(MANAGED_KEY, {}) if isinstance(agents, dict) and isinstance(agents.get(MANAGED_KEY), dict) else {}
        if top.get("marker") == MANAGED_MARKER:
            data.pop(MANAGED_KEY, None)
            touched = True
        if isinstance(agents, dict) and agent.get("marker") == MANAGED_MARKER:
            agents.pop(MANAGED_KEY, None)
            touched = True
        instructions = data.get("instructions")
        if isinstance(instructions, str) and generated_line in instructions:
            kept = [line for line in instructions.splitlines() if MANAGED_MARKER not in line]
            data["instructions"] = "\n".join(kept)
            touched = True
        notes = data.get("orchAgentNotes")
        if isinstance(notes, dict) and notes.get("instructions") == generated_line:
            notes.pop("instructions", None)
            touched = True
        if touched:
            write_json(path, data)
            changed.append(str(path))
    return {"changed": changed}
