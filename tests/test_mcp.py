from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from orchagent.mcp import doctor_mcp, list_mcp

from tests.helpers import IsolatedEnv


ADAPTER_ID = "test.mcp.opencode"


def adapter(
    *,
    adapter_id: Any = ADAPTER_ID,
    adapter_type: Any = "opencode",
    enabled: Any = True,
) -> dict[str, Any]:
    return {"id": adapter_id, "type": adapter_type, "enabled": enabled}


def local_server(
    *,
    server_id: Any = "test.local",
    adapter_id: Any = ADAPTER_ID,
    enabled: Any = True,
    command: Any = None,
) -> dict[str, Any]:
    return {
        "id": server_id,
        "adapter": adapter_id,
        "type": "local",
        "enabled": enabled,
        "command": [sys.executable, "server.py"] if command is None else command,
    }


def remote_server(
    *,
    server_id: Any = "test.remote",
    adapter_id: Any = ADAPTER_ID,
    enabled: Any = True,
    url: Any = "https://example.invalid/mcp",
) -> dict[str, Any]:
    return {
        "id": server_id,
        "adapter": adapter_id,
        "type": "remote",
        "enabled": enabled,
        "url": url,
    }


def registry(*, adapters: Any = None, servers: Any = None) -> dict[str, Any]:
    return {
        "version": 1,
        "adapters": [adapter()] if adapters is None else adapters,
        "servers": [] if servers is None else servers,
    }


class McpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()
        self.env.install_home()

    def tearDown(self) -> None:
        self.env.tearDown()

    def write_registry(self, data: Any) -> None:
        self.env.write_registry("mcp", data)

    def assert_registry_error(self, data: Any) -> None:
        """非法 registry 必须让 list 与 doctor 同时结构化失败。"""
        self.write_registry(data)

        listed = list_mcp(self.env.home)
        ok, diagnosed = doctor_mcp(self.env.home)

        self.assertEqual("error", listed["status"])
        self.assertFalse(ok)
        self.assertTrue(listed["checks"])
        self.assertTrue(diagnosed["checks"])
        self.assertEqual("error", listed["checks"][0]["level"])
        self.assertEqual("error", diagnosed["checks"][0]["level"])

    def test_default_template_list_and_doctor_succeed(self) -> None:
        listed = list_mcp(self.env.home)
        ok, diagnosed = doctor_mcp(self.env.home)

        self.assertEqual("ok", listed["status"])
        self.assertTrue(ok)
        self.assertEqual("notImplemented", listed["runtime"])
        self.assertEqual("notImplemented", diagnosed["runtime"])
        self.assertEqual([], listed["servers"])

    def test_rejects_invalid_and_duplicate_adapters(self) -> None:
        cases = [
            registry(adapters=[adapter(adapter_id="")]),
            registry(adapters=[adapter(adapter_id=1)]),
            registry(adapters=[adapter(), adapter()]),
            registry(adapters=[adapter(adapter_type="filesystem")]),
            registry(adapters=[adapter(enabled="true")]),
        ]
        for data in cases:
            with self.subTest(data=data):
                self.assert_registry_error(data)

    def test_disabled_adapter_does_not_hide_invalid_type(self) -> None:
        self.assert_registry_error(
            registry(adapters=[adapter(adapter_type="filesystem", enabled=False)])
        )

    def test_rejects_invalid_server_identity_and_adapter(self) -> None:
        cases = [
            registry(servers=[local_server(server_id="")]),
            registry(servers=[local_server(), local_server()]),
            registry(servers=[local_server(adapter_id="missing")]),
            registry(servers=[local_server(enabled="true")]),
        ]
        for data in cases:
            with self.subTest(data=data):
                self.assert_registry_error(data)

    def test_http_server_type_is_rejected(self) -> None:
        server = remote_server()
        server["type"] = "http"
        self.assert_registry_error(registry(servers=[server]))

    def test_unknown_server_field_is_rejected(self) -> None:
        server = local_server()
        server["bogus"] = True
        self.assert_registry_error(registry(servers=[server]))

    def test_unknown_registry_field_is_rejected(self) -> None:
        data = registry()
        data["bogus"] = True
        self.assert_registry_error(data)

    def test_unknown_adapter_field_is_rejected(self) -> None:
        invalid_adapter = adapter()
        invalid_adapter["enabld"] = True
        self.assert_registry_error(registry(adapters=[invalid_adapter]))

    def test_rejects_invalid_local_server_fields(self) -> None:
        missing_command = local_server()
        del missing_command["command"]
        with_remote_fields = local_server()
        with_remote_fields.update({"url": "https://example.invalid", "oauth": False, "headers": {}})
        cases = [
            missing_command,
            local_server(command=[]),
            local_server(command="python server.py"),
            local_server(command=[sys.executable, 1]),
            with_remote_fields,
        ]
        for server in cases:
            with self.subTest(server=server):
                self.assert_registry_error(registry(servers=[server]))

    def test_rejects_invalid_remote_server_fields(self) -> None:
        missing_url = remote_server()
        del missing_url["url"]
        invalid_oauth = remote_server()
        invalid_oauth["oauth"] = "false"
        invalid_headers = remote_server()
        invalid_headers["headers"] = []
        with_local_fields = remote_server()
        with_local_fields.update({"command": [sys.executable], "env": {}})
        cases = [
            missing_url,
            remote_server(url=1),
            invalid_oauth,
            invalid_headers,
            with_local_fields,
        ]
        for server in cases:
            with self.subTest(server=server):
                self.assert_registry_error(registry(servers=[server]))

    def test_valid_local_and_remote_servers_succeed(self) -> None:
        local = local_server()
        local["env"] = {"MODE": "test"}
        remote = remote_server()
        remote.update({"oauth": True, "headers": {"X-Test": "value"}})
        self.write_registry(registry(servers=[local, remote]))

        listed = list_mcp(self.env.home)
        ok, diagnosed = doctor_mcp(self.env.home)

        self.assertEqual("ok", listed["status"])
        self.assertTrue(ok)
        self.assertEqual({"test.local", "test.remote"}, {row["id"] for row in listed["servers"]})
        self.assertFalse(any(check["level"] == "error" for check in diagnosed["checks"]))

    def test_registry_root_must_be_object(self) -> None:
        for value in ([], None, "s", 123):
            with self.subTest(value=value):
                self.assert_registry_error(value)

    def test_registry_collections_must_be_lists(self) -> None:
        for data in (
            registry(adapters={"id": "bad"}),
            registry(servers={"id": "bad"}),
        ):
            with self.subTest(data=data):
                self.assert_registry_error(data)

    def test_list_and_doctor_never_start_subprocess(self) -> None:
        self.write_registry(registry(servers=[local_server()]))

        with patch("subprocess.Popen") as popen:
            list_mcp(self.env.home)
            doctor_mcp(self.env.home)

        popen.assert_not_called()

    def test_local_server_command_is_never_executed(self) -> None:
        marker = self.env.root / "mcp-server-ran"
        script = self.env.bin_dir / "marker.py"
        script.write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n",
            encoding="utf-8",
        )
        self.write_registry(
            registry(servers=[local_server(command=[sys.executable, str(script)])])
        )

        list_mcp(self.env.home)
        doctor_mcp(self.env.home)

        self.assertFalse(marker.exists())

    def test_sensitive_header_value_is_not_echoed(self) -> None:
        secret = "Bearer SUPER-SECRET"
        remote = remote_server()
        remote["headers"] = {"Authorization": secret}
        self.write_registry(registry(servers=[remote]))

        listed = list_mcp(self.env.home)
        _, diagnosed = doctor_mcp(self.env.home)
        output = json.dumps([listed, diagnosed], ensure_ascii=False)

        self.assertNotIn(secret, output)


if __name__ == "__main__":
    unittest.main()
