from __future__ import annotations

import argparse
import json
import sys

from .config import validate_all
from .doctor import run_doctor
from .extensions import list_extensions
from .hooks import doctor_hooks, dry_run_hooks, list_hooks
from .install import copy_default_configs, rollback_latest
from .knowledge import list_knowledge, search_knowledge
from .mcp import doctor_mcp, list_mcp
from .opencode import doctor as opencode_doctor
from .opencode import link as opencode_link
from .opencode import unlink as opencode_unlink
from .opencode import sync_agents as opencode_sync_agents
from .opencode import unlink_agents as opencode_unlink_agents
from .paths import DEFAULT_HOME
from .pipeline import doctor_pipelines, list_pipelines, list_roles, run_advance, run_pipeline
from .skills import doctor_skills, list_skills


def print_json(data: object) -> None:
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_install(args: argparse.Namespace) -> int:
    if args.install_cmd == "rollback":
        try:
            restored = rollback_latest(DEFAULT_HOME)
            print_json({"status": "ok", "restored": restored})
            return 0
        except Exception as exc:  # noqa: BLE001
            print_json({"status": "error", "message": str(exc)})
            return 1
    copied = copy_default_configs(DEFAULT_HOME, overwrite=args.force, link_bin=args.link_bin)
    print_json({"status": "ok", "home": str(DEFAULT_HOME), "copied": copied})
    return 0


def cmd_doctor(_: argparse.Namespace) -> int:
    ok, messages = run_doctor(DEFAULT_HOME)
    print_json({"status": "ok" if ok else "error", "home": str(DEFAULT_HOME), "checks": messages})
    return 0 if ok else 1


def cmd_config(args: argparse.Namespace) -> int:
    if args.config_cmd == "validate":
        errors = validate_all(DEFAULT_HOME)
        print_json({"status": "ok" if not errors else "error", "errors": errors})
        return 0 if not errors else 1
    return 2


def cmd_extensions(args: argparse.Namespace) -> int:
    if args.extensions_cmd == "list":
        rows = list_extensions(DEFAULT_HOME)
        if args.type:
            rows = [row for row in rows if row.get("type") == args.type]
        print_json({"status": "ok", "extensions": rows})
        return 0
    return 2


def cmd_opencode(args: argparse.Namespace) -> int:
    if args.opencode_cmd == "doctor":
        result = opencode_doctor(DEFAULT_HOME)
        print_json({"status": "ok" if result.get("linked") else "error", "opencode": result})
        return 0 if result.get("linked") else 1
    if args.opencode_cmd == "link":
        print_json({"status": "ok", "opencode": opencode_link(DEFAULT_HOME)})
        return 0
    if args.opencode_cmd == "unlink":
        result = opencode_unlink_agents(DEFAULT_HOME) if args.agents else opencode_unlink(DEFAULT_HOME)
        print_json({"status": "ok", "opencode": result})
        return 0
    if args.opencode_cmd == "sync-agents":
        result = opencode_sync_agents(DEFAULT_HOME)
        print_json(result)
        return 0 if result.get("status") == "ok" else 1
    if args.opencode_cmd == "rollback":
        restored = rollback_latest(DEFAULT_HOME, kind="opencode")
        print_json({"status": "ok", "restored": restored})
        return 0
    return 2


def cmd_hooks(args: argparse.Namespace) -> int:
    if args.hooks_cmd == "list":
        print_json({"status": "ok", "hooks": list_hooks(DEFAULT_HOME)})
        return 0
    if args.hooks_cmd == "doctor":
        ok, result = doctor_hooks(DEFAULT_HOME)
        print_json({"status": "ok" if ok else "error", "hooks": result})
        return 0 if ok else 1
    if args.hooks_cmd == "run":
        if not args.dry_run:
            print_json({"status": "error", "message": "hooks run currently supports --dry-run only"})
            return 1
        result = dry_run_hooks(args.event, DEFAULT_HOME)
        print_json({"status": "ok", **result})
        return 0
    return 2


def cmd_knowledge(args: argparse.Namespace) -> int:
    if args.knowledge_cmd == "list":
        result = list_knowledge(DEFAULT_HOME)
    elif args.knowledge_cmd == "search":
        result = search_knowledge(args.query, DEFAULT_HOME)
    else:
        return 2
    print_json(result)
    return 0 if result.get("status") == "ok" else 1


def cmd_mcp(args: argparse.Namespace) -> int:
    if args.mcp_cmd == "list":
        result = list_mcp(DEFAULT_HOME)
        print_json(result)
        return 0 if result.get("status") == "ok" else 1
    if args.mcp_cmd == "doctor":
        ok, result = doctor_mcp(DEFAULT_HOME)
        print_json(result)
        return 0 if ok else 1
    return 2


def cmd_skills(args: argparse.Namespace) -> int:
    if args.skills_cmd == "list":
        result = list_skills(DEFAULT_HOME)
        print_json(result)
        return 0 if result.get("status") == "ok" else 1
    if args.skills_cmd == "doctor":
        ok, result = doctor_skills(DEFAULT_HOME)
        print_json(result)
        return 0 if ok else 1
    return 2


def cmd_pipeline(args: argparse.Namespace) -> int:
    if args.pipeline_cmd == "roles":
        if args.pipeline_roles_cmd in {"list", "doctor"}:
            result = list_roles(DEFAULT_HOME)
            print_json(result)
            return 0 if result.get("status") == "ok" else 1
        return 2
    if args.pipeline_cmd == "list":
        result = list_pipelines(DEFAULT_HOME)
        print_json(result)
        return 0 if result.get("status") == "ok" else 1
    if args.pipeline_cmd == "doctor":
        skills_result = list_skills(DEFAULT_HOME)
        skills_lookup = {
            skill["id"]: skill
            for skill in skills_result.get("skills", [])
            if isinstance(skill, dict) and isinstance(skill.get("id"), str)
        }
        ok, result = doctor_pipelines(DEFAULT_HOME, skills_lookup)
        print_json(result)
        return 0 if ok else 1
    if args.pipeline_cmd == "run":
        result = run_pipeline(
            DEFAULT_HOME,
            args.pipeline,
            session_id=args.session_id,
            new_session_summary=args.new_session_summary,
        )
        print_json(result)
        return 0 if result.get("status") == "ok" else 1
    if args.pipeline_cmd == "advance":
        try:
            evidence = json.loads(args.evidence) if args.evidence is not None else None
        except json.JSONDecodeError as exc:
            print_json({"status": "error", "error": "invalid_evidence", "message": str(exc)})
            return 1
        result = run_advance(
            DEFAULT_HOME,
            args.session_id,
            args.verdict,
            evidence=evidence,
        )
        print_json(result)
        return 0 if result.get("status") == "ok" else 1
    return 2


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orchagent")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_install = sub.add_parser("install")
    install_sub = p_install.add_subparsers(dest="install_cmd")
    install_sub.add_parser("rollback")
    p_install.add_argument("--force", action="store_true")
    p_install.add_argument("--link-bin")
    p_install.set_defaults(func=cmd_install)

    p_doctor = sub.add_parser("doctor")
    p_doctor.set_defaults(func=cmd_doctor)

    p_config = sub.add_parser("config")
    config_sub = p_config.add_subparsers(dest="config_cmd", required=True)
    config_sub.add_parser("validate")
    p_config.set_defaults(func=cmd_config)

    p_ext = sub.add_parser("extensions")
    ext_sub = p_ext.add_subparsers(dest="extensions_cmd", required=True)
    p_list = ext_sub.add_parser("list")
    p_list.add_argument("--type", choices=["hooks", "skills", "mcp", "knowledge", "pipeline"])
    p_ext.set_defaults(func=cmd_extensions)

    p_open = sub.add_parser("opencode")
    open_sub = p_open.add_subparsers(dest="opencode_cmd", required=True)
    open_sub.add_parser("doctor")
    open_sub.add_parser("link")
    p_open_unlink = open_sub.add_parser("unlink")
    p_open_unlink.add_argument("--agents", action="store_true")
    open_sub.add_parser("sync-agents")
    open_sub.add_parser("rollback")
    p_open.set_defaults(func=cmd_opencode)

    p_hooks = sub.add_parser("hooks")
    hooks_sub = p_hooks.add_subparsers(dest="hooks_cmd", required=True)
    hooks_sub.add_parser("list")
    hooks_sub.add_parser("doctor")
    p_hooks_run = hooks_sub.add_parser("run")
    p_hooks_run.add_argument("event")
    p_hooks_run.add_argument("--dry-run", action="store_true")
    p_hooks.set_defaults(func=cmd_hooks)

    p_knowledge = sub.add_parser("knowledge")
    knowledge_sub = p_knowledge.add_subparsers(dest="knowledge_cmd", required=True)
    knowledge_sub.add_parser("list")
    p_knowledge_search = knowledge_sub.add_parser("search")
    p_knowledge_search.add_argument("query")
    p_knowledge.set_defaults(func=cmd_knowledge)

    p_mcp = sub.add_parser("mcp")
    mcp_sub = p_mcp.add_subparsers(dest="mcp_cmd", required=True)
    mcp_sub.add_parser("list")
    mcp_sub.add_parser("doctor")
    p_mcp.set_defaults(func=cmd_mcp)

    p_skills = sub.add_parser("skills")
    skills_sub = p_skills.add_subparsers(dest="skills_cmd", required=True)
    skills_sub.add_parser("list")
    skills_sub.add_parser("doctor")
    p_skills.set_defaults(func=cmd_skills)

    p_pipeline = sub.add_parser("pipeline")
    pipeline_sub = p_pipeline.add_subparsers(dest="pipeline_cmd", required=True)
    p_pipeline_roles = pipeline_sub.add_parser("roles")
    pipeline_roles_sub = p_pipeline_roles.add_subparsers(dest="pipeline_roles_cmd", required=True)
    pipeline_roles_sub.add_parser("list")
    pipeline_roles_sub.add_parser("doctor")
    pipeline_sub.add_parser("list")
    pipeline_sub.add_parser("doctor")
    p_pipeline_run = pipeline_sub.add_parser("run")
    p_pipeline_run.add_argument("--pipeline", required=True)
    session_group = p_pipeline_run.add_mutually_exclusive_group(required=True)
    session_group.add_argument("--new-session-summary")
    session_group.add_argument("--session-id")
    p_pipeline_advance = pipeline_sub.add_parser("advance")
    p_pipeline_advance.add_argument("--session-id", required=True)
    p_pipeline_advance.add_argument("--verdict", required=True, choices=["pass", "fail"])
    p_pipeline_advance.add_argument("--evidence")
    p_pipeline.set_defaults(func=cmd_pipeline)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
