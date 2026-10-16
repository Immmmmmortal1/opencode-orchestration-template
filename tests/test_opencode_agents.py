from __future__ import annotations

import json
import copy
import unittest
from fnmatch import fnmatchcase
from unittest import mock

from orchagent.install import backup_root
from orchagent.opencode import sync_agents, sync_agents_from_registry
from tests.helpers import IsolatedEnv, PROJECT_ROOT


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

    def write_prompt_registry(self, registry: object) -> None:
        root = self.env.home / "prompts"
        root.mkdir(parents=True, exist_ok=True)
        (root / "prompts.json").write_text(json.dumps(registry), encoding="utf-8")

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

    def test_sync_uses_rendered_prompt_for_targeted_role(self) -> None:
        root = self.env.home / "prompts"
        (root / "templates").mkdir(parents=True, exist_ok=True)
        (root / "overrides").mkdir(exist_ok=True)
        (root / "templates" / "base.md").write_text("Base role guidance", encoding="utf-8")
        (root / "templates" / "fixer.md").write_text("Fixer guidance", encoding="utf-8")
        (root / "overrides" / "fixer.md").write_text("Project override", encoding="utf-8")
        self.write_prompt_registry({
            "version": 1,
            "prompts": [
                {"id": "base", "template": "templates/base.md"},
                {"id": "fixer", "template": "templates/fixer.md", "extends": "base"},
            ],
            "targets": [{
                "kind": "role", "id": "fixer", "prompt": "fixer", "override": "overrides/fixer.md",
            }],
        })

        code, result, _, _ = self.env.run_cli("opencode", "sync-agents")

        self.assertEqual(0, code, result)
        agents = self.read_config()["agent"]
        self.assertEqual(
            "Base role guidance\n\nFixer guidance\n\nProject override",
            agents["orchagent-role-fixer"]["prompt"],
        )
        self.assertIn("当前 stage", agents["orchagent-role-investigator"]["prompt"])

    def test_sync_renders_from_the_loaded_prompt_registry(self) -> None:
        root = self.env.home / "prompts"
        root.mkdir(parents=True, exist_ok=True)
        (root / "base.md").write_text("Base", encoding="utf-8")
        registry = {
            "version": 1,
            "prompts": [{"id": "base", "template": "base.md"}],
            "targets": [{"kind": "role", "id": "fixer", "prompt": "base"}],
        }
        (root / "prompts.json").write_text("{}", encoding="utf-8")

        with mock.patch("orchagent.opencode.primary_config_path", return_value=self.env.opencode_config):
            with mock.patch("orchagent.opencode.load_prompt_registry", return_value=(registry, None)) as load:
                file_contents = {"base.md": "First snapshot"}
                with mock.patch("orchagent.opencode.load_prompt_file_contents", return_value=file_contents) as load_files:
                    with mock.patch(
                        "orchagent.opencode.render_prompt_registry",
                        return_value={"status": "ok", "markdown": "Loaded registry prompt", "sources": ["base.md"]},
                    ) as render:
                        result = sync_agents(self.env.home)

        self.assertEqual("ok", result["status"])
        load.assert_called_once_with(self.env.home)
        load_files.assert_called_once_with(self.env.home, registry)
        self.assertIs(registry, render.call_args.args[1])
        self.assertIs(file_contents, render.call_args.args[4])
        self.assertEqual("Loaded registry prompt", self.read_config()["agent"]["orchagent-role-fixer"]["prompt"])

    def test_sync_core_uses_passed_registry_without_rereading_disk(self) -> None:
        loaded = v2_registry()
        loaded["roles"][1]["description"] = "immutable input"
        disk = v2_registry()
        disk["roles"][1]["description"] = "changed on disk"
        self.env.write_registry("pipeline", disk)

        with (
            mock.patch("orchagent.opencode.primary_config_path", return_value=self.env.opencode_config),
            mock.patch("orchagent.opencode.load_pipeline_registry") as load,
        ):
            result = sync_agents_from_registry(self.env.home, loaded)

        self.assertEqual("ok", result["status"])
        load.assert_not_called()
        agent = self.read_config()["agent"]["orchagent-role-fixer"]
        self.assertIn("immutable input", agent["description"])
        self.assertNotIn("changed on disk", agent["description"])

    def test_invalid_prompt_registry_fails_without_config_or_backup(self) -> None:
        self.write_prompt_registry({"version": 1, "prompts": "invalid", "targets": []})
        before = self.env.opencode_config.read_bytes()
        backups = list(backup_root(self.env.home).glob("opencode-*"))

        code, result, _, _ = self.env.run_cli("opencode", "sync-agents")

        self.assertEqual(1, code)
        self.assertEqual("error", result["status"])
        self.assertEqual("invalid_prompt_registry", result["error"])
        self.assertEqual(before, self.env.opencode_config.read_bytes())
        self.assertEqual(backups, list(backup_root(self.env.home).glob("opencode-*")))

    def test_prompt_registry_unknown_role_target_fails_without_config_or_backup(self) -> None:
        root = self.env.home / "prompts"
        (root / "templates").mkdir(parents=True, exist_ok=True)
        (root / "templates" / "base.md").write_text("Base", encoding="utf-8")
        self.write_prompt_registry({
            "version": 1,
            "prompts": [{"id": "base", "template": "templates/base.md"}],
            "targets": [{"kind": "role", "id": "missing", "prompt": "base"}],
        })
        before = self.env.opencode_config.read_bytes()
        backups = list(backup_root(self.env.home).glob("opencode-*"))

        code, result, _, _ = self.env.run_cli("opencode", "sync-agents")

        self.assertEqual(1, code)
        self.assertEqual("invalid_prompt_registry", result["error"])
        self.assertEqual(before, self.env.opencode_config.read_bytes())
        self.assertEqual(backups, list(backup_root(self.env.home).glob("opencode-*")))

    def test_sync_without_prompt_registry_keeps_default_role_prompt(self) -> None:
        self.assertFalse((self.env.home / "prompts" / "prompts.json").exists())

        code, result, _, _ = self.env.run_cli("opencode", "sync-agents")

        self.assertEqual(0, code, result)
        self.assertEqual(
            "角色职责：fixer，实施修复。\n"
            "只处理主 agent 派发的当前 stage。\n"
            "返回可用于当前门禁判定的结构化摘要。\n"
            "不得自行推进 pipeline、调用 pipeline advance，或派发其他 agent。\n"
            "只按已确认的解决方法实施最小修复，不得扩大范围，不得顺手重构无关代码。\n"
            "必须保证上游「测试流程」在修复后仍可执行；若确需变更测试流程，必须在结论中显式给出更新后的测试流程。",
            self.read_config()["agent"]["orchagent-role-fixer"]["prompt"],
        )

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

    def test_pipeline_prompt_runtime_hint_follows_stage_params(self) -> None:
        # 配置 params.runtime=debugbridge 的 stage：pipeline prompt 含真机验证提示行
        configured = v2_registry()
        configured["pipelines"][0]["stages"][0]["params"] = {"runtime": "debugbridge"}
        self.env.write_registry("pipeline", configured)
        self.assertEqual(0, self.env.run_cli("opencode", "sync-agents")[0])
        prompt = self.read_config()["agent"]["orchagent-bugfix"]["prompt"]
        self.assertIn("此 stage 启用真机验证（runtime=debugbridge）", prompt)

        # 未配置 runtime 的 stage：该提示行不出现
        self.env.write_registry("pipeline", v2_registry())
        self.assertEqual(0, self.env.run_cli("opencode", "sync-agents")[0])
        bare_prompt = self.read_config()["agent"]["orchagent-bugfix"]["prompt"]
        self.assertNotIn("真机验证", bare_prompt)

    # —— 以下用例基于已安装的默认模板（含 reviewer role，deepseek/deepseek-v4-pro）——

    def install_default_template(self) -> None:
        """恢复默认 pipeline 模板（setUp 已用 v2_registry 覆盖，install_home 不会重写已存在文件）。"""
        template = json.loads((PROJECT_ROOT / "templates" / "extensions" / "pipeline.yaml").read_text(encoding="utf-8"))
        self.env.write_registry("pipeline", template)

    def test_sync_generates_reviewer_subagent_from_default_template(self) -> None:
        self.install_default_template()
        code, result, _, _ = self.env.run_cli("opencode", "sync-agents")

        self.assertEqual(0, code, result)
        agent = self.read_config()["agent"]["orchagent-role-reviewer"]
        self.assertEqual("subagent", agent["mode"])
        self.assertEqual("deepseek/deepseek-v4-pro", agent["model"])
        self.assertEqual("orchAgent-managed", agent["marker"])
        self.assertEqual({"task": "deny"}, agent["permission"])

    def test_verifier_prompt_contains_builtin_contract(self) -> None:
        self.install_default_template()
        self.assertEqual(0, self.env.run_cli("opencode", "sync-agents")[0])
        prompt = self.read_config()["agent"]["orchagent-role-verifier"]["prompt"]

        # 新口径锚点：依据上游测试流程、不得自行编造用例（旧锚点「测试用例」已随口径迁移废弃）
        self.assertIn("不得自行编造", prompt)
        self.assertIn("测试流程", prompt)
        self.assertIn("退出码", prompt)
        self.assertIn("runtime=debugbridge", prompt)
        self.assertIn("runtime-blocked", prompt)

    def test_investigator_prompt_contains_builtin_contract(self) -> None:
        self.install_default_template()
        self.assertEqual(0, self.env.run_cli("opencode", "sync-agents")[0])
        prompt = self.read_config()["agent"]["orchagent-role-investigator"]["prompt"]

        # 改代码前五项产出
        self.assertIn("代码位置", prompt)
        self.assertIn("根因", prompt)
        self.assertIn("证据", prompt)
        self.assertIn("解决方法", prompt)
        self.assertIn("测试流程", prompt)
        self.assertIn("五项未齐", prompt)
        # repro 阶段须建立 red-capable 复现证据
        self.assertIn("red-capable", prompt)

    def test_fixer_prompt_contains_builtin_contract(self) -> None:
        self.install_default_template()
        self.assertEqual(0, self.env.run_cli("opencode", "sync-agents")[0])
        prompt = self.read_config()["agent"]["orchagent-role-fixer"]["prompt"]

        self.assertIn("最小修复", prompt)
        self.assertIn("测试流程", prompt)
        # 若变更测试流程须显式给出更新后的测试流程
        self.assertIn("更新后的测试流程", prompt)

    def test_verifier_prompt_follows_upstream_test_flow(self) -> None:
        self.install_default_template()
        self.assertEqual(0, self.env.run_cli("opencode", "sync-agents")[0])
        prompt = self.read_config()["agent"]["orchagent-role-verifier"]["prompt"]

        self.assertIn("上游", prompt)
        self.assertIn("测试流程", prompt)
        # 旧口径已移除：不再要求 verifier 自行输出测试用例
        self.assertNotIn("输出当前问题的测试用例", prompt)
        # 禁止用 UI 测试框架替代 DebugBridge 真机流程
        self.assertIn("XCTest", prompt)
        self.assertIn("XCUITest", prompt)

    def test_reviewer_prompt_contains_builtin_contract(self) -> None:
        self.install_default_template()
        self.assertEqual(0, self.env.run_cli("opencode", "sync-agents")[0])
        prompt = self.read_config()["agent"]["orchagent-role-reviewer"]["prompt"]

        self.assertIn("是否越界", prompt)
        self.assertIn("是否过度设计", prompt)
        self.assertIn("破坏性修复", prompt)
        self.assertIn("证据一致性", prompt)
        # 第四维须锚定上游测试流程，不得再引用已废弃的「verifier 的测试用例」
        self.assertIn("上游产出的测试流程", prompt)
        self.assertNotIn("verifier 的测试用例", prompt)
        self.assertIn("pass / revise / blocked", prompt)
        self.assertIn("reworkTarget=verify", prompt)

    def test_builtin_contracts_do_not_leak_into_other_roles(self) -> None:
        self.install_default_template()
        self.assertEqual(0, self.env.run_cli("opencode", "sync-agents")[0])
        investigator = self.read_config()["agent"]["orchagent-role-investigator"]["prompt"]
        fixer = self.read_config()["agent"]["orchagent-role-fixer"]["prompt"]

        for prompt in (investigator, fixer):
            for exclusive in (
                "测试用例",
                "退出码",
                "runtime-blocked",
                "是否越界",
                "破坏性修复",
                "reworkTarget=verify",
            ):
                self.assertNotIn(exclusive, prompt)


if __name__ == "__main__":
    unittest.main()
