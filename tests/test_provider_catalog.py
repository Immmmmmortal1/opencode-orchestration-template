from __future__ import annotations

import json
import os
import unittest
from unittest import mock

from orchagent.provider_catalog import load_catalog
from tests.helpers import IsolatedEnv


class ProviderCatalogTests(unittest.TestCase):
    def setUp(self) -> None:
        # OPENCODE_CONFIG 的进程内隔离由 IsolatedEnv 统一负责（setUp/tearDown），此处不再重复设置。
        self.env = IsolatedEnv().setUp()

    def tearDown(self) -> None:
        self.env.tearDown()

    def test_catalog_is_sorted_and_does_not_echo_sensitive_values(self) -> None:
        marker = "never-return-this-catalog-secret"
        self.env.opencode_config.write_text(json.dumps({
            "provider": {
                "zeta": {"name": "Zeta", "options": {"apiKey": marker}, "models": {
                    "b": {"name": "Beta", "token": marker},
                    "a": {"name": "Alpha"},
                }},
                "alpha": {"models": {"x": {"name": "X"}}},
            }
        }), encoding="utf-8")
        result = load_catalog()
        self.assertEqual("ok", result["status"])
        self.assertEqual(["alpha", "zeta"], [item["id"] for item in result["providers"]])
        self.assertEqual(["a", "b"], [item["id"] for item in result["providers"][1]["models"]])
        self.assertNotIn(marker, json.dumps(result))
        self.assertNotIn("options", json.dumps(result))

    def test_catalog_filters_sensitive_ids_and_names(self) -> None:
        self.env.opencode_config.write_text(json.dumps({
            "provider": {
                "safe": {
                    "name": "$OPENAI_URL",
                    "models": {
                        "safe-model": {"name": "$MODEL_NAME"},
                        "api_key_model": {"name": "Bearer hidden-value"},
                    },
                },
                "api_key_provider": {"name": "Should not be returned", "models": {}},
            }
        }), encoding="utf-8")

        result = load_catalog()

        self.assertEqual(["safe"], [item["id"] for item in result["providers"]])
        provider = result["providers"][0]
        self.assertEqual("safe", provider["name"])
        self.assertEqual([{"id": "safe-model", "name": "safe-model"}], provider["models"])

    def test_invalid_or_missing_config_is_unavailable_without_exception_details(self) -> None:
        self.env.opencode_config.write_text("{invalid", encoding="utf-8")
        result = load_catalog()
        self.assertEqual({"status": "ok", "providers": [], "sourceStatus": "unavailable"}, result)

    def test_default_sources_merge_and_later_source_overrides_same_provider(self) -> None:
        os.environ.pop("OPENCODE_CONFIG", None)
        config_dir = self.env.fake_home / ".config"
        config_dir.mkdir()
        first = config_dir / "opencode.json"
        second = self.env.fake_home / ".config" / "opencode.local.json"
        first.write_text(json.dumps({"provider": {"same": {"name": "First"}, "only-first": {}}}), encoding="utf-8")
        second.write_text(json.dumps({"provider": {"same": {"name": "Second", "models": {"m": {}}}, "only-second": {}}}), encoding="utf-8")
        with mock.patch(
            "orchagent.provider_catalog.candidate_config_paths",
            return_value=[first, second],
        ):
            result = load_catalog()
        providers = {item["id"]: item for item in result["providers"]}
        self.assertEqual("same", providers["same"]["name"])
        self.assertIn("only-first", providers)
        self.assertIn("only-second", providers)


if __name__ == "__main__":
    unittest.main()
