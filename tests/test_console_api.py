from __future__ import annotations

import copy
import json
import re
import unittest
from unittest import mock

from orchagent.console_api import apply_draft, diff_draft, preview_draft, snapshot, validate_draft
from tests.helpers import IsolatedEnv


class ConsoleApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()
        self.env.install_home()
        self.pipeline = json.loads((self.env.home / "extensions/pipeline.yaml").read_text(encoding="utf-8"))

    def tearDown(self) -> None:
        self.env.tearDown()

    def test_snapshot_summarizes_default_pipeline(self) -> None:
        result = snapshot(self.env.home)
        self.assertEqual("ok", result["status"])
        self.assertEqual(str(self.env.home / "extensions/pipeline.yaml"), result["pipeline"]["registry"])
        self.assertEqual("dispatchOnly", result["pipeline"]["runtime"])
        self.assertEqual(2, result["pipeline"]["version"])
        self.assertEqual(self.pipeline, result["pipeline"]["definition"])
        self.assertTrue(result["pipeline"]["roles"])
        definition = result["pipeline"]["definition"]["pipelines"][0]
        self.assertTrue(definition["stages"])
        self.assertTrue(definition["gates"])
        self.assertTrue(definition["edges"])
        self.assertTrue(definition["route"])
        self.assertEqual(64, len(result["snapshotHash"]))

    def test_snapshot_returns_prompt_registry_and_sorted_markdown_files(self) -> None:
        root = self.env.home / "prompts"
        (root / "templates").mkdir(parents=True, exist_ok=True)
        (root / "overrides").mkdir(exist_ok=True)
        template = "# Base prompt\n\nUse the project contract."
        override = "# Role override\n\nKeep the change minimal."
        (root / "templates/base.md").write_text(template, encoding="utf-8")
        (root / "overrides/role.md").write_text(override, encoding="utf-8")
        registry = {
            "version": 1,
            "prompts": [{"id": "base", "template": "templates/base.md"}],
            "targets": [{
                "kind": "role",
                "id": self.pipeline["roles"][0]["id"],
                "prompt": "base",
                "override": "overrides/role.md",
            }],
        }
        (root / "prompts.json").write_text(json.dumps(registry), encoding="utf-8")

        result = snapshot(self.env.home)

        self.assertEqual("ok", result["status"])
        self.assertEqual("ok", result["prompts"]["status"])
        self.assertEqual(registry, result["prompts"]["registry"])
        self.assertEqual([
            {"path": "overrides/role.md", "content": override},
            {"path": "templates/base.md", "content": template},
        ], result["prompts"]["files"])

    def test_snapshot_redacts_rejected_prompt_content(self) -> None:
        root = self.env.home / "prompts"
        rejected_body = "password = example-value-for-detection"
        (root / "base.md").write_text(rejected_body, encoding="utf-8")
        registry = {
            "version": 1,
            "prompts": [{"id": "base", "template": "base.md"}],
            "targets": [{
                "kind": "role",
                "id": self.pipeline["roles"][0]["id"],
                "prompt": "base",
            }],
        }
        (root / "prompts.json").write_text(json.dumps(registry), encoding="utf-8")

        result = snapshot(self.env.home)

        self.assertEqual("ok", result["status"])
        self.assertEqual("error", result["prompts"]["status"])
        self.assertIsNone(result["prompts"]["registry"])
        self.assertEqual([], result["prompts"]["files"])
        self.assertNotIn(rejected_body, json.dumps(result))

    def test_validate_accepts_v2_and_rejects_v1_invalid_and_sensitive(self) -> None:
        self.assertEqual("ok", validate_draft(self.env.home, {"pipeline": self.pipeline})["status"])
        v1 = {"version": 1, "pipelines": []}
        self.assertEqual("pipeline_v1_read_only", validate_draft(self.env.home, {"pipeline": v1})["error"])
        invalid = copy.deepcopy(self.pipeline)
        invalid["pipelines"][0]["entryStage"] = "missing"
        self.assertEqual("invalid_pipeline", validate_draft(self.env.home, {"pipeline": invalid})["error"])
        secret = "do-not-echo-819"
        result = validate_draft(self.env.home, {"pipeline": self.pipeline, ("api" + "_" + "key"): secret})
        self.assertEqual("sensitive_data", result["error"])
        self.assertNotIn(secret, json.dumps(result))

    def test_preview_and_diff_do_not_write(self) -> None:
        path = self.env.home / "extensions/pipeline.yaml"
        before = path.read_bytes()
        self.assertEqual("ok", preview_draft(self.env.home, {"pipeline": self.pipeline})["status"])
        self.assertEqual("ok", diff_draft(self.env.home, {"pipeline": self.pipeline})["status"])
        self.assertEqual(before, path.read_bytes())

    def test_diff_contains_bounded_before_and_after_content(self) -> None:
        # 不依赖默认 registry 体积：改 canonical 序列化中靠前（route 在 pipelines[0] 早期）、
        # 且在 4096 字节预览窗口内的字段，证明 before/after 预览承载真实改动内容。
        marker_before = self.pipeline["pipelines"][0]["route"]["priority"]
        changed = copy.deepcopy(self.pipeline)
        changed["pipelines"][0]["route"]["priority"] = marker_before + 1

        result = diff_draft(self.env.home, {"pipeline": changed})

        self.assertEqual("ok", result["status"])
        change = result["changes"][0]
        self.assertEqual("extensions/pipeline.yaml", change["path"])
        self.assertEqual("replace", change["operation"])
        self.assertEqual(64, len(change["beforeHash"]))
        self.assertEqual(64, len(change["afterHash"]))
        self.assertNotEqual(change["beforeHash"], change["afterHash"])
        before_preview = change["beforePreview"]
        after_preview = change["afterPreview"]
        # 直接从预览文本中提取 priority 值，验证承载的是真实改动内容
        def preview_priority(preview: str) -> int:
            match = re.search(r'"priority":\s*(\d+)', preview)
            self.assertIsNotNone(match, preview)
            return int(match.group(1))
        self.assertIsNotNone(before_preview)
        self.assertIsNotNone(after_preview)
        self.assertEqual(marker_before, preview_priority(before_preview))
        self.assertEqual(marker_before + 1, preview_priority(after_preview))
        self.assertEqual(result["changes"], result["diff"]["changes"])

    def test_markdown_diff_uses_raw_content_and_reports_creation(self) -> None:
        role_id = self.pipeline["roles"][0]["id"]
        registry = {
            "version": 1,
            "prompts": [{"id": "base", "template": "base.md"}],
            "targets": [{"kind": "role", "id": role_id, "prompt": "base"}],
        }
        draft = {"prompts": registry, "files": [{"path": "base.md", "content": "Concrete Markdown body"}]}

        result = diff_draft(self.env.home, draft)

        markdown = next(item for item in result["changes"] if item["path"] == "prompts/base.md")
        self.assertEqual("create", markdown["operation"])
        self.assertIsNone(markdown["beforeHash"])
        self.assertIsNone(markdown["beforePreview"])
        self.assertEqual("Concrete Markdown body", markdown["afterPreview"])

    def test_apply_pipeline_and_reject_stale_snapshot(self) -> None:
        before = snapshot(self.env.home)["snapshotHash"]
        changed = copy.deepcopy(self.pipeline)
        changed["roles"][0]["description"] = "updated"
        draft = {"pipeline": changed}
        diff_id = diff_draft(self.env.home, draft)["diffId"]
        result = apply_draft(self.env.home, draft, before, diff_id)
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["saved"])
        self.assertFalse(result["synced"])
        self.assertEqual("not-requested", result["syncStatus"])
        self.assertTrue(result["backupId"])
        self.assertNotEqual(before, result["snapshotHash"])
        stale_draft = {"pipeline": self.pipeline}
        stale = apply_draft(self.env.home, stale_draft, before, diff_draft(self.env.home, stale_draft)["diffId"])
        self.assertEqual("snapshot_conflict", stale["error"])

    def test_apply_can_sync_opencode_agents_after_save(self) -> None:
        before = snapshot(self.env.home)["snapshotHash"]
        changed = copy.deepcopy(self.pipeline)
        changed["roles"][0]["description"] = "sync success"
        draft = {"pipeline": changed}
        diff_id = diff_draft(self.env.home, draft)["diffId"]

        with mock.patch("orchagent.console_api.sync_agents_from_registry", return_value={"status": "ok"}) as sync:
            result = apply_draft(self.env.home, draft, before, diff_id, True)

        self.assertTrue(result["saved"])
        self.assertTrue(result["synced"])
        self.assertEqual("ok", result["syncStatus"])
        sync.assert_called_once()
        self.assertEqual(changed, sync.call_args.args[1])
        self.assertIsNone(sync.call_args.args[2])
        self.assertIsNone(sync.call_args.args[3])

    def test_apply_sync_passes_saved_prompt_snapshot_to_core(self) -> None:
        role_id = self.pipeline["roles"][0]["id"]
        registry = {
            "version": 1,
            "prompts": [{"id": "base", "template": "base.md"}],
            "targets": [{"kind": "role", "id": role_id, "prompt": "base"}],
        }
        draft = {"prompts": registry, "files": [{"path": "base.md", "content": "Saved prompt"}]}
        before = snapshot(self.env.home)["snapshotHash"]
        diff_id = diff_draft(self.env.home, draft)["diffId"]

        with mock.patch("orchagent.console_api.sync_agents_from_registry", return_value={"status": "ok"}) as sync:
            result = apply_draft(self.env.home, draft, before, diff_id, True)

        self.assertTrue(result["synced"])
        self.assertEqual(registry, sync.call_args.args[2])
        self.assertEqual({"base.md": "Saved prompt"}, sync.call_args.args[3])

    def test_apply_does_not_sync_registry_changed_after_save(self) -> None:
        before = snapshot(self.env.home)["snapshotHash"]
        changed = copy.deepcopy(self.pipeline)
        changed["roles"][0]["description"] = "apply A"
        draft = {"pipeline": changed}
        diff_id = diff_draft(self.env.home, draft)["diffId"]
        real_snapshot = snapshot
        snapshot_calls = 0

        def mutate_after_apply(home):
            nonlocal snapshot_calls
            result = real_snapshot(home)
            snapshot_calls += 1
            if snapshot_calls == 1:
                concurrent = copy.deepcopy(changed)
                concurrent["roles"][0]["description"] = "concurrent B"
                (self.env.home / "extensions/pipeline.yaml").write_text(
                    json.dumps(concurrent), encoding="utf-8"
                )
            return result

        with (
            mock.patch("orchagent.console_api.snapshot", side_effect=mutate_after_apply),
            mock.patch("orchagent.console_api.sync_agents_from_registry", return_value={"status": "ok"}) as sync,
        ):
            result = apply_draft(self.env.home, draft, before, diff_id, True)

        self.assertEqual("ok", result["status"])
        self.assertTrue(result["saved"])
        self.assertFalse(result["synced"])
        self.assertEqual("error", result["syncStatus"])
        self.assertEqual("registry_changed_before_sync", result["syncError"]["code"])
        self.assertEqual(
            "Registry changed before OpenCode agent synchronization",
            result["syncError"]["message"],
        )
        sync.assert_not_called()

    def test_apply_keeps_saved_registry_when_opencode_sync_fails(self) -> None:
        before = snapshot(self.env.home)["snapshotHash"]
        changed = copy.deepcopy(self.pipeline)
        changed["roles"][0]["description"] = "saved before sync failure"
        draft = {"pipeline": changed}
        diff_id = diff_draft(self.env.home, draft)["diffId"]

        with mock.patch(
            "orchagent.console_api.sync_agents_from_registry",
            return_value={"status": "error", "error": "unsafe", "message": "secret marker"},
        ):
            result = apply_draft(self.env.home, draft, before, diff_id, True)

        self.assertEqual("ok", result["status"])
        self.assertTrue(result["saved"])
        self.assertFalse(result["synced"])
        self.assertEqual("error", result["syncStatus"])
        self.assertEqual("opencode_sync_failed", result["syncError"]["code"])
        self.assertNotIn("secret marker", json.dumps(result))
        written = json.loads((self.env.home / "extensions/pipeline.yaml").read_text(encoding="utf-8"))
        self.assertEqual("saved before sync failure", written["roles"][0]["description"])

    def test_apply_reports_unsynced_when_registry_changes_during_sync(self) -> None:
        before = snapshot(self.env.home)["snapshotHash"]
        changed = copy.deepcopy(self.pipeline)
        changed["roles"][0]["description"] = "saved input"
        draft = {"pipeline": changed}
        diff_id = diff_draft(self.env.home, draft)["diffId"]
        from orchagent.opencode import sync_agents_from_registry as real_sync

        def mutate_during_sync(home, pipeline_registry, prompt_registry, prompt_file_contents):
            result = real_sync(home, pipeline_registry, prompt_registry, prompt_file_contents)
            concurrent = copy.deepcopy(changed)
            concurrent["roles"][0]["description"] = "concurrent change"
            (self.env.home / "extensions/pipeline.yaml").write_text(
                json.dumps(concurrent), encoding="utf-8"
            )
            return result

        with mock.patch(
            "orchagent.console_api.sync_agents_from_registry",
            side_effect=mutate_during_sync,
        ):
            result = apply_draft(self.env.home, draft, before, diff_id, True)

        self.assertEqual("ok", result["status"])
        self.assertTrue(result["saved"])
        self.assertFalse(result["synced"])
        self.assertEqual("registry_changed_before_sync", result["syncError"]["code"])

    def test_apply_prompt_registry(self) -> None:
        root = self.env.home / "prompts"
        role_id = self.pipeline["roles"][0]["id"]
        registry = {
            "version": 1,
            "prompts": [{"id": "base", "template": "templates/base.md"}],
            "targets": [{"kind": "role", "id": role_id, "prompt": "base"}],
        }
        old_hash = snapshot(self.env.home)["snapshotHash"]
        draft = {"prompts": registry, "files": [{"path": "templates/base.md", "content": "Base"}]}
        diff_id = diff_draft(self.env.home, draft)["diffId"]
        result = apply_draft(self.env.home, draft, old_hash, diff_id)
        self.assertEqual("ok", result["status"])
        written = json.loads((root / "prompts.json").read_text(encoding="utf-8"))
        self.assertEqual(registry, written)
        self.assertEqual("Base", (root / "templates/base.md").read_text(encoding="utf-8"))
        manifest = json.loads((self.env.root / f"home.backups/{result['backupId']}/manifest.json").read_text())
        self.assertEqual({"prompts/prompts.json", "prompts/templates/base.md"}, {item["path"] for item in manifest["files"]})

    def test_preview_uses_target_override_and_unsaved_files(self) -> None:
        (self.env.home / "prompts/templates").mkdir(parents=True, exist_ok=True)
        (self.env.home / "prompts/overrides").mkdir(exist_ok=True)
        role_id = self.pipeline["roles"][0]["id"]
        registry = {
            "version": 1,
            "prompts": [{"id": "base", "template": "templates/new.md"}],
            "targets": [{"kind": "role", "id": role_id, "prompt": "base", "override": "overrides/new.md"}],
        }
        draft = {"prompts": registry, "files": [
            {"path": "templates/new.md", "content": "Draft"},
            {"path": "overrides/new.md", "content": "Override"},
        ]}
        result = preview_draft(self.env.home, draft)
        self.assertEqual("Draft\n\nOverride", result["prompts"][0]["markdown"])

    def test_rejects_unknown_target_role(self) -> None:
        root = self.env.home / "prompts"
        (root / "base.md").write_text("Base", encoding="utf-8")
        registry = {"version": 1, "prompts": [{"id": "base", "template": "base.md"}],
                    "targets": [{"kind": "role", "id": "missing", "prompt": "base"}]}
        self.assertEqual("unknown_role", validate_draft(self.env.home, {"prompts": registry})["error"])

    def test_markdown_change_conflicts_with_old_snapshot_and_wrong_diff_is_rejected(self) -> None:
        root = self.env.home / "prompts"
        (root / "base.md").write_text("Base", encoding="utf-8")
        role_id = self.pipeline["roles"][0]["id"]
        registry = {"version": 1, "prompts": [{"id": "base", "template": "base.md"}],
                    "targets": [{"kind": "role", "id": role_id, "prompt": "base"}]}
        (root / "prompts.json").write_text(json.dumps(registry), encoding="utf-8")
        old_hash = snapshot(self.env.home)["snapshotHash"]
        draft = {"pipeline": self.pipeline}
        diff_id = diff_draft(self.env.home, draft)["diffId"]
        self.assertEqual("diff_mismatch", apply_draft(self.env.home, draft, old_hash, "wrong")["error"])
        (root / "base.md").write_text("Changed", encoding="utf-8")
        self.assertEqual("snapshot_conflict", apply_draft(self.env.home, draft, old_hash, diff_id)["error"])

    def test_new_markdown_file_created_after_diff_changes_diff_id(self) -> None:
        root = self.env.home / "prompts"
        (root / "templates").mkdir(parents=True, exist_ok=True)
        role_id = self.pipeline["roles"][0]["id"]
        registry = {
            "version": 1,
            "prompts": [{"id": "base", "template": "templates/new.md"}],
            "targets": [{"kind": "role", "id": role_id, "prompt": "base"}],
        }
        draft = {"prompts": registry, "files": [{"path": "templates/new.md", "content": "Draft"}]}
        old_hash = snapshot(self.env.home)["snapshotHash"]
        diff_id = diff_draft(self.env.home, draft)["diffId"]

        (root / "templates/new.md").write_text("External", encoding="utf-8")

        self.assertEqual("diff_mismatch", apply_draft(self.env.home, draft, old_hash, diff_id)["error"])

    def test_newly_referenced_existing_markdown_change_blocks_all_writes(self) -> None:
        root = self.env.home / "prompts"
        markdown = root / "existing.md"
        markdown.write_text("Original", encoding="utf-8")
        role_id = self.pipeline["roles"][0]["id"]
        registry = {
            "version": 1,
            "prompts": [{"id": "base", "template": "existing.md"}],
            "targets": [{"kind": "role", "id": role_id, "prompt": "base"}],
        }
        changed_pipeline = copy.deepcopy(self.pipeline)
        changed_pipeline["roles"][0]["description"] = "must not be written"
        draft = {"pipeline": changed_pipeline, "prompts": registry}
        snapshot_hash = snapshot(self.env.home)["snapshotHash"]
        diff_id = diff_draft(self.env.home, draft)["diffId"]
        pipeline_before = (self.env.home / "extensions/pipeline.yaml").read_bytes()

        markdown.write_text("Changed outside", encoding="utf-8")
        result = apply_draft(self.env.home, draft, snapshot_hash, diff_id)

        self.assertEqual("diff_mismatch", result["error"])
        self.assertEqual(pipeline_before, (self.env.home / "extensions/pipeline.yaml").read_bytes())
        self.assertFalse((root / "prompts.json").exists())

    def test_post_apply_snapshot_failure_rolls_back_written_files(self) -> None:
        path = self.env.home / "extensions/pipeline.yaml"
        before = path.read_bytes()
        changed = copy.deepcopy(self.pipeline)
        changed["roles"][0]["description"] = "must be rolled back"
        draft = {"pipeline": changed}
        snapshot_hash = snapshot(self.env.home)["snapshotHash"]
        diff_id = diff_draft(self.env.home, draft)["diffId"]

        with mock.patch("orchagent.console_api.snapshot", return_value={"status": "error", "error": "injected"}):
            result = apply_draft(self.env.home, draft, snapshot_hash, diff_id)

        self.assertEqual("post_apply_snapshot_failed", result["error"])
        self.assertTrue(result["restored"])
        self.assertEqual("ok", result["rollback"]["status"])
        self.assertEqual(before, path.read_bytes())

    def test_pipeline_only_rejects_removing_role_used_by_existing_prompts(self) -> None:
        root = self.env.home / "prompts"
        (root / "base.md").write_text("Base", encoding="utf-8")
        role_id = self.pipeline["roles"][0]["id"]
        registry = {
            "version": 1,
            "prompts": [{"id": "base", "template": "base.md"}],
            "targets": [{"kind": "role", "id": role_id, "prompt": "base"}],
        }
        (root / "prompts.json").write_text(json.dumps(registry), encoding="utf-8")
        changed = copy.deepcopy(self.pipeline)
        changed["roles"] = [role for role in changed["roles"] if role["id"] != role_id]
        changed["pipelines"] = []

        self.assertEqual("unknown_role", validate_draft(self.env.home, {"pipeline": changed})["error"])

    def test_files_only_requires_registry_reference(self) -> None:
        root = self.env.home / "prompts"
        (root / "base.md").write_text("Base", encoding="utf-8")
        role_id = self.pipeline["roles"][0]["id"]
        registry = {
            "version": 1,
            "prompts": [{"id": "base", "template": "base.md"}],
            "targets": [{"kind": "role", "id": role_id, "prompt": "base"}],
        }
        (root / "prompts.json").write_text(json.dumps(registry), encoding="utf-8")

        allowed = validate_draft(self.env.home, {"files": [{"path": "base.md", "content": "Changed"}]})
        rejected = validate_draft(self.env.home, {"files": [{"path": "extra.md", "content": "Extra"}]})
        self.assertEqual("ok", allowed["status"])
        self.assertEqual("unreferenced_file", rejected["error"])

    def test_files_only_preview_renders_existing_registry_with_draft_content(self) -> None:
        root = self.env.home / "prompts"
        (root / "base.md").write_text("Original", encoding="utf-8")
        role_id = self.pipeline["roles"][0]["id"]
        registry = {
            "version": 1,
            "prompts": [{"id": "base", "template": "base.md"}],
            "targets": [{"kind": "role", "id": role_id, "prompt": "base"}],
        }
        (root / "prompts.json").write_text(json.dumps(registry), encoding="utf-8")

        result = preview_draft(self.env.home, {"files": [{"path": "base.md", "content": "Draft update"}]})

        self.assertEqual("ok", result["status"])
        self.assertEqual("Draft update", result["prompts"][0]["markdown"])

    def test_files_only_rejects_missing_prompt_registry(self) -> None:
        root = self.env.home / "prompts"
        (root / "extra.md").write_text("Extra", encoding="utf-8")
        result = validate_draft(self.env.home, {"files": [{"path": "extra.md", "content": "Changed"}]})
        self.assertEqual("prompt_registry_required", result["error"])

    def test_default_install_can_create_first_prompt_registry(self) -> None:
        role_id = self.pipeline["roles"][0]["id"]
        registry = {
            "version": 1,
            "prompts": [{"id": "base", "template": "templates/base.md"}],
            "targets": [{"kind": "role", "id": role_id, "prompt": "base"}],
        }
        old_hash = snapshot(self.env.home)["snapshotHash"]
        draft = {"prompts": registry, "files": [{"path": "templates/base.md", "content": "Base"}]}
        diff_id = diff_draft(self.env.home, draft)["diffId"]

        result = apply_draft(self.env.home, draft, old_hash, diff_id)

        self.assertEqual("ok", result["status"])
        self.assertEqual("Base", (self.env.home / "prompts/templates/base.md").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
