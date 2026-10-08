from __future__ import annotations

import copy
import unittest
from typing import Any
from unittest.mock import patch

from orchagent.pipeline import decide_terminal, doctor_pipelines, list_pipelines, load_pipeline_registry, run_pipeline
from orchagent.session import acquire_lease, create_session, lease_path, load_session

from tests.helpers import IsolatedEnv


EMIT = "orchagent.pipeline.emit-json"
ASSERT = "orchagent.pipeline.assert-json-path-equals"


def pipeline() -> dict[str, Any]:
    return {
        "id": "fixture",
        "revision": "1",
        "enabled": True,
        "entryStage": "emit",
        "stages": [
            {
                "id": "emit",
                "skill": EMIT,
                "maxAttempts": 1,
                "params": {"value": {"ok": True}},
                "gates": ["assert-ok"],
            }
        ],
        "gates": [
            {
                "id": "assert-ok",
                "stage": "emit",
                "evaluator": ASSERT,
                "params": {"path": "$.value.ok", "equals": True},
            }
        ],
        "edges": [
            {
                "from": {"stage": "emit", "gate": "assert-ok", "verdict": "pass"},
                "to": {"terminal": "succeeded"},
            },
            {
                "from": {"stage": "emit", "gate": "assert-ok", "verdict": "fail"},
                "to": {"stage": None},
            },
        ],
    }


def registry(item: Any = None) -> dict[str, Any]:
    return {"version": 1, "pipelines": [pipeline() if item is None else item]}


class PipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()
        self.env.install_home()
        self.skills = {
            EMIT: {"id": EMIT, "backend": "builtin"},
            ASSERT: {"id": ASSERT, "backend": "builtin"},
        }

    def tearDown(self) -> None:
        self.env.tearDown()

    def assert_registry_error(self, data: Any) -> None:
        self.env.write_registry("pipeline", data)
        listed = list_pipelines(self.env.home)
        ok, diagnosed = doctor_pipelines(self.env.home, self.skills)
        self.assertEqual("error", listed["status"])
        self.assertFalse(ok)
        self.assertTrue(any(check["level"] == "error" for check in diagnosed["checks"]))

    def install_builtin_skills(self, *, disabled: str | None = None) -> None:
        root = self.env.home / "skills"
        entries = []
        for skill_id in (EMIT, ASSERT):
            directory = root / skill_id
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "SKILL.md").write_text(
                f"---\nname: {skill_id}\ndescription: fixture\n---\n",
                encoding="utf-8",
            )
            entries.append({
                "id": skill_id,
                "adapter": "fixture",
                "path": skill_id,
                "backend": "builtin",
                "enabled": skill_id != disabled,
            })
        self.env.write_registry("skills", {
            "version": 1,
            "adapters": [{"id": "fixture", "type": "filesystem", "enabled": True, "root": "skills"}],
            "skills": entries,
        })

    def test_default_registry_is_empty_and_builtin_only(self) -> None:
        loaded, error = load_pipeline_registry(self.env.home)
        listed = list_pipelines(self.env.home)
        ok, diagnosed = doctor_pipelines(self.env.home, self.skills)

        self.assertIsNone(error)
        self.assertEqual([], loaded["pipelines"])
        self.assertEqual("ok", listed["status"])
        self.assertEqual("builtinOnly", listed["runtime"])
        self.assertEqual([], listed["pipelines"])
        self.assertTrue(ok, diagnosed)

    def test_valid_pipeline_and_cli_commands_succeed(self) -> None:
        self.env.write_registry("pipeline", registry())
        listed = list_pipelines(self.env.home)
        ok, diagnosed = doctor_pipelines(self.env.home, self.skills)

        self.assertEqual("ok", listed["status"])
        self.assertTrue(ok, diagnosed)
        self.assertEqual("fixture", listed["pipelines"][0]["id"])

    def test_rejects_root_pipeline_and_nested_unknown_fields(self) -> None:
        cases: list[Any] = [[], {"version": 1, "pipelines": [], "extra": True}]
        for location in ("pipeline", "stage", "gate", "edge", "from", "to"):
            item = pipeline()
            if location == "pipeline":
                item["extra"] = True
            elif location == "stage":
                item["stages"][0]["extra"] = True
            elif location == "gate":
                item["gates"][0]["extra"] = True
            elif location == "edge":
                item["edges"][0]["extra"] = True
            else:
                item["edges"][0][location]["extra"] = True
            cases.append(registry(item))
        for data in cases:
            with self.subTest(data=data):
                self.assert_registry_error(data)

    def test_rejects_invalid_pipeline_stage_and_gate_contracts(self) -> None:
        mutations = [
            ("duplicate pipeline", lambda data: data["pipelines"].append(copy.deepcopy(data["pipelines"][0]))),
            ("empty revision", lambda data: data["pipelines"][0].update(revision="")),
            ("invalid enabled", lambda data: data["pipelines"][0].update(enabled=1)),
            ("missing entry", lambda data: data["pipelines"][0].update(entryStage="missing")),
            ("duplicate stage", lambda data: data["pipelines"][0]["stages"].append(copy.deepcopy(data["pipelines"][0]["stages"][0]))),
            ("bool attempts", lambda data: data["pipelines"][0]["stages"][0].update(maxAttempts=True)),
            ("missing gate stage", lambda data: data["pipelines"][0]["gates"][0].update(stage="missing")),
            ("undeclared gate", lambda data: data["pipelines"][0]["stages"][0].update(gates=[])),
            ("duplicate gate", lambda data: data["pipelines"][0]["gates"].append(copy.deepcopy(data["pipelines"][0]["gates"][0]))),
            ("empty stage gates", lambda data: data["pipelines"][0]["stages"][0].update(gates=[])),
        ]
        for name, mutate in mutations:
            data = registry()
            mutate(data)
            with self.subTest(name=name):
                self.assert_registry_error(data)

    def test_rejects_invalid_and_incomplete_edges(self) -> None:
        mutations = [
            ("missing stage", lambda edge: edge["from"].update(stage="missing")),
            ("missing gate", lambda edge: edge["from"].update(gate="missing")),
            ("invalid verdict", lambda edge: edge["from"].update(verdict="maybe")),
            ("invalid terminal", lambda edge: edge.update(to={"terminal": "failed"})),
            ("missing target", lambda edge: edge.update(to={"stage": "missing"})),
            ("mixed target", lambda edge: edge.update(to={"stage": None, "terminal": "succeeded"})),
        ]
        for name, mutate in mutations:
            data = registry()
            mutate(data["pipelines"][0]["edges"][0])
            with self.subTest(name=name):
                self.assert_registry_error(data)

        data = registry()
        data["pipelines"][0]["edges"].pop()
        self.assert_registry_error(data)
        data = registry()
        data["pipelines"][0]["edges"].append(copy.deepcopy(data["pipelines"][0]["edges"][0]))
        self.assert_registry_error(data)

    def test_rejects_verdict_incompatible_targets_and_last_gate_pass_cycles(self) -> None:
        pass_null = registry()
        pass_null["pipelines"][0]["edges"][0]["to"] = {"stage": None}
        self.assert_registry_error(pass_null)

        fail_terminal = registry()
        fail_terminal["pipelines"][0]["edges"][1]["to"] = {"terminal": "succeeded"}
        self.assert_registry_error(fail_terminal)

        cyclic = pipeline()
        second_stage = copy.deepcopy(cyclic["stages"][0])
        second_stage.update(id="second", gates=["assert-second"])
        cyclic["stages"].append(second_stage)
        cyclic["gates"].append({
            "id": "assert-second",
            "stage": "second",
            "evaluator": ASSERT,
            "params": {"path": "$.value.ok", "equals": True},
        })
        cyclic["edges"] = [
            {"from": {"stage": "emit", "gate": "assert-ok", "verdict": "pass"}, "to": {"stage": "second"}},
            {"from": {"stage": "emit", "gate": "assert-ok", "verdict": "fail"}, "to": {"stage": None}},
            {"from": {"stage": "second", "gate": "assert-second", "verdict": "pass"}, "to": {"stage": "emit"}},
            {"from": {"stage": "second", "gate": "assert-second", "verdict": "fail"}, "to": {"stage": None}},
        ]
        self.assert_registry_error(registry(cyclic))

    def test_doctor_rejects_unregistered_and_uncatalogued_skills(self) -> None:
        self.env.write_registry("pipeline", registry())
        ok, result = doctor_pipelines(self.env.home, {EMIT: self.skills[EMIT]})
        self.assertFalse(ok, result)

        lookup = dict(self.skills)
        lookup[EMIT] = {"id": EMIT, "backend": "agent"}
        ok, result = doctor_pipelines(self.env.home, lookup)
        self.assertFalse(ok, result)

        data = registry()
        data["pipelines"][0]["stages"][0]["skill"] = "custom.builtin"
        self.env.write_registry("pipeline", data)
        lookup = dict(self.skills)
        lookup["custom.builtin"] = {"id": "custom.builtin", "backend": "builtin"}
        ok, result = doctor_pipelines(self.env.home, lookup)
        self.assertFalse(ok, result)
        self.assertIn("no catalog implementation", result["checks"][0]["message"])

    def test_doctor_rejects_disabled_skill_reference(self) -> None:
        """引用了 disabled skill（自身 disabled 或 adapter disabled）必须 fail-closed。"""
        self.env.write_registry("pipeline", registry())

        # 自身 disabled
        lookup = dict(self.skills)
        lookup[EMIT] = {**self.skills[EMIT], "enabled": False}
        ok, result = doctor_pipelines(self.env.home, lookup)
        self.assertFalse(ok, result)
        self.assertIn("references disabled skill", result["checks"][0]["message"])

        # adapter disabled（skills list 里表现为 status=disabled）
        lookup = dict(self.skills)
        lookup[EMIT] = {**self.skills[EMIT], "enabled": True, "status": "disabled"}
        ok, result = doctor_pipelines(self.env.home, lookup)
        self.assertFalse(ok, result)
        self.assertIn("references disabled skill", result["checks"][0]["message"])

        # 字段缺失按启用处理（不误报）
        lookup = dict(self.skills)
        lookup[EMIT] = {"id": EMIT, "backend": "builtin"}
        ok, result = doctor_pipelines(self.env.home, lookup)
        self.assertTrue(ok, result)

    def test_run_success_persists_stable_keys_and_releases_lease(self) -> None:
        self.install_builtin_skills()
        self.env.write_registry("pipeline", registry())

        result = run_pipeline(self.env.home, "fixture", new_session_summary="successful pipeline", now_ns=10)

        self.assertEqual("ok", result["status"])
        self.assertEqual("succeeded", result["terminalReason"])
        loaded = load_session(self.env.home, result["sessionId"])
        self.assertEqual("succeeded", loaded["session"]["status"])
        self.assertFalse(lease_path(self.env.home, result["sessionId"]).exists())
        run = loaded["session"]["pipelineRuns"][result["pipeline"]["runId"]]
        self.assertEqual("succeeded", run["status"])
        stage_key = run["stageExecutions"][0]["stageKey"]
        gate_key = run["gateResults"][0]["gateKey"]
        self.assertEqual(1, stage_key["attemptNo"])
        self.assertEqual({"emit": 1}, run["attempts"])
        self.assertEqual(
            {"sessionId", "pipelineRevision", "runId", "stageId", "attemptNo"},
            set(stage_key),
        )
        self.assertEqual(set(stage_key) | {"gateId"}, set(gate_key))
        self.assertIn("evidenceRef", run["gateResults"][0])
        self.assertIsNone(run["gateResults"][0]["evidenceRef"])
        self.assertIn("evidence", run["gateResults"][0])

    def test_multiple_gates_all_pass_advances_via_last_gate_pass_edge(self) -> None:
        """同一 stage 多 gate 全部通过时，以最后一个 gate 的 pass 边决定推进（规范 §5.7）。"""
        self.install_builtin_skills()
        item = pipeline()
        item["stages"][0]["gates"] = ["g1", "g2"]
        item["gates"] = [
            {"id": "g1", "stage": "emit", "evaluator": ASSERT, "params": {"path": "$.value.ok", "equals": True}},
            {"id": "g2", "stage": "emit", "evaluator": ASSERT, "params": {"path": "$.value.ok", "equals": True}},
        ]
        item["edges"] = [
            # 前序 gate 的 pass 边故意自指；它不参与推进，也不参与 pass 图环校验。
            {"from": {"stage": "emit", "gate": "g1", "verdict": "pass"}, "to": {"stage": "emit"}},
            {"from": {"stage": "emit", "gate": "g1", "verdict": "fail"}, "to": {"stage": None}},
            {"from": {"stage": "emit", "gate": "g2", "verdict": "pass"}, "to": {"terminal": "succeeded"}},
            {"from": {"stage": "emit", "gate": "g2", "verdict": "fail"}, "to": {"stage": None}},
        ]
        self.env.write_registry("pipeline", registry(item))

        result = run_pipeline(self.env.home, "fixture", new_session_summary="multi gate", now_ns=15)

        self.assertEqual("ok", result["status"], result)
        self.assertEqual("succeeded", result["terminalReason"])
        run = load_session(self.env.home, result["sessionId"])["session"]["pipelineRuns"][result["pipeline"]["runId"]]
        self.assertEqual(2, len(run["gateResults"]))

    def test_gate_rejection_finalizes_failed_and_releases_lease(self) -> None:
        self.install_builtin_skills()
        data = registry()
        data["pipelines"][0]["stages"][0]["params"]["value"]["ok"] = False
        self.env.write_registry("pipeline", data)

        result = run_pipeline(self.env.home, "fixture", new_session_summary="rejected pipeline", now_ns=20)

        self.assertEqual("error", result["status"])
        self.assertEqual("gate_rejected", result["error"])
        loaded = load_session(self.env.home, result["sessionId"])
        self.assertEqual("failed", loaded["session"]["status"])
        self.assertFalse(lease_path(self.env.home, result["sessionId"]).exists())

    def test_long_run_renews_lease_at_stage_and_gate_boundaries(self) -> None:
        self.install_builtin_skills()
        self.env.write_registry("pipeline", registry())
        moments = iter((0, 0, 45_000_000_000, 90_000_000_000, 135_000_000_000, 180_000_000_000, 225_000_000_000))

        result = run_pipeline(
            self.env.home,
            "fixture",
            new_session_summary="long pipeline",
            now_ns=lambda: next(moments),
        )

        self.assertEqual("ok", result["status"], result)
        self.assertEqual("succeeded", result["terminalReason"])
        self.assertFalse(lease_path(self.env.home, result["sessionId"]).exists())

    def test_renewal_failure_terminates_as_lease_lost_and_releases_lease_best_effort(self) -> None:
        self.install_builtin_skills()
        self.env.write_registry("pipeline", registry())
        moments = iter((0, 0, 45_000_000_000, 46_000_000_000, 47_000_000_000))

        with patch(
            "orchagent.pipeline.renew_lease",
            return_value={"status": "error", "error": "stale_lease_holder"},
        ):
            result = run_pipeline(
                self.env.home,
                "fixture",
                new_session_summary="lost lease",
                now_ns=lambda: next(moments),
            )

        self.assertEqual("error", result["status"])
        self.assertEqual("lease_lost", result["error"])
        self.assertEqual("lease_lost", result["terminalReason"])
        self.assertEqual("failed", result["sessionStatus"])
        self.assertEqual("failed", load_session(self.env.home, result["sessionId"])["session"]["status"])
        self.assertFalse(lease_path(self.env.home, result["sessionId"]).exists())

    def test_rollback_invalidates_output_then_exhausts_attempts(self) -> None:
        self.install_builtin_skills()
        data = registry()
        item = data["pipelines"][0]
        item["stages"][0]["maxAttempts"] = 2
        item["stages"][0]["params"]["value"]["ok"] = False
        item["edges"][1]["to"]["stage"] = "emit"
        self.env.write_registry("pipeline", data)

        result = run_pipeline(self.env.home, "fixture", new_session_summary="rollback pipeline", now_ns=30)

        self.assertEqual("attempts_exhausted", result["terminalReason"])
        loaded = load_session(self.env.home, result["sessionId"])
        run = loaded["session"]["pipelineRuns"][result["pipeline"]["runId"]]
        self.assertEqual(2, run["attempts"]["emit"])
        self.assertEqual("invalidated", run["stageExecutions"][0]["status"])
        self.assertIsNotNone(run["stageExecutions"][0]["invalidatedReason"])
        self.assertIsNotNone(run["gateResults"][0]["invalidatedReason"])
        self.assertEqual("completed", run["stageExecutions"][1]["status"])

    def test_downstream_rerun_increments_attempt_and_exhausts_real_attempts(self) -> None:
        self.install_builtin_skills()
        item = pipeline()
        item["entryStage"] = "a"
        item["stages"] = [
            {"id": "a", "skill": EMIT, "maxAttempts": 3, "params": {"value": {"ok": True}}, "gates": ["a-ok"]},
            {"id": "b", "skill": EMIT, "maxAttempts": 3, "params": {"value": {"ok": False}}, "gates": ["b-ok"]},
        ]
        item["gates"] = [
            {"id": "a-ok", "stage": "a", "evaluator": ASSERT, "params": {"path": "$.value.ok", "equals": True}},
            {"id": "b-ok", "stage": "b", "evaluator": ASSERT, "params": {"path": "$.value.ok", "equals": True}},
        ]
        item["edges"] = [
            {"from": {"stage": "a", "gate": "a-ok", "verdict": "pass"}, "to": {"stage": "b"}},
            {"from": {"stage": "a", "gate": "a-ok", "verdict": "fail"}, "to": {"stage": None}},
            {"from": {"stage": "b", "gate": "b-ok", "verdict": "pass"}, "to": {"terminal": "succeeded"}},
            {"from": {"stage": "b", "gate": "b-ok", "verdict": "fail"}, "to": {"stage": "a"}},
        ]
        self.env.write_registry("pipeline", registry(item))

        result = run_pipeline(self.env.home, "fixture", new_session_summary="downstream rerun", now_ns=35)

        self.assertEqual("attempts_exhausted", result["terminalReason"])
        run = load_session(self.env.home, result["sessionId"])["session"]["pipelineRuns"][result["pipeline"]["runId"]]
        b_keys = [row["stageKey"] for row in run["stageExecutions"] if row["stageKey"]["stageId"] == "b"]
        self.assertEqual([1, 2, 3], [key["attemptNo"] for key in b_keys])
        self.assertEqual(len(b_keys), len({tuple(sorted(key.items())) for key in b_keys}))
        self.assertEqual({"a": 3, "b": 3}, run["attempts"])

    def test_downstream_rerun_cannot_exceed_stage_own_max_attempts(self) -> None:
        self.install_builtin_skills()
        item = pipeline()
        item["entryStage"] = "a"
        item["stages"] = [
            {"id": "a", "skill": EMIT, "maxAttempts": 3, "params": {"value": {"ok": True}}, "gates": ["a-ok"]},
            {"id": "b", "skill": EMIT, "maxAttempts": 1, "params": {"value": {"ok": False}}, "gates": ["b-ok"]},
        ]
        item["gates"] = [
            {"id": "a-ok", "stage": "a", "evaluator": ASSERT, "params": {"path": "$.value.ok", "equals": True}},
            {"id": "b-ok", "stage": "b", "evaluator": ASSERT, "params": {"path": "$.value.ok", "equals": True}},
        ]
        item["edges"] = [
            {"from": {"stage": "a", "gate": "a-ok", "verdict": "pass"}, "to": {"stage": "b"}},
            {"from": {"stage": "a", "gate": "a-ok", "verdict": "fail"}, "to": {"stage": None}},
            {"from": {"stage": "b", "gate": "b-ok", "verdict": "pass"}, "to": {"terminal": "succeeded"}},
            {"from": {"stage": "b", "gate": "b-ok", "verdict": "fail"}, "to": {"stage": "a"}},
        ]
        self.env.write_registry("pipeline", registry(item))

        result = run_pipeline(self.env.home, "fixture", new_session_summary="downstream stage limit", now_ns=36)

        self.assertEqual("attempts_exhausted", result["terminalReason"])
        run = load_session(self.env.home, result["sessionId"])["session"]["pipelineRuns"][result["pipeline"]["runId"]]
        b_keys = [row["stageKey"] for row in run["stageExecutions"] if row["stageKey"]["stageId"] == "b"]
        self.assertEqual([1], [key["attemptNo"] for key in b_keys])
        self.assertEqual({"a": 2, "b": 1}, run["attempts"])

    def test_rollback_invalidates_passed_gate_on_same_stage(self) -> None:
        self.install_builtin_skills()
        item = pipeline()
        item["stages"][0]["maxAttempts"] = 2
        item["stages"][0]["params"]["value"]["ok"] = False
        item["stages"][0]["gates"] = ["assert-false", "assert-ok"]
        item["gates"].insert(0, {
            "id": "assert-false",
            "stage": "emit",
            "evaluator": ASSERT,
            "params": {"path": "$.value.ok", "equals": False},
        })
        item["edges"] = [
            {"from": {"stage": "emit", "gate": "assert-false", "verdict": "pass"}, "to": {"terminal": "succeeded"}},
            {"from": {"stage": "emit", "gate": "assert-false", "verdict": "fail"}, "to": {"stage": None}},
            {"from": {"stage": "emit", "gate": "assert-ok", "verdict": "pass"}, "to": {"terminal": "succeeded"}},
            {"from": {"stage": "emit", "gate": "assert-ok", "verdict": "fail"}, "to": {"stage": "emit"}},
        ]
        self.env.write_registry("pipeline", registry(item))

        result = run_pipeline(self.env.home, "fixture", new_session_summary="multiple gate rollback", now_ns=40)

        loaded = load_session(self.env.home, result["sessionId"])
        run = loaded["session"]["pipelineRuns"][result["pipeline"]["runId"]]
        first_pass = next(row for row in run["gateResults"] if row["gateKey"]["attemptNo"] == 1)
        self.assertEqual("pass", first_pass["verdict"])
        self.assertIsNotNone(first_pass["invalidatedReason"])

    def test_cross_stage_rollback_counts_unexecuted_target_as_zero(self) -> None:
        self.install_builtin_skills()
        item = pipeline()
        first = item["stages"][0]
        first.update(id="first", gates=["first-ok"])
        item["gates"][0].update(id="first-ok", stage="first")
        second = copy.deepcopy(first)
        second.update(id="second", gates=["second-fails"], params={"value": {"ok": False}})
        item["stages"].append(second)
        item["gates"].append({
            "id": "second-fails",
            "stage": "second",
            "evaluator": ASSERT,
            "params": {"path": "$.value.ok", "equals": True},
        })
        item["entryStage"] = "second"
        item["edges"] = [
            {"from": {"stage": "first", "gate": "first-ok", "verdict": "pass"}, "to": {"terminal": "succeeded"}},
            {"from": {"stage": "first", "gate": "first-ok", "verdict": "fail"}, "to": {"stage": None}},
            {"from": {"stage": "second", "gate": "second-fails", "verdict": "pass"}, "to": {"terminal": "succeeded"}},
            {"from": {"stage": "second", "gate": "second-fails", "verdict": "fail"}, "to": {"stage": "first"}},
        ]
        self.env.write_registry("pipeline", registry(item))

        result = run_pipeline(self.env.home, "fixture", new_session_summary="cross stage rollback", now_ns=45)

        self.assertEqual("ok", result["status"], result)
        run = load_session(self.env.home, result["sessionId"])["session"]["pipelineRuns"][result["pipeline"]["runId"]]
        self.assertEqual(1, run["attempts"]["first"])

    def test_rollback_downstream_ignores_earlier_gate_pass_edges(self) -> None:
        self.install_builtin_skills()
        item = pipeline()
        item["entryStage"] = "upstream"
        item["stages"] = [
            {"id": "upstream", "skill": EMIT, "maxAttempts": 1, "params": {"value": {"ok": True}}, "gates": ["upstream-ok"]},
            {"id": "rollback", "skill": EMIT, "maxAttempts": 2, "params": {"value": {"ok": True}}, "gates": ["declarative", "rollback-ok"]},
            {"id": "failing", "skill": EMIT, "maxAttempts": 1, "params": {"value": {"ok": False}}, "gates": ["failing-gate"]},
        ]
        item["gates"] = [
            {"id": "upstream-ok", "stage": "upstream", "evaluator": ASSERT, "params": {"path": "$.value.ok", "equals": True}},
            {"id": "declarative", "stage": "rollback", "evaluator": ASSERT, "params": {"path": "$.value.ok", "equals": True}},
            {"id": "rollback-ok", "stage": "rollback", "evaluator": ASSERT, "params": {"path": "$.value.ok", "equals": True}},
            {"id": "failing-gate", "stage": "failing", "evaluator": ASSERT, "params": {"path": "$.value.ok", "equals": True}},
        ]
        item["edges"] = [
            {"from": {"stage": "upstream", "gate": "upstream-ok", "verdict": "pass"}, "to": {"stage": "rollback"}},
            {"from": {"stage": "upstream", "gate": "upstream-ok", "verdict": "fail"}, "to": {"stage": None}},
            # 前序 gate 的声明性 pass 边指向上游，不应扩张 rollback 的下游集合。
            {"from": {"stage": "rollback", "gate": "declarative", "verdict": "pass"}, "to": {"stage": "upstream"}},
            {"from": {"stage": "rollback", "gate": "declarative", "verdict": "fail"}, "to": {"stage": None}},
            {"from": {"stage": "rollback", "gate": "rollback-ok", "verdict": "pass"}, "to": {"stage": "failing"}},
            {"from": {"stage": "rollback", "gate": "rollback-ok", "verdict": "fail"}, "to": {"stage": None}},
            {"from": {"stage": "failing", "gate": "failing-gate", "verdict": "pass"}, "to": {"terminal": "succeeded"}},
            {"from": {"stage": "failing", "gate": "failing-gate", "verdict": "fail"}, "to": {"stage": "rollback"}},
        ]
        self.env.write_registry("pipeline", registry(item))

        result = run_pipeline(self.env.home, "fixture", new_session_summary="downstream scope", now_ns=46)

        run = load_session(self.env.home, result["sessionId"])["session"]["pipelineRuns"][result["pipeline"]["runId"]]
        upstream_execution = next(row for row in run["stageExecutions"] if row["stageKey"]["stageId"] == "upstream")
        self.assertEqual("completed", upstream_execution["status"])
        self.assertIsNone(upstream_execution["invalidatedReason"])

    def test_terminal_write_failure_keeps_session_and_run_active(self) -> None:
        self.install_builtin_skills()
        self.env.write_registry("pipeline", registry())
        from orchagent import session as session_module

        original_write = session_module._atomic_write_json

        def fail_terminal_write(path: Any, data: dict[str, Any]) -> None:
            if path.name == "session.json" and data.get("status") in {"succeeded", "failed", "cancelled"}:
                raise OSError("injected terminal write failure")
            original_write(path, data)

        with patch("orchagent.session._atomic_write_json", side_effect=fail_terminal_write):
            result = run_pipeline(self.env.home, "fixture", new_session_summary="atomic terminal", now_ns=47)

        self.assertEqual("session_finalize_failed", result["error"])
        persisted = load_session(self.env.home, result["sessionId"])["session"]
        run = persisted["pipelineRuns"][result["pipeline"]["runId"]]
        self.assertEqual("active", persisted["status"])
        self.assertEqual("active", run["status"])
        self.assertTrue(lease_path(self.env.home, result["sessionId"]).exists())

    def test_invalid_or_disabled_skill_has_no_session_side_effect(self) -> None:
        for mode in ("unregistered", "disabled"):
            with self.subTest(mode=mode):
                self.install_builtin_skills(disabled=EMIT if mode == "disabled" else None)
                data = registry()
                if mode == "unregistered":
                    data["pipelines"][0]["stages"][0]["skill"] = "missing.skill"
                self.env.write_registry("pipeline", data)
                before = set((self.env.home / "sessions").iterdir())

                result = run_pipeline(self.env.home, "fixture", new_session_summary="must not create", now_ns=50)

                self.assertEqual("error", result["status"])
                self.assertEqual(before, set((self.env.home / "sessions").iterdir()))

    def test_active_lease_is_not_taken_over(self) -> None:
        self.install_builtin_skills()
        self.env.write_registry("pipeline", registry())
        created = create_session(self.env.home, summary="already leased", task_type="pipeline", now_ns=60)
        session_id = created["session"]["id"]
        owner = acquire_lease(self.env.home, session_id, now_ns=60)

        result = run_pipeline(self.env.home, "fixture", session_id=session_id, now_ns=60)

        self.assertEqual("active_lease", result["error"])
        loaded = load_session(self.env.home, session_id)
        self.assertEqual(owner["lease"]["token"], loaded["session"]["leaseToken"])

    def test_decision_table_six_rows_and_timeout(self) -> None:
        self.assertEqual("gate_rejected", decide_terminal("failure")["terminalReason"])
        self.assertEqual("timeout", decide_terminal("timeout")["terminalReason"])
        self.assertEqual(
            "attempts_exhausted",
            decide_terminal("failure", "emit", attempt_no=2, max_attempts=2)["terminalReason"],
        )
        self.assertEqual(
            {"action": "rollback", "targetStage": "emit"},
            decide_terminal("timeout", "emit", attempt_no=1, max_attempts=2),
        )
        self.assertEqual("succeeded", decide_terminal("succeeded")["terminalReason"])
        self.assertEqual("failed", decide_terminal("internal_error")["terminalReason"])
        self.assertEqual("cancelled", decide_terminal("cancelled")["terminalReason"])

    def test_pipeline_run_cli_and_argument_errors(self) -> None:
        self.install_builtin_skills()
        self.env.write_registry("pipeline", registry())
        code, payload, _, stderr = self.env.run_cli(
            "pipeline", "run", "--pipeline", "fixture", "--new-session-summary", "cli success"
        )
        self.assertEqual(0, code, stderr)
        self.assertEqual("succeeded", payload["terminalReason"])

        code, _, _, _ = self.env.run_cli("pipeline", "run", "--pipeline", "fixture")
        self.assertEqual(2, code)
        code, _, _, _ = self.env.run_cli(
            "pipeline", "run", "--pipeline", "fixture",
            "--new-session-summary", "冲突", "--session-id", "some-session",
        )
        self.assertEqual(2, code)


if __name__ == "__main__":
    unittest.main()
