from __future__ import annotations

import unittest

from orchagent.console_runtime import list_host_resolution, resolve_host_status


class ConsoleRuntimeTests(unittest.TestCase):
    def test_default_resolution_is_unknown_and_bounded(self) -> None:
        result = resolve_host_status(provider="openai", model="gpt")
        self.assertEqual(
            {"provider": "openai", "model": "gpt", "resolvedStatus": "unknown"},
            result,
        )

    def test_adapter_error_and_invalid_status_degrade_to_unknown(self) -> None:
        class Broken:
            def resolve(self, provider: str, model: str | None) -> str:
                raise RuntimeError("private source")

        class Invalid:
            def resolve(self, provider: str, model: str | None) -> str:
                return "configured"

        roles = [{"provider": "p", "model": "m", "ignored": "value"}]
        self.assertEqual("unknown", list_host_resolution(roles=roles, adapter=Broken())[0]["resolvedStatus"])
        result = list_host_resolution(roles=roles, adapter=Invalid())[0]
        self.assertEqual({"provider", "model", "resolvedStatus"}, set(result))
        self.assertIn(result["resolvedStatus"], {"resolved", "unresolved", "unknown"})


if __name__ == "__main__":
    unittest.main()
