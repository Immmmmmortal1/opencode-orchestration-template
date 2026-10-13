from __future__ import annotations

import unittest

from tests.helpers import IsolatedEnv


class ConsoleCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()
        self.env.install_home()

    def tearDown(self) -> None:
        self.env.tearDown()

    def test_snapshot_and_validate_work_in_isolated_env(self) -> None:
        for command in ("snapshot", "validate"):
            code, payload, _, stderr = self.env.run_cli("console", command)
            self.assertEqual(0, code, stderr)
            self.assertEqual("ok", payload["status"])
            self.assertTrue(payload["snapshotHash"])

    def test_serve_rejects_wildcard_bind(self) -> None:
        code, payload, _, _ = self.env.run_cli("console", "serve", "--host", "0.0.0.0")
        self.assertNotEqual(0, code)
        self.assertEqual("console_host_must_be_loopback", payload["error"])


if __name__ == "__main__":
    unittest.main()
