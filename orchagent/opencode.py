from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .backup import create_backup_dir
from .paths import DEFAULT_HOME
from .pipeline import load_pipeline_registry, _validate_registry


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
    backup_dir, created_at_ns, sequence = create_backup_dir(root, "opencode")
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
    manifest = {
        "version": 1,
        "kind": "opencode",
        "createdAt": created_at_ns // 1_000_000_000,
        "createdAtNs": created_at_ns,
        "sequence": sequence,
        "files": files,
    }
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


def _agent_permission() -> dict[str, str]:
    """返回与现有 orchAgent 入口一致的最小权限。"""
    return {"read": "allow", "bash": "deny", "task": "deny"}


def _pipeline_prompt(pipeline: dict[str, Any], roles: dict[str, dict[str, Any]]) -> str:
    """根据 v2 管线生成可直接交给 opencode 的编排说明。"""
    pipeline_id = pipeline["id"]
    lines = [f"你正在执行 orchAgent v2 管线 {pipeline_id}。", "", "阶段序列："]
    for index, stage in enumerate(pipeline["stages"], 1):
        role = roles[stage["role"]]
        model = role.get("model") or "(默认模型)"
        lines.append(
            f"{index}. {stage['id']} → role={stage['role']} → "
            f"{role['provider']}/{model}；门禁：{', '.join(stage['gates'])}"
        )
    lines.extend(["", "门禁与回退边："])
    for edge in pipeline["edges"]:
        source = edge["from"]
        target = edge["to"]
        destination = target.get("stage") or target.get("terminal") or "失败"
        lines.append(
            f"- {source['stage']} / {source['gate']} / verdict={source['verdict']} "
            f"→ {destination}"
        )
    lines.extend(
        [
            "",
            f'开始：pipeline run --pipeline {pipeline_id} --new-session-summary "<任务>"',
            "记住命令返回的 session id，并始终把它记在对话里。",
            "每完成一个 stage 后调：pipeline advance --session-id <id> --verdict pass|fail",
            "根据门禁结果沿对应边继续；不要跳过门禁或自行改变回退目标。",
        ]
    )
    return "\n".join(lines)


def sync_agents(home: Path = DEFAULT_HOME) -> dict[str, Any]:
    """把启用的 v2 管线同步为受 marker 保护的 opencode agents。"""
    registry, error = load_pipeline_registry(home)
    if error:
        return {"status": "error", "error": "invalid_registry", "message": error["message"]}
    assert registry is not None
    if registry.get("version") == 1:
        return {"status": "error", "error": "unsupported", "message": "pipeline registry version 1 does not define agent roles"}
    validation_error = _validate_registry(registry)
    if validation_error:
        return {"status": "error", "error": "invalid_registry", "message": validation_error["message"]}

    roles = {role["id"]: role for role in registry["roles"]}
    desired: dict[str, dict[str, Any]] = {}
    for pipeline in registry["pipelines"]:
        if not pipeline["enabled"]:
            continue
        description = f"orchAgent 管线：{pipeline['id']}"
        if isinstance(pipeline.get("description"), str) and pipeline["description"].strip():
            description += f" {pipeline['description']}"
        desired[f"orchagent-{pipeline['id']}"] = {
            "mode": "primary",
            "description": description,
            "prompt": _pipeline_prompt(pipeline, roles),
            "marker": MANAGED_MARKER,
            "permission": _agent_permission(),
        }

    target = primary_config_path()
    data = read_json(target)
    agents = data.get("agent", {})
    if not isinstance(agents, dict):
        return {"status": "error", "error": "invalid_config", "message": "opencode config field 'agent' is not an object"}
    for agent_id in desired:
        existing = agents.get(agent_id)
        if existing is not None and (not isinstance(existing, dict) or existing.get("marker") != MANAGED_MARKER):
            return {"status": "error", "error": "unmanaged_conflict", "message": f"opencode agent.{agent_id} exists without orchAgent-managed marker"}

    removed = [
        agent_id for agent_id, value in agents.items()
        if str(agent_id).startswith("orchagent-")
        and isinstance(value, dict)
        and value.get("marker") == MANAGED_MARKER
        and agent_id not in desired
    ]
    changed = [agent_id for agent_id, value in desired.items() if agents.get(agent_id) != value]
    changed.extend(removed)
    if not changed:
        return {"status": "ok", "target": str(target), "changed": [], "backupDir": None}
    backup_dir = backup_files([target], home=home)
    for agent_id in removed:
        agents.pop(agent_id, None)
    agents.update(desired)
    data["agent"] = agents
    write_json(target, data)
    return {"status": "ok", "target": str(target), "changed": changed, "backupDir": str(backup_dir)}


def unlink_agents(home: Path = DEFAULT_HOME) -> dict[str, Any]:
    """删除所有带 orchAgent-managed marker 的管线 agents。"""
    planned: list[tuple[Path, dict[str, Any]]] = []
    for path in existing_config_paths():
        data = read_json(path)
        agents = data.get("agent")
        if isinstance(agents, dict) and any(
            isinstance(value, dict) and value.get("marker") == MANAGED_MARKER
            and str(agent_id).startswith("orchagent-")
            for agent_id, value in agents.items()
        ):
            planned.append((path, data))
    if planned:
        backup_files([path for path, _ in planned], home=home)
    changed: list[str] = []
    for path, data in planned:
        agents = data["agent"]
        removed = [agent_id for agent_id, value in agents.items() if str(agent_id).startswith("orchagent-") and isinstance(value, dict) and value.get("marker") == MANAGED_MARKER]
        for agent_id in removed:
            agents.pop(agent_id, None)
        if removed:
            write_json(path, data)
            changed.append(str(path))
    return {"changed": changed}
