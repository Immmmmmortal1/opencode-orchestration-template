from __future__ import annotations

import re
from typing import Callable


BUILTIN_SKILL_IMPLEMENTATIONS: dict[str, str] = {
    "orchagent.pipeline.emit-json": "pipeline.emit_json",
    "orchagent.pipeline.assert-json-path-equals": "pipeline.assert_json_path_equals",
}


def _error(code: str, message: str) -> dict:
    return {"status": "error", "error": code, "message": message}


def emit_json(params: object) -> dict:
    """原样产出 value，供 3A 确定性 fixture 使用。"""
    if not isinstance(params, dict) or "value" not in params:
        return _error("invalid_params", "emit-json params must contain value")
    return {"value": params["value"]}


_JSON_PATH = re.compile(r"^\$(?:\.[A-Za-z_][A-Za-z0-9_-]*)+$")


def assert_json_path_equals(params: object, actual: object) -> dict:
    """读取仅含点号键的 JSONPath-lite，并给出确定性断言结果。"""
    if not isinstance(params, dict) or "path" not in params or "equals" not in params:
        return _error("invalid_params", "assert-json-path-equals params must contain path and equals")

    path = params["path"]
    if not isinstance(path, str) or _JSON_PATH.fullmatch(path) is None:
        return _error("unsupported_json_path", "path must use JSONPath-lite form $.a.b")

    value = actual
    found = True
    for key in path[2:].split("."):
        if not isinstance(value, dict) or key not in value:
            found = False
            value = None
            break
        value = value[key]

    expected = params["equals"]
    return {
        "verdict": "pass" if found and value == expected else "fail",
        "evidence": {"path": path, "actual": value, "expected": expected},
    }


_EXECUTORS: dict[str, Callable[..., dict]] = {
    "pipeline.emit_json": emit_json,
    "pipeline.assert_json_path_equals": assert_json_path_equals,
}


def execute_builtin(skill_id: str, params: object, actual: object = None) -> dict:
    """按 catalog 中的稳定实现标识执行 builtin skill。"""
    implementation_id = BUILTIN_SKILL_IMPLEMENTATIONS.get(skill_id)
    if implementation_id is None:
        return _error("builtin_skill_not_found", "builtin skill is not registered")
    executor = _EXECUTORS.get(implementation_id)
    if executor is None:
        return _error("builtin_implementation_not_found", "builtin skill implementation cannot be resolved")
    if implementation_id == "pipeline.assert_json_path_equals":
        return executor(params, actual)
    return executor(params)
