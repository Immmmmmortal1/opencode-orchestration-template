from __future__ import annotations

import fcntl
import json
import os
import re
import socket
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .paths import DEFAULT_HOME


SESSION_SCHEMA_VERSION = 1
LEASE_SCHEMA_VERSION = 1
LEASE_TTL_MS = 60_000
MAX_SLUG_LENGTH = 48
FINALIZE_EXTRA_FIELDS = {"pipelineRuns", "currentStep"}

_LOCK_FDS: dict[str, int] = {}
_LOCK_OWNERS: dict[str, tuple[Path, str]] = {}

TERMINAL_STATUSES = {"succeeded", "failed", "cancelled", "expired"}
STATUS_TRANSITIONS = {
    "created": {"active", "cancelled", "failed"},
    "active": {"waiting", "completing", "failed", "cancelled", "expired"},
    "waiting": {"active", "failed", "cancelled", "expired"},
    "completing": {"succeeded", "failed"},
    "succeeded": set(),
    "failed": set(),
    "cancelled": set(),
    "expired": set(),
}


def session_dir(home: Path = DEFAULT_HOME, session_id: str = "") -> Path:
    return Path(home) / "sessions" / session_id


def session_path(home: Path = DEFAULT_HOME, session_id: str = "") -> Path:
    return session_dir(home, session_id) / "session.json"


def lock_file(home: Path = DEFAULT_HOME, resource: str = "") -> Path:
    return Path(home) / "locks" / f"{resource}.lock"


def lease_path(home: Path = DEFAULT_HOME, session_id: str = "") -> Path:
    return Path(home) / "leases" / f"session.{session_id}.json"


def _result_error(code: str, message: str, *, status: str = "error", **fields: object) -> dict:
    return {"status": status, "error": code, "message": message, **fields}


def _now_ns(value: int | None) -> int:
    return time.time_ns() if value is None else value


def _valid_name(value: object) -> bool:
    return isinstance(value, str) and bool(value) and re.fullmatch(r"[A-Za-z0-9._-]+", value) is not None


def _valid_session_id(value: object) -> bool:
    return _valid_name(value) and bool(value.strip("."))


def _slug(summary: str) -> str:
    if not isinstance(summary, str):
        return ""
    value = re.sub(r"[^a-z0-9]+", "-", summary.casefold()).strip("-")
    return value[:MAX_SLUG_LENGTH].rstrip("-")


def _new_session_id(summary: str, timestamp_ns: int) -> str:
    slug = _slug(summary)
    if not slug:
        return ""
    timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(timestamp_ns / 1_000_000_000))
    return f"{slug}-{timestamp}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def new_session_id(summary: str) -> str:
    """生成可读且不包含路径分隔符的 session id；空摘要返回空串。"""
    return _new_session_id(summary, time.time_ns())


def _ensure_directory(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_json(path: Path, data: dict) -> None:
    _ensure_directory(path.parent)
    temporary = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    descriptor: int | None = None
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            descriptor = None
            stream.write(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_json(path: Path, label: str) -> tuple[dict | None, dict | None]:
    try:
        with path.open("r", encoding="utf-8") as stream:
            value = json.load(stream)
    except FileNotFoundError:
        return None, _result_error(f"{label}_not_found", f"{label} does not exist", path=str(path))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return None, _result_error(f"{label}_invalid", f"failed to read {label}: {exc}", path=str(path))
    if not isinstance(value, dict):
        return None, _result_error(f"{label}_invalid", f"{label} root must be an object", path=str(path))
    return value, None


def _validate_session(session: dict, expected_id: str | None = None) -> str | None:
    required_fields = (
        "schemaVersion",
        "id",
        "status",
        "version",
        "leaseEpoch",
        "leaseToken",
        "createdAtNs",
        "updatedAtNs",
        "createdBy",
        "task",
        "currentStep",
        "result",
        "error",
    )
    for field in required_fields:
        if field not in session:
            return f"session missing required field: {field}"
    schema_version = session.get("schemaVersion")
    # 严格 int 且非 bool：Python 中 true == 1、1.0 == 1，松散比较会放过非法类型
    if not isinstance(schema_version, int) or isinstance(schema_version, bool) or schema_version != SESSION_SCHEMA_VERSION:
        return "session schemaVersion must be 1"
    if not _valid_session_id(session.get("id")):
        return "session id is invalid"
    if expected_id is not None and session["id"] != expected_id:
        return "session id does not match path"
    if session.get("status") not in STATUS_TRANSITIONS:
        return "session status is invalid"
    if not isinstance(session.get("version"), int) or isinstance(session.get("version"), bool) or session["version"] < 1:
        return "session version must be a positive integer"
    epoch = session.get("leaseEpoch")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 0:
        return "session leaseEpoch must be a non-negative integer"
    token = session.get("leaseToken")
    if token is not None and (not isinstance(token, str) or not token):
        return "session leaseToken must be null or a non-empty string"
    for field in ("createdAtNs", "updatedAtNs"):
        if not isinstance(session.get(field), int) or isinstance(session.get(field), bool):
            return f"session {field} must be an integer"
    created_by = session.get("createdBy")
    if not isinstance(created_by, dict):
        return "session createdBy must be an object"
    if not isinstance(created_by.get("hostname"), str) or not created_by["hostname"]:
        return "session createdBy.hostname must be a non-empty string"
    if not isinstance(created_by.get("pid"), int) or isinstance(created_by.get("pid"), bool):
        return "session createdBy.pid must be an integer"
    task = session.get("task")
    if not isinstance(task, dict):
        return "session task must be an object"
    for field in ("summary", "type"):
        if not isinstance(task.get(field), str) or not task[field].strip():
            return f"session task.{field} must be a non-empty string"
    if session.get("currentStep") is not None and not isinstance(session.get("currentStep"), str):
        return "session currentStep must be null or a string"
    if not isinstance(session.get("result"), dict):
        return "session result must be an object"
    result = session["result"]
    if not isinstance(result.get("status"), str):
        return "session result.status must be a string"
    if not isinstance(result.get("refs"), list):
        return "session result.refs must be a list"
    if session.get("error") is not None and not isinstance(session.get("error"), dict):
        return "session error must be null or an object"
    if "pipelineRuns" in session and not isinstance(session.get("pipelineRuns"), dict):
        return "session pipelineRuns must be an object"
    return None


def create_session(
    home: Path = DEFAULT_HOME,
    *,
    summary: str,
    task_type: str,
    now_ns: int | None = None,
) -> dict:
    now = _now_ns(now_ns)
    session_id = _new_session_id(summary, now)
    if not session_id:
        return _result_error("invalid_summary", "session summary must contain letters or digits")
    if not isinstance(task_type, str) or not task_type.strip():
        return _result_error("invalid_task_type", "session task type must be a non-empty string")

    directory = session_dir(home, session_id)
    try:
        _ensure_directory(Path(home) / "sessions")
        directory.mkdir(mode=0o700)
        session = {
            "schemaVersion": SESSION_SCHEMA_VERSION,
            "id": session_id,
            "status": "created",
            "version": 1,
            "leaseEpoch": 0,
            "leaseToken": None,
            "createdAtNs": now,
            "updatedAtNs": now,
            "createdBy": {"hostname": socket.gethostname(), "pid": os.getpid()},
            "task": {"summary": summary.strip(), "type": task_type.strip()},
            "currentStep": None,
            "result": {"status": "pending", "refs": []},
            "error": None,
        }
        _atomic_write_json(session_path(home, session_id), session)
    except OSError as exc:
        try:
            directory.rmdir()
        except OSError:
            pass
        return _result_error("session_create_failed", f"failed to create session: {exc}")
    return {"status": "ok", "session": session, "path": str(session_path(home, session_id))}


def load_session(home: Path = DEFAULT_HOME, session_id: str = "") -> dict:
    if not _valid_session_id(session_id):
        return _result_error("invalid_session_id", "session id is invalid")
    session, error = _read_json(session_path(home, session_id), "session")
    if error:
        return error
    assert session is not None
    reason = _validate_session(session, session_id)
    if reason:
        return _result_error("session_invalid", reason, path=str(session_path(home, session_id)))
    return {"status": "ok", "session": session, "path": str(session_path(home, session_id))}


def transition_status(session: dict, new_status: str) -> tuple[bool, str | None]:
    current = session.get("status") if isinstance(session, dict) else None
    if current not in STATUS_TRANSITIONS:
        return False, "invalid current status"
    if new_status not in STATUS_TRANSITIONS:
        return False, "invalid target status"
    if current == new_status:
        return True, None
    if new_status not in STATUS_TRANSITIONS[current]:
        if current in TERMINAL_STATUSES:
            return False, f"terminal status {current} cannot transition"
        return False, f"invalid status transition: {current} -> {new_status}"
    return True, None


def terminal_status_path(current: str, target: str) -> list[str] | None:
    """求从 current 到 target 终态的**最短合法路径**（含 target），无合法路径返回 None。

    存在意义：D11 的状态机要求 `created → active → completing → succeeded`，
    但调用方（如 pipeline runner）只想说"结束这次运行"。
    把中间过渡步骤关在本函数里，避免把 D11 的内部状态细节泄漏给每个调用方。
    本函数**只走合法边**，不绕过状态机。
    """
    if current not in STATUS_TRANSITIONS or target not in TERMINAL_STATUSES:
        return None
    if current == target:
        return []
    # BFS 求最短路径（状态空间极小，无需优化）
    queue: list[list[str]] = [[current]]
    seen = {current}
    while queue:
        path = queue.pop(0)
        for nxt in STATUS_TRANSITIONS[path[-1]]:
            if nxt in seen:
                continue
            if nxt == target:
                return path[1:] + [target]
            seen.add(nxt)
            queue.append(path + [nxt])
    return None


def acquire_lock(
    home: Path = DEFAULT_HOME,
    resource: str = "",
) -> dict:
    if not _valid_name(resource):
        return _result_error("invalid_resource", "lock resource is invalid")
    token = uuid.uuid4().hex
    locks = Path(home) / "locks"
    target = lock_file(home, resource)
    try:
        _ensure_directory(locks)
    except OSError as exc:
        return _result_error("lock_directory_failed", f"failed to prepare lock directory: {exc}")

    descriptor: int | None = None
    try:
        descriptor = os.open(target, os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        if descriptor is not None:
            os.close(descriptor)
        return _result_error("lock_busy", "lock is held by another owner", resource=resource)
    except OSError as exc:
        if descriptor is not None:
            os.close(descriptor)
        return _result_error("lock_acquire_failed", f"failed to acquire lock: {exc}")

    try:
        _LOCK_FDS[token] = descriptor
        _LOCK_OWNERS[token] = (target, resource)
    except Exception as exc:
        _LOCK_FDS.pop(token, None)
        _LOCK_OWNERS.pop(token, None)
        try:
            os.close(descriptor)
        except OSError as close_exc:
            return _result_error(
                "lock_acquire_failed",
                f"failed to register lock ownership: {exc}; failed to close lock descriptor: {close_exc}",
            )
        return _result_error("lock_acquire_failed", f"failed to register lock ownership: {exc}")
    return {"status": "ok", "token": token, "path": str(target)}


def release_lock(home: Path = DEFAULT_HOME, resource: str = "", token: str = "") -> dict:
    if not _valid_name(resource):
        return _result_error("invalid_resource", "lock resource is invalid")
    owner = _LOCK_OWNERS.get(token)
    if owner is None or owner != (lock_file(home, resource), resource):
        return _result_error(
            "lock_conflict",
            "lock token or resource does not match current owner",
            status="conflict",
            resource=resource,
        )
    descriptor = _LOCK_FDS[token]
    release_error: OSError | None = None
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    except OSError as exc:
        release_error = exc
    finally:
        try:
            os.close(descriptor)
        except OSError as exc:
            if release_error is None:
                release_error = exc
        finally:
            _LOCK_FDS.pop(token, None)
            _LOCK_OWNERS.pop(token, None)
    if release_error is not None:
        return _result_error("lock_release_failed", f"failed to release lock: {release_error}")
    return {"status": "ok", "resource": resource}


def _session_lock_resource(session_id: str) -> str:
    return f"session.{session_id}"


@contextmanager
def _held_session_lock(home: Path, resource: str) -> Iterator[dict]:
    """持有 session 锁期间统一登记释放结果，异常路径也不会遗留 fd。"""
    held = {"acquire": acquire_lock(home, resource), "release": None}
    try:
        yield held
    finally:
        lock = held["acquire"]
        if lock.get("status") == "ok":
            held["release"] = release_lock(home, resource, lock["token"])


def _lock_release_result(held: dict, result: dict) -> dict:
    released = held.get("release")
    if released is not None and released.get("status") != "ok":
        return _result_error("lock_release_failed", "operation completed but session lock could not be released")
    return result


def save_session(
    home: Path = DEFAULT_HOME,
    session: dict | None = None,
    *,
    lease_token: str | None = None,
    lease_epoch: int | None = None,
    now_ns: int | None = None,
) -> dict:
    if not isinstance(session, dict):
        return _result_error("session_invalid", "session must be an object")
    session_id = session.get("id")
    if not _valid_session_id(session_id):
        return _result_error("invalid_session_id", "session id is invalid")
    validation_error = _validate_session(session, session_id)
    if validation_error:
        return _result_error("session_invalid", validation_error)
    try:
        json.dumps(session, ensure_ascii=False, indent=2, sort_keys=True)
    except (TypeError, ValueError) as exc:
        return _result_error("session_invalid", f"session must be JSON serializable: {exc}")
    resource = _session_lock_resource(session_id)
    now = _now_ns(now_ns)
    with _held_session_lock(home, resource) as held:
        lock = held["acquire"]
        if lock.get("status") != "ok":
            result = lock
        else:
            current_result = load_session(home, session_id)
            if current_result.get("status") != "ok":
                result = current_result
            else:
                current = current_result["session"]
                if current.get("status") in TERMINAL_STATUSES:
                    # 终态是最强不变量：任何写入都以 session_terminal 拒绝，
                    # 优先于 lease 过期 / 版本冲突等诊断性错误，保持错误语义稳定可预期。
                    result = _result_error("session_terminal", "terminal session cannot be saved")
                else:
                    lease, lease_error = _load_lease(home, session_id)
                    if current.get("leaseToken") is None:
                        result = _result_error("lease_required", "session has no active lease")
                    elif lease_error:
                        # session 声称持有 lease，但 lease 文件缺失或损坏 -> 双文件不一致，fail-closed
                        result = _result_error("lease_state_conflict", "current lease file is missing or invalid")
                    elif lease is None or lease.get("token") != current.get("leaseToken") or lease.get("epoch") != current.get("leaseEpoch"):
                        # lease 文件与 session 镜像不一致（如接管双写中途失败）-> fail-closed，拒绝一切写入
                        result = _result_error("lease_state_conflict", "lease file and session ownership do not match")
                    elif lease_token != current.get("leaseToken") or lease_epoch != current.get("leaseEpoch"):
                        # 调用者携带的 token/epoch 已不是当前持有者 -> fenced
                        result = _result_error("stale_lease_holder", "lease token or epoch does not match current holder")
                    elif session.get("leaseToken") != current.get("leaseToken") or session.get("leaseEpoch") != current.get("leaseEpoch"):
                        result = _result_error("stale_lease_holder", "session lease fields cannot be changed by save")
                    elif now > lease["expiresAtNs"]:
                        result = _result_error("lease_expired", "expired lease cannot save session")
                    elif session.get("version") != current.get("version"):
                        result = _result_error("session_version_conflict", "session version does not match persisted version", status="conflict")
                    else:
                        valid, reason = transition_status(current, session.get("status"))
                        if not valid:
                            result = _result_error("invalid_status_transition", reason or "invalid status transition")
                        else:
                            updated = dict(session)
                            updated["version"] = current["version"] + 1
                            updated["updatedAtNs"] = now
                            try:
                                _atomic_write_json(session_path(home, session_id), updated)
                                result = {"status": "ok", "session": updated, "path": str(session_path(home, session_id))}
                            except OSError as exc:
                                result = _result_error("session_save_failed", f"failed to save session: {exc}")
    return _lock_release_result(held, result)


def finalize_session(
    home: Path = DEFAULT_HOME,
    session_id: str = "",
    *,
    lease_token: str | None = None,
    lease_epoch: int | None = None,
    terminal_status: str = "failed",
    result: object | None = None,
    error: object | None = None,
    extra_fields: dict | None = None,
    now_ns: int | None = None,
) -> dict:
    """在一次短锁临界区内写入终态并释放长期 lease。"""
    if terminal_status not in {"succeeded", "failed", "cancelled"}:
        return _result_error("invalid_terminal_status", "terminal status must be succeeded, failed, or cancelled")
    if not _valid_session_id(session_id):
        return _result_error("invalid_session_id", "session id is invalid")
    if extra_fields is not None and not isinstance(extra_fields, dict):
        return _result_error("session_invalid", "extra_fields must be an object")
    if extra_fields is not None:
        unsupported_fields = [field for field in extra_fields if field not in FINALIZE_EXTRA_FIELDS]
        if unsupported_fields:
            return _result_error(
                "session_invalid",
                f"extra_fields contains unsupported field: {unsupported_fields[0]!r}",
            )

    now = _now_ns(now_ns)
    resource = _session_lock_resource(session_id)
    with _held_session_lock(home, resource) as held:
        lock = held["acquire"]
        if lock.get("status") != "ok":
            operation_result = lock
        else:
            loaded = load_session(home, session_id)
            if loaded.get("status") != "ok":
                operation_result = loaded
            else:
                current = loaded["session"]
                if current.get("status") in TERMINAL_STATUSES:
                    if current.get("status") != terminal_status:
                        operation_result = _result_error("session_terminal", "terminal session cannot be finalized")
                    else:
                        # session 已先落终态但 lease 删除失败时，允许同一终态请求幂等补偿。
                        # token/epoch 任一不匹配时不得删除可能属于其他持有者的文件，
                        # 但同一终态本身已达成，仍按幂等成功返回并明确保留 lease。
                        path = lease_path(home, session_id)
                        lease, lease_error = _load_lease(home, session_id) if path.exists() else (None, None)
                        recovered = False
                        if (
                            lease_error is None
                            and lease is not None
                            and lease.get("token") == lease_token
                            and lease.get("epoch") == lease_epoch
                        ):
                            try:
                                path.unlink()
                                _fsync_directory(path.parent)
                            except OSError as exc:
                                operation_result = _result_error(
                                    "session_finalize_failed", f"failed to finalize session: {exc}"
                                )
                            else:
                                recovered = True
                                operation_result = {
                                    "status": "ok",
                                    "session": current,
                                    "terminalStatus": terminal_status,
                                    "recovered": recovered,
                                    "leaseRetained": False,
                                }
                        else:
                            operation_result = {
                                "status": "ok",
                                "session": current,
                                "terminalStatus": terminal_status,
                                "recovered": recovered,
                                "leaseRetained": path.exists(),
                            }
                else:
                    lease, lease_error = _load_lease(home, session_id)
                    if current.get("leaseToken") is None:
                        operation_result = _result_error("lease_required", "session has no active lease")
                    elif lease_error:
                        operation_result = _result_error("lease_state_conflict", "current lease file is missing or invalid")
                    elif lease is None or lease.get("token") != current.get("leaseToken") or lease.get("epoch") != current.get("leaseEpoch"):
                        operation_result = _result_error("lease_state_conflict", "lease file and session ownership do not match")
                    elif lease_token != current.get("leaseToken") or lease_epoch != current.get("leaseEpoch"):
                        operation_result = _result_error("stale_lease_holder", "lease token or epoch does not match current holder")
                    elif now > lease["expiresAtNs"]:
                        operation_result = _result_error("lease_expired", "expired lease cannot finalize session")
                    else:
                        # 允许 finalize 走 D11 的合法路径到达终态（如
                        # created -> active -> completing -> succeeded）；
                        # 中间过渡步骤只在本函数内完成，不暴露给调用方。
                        status_path = terminal_status_path(current.get("status"), terminal_status)
                        if status_path is None:
                            operation_result = _result_error(
                                "invalid_status_transition",
                                f"invalid status transition: {current.get('status')} -> {terminal_status}",
                            )
                        else:
                            updated = dict(current)
                            if extra_fields is not None:
                                updated.update(extra_fields)
                            updated["status"] = terminal_status
                            updated["leaseToken"] = None
                            updated["updatedAtNs"] = now
                            updated["version"] = current["version"] + 1
                            if result is not None:
                                updated["result"] = result
                            if error is not None:
                                updated["error"] = error

                            validation_error = _validate_session(updated, session_id)
                            if validation_error:
                                operation_result = _result_error("session_invalid", validation_error)
                            else:
                                try:
                                    json.dumps(updated, ensure_ascii=False, indent=2, sort_keys=True)
                                except (TypeError, ValueError) as exc:
                                    operation_result = _result_error(
                                        "session_invalid", f"session must be JSON serializable: {exc}"
                                    )
                                else:
                                    try:
                                        _atomic_write_json(session_path(home, session_id), updated)
                                        lease_path(home, session_id).unlink()
                                        # lease 目录项变化也尽力刷盘，与 session 原子写的目录处理保持一致。
                                        _fsync_directory(lease_path(home, session_id).parent)
                                    except OSError as exc:
                                        operation_result = _result_error(
                                            "session_finalize_failed", f"failed to finalize session: {exc}"
                                        )
                                    else:
                                        operation_result = {
                                            "status": "ok",
                                            "session": updated,
                                            "terminalStatus": terminal_status,
                                        }
    return _lock_release_result(held, operation_result)


def _load_lease(home: Path, session_id: str) -> tuple[dict | None, dict | None]:
    lease, error = _read_json(lease_path(home, session_id), "lease")
    if error:
        return None, error
    assert lease is not None
    reason = _validate_lease(lease, session_id)
    if reason:
        return None, _result_error("lease_invalid", reason)
    return lease, None


def _validate_lease(lease: dict, session_id: str) -> str | None:
    # 必须严格是 int 且非 bool：Python 里 true == 1、1.0 == 1，松散比较会放过非法类型
    version = lease.get("schemaVersion")
    if not isinstance(version, int) or isinstance(version, bool) or version != LEASE_SCHEMA_VERSION:
        return "lease schemaVersion must be 1"
    if lease.get("sessionId") != session_id:
        return "lease sessionId does not match path"
    if not isinstance(lease.get("token"), str) or not lease["token"]:
        return "lease token must be a non-empty string"
    if not isinstance(lease.get("hostname"), str) or not lease["hostname"]:
        return "lease hostname must be a non-empty string"
    for field in ("pid", "epoch", "ttlMs"):
        value = lease.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            return f"lease {field} must be a positive integer"
    for field in ("acquiredAtNs", "renewedAtNs", "expiresAtNs"):
        value = lease.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            return f"lease {field} must be a non-negative integer"
    if lease["renewedAtNs"] < lease["acquiredAtNs"]:
        return "lease renewedAtNs must not precede acquiredAtNs"
    if lease["expiresAtNs"] < lease["renewedAtNs"]:
        return "lease expiresAtNs must not precede renewedAtNs"
    return None


def _take_lease(
    home: Path,
    session_id: str,
    *,
    now_ns: int | None,
    ttl_ms: int,
    require_expired: bool,
) -> dict:
    if not _valid_session_id(session_id):
        return _result_error("invalid_session_id", "session id is invalid")
    if not isinstance(ttl_ms, int) or isinstance(ttl_ms, bool) or ttl_ms <= 0:
        return _result_error("invalid_ttl", "lease ttl_ms must be a positive integer")
    now = _now_ns(now_ns)
    resource = _session_lock_resource(session_id)
    with _held_session_lock(home, resource) as held:
        lock = held["acquire"]
        if lock.get("status") != "ok":
            result = lock
        else:
            loaded = load_session(home, session_id)
            if loaded.get("status") != "ok":
                result = loaded
            else:
                session = loaded["session"]
                path = lease_path(home, session_id)
                if session.get("status") in TERMINAL_STATUSES:
                    result = _result_error("session_terminal", "terminal session lease cannot be changed")
                elif path.exists():
                    old_lease, lease_error = _load_lease(home, session_id)
                    if lease_error:
                        result = lease_error
                    else:
                        assert old_lease is not None
                        if old_lease["token"] != session.get("leaseToken") or old_lease["epoch"] != session.get("leaseEpoch"):
                            result = _result_error("lease_state_conflict", "lease and session ownership fields do not match")
                        elif now <= old_lease["expiresAtNs"]:
                            result = _result_error("active_lease", "session lease has not expired", expiresAtNs=old_lease["expiresAtNs"])
                        else:
                            result = _persist_lease_takeover(home, session_id, session, old_lease["epoch"] + 1, now, ttl_ms)
                elif require_expired:
                    result = _result_error("lease_not_found", "no expired lease exists to recover")
                elif session.get("leaseToken") is not None:
                    result = _result_error("lease_state_conflict", "session references a missing lease")
                else:
                    result = _persist_lease_takeover(home, session_id, session, session["leaseEpoch"] + 1, now, ttl_ms)
    return _lock_release_result(held, result)


def _persist_lease_takeover(home: Path, session_id: str, session: dict, epoch: int, now: int, ttl_ms: int) -> dict:
    path = lease_path(home, session_id)
    token = uuid.uuid4().hex
    lease = {
        "schemaVersion": LEASE_SCHEMA_VERSION,
        "sessionId": session_id,
        "token": token,
        "epoch": epoch,
        "hostname": socket.gethostname(),
        "pid": os.getpid(),
        "acquiredAtNs": now,
        "renewedAtNs": now,
        "expiresAtNs": now + ttl_ms * 1_000_000,
        "ttlMs": ttl_ms,
    }
    updated = dict(session)
    updated["leaseToken"] = token
    updated["leaseEpoch"] = epoch
    updated["updatedAtNs"] = now
    updated["version"] = session["version"] + 1
    try:
        _atomic_write_json(path, lease)
        _atomic_write_json(session_path(home, session_id), updated)
    except OSError as exc:
        return _result_error("lease_acquire_failed", f"failed to persist lease ownership: {exc}")
    return {"status": "ok", "lease": lease, "session": updated, "path": str(path)}


def acquire_lease(
    home: Path = DEFAULT_HOME,
    session_id: str = "",
    *,
    now_ns: int | None = None,
    ttl_ms: int = LEASE_TTL_MS,
) -> dict:
    return _take_lease(home, session_id, now_ns=now_ns, ttl_ms=ttl_ms, require_expired=False)


def release_lease(
    home: Path = DEFAULT_HOME,
    session_id: str = "",
    *,
    lease_token: str | None = None,
    lease_epoch: int | None = None,
) -> dict:
    """幂等释放 lease，并修复 token 已清但 lease 文件残留的部分状态。"""
    if not _valid_session_id(session_id):
        return _result_error("invalid_session_id", "session id is invalid")
    resource = _session_lock_resource(session_id)
    with _held_session_lock(home, resource) as held:
        if held["acquire"].get("status") != "ok":
            result = held["acquire"]
        else:
            loaded = load_session(home, session_id)
            if loaded.get("status") != "ok":
                result = loaded
            else:
                current = loaded["session"]
                path = lease_path(home, session_id)
                lease, lease_error = _load_lease(home, session_id) if path.exists() else (None, None)
                if lease_error:
                    result = lease_error
                elif current.get("leaseToken") is None and lease is None:
                    result = {"status": "ok", "session": current, "released": False}
                elif current.get("leaseToken") not in {None, lease_token} or current.get("leaseEpoch") != lease_epoch:
                    result = _result_error("stale_lease_holder", "lease token or epoch does not match current holder")
                elif lease is not None and (lease.get("token") != lease_token or lease.get("epoch") != lease_epoch):
                    result = _result_error("stale_lease_holder", "lease token or epoch does not match current holder")
                else:
                    updated = dict(current)
                    updated["leaseToken"] = None
                    try:
                        if current.get("leaseToken") is not None:
                            _atomic_write_json(session_path(home, session_id), updated)
                        if path.exists():
                            path.unlink()
                            _fsync_directory(path.parent)
                    except OSError as exc:
                        result = _result_error("lease_release_failed", f"failed to release lease: {exc}")
                    else:
                        result = {"status": "ok", "session": updated, "released": True}
    return _lock_release_result(held, result)


def recover_expired_lease(
    home: Path = DEFAULT_HOME,
    session_id: str = "",
    *,
    now_ns: int | None = None,
    ttl_ms: int = LEASE_TTL_MS,
) -> dict:
    return _take_lease(home, session_id, now_ns=now_ns, ttl_ms=ttl_ms, require_expired=True)


def renew_lease(
    home: Path = DEFAULT_HOME,
    session_id: str = "",
    token: str = "",
    *,
    now_ns: int | None = None,
    ttl_ms: int = LEASE_TTL_MS,
) -> dict:
    if not _valid_session_id(session_id):
        return _result_error("invalid_session_id", "session id is invalid")
    if not isinstance(ttl_ms, int) or isinstance(ttl_ms, bool) or ttl_ms <= 0:
        return _result_error("invalid_ttl", "lease ttl_ms must be a positive integer")
    now = _now_ns(now_ns)
    resource = _session_lock_resource(session_id)
    with _held_session_lock(home, resource) as held:
        lock = held["acquire"]
        if lock.get("status") != "ok":
            result = lock
        else:
            loaded = load_session(home, session_id)
            if loaded.get("status") != "ok":
                result = loaded
            else:
                session = loaded["session"]
                if session.get("status") in TERMINAL_STATUSES:
                    result = _result_error("session_terminal", "terminal session lease cannot be changed")
                else:
                    lease, lease_error = _load_lease(home, session_id)
                    if lease_error:
                        result = lease_error
                    else:
                        assert lease is not None
                        if not token or token != lease["token"] or token != session.get("leaseToken"):
                            result = _result_error("stale_lease_holder", "lease token does not match current holder")
                        elif lease["epoch"] != session.get("leaseEpoch"):
                            result = _result_error("stale_lease_holder", "lease epoch does not match session epoch")
                        elif now > lease["expiresAtNs"]:
                            result = _result_error("lease_expired", "expired lease cannot be renewed")
                        else:
                            base = max(now, lease["renewedAtNs"])
                            renewed = dict(lease)
                            renewed["renewedAtNs"] = base
                            renewed["expiresAtNs"] = max(lease["expiresAtNs"], base + ttl_ms * 1_000_000)
                            renewed["ttlMs"] = ttl_ms
                            updated = dict(session)
                            updated["updatedAtNs"] = now
                            updated["version"] = session["version"] + 1
                            try:
                                _atomic_write_json(lease_path(home, session_id), renewed)
                                _atomic_write_json(session_path(home, session_id), updated)
                            except OSError as exc:
                                result = _result_error("lease_renew_failed", f"failed to renew lease: {exc}")
                            else:
                                result = {"status": "ok", "lease": renewed, "session": updated}
    return _lock_release_result(held, result)
