from __future__ import annotations

import json
import sys
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from urllib.parse import unquote

from . import console_api
from .console_runtime import list_host_resolution
from .console_txn import list_config_backups, rollback_config_transaction
from .provider_catalog import load_catalog
from .paths import DEFAULT_HOME


DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8138
MAX_REQUEST_BYTES = 1024 * 1024
STATIC_ROOT = Path(__file__).with_name("console_static")


def _http_status(result: dict[str, Any]) -> HTTPStatus:
    if result.get("status") == "ok":
        return HTTPStatus.OK
    if result.get("error") == "snapshot_conflict":
        return HTTPStatus.CONFLICT
    return HTTPStatus.BAD_REQUEST


def make_handler(home: Path = DEFAULT_HOME) -> type[BaseHTTPRequestHandler]:
    console_home = Path(home)

    class ConsoleHandler(BaseHTTPRequestHandler):
        server_version = "orchAgentConsole/1"

        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def _json(self, status: HTTPStatus, payload: object) -> None:
            encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(encoded)

        def _error(self, status: HTTPStatus, code: str) -> None:
            self._json(status, {"status": "error", "error": code})

        def _static(self, name: str, content_type: str) -> None:
            try:
                encoded = (STATIC_ROOT / name).read_bytes()
            except OSError:
                self._error(HTTPStatus.NOT_FOUND, "not_found")
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(encoded)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            self.wfile.write(encoded)

        def _read_json(self) -> Any | None:
            content_type = self.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            if content_type != "application/json":
                self._error(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "application_json_required")
                return None
            raw_length = self.headers.get("Content-Length")
            try:
                length = int(raw_length) if raw_length is not None else -1
            except ValueError:
                length = -1
            if length < 0:
                self._error(HTTPStatus.LENGTH_REQUIRED, "content_length_required")
                return None
            if length > MAX_REQUEST_BYTES:
                self._error(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_too_large")
                return None
            try:
                return json.loads(self.rfile.read(length))
            except (json.JSONDecodeError, UnicodeDecodeError):
                self._error(HTTPStatus.BAD_REQUEST, "invalid_json")
                return None

        def _request_origin_allowed(self, write: bool = False) -> bool:
            port = self.server.server_port
            allowed_hosts = {
                "127.0.0.1",
                "localhost",
                "[::1]",
                f"127.0.0.1:{port}",
                f"localhost:{port}",
                f"[::1]:{port}",
            }
            hosts = self.headers.get_all("Host", [])
            if len(hosts) != 1 or hosts[0] not in allowed_hosts:
                self._error(HTTPStatus.FORBIDDEN, "host_forbidden")
                return False
            if write:
                origin = self.headers.get("Origin")
                allowed_origins = {
                    f"http://127.0.0.1:{port}",
                    f"http://localhost:{port}",
                    f"http://[::1]:{port}",
                }
                if origin is not None and origin not in allowed_origins:
                    self._error(HTTPStatus.FORBIDDEN, "origin_forbidden")
                    return False
            return True

        def do_GET(self) -> None:  # noqa: N802
            if not self._request_origin_allowed():
                return
            path = urlsplit(self.path).path
            if path == "/":
                self._static("index.html", "text/html; charset=utf-8")
            elif path == "/app.js":
                self._static("app.js", "text/javascript; charset=utf-8")
            elif path == "/styles.css":
                self._static("styles.css", "text/css; charset=utf-8")
            elif path == "/api/v1/snapshot":
                result = console_api.snapshot(console_home)
                self._json(_http_status(result), result)
            elif path == "/api/v1/host-resolution":
                snap = console_api.snapshot(console_home)
                if snap.get("status") != "ok":
                    self._json(_http_status(snap), snap)
                    return
                roles = snap.get("pipeline", {}).get("roles", [])
                self._json(HTTPStatus.OK, {"status": "ok", "roles": list_host_resolution(console_home, roles)})
            elif path == "/api/v1/backups":
                result = list_config_backups(console_home)
                self._json(_http_status(result), result)
            elif path == "/api/v1/provider-catalog":
                self._json(HTTPStatus.OK, load_catalog())
            else:
                self._error(HTTPStatus.NOT_FOUND, "not_found")

        def do_POST(self) -> None:  # noqa: N802
            if not self._request_origin_allowed(write=True):
                return
            path = urlsplit(self.path).path
            rollback_prefix = "/api/v1/backups/"
            rollback_suffix = "/rollback"
            if path.startswith(rollback_prefix) and path.endswith(rollback_suffix):
                encoded_id = path[len(rollback_prefix) : -len(rollback_suffix)]
                backup_id = unquote(encoded_id)
                if not backup_id or "/" in backup_id or backup_id in {".", ".."}:
                    self._error(HTTPStatus.BAD_REQUEST, "invalid_backup_id")
                    return
                payload = self._read_json()
                if payload is None:
                    return
                if not isinstance(payload, dict) or set(payload) != {"expectedHashes"}:
                    result = {"status": "error", "error": "invalid_request"}
                else:
                    expected_hashes = payload.get("expectedHashes")
                    if (
                        not isinstance(expected_hashes, dict)
                        or not expected_hashes
                        or any(
                            not isinstance(key, str) or (value is not None and not isinstance(value, str))
                            for key, value in expected_hashes.items()
                        )
                    ):
                        result = {"status": "error", "error": "invalid_request"}
                    else:
                        result = rollback_config_transaction(console_home, backup_id, expected_hashes)
                self._json(_http_status(result), result)
                return
            operations = {
                "/api/v1/draft/validate": console_api.validate_draft,
                "/api/v1/draft/preview": console_api.preview_draft,
                "/api/v1/draft/diff": console_api.diff_draft,
            }
            if path not in operations and path != "/api/v1/draft/apply":
                self._error(HTTPStatus.NOT_FOUND, "not_found")
                return
            payload = self._read_json()
            if payload is None:
                return
            if path == "/api/v1/draft/apply":
                if not isinstance(payload, dict) or set(payload) != {"draft", "snapshotHash", "confirmDiffId"}:
                    result = {"status": "error", "error": "invalid_request"}
                else:
                    result = console_api.apply_draft(
                        console_home,
                        payload.get("draft"),
                        payload.get("snapshotHash"),
                        payload.get("confirmDiffId"),
                    )
            else:
                draft = payload.get("draft") if isinstance(payload, dict) and set(payload) == {"draft"} else payload
                result = operations[path](console_home, draft)
            self._json(_http_status(result), result)

    return ConsoleHandler


def serve(host: str = DEFAULT_HOST, port: int = DEFAULT_PORT, home: Path = DEFAULT_HOME) -> None:
    if host != DEFAULT_HOST:
        raise ValueError("console_host_must_be_loopback")
    with ThreadingHTTPServer((host, port), make_handler(home)) as server:
        actual_host, actual_port = server.server_address[:2]
        print(f"orchAgent console listening on http://{actual_host}:{actual_port}", file=sys.stderr, flush=True)
        server.serve_forever()
