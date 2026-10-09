from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from orchagent.skills import MAX_FRONTMATTER_BYTES, doctor_skills, list_skills

from tests.helpers import IsolatedEnv


ADAPTER_ID = "test.skills.filesystem"


def adapter(
    root: Any = "skills-root",
    *,
    adapter_id: Any = ADAPTER_ID,
    adapter_type: Any = "filesystem",
    enabled: Any = True,
) -> dict[str, Any]:
    return {
        "id": adapter_id,
        "type": adapter_type,
        "enabled": enabled,
        "root": root,
    }


def skill(
    path: Any = "sample",
    *,
    skill_id: Any = "test-skill",
    adapter_id: Any = ADAPTER_ID,
    enabled: Any = True,
    backend: Any = "builtin",
) -> dict[str, Any]:
    return {
        "id": skill_id,
        "adapter": adapter_id,
        "path": path,
        "enabled": enabled,
        "backend": backend,
    }


def registry(*, adapters: Any = None, skills: Any = None) -> dict[str, Any]:
    return {
        "version": 1,
        "adapters": [adapter()] if adapters is None else adapters,
        "skills": [] if skills is None else skills,
    }


class SkillsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()
        self.env.install_home()

    def tearDown(self) -> None:
        self.env.tearDown()

    def write_registry(self, data: Any) -> None:
        self.env.write_registry("skills", data)

    def assert_registry_error(self, data: Any) -> None:
        """非法 registry 必须让 list 与 doctor 同时结构化失败。"""
        self.write_registry(data)

        listed = list_skills(self.env.home)
        ok, diagnosed = doctor_skills(self.env.home)

        self.assertEqual("error", listed["status"])
        self.assertFalse(ok)
        self.assertTrue(any(check["level"] == "error" for check in listed["checks"]))
        self.assertTrue(any(check["level"] == "error" for check in diagnosed["checks"]))

    def create_skill(self, relative: str = "sample", frontmatter: str | None = None) -> Path:
        directory = self.env.home / "skills-root" / relative
        directory.mkdir(parents=True, exist_ok=True)
        if frontmatter is not None:
            (directory / "SKILL.md").write_text(frontmatter, encoding="utf-8")
        return directory

    def test_default_template_missing_root_is_warning(self) -> None:
        # 让隔离 ORCHAGENT_HOME 与默认模板中的 ~/.orchAgent 对齐。
        self.env.home = self.env.fake_home / ".orchAgent"
        self.env.home.mkdir()
        self.env.install_home()
        missing_root = self.env.home / "skills"

        with patch.dict(os.environ, {"HOME": str(self.env.fake_home)}):
            listed = list_skills(self.env.home)
            ok, diagnosed = doctor_skills(self.env.home)

        self.assertEqual("ok", listed["status"])
        self.assertTrue(ok)
        self.assertEqual("notImplemented", listed["runtime"])
        self.assertEqual("notImplemented", diagnosed["runtime"])
        self.assertTrue(any(check["level"] == "warn" for check in listed["checks"]))
        self.assertFalse(missing_root.exists())

    def test_rejects_invalid_and_duplicate_adapters(self) -> None:
        cases = [
            registry(adapters=[adapter(adapter_id="")]),
            registry(adapters=[adapter(adapter_id=1)]),
            registry(adapters=[adapter(), adapter()]),
            registry(adapters=[adapter(adapter_type="remote")]),
            registry(adapters=[adapter(enabled="true")]),
            registry(adapters=[adapter(root="")]),
        ]
        for data in cases:
            with self.subTest(data=data):
                self.assert_registry_error(data)

    def test_rejects_invalid_skill_entries(self) -> None:
        duplicate = registry(skills=[skill(), skill(path="other")])
        unknown_field = skill()
        unknown_field["bogus"] = True
        cases = [
            registry(skills=[skill(skill_id="")]),
            registry(skills=[skill(skill_id=1)]),
            duplicate,
            registry(skills=[skill(adapter_id="missing")]),
            registry(skills=[skill(path="")]),
            registry(skills=[skill(enabled="true")]),
            registry(skills=[skill(backend=None)]),
            registry(skills=[skill(backend=1)]),
            registry(skills=[skill(backend="unknown")]),
            registry(skills=[skill(backend="agent")]),
            registry(skills=[unknown_field]),
        ]
        for data in cases:
            with self.subTest(data=data):
                self.assert_registry_error(data)

    def test_missing_root_with_enabled_skill_is_error(self) -> None:
        self.assert_registry_error(registry(skills=[skill()]))

    def test_missing_root_with_disabled_skill_is_allowed(self) -> None:
        self.write_registry(registry(skills=[skill(enabled=False)]))

        listed = list_skills(self.env.home)
        ok, _ = doctor_skills(self.env.home)

        self.assertEqual("ok", listed["status"])
        self.assertTrue(ok)
        self.assertEqual("disabled", listed["skills"][0]["status"])

    def test_skill_directory_without_skill_file_is_error_and_not_modified(self) -> None:
        directory = self.create_skill()
        before = sorted(path.relative_to(directory) for path in directory.rglob("*"))
        self.write_registry(registry(skills=[skill()]))

        self.assert_registry_error(registry(skills=[skill()]))

        after = sorted(path.relative_to(directory) for path in directory.rglob("*"))
        self.assertEqual(before, after)

    def test_skill_file_requires_frontmatter(self) -> None:
        self.create_skill(frontmatter="# plain markdown\n")
        self.assert_registry_error(registry(skills=[skill()]))

    def test_frontmatter_requires_name_and_description(self) -> None:
        cases = [
            "---\ndescription: present\n---\n",
            "---\nname: present\n---\n",
        ]
        for content in cases:
            with self.subTest(content=content):
                self.create_skill(frontmatter=content)
                self.assert_registry_error(registry(skills=[skill()]))

    def test_frontmatter_rejects_comment_as_name(self) -> None:
        self.create_skill(frontmatter="---\nname: # comment\ndescription: valid\n---\n")
        self.assert_registry_error(registry(skills=[skill()]))

    def test_frontmatter_rejects_null_name(self) -> None:
        self.create_skill(frontmatter="---\nname: null\ndescription: valid\n---\n")
        self.assert_registry_error(registry(skills=[skill()]))

    def test_frontmatter_rejects_tilde_name(self) -> None:
        self.create_skill(frontmatter="---\nname: ~\ndescription: valid\n---\n")
        self.assert_registry_error(registry(skills=[skill()]))

    def test_frontmatter_rejects_unclosed_quote(self) -> None:
        self.create_skill(frontmatter='---\nname: "unclosed\ndescription: valid\n---\n')
        self.assert_registry_error(registry(skills=[skill()]))

    def test_frontmatter_rejects_inner_unescaped_quote(self) -> None:
        # 值必须只有一对外层引号，内部不得出现未转义的同类引号
        self.create_skill(frontmatter='---\nname: "foo"bar"\ndescription: valid\n---\n')
        self.assert_registry_error(registry(skills=[skill()]))

    def test_frontmatter_rejects_null_with_inline_comment(self) -> None:
        # `null # comment` 按 YAML 注释语义仍是 null
        self.create_skill(frontmatter="---\nname: null # comment\ndescription: valid\n---\n")
        self.assert_registry_error(registry(skills=[skill()]))

    def test_frontmatter_rejects_tilde_with_inline_comment(self) -> None:
        self.create_skill(frontmatter="---\nname: ~ # comment\ndescription: valid\n---\n")
        self.assert_registry_error(registry(skills=[skill()]))

    def test_frontmatter_rejects_escaped_trailing_quote(self) -> None:
        # 末尾的 `\"` 是转义引号，不能同时充当外层闭合引号
        self.create_skill(frontmatter='---\nname: "foo\\"\ndescription: valid\n---\n')
        self.assert_registry_error(registry(skills=[skill()]))

    def test_frontmatter_rejects_empty_quoted_value(self) -> None:
        self.create_skill(frontmatter='---\nname: ""\ndescription: valid\n---\n')
        self.assert_registry_error(registry(skills=[skill()]))

    def test_frontmatter_accepts_quoted_null_as_literal(self) -> None:
        # 加引号后是普通字符串，应保留为合法值
        self.create_skill(frontmatter='---\nname: "null"\ndescription: valid\n---\n')
        self.write_registry(registry(skills=[skill()]))
        listed = list_skills(self.env.home)
        ok, diagnosed = doctor_skills(self.env.home)
        self.assertEqual("ok", listed["status"])
        self.assertTrue(ok, diagnosed)
        names = [row.get("name") for row in listed["skills"]]
        self.assertIn("null", names)

    def test_frontmatter_rejects_block_scalar(self) -> None:
        self.create_skill(frontmatter="---\nname: valid\ndescription: |\n---\n")
        self.assert_registry_error(registry(skills=[skill()]))

    def test_frontmatter_accepts_crlf(self) -> None:
        self.create_skill(frontmatter="---\r\nname: Sample\r\ndescription: Valid\r\n---\r\n")
        self.write_registry(registry(skills=[skill()]))

        listed = list_skills(self.env.home)

        self.assertEqual("ok", listed["status"])
        self.assertEqual("Sample", listed["skills"][0]["name"])
        self.assertEqual("builtin", listed["skills"][0]["backend"])

    def test_frontmatter_rejects_missing_closing_delimiter(self) -> None:
        self.create_skill(frontmatter="---\nname: Sample\ndescription: Valid\n")
        self.assert_registry_error(registry(skills=[skill()]))

    def test_frontmatter_rejects_non_utf8(self) -> None:
        directory = self.create_skill()
        content = b"---\nname: " + bytes([0xFF]) + b"\ndescription: valid\n---\n"
        (directory / "SKILL.md").write_bytes(content)
        self.assert_registry_error(registry(skills=[skill()]))

    def test_frontmatter_rejects_empty_name(self) -> None:
        self.create_skill(frontmatter="---\nname:\ndescription: valid\n---\n")
        self.assert_registry_error(registry(skills=[skill()]))

    def test_valid_skill_succeeds(self) -> None:
        self.create_skill(frontmatter="---\nname: Sample\ndescription: A sample skill\n---\n")
        self.write_registry(registry(skills=[skill()]))

        listed = list_skills(self.env.home)
        ok, diagnosed = doctor_skills(self.env.home)

        self.assertEqual("ok", listed["status"])
        self.assertTrue(ok)
        self.assertEqual("Sample", listed["skills"][0]["name"])
        self.assertTrue(listed["skills"][0]["descriptionPresent"])
        self.assertFalse(any(check["level"] == "error" for check in diagnosed["checks"]))

    def test_large_body_does_not_affect_v1_frontmatter_read(self) -> None:
        self.create_skill(
            frontmatter="---\nname: Sample\ndescription: A sample skill\n---\n"
            + ("x" * (MAX_FRONTMATTER_BYTES * 4))
        )
        self.write_registry(registry(skills=[skill()]))

        listed = list_skills(self.env.home)

        self.assertEqual("ok", listed["status"])
        self.assertEqual("Sample", listed["skills"][0]["name"])

    def test_v1_frontmatter_over_limit_is_structured_error(self) -> None:
        # 用固定字面量（不引用实现常量）撑过 8192 上限，避免测试与实现共用同一常量而互相掩盖。
        extra = "\n".join(f"extra{index}: value" for index in range(600))
        frontmatter = "---\nname: Sample\ndescription: A sample skill\n" + extra + "\n---\nBody\n"
        self.assertGreater(len(frontmatter.encode("utf-8")), 8192)
        self.create_skill(frontmatter=frontmatter)
        self.write_registry(registry(skills=[skill()]))

        listed = list_skills(self.env.home)
        ok, diagnosed = doctor_skills(self.env.home)

        self.assertEqual("error", listed["status"])
        self.assertFalse(ok)
        self.assertTrue(any("exceeds" in item["message"] for item in listed["checks"]))
        self.assertTrue(any("exceeds 8192 bytes" in item["message"] for item in diagnosed["checks"]))

    def test_adapter_root_outside_home_is_rejected(self) -> None:
        self.assert_registry_error(registry(adapters=[adapter(str(self.env.external_dir))]))

    def test_skill_parent_traversal_is_rejected(self) -> None:
        (self.env.home / "skills-root").mkdir()
        self.assert_registry_error(registry(skills=[skill("../../external")]))

    def test_sensitive_skill_path_is_rejected(self) -> None:
        (self.env.home / "skills-root").mkdir()
        self.assert_registry_error(registry(skills=[skill("secrets/x")]))

    def test_intermediate_symlink_is_rejected(self) -> None:
        root = self.env.home / "skills-root"
        real = root / "real" / "sample"
        real.mkdir(parents=True)
        (root / "alias").symlink_to(root / "real", target_is_directory=True)
        self.assert_registry_error(registry(skills=[skill("alias/sample")]))

    def test_registry_root_must_be_object(self) -> None:
        for value in ([], None, "s", 123):
            with self.subTest(value=value):
                self.assert_registry_error(value)

    def test_list_and_doctor_do_not_create_missing_root(self) -> None:
        root = self.env.home / "never-created"
        self.write_registry(registry(adapters=[adapter("never-created")]))

        list_skills(self.env.home)
        doctor_skills(self.env.home)

        self.assertFalse(root.exists())

    def test_description_text_is_not_echoed(self) -> None:
        description = "X" * 500
        self.create_skill(
            frontmatter=f"---\nname: Sample\ndescription: {description}\n---\n"
        )
        self.write_registry(registry(skills=[skill()]))

        listed = list_skills(self.env.home)
        _, diagnosed = doctor_skills(self.env.home)
        output = json.dumps([listed, diagnosed], ensure_ascii=False)

        self.assertNotIn(description, output)


if __name__ == "__main__":
    unittest.main()
