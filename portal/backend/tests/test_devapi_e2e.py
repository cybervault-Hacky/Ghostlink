"""Phase 12 real-socket E2E: Termux-style client ↔ portal over HTTP.

Uses the real WSGI app over a live socket with the stdlib ``http.client``,
driving the full pairing → token → scoped call → refresh → revocation
journey. No mocks of the critical boundary. Connections are opened and
closed explicitly per request (no pooling) so the test is deterministic.
"""

from __future__ import annotations

import http.client
import json
import threading
from pathlib import Path
from wsgiref.simple_server import make_server

from portal_server.app import create_wsgi_app
from portal_server.db import Database
from portal_server.emailing import DevEmailProvider


class RawClient:
    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = port
        self.cookies: dict[str, str] = {}

    def _request(
        self, method: str, path: str, data: dict | None = None, headers: dict | None = None
    ) -> tuple[int, dict]:
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        try:
            body = json.dumps(data).encode() if data is not None else None
            hdrs = dict(headers or {})
            if self.cookies:
                hdrs["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
            conn.request(method, path, body=body, headers=hdrs)
            resp = conn.getresponse()
            for set_cookie in resp.msg.get_all("Set-Cookie", []):
                name, _, value = set_cookie.partition("=")
                self.cookies[name.strip()] = value.split(";", 1)[0]
            raw = resp.read().decode("utf-8")
            return resp.status, json.loads(raw) if raw else {}
        finally:
            conn.close()

    def post(self, path: str, data: dict, headers: dict | None = None) -> tuple[int, dict]:
        hdrs = {"Content-Type": "application/json"}
        if headers:
            hdrs.update(headers)
        return self._request("POST", path, data, hdrs)

    def get(self, path: str, headers: dict | None = None) -> tuple[int, dict]:
        return self._request("GET", path, headers=headers or {})


def test_full_developer_journey_over_socket(tmp_path: Path) -> None:
    db = Database(tmp_path / "portal.db")
    emails = DevEmailProvider(enabled=True)
    app = create_wsgi_app(db, emails=emails)
    server = make_server("127.0.0.1", 0, app)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    c = RawClient("127.0.0.1", port)
    try:
        # Create + verify an account.
        status, _ = c.post(
            "/api/v1/auth/signup", {"email": "e2e@x.com", "password": "super-secure-pass-123"}
        )
        assert status == 201
        assert c.post("/api/v1/auth/verify", {"token": emails.sent[0]["token"]})[0] == 200

        # Termux device registers a pairing.
        status, pairing = c.post(
            "/api/v1/developer/auth/pair-begin",
            {"device_name": "termux-1", "platform": "termux", "client_version": "0.14.0"},
        )
        assert status == 200 and pairing["pairing_code"].startswith("GL-")

        # Approve pairing (needs a session; open one via the web API).
        status, body = c.post(
            "/api/v1/auth/signin", {"email": "e2e@x.com", "password": "super-secure-pass-123"}
        )
        csrf = body["csrfToken"]
        status, approved = c.post(
            "/api/v1/developer/auth/pair-approve",
            {"pairing_code": pairing["pairing_code"], "scopes": "device:read"},
            headers={"X-CSRF-Token": csrf},
        )
        assert status == 201 and approved["credential_secret"].startswith("gl_dev_")

        # No session now: exchange the credential for tokens.
        status, tokens = c.post(
            "/api/v1/developer/auth/token",
            {
                "credential_id": approved["credential_id"],
                "credential_secret": approved["credential_secret"],
            },
        )
        assert status == 200 and tokens["access_token"]
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}

        # device:read allows devices.
        status, dev = c.get("/api/v1/developer/devices", headers)
        assert status == 200 and dev["devices"]

        # project:read NOT granted -> denied.
        status, _ = c.get("/api/v1/developer/projects", headers)
        assert status == 403

        # Refresh rotates tokens.
        status, new_tokens = c.post(
            "/api/v1/developer/auth/refresh", {"refresh_token": tokens["refresh_token"]}
        )
        assert status == 200 and new_tokens["access_token"]

        # Revoke credential -> cannot mint new tokens.
        db.execute(
            "UPDATE api_credentials SET status='revoked' WHERE credential_id=?",
            (approved["credential_id"],),
        )
        status, _ = c.post(
            "/api/v1/developer/auth/token",
            {
                "credential_id": approved["credential_id"],
                "credential_secret": approved["credential_secret"],
            },
        )
        assert status == 401  # revoked credential cannot mint new tokens
    finally:
        server.shutdown()
        server.server_close()
        db.close()
