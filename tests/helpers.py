from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping

from orchagent.install import copy_default_configs


PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLI_PATH = PROJECT_ROOT / "bin" / "orchagent"
REGISTRY_KINDS = frozenset({"hooks", "skills", "mcp", "knowledge"})
SUBPROCESS_ENV_ALLOWLIST = frozenset(
    {
        "PATH",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "TMPDIR",
        "TEMP",
        "TMP",
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
    }
)
SENSITIVE_ENV_MARKERS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PROXY", "API")


def read_json(stdout: str) -> Any:
    """解析 CLI 标准输出中的 JSON。"""
    return json.loads(stdout)


class IsolatedEnv:
    """为 unittest 提供不会接触真实用户目录的临时运行环境。"""

    def __init__(self) -> None:
        self._temporary_directory: tempfile.TemporaryDirectory[str] | None = None
        self.root: Path
        self.home: Path
        self.opencode_config: Path
        self.fake_home: Path
        self.bin_dir: Path
        self.external_dir: Path

    def setUp(self) -> IsolatedEnv:
        """创建隔离目录；可直接从 unittest.TestCase.setUp 调用。"""
        if self._temporary_directory is not None:
            self.tearDown()

        self._temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self._temporary_directory.name)
        self.home = self.root / "home"
        self.opencode_config = self.root / "opencode.json"
        self.fake_home = self.root / "fake-home"
        self.bin_dir = self.root / "bin"
        self.external_dir = self.root / "external"

        for directory in (self.home, self.fake_home, self.bin_dir, self.external_dir):
            directory.mkdir(parents=True)
        self.opencode_config.write_text("{}\n", encoding="utf-8")
        return self

    def tearDown(self) -> None:
        """清理隔离目录；可直接从 unittest.TestCase.tearDown 调用。"""
        if self._temporary_directory is not None:
            self._temporary_directory.cleanup()
            self._temporary_directory = None

    def __enter__(self) -> IsolatedEnv:
        return self.setUp()

    def __exit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        self.tearDown()

    def install_home(self) -> list[str]:
        """把项目默认模板安装到隔离的 ORCHAGENT_HOME。"""
        self._require_setup()
        return copy_default_configs(self.home, overwrite=False, link_bin=None)

    def run_cli(
        self,
        *args: str,
        env_overrides: Mapping[str, str] | None = None,
    ) -> tuple[int, Any | None, str, str]:
        """在隔离环境中运行 CLI，并尽力解析其 JSON 标准输出。"""
        self._require_setup()
        env = self.subprocess_env(env_overrides)
        result = subprocess.run(
            [sys.executable, str(CLI_PATH), *args],
            cwd=PROJECT_ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            parsed = read_json(result.stdout) if result.stdout.strip() else None
        except json.JSONDecodeError:
            parsed = None
        return result.returncode, parsed, result.stdout, result.stderr

    def subprocess_env(
        self,
        env_overrides: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        """构造最小子进程环境，不继承宿主机凭据或代理配置。"""
        self._require_setup()
        env = {
            name: value
            for name, value in os.environ.items()
            if name in SUBPROCESS_ENV_ALLOWLIST and not _is_sensitive_env_name(name)
        }
        if env_overrides:
            env.update(
                {
                    name: value
                    for name, value in env_overrides.items()
                    if not _is_sensitive_env_name(name)
                }
            )
        # 最后覆盖关键路径，避免调用方意外逃逸到真实用户目录。
        env.update(
            {
                "ORCHAGENT_HOME": str(self.home),
                "OPENCODE_CONFIG": str(self.opencode_config),
                "HOME": str(self.fake_home),
            }
        )
        return env

    def write_registry(self, kind: str, data: Mapping[str, Any]) -> Path:
        """以 JSON 兼容 YAML 格式写入隔离环境中的扩展 registry。"""
        self._require_setup()
        if kind not in REGISTRY_KINDS:
            raise ValueError(f"unknown registry kind: {kind}")
        path = self.home / "extensions" / f"{kind}.yaml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(data, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        return path

    def _require_setup(self) -> None:
        if self._temporary_directory is None:
            raise RuntimeError("IsolatedEnv.setUp() must be called first")


def _is_sensitive_env_name(name: str) -> bool:
    upper_name = name.upper()
    return any(marker in upper_name for marker in SENSITIVE_ENV_MARKERS)
