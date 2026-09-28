from __future__ import annotations

import json
import os
import queue
import shutil
import signal
import stat
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Iterator

from .config import extension_registry_paths, read_text_config
from .paths import DEFAULT_HOME


SUPPORTED_ADAPTER_TYPES = {"filesystem", "local_cli"}
TEXT_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml"}
SENSITIVE_PATH_PARTS = {".secrets", "secrets", "api-keys", "mail", "accounts"}
MAX_FILE_BYTES = 1_000_000
MAX_RESULTS = 100
MAX_TEXT_CHARS = 500
MAX_CLI_OUTPUT_CHARS = 10_000
MAX_CLI_STDERR_DISPLAY_CHARS = 500
MAX_CLI_STDOUT_BYTES = 64_000
MAX_CLI_STDERR_BYTES = 64_000
MAX_SCAN_ENTRIES = 10_000
MAX_SCAN_FILES = 2_000
MAX_TOTAL_READ_BYTES = 10_000_000
MAX_SCAN_SECONDS = 5.0
MAX_ADAPTERS = 20
MAX_SOURCES = 100


def knowledge_registry_path(home: Path = DEFAULT_HOME) -> Path:
    return extension_registry_paths(home)["knowledge"]


def _error_result(path: Path, message: str) -> dict[str, Any]:
    return {
        "status": "error",
        "registry": str(path),
        "adapters": [],
        "sources": [],
        "checks": [{"level": "error", "message": message}],
    }


def _load_registry(home: Path) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    path = knowledge_registry_path(home)
    try:
        registry = read_text_config(path)
    except Exception as exc:  # noqa: BLE001
        return None, _error_result(path, f"failed to read knowledge registry: {exc}")
    if not isinstance(registry, dict):
        return None, _error_result(path, "knowledge registry root must be an object")

    adapters = registry.get("adapters")
    sources = registry.get("sources")
    if registry.get("version") != 1:
        return None, _error_result(path, "knowledge registry version must be 1")
    if not isinstance(adapters, list):
        return None, _error_result(path, "knowledge adapters must be a list")
    if not isinstance(sources, list):
        return None, _error_result(path, "knowledge sources must be a list")
    if len(adapters) > MAX_ADAPTERS:
        return None, _error_result(path, f"knowledge adapter limit exceeded: {MAX_ADAPTERS}")
    if len(sources) > MAX_SOURCES:
        return None, _error_result(path, f"knowledge source limit exceeded: {MAX_SOURCES}")

    seen: set[str] = set()
    for index, adapter in enumerate(adapters):
        if not isinstance(adapter, dict):
            return None, _error_result(path, f"knowledge adapter at index {index} must be an object")
        adapter_id = adapter.get("id")
        adapter_type = adapter.get("type")
        enabled = adapter.get("enabled")
        if not isinstance(adapter_id, str) or not adapter_id.strip():
            return None, _error_result(path, f"knowledge adapter at index {index} id must be non-empty string")
        if adapter_id in seen:
            return None, _error_result(path, f"duplicate knowledge adapter id: {adapter_id}")
        if not isinstance(adapter_type, str) or adapter_type not in SUPPORTED_ADAPTER_TYPES:
            return None, _error_result(path, f"unsupported knowledge adapter type: {adapter_type}")
        if not isinstance(enabled, bool):
            return None, _error_result(path, f"knowledge adapter {adapter_id} enabled must be boolean")
        seen.add(adapter_id)

    seen_sources: set[str] = set()
    for index, source in enumerate(sources):
        if not isinstance(source, dict):
            return None, _error_result(path, f"knowledge source at index {index} must be an object")
        if not isinstance(source.get("id"), str) or not source["id"].strip():
            return None, _error_result(path, f"knowledge source at index {index} id must be non-empty string")
        if source["id"] in seen_sources:
            return None, _error_result(path, f"duplicate knowledge source id: {source['id']}")
        if not isinstance(source.get("adapter"), str) or source["adapter"] not in seen:
            return None, _error_result(path, f"knowledge source {source.get('id', index)} references missing adapter")
        if not isinstance(source.get("path"), str) or not source["path"].strip():
            return None, _error_result(path, f"knowledge source {source['id']} path must be non-empty string")
        if "enabled" in source and not isinstance(source["enabled"], bool):
            return None, _error_result(path, f"knowledge source {source['id']} enabled must be boolean")
        seen_sources.add(source["id"])

    return registry, None


def _adapter_row(adapter: dict[str, Any]) -> dict[str, Any]:
    enabled = adapter["enabled"]
    return {
        "id": adapter["id"],
        "type": adapter["type"],
        "enabled": enabled,
        "status": "available" if enabled else "disabled",
    }


def list_knowledge(home: Path = DEFAULT_HOME) -> dict[str, Any]:
    registry, error = _load_registry(home)
    if error:
        return error
    assert registry is not None

    adapters = [_adapter_row(adapter) for adapter in registry["adapters"]]
    sources = [
        {
            "id": source["id"],
            "adapter": source["adapter"],
            "path": source["path"],
            "enabled": source.get("enabled", True),
        }
        for source in registry["sources"]
    ]
    return {
        "status": "ok",
        "registry": str(knowledge_registry_path(home)),
        "adapters": adapters,
        "sources": sources,
        "checks": [{"level": "ok", "message": "knowledge registry is valid"}],
    }


def _has_sensitive_part(path: Path) -> bool:
    for part in path.parts:
        # 先去掉前导点，避免 .secrets / .accounts.yaml 这类隐藏名绕过
        name = part.casefold().lstrip(".")
        if not name:
            continue
        stem = Path(name).stem
        first_segment = name.split(".", 1)[0]
        if {name, stem, first_segment} & SENSITIVE_PATH_PARTS:
            return True
    return False


def _has_symlink_component(path: Path, home: Path, home_resolved: Path) -> bool:
    """检查 home 以内路径链上的每一段是否为 symlink，任一命中即视为跟随 symlink。

    注意：这里必须用"未解析"的 home 作为基准逐段 lstat，不能用 realpath ——
    后者会把 symlink 解析掉，反而让检查失效。
    """
    relative: Path | None = None
    base = home
    for candidate_base in (home, home_resolved):
        try:
            relative = path.relative_to(candidate_base)
            base = candidate_base
            break
        except ValueError:
            continue
    if relative is None:
        return False
    current = base
    for part in relative.parts:
        current = current / part
        try:
            if current.is_symlink():
                return True
        except OSError:
            return True
    return False


def _authorize_path(path: Path, home: Path) -> tuple[Path | None, str | None]:
    home_resolved = home.resolve()
    try:
        resolved = path.resolve()
    except OSError as exc:
        return None, f"path resolution failed: {exc}"
    if _has_sensitive_part(path) or _has_sensitive_part(resolved):
        return None, "path contains a sensitive name"
    if resolved != home_resolved and home_resolved not in resolved.parents:
        return None, "path resolves outside ORCHAGENT_HOME"
    if _has_symlink_component(path, home, home_resolved):
        return None, "path contains a symlink component"
    return resolved, None


def _safe_source_path(raw_path: str, home: Path) -> tuple[Path | None, str | None]:
    expanded = Path(os.path.expandvars(os.path.expanduser(raw_path)))
    candidate = expanded if expanded.is_absolute() else home / expanded
    return _authorize_path(candidate, home)


def _open_verified(open_path: Path, home_resolved: Path) -> tuple[int | None, str | None]:
    """以 O_NOFOLLOW 打开并校验，避免校验后被替换成越界 symlink（TOCTOU）。

    说明：本函数防的是"校验后路径被替换"这类竞态，不防"攻击者已能任意改写
    ORCHAGENT_HOME 目录"的模型——后者已能直接读取任意文件，不属本适配器边界。
    """
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(open_path, flags)
    except OSError as exc:
        return None, f"open failed: {exc}"
    try:
        stat_result = os.fstat(fd)
        if not stat.S_ISREG(stat_result.st_mode):
            os.close(fd)
            return None, "path is not a regular file"
        if stat_result.st_nlink > 1:
            # 硬链接可让 home 内的良性名称指向 home 外的敏感 inode，直接 fail-closed
            os.close(fd)
            return None, "path has multiple hard links"
        real_path = Path(os.path.realpath(open_path))
        if _has_sensitive_part(real_path):
            os.close(fd)
            return None, "path contains a sensitive name"
        if real_path != home_resolved and home_resolved not in real_path.parents:
            os.close(fd)
            return None, "path resolves outside ORCHAGENT_HOME"
    except OSError as exc:
        os.close(fd)
        return None, f"open verification failed: {exc}"
    return fd, None


def _source_files(
    path: Path,
    deadline: float,
    scan_state: dict[str, int],
) -> Iterator[tuple[str, Path | None, str | None]]:
    if path.is_file():
        if time.monotonic() >= deadline:
            yield "limit", None, f"scan deadline reached: {MAX_SCAN_SECONDS:g}s"
            return
        scan_state["entries"] += 1
        scan_state["files"] += 1
        if scan_state["entries"] > MAX_SCAN_ENTRIES:
            yield "limit", None, f"scan entry limit reached: {MAX_SCAN_ENTRIES}"
            return
        if scan_state["files"] > MAX_SCAN_FILES:
            yield "limit", None, f"scan file limit reached: {MAX_SCAN_FILES}"
            return
        if path.suffix.casefold() in TEXT_SUFFIXES:
            yield "file", path, None
        return
    if not path.is_dir():
        return

    pending = [path]
    while pending:
        if time.monotonic() >= deadline:
            yield "limit", None, f"scan deadline reached: {MAX_SCAN_SECONDS:g}s"
            return
        directory = pending.pop()
        try:
            with os.scandir(directory) as entries:
                for entry in entries:
                    scan_state["entries"] += 1
                    if scan_state["entries"] > MAX_SCAN_ENTRIES:
                        yield "limit", None, f"scan entry limit reached: {MAX_SCAN_ENTRIES}"
                        return
                    if time.monotonic() >= deadline:
                        yield "limit", None, f"scan deadline reached: {MAX_SCAN_SECONDS:g}s"
                        return
                    entry_path = Path(entry.path)
                    try:
                        if entry.is_symlink():
                            yield "skip", entry_path, "symlink skipped"
                        elif entry.is_dir(follow_symlinks=False):
                            pending.append(entry_path)
                        elif entry.is_file(follow_symlinks=False) and entry_path.suffix.casefold() in TEXT_SUFFIXES:
                            scan_state["files"] += 1
                            if scan_state["files"] > MAX_SCAN_FILES:
                                yield "limit", None, f"scan file limit reached: {MAX_SCAN_FILES}"
                                return
                            yield "file", entry_path, None
                    except OSError as exc:
                        yield "skip", entry_path, f"entry inspection failed: {exc}"
        except OSError as exc:
            yield "skip", directory, f"directory scan failed: {exc}"


def _search_filesystem(
    query: str,
    source: dict[str, Any],
    home: Path,
    results: list[dict[str, Any]],
    skipped: list[dict[str, Any]],
    errors: list[dict[str, Any]],
    scan_state: dict[str, int],
    deadline: float,
) -> None:
    source_id = source["id"]
    path, reason = _safe_source_path(source["path"], home)
    if reason:
        skipped.append({"source": source_id, "reason": reason})
        return
    assert path is not None
    if not path.exists():
        errors.append({"source": source_id, "message": "source path does not exist"})
        return

    home_resolved = home.resolve()
    query_folded = query.casefold()
    for item_type, file_path, item_reason in _source_files(path, deadline, scan_state):
        if item_type == "limit":
            skipped.append({"source": source_id, "reason": item_reason})
            break
        if item_type == "skip":
            skipped.append({"source": source_id, "path": str(file_path), "reason": item_reason})
            continue
        assert file_path is not None
        if time.monotonic() >= deadline:
            skipped.append({"source": source_id, "reason": f"scan deadline reached: {MAX_SCAN_SECONDS:g}s"})
            break
        if len(results) >= MAX_RESULTS:
            skipped.append({"source": source_id, "reason": f"result limit reached: {MAX_RESULTS}"})
            break
        resolved, unsafe_reason = _authorize_path(file_path, home)
        if unsafe_reason:
            skipped.append({"source": source_id, "path": str(file_path), "reason": unsafe_reason})
            continue
        assert resolved is not None
        if not resolved.is_file():
            continue
        fd, open_reason = _open_verified(file_path, home_resolved)
        if fd is None:
            skipped.append({"source": source_id, "path": str(file_path), "reason": open_reason})
            continue
        try:
            file_size = os.fstat(fd).st_size
            if file_size > MAX_FILE_BYTES:
                skipped.append({"source": source_id, "path": str(file_path), "reason": "file exceeds size limit"})
                continue
            if scan_state["read_bytes"] + file_size > MAX_TOTAL_READ_BYTES:
                skipped.append({
                    "source": source_id,
                    "reason": f"cumulative read limit reached: {MAX_TOTAL_READ_BYTES} bytes",
                })
                break
            remaining = MAX_TOTAL_READ_BYTES - scan_state["read_bytes"]
            with os.fdopen(fd, "rb") as stream:
                fd = None
                content = stream.read(min(MAX_FILE_BYTES, remaining) + 1)
            if len(content) > MAX_FILE_BYTES:
                skipped.append({"source": source_id, "path": str(file_path), "reason": "file exceeds size limit"})
                continue
            if len(content) > remaining:
                skipped.append({
                    "source": source_id,
                    "reason": f"cumulative read limit reached: {MAX_TOTAL_READ_BYTES} bytes",
                })
                break
            scan_state["read_bytes"] += len(content)
            for line_number, line in enumerate(content.decode("utf-8").splitlines(), start=1):
                if query_folded in line.casefold():
                    results.append({
                        "source": source_id,
                        "path": str(resolved.relative_to(home_resolved)),
                        "line": line_number,
                        "text": line.strip()[:MAX_TEXT_CHARS],
                    })
                    if len(results) >= MAX_RESULTS:
                        break
        except (OSError, UnicodeError) as exc:
            errors.append({"source": source_id, "path": str(resolved), "message": str(exc)})
        finally:
            if fd is not None:
                os.close(fd)


def _read_bounded_stream(
    stream: Any,
    limit: int,
    output: bytearray,
    exceeded: threading.Event,
    exceeded_streams: queue.Queue[str],
    stream_name: str,
) -> None:
    try:
        while len(output) <= limit:
            chunk = stream.read(min(64 * 1024, limit + 1 - len(output)))
            if not chunk:
                return
            output.extend(chunk)
            if len(output) > limit:
                exceeded_streams.put(stream_name)
                exceeded.set()
                return
    finally:
        stream.close()


def _kill_process_group(pgid: int, sig: int) -> None:
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError, OSError):
        return


def _stop_process(process: subprocess.Popen[bytes], pgid: int) -> None:
    """终止子进程及其整个进程组，避免派生的孙进程继续占住 stdout/stderr 管道。"""
    _kill_process_group(pgid, signal.SIGTERM)
    if process.poll() is None:
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            pass
    _kill_process_group(pgid, signal.SIGKILL)
    try:
        process.wait(timeout=1)
    except subprocess.TimeoutExpired:
        pass


def _join_readers(readers: list[threading.Thread]) -> bool:
    """等待 reader 退出；返回是否全部退出。管道未关闭时 reader 可能仍阻塞。"""
    for reader in readers:
        reader.join(timeout=1)
    return all(not reader.is_alive() for reader in readers)


def _close_reader_streams(process: subprocess.Popen[bytes]) -> None:
    """reader 无法退出时关闭父端管道，至少释放 fd 并让阻塞读结束。"""
    for stream in (process.stdout, process.stderr):
        if stream is None:
            continue
        try:
            stream.close()
        except OSError:
            continue


def _stop_and_join(
    process: subprocess.Popen[bytes],
    pgid: int,
    readers: list[threading.Thread],
) -> bool:
    """终止整组并确认 reader 全部退出；返回是否清理干净。"""
    _stop_process(process, pgid)
    if _join_readers(readers):
        return True
    _close_reader_streams(process)
    return _join_readers(readers)


def _exceeded_error(adapter_id: str, exceeded_streams: queue.Queue[str]) -> dict[str, Any]:
    try:
        stream_name = exceeded_streams.get_nowait()
    except queue.Empty:
        stream_name = "stdout"
    limit = MAX_CLI_STDOUT_BYTES if stream_name == "stdout" else MAX_CLI_STDERR_BYTES
    return {"adapter": adapter_id, "message": f"local_cli {stream_name} exceeded byte limit: {limit}"}


def _search_local_cli(
    query: str,
    adapter: dict[str, Any],
    results: list[dict[str, Any]],
    errors: list[dict[str, Any]],
) -> None:
    adapter_id = adapter["id"]
    command = adapter.get("command")
    args = adapter.get("args", [])
    if not isinstance(command, str) or not command.strip():
        errors.append({"adapter": adapter_id, "message": "local_cli command must be non-empty string"})
        return
    if not isinstance(args, list) or any(not isinstance(arg, str) for arg in args):
        errors.append({"adapter": adapter_id, "message": "local_cli args must be a list of strings"})
        return
    executable = shutil.which(command)
    if executable is None:
        errors.append({"adapter": adapter_id, "message": f"command not available: {command}"})
        return
    expanded_args = [os.path.expandvars(os.path.expanduser(arg)) for arg in args]
    stdout = bytearray()
    stderr = bytearray()
    exceeded = threading.Event()
    exceeded_streams: queue.Queue[str] = queue.Queue()
    try:
        process = subprocess.Popen(
            [executable, *expanded_args, "search", query],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
    except OSError as exc:
        errors.append({"adapter": adapter_id, "message": str(exc)})
        return
    pgid = process.pid
    assert process.stdout is not None and process.stderr is not None
    readers = [
        threading.Thread(
            target=_read_bounded_stream,
            args=(process.stdout, MAX_CLI_STDOUT_BYTES, stdout, exceeded, exceeded_streams, "stdout"),
            daemon=True,
        ),
        threading.Thread(
            target=_read_bounded_stream,
            args=(process.stderr, MAX_CLI_STDERR_BYTES, stderr, exceeded, exceeded_streams, "stderr"),
            daemon=True,
        ),
    ]
    for reader in readers:
        reader.start()

    deadline = time.monotonic() + 15
    while process.poll() is None and not exceeded.is_set() and time.monotonic() < deadline:
        time.sleep(0.02)
    if exceeded.is_set():
        clean = _stop_and_join(process, pgid, readers)
        errors.append(_exceeded_error(adapter_id, exceeded_streams))
        if not clean:
            errors.append({"adapter": adapter_id, "message": "local_cli reader threads did not exit cleanly"})
        return
    if process.poll() is None:
        clean = _stop_and_join(process, pgid, readers)
        errors.append({"adapter": adapter_id, "message": "local_cli timed out after 15 seconds"})
        if not clean:
            errors.append({"adapter": adapter_id, "message": "local_cli reader threads did not exit cleanly"})
        return
    if not _join_readers(readers):
        if not _stop_and_join(process, pgid, readers):
            errors.append({"adapter": adapter_id, "message": "local_cli reader threads did not exit cleanly"})
            return
    if exceeded.is_set():
        errors.append(_exceeded_error(adapter_id, exceeded_streams))
        return

    stdout_text = bytes(stdout).decode("utf-8", errors="replace").strip()
    stderr_text = bytes(stderr).decode("utf-8", errors="replace").strip()
    if process.returncode != 0:
        errors.append({
            "adapter": adapter_id,
            "message": f"local_cli exited with {process.returncode}",
            "stderr": stderr_text[:MAX_CLI_STDERR_DISPLAY_CHARS],
        })
        return
    if stdout_text:
        results.append({"adapter": adapter_id, "text": stdout_text[:MAX_CLI_OUTPUT_CHARS]})


def search_knowledge(query: str, home: Path = DEFAULT_HOME) -> dict[str, Any]:
    if not query.strip():
        return {
            "status": "error",
            "query": query,
            "results": [],
            "skipped": [],
            "errors": [{"level": "error", "message": "knowledge search query must not be empty"}],
        }
    registry, error = _load_registry(home)
    if error:
        return {
            "status": "error",
            "query": query,
            "results": [],
            "skipped": [],
            "errors": error["checks"],
        }
    assert registry is not None

    results: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    adapters = {adapter["id"]: adapter for adapter in registry["adapters"]}
    scan_state = {"entries": 0, "files": 0, "read_bytes": 0}

    for adapter in registry["adapters"]:
        if adapter["type"] != "local_cli":
            continue
        if not adapter["enabled"]:
            skipped.append({"adapter": adapter["id"], "reason": "adapter disabled"})
            continue
        _search_local_cli(query, adapter, results, errors)

    # filesystem 的遍历 deadline 必须在真正开始扫描前才起算，
    # 否则慢的 local CLI 会提前吃掉 filesystem 的预算。
    scan_deadline = time.monotonic() + MAX_SCAN_SECONDS

    for source in registry["sources"]:
        adapter = adapters[source["adapter"]]
        if adapter["type"] != "filesystem":
            continue
        if not adapter["enabled"]:
            skipped.append({"source": source["id"], "reason": "adapter disabled"})
        elif not source.get("enabled", True):
            skipped.append({"source": source["id"], "reason": "source disabled"})
        else:
            _search_filesystem(
                query,
                source,
                home,
                results,
                skipped,
                errors,
                scan_state,
                scan_deadline,
            )

    return {
        "status": "ok",
        "query": query,
        "results": results,
        "skipped": skipped,
        "errors": errors,
    }
