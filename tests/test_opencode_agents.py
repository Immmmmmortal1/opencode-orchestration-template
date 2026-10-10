from __future__ import annotations

import json
import copy
import unittest

from orchagent.install import backup_root
from tests.helpers import IsolatedEnv


def v2_registry() -> dict[str, object]:
    return {
        "version": 2,
        "roles": [{"id": "fixer", "provider": "glm", "model": "glm-5.2"}],
        "pipelines": [{
            "id": "bugfix", "revision": "1", "enabled": True,
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
        self.assertIn("pipeline advance", agent["prompt"])
        self.assertTrue(list(backup_root(self.env.home).glob("opencode-*")))

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

    def test_v1_is_unsupported(self) -> None:
        self.env.write_registry("pipeline", {"version": 1, "pipelines": []})
        code, result, _, _ = self.env.run_cli("opencode", "sync-agents")
        self.assertEqual(1, code)
        self.assertEqual("unsupported", result["error"])

    def test_agent_has_no_sensitive_key_names(self) -> None:
        self.env.run_cli("opencode", "sync-agents")
        agent = self.read_config()["agent"]["orchagent-bugfix"]
        self.assertFalse(any(key.lower() in {"key", "token", "secret", "api_key"} for key in agent))

    def test_unlink_agents_only_removes_managed_agents(self) -> None:
        self.env.run_cli("opencode", "sync-agents")
        data = self.read_config()
        data["agent"]["custom"] = {"description": "keep"}
        self.env.opencode_config.write_text(json.dumps(data), encoding="utf-8")
        code, _, _, _ = self.env.run_cli("opencode", "unlink", "--agents")
        self.assertEqual(0, code)
        agents = self.read_config()["agent"]
        self.assertNotIn("orchagent-bugfix", agents)
        self.assertIn("custom", agents)


if __name__ == "__main__":
    unittest.main()
