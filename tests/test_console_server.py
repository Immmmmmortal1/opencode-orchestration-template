from __future__ import annotations

import http.client
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from orchagent.console_server import MAX_REQUEST_BYTES, make_handler
from orchagent.console_txn import apply_config_transaction, file_digest
from tests.helpers import IsolatedEnv


class ConsoleServerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.env = IsolatedEnv().setUp()
        self.env.install_home()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(self.env.home))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.env.tearDown()

    def request(self, method: str, path: str, body: bytes | None = None, headers: dict[str, str] | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=3)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, payload

    def test_snapshot_returns_json(self) -> None:
        status, body = self.request("GET", "/api/v1/snapshot")
        self.assertEqual(200, status)
        self.assertEqual("ok", json.loads(body)["status"])

    def test_provider_catalog_is_loopback_read_only_endpoint(self) -> None:
        status, body = self.request("GET", "/api/v1/provider-catalog")
        self.assertEqual(200, status)
        payload = json.loads(body)
        self.assertEqual("ok", payload["status"])
        self.assertNotIn("apiKey", body.decode())
        self.assertNotIn("token", body.decode().lower())

    def test_pipeline_registry_endpoint_is_not_exposed(self) -> None:
        status, body = self.request("GET", "/api/v1/pipeline-registry")
        self.assertEqual(404, status)
        self.assertEqual("not_found", json.loads(body)["error"])

    def test_rejects_untrusted_host_and_origin(self) -> None:
        status, body = self.request("GET", "/api/v1/snapshot", headers={"Host": "evil.example"})
        self.assertEqual(403, status)
        self.assertEqual("host_forbidden", json.loads(body)["error"])

        payload = json.dumps({"pipeline": self.env.home.as_posix()}).encode()
        status, body = self.request(
            "POST",
            "/api/v1/draft/validate",
            payload,
            {"Content-Type": "application/json", "Origin": "http://evil.example"},
        )
        self.assertEqual(403, status)
        self.assertEqual("origin_forbidden", json.loads(body)["error"])

    def test_accepts_loopback_host_and_origin(self) -> None:
        port = self.server.server_port
        status, body = self.request("GET", "/api/v1/snapshot", headers={"Host": f"localhost:{port}"})
        self.assertEqual(200, status)
        self.assertEqual("ok", json.loads(body)["status"])

        payload = json.dumps({"pipeline": {}}).encode()
        status, _ = self.request(
            "POST",
            "/api/v1/draft/validate",
            payload,
            {
                "Content-Type": "application/json",
                "Host": f"127.0.0.1:{port}",
                "Origin": f"http://127.0.0.1:{port}",
            },
        )
        self.assertEqual(400, status)

    def test_validate_rejects_non_json_and_oversized_body(self) -> None:
        status, _ = self.request("POST", "/api/v1/draft/validate", b"{}", {"Content-Type": "text/plain"})
        self.assertEqual(415, status)
        status, body = self.request(
            "POST",
            "/api/v1/draft/validate",
            b"",
            {"Content-Type": "application/json", "Content-Length": str(MAX_REQUEST_BYTES + 1)},
        )
        self.assertEqual(413, status)
        self.assertEqual("request_too_large", json.loads(body)["error"])

    def test_validate_rejects_sensitive_field_without_echo(self) -> None:
        marker = "never-echo-this-value"
        payload = json.dumps({"api_key": marker}).encode()
        status, body = self.request(
            "POST", "/api/v1/draft/validate", payload, {"Content-Type": "application/json"}
        )
        self.assertEqual(400, status)
        self.assertEqual("sensitive_data", json.loads(body)["error"])
        self.assertNotIn(marker.encode(), body)

    def test_static_index_has_no_sensitive_inputs_or_examples(self) -> None:
        status, body = self.request("GET", "/")
        self.assertEqual(200, status)
        text = body.decode().lower()
        self.assertNotIn("api_key", text)
        self.assertNotIn("name=\"env\"", text)
        self.assertNotIn("name=\"source\"", text)

    def test_static_save_actions_expose_save_only_and_safe_opencode_sync(self) -> None:
        status, body = self.request("GET", "/")
        self.assertEqual(200, status)
        text = body.decode()
        self.assertIn("仅保存", text)
        self.assertIn("保存并同步到 OpenCode", text)

        status, body = self.request("GET", "/app.js")
        self.assertEqual(200, status)
        script = body.decode()
        self.assertIn("syncOpencode", script)
        self.assertIn("result.syncError.message", script)

    def test_apply_requires_exact_payload_and_confirm_diff_id(self) -> None:
        base = {"draft": {"pipeline": {}}, "snapshotHash": "hash"}
        invalid_payloads = (
            base,
            {**base, "confirmDiffId": "diff"},
            {**base, "confirmDiffId": "diff", "syncOpencode": False, "extra": True},
            {**base, "confirmDiffId": "diff", "syncOpencode": 0},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                status, body = self.request(
                    "POST", "/api/v1/draft/apply", json.dumps(payload).encode(), {"Content-Type": "application/json"}
                )
                self.assertEqual(400, status)
                self.assertEqual("invalid_request", json.loads(body)["error"])

    def test_apply_sync_partial_success_remains_http_success(self) -> None:
        result = {
            "status": "ok",
            "saved": True,
            "synced": False,
            "syncStatus": "error",
            "syncError": {"code": "opencode_sync_failed", "message": "OpenCode agent synchronization failed"},
        }
        payload = {
            "draft": {"pipeline": {}},
            "snapshotHash": "hash",
            "confirmDiffId": "diff",
            "syncOpencode": True,
        }
        with mock.patch("orchagent.console_server.console_api.apply_draft", return_value=result) as apply:
            status, body = self.request(
                "POST",
                "/api/v1/draft/apply",
                json.dumps(payload).encode(),
                {"Content-Type": "application/json"},
            )

        self.assertEqual(200, status)
        self.assertEqual(result, json.loads(body))
        apply.assert_called_once_with(self.env.home, payload["draft"], "hash", "diff", True)

    def test_backup_list_and_rollback_endpoint(self) -> None:
        target = self.env.home / "extensions/pipeline.yaml"
        original = target.read_text(encoding="utf-8")
        applied = apply_config_transaction(
            self.env.home,
            {"extensions/pipeline.yaml": "changed"},
            {"extensions/pipeline.yaml": file_digest(target)},
        )
        self.assertEqual("ok", applied["status"])

        status, body = self.request("GET", "/api/v1/backups")
        self.assertEqual(200, status)
        backups = json.loads(body)["backups"]
        self.assertEqual(applied["backupId"], backups[0]["backupId"])
        self.assertNotIn("backupDir", backups[0])

        payload = json.dumps({"expectedHashes": applied["hashes"]}).encode()
        status, body = self.request(
            "POST",
            f"/api/v1/backups/{applied['backupId']}/rollback",
            payload,
            {"Content-Type": "application/json"},
        )
        self.assertEqual(200, status)
        self.assertTrue(json.loads(body)["consumed"])
        self.assertEqual(original, target.read_text(encoding="utf-8"))

        status, body = self.request(
            "POST",
            f"/api/v1/backups/{applied['backupId']}/rollback",
            payload,
            {"Content-Type": "application/json"},
        )
        self.assertEqual(400, status)
        self.assertEqual("manifest_consumed", json.loads(body)["error"])

    def test_rollback_rejects_path_traversal_id(self) -> None:
        for backup_id in ("..", "%2e%2e", "%2fetc"):
            with self.subTest(backup_id=backup_id):
                status, body = self.request(
                    "POST",
                    f"/api/v1/backups/{backup_id}/rollback",
                    b"{}",
                    {"Content-Type": "application/json"},
                )
                self.assertEqual(400, status)
                self.assertEqual("invalid_backup_id", json.loads(body)["error"])

    def test_rollback_requires_nonempty_typed_expected_hashes(self) -> None:
        target = self.env.home / "extensions/pipeline.yaml"
        applied = apply_config_transaction(
            self.env.home,
            {"extensions/pipeline.yaml": "changed"},
            {"extensions/pipeline.yaml": file_digest(target)},
        )
        invalid_payloads = (
            {},
            {"expectedHashes": {}},
            {"expectedHashes": None},
            {"expectedHashes": []},
            {"expectedHashes": {"path": 1}},
            {"expectedHashes": {"path": "hash"}, "extra": True},
        )
        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                status, body = self.request(
                    "POST",
                    f"/api/v1/backups/{applied['backupId']}/rollback",
                    json.dumps(payload).encode(),
                    {"Content-Type": "application/json"},
                )
                self.assertEqual(400, status)
                self.assertEqual("invalid_request", json.loads(body)["error"])

    def test_console_docs_do_not_claim_opencode_http_endpoints(self) -> None:
        docs = (Path(__file__).parents[1] / "docs/console.md").read_text(encoding="utf-8")
        self.assertNotIn("/api/v1/opencode/preview", docs)
        self.assertNotIn("/api/v1/opencode/sync", docs)


if __name__ == "__main__":
    unittest.main()
