from __future__ import annotations

import argparse
import json
import sys

from .config import validate_all
from .doctor import run_doctor
from .extensions import list_extensions
from .hooks import doctor_hooks, dry_run_hooks, list_hooks
from .install import copy_default_configs, rollback_latest
from .opencode import doctor as opencode_doctor
from .opencode import link as opencode_link
from .opencode import unlink as opencode_unlink
from .paths import DEFAULT_HOME


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
        print_json({"status": "ok", "opencode": opencode_unlink(DEFAULT_HOME)})
        return 0
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
    p_list.add_argument("--type", choices=["hooks", "skills", "mcp", "knowledge"])
    p_ext.set_defaults(func=cmd_extensions)

    p_open = sub.add_parser("opencode")
    open_sub = p_open.add_subparsers(dest="opencode_cmd", required=True)
    open_sub.add_parser("doctor")
    open_sub.add_parser("link")
    open_sub.add_parser("unlink")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
