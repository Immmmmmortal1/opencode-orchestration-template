from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

from .backup import create_backup_dir
from .paths import DEFAULT_HOME
from .pipeline import load_pipeline_registry, _validate_registry
from .prompts import (
    load_prompt_file_contents,
    load_prompt_registry,
    prompt_markdown_paths,
    prompt_registry_path,
    render_prompt_registry,
    validate_prompt_registry,
)


MANAGED_KEY = "orchAgent"
MANAGED_MARKER = "orchAgent-managed"

CLASSIFY_QUESTIONS = (
    '{"route":{"type":"choice","instructions":"这是什么类型的任务？","criteria":'
    '{"bug":"修复已有功能的缺陷","feature":"新增功能或改变行为",'
    '"ui_review":"现有 UI 与设计稿对齐","research":"调研/搜索/查资料",'
    '"design":"界面视觉设计","qa":"纯问答/解释，无需改代码"}}}'
)


def agent_prompt(home: Path = DEFAULT_HOME) -> str:
    return "\n".join(
        [
            f"读取 {home / 'orchAgent.yaml'}，只使用其中的 orchAgent 注册表。",
            "收到原始任务后，先调用 subagent `decision`。state 必须逐字传入原始任务，不得概括；questions 必须逐字使用以下 classify JSON，不得自创问题：",
            CLASSIFY_QUESTIONS,
            "读取 decision 的 route 与 confidence：confidence >= 0.85 可继续；0.50 <= confidence < 0.85 可继续但必须标记 review=true；confidence < 0.50、provider error 或超时必须停止并 escalate/询问用户，不得调用 route/run。",
            "只有 bug、feature、ui_review 进入 orchAgent pipeline；research、design、qa 交回宿主现有总路由处理。不得自行猜 task type，也不得让 Jev 选择 pipeline id 或 revision。",
            "先执行 `orchagent pipeline route --task-type <type>`，只接受 status=ok，并读取 pipeline.id 与 pipeline.revision；no_match、conflict、unsupported 或其他错误必须 fail-closed。",
            '再执行 `orchagent pipeline run --pipeline <id> --pipeline-revision <revision> --task-type <type> --new-session-summary "<任务摘要>"`，记住返回的 session id。',
            "按每次 dispatch 返回的 role id 调用 `orchagent-role-<role-id>` subagent；根据当前门禁结果执行 `orchagent pipeline advance --session-id <id> --verdict pass|fail`，重复 dispatch 与 advance 直到 terminal。",
            "禁止跳过 Jev/confidence gate、在 orchAgent 中存储 key/token/secret/env、跳过 gate，或自行改变回退目标。",
        ]
    )


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
            "edit": "deny",
            "bash": {
                "*": "deny",
                "orchagent pipeline route *": "allow",
                "orchagent pipeline run *": "allow",
                "orchagent pipeline advance *": "allow",
            },
            "task": {
                "*": "deny",
                "decision": "allow",
                "orchagent-role-*": "allow",
            },
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


def _pipeline_agent_permission(pipeline: dict[str, Any]) -> dict[str, Any]:
    """仅允许固定管线运行、推进，以及派发该管线实际引用的角色。"""
    direct_run_command = _pipeline_direct_run_command(pipeline)
    task: dict[str, str] = {"*": "deny"}
    for role_id in dict.fromkeys(stage["role"] for stage in pipeline["stages"]):
        task[f"orchagent-role-{role_id}"] = "allow"
    return {
        "read": "allow",
        "edit": "deny",
        "bash": {
            "*": "deny",
            direct_run_command: "allow",
            "orchagent pipeline advance *": "allow",
        },
        "task": task,
    }


def _pipeline_direct_run_command(pipeline: dict[str, Any]) -> str:
    task_types = pipeline.get("route", {}).get("taskTypes", [])
    task_type_arg = f" --task-type {task_types[0]}" if len(task_types) == 1 else ""
    return (
        f"orchagent pipeline direct-run --pipeline {pipeline['id']} "
        f"--pipeline-revision {pipeline['revision']}{task_type_arg}"
    )


def _role_prompt(role: dict[str, Any]) -> str:
    description = role.get("description") or "执行注册表声明的角色职责"
    lines = [
        f"角色职责：{role['id']}，{description}。",
        "只处理主 agent 派发的当前 stage。",
        "返回可用于当前门禁判定的结构化摘要。",
        "不得自行推进 pipeline、调用 pipeline advance，或派发其他 agent。",
    ]
    # verifier / reviewer 内置行为契约（见需求链 State chain 三张表），其余 role 保持通用文本
    if role["id"] == "verifier":
        lines.extend([
            "始终必做（缺任一项即不得给出 pass）：",
            "- 读取 investigate / repro / fix 阶段证据，输出当前问题的测试用例（含：前置条件、操作步骤、预期结果、实际结果）。",
            "- 运行自动化验证并回填证据：执行的命令、退出码、输出摘要。",
            "- 回归检查：确认原问题已消失，且未引入新的回归。",
            "仅当本 stage 的 params 中 runtime=debugbridge 时，追加必做：",
            "- 必须配合 XcodeBuildMCP 完成构建 → 安装 → 启动调试设备（沿用既有原子链：session_set_defaults → session_show_defaults → build_run_device/build_run_sim）。",
            "- 启动完成后必须对同一目标执行 DebugBridge ping；ping 成功才可继续。",
            "- 按测试用例在真机上操作，读取实时 view 层级与当前运行日志作为证据。",
            "- 构建/安装/启动失败、DebugBridge ping 失败或缺少运行时证据时，结论必须是 runtime-blocked：不得转为 pass，不得更换设备，不得降级为截图或静态检查。",
            "runtime-blocked 时宿主不得调用 pipeline advance，也不得把 runtime-blocked 翻译为 fail 推进",
            "——那会沿 verify/fail → repro 空转，repro→fix→verify 重跑后设备/环境仍不可用，白耗 maxAttempts，",
            "甚至促使 fixer 对非代码问题做无效改动；应先解决设备/环境阻塞，再重新执行 verify。",
            "未配置 runtime 时，只要求测试用例与自动化证据，不要求真机验证。",
        ])
    elif role["id"] == "reviewer":
        lines.extend([
            "输入必须包含：用户需求 / 已确认方案、完整 diff、修改文件清单、verifier 的验证报告；缺任一项应返回 blocked，不得猜测。",
            "固定四个审查维度，逐项给出结论：",
            "1. 是否越界：是否改动了需求范围之外的模块、接口或行为。",
            "2. 是否过度设计：是否引入不必要的抽象、配置、状态机或兼容层。",
            "3. 是否属于破坏性修复：是否破坏既有 API / 配置 / 数据格式 / 工作流，且缺少迁移或回退方案。",
            "4. 证据一致性：代码实现是否真的对应 verifier 的测试用例，是否存在假通过。",
            "输出 pass / revise / blocked 三者之一；不得复用 verifier 的 verdict 作为自身结论。",
            "revise 由宿主归一为 fail 调用 advance；blocked 表示无法判定（审查输入不足），此时宿主不得调用 pipeline advance",
            "（不消耗门禁路径、不触发回退边，也不得把 blocked 翻译为 fail 推进——那会错误回退到 fix）；",
            "应由主 agent 补齐缺失输入后重新执行 review，再按 pass / revise 归一为 pass / fail 调用 advance。",
            "若判定问题出在「验证证据不足」而非「代码有问题」，必须在结论中显式标注 reworkTarget=verify。",
        ])
    return "\n".join(lines)


def _pipeline_prompt(pipeline: dict[str, Any], roles: dict[str, dict[str, Any]]) -> str:
    """根据 v2 管线生成可直接交给 opencode 的编排说明。"""
    pipeline_id = pipeline["id"]
    revision = pipeline["revision"]
    lines = [
        f"你正在执行已固定的 orchAgent v2 管线 {pipeline_id} revision {revision}。",
        "此直达入口不得调用 Jev、decision 或 pipeline route；管线与 revision 已固定。",
        "多类型或无类型的直达入口不做语义分类，session 使用兼容类型 `pipeline`；如需 Jev 分类必须使用主 `orchAgent` 入口。",
        "",
        "阶段序列：",
    ]
    for index, stage in enumerate(pipeline["stages"], 1):
        role = roles[stage["role"]]
        model = role.get("model") or "(默认模型)"
        lines.append(
            f"{index}. {stage['id']} → role={stage['role']} → "
            f"{role['provider']}/{model}；门禁：{', '.join(stage['gates'])}"
        )
        # 带 params.runtime 的 stage 追加真机验证提示，未配置则不输出
        if stage.get("params", {}).get("runtime") == "debugbridge":
            lines.append("   此 stage 启用真机验证（runtime=debugbridge），验证证据要求见 verifier 角色契约。")
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
            f"开始：{_pipeline_direct_run_command(pipeline)}",
            "记住命令返回的 session id，并始终把它记在对话里。",
            "按 dispatch 返回的 role id 调用对应 `orchagent-role-<role-id>` subagent。",
            "每完成一个 stage 后调：orchagent pipeline advance --session-id <id> --verdict pass|fail",
            "根据门禁结果沿对应边继续；不要跳过门禁或自行改变回退目标。",
        ]
    )
    return "\n".join(lines)


def sync_agents_from_registry(
    home: Path,
    registry: dict[str, Any],
    prompt_registry: dict[str, Any] | None = None,
    prompt_file_contents: dict[str, str] | None = None,
) -> dict[str, Any]:
    """使用调用方提供的注册表快照同步 agents，不重读注册表文件。"""
    if not isinstance(registry, dict):
        return {"status": "error", "error": "invalid_registry", "message": "pipeline registry is invalid"}
    if registry.get("version") == 1:
        return {"status": "error", "error": "unsupported", "message": "pipeline registry version 1 does not define agent roles"}
    validation_error = _validate_registry(registry)
    if validation_error:
        return {"status": "error", "error": "invalid_registry", "message": validation_error["message"]}

    role_ids = {role["id"] for role in registry["roles"]}
    role_prompts: dict[str, str] = {}
    if prompt_registry is None:
        if prompt_file_contents not in (None, {}):
            return {"status": "error", "error": "invalid_prompt_registry", "message": "prompt registry is invalid"}
    else:
        if not isinstance(prompt_file_contents, dict) or set(prompt_file_contents) != set(prompt_markdown_paths(prompt_registry)):
            return {"status": "error", "error": "invalid_prompt_registry", "message": "prompt registry is invalid"}
        prompt_validation = validate_prompt_registry(home, prompt_registry, prompt_file_contents)
        if prompt_validation["status"] != "ok":
            return {"status": "error", "error": "invalid_prompt_registry", "message": "prompt registry is invalid"}
        if any(target_prompt["id"] not in role_ids for target_prompt in prompt_registry["targets"]):
            return {"status": "error", "error": "invalid_prompt_registry", "message": "prompt registry is invalid"}
        for target_prompt in prompt_registry["targets"]:
            rendered = render_prompt_registry(
                home,
                prompt_registry,
                target_prompt["prompt"],
                target_prompt.get("override"),
                prompt_file_contents,
            )
            if rendered["status"] != "ok":
                return {"status": "error", "error": "invalid_prompt_registry", "message": "prompt registry is invalid"}
            role_prompts[target_prompt["id"]] = rendered["markdown"]

    roles = {role["id"]: role for role in registry["roles"]}
    agent_sources: dict[str, list[dict[str, str]]] = {}
    for role in registry["roles"]:
        agent_sources.setdefault(f"orchagent-role-{role['id']}", []).append({"kind": "role", "id": role["id"]})
    for pipeline in registry["pipelines"]:
        if pipeline["enabled"]:
            agent_sources.setdefault(f"orchagent-{pipeline['id']}", []).append({"kind": "pipeline", "id": pipeline["id"]})
    for agent_id, sources in agent_sources.items():
        if len(sources) > 1:
            return {
                "status": "error",
                "error": "agent_id_conflict",
                "message": f"multiple registry entries derive opencode agent.{agent_id}",
                "agentId": agent_id,
                "sources": sources,
            }

    desired: dict[str, dict[str, Any]] = {}

    def add_desired(agent_id: str, agent: dict[str, Any]) -> None:
        if agent_id in desired:
            raise AssertionError(f"duplicate desired agent id after conflict validation: {agent_id}")
        desired[agent_id] = agent

    for role in registry["roles"]:
        description = f"orchAgent 角色：{role['id']}"
        if isinstance(role.get("description"), str) and role["description"].strip():
            description += f" {role['description']}"
        agent: dict[str, Any] = {
            "mode": "subagent",
            "description": description,
            "prompt": role_prompts.get(role["id"], _role_prompt(role)),
            "marker": MANAGED_MARKER,
            "permission": {"task": "deny"},
        }
        if role.get("model"):
            agent["model"] = f"{role['provider']}/{role['model']}"
        add_desired(f"orchagent-role-{role['id']}", agent)
    for pipeline in registry["pipelines"]:
        if not pipeline["enabled"]:
            continue
        description = f"orchAgent 管线：{pipeline['id']}"
        if isinstance(pipeline.get("description"), str) and pipeline["description"].strip():
            description += f" {pipeline['description']}"
        add_desired(f"orchagent-{pipeline['id']}", {
            "mode": "primary",
            "description": description,
            "prompt": _pipeline_prompt(pipeline, roles),
            "marker": MANAGED_MARKER,
            "permission": _pipeline_agent_permission(pipeline),
        })

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


def sync_agents(home: Path = DEFAULT_HOME) -> dict[str, Any]:
    """从磁盘加载注册表，再把启用的 v2 管线同步为 opencode agents。"""
    registry, error = load_pipeline_registry(home)
    if error:
        return {"status": "error", "error": "invalid_registry", "message": error["message"]}
    assert registry is not None

    prompt_registry: dict[str, Any] | None = None
    prompt_file_contents: dict[str, str] | None = None
    if os.path.lexists(prompt_registry_path(home)):
        prompt_registry, prompt_error = load_prompt_registry(home)
        if prompt_error:
            return {"status": "error", "error": "invalid_prompt_registry", "message": "prompt registry is invalid"}
        assert prompt_registry is not None
        try:
            prompt_file_contents = load_prompt_file_contents(home, prompt_registry)
        except ValueError:
            return {"status": "error", "error": "invalid_prompt_registry", "message": "prompt registry is invalid"}
    return sync_agents_from_registry(home, registry, prompt_registry, prompt_file_contents)


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
