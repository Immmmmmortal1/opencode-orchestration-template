from __future__ import annotations

import json
import os
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path

from .backup import create_backup_dir
from .paths import DEFAULT_HOME, PROJECT_ROOT, template_path


RUNTIME_DIRS = [
    "extensions",
    "integrations",
    "state",
    "sessions",
    "locks",
    "leases",
    "logs",
    "backups",
]


@dataclass(frozen=True)
class BackupCandidate:
    """可参与“最新备份”排序的合法候选。"""

    created_at: int
    created_at_ns: int
    sequence: int
    directory_name: str
    path: Path

    @property
    def sort_key(self) -> tuple[int, int, int, str]:
        return (self.created_at, self.created_at_ns, self.sequence, self.directory_name)


def backup_root(home: Path = DEFAULT_HOME) -> Path:
    return home.parent / f"{home.name}.backups"


def ensure_home(home: Path = DEFAULT_HOME) -> list[dict[str, object]]:
    created: list[dict[str, object]] = []
    if not home.exists():
        created.append({"type": "dir", "target": str(home), "existed": False})
    home.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(home, 0o700)
    for dirname in RUNTIME_DIRS:
        path = home / dirname
        if not path.exists():
            created.append({"type": "dir", "target": str(path), "existed": False})
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(path, 0o700)
    return created


def render_template(src: Path, home: Path) -> str:
    return src.read_text(encoding="utf-8").replace("~/.orchAgent", str(home))


def write_rendered_template(src: Path, dst: Path, home: Path) -> None:
    dst.write_text(render_template(src, home), encoding="utf-8")
    os.chmod(dst, 0o600)


def copy_default_configs(home: Path = DEFAULT_HOME, overwrite: bool = False, link_bin: str | None = None) -> list[str]:
    if link_bin:
        link_path = Path(link_bin).expanduser()
        source = PROJECT_ROOT / "bin" / "orchagent"
        if link_path.exists() or link_path.is_symlink():
            current = os.path.realpath(link_path)
            expected = os.path.realpath(source)
            if current != expected:
                raise RuntimeError(f"refuse to overwrite existing command: {link_path} -> {current}")
    manifest_entries = ensure_home(home)
    copied: list[str] = []
    mappings = [
        (template_path("orchAgent.yaml"), home / "orchAgent.yaml"),
        (template_path("extensions", "hooks.yaml"), home / "extensions" / "hooks.yaml"),
        (template_path("extensions", "skills.yaml"), home / "extensions" / "skills.yaml"),
        (template_path("extensions", "mcp.yaml"), home / "extensions" / "mcp.yaml"),
        (template_path("extensions", "knowledge.yaml"), home / "extensions" / "knowledge.yaml"),
        (template_path("extensions", "pipeline.yaml"), home / "extensions" / "pipeline.yaml"),
        (template_path("integrations", "opencode.patch.json"), home / "integrations" / "opencode.patch.json"),
    ]
    root = backup_root(home)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    backup_dir, created_at_ns, sequence = create_backup_dir(root, "install")
    for src, dst in mappings:
        if dst.exists():
            if not overwrite:
                continue
            backup_path = backup_dir / dst.name
            shutil.copyfile(dst, backup_path)
            os.chmod(backup_path, 0o600)
            manifest_entries.append({"type": "file", "target": str(dst), "backup": str(backup_path), "existed": True})
        else:
            manifest_entries.append({"type": "file", "target": str(dst), "backup": "", "existed": False})
        write_rendered_template(src, dst, home)
        copied.append(str(dst))

    install_manifest_path = home / "install-manifest.json"
    if install_manifest_path.exists():
        backup_path = backup_dir / "install-manifest.json"
        shutil.copyfile(install_manifest_path, backup_path)
        os.chmod(backup_path, 0o600)
        manifest_entries.append({"type": "file", "target": str(install_manifest_path), "backup": str(backup_path), "existed": True})
    else:
        manifest_entries.append({"type": "file", "target": str(install_manifest_path), "backup": "", "existed": False})

    if link_bin:
        link_path = Path(link_bin).expanduser()
        source = PROJECT_ROOT / "bin" / "orchagent"
        if link_path.exists() or link_path.is_symlink():
            current = os.path.realpath(link_path)
            entry: dict[str, object] = {"type": "symlink", "target": str(link_path), "backup": "", "existed": True, "previous": current}
            if link_path.is_symlink():
                entry["linkTarget"] = os.readlink(link_path)
            manifest_entries.append(entry)
        else:
            manifest_entries.append({"type": "symlink", "target": str(link_path), "backup": "", "existed": False})
        link_path.parent.mkdir(parents=True, exist_ok=True)
        if link_path.exists() or link_path.is_symlink():
            link_path.unlink()
        link_path.symlink_to(source)

    manifest = {
        "version": 1,
        "installedAt": int(time.time()),
        "projectRoot": str(PROJECT_ROOT),
        "home": str(home),
        "copied": copied,
    }
    install_manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(install_manifest_path, 0o600)

    rollback_manifest = {
        "version": 1,
        "kind": "install",
        "createdAt": created_at_ns // 1_000_000_000,
        "createdAtNs": created_at_ns,
        "sequence": sequence,
        "files": manifest_entries,
    }
    rollback_path = backup_dir / "rollback-manifest.json"
    rollback_path.write_text(json.dumps(rollback_manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(rollback_path, 0o600)
    return copied


def latest_backup_dir(home: Path = DEFAULT_HOME, kind: str | None = None) -> Path | None:
    backups = backup_root(home)
    try:
        backups.stat()
    except FileNotFoundError:
        return None
    try:
        backup_paths = list(backups.iterdir())
    except FileNotFoundError:
        return None
    candidates: list[BackupCandidate] = []
    for path in backup_paths:
        try:
            path_stat = path.stat()
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(path_stat.st_mode):
            continue
        manifest_path = path / "rollback-manifest.json"
        try:
            manifest_text = manifest_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            continue
        try:
            manifest = json.loads(manifest_text)
            if not isinstance(manifest, dict):
                continue
            if kind is not None and manifest.get("kind") != kind:
                continue
            created_at_ns_value = (
                manifest["createdAtNs"]
                if "createdAtNs" in manifest
                else manifest_path.stat().st_mtime_ns
            )
            created_at_ns = int(created_at_ns_value)
            sequence = int(manifest.get("sequence", 0))
            created_at = int(manifest.get("createdAt", 0))
        except FileNotFoundError:
            continue
        except (json.JSONDecodeError, TypeError, ValueError):
            # 单个损坏备份不能阻断其它合法候选的回滚。
            continue
        candidates.append(
            BackupCandidate(
                created_at=created_at,
                created_at_ns=created_at_ns,
                sequence=sequence,
                directory_name=path.name,
                path=path,
            )
        )
    if not candidates:
        return None
    return max(candidates, key=lambda candidate: candidate.sort_key).path


def rollback_latest(home: Path = DEFAULT_HOME, kind: str | None = "install") -> list[str]:
    backup = latest_backup_dir(home, kind=kind)
    if backup is None:
        raise RuntimeError("no backup found")
    manifest_path = backup / "rollback-manifest.json"
    if not manifest_path.exists():
        raise RuntimeError(f"missing rollback manifest: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    restored: list[str] = []
    for item in reversed(manifest.get("files", [])):
        item_type = item.get("type", "file")
        src = Path(str(item.get("backup", ""))) if item.get("backup") else None
        dst = Path(item["target"])
        existed = bool(item.get("existed", True))
        if item_type == "dir":
            if not existed and dst.exists():
                try:
                    dst.rmdir()
                    restored.append(str(dst))
                except OSError:
                    pass
        elif item_type == "symlink":
            if not existed and (dst.exists() or dst.is_symlink()):
                dst.unlink()
                restored.append(str(dst))
            elif existed and item.get("linkTarget"):
                link_target = str(item["linkTarget"])
                if dst.exists() or dst.is_symlink():
                    if not dst.is_symlink() or os.readlink(dst) != link_target:
                        dst.unlink()
                        dst.symlink_to(link_target)
                        restored.append(str(dst))
                else:
                    dst.symlink_to(link_target)
                    restored.append(str(dst))
        elif item_type == "symlinkFile":
            link_target = str(item.get("linkTarget", ""))
            resolved = Path(str(item.get("resolvedTarget", ""))) if item.get("resolvedTarget") else None
            resolved_existed = bool(item.get("resolvedExisted", True))
            if not dst.is_symlink() or os.readlink(dst) != link_target:
                if dst.exists() or dst.is_symlink():
                    dst.unlink()
                dst.symlink_to(link_target)
                restored.append(str(dst))
            if not resolved_existed and resolved is not None and resolved.exists():
                resolved.unlink()
                restored.append(str(resolved))
            elif src is not None and resolved is not None and src.exists():
                resolved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, resolved)
                os.chmod(resolved, 0o600)
                restored.append(str(resolved))
        elif not existed and dst.exists():
            dst.unlink()
            restored.append(str(dst))
        elif src is not None and src.exists():
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            os.chmod(dst, 0o600)
            restored.append(str(dst))
    consumed_path = backup / "rollback-manifest.consumed.json"
    os.replace(manifest_path, consumed_path)
    return restored
