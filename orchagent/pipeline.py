from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable
import uuid

from .builtin_skills import BUILTIN_SKILL_IMPLEMENTATIONS, execute_builtin
from .config import extension_registry_paths, read_text_config
from .paths import DEFAULT_HOME
from .session import (
    TERMINAL_STATUSES,
    LEASE_TTL_MS,
    acquire_lease,
    create_session,
    finalize_session,
    load_session,
    renew_lease,
    release_lease,
    save_session,
)
from .skills import list_skills


REGISTRY_FIELDS = {"version", "pipelines"}
V2_REGISTRY_FIELDS = {"version", "roles", "pipelines"}
ROLE_FIELDS = {"id", "provider", "model", "description"}
PIPELINE_FIELDS = {"id", "revision", "enabled", "entryStage", "stages", "gates", "edges"}
STAGE_FIELDS = {"id", "skill", "maxAttempts", "params", "gates"}
V2_STAGE_FIELDS = {"id", "role", "maxAttempts", "params", "gates"}
GATE_FIELDS = {"id", "stage", "evaluator", "params"}
EDGE_FIELDS = {"from", "to"}
EDGE_FROM_FIELDS = {"stage", "gate", "verdict"}
EDGE_TO_FIELDS = {"terminal", "stage"}
VERDICTS = {"pass", "fail"}
TERMINALS = {"succeeded"}
LEASE_RENEW_THRESHOLD_DIVISOR = 3


Clock = int | Callable[[], int] | None


def pipeline_registry_path(home: Path = DEFAULT_HOME) -> Path:
    return extension_registry_paths(home)["pipeline"]


def _fallback_path(home: Path) -> Path:
    return home / "extensions" / "pipeline.yaml"


def _runtime_for_version(version: Any) -> str:
    """按管线 registry 版本标记运行时能力，无法确定时保持 fail-closed。"""
    if isinstance(version, bool) or not isinstance(version, int):
        return "unknown"
    return {1: "builtinOnly", 2: "dispatchOnly"}.get(version, "unknown")


def _error(message: str) -> dict[str, str]:
    return {"level": "error", "message": message}


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _unknown_fields(value: dict[str, Any], allowed: set[str], label: str) -> dict[str, str] | None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        return _error(f"{label} has unknown fields: {', '.join(unknown)}")
    return None


def load_pipeline_registry(home: Path = DEFAULT_HOME) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    try:
        path = pipeline_registry_path(home)
    except Exception as exc:  # noqa: BLE001
        return None, _error(f"failed to resolve pipeline registry path: {exc}")
    try:
        registry = read_text_config(path)
    except Exception as exc:  # noqa: BLE001
        return None, _error(f"failed to read pipeline registry: {exc}")
    if not isinstance(registry, dict):
        return None, _error("pipeline registry root must be an object")
    return registry, None


def _validate_registry(
    registry: dict[str, Any],
    skills_lookup: Mapping[str, Any] | None = None,
) -> dict[str, str] | None:
    version = registry.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version not in {1, 2}:
        return _error("pipeline registry version must be 1 or 2")
    is_v2 = version == 2
    error = _unknown_fields(registry, V2_REGISTRY_FIELDS if is_v2 else REGISTRY_FIELDS, "pipeline registry")
    if error:
        return error

    role_ids: set[str] = set()
    if is_v2:
        roles = registry.get("roles")
        if not isinstance(roles, list):
            return _error("roles must be a list")
        for role_index, role in enumerate(roles):
            if not isinstance(role, dict):
                return _error(f"role at index {role_index} must be an object")
            role_id = role.get("id")
            if not _non_empty_string(role_id):
                return _error(f"role at index {role_index} id must be non-empty string")
            if role_id in role_ids:
                return _error(f"duplicate role id: {role_id}")
            role_ids.add(role_id)
            error = _unknown_fields(role, ROLE_FIELDS, f"role {role_id}")
            if error:
                return error
            if not _non_empty_string(role.get("provider")):
                return _error(f"role {role_id} provider must be non-empty string")
            for field in ("model", "description"):
                if field in role and not _non_empty_string(role[field]):
                    return _error(f"role {role_id} {field} must be non-empty string")

    pipelines = registry.get("pipelines")
    if not isinstance(pipelines, list):
        return _error("pipelines must be a list")

    pipeline_ids: set[str] = set()
    for pipeline_index, pipeline in enumerate(pipelines):
        if not isinstance(pipeline, dict):
            return _error(f"pipeline at index {pipeline_index} must be an object")
        pipeline_id = pipeline.get("id")
        if not _non_empty_string(pipeline_id):
            return _error(f"pipeline at index {pipeline_index} id must be non-empty string")
        if pipeline_id in pipeline_ids:
            return _error(f"duplicate pipeline id: {pipeline_id}")
        pipeline_ids.add(pipeline_id)
        error = _unknown_fields(pipeline, PIPELINE_FIELDS, f"pipeline {pipeline_id}")
        if error:
            return error
        if not _non_empty_string(pipeline.get("revision")):
            return _error(f"pipeline {pipeline_id} revision must be non-empty string")
        if not isinstance(pipeline.get("enabled"), bool):
            return _error(f"pipeline {pipeline_id} enabled must be boolean")
        stages = pipeline.get("stages")
        gates = pipeline.get("gates")
        edges = pipeline.get("edges")
        if not isinstance(stages, list):
            return _error(f"pipeline {pipeline_id} stages must be a list")
        if not isinstance(gates, list):
            return _error(f"pipeline {pipeline_id} gates must be a list")
        if not isinstance(edges, list):
            return _error(f"pipeline {pipeline_id} edges must be a list")

        stage_ids: set[str] = set()
        stage_gate_ids: dict[str, list[str]] = {}
        for stage_index, stage in enumerate(stages):
            if not isinstance(stage, dict):
                return _error(f"pipeline {pipeline_id} stage at index {stage_index} must be an object")
            stage_id = stage.get("id")
            if not _non_empty_string(stage_id):
                return _error(f"pipeline {pipeline_id} stage at index {stage_index} id must be non-empty string")
            if stage_id in stage_ids:
                return _error(f"pipeline {pipeline_id} has duplicate stage id: {stage_id}")
            stage_ids.add(stage_id)
            error = _unknown_fields(stage, V2_STAGE_FIELDS if is_v2 else STAGE_FIELDS, f"pipeline {pipeline_id} stage {stage_id}")
            if error:
                return error
            stage_reference = stage.get("role" if is_v2 else "skill")
            reference_kind = "role" if is_v2 else "skill"
            if not _non_empty_string(stage_reference):
                return _error(f"pipeline {pipeline_id} stage {stage_id} {reference_kind} must be non-empty string")
            if is_v2 and stage_reference not in role_ids:
                return _error(f"pipeline {pipeline_id} stage {stage_id} references undeclared role: {stage_reference}")
            if not is_v2 and skills_lookup is not None and stage_reference not in skills_lookup:
                return _error(f"pipeline {pipeline_id} stage {stage_id} references unregistered skill: {stage_reference}")
            max_attempts = stage.get("maxAttempts")
            if isinstance(max_attempts, bool) or not isinstance(max_attempts, int) or max_attempts <= 0:
                return _error(f"pipeline {pipeline_id} stage {stage_id} maxAttempts must be a positive integer")
            if not isinstance(stage.get("params"), dict):
                return _error(f"pipeline {pipeline_id} stage {stage_id} params must be an object")
            stage_gates = stage.get("gates")
            if not isinstance(stage_gates, list) or any(not _non_empty_string(item) for item in stage_gates):
                return _error(f"pipeline {pipeline_id} stage {stage_id} gates must be a list of non-empty strings")
            if not stage_gates:
                return _error(f"pipeline {pipeline_id} stage {stage_id} gates must not be empty")
            if len(stage_gates) != len(set(stage_gates)):
                return _error(f"pipeline {pipeline_id} stage {stage_id} gates must not contain duplicates")
            stage_gate_ids[stage_id] = stage_gates

        entry_stage = pipeline.get("entryStage")
        if not _non_empty_string(entry_stage) or entry_stage not in stage_ids:
            return _error(f"pipeline {pipeline_id} entryStage references missing stage")

        gate_ids: set[str] = set()
        gate_stages: dict[str, str] = {}
        for gate_index, gate in enumerate(gates):
            if not isinstance(gate, dict):
                return _error(f"pipeline {pipeline_id} gate at index {gate_index} must be an object")
            gate_id = gate.get("id")
            if not _non_empty_string(gate_id):
                return _error(f"pipeline {pipeline_id} gate at index {gate_index} id must be non-empty string")
            if gate_id in gate_ids:
                return _error(f"pipeline {pipeline_id} has duplicate gate id: {gate_id}")
            gate_ids.add(gate_id)
            error = _unknown_fields(gate, GATE_FIELDS, f"pipeline {pipeline_id} gate {gate_id}")
            if error:
                return error
            stage_id = gate.get("stage")
            if not isinstance(stage_id, str) or stage_id not in stage_ids:
                return _error(f"pipeline {pipeline_id} gate {gate_id} references missing stage")
            if gate_id not in stage_gate_ids[stage_id]:
                return _error(f"pipeline {pipeline_id} gate {gate_id} is not declared by stage {stage_id}")
            evaluator = gate.get("evaluator")
            if not _non_empty_string(evaluator):
                return _error(f"pipeline {pipeline_id} gate {gate_id} evaluator must be non-empty string")
            if not is_v2 and skills_lookup is not None and evaluator not in skills_lookup:
                return _error(f"pipeline {pipeline_id} gate {gate_id} references unregistered evaluator: {evaluator}")
            if not isinstance(gate.get("params"), dict):
                return _error(f"pipeline {pipeline_id} gate {gate_id} params must be an object")
            gate_stages[gate_id] = stage_id

        for stage_id, declared_gate_ids in stage_gate_ids.items():
            for gate_id in declared_gate_ids:
                if gate_id not in gate_ids:
                    return _error(f"pipeline {pipeline_id} stage {stage_id} references missing gate: {gate_id}")
                if gate_stages[gate_id] != stage_id:
                    return _error(f"pipeline {pipeline_id} gate {gate_id} is declared by a different stage")

        edge_keys: set[tuple[str, str, str]] = set()
        verdicts_by_gate: dict[str, set[str]] = {gate_id: set() for gate_id in gate_ids}
        for edge_index, edge in enumerate(edges):
            if not isinstance(edge, dict):
                return _error(f"pipeline {pipeline_id} edge at index {edge_index} must be an object")
            error = _unknown_fields(edge, EDGE_FIELDS, f"pipeline {pipeline_id} edge at index {edge_index}")
            if error:
                return error
            source = edge.get("from")
            target = edge.get("to")
            if not isinstance(source, dict):
                return _error(f"pipeline {pipeline_id} edge at index {edge_index} from must be an object")
            error = _unknown_fields(source, EDGE_FROM_FIELDS, f"pipeline {pipeline_id} edge at index {edge_index} from")
            if error:
                return error
            source_stage = source.get("stage")
            source_gate = source.get("gate")
            verdict = source.get("verdict")
            if not isinstance(source_stage, str) or source_stage not in stage_ids:
                return _error(f"pipeline {pipeline_id} edge at index {edge_index} references missing from.stage")
            if not isinstance(source_gate, str) or source_gate not in gate_ids:
                return _error(f"pipeline {pipeline_id} edge at index {edge_index} references missing from.gate")
            if gate_stages[source_gate] != source_stage:
                return _error(f"pipeline {pipeline_id} edge at index {edge_index} gate does not belong to from.stage")
            if verdict not in VERDICTS:
                return _error(f"pipeline {pipeline_id} edge at index {edge_index} verdict must be pass or fail")
            edge_key = (source_stage, source_gate, verdict)
            if edge_key in edge_keys:
                return _error(f"pipeline {pipeline_id} has duplicate edge for gate {source_gate} verdict {verdict}")
            edge_keys.add(edge_key)
            verdicts_by_gate[source_gate].add(verdict)

            if not isinstance(target, dict):
                return _error(f"pipeline {pipeline_id} edge at index {edge_index} to must be an object")
            error = _unknown_fields(target, EDGE_TO_FIELDS, f"pipeline {pipeline_id} edge at index {edge_index} to")
            if error:
                return error
            if verdict == "pass" and set(target) == {"terminal"}:
                if target["terminal"] not in TERMINALS:
                    return _error(f"pipeline {pipeline_id} edge at index {edge_index} terminal must be succeeded")
            elif verdict == "pass" and set(target) == {"stage"}:
                target_stage = target["stage"]
                if target_stage is None:
                    return _error(f"pipeline {pipeline_id} pass edge at index {edge_index} must target a stage or succeeded")
                if target_stage not in stage_ids:
                    return _error(f"pipeline {pipeline_id} edge at index {edge_index} references missing to.stage")
            elif verdict == "fail" and set(target) == {"stage"}:
                target_stage = target["stage"]
                if target_stage is not None and target_stage not in stage_ids:
                    return _error(f"pipeline {pipeline_id} edge at index {edge_index} references missing to.stage")
            elif verdict == "fail" and set(target) == {"terminal"}:
                return _error(f"pipeline {pipeline_id} fail edge at index {edge_index} must target a stage or null")
            else:
                return _error(
                    f"pipeline {pipeline_id} edge at index {edge_index} has invalid target for verdict {verdict}"
                )

        for gate_id, verdicts in verdicts_by_gate.items():
            missing = VERDICTS - verdicts
            if missing:
                return _error(f"pipeline {pipeline_id} gate {gate_id} is missing edge verdicts: {', '.join(sorted(missing))}")

        # 正常推进只由每个 stage 的最后一个 gate 决定；该图有环时 runner 会在不递增
        # attempt 的情况下重放 stage，因此定义期必须 fail-closed。
        pass_successors: dict[str, str] = {}
        for stage_id, declared_gate_ids in stage_gate_ids.items():
            target = next(
                edge["to"]
                for edge in edges
                if edge["from"] == {
                    "stage": stage_id,
                    "gate": declared_gate_ids[-1],
                    "verdict": "pass",
                }
            )
            if isinstance(target.get("stage"), str):
                pass_successors[stage_id] = target["stage"]

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(stage_id: str) -> bool:
            if stage_id in visiting:
                return False
            if stage_id in visited:
                return True
            visiting.add(stage_id)
            successor = pass_successors.get(stage_id)
            if successor is not None and not visit(successor):
                return False
            visiting.remove(stage_id)
            visited.add(stage_id)
            return True

        if any(not visit(stage_id) for stage_id in stage_ids):
            return _error(f"pipeline {pipeline_id} last-gate pass graph must be acyclic")

        if not is_v2 and skills_lookup is not None:
            referenced = {
                stage["skill"] for stage in stages
            } | {
                gate["evaluator"] for gate in gates
            }
            for skill_id in referenced:
                skill = skills_lookup[skill_id]
                backend = skill.get("backend") if isinstance(skill, Mapping) else None
                if backend != "builtin":
                    return _error(f"pipeline {pipeline_id} skill {skill_id} backend must be builtin in 3A")
                # disabled skill（自身 disabled 或所属 adapter disabled）不得被流水线引用：
                # 引用了也跑不起来，必须 fail-closed 而不是放行到运行时才失败。
                # 注意：字段缺失按"启用"处理（与 skills registry 的默认值一致），只有明确
                # enabled=False 或 status=disabled 才算禁用。
                if not isinstance(skill, Mapping) or skill.get("enabled") is False or skill.get("status") == "disabled":
                    return _error(f"pipeline {pipeline_id} references disabled skill: {skill_id}")
                if skill_id not in BUILTIN_SKILL_IMPLEMENTATIONS:
                    return _error(f"pipeline {pipeline_id} builtin skill has no catalog implementation: {skill_id}")

    return None


def _error_payload(
    home: Path,
    error: dict[str, Any],
    *,
    include_status: bool,
    runtime_version: Any = None,
) -> dict[str, Any]:
    try:
        path = pipeline_registry_path(home)
    except Exception:  # noqa: BLE001
        path = _fallback_path(home)
    payload: dict[str, Any] = {
        "registry": str(path),
        "runtime": _runtime_for_version(runtime_version),
        "pipelines": [],
        "checks": [error],
    }
    return {"status": "error", **payload} if include_status else payload


def list_pipelines(home: Path = DEFAULT_HOME) -> dict[str, Any]:
    registry, error = load_pipeline_registry(home)
    if error:
        return _error_payload(home, error, include_status=True)
    assert registry is not None
    error = _validate_registry(registry)
    if error:
        return _error_payload(
            home,
            error,
            include_status=True,
            runtime_version=registry.get("version"),
        )
    rows = [
        {
            "id": pipeline["id"],
            "revision": pipeline["revision"],
            "enabled": pipeline["enabled"],
            "entryStage": pipeline["entryStage"],
            "stages": len(pipeline["stages"]),
            "gates": len(pipeline["gates"]),
            "edges": len(pipeline["edges"]),
        }
        for pipeline in registry["pipelines"]
    ]
    payload = {
        "status": "ok",
        "registry": str(pipeline_registry_path(home)),
        "runtime": _runtime_for_version(registry.get("version")),
        "pipelines": rows,
        "checks": [{"level": "ok", "message": "pipeline registry is valid"}],
    }
    if registry["version"] == 2:
        payload["roles"] = [
            {
                key: role[key]
                for key in ("id", "provider", "model", "description")
                if key in role
            }
            for role in registry["roles"]
        ]
    return payload


def list_roles(home: Path = DEFAULT_HOME) -> dict[str, Any]:
    registry, error = load_pipeline_registry(home)
    if error:
        return _error_payload(home, error, include_status=True)
    assert registry is not None
    validation_error = _validate_registry(registry)
    if validation_error:
        return _error_payload(home, validation_error, include_status=True)
    if registry["version"] == 1:
        return {
            "status": "unsupported",
            "message": "pipeline registry version 1 does not define roles",
        }
    return {
        "status": "ok",
        "version": 2,
        "roles": [
            {
                key: role[key]
                for key in ("id", "provider", "model", "description")
                if key in role
            }
            for role in registry["roles"]
        ],
    }


def doctor_pipelines(
    home: Path,
    skills_lookup: Mapping[str, Any],
) -> tuple[bool, dict[str, Any]]:
    registry, error = load_pipeline_registry(home)
    if error:
        return False, _error_payload(home, error, include_status=False)
    assert registry is not None
    error = _validate_registry(registry, skills_lookup)
    if error:
        return False, _error_payload(
            home,
            error,
            include_status=False,
            runtime_version=registry.get("version"),
        )
    listed = list_pipelines(home)
    listed.pop("status", None)
    return True, listed


def decide_terminal(
    event: str,
    fail_target: str | None = None,
    *,
    attempt_no: int | None = None,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    """按 §5.6 将事件和可选回退目标归一化为唯一动作。"""
    if event in {"failure", "timeout"}:
        if fail_target is None:
            return {
                "action": "terminate",
                "terminalReason": "timeout" if event == "timeout" else "gate_rejected",
            }
        if (
            isinstance(attempt_no, bool)
            or not isinstance(attempt_no, int)
            or isinstance(max_attempts, bool)
            or not isinstance(max_attempts, int)
        ):
            return {"action": "terminate", "terminalReason": "failed"}
        if attempt_no >= max_attempts:
            return {"action": "terminate", "terminalReason": "attempts_exhausted"}
        return {"action": "rollback", "targetStage": fail_target}
    if event == "succeeded":
        return {"action": "terminate", "terminalReason": "succeeded"}
    if event == "internal_error":
        return {"action": "terminate", "terminalReason": "failed"}
    if event == "cancelled":
        return {"action": "terminate", "terminalReason": "cancelled"}
    return {"action": "terminate", "terminalReason": "failed"}


def _run_error(code: str, message: str, **fields: Any) -> dict[str, Any]:
    return {"status": "error", "error": code, "message": message, **fields}


def _skills_lookup(home: Path) -> tuple[dict[str, dict[str, Any]] | None, dict[str, Any] | None]:
    result = list_skills(home)
    if result.get("status") != "ok":
        return None, _run_error("skills_registry_invalid", "skills registry validation failed", checks=result.get("checks", []))
    lookup = {
        skill["id"]: skill
        for skill in result.get("skills", [])
        if isinstance(skill, dict) and isinstance(skill.get("id"), str)
    }
    return lookup, None


def _edge_target(pipeline: dict[str, Any], stage_id: str, gate_id: str, verdict: str) -> dict[str, Any]:
    for edge in pipeline["edges"]:
        source = edge["from"]
        if source == {"stage": stage_id, "gate": gate_id, "verdict": verdict}:
            return edge["to"]
    return {"stage": None}


def _downstream_stages(pipeline: dict[str, Any], start: str) -> set[str]:
    """按 §5.7 仅沿每个 stage 最后一个 gate 的 pass 边查找下游。"""
    stages = {stage["id"]: stage for stage in pipeline["stages"]}
    downstream = {start}
    pending = [start]
    while pending:
        stage_id = pending.pop()
        last_gate_id = stages[stage_id]["gates"][-1]
        target_stage = _edge_target(pipeline, stage_id, last_gate_id, "pass").get("stage")
        if target_stage is not None and target_stage not in downstream:
            downstream.add(target_stage)
            pending.append(target_stage)
    return downstream


def _stable_stage_key(session_id: str, revision: str, run_id: str, stage_id: str, attempt_no: int) -> dict[str, Any]:
    return {
        "sessionId": session_id,
        "pipelineRevision": revision,
        "runId": run_id,
        "stageId": stage_id,
        "attemptNo": attempt_no,
    }


def _clock_value(now_ns: Clock) -> int | None:
    return now_ns() if callable(now_ns) else now_ns


def _touch_lease(
    home: Path,
    session_id: str,
    lease_token: str,
    lease: dict[str, Any],
    *,
    now_ns: int | None,
) -> dict[str, Any]:
    """在状态边界按剩余 TTL 续约；失败即视为 runner 已失权。"""
    now = now_ns if now_ns is not None else __import__("time").time_ns()
    ttl_ms = lease.get("ttlMs", LEASE_TTL_MS)
    threshold_ns = ttl_ms * 1_000_000 // LEASE_RENEW_THRESHOLD_DIVISOR
    if lease.get("expiresAtNs", 0) - now > threshold_ns:
        return {"status": "ok", "lease": lease}
    renewed = renew_lease(home, session_id, lease_token, now_ns=now, ttl_ms=ttl_ms)
    if renewed.get("status") != "ok":
        return _run_error(
            "lease_lost",
            "pipeline runner lost its lease while renewing",
            leaseError=renewed.get("error", "lease_renew_failed"),
        )
    return {"status": "ok", "lease": renewed["lease"], "session": renewed["session"]}


def _save_with_lease(
    home: Path,
    session: dict[str, Any],
    lease_token: str,
    lease_epoch: int,
    lease: dict[str, Any],
    *,
    now_ns: Clock,
) -> dict[str, Any]:
    boundary_now = _clock_value(now_ns)
    touched = _touch_lease(
        home,
        session["id"],
        lease_token,
        lease,
        now_ns=boundary_now,
    )
    if touched.get("status") != "ok":
        return touched
    candidate = session
    if "session" in touched:
        # renew_lease 自身会推进持久化版本；保留调用方尚未保存的业务字段，
        # 只把乐观并发基线同步到续约后的版本。
        candidate = dict(session)
        candidate["version"] = touched["session"]["version"]
    saved = save_session(
        home,
        candidate,
        lease_token=lease_token,
        lease_epoch=lease_epoch,
        now_ns=boundary_now,
    )
    if saved.get("status") == "ok":
        saved["lease"] = touched["lease"]
    return saved


def _release_dispatch_lease(home: Path, session: dict[str, Any], lease_token: str, lease_epoch: int) -> dict[str, Any]:
    """持久化非终态 dispatch 后释放 lease，避免等待主 agent 时长期占锁。"""
    return release_lease(home, session["id"], lease_token=lease_token, lease_epoch=lease_epoch)


def _next_run_sequence(runs: Mapping[str, Any]) -> int:
    """分配不依赖 JSON 对象键顺序的单调 run 序号。"""
    sequences = [
        run.get("sequence")
        for run in runs.values()
        if isinstance(run, Mapping)
        and isinstance(run.get("sequence"), int)
        and not isinstance(run.get("sequence"), bool)
    ]
    return max(sequences, default=0) + 1


def _latest_run(runs: Any) -> tuple[str, dict[str, Any]] | None:
    """按持久化 sequence 取最新 run；不依赖被 sort_keys 重排的字典顺序。"""
    if not isinstance(runs, dict):
        return None
    candidates = [
        (run["sequence"], run_id, run)
        for run_id, run in runs.items()
        if isinstance(run_id, str)
        and isinstance(run, dict)
        and isinstance(run.get("sequence"), int)
        and not isinstance(run.get("sequence"), bool)
        and run["sequence"] > 0
    ]
    if not candidates:
        return None
    _, run_id, run = max(candidates, key=lambda item: item[0])
    return run_id, run


def _dispatch_result(
    session_id: str,
    pipeline: dict[str, Any],
    run_id: str,
    stage_id: str,
    role: dict[str, Any],
) -> dict[str, Any]:
    role_id = role["id"]
    return {
        "status": "ok",
        "mode": "dispatch",
        "sessionId": session_id,
        "pipeline": {"id": pipeline["id"], "revision": pipeline["revision"], "runId": run_id},
        "stage": stage_id,
        "role": {
            key: role[key]
            for key in ("id", "provider", "model")
            if key in role
        },
        "message": (
            f"next: dispatch role '{role_id}' "
            f"(provider={role['provider']}, model={role.get('model', '')})"
        ),
    }


def run_advance(
    home: Path = DEFAULT_HOME,
    session_id: str = "",
    verdict: str = "",
    evidence: Any = None,
    error: Any = None,
) -> dict[str, Any]:
    """回填单 gate v2 stage 结果，并推进、回退或终止当前 run。"""
    home = Path(home)
    if verdict not in VERDICTS:
        return _run_error("invalid_verdict", "verdict must be pass or fail")

    loaded = load_session(home, session_id)
    if loaded.get("status") != "ok":
        return loaded
    session = loaded["session"]
    if session["status"] in TERMINAL_STATUSES:
        return _run_error("session_terminal", "terminal session cannot advance", sessionId=session_id)

    leased = acquire_lease(home, session_id)
    if leased.get("status") != "ok":
        return leased
    session = leased["session"]
    lease = leased["lease"]
    lease_token = lease["token"]
    lease_epoch = lease["epoch"]

    def release_with(result: dict[str, Any]) -> dict[str, Any]:
        released = _release_dispatch_lease(home, session, lease_token, lease_epoch)
        return result if released.get("status") == "ok" else released

    latest = _latest_run(session.get("pipelineRuns"))
    if latest is None:
        return release_with(_run_error("pipeline_run_not_found", "session has no sequenced pipeline run"))
    run_id, run = latest
    if run.get("status") != "active":
        return release_with(_run_error("pipeline_run_inactive", "latest pipeline run is not active"))

    registry, registry_error = load_pipeline_registry(home)
    if registry_error:
        return release_with(_run_error("pipeline_registry_invalid", registry_error["message"]))
    assert registry is not None
    validation_error = _validate_registry(registry)
    if validation_error:
        return release_with(_run_error("pipeline_registry_invalid", validation_error["message"]))
    if registry["version"] != 2:
        return release_with(_run_error("pipeline_advance_unsupported", "pipeline advance requires registry version 2"))

    selected = next(
        (
            item
            for item in registry["pipelines"]
            if item["id"] == run.get("pipelineId") and item["revision"] == run.get("pipelineRevision")
        ),
        None,
    )
    if selected is None:
        return release_with(_run_error("pipeline_not_found", "pipeline revision for latest run does not exist"))

    stages = {stage["id"]: stage for stage in selected["stages"]}
    roles = {role["id"]: role for role in registry["roles"]}
    current_stage = session.get("currentStep")
    stage = stages.get(current_stage)
    if stage is None or run.get("currentStage") != current_stage:
        return release_with(_run_error("pipeline_state_invalid", "current pipeline stage is invalid"))
    if len(stage["gates"]) != 1:
        return release_with(_run_error("unsupported_multi_gate", "pipeline advance supports exactly one gate per stage"))

    gate_id = stage["gates"][0]
    gate = next(item for item in selected["gates"] if item["id"] == gate_id)
    attempt_no = run["attempts"].get(current_stage, 0) + 1
    run["attempts"][current_stage] = attempt_no
    gate_result = {
        "gateKey": {
            **_stable_stage_key(session_id, selected["revision"], run_id, current_stage, attempt_no),
            "gateId": gate_id,
        },
        "evaluator": gate["evaluator"],
        "verdict": verdict,
        "evidenceRef": None,
        "evidence": evidence,
        "schemaVersion": 1,
        "invalidatedReason": None,
    }
    if error is not None:
        gate_result["error"] = error
    run["gateResults"].append(gate_result)

    terminal_reason: str | None = None
    next_stage: str | None = None
    target = _edge_target(selected, current_stage, gate_id, verdict)
    if verdict == "pass":
        if target.get("terminal") == "succeeded":
            terminal_reason = "succeeded"
        else:
            next_stage = target.get("stage")
    else:
        fail_target = target.get("stage")
        decision = decide_terminal(
            "failure",
            fail_target,
            attempt_no=run["attempts"].get(fail_target, 0) if fail_target is not None else None,
            max_attempts=stages[fail_target]["maxAttempts"] if fail_target is not None else None,
        )
        if decision["action"] == "terminate":
            terminal_reason = decision["terminalReason"]
        else:
            next_stage = decision["targetStage"]

    if terminal_reason is None and verdict == "fail" and next_stage is not None:
        invalidated_stages = _downstream_stages(selected, next_stage)
        reason = f"rollback_to:{next_stage}"
        for result in run["gateResults"]:
            if result["gateKey"]["stageId"] in invalidated_stages and result["invalidatedReason"] is None:
                result["invalidatedReason"] = reason

    if terminal_reason is None and next_stage not in stages:
        return release_with(_run_error("pipeline_state_invalid", "edge target stage is invalid"))

    if terminal_reason is not None:
        run["status"] = "succeeded" if terminal_reason == "succeeded" else "failed"
        run["terminalReason"] = terminal_reason
    else:
        assert next_stage is not None
        run["currentStage"] = next_stage
        session["currentStep"] = next_stage

    saved = _save_with_lease(home, session, lease_token, lease_epoch, lease, now_ns=None)
    if saved.get("status") != "ok":
        return release_with(saved)
    session = saved["session"]

    pipeline_result = {"id": selected["id"], "revision": selected["revision"], "runId": run_id}
    if terminal_reason is not None:
        session_status = "succeeded" if terminal_reason == "succeeded" else "failed"
        finalized = finalize_session(
            home,
            session_id,
            lease_token=lease_token,
            lease_epoch=lease_epoch,
            terminal_status=session_status,
            result={"status": session_status, "refs": []},
            error=None if session_status == "succeeded" else {"code": terminal_reason},
            extra_fields={"pipelineRuns": session["pipelineRuns"], "currentStep": session.get("currentStep")},
        )
        if finalized.get("status") != "ok":
            return finalized
        return {
            "status": "ok",
            "mode": "terminal",
            "sessionId": session_id,
            "pipeline": pipeline_result,
            "terminalReason": terminal_reason,
        }

    released = _release_dispatch_lease(home, session, lease_token, lease_epoch)
    if released.get("status") != "ok":
        return released
    assert next_stage is not None
    role = roles[stages[next_stage]["role"]]
    return _dispatch_result(session_id, selected, run_id, next_stage, role)


def run_pipeline(
    home: Path = DEFAULT_HOME,
    pipeline_id: str = "",
    *,
    session_id: str | None = None,
    new_session_summary: str | None = None,
    now_ns: Clock = None,
) -> dict[str, Any]:
    """运行 3A builtin-only pipeline；所有异常均收敛为结构化结果。"""
    home = Path(home)
    if (session_id is None) == (new_session_summary is None):
        return _run_error("invalid_session_selection", "provide exactly one of session_id or new_session_summary")
    if not _non_empty_string(pipeline_id):
        return _run_error("invalid_pipeline_id", "pipeline id must be a non-empty string")

    lease_token: str | None = None
    lease_epoch: int | None = None
    active_session_id: str | None = None
    selected: dict[str, Any] | None = None
    run_id: str | None = None
    try:
        registry, registry_error = load_pipeline_registry(home)
        if registry_error:
            return _run_error("pipeline_registry_invalid", registry_error["message"])
        assert registry is not None
        skills_lookup: Mapping[str, Any] | None = None
        if registry.get("version") == 1:
            skills_lookup, skills_error = _skills_lookup(home)
            if skills_error:
                return skills_error
            assert skills_lookup is not None
        validation_error = _validate_registry(registry, skills_lookup)
        if validation_error:
            return _run_error("pipeline_registry_invalid", validation_error["message"])

        selected = next((item for item in registry["pipelines"] if item["id"] == pipeline_id), None)
        if selected is None:
            return _run_error("pipeline_not_found", "pipeline does not exist")
        if not selected["enabled"]:
            return _run_error("pipeline_disabled", "pipeline is disabled")

        if new_session_summary is not None:
            prepared = create_session(
                home, summary=new_session_summary, task_type="pipeline", now_ns=_clock_value(now_ns)
            )
        else:
            prepared = load_session(home, session_id or "")
        if prepared.get("status") != "ok":
            return prepared
        session = prepared["session"]
        active_session_id = session["id"]
        if session["status"] in TERMINAL_STATUSES:
            return _run_error("session_terminal", "terminal session cannot run a pipeline", sessionId=active_session_id)

        leased = acquire_lease(home, active_session_id, now_ns=_clock_value(now_ns))
        if leased.get("status") != "ok":
            return leased
        session = leased["session"]
        lease_token = leased["lease"]["token"]
        lease_epoch = leased["lease"]["epoch"]
        lease = leased["lease"]
        if session["status"] == "created":
            session["status"] = "active"

        run_id = uuid.uuid4().hex
        stages = {stage["id"]: stage for stage in selected["stages"]}
        gates = {gate["id"]: gate for gate in selected["gates"]}
        current_stage = selected["entryStage"]
        pipeline_runs = session.setdefault("pipelineRuns", {})
        run = {
            "schemaVersion": 1,
            "pipelineId": selected["id"],
            "pipelineRevision": selected["revision"],
            "status": "active",
            "currentStage": current_stage,
            "terminalReason": None,
            "attempts": {},
            "stageExecutions": [],
            "gateResults": [],
        }
        if registry["version"] == 2:
            run["sequence"] = _next_run_sequence(pipeline_runs)
        pipeline_runs[run_id] = run
        session["currentStep"] = current_stage
        saved = _save_with_lease(
            home, session, lease_token, lease_epoch, lease, now_ns=now_ns
        )
        if saved.get("status") != "ok":
            reason = "lease_lost" if saved.get("error") == "lease_lost" else "failed"
            return _finish_failed_run(
                home, active_session_id, selected, run_id, lease_token, lease_epoch, lease, reason, now_ns
            )
        session = saved["session"]
        lease = saved["lease"]

        if registry["version"] == 2:
            role_id = stages[current_stage]["role"]
            role = next(role for role in registry["roles"] if role["id"] == role_id)
            released = _release_dispatch_lease(home, session, lease_token, lease_epoch)
            if released.get("status") != "ok":
                return released
            return _dispatch_result(active_session_id, selected, run_id, current_stage, role)

        terminal_reason: str | None = None
        while terminal_reason is None:
            run = session["pipelineRuns"][run_id]
            stage = stages[current_stage]
            # attempt_no 表示实际执行次数；无论是直接回退还是下游失效后重跑，
            # 都只在真正进入 stage 执行时统一分配，避免稳定键重复。
            attempt_no = run["attempts"].get(current_stage, 0) + 1
            # 下游重跑也受当前 stage 自身额度约束；必须在执行 skill 前终止，
            # 避免超额执行已经产生副作用。
            if attempt_no > stage["maxAttempts"]:
                terminal_reason = "attempts_exhausted"
                break
            run["attempts"][current_stage] = attempt_no
            stage_key = _stable_stage_key(active_session_id, selected["revision"], run_id, current_stage, attempt_no)
            output = execute_builtin(stage["skill"], stage["params"])
            run["stageExecutions"].append({
                "stageKey": stage_key,
                "status": "completed",
                "skill": stage["skill"],
                "output": output,
                "invalidatedReason": None,
            })
            saved = _save_with_lease(home, session, lease_token, lease_epoch, lease, now_ns=now_ns)
            if saved.get("status") != "ok":
                terminal_reason = "lease_lost" if saved.get("error") == "lease_lost" else "failed"
                break
            session = saved["session"]
            lease = saved["lease"]
            if output.get("status") == "error":
                terminal_reason = "failed"
                break

            last_gate_id: str | None = None
            rolled_back = False
            for gate_id in stage["gates"]:
                last_gate_id = gate_id
                gate = gates[gate_id]
                evaluated = execute_builtin(gate["evaluator"], gate["params"], actual=output)
                if evaluated.get("status") == "error" or evaluated.get("verdict") not in VERDICTS:
                    terminal_reason = "failed"
                    break
                gate_key = {**stage_key, "gateId": gate_id}
                run = session["pipelineRuns"][run_id]
                run["gateResults"].append({
                        "gateKey": gate_key,
                        "evaluator": gate["evaluator"],
                        "verdict": evaluated["verdict"],
                        "evidenceRef": None,
                        "evidence": evaluated.get("evidence", {}),
                    "schemaVersion": 1,
                    "invalidatedReason": None,
                })
                saved = _save_with_lease(home, session, lease_token, lease_epoch, lease, now_ns=now_ns)
                if saved.get("status") != "ok":
                    terminal_reason = "lease_lost" if saved.get("error") == "lease_lost" else "failed"
                    break
                session = saved["session"]
                lease = saved["lease"]
                run = session["pipelineRuns"][run_id]
                if evaluated["verdict"] == "fail":
                    target = _edge_target(selected, current_stage, gate_id, "fail").get("stage")
                    decision = decide_terminal(
                        "failure",
                        target,
                        attempt_no=run["attempts"].get(target, 0) if target is not None else None,
                        max_attempts=stages[target]["maxAttempts"] if target is not None else None,
                    )
                    if decision["action"] == "terminate":
                        terminal_reason = decision["terminalReason"]
                        break
                    rollback_target = decision["targetStage"]
                    invalidated_stages = _downstream_stages(selected, rollback_target)
                    reason = f"rollback_to:{rollback_target}"
                    for execution in run["stageExecutions"]:
                        if execution["stageKey"]["stageId"] in invalidated_stages and execution["invalidatedReason"] is None:
                            execution["status"] = "invalidated"
                            execution["invalidatedReason"] = reason
                    for result in run["gateResults"]:
                        if result["gateKey"]["stageId"] in invalidated_stages and result["invalidatedReason"] is None:
                            result["invalidatedReason"] = reason
                    current_stage = rollback_target
                    run["currentStage"] = current_stage
                    session["currentStep"] = current_stage
                    saved = _save_with_lease(home, session, lease_token, lease_epoch, lease, now_ns=now_ns)
                    if saved.get("status") != "ok":
                        terminal_reason = "lease_lost" if saved.get("error") == "lease_lost" else "failed"
                    else:
                        session = saved["session"]
                        lease = saved["lease"]
                        rolled_back = True
                    break
            if terminal_reason is not None or rolled_back:
                continue
            if last_gate_id is None:
                terminal_reason = "failed"
                break
            target = _edge_target(selected, current_stage, last_gate_id, "pass")
            if target.get("terminal") == "succeeded":
                terminal_reason = "succeeded"
            else:
                current_stage = target.get("stage")
                if current_stage not in stages:
                    terminal_reason = "failed"
                    break
                run = session["pipelineRuns"][run_id]
                run["currentStage"] = current_stage
                session["currentStep"] = current_stage
                saved = _save_with_lease(home, session, lease_token, lease_epoch, lease, now_ns=now_ns)
                if saved.get("status") != "ok":
                    terminal_reason = "lease_lost" if saved.get("error") == "lease_lost" else "failed"
                else:
                    session = saved["session"]
                    lease = saved["lease"]

        assert terminal_reason is not None
        run = session["pipelineRuns"][run_id]
        run["status"] = "succeeded" if terminal_reason == "succeeded" else "failed"
        run["terminalReason"] = terminal_reason
        return _finalize_run(
            home,
            active_session_id,
            selected,
            run_id,
            lease_token,
            lease_epoch,
            terminal_reason,
            _clock_value(now_ns),
            session_fields={"pipelineRuns": session["pipelineRuns"], "currentStep": session.get("currentStep")},
        )
    except Exception as exc:  # noqa: BLE001
        if active_session_id and selected and run_id and lease_token is not None and lease_epoch is not None:
            return _finish_failed_run(
                home, active_session_id, selected, run_id, lease_token, lease_epoch, lease, "failed", now_ns
            )
        return _run_error("pipeline_run_failed", f"pipeline run failed: {exc}")


def _finish_failed_run(
    home: Path,
    session_id: str,
    pipeline: dict[str, Any],
    run_id: str,
    lease_token: str,
    lease_epoch: int,
    lease: dict[str, Any],
    reason: str,
    now_ns: Clock,
) -> dict[str, Any]:
    loaded = load_session(home, session_id)
    session_fields: dict[str, Any] | None = None
    if loaded.get("status") == "ok":
        session = loaded["session"]
        run = session.get("pipelineRuns", {}).get(run_id)
        if isinstance(run, dict):
            run["status"] = "failed"
            run["terminalReason"] = reason
            session_fields = {
                "pipelineRuns": session["pipelineRuns"],
                "currentStep": session.get("currentStep"),
            }
    return _finalize_run(
        home,
        session_id,
        pipeline,
        run_id,
        lease_token,
        lease_epoch,
        reason,
        _clock_value(now_ns),
        session_fields=session_fields,
    )


def _finalize_run(
    home: Path,
    session_id: str,
    pipeline: dict[str, Any],
    run_id: str,
    lease_token: str,
    lease_epoch: int,
    terminal_reason: str,
    now_ns: int | None,
    *,
    session_fields: dict[str, Any] | None = None,
) -> dict[str, Any]:
    session_status = "succeeded" if terminal_reason == "succeeded" else "cancelled" if terminal_reason == "cancelled" else "failed"
    finalized = finalize_session(
        home,
        session_id,
        lease_token=lease_token,
        lease_epoch=lease_epoch,
        terminal_status=session_status,
        result={"status": session_status, "refs": []},
        error=None if session_status == "succeeded" else {"code": terminal_reason},
        extra_fields=session_fields,
        now_ns=now_ns,
    )
    if finalized.get("status") != "ok":
        persisted = load_session(home, session_id)
        persisted_status = (
            persisted["session"].get("status") if persisted.get("status") == "ok" else "unknown"
        )
        return _run_error(
            "lease_lost" if terminal_reason == "lease_lost" else finalized.get("error", "session_finalize_failed"),
            finalized.get("message", "failed to finalize pipeline session"),
            sessionId=session_id,
            pipeline={"id": pipeline["id"], "revision": pipeline["revision"], "runId": run_id},
            terminalReason=terminal_reason,
            sessionStatus=persisted_status,
            finalizeError=finalized.get("error", "session_finalize_failed"),
        )
    result = {
        "status": "ok" if terminal_reason == "succeeded" else "error",
        "sessionId": session_id,
        "pipeline": {"id": pipeline["id"], "revision": pipeline["revision"], "runId": run_id},
        "terminalReason": terminal_reason,
        "sessionStatus": session_status,
    }
    if result["status"] == "error":
        result["error"] = terminal_reason
    return result
