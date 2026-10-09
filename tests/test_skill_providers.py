from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest.mock import patch

from orchagent.builtin_skills import BUILTIN_SKILL_IMPLEMENTATIONS
from orchagent.skill_providers import (
    BuiltinSkillProvider,
    CodexHostSkillProvider,
    FilesystemSkillProvider,
    MAX_CONTENT_BYTES,
    MAX_FRONTMATTER_BYTES,
    OpencodeHostSkillProvider,
)
from orchagent.skill_seam import SkillRegistry

from tests.helpers import IsolatedEnv


def skill_content(
    name: str,
    *,
    description: str = "测试 skill",
    extra: str = "",
) -> str:
    return f"---\nname: {name}\ndescription: {description}\n{extra}---\n# {name}\n"


class SkillProvidersTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()
        self.base = self.env.home
        self.root = self.base / "skills"
        self.root.mkdir()

    def tearDown(self) -> None:
        self.env.tearDown()

    def provider(
        self,
        *,
        provider_id: str = "test.filesystem",
        rank: int = 100,
        roots: tuple[Path, ...] | None = None,
        allowed_bases: tuple[Path, ...] | None = None,
    ) -> FilesystemSkillProvider:
        return FilesystemSkillProvider(
            provider_id=provider_id,
            rank=rank,
            roots=roots if roots is not None else (self.root,),
            allowed_bases=allowed_bases if allowed_bases is not None else (self.base,),
        )

    def write_package(self, name: str, content: str | None = None) -> Path:
        directory = self.root / name
        directory.mkdir(parents=True)
        path = directory / "SKILL.md"
        path.write_text(content or skill_content(name), encoding="utf-8")
        return path

    def test_get_definition_rejects_locator_outside_its_allowed_base(self) -> None:
        """locator 的 allowed_base 合法但路径越界时必须拒绝（_read_safe 边界检查）。"""
        outside = self.base.parent / "outside-skill.md"
        outside.write_text(skill_content("outside-skill"), encoding="utf-8")
        provider = self.provider()
        # 手工构造被篡改的候选：allowed_base 在允许集合内，但路径在它之外。
        from orchagent.skill_seam import SkillCandidate, SkillInvocationPolicy

        forged = SkillCandidate(
            name="outside-skill",
            description="伪造候选",
            provider_id=provider.provider_id,
            rank=provider.rank,
            source=provider.source,
            invocation=SkillInvocationPolicy(),
            locator=(str(outside), str(self.base)),
        )
        self.assertIsNone(provider.get_definition(forged))

    def test_root_that_is_a_symlink_is_rejected(self) -> None:
        """root 自身是 symlink 时必须拒绝，不得跟随（_authorize_root 检查）。"""
        real = self.base / "real"
        real.mkdir()
        (real / "linked-skill").mkdir()
        (real / "linked-skill" / "SKILL.md").write_text(
            skill_content("linked-skill"), encoding="utf-8"
        )
        link_root = self.base / "linked-root"
        link_root.symlink_to(real, target_is_directory=True)

        provider = self.provider(roots=(link_root,))
        observation = provider.list_candidates()
        self.assertEqual((), observation.candidates)
        self.assertFalse(observation.complete)
        self.assertTrue(observation.warnings)

    def test_allowed_base_symlink_is_rejected(self) -> None:
        real = self.base / "real-allowed"
        real.mkdir()
        (real / "escaped.md").write_text(skill_content("escaped"), encoding="utf-8")
        linked = self.base / "linked-allowed"
        linked.symlink_to(real, target_is_directory=True)

        observation = self.provider(roots=(linked,), allowed_bases=(linked,)).list_candidates()

        self.assertEqual((), observation.candidates)
        self.assertFalse(observation.complete)
        self.assertTrue(any("allowed base is a symlink" in item for item in observation.warnings))

    def test_allowed_base_parent_symlink_is_rejected(self) -> None:
        outside = self.base / "outside"
        outside_skills = outside / "skills"
        outside_skills.mkdir(parents=True)
        (outside_skills / "escaped.md").write_text(skill_content("escaped"), encoding="utf-8")
        authorized = self.base / "authorized"
        authorized.mkdir()
        link = authorized / "link"
        link.symlink_to(outside, target_is_directory=True)
        declared_base = link / "skills"

        observation = self.provider(
            roots=(declared_base,),
            allowed_bases=(declared_base,),
        ).list_candidates()

        self.assertEqual((), observation.candidates)
        self.assertFalse(observation.complete)
        self.assertTrue(any("symlink" in item for item in observation.warnings))

    def test_host_explicit_symlink_root_is_rejected(self) -> None:
        real = self.base / "real-host"
        real.mkdir()
        (real / "escaped.md").write_text(skill_content("escaped"), encoding="utf-8")
        linked = self.base / "linked-host"
        linked.symlink_to(real, target_is_directory=True)
        provider = OpencodeHostSkillProvider(
            rank=80,
            roots=(linked,),
            allowed_bases=(linked,),
        )

        observation = provider.list_candidates()

        self.assertEqual((), observation.candidates)
        self.assertFalse(observation.complete)
        self.assertTrue(any("allowed base is a symlink" in item for item in observation.warnings))

    def test_host_allowed_base_parent_symlink_is_rejected(self) -> None:
        outside = self.base / "outside-host"
        outside_skills = outside / "skills"
        outside_skills.mkdir(parents=True)
        (outside_skills / "escaped.md").write_text(skill_content("escaped"), encoding="utf-8")
        authorized = self.base / "authorized-host"
        authorized.mkdir()
        link = authorized / "link"
        link.symlink_to(outside, target_is_directory=True)
        declared_base = link / "skills"
        provider = OpencodeHostSkillProvider(
            rank=80,
            roots=(declared_base,),
            allowed_bases=(declared_base,),
        )

        observation = provider.list_candidates()

        self.assertEqual((), observation.candidates)
        self.assertFalse(observation.complete)
        self.assertTrue(any("symlink" in item for item in observation.warnings))

    def write_flat(self, name: str, content: str | None = None) -> Path:
        path = self.root / f"{name}.md"
        path.write_text(content or skill_content(name), encoding="utf-8")
        return path

    def test_builtin_catalog_and_definition_use_dot_name_pattern(self) -> None:
        provider = BuiltinSkillProvider()
        observation = provider.list_candidates()
        names = {candidate.name for candidate in observation.candidates}
        self.assertIn("orchagent.pipeline.emit-json", names)
        self.assertIn("orchagent.pipeline.assert-json-path-equals", names)

        registry = SkillRegistry()
        registry.register(provider)
        snapshot = registry.snapshot()

        self.assertTrue(snapshot.complete, snapshot.warnings)
        definition = registry.get("orchagent.pipeline.emit-json")
        self.assertIsNotNone(definition)
        assert definition is not None
        self.assertEqual("orchagent.pipeline.emit-json", definition.candidate.name)
        self.assertEqual("builtin:orchagent.pipeline.emit-json", definition.content)

    def test_builtin_revision_is_non_empty_and_stable(self) -> None:
        provider = BuiltinSkillProvider()

        first = provider.list_candidates().revision
        second = provider.list_candidates().revision

        self.assertTrue(first)
        self.assertEqual(first, second)

    def test_builtin_get_rejects_candidate_name_different_from_locator(self) -> None:
        from orchagent.skill_seam import SkillCandidate, SkillInvocationPolicy

        provider = BuiltinSkillProvider()
        forged = SkillCandidate(
            name="other",
            description="伪造候选",
            provider_id=provider.provider_id,
            rank=provider.rank,
            source=provider.source,
            invocation=SkillInvocationPolicy(),
            locator="orchagent.pipeline.emit-json",
        )

        self.assertIsNone(provider.get_definition(forged))

    def test_filesystem_discovers_directory_package(self) -> None:
        self.write_package("sample-skill")

        observation = self.provider().list_candidates()

        self.assertTrue(observation.complete, observation.warnings)
        self.assertEqual(["sample-skill"], [item.name for item in observation.candidates])

    def test_filesystem_discovers_flat_markdown(self) -> None:
        self.write_flat("flat-skill")

        observation = self.provider().list_candidates()

        self.assertTrue(observation.complete, observation.warnings)
        self.assertEqual(["flat-skill"], [item.name for item in observation.candidates])

    def test_catalog_does_not_read_large_body(self) -> None:
        content = skill_content("large-body") + ("x" * (MAX_CONTENT_BYTES * 2))
        self.write_flat("large-body", content)

        observation = self.provider().list_candidates()

        self.assertTrue(observation.complete, observation.warnings)
        self.assertEqual(["large-body"], [item.name for item in observation.candidates])

    def test_frontmatter_over_limit_with_valid_lines_is_rejected(self) -> None:
        """用合法 key:value 行把 frontmatter 撑过上限（固定 9000 字节，不依赖常量）。"""
        lines = ["---", "name: big-frontmatter", "description: test"]
        extra = "\n".join(f"extra{index}: value" for index in range(600))
        content = "\n".join(lines) + "\n" + extra + "\n---\nBody\n"
        self.assertGreater(len(content.encode("utf-8")), 8192)
        self.write_flat("big-frontmatter", content)

        observation = self.provider().list_candidates()

        self.assertFalse(observation.complete)
        self.assertEqual((), observation.candidates)
        self.assertTrue(
            any("exceeds" in item for item in observation.warnings), observation.warnings
        )

    def test_frontmatter_over_limit_is_rejected(self) -> None:
        padding = "#" * MAX_FRONTMATTER_BYTES
        self.write_flat(
            "large-frontmatter",
            f"---\nname: large-frontmatter\ndescription: test\n{padding}\n---\nBody\n",
        )

        observation = self.provider().list_candidates()

        self.assertFalse(observation.complete)
        self.assertEqual((), observation.candidates)
        self.assertTrue(any("exceeds 8192 bytes" in item for item in observation.warnings))

    def test_get_truncates_large_content_and_reports_real_size(self) -> None:
        path = self.write_flat(
            "large-content",
            skill_content("large-content") + ("x" * (300 * 1024)),
        )
        provider = self.provider()
        candidate = provider.list_candidates().candidates[0]

        definition = provider.get_definition(candidate)

        self.assertIsNotNone(definition)
        assert definition is not None
        self.assertTrue(definition.truncated)
        self.assertLessEqual(len(definition.content.encode("utf-8")), MAX_CONTENT_BYTES)
        self.assertEqual(path.stat().st_size, definition.size_bytes)

    def test_filesystem_revision_changes_with_file_metadata(self) -> None:
        path = self.write_flat("revision-skill")
        provider = self.provider()
        first = provider.list_candidates().revision
        path.write_text(skill_content("revision-skill") + "changed\n", encoding="utf-8")
        metadata = path.stat()
        os.utime(path, ns=(metadata.st_atime_ns, metadata.st_mtime_ns + 1_000_000))

        second = provider.list_candidates().revision

        self.assertTrue(first)
        self.assertTrue(second)
        self.assertNotEqual(first, second)

    def test_filesystem_does_not_recurse(self) -> None:
        nested = self.root / "sub" / "nested-skill"
        nested.mkdir(parents=True)
        (nested / "SKILL.md").write_text(skill_content("nested-skill"), encoding="utf-8")

        observation = self.provider().list_candidates()

        self.assertEqual((), observation.candidates)

    def test_root_outside_allowed_base_is_structured_warning(self) -> None:
        outside = self.env.external_dir / "skills"
        outside.mkdir()
        (outside / "outside.md").write_text(skill_content("outside"), encoding="utf-8")

        observation = self.provider(roots=(outside,)).list_candidates()

        self.assertFalse(observation.complete)
        self.assertEqual((), observation.candidates)
        self.assertTrue(observation.warnings)

    def test_intermediate_symlink_is_rejected(self) -> None:
        real = self.base / "real-skill"
        real.mkdir()
        (real / "SKILL.md").write_text(skill_content("alias-skill"), encoding="utf-8")
        (self.root / "alias-skill").symlink_to(real, target_is_directory=True)

        observation = self.provider().list_candidates()

        self.assertFalse(observation.complete)
        self.assertEqual((), observation.candidates)
        self.assertTrue(any("symlink" in warning for warning in observation.warnings))

    def test_hardlink_is_rejected(self) -> None:
        source = self.base / "source.md"
        source.write_text(skill_content("hard-linked"), encoding="utf-8")
        target = self.root / "hard-linked.md"
        os.link(source, target)

        observation = self.provider().list_candidates()

        self.assertFalse(observation.complete)
        self.assertEqual((), observation.candidates)
        self.assertTrue(any("hardlink" in warning for warning in observation.warnings))

    def test_sensitive_name_is_rejected(self) -> None:
        self.write_flat("secrets")

        observation = self.provider().list_candidates()

        self.assertFalse(observation.complete)
        self.assertEqual((), observation.candidates)
        self.assertTrue(any("敏感名" in warning for warning in observation.warnings))

    def test_frontmatter_name_must_match_path_name(self) -> None:
        self.write_package("path-name", skill_content("other-name"))

        observation = self.provider().list_candidates()

        self.assertFalse(observation.complete)
        self.assertEqual((), observation.candidates)
        self.assertTrue(any("不一致" in warning for warning in observation.warnings))

    def test_disable_model_invocation_true(self) -> None:
        self.write_package(
            "model-policy",
            skill_content("model-policy", extra="disable-model-invocation: true\n"),
        )

        candidate = self.provider().list_candidates().candidates[0]

        self.assertFalse(candidate.invocation.model_invocable)

    def test_user_invocable_false(self) -> None:
        self.write_package(
            "user-policy",
            skill_content("user-policy", extra="user-invocable: false\n"),
        )

        candidate = self.provider().list_candidates().candidates[0]

        self.assertFalse(candidate.invocation.user_invocable)

    def test_invalid_boolean_is_rejected(self) -> None:
        self.write_package(
            "invalid-policy",
            skill_content("invalid-policy", extra="disable-model-invocation: yes\n"),
        )

        observation = self.provider().list_candidates()

        self.assertFalse(observation.complete)
        self.assertEqual((), observation.candidates)
        self.assertTrue(any("仅接受 true 或 false" in warning for warning in observation.warnings))

    def test_non_utf8_is_structured_warning(self) -> None:
        path = self.root / "binary-skill.md"
        path.write_bytes(b"---\nname: binary-skill\ndescription: \xff\n---\n")

        observation = self.provider().list_candidates()

        self.assertFalse(observation.complete)
        self.assertEqual((), observation.candidates)
        self.assertTrue(any("UTF-8" in warning for warning in observation.warnings))

    def test_missing_frontmatter_is_rejected(self) -> None:
        self.write_flat("plain-skill", "# plain markdown\n")

        observation = self.provider().list_candidates()

        self.assertFalse(observation.complete)
        self.assertEqual((), observation.candidates)
        self.assertTrue(any("frontmatter" in warning for warning in observation.warnings))

    def test_opencode_default_roots_are_opt_in(self) -> None:
        default_root = self.env.fake_home / ".config" / "opencode" / "skills"
        default_root.mkdir(parents=True)
        (default_root / "hidden.md").write_text(skill_content("hidden"), encoding="utf-8")

        with patch.dict(os.environ, {"HOME": str(self.env.fake_home)}):
            provider = OpencodeHostSkillProvider(rank=80, roots=None, use_default_roots=False)

        observation = provider.list_candidates()
        self.assertTrue(observation.complete)
        self.assertEqual((), observation.candidates)

    def test_opencode_default_roots_require_allowed_bases(self) -> None:
        with self.assertRaises(ValueError):
            OpencodeHostSkillProvider(rank=80, use_default_roots=True, allowed_bases=())

    def test_codex_default_roots_are_opt_in(self) -> None:
        default_root = self.env.fake_home / ".codex" / "skills"
        default_root.mkdir(parents=True)
        (default_root / "hidden.md").write_text(skill_content("hidden"), encoding="utf-8")

        with patch.dict(os.environ, {"HOME": str(self.env.fake_home)}):
            provider = CodexHostSkillProvider(rank=80, roots=None, use_default_roots=False)

        observation = provider.list_candidates()
        self.assertTrue(observation.complete)
        self.assertEqual((), observation.candidates)

    def test_codex_default_roots_require_allowed_bases(self) -> None:
        with self.assertRaises(ValueError):
            CodexHostSkillProvider(rank=80, use_default_roots=True, allowed_bases=())

    def test_get_definition_returns_none_after_file_deleted(self) -> None:
        path = self.write_package("deletable")
        provider = self.provider()
        candidate = provider.list_candidates().candidates[0]
        path.unlink()

        self.assertIsNone(provider.get_definition(candidate))

    def test_get_definition_rejects_name_changed_after_discovery(self) -> None:
        path = self.write_flat("safe")
        provider = self.provider()
        registry = SkillRegistry()
        registry.register(provider)
        self.assertEqual(("safe",), tuple(item.name for item in registry.snapshot().skills))
        path.write_text(skill_content("other"), encoding="utf-8")

        self.assertIsNone(registry.get("safe"))
        self.assertEqual((), registry.snapshot().skills)

    def test_platform_symlink_exemption_is_darwin_only(self) -> None:
        """平台别名豁免只应存在于 macOS；其他平台不得放行。"""
        from orchagent import skill_providers as module

        with patch.object(module.sys, "platform", "linux"):
            self.assertEqual(frozenset(), module._platform_symlink_prefixes())
        with patch.object(module.sys, "platform", "darwin"):
            self.assertEqual(
                module._PLATFORM_SYMLINK_PREFIXES, module._platform_symlink_prefixes()
            )

    def test_filesystem_requires_allowed_bases(self) -> None:
        with self.assertRaises(ValueError):
            self.provider(allowed_bases=())

    def test_registry_prefers_builtin_rank_for_same_name(self) -> None:
        self.write_flat("shared-skill")
        filesystem = self.provider(rank=100)
        registry = SkillRegistry()

        with patch.dict(
            BUILTIN_SKILL_IMPLEMENTATIONS,
            {"shared-skill": "pipeline.emit_json"},
            clear=False,
        ):
            registry.register(filesystem)
            registry.register(BuiltinSkillProvider())
            snapshot = registry.snapshot()
            definition = registry.get("shared-skill")

        self.assertTrue(snapshot.complete, snapshot.warnings)
        self.assertEqual("orchagent.skills.builtin", snapshot.conflicts[0].winner_provider)
        self.assertIsNotNone(definition)
        assert definition is not None
        self.assertEqual("builtin:shared-skill", definition.content)


if __name__ == "__main__":
    unittest.main()
