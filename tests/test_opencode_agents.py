from __future__ import annotations

import json
import copy
import unittest
from fnmatch import fnmatchcase

from orchagent.install import backup_root
from tests.helpers import IsolatedEnv


def v2_registry() -> dict[str, object]:
    return {
        "version": 2,
        "roles": [
            {"id": "investigator", "provider": "deepseek", "model": "deepseek-v4-flash", "description": "定位根因"},
            {"id": "fixer", "provider": "glm", "model": "glm-5.2", "description": "实施修复"},
            {"id": "verifier", "provider": "openai", "model": "gpt-5.5", "description": "验证结果"},
        ],
        "pipelines": [{
            "id": "bugfix", "revision": "1", "enabled": True,
            "route": {"taskTypes": ["bug"], "priority": 10},
            "entryStage": "fix", "stages": [{"id": "fix", "role": "fixer", "maxAttempts": 1, "params": {}, "gates": ["gate-fix"]}],
            "gates": [{"id": "gate-fix", "stage": "fix", "evaluator": "gate-fix", "params": {}}],
            "edges": [
                {"from": {"stage": "fix", "gate": "gate-fix", "verdict": "pass"}, "to": {"terminal": "succeeded"}},
                {"from": {"stage": "fix", "gate": "gate-fix", "verdict": "fail"}, "to": {"stage": "fix"}},
            ],
        }],
    }


class OpenCodeAgentTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()
        self.env.install_home()
        self.env.write_registry("pipeline", v2_registry())

    def tearDown(self) -> None:
        self.env.tearDown()

    def read_config(self) -> dict[str, object]:
        return json.loads(self.env.opencode_config.read_text(encoding="utf-8"))

    def test_sync_generates_managed_agent_and_backup(self) -> None:
        code, result, _, _ = self.env.run_cli("opencode", "sync-agents")
        self.assertEqual(0, code)
        self.assertEqual("ok", result["status"])
        agent = self.read_config()["agent"]["orchagent-bugfix"]
        self.assertEqual("orchAgent-managed", agent["marker"])
        self.assertIn("fix → role=fixer → glm/glm-5.2", agent["prompt"])
        direct_run_command = "orchagent pipeline direct-run --pipeline bugfix --pipeline-revision 1 --task-type bug"
        self.assertIn(f"开始：{direct_run_command}", agent["prompt"])
        self.assertNotIn("summary", agent["prompt"])
        self.assertIn("orchagent-role-<role-id>", agent["prompt"])
        self.assertIn("pipeline advance", agent["prompt"])
        self.assertNotIn("调用 Jev", agent["prompt"].replace("不得调用 Jev", ""))
        permission = agent["permission"]
        self.assertEqual(["*", direct_run_command, "orchagent pipeline advance *"], list(permission["bash"]))
        self.assertNotIn("orchagent pipeline run *", permission["bash"])
        self.assertFalse(any(character in direct_run_command for character in "*?["))
        self.assertTrue(fnmatchcase(direct_run_command, direct_run_command))
        for rejected in (
            "orchagent pipeline direct-run --pipeline review --pipeline-revision 1 --task-type bug",
            "orchagent pipeline direct-run --pipeline bugfix --pipeline-revision 2 --task-type bug",
            direct_run_command + " --pipeline bugfix",
            direct_run_command + " --pipeline-revision 1",
            direct_run_command + "; command",
            direct_run_command + " ",
        ):
            self.assertFalse(fnmatchcase(rejected, direct_run_command))
        self.assertEqual({"*": "deny", "orchagent-role-fixer": "allow"}, permission["task"])
        self.assertTrue(list(backup_root(self.env.home).glob("opencode-*")))

    def test_pipeline_prompt_omits_task_type_for_multi_type_route(self) -> None:
        registry = v2_registry()
        registry["pipelines"][0]["route"]["taskTypes"] = ["bug", "feature"]
        self.env.write_registry("pipeline", registry)

        self.assertEqual(0, self.env.run_cli("opencode", "sync-agents")[0])
        agent = self.read_config()["agent"]["orchagent-bugfix"]
        prompt = agent["prompt"]

        direct_run_command = "orchagent pipeline direct-run --pipeline bugfix --pipeline-revision 1"
        run_line = next(line for line in prompt.splitlines() if line.startswith("开始："))
        self.assertNotIn("<explicit-task-type>", prompt)
        self.assertNotIn("--task-type", run_line)
        self.assertEqual(f"开始：{direct_run_command}", run_line)
        self.assertNotIn("summary", run_line)
        self.assertIn(direct_run_command, agent["permission"]["bash"])
        self.assertFalse(any(character in direct_run_command for character in "*?["))
        self.assertNotIn("--task-type", direct_run_command)
        self.assertNotIn("orchagent pipeline run *", agent["permission"]["bash"])

    def test_sync_generates_all_role_subagents_with_minimum_permissions(self) -> None:
        self.assertEqual(0, self.env.run_cli("opencode", "sync-agents")[0])
        agents = self.read_config()["agent"]
        for role_id, model in (
            ("investigator", "deepseek/deepseek-v4-flash"),
            ("fixer", "glm/glm-5.2"),
            ("verifier", "openai/gpt-5.5"),
        ):
            agent = agents[f"orchagent-role-{role_id}"]
            self.assertEqual("subagent", agent["mode"])
            self.assertEqual(model, agent["model"])
            self.assertEqual("orchAgent-managed", agent["marker"])
            self.assertEqual({"task": "deny"}, agent["permission"])
            self.assertIn("当前 stage", agent["prompt"])
            self.assertIn("结构化摘要", agent["prompt"])
            self.assertIn("不得自行推进", agent["prompt"])

    def test_sync_is_idempotent_without_backup(self) -> None:
        self.env.run_cli("opencode", "sync-agents")
        before = self.read_config()
        backups = list(backup_root(self.env.home).glob("opencode-*"))
        code, result, _, _ = self.env.run_cli("opencode", "sync-agents")
        self.assertEqual(0, code)
        self.assertEqual([], result["changed"])
        self.assertEqual(before, self.read_config())
        self.assertEqual(backups, list(backup_root(self.env.home).glob("opencode-*")))

    def test_unmanaged_conflict_is_rejected_without_modification(self) -> None:
        self.env.opencode_config.write_text(json.dumps({"agent": {"orchagent-bugfix": {"description": "keep"}}}), encoding="utf-8")
        before = self.env.opencode_config.read_bytes()
        code, result, _, _ = self.env.run_cli("opencode", "sync-agents")
        self.assertEqual(1, code)
        self.assertEqual("unmanaged_conflict", result["error"])
        self.assertEqual(before, self.env.opencode_config.read_bytes())

    def test_unmanaged_role_conflict_is_rejected_without_modification(self) -> None:
        self.env.opencode_config.write_text(json.dumps({"agent": {"orchagent-role-fixer": {"description": "keep"}}}), encoding="utf-8")
        before = self.env.opencode_config.read_bytes()
        code, result, _, _ = self.env.run_cli("opencode", "sync-agents")
        self.assertEqual(1, code)
        self.assertEqual("unmanaged_conflict", result["error"])
        self.assertEqual(before, self.env.opencode_config.read_bytes())

    def test_derived_role_pipeline_agent_id_conflict_is_rejected_without_side_effects(self) -> None:
        registry = v2_registry()
        registry["pipelines"][0]["id"] = "role-fixer"
        self.env.write_registry("pipeline", registry)
        before = self.env.opencode_config.read_bytes()
        backups = list(backup_root(self.env.home).glob("opencode-*"))

        code, result, _, _ = self.env.run_cli("opencode", "sync-agents")

        self.assertEqual(1, code)
        self.assertEqual("agent_id_conflict", result["error"])
        self.assertEqual("orchagent-role-fixer", result["agentId"])
        self.assertEqual(
            [{"kind": "role", "id": "fixer"}, {"kind": "pipeline", "id": "role-fixer"}],
            result["sources"],
        )
        self.assertEqual(before, self.env.opencode_config.read_bytes())
        self.assertEqual(backups, list(backup_root(self.env.home).glob("opencode-*")))

    def test_sync_removes_disabled_managed_agent_and_preserves_other_and_unmanaged(self) -> None:
        registry = v2_registry()
        second = copy.deepcopy(registry["pipelines"][0])
        second["id"] = "review"
        registry["pipelines"].append(second)
        self.env.write_registry("pipeline", registry)
        self.assertEqual(0, self.env.run_cli("opencode", "sync-agents")[0])

        data = self.read_config()
        data["agent"]["orchagent-custom"] = {"description": "keep", "marker": "other"}
        registry["pipelines"][1]["enabled"] = False
        self.env.write_registry("pipeline", registry)
        self.env.opencode_config.write_text(json.dumps(data), encoding="utf-8")

        code, result, _, _ = self.env.run_cli("opencode", "sync-agents")

        self.assertEqual(0, code, result)
        agents = self.read_config()["agent"]
        self.assertNotIn("orchagent-review", agents)
        self.assertIn("orchagent-bugfix", agents)
        self.assertEqual({"description": "keep", "marker": "other"}, agents["orchagent-custom"])

    def test_sync_removes_deleted_managed_role_and_preserves_unmanaged(self) -> None:
        self.assertEqual(0, self.env.run_cli("opencode", "sync-agents")[0])
        data = self.read_config()
        data["agent"]["ordinary"] = {"description": "keep"}
        self.env.opencode_config.write_text(json.dumps(data), encoding="utf-8")
        registry = v2_registry()
        registry["roles"] = [role for role in registry["roles"] if role["id"] != "verifier"]
        self.env.write_registry("pipeline", registry)
        self.assertEqual(0, self.env.run_cli("opencode", "sync-agents")[0])
        agents = self.read_config()["agent"]
        self.assertNotIn("orchagent-role-verifier", agents)
        self.assertIn("ordinary", agents)

    def test_v1_is_unsupported(self) -> None:
        self.env.write_registry("pipeline", {"version": 1, "pipelines": []})
        code, result, _, _ = self.env.run_cli("opencode", "sync-agents")
        self.assertEqual(1, code)
        self.assertEqual("unsupported", result["error"])

    def test_agent_has_no_sensitive_key_names(self) -> None:
        self.env.run_cli("opencode", "sync-agents")
        agents = self.read_config()["agent"]
        serialized = json.dumps(agents).lower()
        for sensitive in ('"key"', '"token"', '"secret"', '"api_key"', '"env"'):
            self.assertNotIn(sensitive, serialized)

    def test_unlink_agents_only_removes_managed_agents(self) -> None:
        self.env.run_cli("opencode", "sync-agents")
        data = self.read_config()
        data["agent"]["custom"] = {"description": "keep"}
        self.env.opencode_config.write_text(json.dumps(data), encoding="utf-8")
        code, _, _, _ = self.env.run_cli("opencode", "unlink", "--agents")
        self.assertEqual(0, code)
        agents = self.read_config()["agent"]
        self.assertNotIn("orchagent-bugfix", agents)
        self.assertNotIn("orchagent-role-fixer", agents)
        self.assertIn("custom", agents)


if __name__ == "__main__":
    unittest.main()
