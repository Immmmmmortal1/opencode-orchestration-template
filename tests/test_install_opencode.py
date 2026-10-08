from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from orchagent.backup import create_backup_dir
from orchagent.install import (
    RUNTIME_DIRS,
    backup_root,
    copy_default_configs,
    latest_backup_dir,
    rollback_latest,
)
from orchagent.opencode import backup_files

from tests.helpers import IsolatedEnv


class InstallTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()

    def tearDown(self) -> None:
        self.env.tearDown()

    def test_install_is_idempotent_and_force_controls_overwrite(self) -> None:
        copied = self.env.install_home()

        self.assertEqual(7, len(copied))
        for dirname in RUNTIME_DIRS:
            self.assertTrue((self.env.home / dirname).is_dir())
        manifest = self.env.home / "install-manifest.json"
        self.assertTrue(manifest.is_file())

        main_config = self.env.home / "orchAgent.yaml"
        sentinel = b'{"custom": "keep-me"}\n'
        main_config.write_bytes(sentinel)

        self.assertEqual([], copy_default_configs(self.env.home, overwrite=False))
        self.assertEqual(sentinel, main_config.read_bytes())

        overwritten = copy_default_configs(self.env.home, overwrite=True)
        self.assertEqual(7, len(overwritten))
        self.assertNotEqual(sentinel, main_config.read_bytes())

    def test_backup_root_is_sibling_of_home(self) -> None:
        self.env.install_home()

        backups = backup_root(self.env.home)
        self.assertEqual(self.env.home.parent / f"{self.env.home.name}.backups", backups)
        self.assertTrue(backups.is_dir())
        self.assertNotEqual(self.env.home, backups)
        self.assertNotIn(self.env.home, backups.parents)

    def test_install_rollback_consumes_manifest_and_is_one_shot(self) -> None:
        self.env.install_home()
        backup = next(backup_root(self.env.home).glob("install-*"))

        code, payload, _, _ = self.env.run_cli("install", "rollback")

        self.assertEqual(0, code)
        self.assertEqual("ok", payload["status"])
        self.assertFalse((self.env.home / "install-manifest.json").exists())
        self.assertFalse((backup / "rollback-manifest.json").exists())
        self.assertTrue((backup / "rollback-manifest.consumed.json").is_file())

        code, payload, _, _ = self.env.run_cli("install", "rollback")
        self.assertEqual(1, code)
        self.assertEqual("error", payload["status"])
        self.assertEqual("no backup found", payload["message"])

    def test_same_second_same_process_backup_directories_are_unique(self) -> None:
        root = backup_root(self.env.home)
        with (
            patch("orchagent.backup.time.strftime", return_value="20260929-120000"),
            patch("orchagent.backup.time.time_ns", return_value=9_000_000_000_000_000_000),
            patch("orchagent.backup.os.getpid", return_value=456),
        ):
            created = [create_backup_dir(root, "install") for _ in range(3)]

        self.assertEqual(3, len({path.name for path, _, _ in created}))
        self.assertEqual([0, 1, 2], [sequence for _, _, sequence in created])

    def test_same_second_install_backups_select_and_rollback_the_last_one(self) -> None:
        self.env.install_home()
        config = self.env.home / "orchAgent.yaml"
        with (
            patch("orchagent.backup.time.strftime", return_value="20260929-120001"),
            patch("orchagent.backup.time.time_ns", return_value=9_000_000_000_000_000_001),
            patch("orchagent.backup.os.getpid", return_value=457),
        ):
            config.write_text("first\n", encoding="utf-8")
            copy_default_configs(self.env.home, overwrite=True)
            config.write_text("second\n", encoding="utf-8")
            copy_default_configs(self.env.home, overwrite=True)
            config.write_text("third\n", encoding="utf-8")
            copy_default_configs(self.env.home, overwrite=True)

        matching = sorted(backup_root(self.env.home).glob("install-20260929-120001-457*"))
        self.assertEqual(3, len(matching))
        latest = latest_backup_dir(self.env.home, kind="install")
        self.assertEqual(matching[-1], latest)
        manifest = json.loads((latest / "rollback-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(9_000_000_000_000_000_001, manifest["createdAtNs"])
        self.assertEqual(2, manifest["sequence"])

        rollback_latest(self.env.home, kind="install")
        self.assertEqual("third\n", config.read_text(encoding="utf-8"))

    def test_latest_backup_accepts_legacy_manifest_without_sort_metadata(self) -> None:
        legacy = backup_root(self.env.home) / "install-legacy"
        legacy.mkdir(parents=True)
        (legacy / "rollback-manifest.json").write_text(
            json.dumps({"version": 1, "kind": "install", "files": []}),
            encoding="utf-8",
        )

        self.assertEqual(legacy, latest_backup_dir(self.env.home, kind="install"))

    def test_latest_backup_skips_invalid_sort_metadata(self) -> None:
        root = backup_root(self.env.home)
        valid = root / "install-valid"
        broken = root / "install-broken"
        valid.mkdir(parents=True)
        broken.mkdir()
        (valid / "rollback-manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "kind": "install",
                    "createdAt": 1,
                    "createdAtNs": 1,
                    "sequence": 0,
                    "files": [],
                }
            ),
            encoding="utf-8",
        )
        (broken / "rollback-manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "kind": "install",
                    "createdAt": 2,
                    "createdAtNs": "not-a-number",
                    "sequence": 0,
                    "files": [],
                }
            ),
            encoding="utf-8",
        )

        self.assertEqual(valid, latest_backup_dir(self.env.home, kind="install"))

    def test_latest_backup_returns_none_when_backup_directory_disappears_during_iteration(self) -> None:
        backup_root(self.env.home).mkdir(parents=True)

        with patch.object(Path, "iterdir", autospec=True, side_effect=FileNotFoundError):
            self.assertIsNone(latest_backup_dir(self.env.home, kind="install"))

    def test_latest_backup_fails_closed_when_backup_directory_iteration_has_io_error(self) -> None:
        backup_root(self.env.home).mkdir(parents=True)

        with patch.object(
            Path,
            "iterdir",
            autospec=True,
            side_effect=OSError("simulated backup directory iteration failure"),
        ):
            with self.assertRaisesRegex(OSError, "simulated backup directory iteration failure"):
                latest_backup_dir(self.env.home, kind="install")

    def test_latest_backup_skips_legacy_manifest_that_disappears_before_stat(self) -> None:
        root = backup_root(self.env.home)
        valid = root / "install-valid"
        disappeared = root / "install-disappeared"
        valid.mkdir(parents=True)
        disappeared.mkdir()
        (valid / "rollback-manifest.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "kind": "install",
                    "createdAt": 1,
                    "createdAtNs": 1,
                    "sequence": 0,
                    "files": [],
                }
            ),
            encoding="utf-8",
        )
        disappeared_manifest = disappeared / "rollback-manifest.json"
        disappeared_manifest.write_text(
            json.dumps({"version": 1, "kind": "install", "files": []}),
            encoding="utf-8",
        )
        original_stat = Path.stat

        def stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
            if path == disappeared_manifest:
                raise FileNotFoundError("simulated disappeared legacy manifest")
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", autospec=True, side_effect=stat):
            self.assertEqual(valid, latest_backup_dir(self.env.home, kind="install"))

    def test_install_rollback_does_not_fall_back_when_newest_directory_stat_fails(self) -> None:
        root = backup_root(self.env.home)
        older = root / "install-older"
        newest = root / "install-newest"
        older.mkdir(parents=True)
        newest.mkdir()
        for backup, created_at_ns in ((older, 1), (newest, 2)):
            (backup / "rollback-manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "kind": "install",
                        "createdAt": 0,
                        "createdAtNs": created_at_ns,
                        "sequence": 0,
                        "files": [],
                    }
                ),
                encoding="utf-8",
            )
        original_stat = Path.stat

        def stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
            if path == newest:
                raise OSError("simulated newest directory stat failure")
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", autospec=True, side_effect=stat):
            with self.assertRaisesRegex(OSError, "simulated newest directory stat failure"):
                rollback_latest(self.env.home, kind="install")

    def test_install_rollback_fails_closed_when_manifest_read_has_io_error(self) -> None:
        self.env.install_home()
        manifest_path = next(backup_root(self.env.home).glob("install-*")) / "rollback-manifest.json"
        original_read_text = Path.read_text

        def read_text(path: Path, *args: object, **kwargs: object) -> str:
            if path == manifest_path:
                raise OSError("simulated manifest read failure")
            return original_read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", autospec=True, side_effect=read_text):
            with self.assertRaisesRegex(OSError, "simulated manifest read failure"):
                rollback_latest(self.env.home, kind="install")


class OpenCodeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()
        self.env.install_home()

    def tearDown(self) -> None:
        self.env.tearDown()

    def test_link_doctor_unlink_and_rollback_form_a_closed_loop(self) -> None:
        code, payload, _, _ = self.env.run_cli("opencode", "link")
        self.assertEqual(0, code)
        self.assertEqual(str(self.env.opencode_config), payload["opencode"]["target"])

        code, payload, _, _ = self.env.run_cli("opencode", "doctor")
        self.assertEqual(0, code)
        self.assertTrue(payload["opencode"]["linked"])

        code, payload, _, _ = self.env.run_cli("opencode", "unlink")
        self.assertEqual(0, code)
        self.assertEqual([str(self.env.opencode_config)], payload["opencode"]["changed"])

        code, payload, _, _ = self.env.run_cli("opencode", "doctor")
        self.assertEqual(1, code)
        self.assertFalse(payload["opencode"]["linked"])

        code, payload, _, _ = self.env.run_cli("opencode", "rollback")
        self.assertEqual(0, code)
        self.assertIn(str(self.env.opencode_config), payload["restored"])

        code, payload, _, _ = self.env.run_cli("opencode", "doctor")
        self.assertEqual(0, code)
        self.assertTrue(payload["opencode"]["linked"])

    def test_opencode_config_env_strictly_isolates_default_configs(self) -> None:
        default_config = self.env.fake_home / ".config" / "opencode" / "opencode.json"
        local_config = self.env.fake_home / ".config" / "opencode.local.json"
        default_config.parent.mkdir(parents=True)
        default_config.write_bytes(b'{"default": true}\n')
        local_config.write_bytes(b'{"local": true}\n')
        before_default = default_config.read_bytes()
        before_local = local_config.read_bytes()
        before_target = self.env.opencode_config.read_bytes()

        code, _, _, _ = self.env.run_cli("opencode", "link")

        self.assertEqual(0, code)
        self.assertEqual(before_default, default_config.read_bytes())
        self.assertEqual(before_local, local_config.read_bytes())
        self.assertNotEqual(before_target, self.env.opencode_config.read_bytes())

        # D6 契约要求“只操作 OPENCODE_CONFIG 指定路径，不扫描默认真实配置”，
        # 因此 doctor 暴露的候选路径集合必须只含隔离路径。
        code, payload, out, err = self.env.run_cli("opencode", "doctor")
        self.assertEqual(0, code, (out, err))
        self.assertIsNotNone(payload)
        self.assertEqual(
            [str(self.env.opencode_config)],
            payload["opencode"]["candidatePaths"],
        )

    def test_symlink_config_writes_and_restores_real_target(self) -> None:
        self.env.opencode_config.unlink()
        target = self.env.external_dir / "real-opencode.json"
        original = b'{"theme": "dark"}\n'
        target.write_bytes(original)
        self.env.opencode_config.symlink_to(target)
        link_target = os.readlink(self.env.opencode_config)

        code, _, _, _ = self.env.run_cli("opencode", "link")
        self.assertEqual(0, code)
        self.assertTrue(self.env.opencode_config.is_symlink())
        self.assertEqual(link_target, os.readlink(self.env.opencode_config))
        self.assertIn("orchAgent", target.read_text(encoding="utf-8"))

        code, _, _, _ = self.env.run_cli("opencode", "rollback")
        self.assertEqual(0, code)
        self.assertTrue(self.env.opencode_config.is_symlink())
        self.assertEqual(link_target, os.readlink(self.env.opencode_config))
        self.assertEqual(original, target.read_bytes())

    def test_dangling_symlink_target_is_removed_by_rollback(self) -> None:
        self.env.opencode_config.unlink()
        target = self.env.external_dir / "missing-opencode.json"
        self.env.opencode_config.symlink_to(target)
        link_target = os.readlink(self.env.opencode_config)

        code, _, _, _ = self.env.run_cli("opencode", "link")
        self.assertEqual(0, code)
        self.assertTrue(target.is_file())
        self.assertTrue(self.env.opencode_config.is_symlink())

        code, _, _, _ = self.env.run_cli("opencode", "rollback")
        self.assertEqual(0, code)
        self.assertFalse(target.exists())
        self.assertTrue(self.env.opencode_config.is_symlink())
        self.assertEqual(link_target, os.readlink(self.env.opencode_config))

    def test_link_refuses_unmanaged_fields_with_same_name(self) -> None:
        cases = {
            "top-level": {"orchAgent": {"enabled": False}},
            "agent": {"agent": {"orchAgent": {"mode": "primary"}}},
        }
        for name, config in cases.items():
            with self.subTest(name=name):
                original = (json.dumps(config, ensure_ascii=False) + "\n").encode()
                self.env.opencode_config.write_bytes(original)

                code, _, _, stderr = self.env.run_cli("opencode", "link")

                self.assertNotEqual(0, code)
                self.assertIn("without orchAgent-managed marker" if name == "agent" else "unmanaged orchAgent key", stderr)
                self.assertEqual(original, self.env.opencode_config.read_bytes())

    def test_same_second_opencode_backups_select_and_rollback_the_last_one(self) -> None:
        target = self.env.opencode_config
        with (
            patch("orchagent.backup.time.strftime", return_value="20260929-120002"),
            patch("orchagent.backup.time.time_ns", return_value=9_000_000_000_000_000_002),
            patch("orchagent.backup.os.getpid", return_value=458),
        ):
            target.write_text('{"step": 1}\n', encoding="utf-8")
            backup_files([target], home=self.env.home)
            target.write_text('{"step": 2}\n', encoding="utf-8")
            backup_files([target], home=self.env.home)
            target.write_text('{"step": 3}\n', encoding="utf-8")
            backup_files([target], home=self.env.home)

        matching = sorted(backup_root(self.env.home).glob("opencode-20260929-120002-458*"))
        self.assertEqual(3, len(matching))
        latest = latest_backup_dir(self.env.home, kind="opencode")
        self.assertEqual(matching[-1], latest)
        manifest = json.loads((latest / "rollback-manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(9_000_000_000_000_000_002, manifest["createdAtNs"])
        self.assertEqual(2, manifest["sequence"])

        rollback_latest(self.env.home, kind="opencode")
        self.assertEqual('{"step": 3}\n', target.read_text(encoding="utf-8"))

    def test_opencode_rollback_fails_closed_when_legacy_manifest_stat_has_io_error(self) -> None:
        root = backup_root(self.env.home)
        backup = root / "opencode-legacy"
        backup.mkdir(parents=True)
        manifest_path = backup / "rollback-manifest.json"
        manifest_path.write_text(
            json.dumps({"version": 1, "kind": "opencode", "files": []}),
            encoding="utf-8",
        )
        original_stat = Path.stat

        def stat(path: Path, *args: object, **kwargs: object) -> os.stat_result:
            if path == manifest_path:
                raise OSError("simulated manifest stat failure")
            return original_stat(path, *args, **kwargs)

        with patch.object(Path, "stat", autospec=True, side_effect=stat):
            with self.assertRaisesRegex(OSError, "simulated manifest stat failure"):
                rollback_latest(self.env.home, kind="opencode")

    def test_opencode_rollback_does_not_fall_back_when_newest_manifest_read_fails(self) -> None:
        root = backup_root(self.env.home)
        older = root / "opencode-older"
        newest = root / "opencode-newest"
        older.mkdir(parents=True)
        newest.mkdir()
        for backup, created_at_ns in ((older, 1), (newest, 2)):
            (backup / "rollback-manifest.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "kind": "opencode",
                        "createdAt": 0,
                        "createdAtNs": created_at_ns,
                        "sequence": 0,
                        "files": [],
                    }
                ),
                encoding="utf-8",
            )
        newest_manifest = newest / "rollback-manifest.json"
        original_read_text = Path.read_text

        def read_text(path: Path, *args: object, **kwargs: object) -> str:
            if path == newest_manifest:
                raise OSError("simulated newest manifest read failure")
            return original_read_text(path, *args, **kwargs)

        with patch.object(Path, "read_text", autospec=True, side_effect=read_text):
            with self.assertRaisesRegex(OSError, "simulated newest manifest read failure"):
                rollback_latest(self.env.home, kind="opencode")


if __name__ == "__main__":
    unittest.main()
