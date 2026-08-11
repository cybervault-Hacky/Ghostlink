"""Phase 12 Termux↔portal local store + client tests.

Verifies the local token store security (permissions, symlink, atomicity,
corruption, schema) and that the client never leaks tokens/credentials into
URLs or error messages.
"""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from ghostlink.developer_portal.client import PortalClient, PortalClientError
from ghostlink.developer_portal.store import PortalStore, PortalStoreError


class TestStore:
    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        store = PortalStore(tmp_path)
        store.save({"access_token": "at", "refresh_token": "rt", "developer_id": "dev_x"})
        data = store.load()
        assert data["access_token"] == "at"
        assert data["refresh_token"] == "rt"

    def test_store_permissions(self, tmp_path: Path) -> None:
        store = PortalStore(tmp_path)
        store.save({"access_token": "at"})
        assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((store.directory / "tokens.json").stat().st_mode) == 0o600

    def test_clear(self, tmp_path: Path) -> None:
        store = PortalStore(tmp_path)
        store.save({"access_token": "at"})
        store.clear()
        assert store.load() == {}

    def test_future_schema_rejected(self, tmp_path: Path) -> None:
        store = PortalStore(tmp_path)
        store.save({"access_token": "at"})
        # Corrupt the schema version.
        from ghostlink.storage.json_store import JsonFileStorage

        JsonFileStorage(store.directory / "tokens.json").replace({"v": 99, "data": {}})
        with pytest.raises(PortalStoreError):
            store.load()

    def test_symlink_dir_refused(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "developer_portal"
        link.symlink_to(real, target_is_directory=True)
        with pytest.raises(PortalStoreError):
            PortalStore(tmp_path)


class TestClient:
    def test_bearer_header_never_in_url(self) -> None:
        # Ensure the client sends tokens via header only.
        import inspect

        source = inspect.getsource(PortalClient._request)
        assert "Authorization" in source
        assert "Bearer" in source
        # The URL is built from base + path only; token is never in path.
        assert (
            "access_token" not in source.split("def _request")[1].split("url =")[1].split("\n")[0]
        )

    def test_network_failure_never_success(self) -> None:
        client = PortalClient("http://127.0.0.1:1")  # unreachable port
        with pytest.raises(PortalClientError) as excinfo:
            client.health()
        assert excinfo.value.code == "network"

    def test_error_response_never_leaks_internals(self) -> None:
        # A malformed/non-JSON server response is surfaced without tokens.
        client = PortalClient("http://127.0.0.1:9")
        try:
            client.projects_list("a-secret-token")
            raise AssertionError("expected failure")
        except PortalClientError as exc:
            assert "a-secret-token" not in str(exc)


class _ErrorServer:
    """A tiny local HTTP server that returns a fixed status + JSON body."""

    def __init__(self, status: int, body: dict | None = None) -> None:
        import http.server
        import threading

        self.status = status
        self.body = body or {}
        self._port: int | None = None

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self._reply()

            def do_POST(self) -> None:
                self._reply()

            def _reply(self) -> None:
                payload = json.dumps(self.server._body).encode("utf-8")  # type: ignore[attr-defined]
                self.send_response(self.server._status)  # type: ignore[attr-defined]
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, *args: object) -> None:
                pass

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self._server._status = status  # type: ignore[attr-defined]
        self._server._body = self.body  # type: ignore[attr-defined]
        self._port = int(self._server.server_address[1])
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()


class TestClientHttpErrors:
    """Phase 14 — Termux reliability: HTTP 429/500/503 and network errors."""

    def _expect_error(self, server: _ErrorServer, expected_code: str, expected_status: int) -> None:
        from ghostlink.developer_portal.client import PortalClient, PortalClientError

        client = PortalClient(server.url)
        try:
            client.health()
            raise AssertionError("expected a client error")
        except PortalClientError as exc:
            assert exc.code == expected_code
            assert exc.status == expected_status
            assert "token" not in str(exc).lower()
        finally:
            server.close()

    def test_http_429_rate_limited(self) -> None:
        self._expect_error(
            _ErrorServer(429, {"error": {"code": "rate_limited", "message": "Too many."}}),
            "rate_limited",
            429,
        )

    def test_http_500_internal(self) -> None:
        self._expect_error(
            _ErrorServer(500, {"error": {"code": "internal", "message": "boom"}}),
            "internal",
            500,
        )

    def test_http_503_unavailable(self) -> None:
        self._expect_error(
            _ErrorServer(
                503, {"error": {"code": "rate_limit_store_unavailable", "message": "store down"}}
            ),
            "rate_limit_store_unavailable",
            503,
        )

    def test_http_401_unauthorized_never_success(self) -> None:
        self._expect_error(
            _ErrorServer(401, {"error": {"code": "unauthorized", "message": "invalid session"}}),
            "unauthorized",
            401,
        )

    def test_malformed_server_response_fails_closed(self) -> None:
        # Non-JSON body must not crash or be treated as success.
        import http.server
        import threading

        from ghostlink.developer_portal.client import PortalClient, PortalClientError

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = b"<html>oops</html>"
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *a: object) -> None:
                pass

        srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        th = threading.Thread(target=srv.serve_forever, daemon=True)
        th.start()
        try:
            client = PortalClient(f"http://127.0.0.1:{srv.server_address[1]}")
            with pytest.raises(PortalClientError):
                client.health()
        finally:
            srv.shutdown()
            srv.server_close()


class TestDeveloperOrchestrator:
    def test_prerequisites_check(self) -> None:
        from ghostlink.developer_portal.server import check_prerequisites

        prereqs = check_prerequisites()
        assert prereqs.python_ok is True
        assert prereqs.backend_code_ok is True

    def test_find_available_port(self) -> None:
        from ghostlink.developer_portal.server import find_available_port

        port = find_available_port(8788)
        assert 8788 <= port <= 8888

    def test_dev_servers_state_lifecycle(self, tmp_path: Path) -> None:
        from ghostlink.developer_portal.server import (
            clear_dev_servers_state,
            load_dev_servers_state,
            save_dev_servers_state,
        )

        assert load_dev_servers_state(tmp_path) is None
        state = {
            "backend_pid": 12345,
            "backend_port": 8788,
            "backend_url": "http://127.0.0.1:8788",
            "frontend_pid": 12346,
            "frontend_port": 5173,
            "frontend_url": "http://127.0.0.1:5173",
            "started_at": "2026-08-11T12:00:00Z",
        }
        save_dev_servers_state(tmp_path, state)
        loaded = load_dev_servers_state(tmp_path)
        assert loaded is not None
        assert loaded["backend_pid"] == 12345
        assert loaded["frontend_port"] == 5173

        clear_dev_servers_state(tmp_path)
        assert load_dev_servers_state(tmp_path) is None

    def test_stop_developer_services_empty(self, tmp_path: Path) -> None:
        from ghostlink.developer_portal.server import stop_developer_services

        res = stop_developer_services(tmp_path)
        assert res["stopped"] is True
        assert res["stopped_pids"] == []

    def test_get_developer_status(self, tmp_path: Path) -> None:
        from ghostlink.developer_portal.server import get_developer_status

        status = get_developer_status(tmp_path)
        assert "backend_ready" in status
        assert "frontend_ready" in status
        assert "authenticated" in status
        assert status["authenticated"] is False


class TestPairingFlow:
    def test_pair_complete_client_method(self) -> None:
        import inspect

        source = inspect.getsource(PortalClient.pair_complete)
        assert "/api/v1/developer/auth/pair-complete" in source
        assert "pairing_code" in source
