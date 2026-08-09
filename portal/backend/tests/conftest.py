"""Shared fixtures for Developer Portal backend tests (Phase 10B).

Tests drive the real WSGI application in-process (no live sockets), using
a temp SQLite database and a recording email adapter. All fixtures are
deterministic and isolated.
"""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from portal_server.app import EmailSender, create_wsgi_app
from portal_server.db import Database


class TestClient:
    """A tiny WSGI test client (requests semantics without the dependency)."""

    def __init__(self, app) -> None:
        self.app = app
        self.cookies: dict[str, str] = {}
        self.csrf_token: str | None = None

    def _request(
        self, method: str, path: str, *, json_body: dict | None = None, headers: dict | None = None
    ) -> tuple[int, dict]:
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": "",
            "REMOTE_ADDR": "127.0.0.1",
            "HTTP_COOKIE": "; ".join(f"{k}={v}" for k, v in self.cookies.items()),
            "HTTP_USER_AGENT": "pytest-client/1.0",
            "wsgi.input": io.BytesIO(
                json.dumps(json_body or {}).encode("utf-8") if json_body is not None else b""
            ),
            "CONTENT_LENGTH": str(
                len(json.dumps(json_body or {}).encode("utf-8")) if json_body is not None else 0
            ),
        }
        if headers:
            for name, value in headers.items():
                environ["HTTP_" + name.upper().replace("-", "_")] = value
        if self.csrf_token:
            environ.setdefault("HTTP_X_CSRF_TOKEN", self.csrf_token)
        captured: dict = {}

        def start_response(status: str, headers_list: list) -> None:
            captured["status"] = status.split(" ")[0]
            captured["headers"] = dict(headers_list)

        body = self.app(environ, start_response)
        raw = b"".join(body).decode("utf-8")
        captured["body"] = json.loads(raw) if raw else {}
        for name, value in captured["headers"].items():
            if name.lower() == "set-cookie":
                cookie_part = value.split(";", 1)[0]
                if "=" in cookie_part:
                    k, _, v = cookie_part.partition("=")
                    self.cookies[k] = v
        return int(captured["status"]), captured["body"]

    def get(self, path: str) -> tuple[int, dict]:
        return self._request("GET", path)

    def post(self, path: str, *, json: dict | None = None) -> tuple[int, dict]:
        return self._request("POST", path, json_body=json)

    def patch(self, path: str, *, json: dict | None = None) -> tuple[int, dict]:
        return self._request("PATCH", path, json_body=json)

    def signin(
        self, email: str = "dev@example.com", password: str = "super-secure-pass-123"
    ) -> tuple[int, dict]:
        status, body = self.post("/api/v1/auth/signin", json={"email": email, "password": password})
        if "csrfToken" in body:
            self.csrf_token = body["csrfToken"]
        return status, body


@pytest.fixture()
def portal_app(tmp_path: Path):
    db = Database(tmp_path / "portal.db")
    emails = EmailSender(enabled=True)
    app = create_wsgi_app(db, secure_cookies=False, emails=emails)
    portal = type(
        "Portal", (), {"db": db, "emails": emails, "client": TestClient(app), "app": app}
    )()
    yield portal
    db.close()


def make_active_user(
    portal, *, email: str = "dev@example.com", password: str = "super-secure-pass-123"
) -> None:
    """Sign up + verify email via the recording adapter."""
    status, body = portal.client.post(
        "/api/v1/auth/signup", json={"email": email, "password": password}
    )
    assert status == 201, f"signup failed: {status} {body}"
    sent = portal.emails.sent[-1]
    assert sent["kind"] == "email_verify"
    status, body = portal.client.post("/api/v1/auth/verify", json={"token": sent["token"]})
    assert status == 200 and body.get("verified") is True


def signin_and_set_csrf(
    portal, email: str = "dev@example.com", password: str = "super-secure-pass-123"
) -> None:
    status, body = portal.client.signin(email, password)
    assert status == 200, body
    assert body.get("csrfToken")
