from __future__ import annotations

import unittest
from dataclasses import fields
from typing import Any

from orchagent.skill_seam import (
    SkillCandidate,
    SkillDefinition,
    SkillInvocationPolicy,
    SkillProviderObservation,
    SkillRegistry,
    SkillSummary,
)


def candidate(
    name: Any = "sample-skill",
    *,
    provider_id: Any = "provider-a",
    rank: Any = 100,
    description: Any = "示例 skill",
    source: Any = "memory",
) -> SkillCandidate:
    """构造可按字段覆盖的内存候选。"""
    return SkillCandidate(
        name=name,
        description=description,
        provider_id=provider_id,
        rank=rank,
        source=source,
        invocation=SkillInvocationPolicy(),
        locator=(provider_id, name),
        backend="builtin",
    )


class FakeProvider:
    """只在内存中发现和读取定义的假 provider。"""

    def __init__(
        self,
        provider_id: str,
        candidates: tuple[SkillCandidate, ...] = (),
        *,
        rank: int = 100,
        complete: bool = True,
        revision: str | None = None,
        warnings: tuple[str, ...] = (),
    ) -> None:
        self.provider_id = provider_id
        self.rank = rank
        self.candidates = candidates
        self.complete = complete
        self.revision = revision
        self.warnings = warnings
        self.list_calls = 0
        self.definition: SkillDefinition | None = None
        self.list_error: Exception | None = None
        self.get_error: Exception | None = None

    def list_candidates(self) -> SkillProviderObservation:
        self.list_calls += 1
        if self.list_error is not None:
            raise self.list_error
        return SkillProviderObservation(
            candidates=self.candidates,
            complete=self.complete,
            revision=self.revision,
            warnings=self.warnings,
        )

    def get_definition(self, selected: SkillCandidate) -> SkillDefinition | None:
        if self.get_error is not None:
            raise self.get_error
        if self.definition is not None:
            return self.definition
        return SkillDefinition(candidate=selected, content=f"正文:{selected.name}")

    def invalidate(self) -> None:
        return None


class SkillRegistryTests(unittest.TestCase):
    def test_register_rejects_duplicate_provider_id(self) -> None:
        registry = SkillRegistry()
        registry.register(FakeProvider("same"))

        with self.assertRaises(ValueError):
            registry.register(FakeProvider("same"))

    def test_register_rejects_object_missing_required_attributes(self) -> None:
        with self.assertRaises(ValueError):
            SkillRegistry().register(object())  # type: ignore[arg-type]

    def test_invalid_candidates_are_dropped_and_mark_snapshot_incomplete(self) -> None:
        invalid = (
            candidate("Bad_Name"),
            candidate("empty-description", description=""),
            candidate("bool-rank", rank=True),
            candidate("string-rank", rank="1"),
            candidate("empty-provider", provider_id=""),
        )
        registry = SkillRegistry()
        registry.register(FakeProvider("provider-a", invalid))

        snapshot = registry.snapshot()

        self.assertEqual((), snapshot.skills)
        self.assertFalse(snapshot.complete)
        self.assertEqual(len(invalid), len(snapshot.warnings))

    def test_lower_rank_wins_and_loser_is_reported(self) -> None:
        registry = SkillRegistry()
        registry.register(FakeProvider("a", (candidate(provider_id="a", rank=100),)))
        registry.register(FakeProvider("b", (candidate(provider_id="b", rank=200),)))

        snapshot = registry.snapshot()

        self.assertEqual("a", snapshot.skills[0].provider)
        self.assertEqual(("b", 200), snapshot.conflicts[0].shadowed[0])

    def test_equal_rank_uses_provider_registration_order(self) -> None:
        registry = SkillRegistry()
        registry.register(FakeProvider("first", (candidate(provider_id="first"),)))
        registry.register(FakeProvider("second", (candidate(provider_id="second"),)))

        self.assertEqual("first", registry.snapshot().skills[0].provider)

    def test_same_provider_uses_candidate_return_order(self) -> None:
        first = candidate(provider_id="same", description="第一项")
        second = candidate(provider_id="same", description="第二项")
        registry = SkillRegistry()
        registry.register(FakeProvider("same", (first, second)))

        snapshot = registry.snapshot()

        self.assertEqual("第一项", snapshot.skills[0].description)
        self.assertEqual((("same", 100),), snapshot.conflicts[0].shadowed)

    def test_all_non_winners_appear_in_comparison_order(self) -> None:
        registry = SkillRegistry()
        registry.register(FakeProvider("p200", (candidate(provider_id="p200", rank=200),)))
        registry.register(FakeProvider("p100", (candidate(provider_id="p100", rank=100),)))
        registry.register(FakeProvider("p300", (candidate(provider_id="p300", rank=300),)))

        conflict = registry.snapshot().conflicts[0]

        self.assertEqual("p100", conflict.winner_provider)
        self.assertEqual((("p200", 200), ("p300", 300)), conflict.shadowed)

    def test_skills_and_conflicts_are_sorted_by_name(self) -> None:
        first = FakeProvider(
            "first",
            (
                candidate("z-skill", provider_id="first"),
                candidate("a-skill", provider_id="first"),
            ),
        )
        second = FakeProvider(
            "second",
            (
                candidate("z-skill", provider_id="second"),
                candidate("a-skill", provider_id="second"),
            ),
        )
        registry = SkillRegistry()
        registry.register(first)
        registry.register(second)

        snapshot = registry.snapshot()

        self.assertEqual(["a-skill", "z-skill"], [item.name for item in snapshot.skills])
        self.assertEqual(["a-skill", "z-skill"], [item.name for item in snapshot.conflicts])

    def test_list_failure_is_isolated(self) -> None:
        failed = FakeProvider("failed")
        failed.list_error = RuntimeError("boom")
        healthy = FakeProvider("healthy", (candidate(provider_id="healthy"),))
        registry = SkillRegistry()
        registry.register(failed)
        registry.register(healthy)

        snapshot = registry.snapshot()

        self.assertEqual(("sample-skill",), tuple(item.name for item in snapshot.skills))
        self.assertFalse(snapshot.complete)
        self.assertTrue(snapshot.warnings)

    def test_incomplete_observation_marks_snapshot_incomplete(self) -> None:
        registry = SkillRegistry()
        registry.register(FakeProvider("incomplete", complete=False))

        self.assertFalse(registry.snapshot().complete)

    def test_incomplete_observation_is_not_cached(self) -> None:
        provider = FakeProvider("recovering", complete=False)
        registry = SkillRegistry()
        registry.register(provider)

        first = registry.snapshot()
        provider.complete = True
        provider.candidates = (candidate(provider_id="recovering"),)
        second = registry.snapshot()

        self.assertFalse(first.complete)
        self.assertEqual((), first.skills)
        self.assertTrue(second.complete)
        self.assertEqual(("sample-skill",), tuple(item.name for item in second.skills))
        self.assertEqual(2, provider.list_calls)

    def test_get_returns_definition_content(self) -> None:
        registry = SkillRegistry()
        registry.register(FakeProvider("provider-a", (candidate(),)))

        definition = registry.get("sample-skill")

        self.assertIsNotNone(definition)
        self.assertEqual("正文:sample-skill", definition.content)

    def test_get_rejects_stale_none_and_provider_error(self) -> None:
        cases = []

        stale = FakeProvider("stale", (candidate(provider_id="stale"),))
        stale.definition = SkillDefinition(
            candidate=candidate("other-skill", provider_id="stale"),
            content="陈旧正文",
        )
        cases.append(stale)

        missing = FakeProvider("missing", (candidate(provider_id="missing"),))
        missing.get_definition = lambda selected: None  # type: ignore[method-assign]
        cases.append(missing)

        failed = FakeProvider("failed", (candidate(provider_id="failed"),))
        failed.get_error = RuntimeError("boom")
        cases.append(failed)

        for provider in cases:
            with self.subTest(provider=provider.provider_id):
                registry = SkillRegistry()
                registry.register(provider)
                self.assertIsNone(registry.get("sample-skill"))

    def test_get_none_invalidates_cached_snapshot(self) -> None:
        provider = FakeProvider("missing", (candidate(provider_id="missing"),))
        provider.get_definition = lambda selected: None  # type: ignore[method-assign]
        registry = SkillRegistry()
        registry.register(provider)

        self.assertIsNone(registry.get("sample-skill"))
        registry.snapshot()

        self.assertEqual(2, provider.list_calls)

    def test_get_invalid_name_returns_none(self) -> None:
        self.assertIsNone(SkillRegistry().get("Bad_Name"))

    def test_invalidate_forces_rediscovery(self) -> None:
        provider = FakeProvider("provider-a", (candidate(),))
        registry = SkillRegistry()
        registry.register(provider)

        registry.snapshot()
        registry.snapshot()
        self.assertEqual(1, provider.list_calls)

        registry.invalidate()
        registry.snapshot()
        self.assertEqual(2, provider.list_calls)

    def test_skill_summary_has_no_content_field(self) -> None:
        self.assertNotIn("content", {field.name for field in fields(SkillSummary)})

    def test_register_returns_disposer_that_removes_provider(self) -> None:
        """register 返回幂等 disposer；调用后 provider 与其贡献一并移除。"""
        registry = SkillRegistry()
        provider = FakeProvider("removable", (candidate("removable-skill", provider_id="removable"),))
        dispose = registry.register(provider)

        self.assertEqual(("removable-skill",), tuple(item.name for item in registry.snapshot().skills))

        dispose()
        self.assertEqual((), registry.snapshot().skills)
        self.assertIsNone(registry.get("removable-skill"))

        # 幂等：再次调用不得抛错，也不得影响后续同名重新注册。
        dispose()
        registry.register(FakeProvider("removable", (candidate("removable-skill", provider_id="removable"),)))
        self.assertEqual(("removable-skill",), tuple(item.name for item in registry.snapshot().skills))

    def test_disposer_does_not_remove_a_replacement_provider(self) -> None:
        """旧 provider 的 disposer 不得误删后来注册的同名替代者（对象同一性）。"""
        registry = SkillRegistry()
        first = FakeProvider("same-id", (candidate("first-skill", provider_id="same-id"),))
        dispose_first = registry.register(first)
        dispose_first()
        registry.register(FakeProvider("same-id", (candidate("second-skill", provider_id="same-id"),)))

        dispose_first()  # 过期 disposer：应为 no-op
        self.assertEqual(("second-skill",), tuple(item.name for item in registry.snapshot().skills))

    def test_disposer_uses_identity_not_equality(self) -> None:
        """provider 定义了 __eq__ 时，旧 disposer 也不得误删判等但非同一的替代者。"""

        class EqualityProvider:
            def __init__(self, provider_id: str, skills: tuple[SkillCandidate, ...]) -> None:
                self.provider_id = provider_id
                self.rank = 100
                self._skills = skills

            def list_candidates(self) -> SkillProviderObservation:
                return SkillProviderObservation(candidates=self._skills)

            def get_definition(self, selected: SkillCandidate) -> SkillDefinition:
                return SkillDefinition(candidate=selected, content="正文")

            def invalidate(self) -> None:
                return None

            def __eq__(self, other: object) -> bool:
                return self.provider_id == getattr(other, "provider_id", None)

            def __hash__(self) -> int:
                return hash(self.provider_id)

        registry = SkillRegistry()
        first = EqualityProvider("shared", (candidate("first", provider_id="shared"),))
        dispose_first = registry.register(first)
        dispose_first()
        second = EqualityProvider("shared", (candidate("second", provider_id="shared"),))
        registry.register(second)

        dispose_first()  # 过期 disposer：判等但非同一，必须是 no-op
        self.assertEqual(("second",), tuple(item.name for item in registry.snapshot().skills))

    def test_revision_aggregates_non_none_provider_revisions(self) -> None:
        registry = SkillRegistry()
        registry.register(FakeProvider("a", revision="rev-a"))
        registry.register(FakeProvider("b", revision=None))
        registry.register(FakeProvider("c", revision="rev-c"))

        self.assertEqual("rev-a|rev-c", registry.snapshot().revision)

    def test_provider_declared_name_pattern_allows_namespaced_names(self) -> None:
        """provider 可声明自己的 name pattern；缺省仍强制 kebab-case。"""
        import re as _re

        class NamespacedProvider(FakeProvider):
            name_pattern = _re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")

        dotted = candidate("orchagent.pipeline.emit-json", provider_id="builtin")
        registry = SkillRegistry()
        registry.register(NamespacedProvider("builtin", (dotted,)))

        snapshot = registry.snapshot()
        self.assertTrue(snapshot.complete)
        self.assertEqual(
            ("orchagent.pipeline.emit-json",),
            tuple(item.name for item in snapshot.skills),
        )
        definition = registry.get("orchagent.pipeline.emit-json")
        self.assertIsNotNone(definition)
        assert definition is not None
        self.assertEqual("orchagent.pipeline.emit-json", definition.candidate.name)

    def test_default_pattern_still_rejects_dotted_names(self) -> None:
        """未声明 name_pattern 的 provider 仍必须遵循 kebab-case。"""
        dotted = candidate("orchagent.pipeline.emit-json", provider_id="plain")
        registry = SkillRegistry()
        registry.register(FakeProvider("plain", (dotted,)))

        snapshot = registry.snapshot()
        self.assertFalse(snapshot.complete)
        self.assertEqual((), snapshot.skills)
        self.assertTrue(any("pattern" in warning for warning in snapshot.warnings))


if __name__ == "__main__":
    unittest.main()
