from __future__ import annotations

import fcntl
import json
import os
import stat
import unittest
from pathlib import Path
from unittest import mock

from orchagent.console_txn import (
    LOCK_NAME,
    apply_config_transaction,
    file_digest,
    list_config_backups,
    rollback_config_transaction,
    snapshot_files,
)
from tests.helpers import IsolatedEnv


class ConsoleTransactionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()
        (self.env.home / "extensions").mkdir()
        (self.env.home / "prompts").mkdir()

    def tearDown(self) -> None:
        self.env.tearDown()

    def write(self, relative: str, text: str) -> Path:
        path = self.env.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_applies_single_and_multiple_files_with_one_secure_backup(self) -> None:
        first = self.write("extensions/pipeline.yaml", "old-pipeline")
        second = self.write("prompts/prompts.json", "old-prompts")
        expected = {
            "extensions/pipeline.yaml": file_digest(first),
            "prompts/prompts.json": file_digest(second),
        }

        result = apply_config_transaction(
            self.env.home,
            {
                "extensions/pipeline.yaml": "new-pipeline",
                "prompts/prompts.json": "new-prompts",
            },
            expected,
        )

        self.assertEqual("ok", result["status"])
        self.assertEqual("new-pipeline", first.read_text(encoding="utf-8"))
        self.assertEqual("new-prompts", second.read_text(encoding="utf-8"))
        self.assertEqual(file_digest(first), result["hashes"]["extensions/pipeline.yaml"])
        backup = Path(result["backupDir"])
        manifest_path = backup / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        self.assertEqual("console", manifest["kind"])
        self.assertEqual(2, len(manifest["files"]))
        self.assertFalse(manifest["consumed"])
        self.assertEqual(0o700, stat.S_IMODE(backup.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(manifest_path.stat().st_mode))
        self.assertEqual(0o600, stat.S_IMODE(first.stat().st_mode))

    def test_expected_hash_mismatch_writes_nothing_and_creates_no_backup(self) -> None:
        target = self.write("extensions/pipeline.yaml", "original")

        result = apply_config_transaction(
            self.env.home,
            {"extensions/pipeline.yaml": "changed"},
            {"extensions/pipeline.yaml": "0" * 64},
        )

        self.assertEqual({"status": "error", "error": "hash_mismatch"}, result)
        self.assertEqual("original", target.read_text(encoding="utf-8"))
        self.assertFalse(Path(f"{self.env.home}.backups").exists())

    def test_rollback_restores_old_file_deletes_new_file_and_is_one_shot(self) -> None:
        old = self.write("extensions/pipeline.yaml", "before")
        expected = {"extensions/pipeline.yaml": file_digest(old), "prompts/new.md": None}
        applied = apply_config_transaction(
            self.env.home,
            {"extensions/pipeline.yaml": "after", "prompts/new.md": "created"},
            expected,
        )
        self.assertEqual("ok", applied["status"])
        new = self.env.home / "prompts/new.md"
        rollback_expected = {
            "extensions/pipeline.yaml": file_digest(old),
            "prompts/new.md": file_digest(new),
        }

        rolled_back = rollback_config_transaction(self.env.home, applied["backupId"], rollback_expected)

        self.assertEqual("ok", rolled_back["status"])
        self.assertEqual("before", old.read_text(encoding="utf-8"))
        self.assertFalse(new.exists())
        repeated = rollback_config_transaction(self.env.home, applied["backupId"])
        self.assertEqual("manifest_consumed", repeated["error"])

    def test_lists_sanitized_console_backup_summaries(self) -> None:
        marker = "backup-file-content-must-not-leak"
        target = self.write("extensions/pipeline.yaml", marker)
        applied = apply_config_transaction(
            self.env.home,
            {"extensions/pipeline.yaml": "after"},
            {"extensions/pipeline.yaml": file_digest(target)},
        )
        self.assertEqual("ok", applied["status"])

        result = list_config_backups(self.env.home)

        self.assertEqual("ok", result["status"])
        self.assertEqual(1, len(result["backups"]))
        summary = result["backups"][0]
        self.assertEqual(applied["backupId"], summary["backupId"])
        self.assertEqual(False, summary["consumed"])
        self.assertEqual("extensions/pipeline.yaml", summary["files"][0]["path"])
        serialized = json.dumps(result)
        self.assertNotIn(marker, serialized)
        self.assertNotIn(str(Path(applied["backupDir"])), serialized)

    def test_rejects_absolute_parent_escape_and_symlink_target(self) -> None:
        outside = self.env.external_dir / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        link = self.env.home / "prompts/link.md"
        os.symlink(outside, link)

        for relative in (str(outside), "../outside.txt", "prompts/link.md"):
            with self.subTest(relative=relative):
                result = apply_config_transaction(self.env.home, {relative: "changed"}, {relative: None})
                self.assertEqual("error", result["status"])
        self.assertEqual("outside", outside.read_text(encoding="utf-8"))

    def test_midway_replace_failure_restores_already_replaced_file(self) -> None:
        first = self.write("extensions/first.json", "first-old")
        second = self.write("extensions/second.json", "second-old")
        expected = {
            "extensions/first.json": file_digest(first),
            "extensions/second.json": file_digest(second),
        }
        real_rename = os.rename
        rename_calls = 0

        def fail_second(source: str | Path, destination: str | Path, **kwargs: object) -> None:
            nonlocal rename_calls
            rename_calls += 1
            if rename_calls >= 4:
                raise OSError("injected replace failure")
            real_rename(source, destination, **kwargs)

        with mock.patch("orchagent.console_txn.os.rename", side_effect=fail_second):
            result = apply_config_transaction(
                self.env.home,
                {"extensions/first.json": "first-new", "extensions/second.json": "second-new"},
                expected,
            )

        self.assertEqual("write_failed", result["error"])
        self.assertTrue(result["restored"])
        self.assertEqual("first-old", first.read_text(encoding="utf-8"))
        self.assertEqual("second-old", second.read_text(encoding="utf-8"))

    def test_post_replace_chmod_failure_restores_current_file(self) -> None:
        target = self.write("extensions/pipeline.yaml", "old")
        expected = {"extensions/pipeline.yaml": file_digest(target)}
        real_fsync = os.fsync
        failed = False
        fsync_calls = 0

        def fail_target_once(descriptor: int) -> None:
            nonlocal failed, fsync_calls
            fsync_calls += 1
            if not failed and fsync_calls >= 5:
                failed = True
                raise OSError("injected fsync failure")
            real_fsync(descriptor)

        with mock.patch("orchagent.console_txn.os.fsync", side_effect=fail_target_once):
            result = apply_config_transaction(
                self.env.home, {"extensions/pipeline.yaml": "new"}, expected
            )

        self.assertEqual("write_failed", result["error"])
        self.assertTrue(result["restored"])
        self.assertEqual("old", target.read_text(encoding="utf-8"))

    def test_post_write_snapshot_failure_restores_all_files(self) -> None:
        target = self.write("extensions/pipeline.yaml", "old")
        original_digest = file_digest(target)
        def fail_after_write(*args: object, **kwargs: object) -> tuple[bytes | None, str | None]:
            nonlocal calls
            calls += 1
            if calls >= 3:
                raise OSError("injected digest failure")
            return original_reader(*args, **kwargs)

        calls = 0
        original_reader = __import__("orchagent.console_txn", fromlist=["_read_home_file_with_digest"])._read_home_file_with_digest
        with mock.patch("orchagent.console_txn._read_home_file_with_digest", side_effect=fail_after_write):
            result = apply_config_transaction(
                self.env.home, {"extensions/pipeline.yaml": "new"}, {"extensions/pipeline.yaml": original_digest}
            )
        self.assertEqual("post_write_snapshot_failed", result["error"])
        self.assertTrue(result["restored"])
        self.assertEqual("old", target.read_text(encoding="utf-8"))

    def test_rollback_rejects_parent_escape_in_backup_path(self) -> None:
        target = self.write("extensions/pipeline.yaml", "old")
        applied = apply_config_transaction(
            self.env.home, {"extensions/pipeline.yaml": "new"}, {"extensions/pipeline.yaml": file_digest(target)}
        )
        manifest_path = Path(applied["backupDir"]) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["files"][0]["backup"] = "files/../manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        result = rollback_config_transaction(self.env.home, applied["backupId"])
        self.assertEqual("manifest_invalid", result["error"])

    def test_rollback_rejects_tampered_backup_content(self) -> None:
        target = self.write("extensions/pipeline.yaml", "old")
        applied = apply_config_transaction(
            self.env.home, {"extensions/pipeline.yaml": "new"}, {"extensions/pipeline.yaml": file_digest(target)}
        )
        backup_file = Path(applied["backupDir"]) / "files/extensions/pipeline.yaml"
        backup_file.write_text("tampered", encoding="utf-8")

        result = rollback_config_transaction(self.env.home, applied["backupId"])

        self.assertEqual("manifest_invalid", result["error"])
        self.assertEqual("new", target.read_text(encoding="utf-8"))

    def test_failed_rollback_does_not_create_backup_root(self) -> None:
        backup_root = Path(f"{self.env.home}.backups")
        result = rollback_config_transaction(self.env.home, "missing-backup")
        self.assertEqual("rollback_unavailable", result["error"])
        self.assertFalse(backup_root.exists())

    def test_rollback_rejects_boolean_manifest_version(self) -> None:
        target = self.write("extensions/pipeline.yaml", "old")
        applied = apply_config_transaction(
            self.env.home, {"extensions/pipeline.yaml": "new"}, {"extensions/pipeline.yaml": file_digest(target)}
        )
        manifest_path = Path(applied["backupDir"]) / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["version"] = True
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

        result = rollback_config_transaction(self.env.home, applied["backupId"])

        self.assertEqual("manifest_invalid", result["error"])
        self.assertEqual("new", target.read_text(encoding="utf-8"))
        self.assertEqual("new", target.read_text(encoding="utf-8"))

    def test_lock_is_mutually_exclusive(self) -> None:
        target = self.write("extensions/pipeline.yaml", "old")
        lock_path = self.env.home / LOCK_NAME
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = apply_config_transaction(
                self.env.home,
                {"extensions/pipeline.yaml": "new"},
                {"extensions/pipeline.yaml": file_digest(target)},
            )
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)
        self.assertEqual({"status": "error", "error": "lock_busy"}, result)
        self.assertEqual("old", target.read_text(encoding="utf-8"))

    def test_lock_symlink_is_rejected_without_touching_external_file(self) -> None:
        target = self.write("extensions/pipeline.yaml", "old")
        external = self.env.external_dir / "external-lock"
        external.write_text("outside", encoding="utf-8")
        external.chmod(0o644)
        os.symlink(external, self.env.home / LOCK_NAME)

        result = apply_config_transaction(
            self.env.home,
            {"extensions/pipeline.yaml": "new"},
            {"extensions/pipeline.yaml": file_digest(target)},
        )

        self.assertEqual("error", result["status"])
        self.assertEqual("outside", external.read_text(encoding="utf-8"))
        self.assertEqual(0o644, stat.S_IMODE(external.stat().st_mode))
        self.assertEqual("old", target.read_text(encoding="utf-8"))

    def test_backup_root_symlink_is_rejected_without_writing_external_directory(self) -> None:
        target = self.write("extensions/pipeline.yaml", "old")
        backup_root = Path(f"{self.env.home}.backups")
        os.symlink(self.env.external_dir, backup_root)

        result = apply_config_transaction(
            self.env.home,
            {"extensions/pipeline.yaml": "new"},
            {"extensions/pipeline.yaml": file_digest(target)},
        )

        self.assertEqual({"status": "error", "error": "backup_failed"}, result)
        self.assertEqual([], list(self.env.external_dir.iterdir()))
        self.assertEqual("old", target.read_text(encoding="utf-8"))

    def test_list_backups_rejects_symlink_backup_root(self) -> None:
        os.symlink(self.env.external_dir, Path(f"{self.env.home}.backups"))

        result = list_config_backups(self.env.home)

        self.assertEqual({"status": "error", "error": "backup_root_invalid"}, result)

    def test_list_backups_does_not_create_missing_backup_root(self) -> None:
        backup_root = Path(f"{self.env.home}.backups")
        self.assertFalse(backup_root.exists())

        result = list_config_backups(self.env.home)

        self.assertEqual({"status": "ok", "backups": []}, result)
        self.assertFalse(backup_root.exists())

    def test_prepare_backup_closes_descriptor_when_backup_write_fails(self) -> None:
        target = self.write("extensions/pipeline.yaml", "old")
        expected = {"extensions/pipeline.yaml": file_digest(target)}
        def fail_write(*args: object, **kwargs: object) -> None:
            raise OSError("injected backup failure")

        before = len(os.listdir("/dev/fd"))
        with mock.patch("orchagent.console_txn._write_file_at", side_effect=fail_write):
            result = apply_config_transaction(self.env.home, {"extensions/pipeline.yaml": "new"}, expected)
        after = len(os.listdir("/dev/fd"))

        self.assertEqual({"status": "error", "error": "backup_failed"}, result)
        self.assertLessEqual(after, before + 1)

    def test_rollback_lock_busy_closes_backup_descriptor(self) -> None:
        target = self.write("extensions/pipeline.yaml", "old")
        applied = apply_config_transaction(
            self.env.home,
            {"extensions/pipeline.yaml": "new"},
            {"extensions/pipeline.yaml": file_digest(target)},
        )
        lock_path = self.env.home / LOCK_NAME
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            before = len(os.listdir("/dev/fd"))
            result = rollback_config_transaction(self.env.home, applied["backupId"])
            after = len(os.listdir("/dev/fd"))
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

        self.assertEqual({"status": "error", "error": "lock_busy"}, result)
        self.assertLessEqual(after, before + 1)

    def test_rollback_rejects_symlink_backup_root_with_external_manifest(self) -> None:
        target = self.write("extensions/pipeline.yaml", "old")
        external_backup = self.env.external_dir / "console-fake"
        (external_backup / "files/extensions").mkdir(parents=True)
        (external_backup / "files/extensions/pipeline.yaml").write_text("external", encoding="utf-8")
        (external_backup / "manifest.json").write_text(json.dumps({
            "version": 1,
            "kind": "console",
            "createdAtNs": 1,
            "sequence": 0,
            "consumed": False,
            "files": [{
                "path": "extensions/pipeline.yaml",
                "type": "file",
                "existed": True,
                "hash": file_digest(target),
                "backup": "files/extensions/pipeline.yaml",
            }],
        }), encoding="utf-8")
        os.symlink(self.env.external_dir, Path(f"{self.env.home}.backups"))

        result = rollback_config_transaction(self.env.home, "console-fake")

        self.assertEqual("backup_root_invalid", result["error"])
        self.assertEqual("old", target.read_text(encoding="utf-8"))

    def test_watch_hash_mismatch_blocks_write_targets(self) -> None:
        target = self.write("extensions/pipeline.yaml", "old")
        watched = self.write("prompts/prompts.json", "old-prompts")
        expected = {"extensions/pipeline.yaml": file_digest(target)}
        watch = {
            "extensions/pipeline.yaml": expected["extensions/pipeline.yaml"],
            "prompts/prompts.json": file_digest(watched),
        }
        watched.write_text("changed-outside", encoding="utf-8")

        result = apply_config_transaction(
            self.env.home,
            {"extensions/pipeline.yaml": "new"},
            expected,
            watch_hashes=watch,
        )

        self.assertEqual({"status": "error", "error": "hash_mismatch"}, result)
        self.assertEqual("old", target.read_text(encoding="utf-8"))

    def test_snapshot_files_reports_existing_and_missing_hashes(self) -> None:
        target = self.write("extensions/pipeline.yaml", "value")
        result = snapshot_files(self.env.home, ["extensions/pipeline.yaml", "prompts/missing.md"])
        self.assertEqual("ok", result["status"])
        self.assertEqual(file_digest(target), result["hashes"]["extensions/pipeline.yaml"])
        self.assertIsNone(result["hashes"]["prompts/missing.md"])


if __name__ == "__main__":
    unittest.main()
