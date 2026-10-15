from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .console_txn import apply_config_transaction, rollback_config_transaction, snapshot_files
from .opencode import sync_agents_from_registry
from .pipeline import _validate_registry, list_pipelines, load_pipeline_registry
from .prompts import (
    load_prompt_file_contents,
    load_prompt_registry,
    prompt_markdown_paths,
    prompt_registry_path,
    render_prompt_registry,
    sensitive_data_location,
    validate_prompt_registry,
)


PIPELINE_FILE = "extensions/pipeline.yaml"
PROMPT_FILE = "prompts/prompts.json"
DRAFT_FIELDS = frozenset({"pipeline", "prompts", "files"})
FILE_FIELDS = frozenset({"path", "content"})
DIFF_PREVIEW_LIMIT = 4096


def _error(code: str, location: str = "draft") -> dict[str, Any]:
    return {
        "status": "error",
        "error": code,
        "checks": [{"level": "error", "code": code, "location": location}],
    }


def _snapshot_hash(hashes: dict[str, str | None]) -> str:
    canonical = json.dumps(hashes, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _file_snapshot(home: Path) -> dict[str, Any]:
    paths = [PIPELINE_FILE]
    prompt_path = prompt_registry_path(home)
    if prompt_path.exists():
        paths.append(PROMPT_FILE)
        registry, error = load_prompt_registry(home)
        if error is None and registry is not None:
            try:
                contents = load_prompt_file_contents(home, registry)
            except ValueError:
                pass
            else:
                if validate_prompt_registry(home, registry, contents).get("status") == "ok":
                    paths.extend(f"prompts/{path}" for path in prompt_markdown_paths(registry))
    elif prompt_path.parent.is_dir():
        paths.append(PROMPT_FILE)
    pipeline = snapshot_files(home, paths)
    if pipeline.get("status") != "ok":
        return _error("snapshot_failed", "snapshot")
    hashes = dict(pipeline["hashes"])
    hashes.setdefault(PROMPT_FILE, None)
    return {"status": "ok", "hashes": hashes}


def snapshot(home: Path) -> dict[str, Any]:
    home = Path(home)
    files = _file_snapshot(home)
    if files.get("status") != "ok":
        return files
    pipeline = list_pipelines(home)
    if pipeline.get("status") == "ok":
        pipeline_registry, pipeline_error = load_pipeline_registry(home)
        if (
            pipeline_error is None
            and pipeline_registry is not None
            and _validate_registry(pipeline_registry) is None
        ):
            pipeline["version"] = pipeline_registry["version"]
            pipeline["definition"] = pipeline_registry
    prompt_path = prompt_registry_path(home)
    if prompt_path.exists():
        prompt_registry, prompt_error = load_prompt_registry(home)
        if prompt_error is not None or prompt_registry is None:
            prompt_result = prompt_error or _error("registry_read_error", "registry")
            prompts = {**prompt_result, "registry": None, "files": []}
        else:
            try:
                prompt_contents = load_prompt_file_contents(home, prompt_registry)
            except ValueError as exc:
                prompts = {
                    "status": "error",
                    "checks": [{"level": "error", "code": str(exc), "location": "registry"}],
                    "registry": None,
                    "files": [],
                }
            else:
                prompt_validation = validate_prompt_registry(home, prompt_registry, prompt_contents)
                if prompt_validation.get("status") == "ok":
                    prompts = {
                        "status": prompt_validation["status"],
                        "checks": prompt_validation.get("checks", []),
                        "registry": prompt_registry,
                        "files": [
                            {"path": path, "content": prompt_contents[path]}
                            for path in prompt_markdown_paths(prompt_registry)
                        ],
                    }
                else:
                    prompts = {**prompt_validation, "registry": None, "files": []}
    else:
        prompts = {"status": "unknown", "prompts": 0, "targets": 0, "registry": None, "files": []}
    return {
        "status": "ok",
        "snapshotHash": _snapshot_hash(files["hashes"]),
        "pipeline": pipeline,
        "prompts": prompts,
        "managedAgents": {"status": "unknown", "count": 0},
    }


def validate_draft(home: Path, draft: Any) -> dict[str, Any]:
    home = Path(home)
    if sensitive_data_location(draft) is not None:
        return _error("sensitive_data")
    if not isinstance(draft, dict):
        return _error("invalid_draft")
    if set(draft) - DRAFT_FIELDS:
        return _error("unknown_field")
    if not draft or not ({"pipeline", "prompts", "files"} & set(draft)):
        return _error("empty_draft")
    files = draft.get("files", [])
    if not isinstance(files, list):
        return _error("invalid_files", "draft.files")
    file_contents: dict[str, str] = {}
    prompt_root = prompt_registry_path(home).parent
    for index, item in enumerate(files):
        location = f"draft.files[{index}]"
        if not isinstance(item, dict) or set(item) != FILE_FIELDS:
            return _error("invalid_file", location)
        path = item.get("path")
        content = item.get("content")
        if not isinstance(path, str) or not isinstance(content, str):
            return _error("invalid_file", location)
        relative = Path(path)
        if relative.is_absolute() or relative.suffix != ".md" or any(part in {"", ".", ".."} for part in relative.parts):
            return _error("invalid_file_path", f"{location}.path")
        normalized = relative.as_posix()
        if normalized in file_contents:
            return _error("duplicate_file", f"{location}.path")
        current = prompt_root
        try:
            for part in relative.parts[:-1]:
                current = current / part
                if current.is_symlink() or not current.is_dir():
                    return _error("invalid_file_path", f"{location}.path")
            target = prompt_root / relative
            if target.is_symlink() or (target.exists() and not target.is_file()):
                return _error("invalid_file_path", f"{location}.path")
        except OSError:
            return _error("invalid_file_path", f"{location}.path")
        file_contents[normalized] = content

    pipeline = draft.get("pipeline")
    if pipeline is not None:
        if not isinstance(pipeline, dict):
            return _error("invalid_pipeline", "draft.pipeline")
        if pipeline.get("version") == 1:
            return _error("pipeline_v1_read_only", "draft.pipeline.version")
        error = _validate_registry(pipeline)
        if error:
            return _error("invalid_pipeline", "draft.pipeline")

    prompt_registry = draft.get("prompts")
    final_prompts = prompt_registry
    if final_prompts is None and prompt_registry_path(home).exists():
        final_prompts, prompt_error = load_prompt_registry(home)
        if prompt_error is not None or final_prompts is None:
            return _error("invalid_prompts", "prompts")
    if final_prompts is not None:
        result = validate_prompt_registry(home, final_prompts, file_contents)
        if result.get("status") != "ok":
            location = "draft.prompts" if prompt_registry is not None else "prompts"
            return _error(result.get("checks", [{}])[0].get("code", "invalid_prompts"), location)

        allowed_files = set(prompt_markdown_paths(final_prompts))
        if not set(file_contents).issubset(allowed_files):
            return _error("unreferenced_file", "draft.files")

        final_pipeline = pipeline
        if final_pipeline is None:
            final_pipeline, pipeline_error = load_pipeline_registry(home)
            if pipeline_error is not None or final_pipeline is None:
                return _error("invalid_pipeline", "pipeline")
        if final_pipeline.get("version") != 2 or _validate_registry(final_pipeline):
            return _error("invalid_pipeline", "pipeline")
        role_ids = {role["id"] for role in final_pipeline["roles"]}
        if any(target["id"] not in role_ids for target in final_prompts["targets"]):
            return _error("unknown_role", "draft.prompts.targets")
    elif file_contents:
        return _error("prompt_registry_required", "draft.files")
    return {"status": "ok", "checks": [{"level": "ok", "message": "draft is valid"}]}


def _render_prompt_draft(home: Path, registry: dict[str, Any], files: list[dict[str, str]]) -> list[dict[str, Any]]:
    file_contents = {item["path"]: item["content"] for item in files}
    previews: list[dict[str, Any]] = []
    for target in registry["targets"]:
        rendered = render_prompt_registry(
            home, registry, target["prompt"], target.get("override"), file_contents
        )
        previews.append({"kind": target["kind"], "id": target["id"], **{key: rendered[key] for key in ("sources", "markdown")}})
    return previews


def preview_draft(home: Path, draft: Any) -> dict[str, Any]:
    validation = validate_draft(home, draft)
    if validation["status"] != "ok":
        return validation
    prompt_registry = draft.get("prompts")
    if prompt_registry is None and draft.get("files"):
        prompt_registry, error = load_prompt_registry(Path(home))
        if error is not None or prompt_registry is None:
            return _error("invalid_prompts", "prompts")
    prompts = (
        _render_prompt_draft(Path(home), prompt_registry, draft.get("files", []))
        if prompt_registry is not None
        else []
    )
    return {
        "status": "ok",
        "prompts": prompts,
        "affectedTargets": {"opencode": "explicit-sync-required", "files": [item["path"] for item in draft.get("files", [])]},
    }


def _canonical(value: Any) -> str:
    return json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n"


def _content_hash(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _bounded_preview(content: str | None) -> str | None:
    if content is None or len(content) <= DIFF_PREVIEW_LIMIT:
        return content
    return content[:DIFF_PREVIEW_LIMIT] + "\n…[truncated]"


def _diff_change(home: Path, path: str, after: str, before_hash: str | None) -> dict[str, Any]:
    before: str | None = None
    if before_hash is not None:
        try:
            from .console_txn import _read_home_file
            before = _read_home_file(home, path).decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError("snapshot_failed") from exc
        if _content_hash(before) != before_hash:
            raise ValueError("snapshot_failed")
    return {
        "path": path,
        "operation": "create" if before_hash is None else "replace",
        "beforeHash": before_hash,
        "afterHash": _content_hash(after),
        "beforePreview": _bounded_preview(before),
        "afterPreview": _bounded_preview(after),
    }


def _final_prompt_watch_hashes(home: Path, draft: dict[str, Any]) -> dict[str, str | None] | None:
    registry = draft.get("prompts")
    if registry is None and prompt_registry_path(home).exists():
        registry, error = load_prompt_registry(home)
        if error is not None or registry is None:
            return None
    if registry is None:
        return {}
    paths = [f"prompts/{path}" for path in prompt_markdown_paths(registry)]
    watched = snapshot_files(home, paths)
    if watched.get("status") != "ok":
        return None
    return watched["hashes"]


def diff_draft(home: Path, draft: Any) -> dict[str, Any]:
    validation = validate_draft(home, draft)
    if validation["status"] != "ok":
        return validation
    home = Path(home)
    intended: list[tuple[str, str]] = []
    if "pipeline" in draft:
        intended.append((PIPELINE_FILE, _canonical(draft["pipeline"])))
    if "prompts" in draft:
        intended.append((PROMPT_FILE, _canonical(draft["prompts"])))
    draft_file_hashes: dict[str, str | None] = {}
    for item in draft.get("files", []):
        path = f"prompts/{item['path']}"
        intended.append((path, item["content"]))
        file_snapshot = snapshot_files(home, (path,))
        if file_snapshot.get("status") != "ok":
            return _error("snapshot_failed", "draft.files")
        draft_file_hashes[path] = file_snapshot["hashes"][path]
    current = _file_snapshot(Path(home))
    if current.get("status") != "ok":
        return current
    prompt_watch_hashes = _final_prompt_watch_hashes(Path(home), draft)
    if prompt_watch_hashes is None:
        return _error("snapshot_failed", "prompts")
    try:
        changes = [
            _diff_change(home, path, content, current["hashes"].get(path, draft_file_hashes.get(path)))
            for path, content in intended
        ]
    except ValueError:
        return _error("snapshot_failed", "diff")
    diff_payload = {
        "snapshotHash": _snapshot_hash(current["hashes"]),
        "draftFileHashes": draft_file_hashes,
        "promptWatchHashes": prompt_watch_hashes,
        "changes": changes,
    }
    diff_id = hashlib.sha256(json.dumps(diff_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    return {"status": "ok", "changes": changes, "diff": diff_payload, "diffId": diff_id}


def apply_draft(
    home: Path,
    draft: Any,
    snapshotHash: str,
    confirmDiffId: str,
    syncOpencode: bool = False,
) -> dict[str, Any]:
    home = Path(home)
    validation = validate_draft(home, draft)
    if validation["status"] != "ok":
        return validation
    current = _file_snapshot(home)
    if current.get("status") != "ok":
        return current
    prompt_watch_hashes = _final_prompt_watch_hashes(home, draft)
    if prompt_watch_hashes is None:
        return _error("snapshot_failed", "prompts")
    if not isinstance(snapshotHash, str) or snapshotHash != _snapshot_hash(current["hashes"]):
        return _error("snapshot_conflict", "snapshotHash")
    diff = diff_draft(home, draft)
    if diff["status"] != "ok":
        return diff
    if not isinstance(confirmDiffId, str) or confirmDiffId != diff["diffId"]:
        return _error("diff_mismatch", "confirmDiffId")

    contents: dict[str, str] = {}
    expected: dict[str, str | None] = {}
    if "pipeline" in draft:
        contents[PIPELINE_FILE] = _canonical(draft["pipeline"])
        expected[PIPELINE_FILE] = current["hashes"][PIPELINE_FILE]
    if "prompts" in draft:
        # Prompt Registry 的父目录由安装/Prompt 模块拥有；本 API 不创建任意目录。
        if not prompt_registry_path(home).parent.is_dir():
            return _error("prompts_root_unavailable", "draft.prompts")
        contents[PROMPT_FILE] = _canonical(draft["prompts"])
        expected[PROMPT_FILE] = current["hashes"][PROMPT_FILE]
    for item in draft.get("files", []):
        path = f"prompts/{item['path']}"
        contents[path] = item["content"]
        file_snapshot = snapshot_files(home, (path,))
        if file_snapshot.get("status") != "ok":
            return _error("snapshot_failed", "draft.files")
        expected[path] = file_snapshot["hashes"][path]

    watch_hashes = dict(current["hashes"])
    watch_hashes.update(prompt_watch_hashes)
    applied = apply_config_transaction(home, contents, expected, kind="console", watch_hashes=watch_hashes)
    if applied.get("status") != "ok":
        code = "snapshot_conflict" if applied.get("error") == "hash_mismatch" else "apply_failed"
        return _error(code, "transaction")
    updated = snapshot(home)
    if updated.get("status") != "ok":
        rollback = rollback_config_transaction(home, applied["backupId"], applied["hashes"])
        result = _error("post_apply_snapshot_failed", "snapshot")
        result["restored"] = rollback.get("status") == "ok" or rollback.get("restored") is True
        result["rollback"] = rollback
        return result
    result = {
        "status": "ok",
        "saved": True,
        "backupId": applied["backupId"],
        "snapshotHash": updated["snapshotHash"],
        "reloadRequired": True,
        "files": diff["changes"],
        "synced": False,
        "syncStatus": "not-requested",
    }
    if not syncOpencode:
        return result

    try:
        pipeline_registry = json.loads(json.dumps(updated["pipeline"]["definition"]))
        prompt_summary = updated["prompts"]
        if prompt_summary["status"] == "unknown":
            prompt_registry = None
            prompt_file_contents = None
        elif prompt_summary["status"] == "ok":
            prompt_registry = json.loads(json.dumps(prompt_summary["registry"]))
            prompt_file_contents = {
                item["path"]: item["content"] for item in prompt_summary["files"]
            }
        else:
            raise ValueError("invalid_prompt_registry")
        sync_snapshot = snapshot(home)
        if (
            sync_snapshot.get("status") != "ok"
            or sync_snapshot.get("snapshotHash") != updated["snapshotHash"]
        ):
            sync_result = {"status": "registry_changed_before_sync"}
        else:
            sync_result = sync_agents_from_registry(
                home, pipeline_registry, prompt_registry, prompt_file_contents
            )
            final_snapshot = snapshot(home)
            if (
                final_snapshot.get("status") != "ok"
                or final_snapshot.get("snapshotHash") != updated["snapshotHash"]
            ):
                sync_result = {"status": "registry_changed_before_sync"}
    except Exception:
        sync_result = {"status": "error"}
    if sync_result.get("status") == "ok":
        result.update({"synced": True, "syncStatus": "ok"})
        return result

    if sync_result.get("status") == "registry_changed_before_sync":
        result.update({
            "syncStatus": "error",
            "syncError": {
                "code": "registry_changed_before_sync",
                "message": "Registry changed before OpenCode agent synchronization",
            },
        })
        return result

    # Registry 已成功保存；同步错误只返回固定安全信息，避免透出宿主配置细节。
    result.update({
        "syncStatus": "error",
        "syncError": {
            "code": "opencode_sync_failed",
            "message": "OpenCode agent synchronization failed",
        },
    })
    return result
