from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from typing import Any
from unittest.mock import patch

from tests.helpers import IsolatedEnv, SENSITIVE_ENV_MARKERS


class CliSmokeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()

    def tearDown(self) -> None:
        self.env.tearDown()

    def assert_ok(self, *args: str) -> dict[str, Any]:
        """运行 JSON CLI 命令并断言成功。"""
        code, payload, _, stderr = self.env.run_cli(*args)
        self.assertEqual(0, code, f"{' '.join(args)}: {stderr}")
        self.assertIsInstance(payload, dict, f"{' '.join(args)} 未返回 JSON")
        self.assertEqual("ok", payload["status"])
        return payload

    def assert_success_json(self, *args: str) -> dict[str, Any]:
        """断言命令成功并返回 JSON，不要求 payload 带 status 字段。"""
        code, payload, _, stderr = self.env.run_cli(*args)
        self.assertEqual(0, code, f"{' '.join(args)}: {stderr}")
        self.assertIsInstance(payload, dict, f"{' '.join(args)} 未返回 JSON")
        return payload

    def test_full_cli_smoke_flow(self) -> None:
        self.assert_ok("install", "--force")
        self.assert_ok("config", "validate")
        self.assert_ok("doctor")

        extensions = self.assert_ok("extensions", "list")
        self.assertEqual(
            {"hooks", "skills", "mcp", "knowledge", "pipeline"},
            {row["type"] for row in extensions["extensions"]},
        )

        self.assert_ok("hooks", "list")
        self.assert_ok("hooks", "doctor")
        self.assert_ok("hooks", "run", "session.start", "--dry-run")

        code, payload, _, stderr = self.env.run_cli("hooks", "run", "session.start")
        self.assertNotEqual(0, code)
        self.assertIsInstance(payload, dict, stderr)
        self.assertEqual("error", payload["status"])

        self.assert_ok("knowledge", "list")
        self.assert_ok("knowledge", "search", "关键词")

        self.assert_ok("mcp", "list")
        self.assert_success_json("mcp", "doctor")

        # 隔离环境使用相对 root，避免默认的用户目录写法逃逸 ORCHAGENT_HOME。
        self.env.write_registry(
            "skills",
            {
                "version": 1,
                "adapters": [
                    {
                        "id": "orchAgent.skills.filesystem",
                        "type": "filesystem",
                        "enabled": True,
                        "root": "skills",
                    }
                ],
                "skills": [],
            },
        )
        self.assert_ok("skills", "list")
        self.assert_success_json("skills", "doctor")
        self.assert_ok("pipeline", "list")
        self.assert_success_json("pipeline", "doctor")

        self.assert_ok("opencode", "link")
        self.assert_ok("opencode", "doctor")
        self.assert_ok("opencode", "unlink")
        self.assert_ok("opencode", "rollback")
        self.assert_ok("install", "rollback")

        # argparse 在解析未知子命令时按约定写 stderr，并以状态码 2 退出。
        code, payload, _, stderr = self.env.run_cli("unknown-command")
        self.assertEqual(2, code)
        self.assertIsNone(payload)
        self.assertIn("invalid choice", stderr)

    def test_cli_subprocess_environment_does_not_inherit_credentials(self) -> None:
        sensitive = {
            "REAL_API_KEY": "must-not-leak",
            "ACCESS_TOKEN": "must-not-leak",
            "CLIENT_SECRET": "must-not-leak",
            "DB_PASSWORD": "must-not-leak",
            "HTTPS_PROXY": "must-not-leak",
        }
        with patch.dict(os.environ, sensitive, clear=False):
            child = subprocess.run(
                [sys.executable, "-c", "import json, os; print(json.dumps(dict(os.environ)))"],
                env=self.env.subprocess_env(),
                capture_output=True,
                text=True,
                check=True,
            )

        child_env = json.loads(child.stdout)
        for name in sensitive:
            self.assertNotIn(name, child_env)
        self.assertFalse(
            any(
                marker in name.upper()
                for name in child_env
                for marker in SENSITIVE_ENV_MARKERS
            )
        )
        self.assertEqual(str(self.env.home), child_env["ORCHAGENT_HOME"])
        self.assertEqual(str(self.env.opencode_config), child_env["OPENCODE_CONFIG"])
        self.assertEqual(str(self.env.fake_home), child_env["HOME"])

    def test_cli_subprocess_environment_does_not_load_host_sitecustomize(self) -> None:
        injection_dir = self.env.external_dir / "python-injection"
        injection_dir.mkdir()
        marker = self.env.external_dir / "sitecustomize-loaded"
        (injection_dir / "sitecustomize.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('loaded')\n",
            encoding="utf-8",
        )

        with patch.dict(
            os.environ,
            {"PYTHONPATH": str(injection_dir), "PYTHONHOME": str(injection_dir)},
            clear=False,
        ):
            subprocess.run(
                [sys.executable, "-c", "pass"],
                env=self.env.subprocess_env(),
                capture_output=True,
                text=True,
                check=True,
            )

        self.assertFalse(marker.exists())


if __name__ == "__main__":
    unittest.main()
