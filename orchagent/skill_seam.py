from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, Protocol, runtime_checkable


SKILL_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

# Registry 的缺省 name pattern（kebab-case）；provider 可通过 `name_pattern`
# 声明自己的命名空间（例如 builtin fixture 使用点号 id）。
DEFAULT_NAME_PATTERN = SKILL_NAME_RE


@dataclass(frozen=True)
class SkillInvocationPolicy:
    """声明 skill 可由哪些调用方触发。"""

    model_invocable: bool = True
    user_invocable: bool = True


@dataclass(frozen=True)
class SkillCandidate:
    """Provider 在目录发现阶段返回的 skill 候选。"""

    name: str
    description: str
    provider_id: str
    rank: int
    source: str
    invocation: SkillInvocationPolicy
    locator: object
    backend: str | None = None
    when_to_use: str | None = None


@dataclass(frozen=True)
class SkillProviderObservation:
    """Provider 一次目录发现的结果。"""

    candidates: tuple[SkillCandidate, ...]
    complete: bool = True
    revision: str | None = None
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class SkillDefinition:
    """按需加载的 skill 定义与正文。"""

    candidate: SkillCandidate
    content: str
    truncated: bool = False
    size_bytes: int = 0


@dataclass(frozen=True)
class SkillSummary:
    """面向目录消费者的元数据，不包含正文。"""

    name: str
    description: str
    provider: str
    source: str
    rank: int
    invocation: SkillInvocationPolicy
    backend: str | None = None
    when_to_use: str | None = None


@dataclass(frozen=True)
class SkillConflict:
    """记录同名 skill 的胜出来源与被遮蔽来源。"""

    name: str
    winner_provider: str
    shadowed: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class SkillSnapshot:
    """合并所有 provider 后的只读目录快照。"""

    skills: tuple[SkillSummary, ...]
    conflicts: tuple[SkillConflict, ...]
    complete: bool
    revision: str | None
    warnings: tuple[str, ...]


@runtime_checkable
class SkillProvider(Protocol):
    """Skill capability seam 的 provider 协议。"""

    provider_id: str
    rank: int

    def list_candidates(self) -> SkillProviderObservation: ...

    def get_definition(self, candidate: SkillCandidate) -> SkillDefinition | None: ...

    def invalidate(self) -> None: ...


@dataclass(frozen=True)
class _ResolvedCandidate:
    """Registry 内部保留的候选及其实际 provider。"""

    candidate: SkillCandidate
    provider: SkillProvider
    provider_order: int
    candidate_order: int


class SkillRegistry:
    """注册 provider，并按稳定优先级生成 skill 目录。"""

    def __init__(self) -> None:
        self._providers: list[SkillProvider] = []
        self._provider_ids: set[str] = set()
        self._snapshot: SkillSnapshot | None = None
        self._winners: dict[str, _ResolvedCandidate] = {}

    def register(self, provider: SkillProvider) -> Callable[[], None]:
        """注册 provider；返回**幂等 disposer**，调用后移除该 provider 并使目录失效。

        对应 [`docs/capability-seams.md`](../docs/capability-seams.md) §4.2 的
        「`register(...) -> disposer`」契约：注册是可撤销的副作用，disposer 只对
        **本条精确注册**生效（provider 被移除后再次调用为 no-op，不会误删同名替代者）。
        """
        required_attributes = (
            "provider_id",
            "rank",
            "list_candidates",
            "get_definition",
        )
        if any(not hasattr(provider, attribute) for attribute in required_attributes):
            raise ValueError("provider 缺少必需属性")
        if not isinstance(provider.provider_id, str) or not provider.provider_id:
            raise ValueError("provider_id 必须是非空字符串")
        if not callable(provider.list_candidates) or not callable(provider.get_definition):
            raise ValueError("provider 方法必须可调用")
        if provider.provider_id in self._provider_ids:
            raise ValueError(f"provider_id 重复: {provider.provider_id}")

        self._providers.append(provider)
        self._provider_ids.add(provider.provider_id)
        # 记录注册时的 provider_id，dispose 后即便 provider 自身状态变化也能正确清理。
        registered_id = provider.provider_id
        self.invalidate()

        def dispose() -> None:
            # 必须用**对象同一性**（`is`）而非相等性：provider 可能定义 `__eq__`，
            # 用 `in` / `remove` 会误删「判等但非同一」的替代者。
            for index, registered in enumerate(self._providers):
                if registered is provider:
                    del self._providers[index]
                    self._provider_ids.discard(registered_id)
                    self.invalidate()
                    return

        return dispose

    def snapshot(self) -> SkillSnapshot:
        """发现并合并 provider 目录；单个 provider 失败不影响其余来源。"""
        if self._snapshot is not None:
            return self._snapshot

        complete = True
        warnings: list[str] = []
        revisions: list[str] = []
        candidates_by_name: dict[str, list[_ResolvedCandidate]] = {}

        for provider_order, provider in enumerate(self._providers):
            try:
                observation = provider.list_candidates()
            except Exception as error:  # Provider 是隔离边界，不能让其异常逃逸。
                complete = False
                warnings.append(
                    f"provider {provider.provider_id} 发现失败: {type(error).__name__}"
                )
                continue

            if not isinstance(observation, SkillProviderObservation):
                complete = False
                warnings.append(f"provider {provider.provider_id} 返回非法 observation")
                continue

            warnings.extend(observation.warnings)
            if not observation.complete:
                complete = False
            if observation.revision is not None:
                revisions.append(observation.revision)

            name_pattern = _provider_name_pattern(provider)
            for candidate_order, candidate in enumerate(observation.candidates):
                issue = _candidate_issue(candidate, name_pattern)
                if issue is not None:
                    complete = False
                    warnings.append(f"provider {provider.provider_id} 丢弃非法 candidate: {issue}")
                    continue
                resolved = _ResolvedCandidate(
                    candidate=candidate,
                    provider=provider,
                    provider_order=provider_order,
                    candidate_order=candidate_order,
                )
                candidates_by_name.setdefault(candidate.name, []).append(resolved)

        summaries: list[SkillSummary] = []
        conflicts: list[SkillConflict] = []
        winners: dict[str, _ResolvedCandidate] = {}
        for name in sorted(candidates_by_name):
            ordered = sorted(
                candidates_by_name[name],
                key=lambda item: (
                    item.candidate.rank,
                    item.provider_order,
                    item.candidate_order,
                ),
            )
            winner = ordered[0]
            winners[name] = winner
            summaries.append(_summary(winner.candidate))
            if len(ordered) > 1:
                conflicts.append(
                    SkillConflict(
                        name=name,
                        winner_provider=winner.candidate.provider_id,
                        shadowed=tuple(
                            (item.candidate.provider_id, item.candidate.rank)
                            for item in ordered[1:]
                        ),
                    )
                )

        self._winners = winners
        snapshot = SkillSnapshot(
            skills=tuple(summaries),
            conflicts=tuple(conflicts),
            complete=complete,
            revision="|".join(revisions) if revisions else None,
            warnings=tuple(warnings),
        )
        # 不完整观测不是权威目录：返回本轮结果，但不能阻止后续重新发现。
        if complete:
            self._snapshot = snapshot
        return snapshot

    def get(self, name: str) -> SkillDefinition | None:
        """按目录裁决结果加载正文；陈旧或异常定义统一返回 None。"""
        # 入口只做「非空字符串」检查：name 只是查找键，绝不被当作路径使用；
        # 真正的命名规则由 winning provider 自己声明的 pattern 决定。
        if not isinstance(name, str) or not name:
            return None

        self.snapshot()
        winner = self._winners.get(name)
        if winner is None:
            return None
        if _provider_name_pattern(winner.provider).fullmatch(name) is None:
            return None
        try:
            definition = winner.provider.get_definition(winner.candidate)
        except Exception:  # Provider 是隔离边界，读取失败按未找到处理。
            return None
        if definition is None:
            self.invalidate()
            return None
        if not isinstance(definition, SkillDefinition):
            return None
        if definition.candidate.name != name:
            self.invalidate()
            return None
        return definition

    def invalidate(self) -> None:
        """清空 Registry 的内存快照，下一次读取将重新发现。"""
        self._snapshot = None
        self._winners = {}


def _provider_name_pattern(provider: object) -> re.Pattern[str]:
    """返回 provider 声明的 name pattern；缺省为 kebab-case。

    seam 允许不同 provider 使用各自命名空间（如 builtin 的 fixture id 为点号命名），
    Registry 只强制「该 provider 自己声明的」命名规则，避免跨命名空间的强制重命名。
    """
    pattern = getattr(provider, "name_pattern", None)
    if isinstance(pattern, re.Pattern):
        return pattern
    return DEFAULT_NAME_PATTERN


def _candidate_issue(
    candidate: object, name_pattern: re.Pattern[str] = SKILL_NAME_RE
) -> str | None:
    """返回候选的首个非法字段；合法时返回 None。"""
    if not isinstance(candidate, SkillCandidate):
        return "类型错误"
    if not isinstance(candidate.name, str) or name_pattern.fullmatch(candidate.name) is None:
        return "name 不匹配 provider 的 name pattern"
    if not isinstance(candidate.description, str) or not candidate.description:
        return "description 为空"
    if not isinstance(candidate.provider_id, str) or not candidate.provider_id:
        return "provider_id 为空"
    if isinstance(candidate.rank, bool) or not isinstance(candidate.rank, int) or candidate.rank < 0:
        return "rank 必须是非负整数且不能是 bool"
    if not isinstance(candidate.source, str) or not candidate.source:
        return "source 为空"
    if not isinstance(candidate.invocation, SkillInvocationPolicy):
        return "invocation 类型错误"
    if candidate.backend is not None and not isinstance(candidate.backend, str):
        return "backend 类型错误"
    if candidate.when_to_use is not None and not isinstance(candidate.when_to_use, str):
        return "when_to_use 类型错误"
    return None


def _summary(candidate: SkillCandidate) -> SkillSummary:
    """把候选转换为不含正文的目录摘要。"""
    return SkillSummary(
        name=candidate.name,
        description=candidate.description,
        provider=candidate.provider_id,
        source=candidate.source,
        rank=candidate.rank,
        invocation=candidate.invocation,
        backend=candidate.backend,
        when_to_use=candidate.when_to_use,
    )


__all__ = [
    "SKILL_NAME_RE",
    "SkillCandidate",
    "SkillConflict",
    "SkillDefinition",
    "SkillInvocationPolicy",
    "SkillProvider",
    "SkillProviderObservation",
    "SkillRegistry",
    "SkillSnapshot",
    "SkillSummary",
]
