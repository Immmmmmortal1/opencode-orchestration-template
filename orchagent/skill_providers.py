from __future__ import annotations

import codecs
import hashlib
import os
import sys
import re
import stat
from pathlib import Path
from typing import Sequence

from .builtin_skills import BUILTIN_SKILL_IMPLEMENTATIONS
from .skill_seam import (
    SkillCandidate,
    SkillDefinition,
    SkillInvocationPolicy,
    SkillProviderObservation,
)
from .skills import (
    MAX_FRONTMATTER_BYTES,
    _has_sensitive_part,
    _has_symlink_component,
    _parse_scalar,
)


_FILESYSTEM_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_BUILTIN_NAME_PATTERN = re.compile(r"^[a-z0-9]+(?:[.-][a-z0-9]+)*$")
MAX_CONTENT_BYTES = 262144
_PLATFORM_SYMLINK_PREFIXES = frozenset({"/var", "/tmp", "/etc"})
"""macOS 系统别名豁免（精确匹配这三个顶层路径）。

`/var`、`/tmp`、`/etc` 由操作系统拥有，本身是指向 `/private/*` 的系统 symlink，
不属于「配置声明者可控」范围；因此只在这三条**精确路径**上放行。
其**之下**的任一段 symlink 仍被逐段拒绝——豁免不可被用于越界。
仅适用 macOS；其他平台无豁免。见 `docs/capability-seams.md` §7。
"""


def _platform_symlink_prefixes() -> frozenset[str]:
    """返回当前平台允许的**系统别名**豁免集合。

    仅 macOS（`darwin`）适用：`/var` `/tmp` `/etc` 由系统拥有并指向 `/private/*`。
    其他平台**无豁免**——规范（`docs/capability-seams.md` §7）明确限定为 macOS。
    """
    if sys.platform == "darwin":
        return _PLATFORM_SYMLINK_PREFIXES
    return frozenset()


def _symlinked_base_prefix(base: Path) -> str | None:
    """返回 allowed base 声明路径上第一个非豁免 symlink 段；无则 None。"""
    exempt = _platform_symlink_prefixes()
    current = Path(base.anchor) if base.anchor else Path()
    for part in base.parts:
        if part == base.anchor:
            continue
        current = current / part
        current_text = str(current)
        if os.path.islink(current) and current_text not in exempt:
            return current_text
    return None


class BuiltinSkillProvider:
    """只读暴露 orchAgent 内建 fixture skill。"""

    provider_id = "orchagent.skills.builtin"
    rank = 50
    source = "builtin"
    backend = "builtin"
    name_pattern = _BUILTIN_NAME_PATTERN

    def list_candidates(self) -> SkillProviderObservation:
        skill_ids = sorted(BUILTIN_SKILL_IMPLEMENTATIONS)
        candidates = tuple(
            SkillCandidate(
                name=skill_id,
                description=f"builtin fixture skill: {skill_id}",
                provider_id=self.provider_id,
                rank=self.rank,
                source=self.source,
                invocation=SkillInvocationPolicy(),
                locator=skill_id,
                backend=self.backend,
            )
            for skill_id in skill_ids
        )
        revision = hashlib.sha256("\n".join(skill_ids).encode("utf-8")).hexdigest()
        return SkillProviderObservation(candidates=candidates, revision=revision)

    def get_definition(self, candidate: SkillCandidate) -> SkillDefinition | None:
        skill_id = candidate.locator
        if (
            not isinstance(skill_id, str)
            or skill_id not in BUILTIN_SKILL_IMPLEMENTATIONS
            or candidate.name != skill_id
        ):
            return None
        content = f"builtin:{skill_id}"
        return SkillDefinition(
            candidate=candidate,
            content=content,
            size_bytes=len(content.encode("utf-8")),
        )

    def invalidate(self) -> None:
        """Builtin catalog 没有运行时缓存。"""


class FilesystemSkillProvider:
    """从受限根目录的顶层只读发现 skill。"""

    def __init__(
        self,
        provider_id: str,
        rank: int,
        roots: Sequence[Path],
        allowed_bases: Sequence[Path],
        source: str = "filesystem",
    ) -> None:
        if not allowed_bases:
            raise ValueError("allowed_bases 不能为空")
        self.provider_id = provider_id
        self.rank = rank
        self.source = source
        self._roots = tuple(Path(root) for root in roots)
        self._allowed_bases = tuple(Path(base) for base in allowed_bases)

    def list_candidates(self) -> SkillProviderObservation:
        candidates: list[SkillCandidate] = []
        warnings: list[str] = []
        revision_entries: list[str] = []
        complete = True

        for root in self._roots:
            authorized, authorization_error = self._authorize_root(root)
            if authorized is None:
                warnings.append(f"root 被拒绝: {root}: {authorization_error or '授权失败'}")
                complete = False
                continue
            root_absolute, allowed_base = authorized
            try:
                entries = tuple(root_absolute.iterdir())
            except FileNotFoundError:
                continue
            except OSError as error:
                warnings.append(f"root 读取失败: {root}: {type(error).__name__}")
                complete = False
                continue

            for entry in sorted(entries, key=lambda path: path.name):
                skill_file, expected_name = self._candidate_path(entry)
                if skill_file is None or expected_name is None:
                    continue
                candidate, warning, metadata = self._inspect_candidate(
                    skill_file,
                    expected_name,
                    allowed_base,
                )
                if warning is not None:
                    warnings.append(warning)
                    complete = False
                elif candidate is not None:
                    candidates.append(candidate)
                    assert metadata is not None
                    try:
                        relative_path = skill_file.absolute().relative_to(root_absolute)
                    except ValueError:
                        relative_path = skill_file.absolute()
                    revision_entries.append(
                        f"{root_absolute}\0{relative_path}\0{metadata.st_mtime_ns}\0{metadata.st_size}"
                    )

        revision_roots = [str(root.absolute()) for root in self._roots]
        revision_payload = "\n".join(sorted(revision_roots) + sorted(revision_entries))
        return SkillProviderObservation(
            candidates=tuple(candidates),
            complete=complete,
            revision=hashlib.sha256(revision_payload.encode("utf-8")).hexdigest(),
            warnings=tuple(warnings),
        )

    def get_definition(self, candidate: SkillCandidate) -> SkillDefinition | None:
        locator = candidate.locator
        if (
            not isinstance(locator, tuple)
            or len(locator) != 2
            or not all(isinstance(value, str) for value in locator)
        ):
            return None
        path = Path(locator[0])
        allowed_base = Path(locator[1])
        if allowed_base not in self._allowed_bases:
            return None
        content, _, size_bytes, truncated = self._read_safe(
            path,
            allowed_base,
            max_bytes=MAX_CONTENT_BYTES,
        )
        if content is None:
            return None
        frontmatter, error = _read_catalog_frontmatter(content)
        if error is not None or frontmatter.get("name") != candidate.name:
            return None
        return SkillDefinition(
            candidate=candidate,
            content=content,
            truncated=truncated,
            size_bytes=size_bytes,
        )

    def invalidate(self) -> None:
        """Filesystem provider 不持有目录缓存。"""

    def _authorize_root(self, root: Path) -> tuple[tuple[Path, Path] | None, str | None]:
        try:
            root_absolute = root.absolute()
            root_resolved = root.resolve()
        except OSError:
            return None, "路径解析失败"
        if _has_sensitive_part(root_absolute) or _has_sensitive_part(root_resolved):
            return None, "路径含敏感名"

        for allowed_base in self._allowed_bases:
            try:
                base_absolute = allowed_base.absolute()
                if base_absolute.is_symlink():
                    return None, "allowed base is a symlink"
                symlinked_prefix = _symlinked_base_prefix(base_absolute)
                if symlinked_prefix is not None:
                    return None, f"allowed base contains symlink: {symlinked_prefix}"
                base_resolved = allowed_base.resolve()
            except OSError:
                continue
            if root_resolved != base_resolved and base_resolved not in root_resolved.parents:
                continue
            # 逐段拒绝 symlink，home 参数必须是命中的 allowed_base。
            if _has_symlink_component(root_absolute, base_absolute, base_resolved):
                return None, "路径含 symlink"
            return (root_absolute, allowed_base), None
        return None, "路径越界"

    @staticmethod
    def _candidate_path(entry: Path) -> tuple[Path | None, str | None]:
        try:
            if entry.is_dir():
                return entry / "SKILL.md", entry.name
            if entry.suffix == ".md":
                return entry, entry.stem
        except OSError:
            return None, None
        return None, None

    def _inspect_candidate(
        self,
        path: Path,
        expected_name: str,
        allowed_base: Path,
    ) -> tuple[SkillCandidate | None, str | None, os.stat_result | None]:
        content, metadata, error = self._read_frontmatter_safe(path, allowed_base)
        if error is not None:
            return None, f"候选 {path} 被拒绝: {error}", None
        assert content is not None and metadata is not None

        frontmatter, error = _read_catalog_frontmatter(content)
        if error is not None:
            return None, f"候选 {path} frontmatter 无效: {error}", None
        name = frontmatter.get("name", "")
        description = frontmatter.get("description", "")
        if _FILESYSTEM_NAME_PATTERN.fullmatch(name) is None:
            return None, f"候选 {path} name 非法", None
        if not description:
            return None, f"候选 {path} description 为空", None
        if name != expected_name:
            return None, f"候选 {path} name 与路径名不一致", None

        optional_fields, error = _read_optional_frontmatter(content)
        if error is not None:
            return None, f"候选 {path} frontmatter 无效: {error}", None
        disable_model, error = _boolean_field(optional_fields, "disable-model-invocation", False)
        if error is not None:
            return None, f"候选 {path} frontmatter 无效: {error}", None
        user_invocable, error = _boolean_field(optional_fields, "user-invocable", True)
        if error is not None:
            return None, f"候选 {path} frontmatter 无效: {error}", None

        backend = optional_fields.get("backend")
        return (
            SkillCandidate(
                name=name,
                description=description,
                provider_id=self.provider_id,
                rank=self.rank,
                source=self.source,
                invocation=SkillInvocationPolicy(
                    model_invocable=not disable_model,
                    user_invocable=user_invocable,
                ),
                locator=(str(path.absolute()), str(allowed_base)),
                backend=backend,
            ),
            None,
            metadata,
        )

    @staticmethod
    def _open_safe(
        path: Path,
        allowed_base: Path,
    ) -> tuple[int | None, os.stat_result | None, str | None]:
        try:
            path_absolute = path.absolute()
            path_resolved = path.resolve()
            base_absolute = allowed_base.absolute()
            base_resolved = allowed_base.resolve()
        except OSError as error:
            return None, None, f"路径解析失败: {type(error).__name__}"
        if base_absolute.is_symlink():
            return None, None, "allowed base is a symlink"
        symlinked_prefix = _symlinked_base_prefix(base_absolute)
        if symlinked_prefix is not None:
            return None, None, f"allowed base contains symlink: {symlinked_prefix}"
        if path_resolved != base_resolved and base_resolved not in path_resolved.parents:
            return None, None, "路径越界"
        if _has_sensitive_part(path_absolute) or _has_sensitive_part(path_resolved):
            return None, None, "路径含敏感名"
        # 对候选文件的完整路径链逐段检查，避免中间目录 symlink 绕过。
        if _has_symlink_component(path_absolute, base_absolute, base_resolved):
            return None, None, "路径含 symlink"

        fd = -1
        try:
            if os.lstat(path_absolute).st_nlink > 1:
                return None, None, "文件是 hardlink"
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path_absolute, flags)
            metadata = os.fstat(fd)
            if not stat.S_ISREG(metadata.st_mode):
                os.close(fd)
                return None, None, "不是 regular file"
            if metadata.st_nlink > 1:
                os.close(fd)
                return None, None, "文件是 hardlink"
            return fd, metadata, None
        except OSError as error:
            if fd >= 0:
                os.close(fd)
            return None, None, f"文件读取失败: {type(error).__name__}"

    @classmethod
    def _read_frontmatter_safe(
        cls,
        path: Path,
        allowed_base: Path,
    ) -> tuple[str | None, os.stat_result | None, str | None]:
        fd, metadata, error = cls._open_safe(path, allowed_base)
        if fd is None:
            return None, None, error
        chunks: list[bytes] = []
        consumed = 0
        try:
            with os.fdopen(fd, "rb") as stream:
                fd = -1
                while consumed < MAX_FRONTMATTER_BYTES:
                    line = stream.readline(MAX_FRONTMATTER_BYTES - consumed)
                    if not line:
                        break
                    chunks.append(line)
                    consumed += len(line)
                    if len(chunks) > 1 and line.strip() == b"---":
                        break
                else:
                    return None, None, "frontmatter exceeds 8192 bytes"
        finally:
            if fd >= 0:
                os.close(fd)
        if len(chunks) < 2 or chunks[-1].strip() != b"---":
            if metadata is not None and metadata.st_size > MAX_FRONTMATTER_BYTES:
                return None, None, "frontmatter exceeds 8192 bytes"
            return None, None, "frontmatter 未闭合"
        try:
            return b"".join(chunks).decode("utf-8"), metadata, None
        except UnicodeError:
            return None, None, "文件不是 UTF-8"

    @classmethod
    def _read_safe(
        cls,
        path: Path,
        allowed_base: Path,
        *,
        max_bytes: int,
    ) -> tuple[str | None, str | None, int, bool]:
        try:
            if allowed_base.absolute().is_symlink():
                return None, "allowed base is a symlink", 0, False
            symlinked_prefix = _symlinked_base_prefix(allowed_base.absolute())
            if symlinked_prefix is not None:
                return None, f"allowed base contains symlink: {symlinked_prefix}", 0, False
        except OSError as error:
            return None, f"路径解析失败: {type(error).__name__}", 0, False
        fd, metadata, error = cls._open_safe(path, allowed_base)
        if fd is None or metadata is None:
            return None, error, 0, False
        try:
            with os.fdopen(fd, "rb") as stream:
                fd = -1
                raw = stream.read(max_bytes)
        finally:
            if fd >= 0:
                os.close(fd)
        truncated = metadata.st_size > max_bytes
        try:
            if truncated:
                decoder = codecs.getincrementaldecoder("utf-8")()
                content = decoder.decode(raw, final=False)
            else:
                content = raw.decode("utf-8")
        except UnicodeError:
            return None, "文件不是 UTF-8", metadata.st_size, truncated
        return content, None, metadata.st_size, truncated


class _HostSkillProvider:
    """Host provider 的组合式公共封装。"""

    provider_id: str
    source: str
    default_roots: tuple[Path, ...]

    def __init__(
        self,
        rank: int,
        roots: Sequence[Path] | None = None,
        use_default_roots: bool = False,
        allowed_bases: Sequence[Path] = (),
    ) -> None:
        selected_roots = self.default_roots if use_default_roots and roots is None else roots
        if use_default_roots and roots is None and not allowed_bases:
            raise ValueError("使用默认 roots 时 allowed_bases 不能为空")
        self.rank = rank
        self._delegate = (
            FilesystemSkillProvider(
                provider_id=self.provider_id,
                rank=rank,
                roots=selected_roots,
                allowed_bases=allowed_bases,
                source=self.source,
            )
            if selected_roots
            else None
        )

    def list_candidates(self) -> SkillProviderObservation:
        if self._delegate is None:
            return SkillProviderObservation(candidates=())
        return self._delegate.list_candidates()

    def get_definition(self, candidate: SkillCandidate) -> SkillDefinition | None:
        if self._delegate is None:
            return None
        return self._delegate.get_definition(candidate)

    def invalidate(self) -> None:
        if self._delegate is not None:
            self._delegate.invalidate()


class OpencodeHostSkillProvider(_HostSkillProvider):
    """发现 OpenCode 兼容宿主目录中的 skill。"""

    provider_id = "orchagent.skills.opencode-host"
    source = "opencode-host"
    default_roots = (
        Path("~/.config/opencode/skills").expanduser(),
        Path("~/.config/opencode/skill").expanduser(),
        Path("~/.agents/skills").expanduser(),
        Path("~/.claude/skills").expanduser(),
    )


class CodexHostSkillProvider(_HostSkillProvider):
    """发现 Codex 宿主目录中的 skill。"""

    provider_id = "orchagent.skills.codex-host"
    source = "codex-host"
    default_roots = (Path("~/.codex/skills").expanduser(),)


def _read_optional_frontmatter(content: str) -> tuple[dict[str, str], str | None]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, "缺少 frontmatter"
    fields: dict[str, str] = {}
    optional_names = {"disable-model-invocation", "user-invocable", "backend"}
    for line in lines[1:]:
        if line.strip() == "---":
            return fields, None
        key, separator, raw_value = line.partition(":")
        key = key.strip()
        if not separator or key not in optional_names:
            continue
        value = _parse_scalar(raw_value)
        if value is None:
            return {}, f"{key} 必须是非空标量"
        fields[key] = value
    return {}, "frontmatter 未闭合"


def _read_catalog_frontmatter(content: str) -> tuple[dict[str, str], str | None]:
    """解析 catalog 所需字段；content 只包含有界 frontmatter。"""
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}, "缺少 frontmatter"
    fields: dict[str, str] = {}
    supported = {
        "name",
        "description",
        "disable-model-invocation",
        "user-invocable",
        "backend",
    }
    for line in lines[1:]:
        if line.strip() == "---":
            return fields, None
        key, separator, raw_value = line.partition(":")
        key = key.strip()
        if not separator or key not in supported:
            continue
        value = _parse_scalar(raw_value)
        if value is None:
            return {}, f"{key} 必须是非空标量"
        fields[key] = value
    return {}, "frontmatter 未闭合"


def _boolean_field(
    fields: dict[str, str],
    name: str,
    default: bool,
) -> tuple[bool, str | None]:
    value = fields.get(name)
    if value is None:
        return default, None
    if value == "true":
        return True, None
    if value == "false":
        return False, None
    return default, f"{name} 仅接受 true 或 false"


__all__ = [
    "BuiltinSkillProvider",
    "CodexHostSkillProvider",
    "FilesystemSkillProvider",
    "MAX_CONTENT_BYTES",
    "MAX_FRONTMATTER_BYTES",
    "OpencodeHostSkillProvider",
]
