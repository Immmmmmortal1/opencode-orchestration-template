from __future__ import annotations

import json
import os
import unittest
from pathlib import Path

from orchagent.prompts import (
    list_prompt_targets,
    load_prompt_registry,
    prompt_registry_path,
    render_prompt,
    validate_prompt_registry,
)
from tests.helpers import IsolatedEnv


class PromptRegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()
        self.root = self.env.home / "prompts"
        (self.root / "templates").mkdir(parents=True)
        (self.root / "overrides").mkdir()

    def tearDown(self) -> None:
        self.env.tearDown()

    def write_registry(self, registry: object) -> Path:
        path = prompt_registry_path(self.env.home)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(registry), encoding="utf-8")
        return path

    def valid_registry(self) -> dict[str, object]:
        return {
            "version": 1,
            "prompts": [
                {"id": "base", "description": "base", "template": "templates/base.md"},
                {"id": "child", "template": "templates/child.md", "extends": "base"},
            ],
            "targets": [
                {"kind": "role", "id": "worker", "prompt": "child", "override": "overrides/worker.md"}
            ],
        }

    def prepare_valid(self) -> None:
        self.write_registry(self.valid_registry())
        (self.root / "templates/base.md").write_text("Base", encoding="utf-8")
        (self.root / "templates/child.md").write_text("Child", encoding="utf-8")
        (self.root / "overrides/worker.md").write_text("Override", encoding="utf-8")

    def assert_invalid(self, registry: object) -> dict[str, object]:
        result = validate_prompt_registry(self.env.home, registry)
        self.assertEqual("error", result["status"])
        return result

    def test_load_validate_render_inheritance_and_override(self) -> None:
        self.prepare_valid()
        registry, error = load_prompt_registry(self.env.home)
        self.assertIsNone(error)
        self.assertEqual(1, registry["version"])
        self.assertEqual("ok", validate_prompt_registry(self.env.home)["status"])

        result = render_prompt(self.env.home, "child", "overrides/worker.md")

        self.assertEqual("ok", result["status"])
        self.assertEqual("Base\n\nChild\n\nOverride", result["markdown"])
        self.assertEqual(
            ["templates/base.md", "templates/child.md", "overrides/worker.md"],
            result["sources"],
        )

    def test_rejects_unknown_fields_bool_version_and_duplicates(self) -> None:
        cases = []
        unknown = self.valid_registry()
        unknown["extra"] = True
        cases.append(unknown)
        bool_version = self.valid_registry()
        bool_version["version"] = True
        cases.append(bool_version)
        duplicate_prompt = self.valid_registry()
        duplicate_prompt["prompts"] = [
            {"id": "same", "template": "templates/base.md"},
            {"id": "same", "template": "templates/child.md"},
        ]
        cases.append(duplicate_prompt)
        duplicate_target = self.valid_registry()
        duplicate_target["targets"] = duplicate_target["targets"] * 2
        cases.append(duplicate_target)
        for registry in cases:
            with self.subTest(registry=registry):
                self.assert_invalid(registry)

    def test_rejects_missing_template_unknown_parent_and_cycle(self) -> None:
        missing = self.valid_registry()
        missing["prompts"] = [{"id": "base"}]
        self.assert_invalid(missing)
        unknown_parent = self.valid_registry()
        unknown_parent["prompts"] = [{"id": "child", "template": "templates/child.md", "extends": "missing"}]
        unknown_parent["targets"] = []
        self.assert_invalid(unknown_parent)
        cycle = self.valid_registry()
        cycle["prompts"] = [
            {"id": "a", "template": "templates/base.md", "extends": "b"},
            {"id": "b", "template": "templates/child.md", "extends": "a"},
        ]
        cycle["targets"] = []
        self.assert_invalid(cycle)

    def test_rejects_unsafe_paths_and_symlink(self) -> None:
        outside = self.env.external_dir / "outside.md"
        outside.write_text("outside", encoding="utf-8")
        for unsafe in (str(outside), "../outside.md"):
            registry = self.valid_registry()
            registry["prompts"] = [{"id": "base", "template": unsafe}]
            registry["targets"] = []
            self.assert_invalid(registry)

        link = self.root / "templates/link.md"
        os.symlink(outside, link)
        registry = self.valid_registry()
        registry["prompts"] = [{"id": "base", "template": "templates/link.md"}]
        registry["targets"] = []
        self.assert_invalid(registry)

    def test_rejects_sensitive_field_and_content_without_echo(self) -> None:
        marker = "to" + "ken"
        rejected_value = "do-not-repeat-493"
        registry = self.valid_registry()
        registry[marker] = rejected_value
        result = self.assert_invalid(registry)
        self.assertNotIn(rejected_value, json.dumps(result))

        self.prepare_valid()
        rejected_body = marker + "=" + rejected_value
        (self.root / "templates/base.md").write_text(rejected_body, encoding="utf-8")
        result = validate_prompt_registry(self.env.home)
        self.assertEqual("error", result["status"])
        self.assertNotIn(rejected_value, json.dumps(result))

    def test_allows_environment_as_ordinary_markdown_word(self) -> None:
        self.prepare_valid()
        (self.root / "templates/base.md").write_text(
            "Describe the development environment and environment setup.", encoding="utf-8"
        )

        self.assertEqual("ok", validate_prompt_registry(self.env.home)["status"])

    def test_rejects_explicit_api_key_assignment_without_echo(self) -> None:
        self.prepare_valid()
        rejected_value = "do-not-repeat-credential-731"
        (self.root / "templates/base.md").write_text(
            "api_key=" + rejected_value, encoding="utf-8"
        )

        result = validate_prompt_registry(self.env.home)

        self.assertEqual("error", result["status"])
        self.assertEqual("sensitive_data", result["checks"][0]["code"])
        self.assertNotIn(rejected_value, json.dumps(result))

    def test_rejects_common_credential_field_variants_without_echo(self) -> None:
        self.prepare_valid()
        for field in ("access_token", "client_secret", "api_secret", "authorization"):
            with self.subTest(field=field):
                value = f"do-not-repeat-{field}-742"
                registry = self.valid_registry()
                registry[field] = value
                result = self.assert_invalid(registry)
                self.assertNotIn(value, json.dumps(result))

        for assignment in ("access_token", "client_secret", "api_secret", "authorization"):
            with self.subTest(assignment=assignment):
                value = f"do-not-repeat-{assignment}-743"
                (self.root / "templates/base.md").write_text(f"{assignment}={value}", encoding="utf-8")
                result = validate_prompt_registry(self.env.home)
                self.assertEqual("error", result["status"])
                self.assertNotIn(value, json.dumps(result))

    def test_rejects_bare_credential_names_in_markdown(self) -> None:
        self.prepare_valid()
        for name in (
            "api_key",
            "access-token",
            "client_secret",
            "authorization",
            "token",
            "vendor.API_KEY",
            "service.client-secret",
        ):
            with self.subTest(name=name):
                (self.root / "templates/base.md").write_text(f"Document field: {name}", encoding="utf-8")
                result = validate_prompt_registry(self.env.home)
                self.assertEqual("error", result["status"])
                self.assertEqual("sensitive_data", result["checks"][0]["code"])

    def test_lists_role_targets(self) -> None:
        self.prepare_valid()
        result = list_prompt_targets(self.env.home)
        self.assertEqual("ok", result["status"])
        self.assertEqual(
            [{"kind": "role", "id": "worker", "prompt": "child", "override": "overrides/worker.md"}],
            result["targets"],
        )


if __name__ == "__main__":
    unittest.main()
