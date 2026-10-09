from __future__ import annotations

import argparse
import json
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

from orchagent.cli import cmd_pipeline
from orchagent.skills import get_skill, list_skill_providers, list_skills

from tests.helpers import IsolatedEnv


BUILTIN_ID = "orchagent.pipeline.emit-json"
ASSERT_ID = "orchagent.pipeline.assert-json-path-equals"


def provider(
    provider_id: str = "orchagent.skills.builtin",
    *,
    provider_type: str = "builtin",
    enabled: bool = True,
    rank: Any = 50,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": provider_id,
        "type": provider_type,
        "enabled": enabled,
        "rank": rank,
        **extra,
    }


def registry(*, providers: Any = None, overrides: Any = None, **extra: Any) -> dict[str, Any]:
    return {
        "version": 2,
        "providers": [provider()] if providers is None else providers,
        "overrides": [] if overrides is None else overrides,
        **extra,
    }


class SkillsV2Tests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()
        self.env.install_home()

    def tearDown(self) -> None:
        self.env.tearDown()

    def write_registry(self, data: dict[str, Any]) -> None:
        self.env.write_registry("skills", data)

    def assert_registry_error(self, data: dict[str, Any], code: str | None = None) -> None:
        self.write_registry(data)
        result = list_skills(self.env.home)
        self.assertEqual("error", result["status"])
        self.assertTrue(any(check["level"] == "error" for check in result["checks"]))
        if code is not None:
            self.assertIn(code, result["checks"][0]["message"])

    def create_filesystem_skill(self, name: str = "sample") -> tuple[Path, str]:
        root = self.env.root / "v2-skills"
        directory = root / name
        directory.mkdir(parents=True)
        content = f"---\nname: {name}\ndescription: v2 sample\nbackend: builtin\n---\nBody\n"
        (directory / "SKILL.md").write_text(content, encoding="utf-8")
        return root, content

    def filesystem_provider(self, root: Path, *, enabled: bool = True) -> dict[str, Any]:
        return provider(
            "custom.fs",
            provider_type="filesystem",
            enabled=enabled,
            rank=100,
            roots=[str(root)],
            allowedBases=[str(root)],
            source="custom",
        )

    def test_valid_v2_lists_builtin_catalog_with_ids(self) -> None:
        self.write_registry(registry())

        result = list_skills(self.env.home)

        self.assertEqual("ok", result["status"])
        self.assertEqual(2, result["version"])
        self.assertEqual("seamCatalog", result["runtime"])
        self.assertIn(BUILTIN_ID, {skill["id"] for skill in result["skills"]})
        self.assertTrue(all(skill["id"] for skill in result["skills"]))

    def test_rejects_non_integer_versions(self) -> None:
        for version in (2.0, True):
            with self.subTest(version=version):
                data = registry()
                data["version"] = version
                self.assert_registry_error(data)

    def test_rejects_unknown_registry_and_provider_fields(self) -> None:
        self.assert_registry_error(registry(bogus=True), "registry_unknown_fields")
        invalid = provider()
        invalid["bogus"] = True
        self.assert_registry_error(registry(providers=[invalid]), "provider_unknown_fields")

    def test_rejects_duplicate_id_bool_rank_and_unknown_type(self) -> None:
        cases = [
            registry(providers=[provider(), provider()]),
            registry(providers=[provider(rank=True)]),
            registry(providers=[provider(provider_type="unknown")]),
        ]
        for data in cases:
            with self.subTest(data=data):
                self.assert_registry_error(data)

    def test_rejects_filesystem_without_allowed_bases(self) -> None:
        self.assert_registry_error(
            registry(providers=[provider("fs", provider_type="filesystem", roots=["/tmp"])]),
        )

    def test_rejects_default_host_roots_without_allowed_bases(self) -> None:
        self.assert_registry_error(
            registry(
                providers=[
                    provider(
                        "host.opencode",
                        provider_type="opencode-host",
                        useDefaultRoots=True,
                        roots=[],
                        allowedBases=[],
                    )
                ]
            )
        )

    def test_host_explicit_symlink_root_without_allowed_bases_is_rejected(self) -> None:
        real = self.env.root / "real-host-skills"
        real.mkdir()
        (real / "escaped.md").write_text(
            "---\nname: escaped\ndescription: escaped\n---\nBody\n",
            encoding="utf-8",
        )
        linked = self.env.root / "linked-host-skills"
        linked.symlink_to(real, target_is_directory=True)
        self.write_registry(
            registry(
                providers=[
                    provider(
                        "host.opencode",
                        provider_type="opencode-host",
                        rank=80,
                        roots=[str(linked)],
                    )
                ]
            )
        )

        result = list_skills(self.env.home)

        self.assertEqual("ok", result["status"])
        self.assertEqual([], result["skills"])
        self.assertFalse(result["complete"])
        self.assertTrue(any("allowed base is a symlink" in item for item in result["warnings"]))

    def test_rejects_non_empty_overrides(self) -> None:
        self.assert_registry_error(registry(overrides=[{"name": "x"}]), "overrides_not_supported")

    def test_disabled_provider_is_reported_but_not_registered(self) -> None:
        root, _ = self.create_filesystem_skill()
        self.write_registry(registry(providers=[self.filesystem_provider(root, enabled=False)]))

        result = list_skills(self.env.home)

        self.assertEqual("disabled", result["providers"][0]["status"])
        self.assertEqual([], result["skills"])

    def test_filesystem_provider_lists_and_gets_skill_content(self) -> None:
        root, content = self.create_filesystem_skill()
        self.write_registry(registry(providers=[self.filesystem_provider(root)]))

        listed = list_skills(self.env.home)
        found = get_skill("sample", self.env.home)
        missing = get_skill("missing", self.env.home)

        self.assertEqual(["sample"], [skill["id"] for skill in listed["skills"]])
        self.assertEqual("ok", found["status"])
        self.assertEqual(content, found["content"])
        self.assertFalse(found["truncated"])
        self.assertEqual(len(content.encode("utf-8")), found["sizeBytes"])
        self.assertEqual({"status": "not_found", "id": "missing"}, missing)

    def test_filesystem_get_truncates_large_content(self) -> None:
        root, _ = self.create_filesystem_skill("large-skill")
        path = root / "large-skill" / "SKILL.md"
        prefix = "---\nname: large-skill\ndescription: v2 sample\n---\n"
        path.write_text(prefix + ("x" * (300 * 1024)), encoding="utf-8")
        self.write_registry(registry(providers=[self.filesystem_provider(root)]))

        found = get_skill("large-skill", self.env.home)

        self.assertEqual("ok", found["status"])
        self.assertTrue(found["truncated"])
        self.assertLessEqual(len(found["content"].encode("utf-8")), 256 * 1024)
        self.assertEqual(path.stat().st_size, found["sizeBytes"])

    def test_v1_list_and_get_compatibility(self) -> None:
        original = list_skills(self.env.home)

        self.assertEqual("notImplemented", original["runtime"])
        self.assertNotIn("version", original)
        self.assertEqual(
            {"status": "unsupported", "message": "skills get requires registry version 2"},
            get_skill("sample", self.env.home),
        )

    def test_list_providers_supports_v2_only(self) -> None:
        self.write_registry(registry())
        result = list_skill_providers(self.env.home)
        self.assertEqual("ok", result["status"])
        self.assertEqual("orchagent.skills.builtin", result["providers"][0]["id"])

        self.env.install_home()
        self.env.write_registry(
            "skills",
            {"version": 1, "adapters": [], "skills": []},
        )
        self.assertEqual("unsupported", list_skill_providers(self.env.home)["status"])

    def test_cli_get_and_providers_exit_codes(self) -> None:
        root, _ = self.create_filesystem_skill()
        self.write_registry(registry(providers=[self.filesystem_provider(root)]))

        found_code, found, _, _ = self.env.run_cli("skills", "get", "sample")
        missing_code, missing, _, _ = self.env.run_cli("skills", "get", "missing")
        providers_code, providers, _, _ = self.env.run_cli("skills", "providers")

        self.assertEqual((0, "ok"), (found_code, found["status"]))
        self.assertEqual((1, "not_found"), (missing_code, missing["status"]))
        self.assertEqual((0, "ok"), (providers_code, providers["status"]))

    def test_cmd_pipeline_doctor_accepts_v2_skill_ids(self) -> None:
        self.write_registry(registry())
        self.env.write_registry(
            "pipeline",
            {
                "version": 1,
                "pipelines": [
                    {
                        "id": "fixture",
                        "revision": "1",
                        "enabled": True,
                        "entryStage": "emit",
                        "stages": [
                            {
                                "id": "emit",
                                "skill": BUILTIN_ID,
                                "maxAttempts": 1,
                                "params": {"value": {"ok": True}},
                                "gates": ["assert-ok"],
                            }
                        ],
                        "gates": [
                            {
                                "id": "assert-ok",
                                "stage": "emit",
                                "evaluator": ASSERT_ID,
                                "params": {"path": "$.value.ok", "equals": True},
                            }
                        ],
                        "edges": [
                            {
                                "from": {
                                    "stage": "emit",
                                    "gate": "assert-ok",
                                    "verdict": "pass",
                                },
                                "to": {"terminal": "succeeded"},
                            },
                            {
                                "from": {
                                    "stage": "emit",
                                    "gate": "assert-ok",
                                    "verdict": "fail",
                                },
                                "to": {"stage": None},
                            },
                        ],
                    }
                ],
            },
        )
        output = StringIO()
        with patch("orchagent.cli.DEFAULT_HOME", self.env.home), redirect_stdout(output):
            code = cmd_pipeline(argparse.Namespace(pipeline_cmd="doctor"))

        payload = json.loads(output.getvalue())
        self.assertEqual(0, code, payload)


if __name__ == "__main__":
    unittest.main()
