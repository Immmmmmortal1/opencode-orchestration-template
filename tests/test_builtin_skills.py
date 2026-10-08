from __future__ import annotations

import unittest

from orchagent.builtin_skills import (
    BUILTIN_SKILL_IMPLEMENTATIONS,
    assert_json_path_equals,
    emit_json,
    execute_builtin,
)


class BuiltinSkillsTests(unittest.TestCase):
    def test_emit_json_preserves_value(self) -> None:
        value = {"nested": [1, None, {"ok": True}]}

        self.assertEqual({"value": value}, emit_json({"value": value}))
        self.assertEqual(
            {"value": value},
            execute_builtin("orchagent.pipeline.emit-json", {"value": value}),
        )

    def test_emit_json_requires_value(self) -> None:
        for params in ({}, None, []):
            with self.subTest(params=params):
                self.assertEqual("invalid_params", emit_json(params)["error"])

    def test_assert_json_path_equals_passes_and_fails(self) -> None:
        actual = {"value": {"state": "ready"}}

        passed = assert_json_path_equals({"path": "$.value.state", "equals": "ready"}, actual)
        failed = assert_json_path_equals({"path": "$.value.state", "equals": "failed"}, actual)

        self.assertEqual("pass", passed["verdict"])
        self.assertEqual("fail", failed["verdict"])
        self.assertEqual("ready", failed["evidence"]["actual"])

    def test_assert_json_path_equals_reports_missing_path_as_failure(self) -> None:
        checked = assert_json_path_equals({"path": "$.value.missing", "equals": None}, {"value": {}})

        self.assertEqual("fail", checked["verdict"])
        self.assertIsNone(checked["evidence"]["actual"])

    def test_assert_json_path_equals_rejects_missing_params_and_unsupported_paths(self) -> None:
        for params in ({"path": "$.value"}, {"equals": 1}, None):
            with self.subTest(params=params):
                self.assertEqual("invalid_params", assert_json_path_equals(params, {})["error"])

        for path in ("value", "$", "$.items[0]", "$.items.*", "$.a..b"):
            with self.subTest(path=path):
                rejected = assert_json_path_equals({"path": path, "equals": 1}, {})
                self.assertEqual("unsupported_json_path", rejected["error"])

    def test_execute_builtin_resolves_catalog_and_rejects_unknown_skill(self) -> None:
        self.assertEqual(
            {"verdict": "pass", "evidence": {"path": "$.value", "actual": 1, "expected": 1}},
            execute_builtin(
                "orchagent.pipeline.assert-json-path-equals",
                {"path": "$.value", "equals": 1},
                {"value": 1},
            ),
        )
        self.assertEqual("builtin_skill_not_found", execute_builtin("unknown", {})["error"])
        self.assertEqual(
            {"pipeline.emit_json", "pipeline.assert_json_path_equals"},
            set(BUILTIN_SKILL_IMPLEMENTATIONS.values()),
        )


if __name__ == "__main__":
    unittest.main()
