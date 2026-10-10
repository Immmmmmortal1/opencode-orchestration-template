from __future__ import annotations

import copy
import json
import unittest
from unittest.mock import patch
from typing import Any

from orchagent.config import validate_extension_registry
from orchagent.pipeline import doctor_pipelines, list_pipelines, list_roles, run_advance, run_pipeline
from orchagent.session import create_session, load_session, lease_path
from tests.helpers import IsolatedEnv


def role_registry() -> dict[str, Any]:
    return {
        "version": 2,
        "roles": [
            {"id": "investigator", "provider": "deepseek", "model": "deepseek-v4-flash", "description": "定位根因"},
            {"id": "coder", "provider": "glm", "model": "glm-5.2", "description": "写代码"},
        ],
        "pipelines": [{
            "id": "bugfix", "revision": "1", "enabled": True, "entryStage": "s1",
            "stages": [{"id": "s1", "role": "investigator", "maxAttempts": 3, "params": {}, "gates": ["g1"]}],
            "gates": [{"id": "g1", "stage": "s1", "evaluator": "some-check", "params": {}}],
            "edges": [
                {"from": {"stage": "s1", "gate": "g1", "verdict": "pass"}, "to": {"terminal": "succeeded"}},
                {"from": {"stage": "s1", "gate": "g1", "verdict": "fail"}, "to": {"stage": "s1"}},
            ],
        }],
    }


def advance_registry() -> dict[str, Any]:
    roles = [
        {"id": "investigator", "provider": "deepseek", "model": "deepseek-v4-flash"},
        {"id": "fixer", "provider": "glm", "model": "glm-5.2"},
        {"id": "verifier", "provider": "openai", "model": "gpt-5.5"},
    ]
    stage_specs = (
        ("investigate", "investigator", "gate-investigate", 2),
        ("repro", "investigator", "gate-repro", 2),
        ("fix", "fixer", "gate-fix", 2),
        ("verify", "verifier", "gate-verify", 2),
    )
    return {
        "version": 2,
        "roles": roles,
        "pipelines": [{
            "id": "bugfix",
            "revision": "1",
            "enabled": True,
            "entryStage": "investigate",
            "stages": [
                {"id": stage, "role": role, "maxAttempts": attempts, "params": {}, "gates": [gate]}
                for stage, role, gate, attempts in stage_specs
            ],
            "gates": [
                {"id": gate, "stage": stage, "evaluator": f"evaluate-{stage}", "params": {}}
                for stage, _, gate, _ in stage_specs
            ],
            "edges": [
                {"from": {"stage": "investigate", "gate": "gate-investigate", "verdict": "pass"}, "to": {"stage": "repro"}},
                {"from": {"stage": "investigate", "gate": "gate-investigate", "verdict": "fail"}, "to": {"stage": None}},
                {"from": {"stage": "repro", "gate": "gate-repro", "verdict": "pass"}, "to": {"stage": "fix"}},
                {"from": {"stage": "repro", "gate": "gate-repro", "verdict": "fail"}, "to": {"stage": None}},
                {"from": {"stage": "fix", "gate": "gate-fix", "verdict": "pass"}, "to": {"stage": "verify"}},
                {"from": {"stage": "fix", "gate": "gate-fix", "verdict": "fail"}, "to": {"stage": "fix"}},
                {"from": {"stage": "verify", "gate": "gate-verify", "verdict": "pass"}, "to": {"terminal": "succeeded"}},
                {"from": {"stage": "verify", "gate": "gate-verify", "verdict": "fail"}, "to": {"stage": "repro"}},
            ],
        }],
    }


class PipelineV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()
        self.env.install_home()

    def tearDown(self) -> None:
        self.env.tearDown()

    def assert_error(self, data: dict[str, Any], message: str | None = None) -> None:
        self.env.write_registry("pipeline", data)
        listed = list_pipelines(self.env.home)
        self.assertEqual("error", listed["status"])
        if message:
            self.assertIn(message, listed["checks"][0]["message"])

    def install_v1_skills(self) -> None:
        skills = [
            ("orchagent.pipeline.emit-json", "emit-json"),
            ("orchagent.pipeline.assert-json-path-equals", "assert-json-path-equals"),
        ]
        entries = []
        for skill_id, path in skills:
            directory = self.env.home / "skills" / path
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "SKILL.md").write_text(
                f"---\nname: {skill_id}\ndescription: fixture\n---\n", encoding="utf-8"
            )
            entries.append({
                "id": skill_id,
                "adapter": "fixture",
                "path": path,
                "backend": "builtin",
                "enabled": True,
            })
        self.env.write_registry("skills", {
            "version": 1,
            "adapters": [{"id": "fixture", "type": "filesystem", "enabled": True, "root": "skills"}],
            "skills": entries,
        })

    def start_advance_run(self) -> dict[str, Any]:
        self.env.write_registry("pipeline", advance_registry())
        return run_pipeline(self.env.home, "bugfix", new_session_summary="advance test")

    def advance_passes(self, session_id: str, count: int) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for _ in range(count):
            result = run_advance(self.env.home, session_id, "pass")
            self.assertEqual("ok", result["status"])
        return result

    def test_valid_v2_lists_roles_and_role_reference(self) -> None:
        data = role_registry()
        self.env.write_registry("pipeline", data)
        listed = list_pipelines(self.env.home)
        self.assertEqual("ok", listed["status"])
        self.assertEqual(data["roles"], listed["roles"])
        self.assertEqual("bugfix", listed["pipelines"][0]["id"])
        self.assertTrue(doctor_pipelines(self.env.home, {})[0])

    def test_version_is_strict_and_v1_roles_are_rejected(self) -> None:
        for version in (2.0, True, "2"):
            data = role_registry()
            data["version"] = version
            self.assert_error(data)
        data = {"version": 1, "roles": [], "pipelines": []}
        self.assert_error(data)

    def test_v2_stage_role_contract(self) -> None:
        for mutation, message in (
            (lambda d: d["pipelines"][0]["stages"][0].update(skill="x"), "unknown fields"),
            (lambda d: d["pipelines"][0]["stages"][0].update(role="missing"), "undeclared role"),
            (lambda d: d["pipelines"][0]["stages"][0].pop("role"), "role must be non-empty string"),
        ):
            data = role_registry()
            mutation(data)
            self.assert_error(data, message)

    def test_role_fields_are_strict_and_credentials_are_rejected(self) -> None:
        data = role_registry()
        data["roles"].append(copy.deepcopy(data["roles"][0]))
        self.assert_error(data, "duplicate role id")
        for key in ("api_key", "key", "token", "secret", "env"):
            data = role_registry()
            data["roles"][0][key] = "forbidden"
            self.assert_error(data, "unknown fields")
        for provider in (None, ""):
            data = role_registry()
            if provider is None:
                data["roles"][0].pop("provider")
            else:
                data["roles"][0]["provider"] = provider
            self.assert_error(data, "provider must be non-empty string")

    def test_list_roles_v2_and_v1(self) -> None:
        self.env.write_registry("pipeline", role_registry())
        result = list_roles(self.env.home)
        self.assertEqual({"status", "version", "roles"}, set(result))
        self.assertEqual(2, result["version"])
        self.assertEqual(2, len(result["roles"]))
        self.env.write_registry("pipeline", {"version": 1, "pipelines": []})
        self.assertEqual("unsupported", list_roles(self.env.home)["status"])

    def test_v1_listing_shape_remains_unchanged_and_roles_are_redacted(self) -> None:
        self.env.write_registry("pipeline", {"version": 1, "pipelines": []})
        listed = list_pipelines(self.env.home)
        self.assertEqual({"status", "registry", "runtime", "pipelines", "checks"}, set(listed))
        self.assertNotIn("roles", listed)
        self.env.write_registry("pipeline", role_registry())
        listed = list_pipelines(self.env.home)
        for role in listed["roles"]:
            self.assertFalse({"api_key", "key", "token", "secret", "env"} & set(role))

    def test_config_validation_accepts_only_integer_pipeline_versions_one_or_two(self) -> None:
        path = self.env.home / "extensions" / "pipeline.yaml"
        for version in (True, 1.0, "2", 3):
            path.write_text(f'{{"version": {json.dumps(version)}, "pipelines": []}}\n', encoding="utf-8")
            self.assertEqual([f"{path}: version must be 1 or 2"], validate_extension_registry(path))

        path.write_text('{"version": 2, "roles": [], "pipelines": []}\n', encoding="utf-8")
        self.assertEqual([], validate_extension_registry(path))

        skills_path = self.env.home / "extensions" / "skills.yaml"
        skills_path.write_text('{"version": 2, "adapters": []}\n', encoding="utf-8")
        self.assertEqual([f"{skills_path}: version must be 1"], validate_extension_registry(skills_path))

    def test_v2_dispatch_with_new_session_echoes_session_and_role(self) -> None:
        self.env.write_registry("pipeline", role_registry())

        result = run_pipeline(self.env.home, "bugfix", new_session_summary="定位登录 bug", now_ns=10)

        self.assertEqual("ok", result["status"])
        self.assertEqual("dispatch", result["mode"])
        self.assertTrue(result["sessionId"])
        self.assertEqual("investigator", result["role"]["id"])
        self.assertEqual("deepseek", result["role"]["provider"])
        self.assertEqual("deepseek-v4-flash", result["role"]["model"])
        self.assertIn("investigator", result["message"])
        self.assertFalse(lease_path(self.env.home, result["sessionId"]).exists())

    def test_v2_dispatch_with_existing_session(self) -> None:
        self.env.write_registry("pipeline", role_registry())
        session = create_session(self.env.home, summary="existing session", task_type="pipeline", now_ns=10)["session"]
        result = run_pipeline(self.env.home, "bugfix", session_id=session["id"], now_ns=20)

        self.assertEqual("ok", result["status"])
        self.assertEqual("dispatch", result["mode"])
        self.assertEqual(session["id"], result["sessionId"])
        self.assertEqual("investigator", result["role"]["id"])

    def test_v2_dispatch_never_executes_skill_and_session_stays_non_terminal(self) -> None:
        self.env.write_registry("pipeline", role_registry())

        with patch("orchagent.pipeline.execute_builtin", side_effect=AssertionError("skill executed")):
            result = run_pipeline(self.env.home, "bugfix", new_session_summary="不执行 skill", now_ns=30)

        self.assertEqual("dispatch", result["mode"])
        persisted = load_session(self.env.home, result["sessionId"])["session"]
        self.assertNotIn(persisted["status"], {"succeeded", "failed", "cancelled"})
        run = persisted["pipelineRuns"][result["pipeline"]["runId"]]
        self.assertEqual("active", run["status"])
        self.assertEqual([], run["stageExecutions"])

    def test_advance_pass_from_fix_dispatches_verifier(self) -> None:
        started = self.start_advance_run()
        result = self.advance_passes(started["sessionId"], 3)

        self.assertEqual("dispatch", result["mode"])
        self.assertEqual("verify", result["stage"])
        self.assertEqual("verifier", result["role"]["id"])
        self.assertEqual("openai", result["role"]["provider"])

    def test_advance_pass_to_terminal_finalizes_session(self) -> None:
        started = self.start_advance_run()
        result = self.advance_passes(started["sessionId"], 4)

        self.assertEqual("terminal", result["mode"])
        self.assertEqual("succeeded", result["terminalReason"])
        persisted = load_session(self.env.home, started["sessionId"])["session"]
        self.assertEqual("succeeded", persisted["status"])
        self.assertFalse(lease_path(self.env.home, started["sessionId"]).exists())

    def test_advance_fail_with_capacity_rolls_back(self) -> None:
        started = self.start_advance_run()
        self.advance_passes(started["sessionId"], 3)

        result = run_advance(self.env.home, started["sessionId"], "fail")

        self.assertEqual("dispatch", result["mode"])
        self.assertEqual("repro", result["stage"])
        self.assertEqual("investigator", result["role"]["id"])

    def test_advance_rollback_invalidates_target_and_downstream_gate_results(self) -> None:
        started = self.start_advance_run()
        self.advance_passes(started["sessionId"], 3)

        result = run_advance(self.env.home, started["sessionId"], "fail")

        self.assertEqual("repro", result["stage"])
        run = load_session(self.env.home, started["sessionId"])["session"]["pipelineRuns"][started["pipeline"]["runId"]]
        invalidated = {
            row["gateKey"]["stageId"]: row["invalidatedReason"]
            for row in run["gateResults"]
            if row["gateKey"]["stageId"] in {"repro", "fix", "verify"}
        }
        self.assertEqual({"repro", "fix", "verify"}, set(invalidated))
        self.assertTrue(all(reason == "rollback_to:repro" for reason in invalidated.values()))

    def test_advance_fail_with_exhausted_target_terminates(self) -> None:
        started = self.start_advance_run()
        self.advance_passes(started["sessionId"], 3)
        self.assertEqual("repro", run_advance(self.env.home, started["sessionId"], "fail")["stage"])
        self.advance_passes(started["sessionId"], 2)
        result = run_advance(self.env.home, started["sessionId"], "fail")

        self.assertEqual("terminal", result["mode"])
        self.assertEqual("attempts_exhausted", result["terminalReason"])
        self.assertEqual("failed", load_session(self.env.home, started["sessionId"])["session"]["status"])

    def test_advance_fail_without_rollback_target_is_gate_rejected(self) -> None:
        started = self.start_advance_run()

        result = run_advance(self.env.home, started["sessionId"], "fail")

        self.assertEqual("terminal", result["mode"])
        self.assertEqual("gate_rejected", result["terminalReason"])

    def test_advance_rejects_invalid_verdict(self) -> None:
        started = self.start_advance_run()
        result = run_advance(self.env.home, started["sessionId"], "maybe")
        self.assertEqual("invalid_verdict", result["error"])

    def test_terminal_session_cannot_advance_again(self) -> None:
        started = self.start_advance_run()
        self.advance_passes(started["sessionId"], 4)

        result = run_advance(self.env.home, started["sessionId"], "pass")

        self.assertEqual("session_terminal", result["error"])

    def test_advance_rejects_multi_gate_stage(self) -> None:
        data = role_registry()
        pipeline = data["pipelines"][0]
        pipeline["stages"][0]["gates"].append("g2")
        pipeline["gates"].append({"id": "g2", "stage": "s1", "evaluator": "second-check", "params": {}})
        pipeline["edges"] = [
            {"from": {"stage": "s1", "gate": gate, "verdict": verdict}, "to": target}
            for gate in ("g1", "g2")
            for verdict, target in (
                ("pass", {"terminal": "succeeded"}),
                ("fail", {"stage": "s1"}),
            )
        ]
        self.env.write_registry("pipeline", data)
        started = run_pipeline(self.env.home, "bugfix", new_session_summary="multi gate")

        result = run_advance(self.env.home, started["sessionId"], "pass")

        self.assertEqual("unsupported_multi_gate", result["error"])
        self.assertFalse(lease_path(self.env.home, started["sessionId"]).exists())

    def test_each_advance_releases_lease_and_can_advance_again(self) -> None:
        started = self.start_advance_run()
        first = run_advance(self.env.home, started["sessionId"], "pass")
        self.assertEqual("ok", first["status"])
        self.assertFalse(lease_path(self.env.home, started["sessionId"]).exists())

        second = run_advance(self.env.home, started["sessionId"], "pass")
        self.assertEqual("ok", second["status"])
        self.assertFalse(lease_path(self.env.home, started["sessionId"]).exists())

    def test_advance_uses_highest_sequence_run(self) -> None:
        self.env.write_registry("pipeline", advance_registry())
        session = create_session(self.env.home, summary="multiple runs", task_type="pipeline")["session"]
        first = run_pipeline(self.env.home, "bugfix", session_id=session["id"])
        second = run_pipeline(self.env.home, "bugfix", session_id=session["id"])

        result = run_advance(self.env.home, session["id"], "pass")

        self.assertEqual(second["pipeline"]["runId"], result["pipeline"]["runId"])
        persisted = load_session(self.env.home, session["id"])["session"]
        first_run = persisted["pipelineRuns"][first["pipeline"]["runId"]]
        second_run = persisted["pipelineRuns"][second["pipeline"]["runId"]]
        self.assertEqual({}, first_run["attempts"])
        self.assertEqual(1, second_run["attempts"]["investigate"])
        self.assertLess(first_run["sequence"], second_run["sequence"])

    def test_cli_advance_parses_evidence_and_returns_success(self) -> None:
        started = self.start_advance_run()
        code, payload, _, stderr = self.env.run_cli(
            "pipeline",
            "advance",
            "--session-id",
            started["sessionId"],
            "--verdict",
            "pass",
            "--evidence",
            '{"case":"red"}',
        )

        self.assertEqual(0, code, stderr)
        self.assertEqual("repro", payload["stage"])
        persisted = load_session(self.env.home, started["sessionId"])["session"]
        run = persisted["pipelineRuns"][started["pipeline"]["runId"]]
        self.assertEqual({"case": "red"}, run["gateResults"][0]["evidence"])

    def test_v1_pipeline_run_remains_builtin_execution(self) -> None:
        registry = {
            "version": 1,
            "pipelines": [{
                "id": "fixture", "revision": "1", "enabled": True, "entryStage": "s1",
                "stages": [{"id": "s1", "skill": "orchagent.pipeline.emit-json", "maxAttempts": 1, "params": {"value": {"ok": True}}, "gates": ["g1"]}],
                "gates": [{"id": "g1", "stage": "s1", "evaluator": "orchagent.pipeline.assert-json-path-equals", "params": {"path": "$.value.ok", "equals": True}}],
                "edges": [
                    {"from": {"stage": "s1", "gate": "g1", "verdict": "pass"}, "to": {"terminal": "succeeded"}},
                    {"from": {"stage": "s1", "gate": "g1", "verdict": "fail"}, "to": {"stage": None}},
                ],
            }],
        }
        self.install_v1_skills()
        self.env.write_registry("pipeline", registry)

        result = run_pipeline(self.env.home, "fixture", new_session_summary="v1", now_ns=40)

        self.assertEqual("succeeded", result["terminalReason"])
        persisted = load_session(self.env.home, result["sessionId"])["session"]
        run = persisted["pipelineRuns"][result["pipeline"]["runId"]]
        self.assertNotIn("sequence", run)

    def test_cli_roles_list_v2_and_v1_exit_codes(self) -> None:
        self.env.write_registry("pipeline", role_registry())
        code, payload, _, stderr = self.env.run_cli("pipeline", "roles", "list")
        self.assertEqual(0, code, stderr)
        self.assertEqual("ok", payload["status"])
        self.assertEqual("investigator", payload["roles"][0]["id"])

        self.env.write_registry("pipeline", {"version": 1, "pipelines": []})
        code, payload, _, stderr = self.env.run_cli("pipeline", "roles", "list")
        self.assertEqual(1, code, stderr)
        self.assertEqual("unsupported", payload["status"])

    def test_session_selection_is_mutually_exclusive(self) -> None:
        self.env.write_registry("pipeline", role_registry())
        result = run_pipeline(
            self.env.home,
            "bugfix",
            session_id="session",
            new_session_summary="同时传入",
        )
        self.assertEqual("invalid_session_selection", result["error"])


if __name__ == "__main__":
    unittest.main()
