from __future__ import annotations

import unittest

from tests.helpers import IsolatedEnv


EXTENSION_TYPES = ("hooks", "skills", "mcp", "knowledge", "pipeline")


class ExtensionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()
        self.env.install_home()

    def tearDown(self) -> None:
        self.env.tearDown()

    def test_default_templates_report_accurate_runtime_status(self) -> None:
        code, payload, _, _ = self.env.run_cli("extensions", "list")

        self.assertEqual(0, code)
        rows = {row["type"]: row for row in payload["extensions"]}
        self.assertEqual(
            {
                "hooks": "dryRunOnly",
                "skills": "notImplemented",
                "mcp": "notImplemented",
                "knowledge": "searchOnly",
                "pipeline": "builtinOnly",
            },
            {kind: row["runtime"] for kind, row in rows.items()},
        )
        self.assertTrue(all(row["status"] == "declared" for row in rows.values()))

    def test_type_filter_only_returns_knowledge(self) -> None:
        code, payload, _, _ = self.env.run_cli(
            "extensions", "list", "--type", "knowledge"
        )

        self.assertEqual(0, code)
        self.assertEqual(1, len(payload["extensions"]))
        self.assertEqual("knowledge", payload["extensions"][0]["type"])

    def test_non_object_registry_roots_return_structured_error(self) -> None:
        malformed_roots = ("[]\n", "null\n", '"str"\n', "123\n")
        for kind in EXTENSION_TYPES:
            registry = self.env.home / "extensions" / f"{kind}.yaml"
            for content in malformed_roots:
                with self.subTest(kind=kind, content=content.strip()):
                    registry.write_text(content, encoding="utf-8")

                    code, payload, _, stderr = self.env.run_cli(
                        "extensions", "list", "--type", kind
                    )

                    self.assertEqual(0, code, stderr)
                    self.assertEqual(1, len(payload["extensions"]))
                    row = payload["extensions"][0]
                    self.assertEqual(kind, row["type"])
                    self.assertEqual("error", row["status"])
                    self.assertEqual("registry root must be an object", row["reason"])

    def test_missing_registry_reports_missing(self) -> None:
        for kind in EXTENSION_TYPES:
            with self.subTest(kind=kind):
                registry = self.env.home / "extensions" / f"{kind}.yaml"
                original = registry.read_bytes()
                registry.unlink()

                code, payload, _, stderr = self.env.run_cli(
                    "extensions", "list", "--type", kind
                )

                self.assertEqual(0, code, stderr)
                self.assertEqual("missing", payload["extensions"][0]["status"])
                # 下一个子用例开始前恢复模板，避免共享环境产生串扰。
                registry.write_bytes(original)


if __name__ == "__main__":
    unittest.main()
