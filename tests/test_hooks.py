from __future__ import annotations

import hashlib
import os
import unittest
from pathlib import Path
from typing import Any

from tests.helpers import IsolatedEnv


ADAPTER_ID = "orchAgent.hooks.builtin"


def registry(
    *,
    adapter_enabled: Any = True,
    adapter_type: Any = "builtin",
    hook_enabled: Any = True,
    hook_adapter: str = ADAPTER_ID,
) -> dict[str, Any]:
    """构造只包含一个 adapter 和一个 hook 的隔离 registry。"""
    return {
        "version": 1,
        "adapters": [
            {
                "id": ADAPTER_ID,
                "type": adapter_type,
                "enabled": adapter_enabled,
            }
        ],
        "hooks": [
            {
                "id": "test.session.audit",
                "adapter": hook_adapter,
                "enabled": hook_enabled,
                "events": ["session.start"],
            }
        ],
    }


class HooksTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()
        self.env.install_home()

    def tearDown(self) -> None:
        self.env.tearDown()

    def run_hooks(self, *args: str) -> tuple[int, dict[str, Any]]:
        code, payload, _, stderr = self.env.run_cli("hooks", *args)
        self.assertIsInstance(payload, dict, stderr)
        return code, payload

    def assert_contract_failure(
        self,
        data: dict[str, Any],
        *,
        reason_fragment: str | None = None,
    ) -> dict[str, Any]:
        """断言非法契约同时让 doctor 与 dry-run fail-closed。"""
        self.env.write_registry("hooks", data)

        doctor_code, doctor = self.run_hooks("doctor")
        self.assertNotEqual(0, doctor_code)
        self.assertEqual("error", doctor["status"])

        run_code, result = self.run_hooks("run", "session.start", "--dry-run")
        self.assertEqual(0, run_code)
        self.assertEqual([], result["planned"])
        self.assertEqual(1, len(result["skipped"]))
        if reason_fragment is not None:
            self.assertIn(reason_fragment, result["skipped"][0]["reason"])
        return result

    def test_default_templates_list_and_doctor_succeed(self) -> None:
        code, payload = self.run_hooks("list")

        self.assertEqual(0, code)
        self.assertTrue(payload["hooks"])
        self.assertTrue(all(row["runtime"] == "dryRunOnly" for row in payload["hooks"]))

        code, payload = self.run_hooks("doctor")
        self.assertEqual(0, code)
        self.assertEqual("ok", payload["status"])

    def test_session_start_plans_matches_and_skips_other_hooks(self) -> None:
        code, payload = self.run_hooks("run", "session.start", "--dry-run")

        self.assertEqual(0, code)
        self.assertTrue(payload["planned"])
        self.assertTrue(all(item["wouldRun"] is True for item in payload["planned"]))
        self.assertTrue(all(item["mode"] == "dry-run" for item in payload["planned"]))
        self.assertTrue(
            any(item["reason"] == "event not declared for hook" for item in payload["skipped"])
        )

    def test_unknown_event_has_no_planned_hooks(self) -> None:
        code, payload = self.run_hooks("run", "unknown.event", "--dry-run")

        self.assertEqual(0, code)
        self.assertEqual([], payload["planned"])

    def test_adapter_enabled_must_be_boolean(self) -> None:
        self.assert_contract_failure(
            registry(adapter_enabled="false"),
            reason_fragment="boolean",
        )

    def test_adapter_type_must_be_string(self) -> None:
        self.assert_contract_failure(registry(adapter_type=["builtin"]))

    def test_disabled_adapter_does_not_hide_invalid_type(self) -> None:
        self.assert_contract_failure(
            registry(adapter_enabled=False, adapter_type=["builtin"]),
            reason_fragment="string",
        )

    def test_disabled_adapter_is_warning_and_skipped(self) -> None:
        self.env.write_registry("hooks", registry(adapter_enabled=False))

        doctor_code, doctor = self.run_hooks("doctor")
        self.assertEqual(0, doctor_code)
        self.assertEqual("ok", doctor["status"])
        self.assertTrue(
            any(
                check["level"] == "warn" and check["message"] == "disabled"
                for check in doctor["hooks"]["checks"]
            )
        )

        run_code, result = self.run_hooks("run", "session.start", "--dry-run")
        self.assertEqual(0, run_code)
        self.assertEqual([], result["planned"])
        self.assertEqual("disabled", result["skipped"][0]["reason"])

    def test_disabled_hook_is_skipped(self) -> None:
        self.env.write_registry("hooks", registry(hook_enabled=False))

        code, payload = self.run_hooks("run", "session.start", "--dry-run")
        self.assertEqual(0, code)
        self.assertEqual([], payload["planned"])
        self.assertIn("disabled", payload["skipped"][0]["reason"])

    def test_hook_enabled_must_be_boolean(self) -> None:
        self.assert_contract_failure(registry(hook_enabled="false"), reason_fragment="boolean")

    def test_missing_adapter_fails_doctor_and_is_skipped(self) -> None:
        self.assert_contract_failure(
            registry(hook_adapter="missing.adapter"),
            reason_fragment="not found",
        )

    def test_unsupported_adapter_type_is_fail_closed(self) -> None:
        self.assert_contract_failure(
            registry(adapter_type="shell"),
            reason_fragment="unsupported adapter type",
        )

    def test_run_without_dry_run_is_rejected(self) -> None:
        code, payload = self.run_hooks("run", "session.start")

        self.assertNotEqual(0, code)
        self.assertEqual("error", payload["status"])
        self.assertIn("dry-run only", payload["message"])

    def test_dry_run_does_not_create_runtime_files(self) -> None:
        def root_snapshot() -> dict[Path, tuple[str, str]]:
            """记录整个隔离根目录的路径、类型与内容摘要。"""
            snapshot: dict[Path, tuple[str, str]] = {}
            for path in self.env.root.rglob("*"):
                relative = path.relative_to(self.env.root)
                if path.is_symlink():
                    snapshot[relative] = ("symlink", os.readlink(path))
                elif path.is_dir():
                    snapshot[relative] = ("directory", "")
                elif path.is_file():
                    digest = hashlib.sha256(path.read_bytes()).hexdigest()
                    snapshot[relative] = ("file", digest)
                else:
                    snapshot[relative] = ("other", "")
            return snapshot

        before = root_snapshot()
        code, _ = self.run_hooks("run", "session.start", "--dry-run")
        after = root_snapshot()

        self.assertEqual(0, code)
        # dry-run 在隔离根目录内不允许产生任何预期外变化。
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
