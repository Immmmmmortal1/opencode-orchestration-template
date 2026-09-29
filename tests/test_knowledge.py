from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from orchagent.knowledge import list_knowledge, search_knowledge

from tests.helpers import IsolatedEnv, SENSITIVE_ENV_MARKERS


FILESYSTEM_ID = "test.knowledge.filesystem"
LOCAL_CLI_ID = "test.knowledge.local_cli"


def filesystem_adapter(*, enabled: Any = True) -> dict[str, Any]:
    return {"id": FILESYSTEM_ID, "type": "filesystem", "enabled": enabled}


def local_cli_adapter(
    script: Path,
    *,
    enabled: Any = True,
    command: str | None = None,
) -> dict[str, Any]:
    return {
        "id": LOCAL_CLI_ID,
        "type": "local_cli",
        "enabled": enabled,
        "command": command or sys.executable,
        "args": [str(script)],
    }


def source(
    path: str,
    *,
    source_id: str = "test-source",
    adapter: str = FILESYSTEM_ID,
    enabled: Any = True,
) -> dict[str, Any]:
    return {
        "id": source_id,
        "adapter": adapter,
        "path": path,
        "enabled": enabled,
    }


def registry(
    *,
    adapters: Any = None,
    sources: Any = None,
) -> dict[str, Any]:
    return {
        "version": 1,
        "adapters": [filesystem_adapter()] if adapters is None else adapters,
        "sources": [] if sources is None else sources,
    }


class KnowledgeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()
        self.env.install_home()

    def tearDown(self) -> None:
        self.env.tearDown()

    def write_registry(self, data: dict[str, Any]) -> None:
        self.env.write_registry("knowledge", data)

    def write_raw_registry(self, data: Any) -> None:
        path = self.env.home / "extensions" / "knowledge.yaml"
        path.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")

    def write_script(self, name: str, body: str) -> Path:
        path = self.env.bin_dir / name
        path.write_text(body, encoding="utf-8")
        return path

    def search(self, query: str = "needle") -> dict[str, Any]:
        # local_cli 按产品契约继承父环境，因此测试进程先收敛到最小环境。
        with patch.dict(os.environ, self.env.subprocess_env(), clear=True):
            return search_knowledge(query, self.env.home)

    def assert_registry_error(self, data: Any) -> None:
        """非法 registry 必须让 list/search 同时结构化失败。"""
        self.write_raw_registry(data)
        listed = list_knowledge(self.env.home)
        searched = self.search()
        self.assertEqual("error", listed["status"])
        self.assertEqual("error", searched["status"])
        self.assertEqual([], searched["results"])

    def test_registry_root_must_be_object(self) -> None:
        for value in ([], None, "str", 123):
            with self.subTest(value=value):
                self.assert_registry_error(value)

    def test_registry_rejects_duplicate_ids(self) -> None:
        cases = {
            "adapter": registry(adapters=[filesystem_adapter(), filesystem_adapter()]),
            "source": registry(
                sources=[source("one.md"), source("two.md")],
            ),
        }
        for name, data in cases.items():
            with self.subTest(name=name):
                self.assert_registry_error(data)

    def test_registry_rejects_invalid_adapter_fields(self) -> None:
        invalid_adapters = [
            {"id": "", "type": "filesystem", "enabled": True},
            {"id": 1, "type": "filesystem", "enabled": True},
            {"id": "bad", "type": ["filesystem"], "enabled": True},
            {"id": "bad", "type": "remote", "enabled": True},
            {"id": "bad", "type": "filesystem", "enabled": "true"},
        ]
        for adapter in invalid_adapters:
            with self.subTest(adapter=adapter):
                self.assert_registry_error(registry(adapters=[adapter]))

    def test_registry_rejects_invalid_source_fields(self) -> None:
        invalid_sources = [
            source("file.md", adapter="missing"),
            source(""),
            source("file.md", enabled="true"),
        ]
        for invalid_source in invalid_sources:
            with self.subTest(source=invalid_source):
                self.assert_registry_error(registry(sources=[invalid_source]))

    def test_registry_rejects_non_list_collections(self) -> None:
        for data in (
            registry(adapters={"id": "bad"}),
            registry(sources={"id": "bad"}),
        ):
            with self.subTest(data=data):
                self.assert_registry_error(data)

    def test_invalid_registry_does_not_execute_provider(self) -> None:
        marker = self.env.root / "provider-ran"
        script = self.write_script(
            "marker.py",
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        )
        adapter = local_cli_adapter(script)
        self.assert_registry_error(
            registry(
                adapters=[adapter, dict(adapter)],
                sources=[source("ignored", adapter=LOCAL_CLI_ID)],
            )
        )
        self.assertFalse(marker.exists())

    def test_filesystem_search_returns_source_path_line_and_text(self) -> None:
        notes = self.env.home / "notes"
        notes.mkdir()
        (notes / "one.md").write_text("first\nNeedle in markdown\n", encoding="utf-8")
        (notes / "two.txt").write_text("needle in text\n", encoding="utf-8")
        self.write_registry(registry(sources=[source("notes")]))

        result = self.search()

        self.assertEqual("ok", result["status"])
        self.assertEqual(2, len(result["results"]))
        for row in result["results"]:
            self.assertEqual("test-source", row["source"])
            self.assertIn("path", row)
            self.assertIn("line", row)
            self.assertIn("text", row)

    def test_outside_and_parent_traversal_paths_are_skipped(self) -> None:
        cases = {
            "absolute": str(self.env.external_dir),
            "parent": "../external",
        }
        for name, raw_path in cases.items():
            with self.subTest(name=name):
                self.write_registry(registry(sources=[source(raw_path)]))
                result = self.search()
                self.assertEqual([], result["results"])
                self.assertIn("outside ORCHAGENT_HOME", result["skipped"][0]["reason"])

    def test_sensitive_names_are_skipped(self) -> None:
        paths = [
            "secrets.json",
            "api-keys.md",
            "accounts.yaml",
            ".accounts.yaml",
            ".api-keys.md",
            ".secrets/note.md",
        ]
        for relative in paths:
            with self.subTest(path=relative):
                target = self.env.home / relative
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("needle\n", encoding="utf-8")
                self.write_registry(registry(sources=[source(relative)]))
                result = self.search()
                self.assertEqual([], result["results"])
                self.assertIn("sensitive", result["skipped"][0]["reason"])

    def test_intermediate_symlink_is_skipped(self) -> None:
        real = self.env.home / "real"
        real.mkdir()
        (real / "x.md").write_text("needle\n", encoding="utf-8")
        (self.env.home / "alias").symlink_to(real, target_is_directory=True)
        self.write_registry(registry(sources=[source("alias/x.md")]))

        result = self.search()

        self.assertEqual([], result["results"])
        self.assertIn("symlink", result["skipped"][0]["reason"])

    def test_file_symlink_to_outside_is_not_read(self) -> None:
        external = self.env.external_dir / "outside.md"
        external.write_text("needle must stay private\n", encoding="utf-8")
        (self.env.home / "link.md").symlink_to(external)
        self.write_registry(registry(sources=[source("link.md")]))

        result = self.search()

        self.assertEqual([], result["results"])
        self.assertTrue(result["skipped"])

    def test_hard_link_is_skipped(self) -> None:
        external = self.env.external_dir / "outside.md"
        external.write_text("needle\n", encoding="utf-8")
        linked = self.env.home / "linked.md"
        os.link(external, linked)
        self.write_registry(registry(sources=[source("linked.md")]))

        result = self.search()

        self.assertEqual([], result["results"])
        self.assertIn("hard links", result["skipped"][0]["reason"])

    def test_missing_source_path_is_an_error(self) -> None:
        self.write_registry(registry(sources=[source("missing.md")]))
        result = self.search()
        self.assertEqual([], result["results"])
        self.assertIn("does not exist", result["errors"][0]["message"])

    def test_empty_query_is_rejected(self) -> None:
        for query in ("", "   ", "\t\n"):
            with self.subTest(query=query):
                result = self.search(query)
                self.assertEqual("error", result["status"])

    def test_oversized_file_is_skipped(self) -> None:
        target = self.env.home / "large.md"
        target.write_text("needle-too-large", encoding="utf-8")
        self.write_registry(registry(sources=[source("large.md")]))
        with patch("orchagent.knowledge.MAX_FILE_BYTES", 4):
            result = self.search()
        self.assertEqual([], result["results"])
        self.assertIn("size limit", result["skipped"][0]["reason"])

    def test_result_limit_is_reported(self) -> None:
        first = self.env.home / "first.md"
        second = self.env.home / "second.md"
        first.write_text("needle one\nneedle two\n", encoding="utf-8")
        second.write_text("needle three\n", encoding="utf-8")
        self.write_registry(
            registry(
                sources=[
                    source("first.md", source_id="first"),
                    source("second.md", source_id="second"),
                ]
            )
        )
        with patch("orchagent.knowledge.MAX_RESULTS", 2):
            result = self.search()
        self.assertEqual(2, len(result["results"]))
        self.assertTrue(any("result limit" in row["reason"] for row in result["skipped"]))

    def test_matching_line_is_truncated(self) -> None:
        target = self.env.home / "line.md"
        target.write_text("needle-abcdefghij\n", encoding="utf-8")
        self.write_registry(registry(sources=[source("line.md")]))
        with patch("orchagent.knowledge.MAX_TEXT_CHARS", 8):
            result = self.search()
        self.assertEqual("needle-a", result["results"][0]["text"])

    def test_scan_entry_limit_is_reported(self) -> None:
        target = self.env.home / "one.md"
        target.write_text("needle\n", encoding="utf-8")
        self.write_registry(registry(sources=[source("one.md")]))
        with patch("orchagent.knowledge.MAX_SCAN_ENTRIES", 0):
            result = self.search()
        self.assertIn("entry limit", result["skipped"][0]["reason"])

    def test_scan_file_limit_is_reported(self) -> None:
        target = self.env.home / "one.md"
        target.write_text("needle\n", encoding="utf-8")
        self.write_registry(registry(sources=[source("one.md")]))
        with patch("orchagent.knowledge.MAX_SCAN_FILES", 0):
            result = self.search()
        self.assertIn("file limit", result["skipped"][0]["reason"])

    def test_total_read_limit_is_reported(self) -> None:
        target = self.env.home / "one.md"
        target.write_text("needle\n", encoding="utf-8")
        self.write_registry(registry(sources=[source("one.md")]))
        with patch("orchagent.knowledge.MAX_TOTAL_READ_BYTES", 3):
            result = self.search()
        self.assertEqual([], result["results"])
        self.assertIn("cumulative read limit", result["skipped"][0]["reason"])

    def test_multiple_sources_share_scan_budget(self) -> None:
        (self.env.home / "first.md").write_text("xxxx", encoding="utf-8")
        (self.env.home / "second.md").write_text("needle", encoding="utf-8")
        self.write_registry(
            registry(
                sources=[
                    source("first.md", source_id="first"),
                    source("second.md", source_id="second"),
                ]
            )
        )
        with patch("orchagent.knowledge.MAX_TOTAL_READ_BYTES", 5):
            result = self.search()
        self.assertEqual([], result["results"])
        self.assertTrue(
            any(
                row["source"] == "second" and "cumulative read limit" in row["reason"]
                for row in result["skipped"]
            )
        )

    def test_disabled_local_cli_never_executes(self) -> None:
        marker = self.env.root / "disabled-ran"
        script = self.write_script(
            "disabled.py",
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
        )
        self.write_registry(registry(adapters=[local_cli_adapter(script, enabled=False)]))

        result = self.search()

        self.assertEqual([], result["results"])
        self.assertIn("disabled", result["skipped"][0]["reason"])
        self.assertFalse(marker.exists())

    def test_missing_local_cli_command_is_an_error(self) -> None:
        script = self.write_script("unused.py", "")
        adapter = local_cli_adapter(script, command="definitely-missing-orchagent-command")
        self.write_registry(registry(adapters=[adapter]))
        result = self.search()
        self.assertIn("command not available", result["errors"][0]["message"])

    def test_local_cli_provider_does_not_receive_sensitive_environment(self) -> None:
        script = self.write_script(
            "environment.py",
            "import json, os\nprint(json.dumps(dict(os.environ)))\n",
        )
        self.write_registry(registry(adapters=[local_cli_adapter(script)]))

        with patch.dict(
            os.environ,
            {
                "REAL_API_KEY": "must-not-leak",
                "CLIENT_SECRET": "must-not-leak",
            },
        ):
            result = self.search()

        provider_env = json.loads(result["results"][0]["text"])
        self.assertNotIn("REAL_API_KEY", provider_env)
        self.assertNotIn("CLIENT_SECRET", provider_env)
        self.assertFalse(
            any(
                marker in name.upper()
                for name in provider_env
                for marker in SENSITIVE_ENV_MARKERS
            )
        )

    def test_nonzero_local_cli_truncates_stderr(self) -> None:
        script = self.write_script(
            "nonzero.py",
            "import sys\nsys.stderr.write('x' * 1000)\nraise SystemExit(7)\n",
        )
        self.write_registry(registry(adapters=[local_cli_adapter(script)]))

        result = self.search()

        self.assertIn("exited with 7", result["errors"][0]["message"])
        self.assertLessEqual(len(result["errors"][0]["stderr"]), 500)

    def test_local_cli_stdout_limit_is_enforced(self) -> None:
        script = self.write_script("stdout.py", "import sys\nsys.stdout.write('x' * 100)\n")
        self.write_registry(registry(adapters=[local_cli_adapter(script)]))
        with patch("orchagent.knowledge.MAX_CLI_STDOUT_BYTES", 16):
            result = self.search()
        self.assertIn("stdout exceeded", result["errors"][0]["message"])

    def test_local_cli_stderr_limit_is_enforced(self) -> None:
        script = self.write_script("stderr.py", "import sys\nsys.stderr.write('x' * 100)\n")
        self.write_registry(registry(adapters=[local_cli_adapter(script)]))
        with patch("orchagent.knowledge.MAX_CLI_STDERR_BYTES", 16):
            result = self.search()
        self.assertIn("stderr exceeded", result["errors"][0]["message"])

    def test_local_cli_timeout_is_reported_without_waiting_fifteen_seconds(self) -> None:
        script = self.write_script("slow.py", "import time\ntime.sleep(60)\n")
        self.write_registry(registry(adapters=[local_cli_adapter(script)]))
        calls = 0

        def fake_monotonic() -> float:
            """首次建立 deadline，后续始终保持超时且不会耗尽。"""
            nonlocal calls
            calls += 1
            return 0.0 if calls == 1 else 16.0

        with patch("orchagent.knowledge.time.monotonic", side_effect=fake_monotonic):
            result = self.search()
        self.assertTrue(any("timed out" in row["message"] for row in result["errors"]))

    def test_local_cli_query_is_passed_as_literal_argv(self) -> None:
        marker = self.env.root / "shell-expanded"
        script = self.write_script(
            "argv.py",
            "import json, sys\nprint(json.dumps(sys.argv[1:]))\n",
        )
        self.write_registry(registry(adapters=[local_cli_adapter(script)]))
        query = f"needle; touch {marker}"

        result = self.search(query)

        self.assertFalse(marker.exists())
        self.assertIn(query, result["results"][0]["text"])

    def test_local_cli_time_does_not_consume_filesystem_deadline(self) -> None:
        script = self.write_script("briefly-slow.py", "import time\ntime.sleep(0.05)\n")
        target = self.env.home / "note.md"
        target.write_text("needle\n", encoding="utf-8")
        self.write_registry(
            registry(
                adapters=[local_cli_adapter(script), filesystem_adapter()],
                sources=[source("note.md")],
            )
        )

        with patch("orchagent.knowledge.MAX_SCAN_SECONDS", 0.02):
            result = self.search()

        self.assertTrue(any(row.get("path") == "note.md" for row in result["results"]))


if __name__ == "__main__":
    unittest.main()
