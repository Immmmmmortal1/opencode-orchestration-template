from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

MANIFEST_VERSION = 1
MANIFEST_NAME = "manifest.json"
LOCK_NAME = ".console.lock"


class AtomicWriteError(OSError):
    def __init__(self, replaced: bool) -> None:
        super().__init__("atomic_write_failed")
        self.replaced = replaced


def file_digest(path: Path) -> str:
    """返回普通文件的 SHA-256；调用方负责限定允许访问的根目录。"""
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _error(code: str, *, restored: bool | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {"status": "error", "error": code}
    if restored is not None:
        result["restored"] = restored
    return result


def _home_path(home: Path) -> Path:
    home = Path(home)
    if not home.is_absolute():
        home = home.absolute()
    try:
        mode = os.lstat(home).st_mode
    except OSError as exc:
        raise ValueError("home_unavailable") from exc
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ValueError("home_invalid")
    return home


def _safe_target(home: Path, relative_path: str, *, allow_missing: bool) -> Path:
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError("invalid_relative_path")
    relative = Path(relative_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("invalid_relative_path")

    current = home
    for part in relative.parts[:-1]:
        current = current / part
        try:
            mode = os.lstat(current).st_mode
        except OSError as exc:
            raise ValueError("parent_unavailable") from exc
        if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
            raise ValueError("parent_invalid")

    target = home / relative
    try:
        mode = os.lstat(target).st_mode
    except FileNotFoundError:
        if allow_missing:
            return target
        raise ValueError("file_unavailable")
    except OSError as exc:
        raise ValueError("file_unavailable") from exc
    if stat.S_ISLNK(mode):
        raise ValueError("symlink_rejected")
    if not stat.S_ISREG(mode):
        raise ValueError("not_regular_file")
    return target


def _digest_or_none(path: Path) -> str | None:
    try:
        mode = os.lstat(path).st_mode
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(mode):
        raise ValueError("symlink_rejected")
    if not stat.S_ISREG(mode):
        raise ValueError("not_regular_file")
    return file_digest(path)


def snapshot_files(home: Path, paths: Sequence[str]) -> dict[str, Any]:
    """读取 home 内相对路径的 hash；不存在的目标用 ``None`` 表示。"""
    try:
        safe_home = _home_path(home)
        hashes = {relative: _read_home_file_with_digest(safe_home, relative)[1] for relative in paths}
    except (OSError, ValueError):
        return _error("snapshot_failed")
    return {"status": "ok", "hashes": hashes}


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_write(path: Path, content: bytes) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    replaced = False
    try:
        try:
            os.fchmod(descriptor, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = -1
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            replaced = True
            os.chmod(path, 0o600)
            _fsync_directory(path.parent)
        except OSError as exc:
            raise AtomicWriteError(replaced) from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    _atomic_write(path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _acquire_lock(home: Path) -> int:
    lock_path = home / LOCK_NAME
    try:
        mode = os.lstat(lock_path).st_mode
    except FileNotFoundError:
        mode = None
    if mode is not None and (stat.S_ISLNK(mode) or not stat.S_ISREG(mode)):
        raise ValueError("lock_invalid")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(lock_path, flags, 0o600)
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(lock_path)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or opened.st_dev != current.st_dev
            or opened.st_ino != current.st_ino
        ):
            raise ValueError("lock_invalid")
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = descriptor
        descriptor = -1
        return result
    except BlockingIOError as exc:
        raise ValueError("lock_busy") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _release_lock(descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def _open_backup_root(home: Path, *, create: bool = True) -> tuple[Path, int, os.stat_result]:
    backup_root = Path(f"{home}.backups")
    try:
        mode = os.lstat(backup_root).st_mode
    except FileNotFoundError:
        if not create:
            raise
        backup_root.mkdir(exist_ok=False, mode=0o700)
        mode = os.lstat(backup_root).st_mode
    if stat.S_ISLNK(mode) or not stat.S_ISDIR(mode):
        raise ValueError("backup_root_invalid")
    descriptor = -1
    try:
        descriptor = os.open(
            backup_root,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
        )
        opened = os.fstat(descriptor)
        current = os.lstat(backup_root)
        if (
            not stat.S_ISDIR(opened.st_mode)
            or stat.S_ISLNK(current.st_mode)
            or opened.st_dev != current.st_dev
            or opened.st_ino != current.st_ino
        ):
            raise ValueError("backup_root_invalid")
        if create:
            os.fchmod(descriptor, 0o700)
        result = descriptor
        descriptor = -1
        return backup_root, result, opened
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_dir_at(parent_fd: int, name: str) -> int:
    descriptor = -1
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=parent_fd,
        )
        if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
            raise ValueError("backup_invalid")
        result = descriptor
        descriptor = -1
        return result
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _ensure_dir_at(parent_fd: int, name: str) -> int:
    if not name or name in {".", ".."} or "/" in name:
        raise ValueError("manifest_invalid")
    try:
        os.mkdir(name, mode=0o700, dir_fd=parent_fd)
    except FileExistsError:
        pass
    descriptor = _open_dir_at(parent_fd, name)
    try:
        os.fchmod(descriptor, 0o700)
        result = descriptor
        descriptor = -1
        return result
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _open_parent_dir_for_file(base_fd: int, relative_path: str, *, create: bool) -> tuple[int, str]:
    relative = Path(relative_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("manifest_invalid")
    if len(relative.parts) == 0:
        raise ValueError("manifest_invalid")
    descriptor = os.dup(base_fd)
    try:
        for part in relative.parts[:-1]:
            next_descriptor = _ensure_dir_at(descriptor, part) if create else _open_dir_at(descriptor, part)
            os.close(descriptor)
            descriptor = next_descriptor
        return descriptor, relative.parts[-1]
    except Exception:
        os.close(descriptor)
        raise


def _write_file_at(base_fd: int, relative_path: str, content: bytes) -> None:
    parent_fd, name = _open_parent_dir_for_file(base_fd, relative_path, create=True)
    temporary = f".{name}.tmp-{os.getpid()}-{time.time_ns()}"
    descriptor = -1
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
        view = memoryview(content)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("short_write")
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.rename(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
        os.fsync(parent_fd)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=parent_fd)
        except FileNotFoundError:
            pass
        os.close(parent_fd)


def _read_file_at(base_fd: int, relative_path: str) -> bytes:
    parent_fd, name = _open_parent_dir_for_file(base_fd, relative_path, create=False)
    descriptor = -1
    try:
        descriptor = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=parent_fd)
        mode = os.fstat(descriptor).st_mode
        if not stat.S_ISREG(mode):
            raise ValueError("manifest_invalid")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_fd)


def _read_home_file(home: Path, relative_path: str) -> bytes:
    home_descriptor = os.open(
        home,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        return _read_file_at(home_descriptor, relative_path)
    finally:
        os.close(home_descriptor)


def _read_home_file_with_digest(home: Path, relative_path: str) -> tuple[bytes | None, str | None]:
    home_descriptor = os.open(
        home,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        try:
            content = _read_file_at(home_descriptor, relative_path)
        except FileNotFoundError:
            return None, None
        return content, hashlib.sha256(content).hexdigest()
    finally:
        os.close(home_descriptor)


def _atomic_write_home_file(home: Path, relative_path: str, content: bytes) -> None:
    home_descriptor = os.open(
        home,
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        parent_fd, name = _open_parent_dir_for_file(home_descriptor, relative_path, create=False)
        temporary = f".{name}.tmp-{os.getpid()}-{time.time_ns()}"
        descriptor = -1
        replaced = False
        try:
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=parent_fd)
            view = memoryview(content)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise OSError("short_write")
                view = view[written:]
            os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            os.rename(temporary, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
            replaced = True
            os.fsync(parent_fd)
        except OSError as exc:
            raise AtomicWriteError(replaced) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=parent_fd)
            except FileNotFoundError:
                pass
            os.close(parent_fd)
    finally:
        os.close(home_descriptor)


def _unlink_home_file(home: Path, relative_path: str) -> None:
    home_descriptor = os.open(home, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0))
    try:
        parent_fd, name = _open_parent_dir_for_file(home_descriptor, relative_path, create=False)
        try:
            os.unlink(name, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    finally:
        os.close(home_descriptor)


def _write_json_at(base_fd: int, relative_path: str, value: Mapping[str, Any]) -> None:
    _write_file_at(base_fd, relative_path, (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8"))


def _read_json_at(base_fd: int, relative_path: str) -> dict[str, Any]:
    try:
        manifest = json.loads(_read_file_at(base_fd, relative_path).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest_invalid") from exc
    if not isinstance(manifest, dict):
        raise ValueError("manifest_invalid")
    return manifest


def _create_console_backup_dir(backup_root: Path, root_descriptor: int, kind: str) -> tuple[Path, int, int, int]:
    created_at_ns = time.time_ns()
    base_name = f"{kind}-{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}"
    sequence = 0
    while True:
        name = base_name if sequence == 0 else f"{base_name}-{sequence}"
        try:
            os.mkdir(name, mode=0o700, dir_fd=root_descriptor)
        except FileExistsError:
            sequence += 1
            continue
        child = backup_root / name
        child_descriptor = -1
        try:
            child_descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=root_descriptor,
            )
            opened = os.fstat(child_descriptor)
            current = os.lstat(child)
            if (
                not stat.S_ISDIR(opened.st_mode)
                or stat.S_ISLNK(current.st_mode)
                or opened.st_dev != current.st_dev
                or opened.st_ino != current.st_ino
            ):
                raise ValueError("backup_invalid")
            os.fchmod(child_descriptor, 0o700)
            result = child_descriptor
            child_descriptor = -1
            return child, result, created_at_ns, sequence
        finally:
            if child_descriptor >= 0:
                os.close(child_descriptor)


def _prepare_backup(home: Path, kind: str, targets: Mapping[str, Path]) -> tuple[Path, int, dict[str, Any]]:
    backup_root, root_descriptor, _ = _open_backup_root(home)
    backup_descriptor = -1
    try:
        backup_dir, backup_descriptor, created_at_ns, sequence = _create_console_backup_dir(backup_root, root_descriptor, kind)
    finally:
        os.close(root_descriptor)
    files: list[dict[str, Any]] = []
    try:
        for relative, target in targets.items():
            content, old_hash = _read_home_file_with_digest(home, relative)
            entry: dict[str, Any] = {
                "path": relative,
                "type": "file",
                "existed": old_hash is not None,
                "hash": old_hash,
            }
            if old_hash is not None:
                backup_relative = Path("files") / Path(relative)
                assert content is not None
                _write_file_at(backup_descriptor, backup_relative.as_posix(), content)
                entry["backup"] = backup_relative.as_posix()
            files.append(entry)
        manifest: dict[str, Any] = {
            "version": MANIFEST_VERSION,
            "kind": kind,
            "createdAtNs": created_at_ns,
            "sequence": sequence,
            "files": files,
            "consumed": False,
        }
        _write_json_at(backup_descriptor, MANIFEST_NAME, manifest)
        return backup_dir, backup_descriptor, manifest
    except Exception:
        # 不删除失败备份，保留现场；目标文件尚未写入。
        if backup_descriptor >= 0:
            os.close(backup_descriptor)
        raise


def _backup_entry_bytes(backup_descriptor: int, entry: Mapping[str, Any]) -> bytes:
    backup = entry.get("backup")
    expected_hash = entry.get("hash")
    if not isinstance(backup, str) or not isinstance(expected_hash, str):
        raise ValueError("manifest_invalid")
    content = _read_file_at(backup_descriptor, backup)
    if hashlib.sha256(content).hexdigest() != expected_hash:
        raise ValueError("backup_hash_mismatch")
    return content


def _restore_entries(home: Path, backup_descriptor: int, entries: Sequence[Mapping[str, Any]]) -> bool:
    restored = True
    for entry in reversed(entries):
        try:
            _safe_target(home, str(entry["path"]), allow_missing=True)
            if entry.get("existed") is True:
                _atomic_write_home_file(home, str(entry["path"]), _backup_entry_bytes(backup_descriptor, entry))
            else:
                try:
                    _unlink_home_file(home, str(entry["path"]))
                except FileNotFoundError:
                    pass
        except (KeyError, OSError, ValueError):
            restored = False
    return restored


def apply_config_transaction(
    home: Path,
    files: Mapping[str, str],
    expected_hashes: Mapping[str, str | None],
    kind: str = "console",
    watch_hashes: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """在短 flock 内校验快照、统一备份并原子替换一组配置文件。"""
    if not files or set(files) != set(expected_hashes):
        return _error("invalid_transaction")
    if not isinstance(kind, str) or not kind or any(character not in "abcdefghijklmnopqrstuvwxyz0123456789-_" for character in kind):
        return _error("invalid_kind")
    try:
        safe_home = _home_path(home)
        watch = dict(watch_hashes or {})
        watch.update(expected_hashes)
        targets = {relative: _safe_target(safe_home, relative, allow_missing=True) for relative in files}
        watch_targets = {relative: _safe_target(safe_home, relative, allow_missing=True) for relative in watch}
        encoded = {relative: text.encode("utf-8") for relative, text in files.items() if isinstance(text, str)}
        if len(encoded) != len(files):
            return _error("invalid_content")
        lock_descriptor = _acquire_lock(safe_home)
    except ValueError as exc:
        return _error(str(exc))
    except (OSError, UnicodeError):
        return _error("transaction_unavailable")

    try:
        try:
            current_hashes = {relative: _read_home_file_with_digest(safe_home, relative)[1] for relative in watch_targets}
        except (OSError, ValueError):
            return _error("snapshot_failed")
        if current_hashes != watch:
            return _error("hash_mismatch")
        try:
            backup_dir, backup_descriptor, manifest = _prepare_backup(safe_home, kind, targets)
        except (OSError, ValueError):
            return _error("backup_failed")

        replaced: list[Mapping[str, Any]] = []
        entries = {str(entry["path"]): entry for entry in manifest["files"]}
        try:
            for relative, target in targets.items():
                # 备份与替换之间再次拒绝被换成 symlink/特殊文件的目标。
                _safe_target(safe_home, relative, allow_missing=True)
                _atomic_write_home_file(safe_home, relative, encoded[relative])
                replaced.append(entries[relative])
        except AtomicWriteError as exc:
            if exc.replaced:
                replaced.append(entries[relative])
            return _error("write_failed", restored=_restore_entries(safe_home, backup_descriptor, replaced))
        except (OSError, ValueError):
            return _error("write_failed", restored=_restore_entries(safe_home, backup_descriptor, replaced))

        try:
            new_hashes = {relative: _read_home_file_with_digest(safe_home, relative)[1] for relative in targets}
        except (OSError, ValueError):
            return _error("post_write_snapshot_failed", restored=_restore_entries(safe_home, backup_descriptor, replaced))
        return {"status": "ok", "hashes": new_hashes, "backupId": backup_dir.name, "backupDir": str(backup_dir)}
    finally:
        if "backup_descriptor" in locals():
            os.close(backup_descriptor)
        _release_lock(lock_descriptor)


def _resolve_backup(home: Path, backup_id_or_path: str | Path) -> tuple[Path, int]:
    backup_root, root_descriptor, _ = _open_backup_root(home, create=False)
    descriptor = -1
    try:
        candidate = Path(backup_id_or_path)
        if candidate.is_absolute():
            if candidate.parent != backup_root:
                raise ValueError("backup_outside_root")
            name = candidate.name
        else:
            if len(candidate.parts) != 1 or candidate.name in {"", ".", ".."}:
                raise ValueError("invalid_backup_id")
            name = candidate.name
        descriptor = _open_dir_at(root_descriptor, name)
    except OSError as exc:
        raise ValueError("backup_unavailable") from exc
    finally:
        os.close(root_descriptor)
    return backup_root / name, descriptor


def _load_manifest(backup_descriptor: int) -> dict[str, Any]:
    try:
        manifest = _read_json_at(backup_descriptor, MANIFEST_NAME)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("manifest_invalid") from exc
    required = {"version", "kind", "createdAtNs", "sequence", "files", "consumed"}
    if not isinstance(manifest, dict) or set(manifest) != required:
        raise ValueError("manifest_invalid")
    if (
        isinstance(manifest["version"], bool)
        or not isinstance(manifest["version"], int)
        or manifest["version"] != MANIFEST_VERSION
        or not isinstance(manifest["files"], list)
    ):
        raise ValueError("manifest_invalid")
    return manifest


def list_config_backups(home: Path) -> dict[str, Any]:
    """列出可识别的 console 备份，仅返回 manifest 中的脱敏摘要。"""
    try:
        safe_home = _home_path(home)
        try:
            backup_root, root_descriptor, _ = _open_backup_root(safe_home, create=False)
        except FileNotFoundError:
            return {"status": "ok", "backups": []}
        except ValueError as exc:
            return _error(str(exc))
        backups: list[dict[str, Any]] = []
        try:
            for name in os.listdir(root_descriptor):
                candidate_descriptor = -1
                try:
                    candidate_descriptor = _open_dir_at(root_descriptor, name)
                    manifest = _load_manifest(candidate_descriptor)
                    if manifest.get("kind") != "console":
                        continue
                    created_at_ns = manifest.get("createdAtNs")
                    sequence = manifest.get("sequence")
                    consumed = manifest.get("consumed")
                    entries = manifest.get("files")
                    if (
                        not isinstance(created_at_ns, int)
                        or isinstance(created_at_ns, bool)
                        or not isinstance(sequence, int)
                        or isinstance(sequence, bool)
                        or not isinstance(consumed, bool)
                        or not isinstance(entries, list)
                    ):
                        continue
                    files: list[dict[str, Any]] = []
                    for entry in entries:
                        if not isinstance(entry, dict) or entry.get("type") != "file":
                            raise ValueError("manifest_invalid")
                        relative = entry.get("path")
                        existed = entry.get("existed")
                        digest = entry.get("hash")
                        _safe_target(safe_home, relative, allow_missing=True)
                        if not isinstance(existed, bool) or (digest is not None and not isinstance(digest, str)):
                            raise ValueError("manifest_invalid")
                        files.append({"path": relative, "existed": existed, "hash": digest})
                    backups.append(
                        {
                            "backupId": name,
                            "createdAtNs": created_at_ns,
                            "sequence": sequence,
                            "files": files,
                            "consumed": consumed,
                        }
                    )
                except (OSError, UnicodeError, ValueError, KeyError):
                    continue
                finally:
                    if candidate_descriptor >= 0:
                        os.close(candidate_descriptor)
        finally:
            os.close(root_descriptor)
        backups.sort(key=lambda item: (item["createdAtNs"], item["sequence"], item["backupId"]), reverse=True)
        return {"status": "ok", "backups": backups}
    except (OSError, ValueError):
        return _error("backup_list_unavailable")


def _safe_backup_file(backup_descriptor: int, relative_path: str) -> None:
    if not isinstance(relative_path, str) or not relative_path:
        raise ValueError("manifest_invalid")
    relative = Path(relative_path)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise ValueError("manifest_invalid")
    _read_file_at(backup_descriptor, relative.as_posix())


def _validate_manifest_entries(backup_descriptor: int, entries: Sequence[Mapping[str, Any]]) -> None:
    for entry in entries:
        if not isinstance(entry, dict) or entry.get("type") != "file":
            raise ValueError("manifest_invalid")
        relative = entry.get("path")
        existed = entry.get("existed")
        digest = entry.get("hash")
        if not isinstance(relative, str) or not isinstance(existed, bool):
            raise ValueError("manifest_invalid")
        if existed is True:
            if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("manifest_invalid")
            _backup_entry_bytes(backup_descriptor, entry)
        elif digest is not None or "backup" in entry:
            raise ValueError("manifest_invalid")


def rollback_config_transaction(
    home: Path,
    backup_id_or_path: str | Path,
    expected_hashes: Mapping[str, str | None] | None = None,
) -> dict[str, Any]:
    """一次性消费 console manifest，并恢复备份时的文件内容与存在性。"""
    try:
        safe_home = _home_path(home)
        backup_dir, backup_descriptor = _resolve_backup(safe_home, backup_id_or_path)
        try:
            lock_descriptor = _acquire_lock(safe_home)
        except Exception:
            os.close(backup_descriptor)
            raise
    except ValueError as exc:
        return _error(str(exc))
    except OSError:
        return _error("rollback_unavailable")

    try:
        try:
            manifest = _load_manifest(backup_descriptor)
            if manifest.get("kind") != "console":
                return _error("manifest_kind_rejected")
            if manifest.get("consumed") is not False:
                return _error("manifest_consumed")
            entries = manifest["files"]
            targets: dict[str, Path] = {}
            if not isinstance(entries, list):
                return _error("manifest_invalid")
            _validate_manifest_entries(backup_descriptor, entries)
            for entry in entries:
                relative = entry.get("path")
                assert isinstance(relative, str)
                targets[relative] = _safe_target(safe_home, relative, allow_missing=True)
        except (KeyError, OSError, ValueError):
            return _error("manifest_invalid")

        if expected_hashes is not None:
            if set(expected_hashes) != set(targets):
                return _error("invalid_expected_hashes")
            try:
                current = {relative: _read_home_file_with_digest(safe_home, relative)[1] for relative in targets}
            except (OSError, ValueError):
                return _error("snapshot_failed")
            if current != dict(expected_hashes):
                return _error("hash_mismatch")

        if not _restore_entries(safe_home, backup_descriptor, entries):
            return _error("rollback_failed", restored=False)
        hashes = {relative: _read_home_file_with_digest(safe_home, relative)[1] for relative in targets}
        for entry in entries:
            relative = str(entry["path"])
            if entry.get("existed") is True and hashes.get(relative) != entry.get("hash"):
                return _error("rollback_failed", restored=False)
            if entry.get("existed") is False and hashes.get(relative) is not None:
                return _error("rollback_failed", restored=False)
        manifest["consumed"] = True
        try:
            _write_json_at(backup_descriptor, MANIFEST_NAME, manifest)
        except OSError:
            return _error("manifest_update_failed", restored=True)
        return {"status": "ok", "hashes": hashes, "backupId": backup_dir.name, "consumed": True}
    finally:
        if "backup_descriptor" in locals():
            os.close(backup_descriptor)
        _release_lock(lock_descriptor)
