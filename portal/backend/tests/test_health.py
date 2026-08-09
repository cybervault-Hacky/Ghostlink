"""Phase 13 — health / readiness / liveness endpoints and proxy/host tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from portal_server.config import load_config
from portal_server.db import Database


@pytest.fixture
def app(tmp_path: Path):
    from portal_server.app import create_wsgi_app
    from portal_server.emailing import DevEmailProvider

    db = Database(tmp_path / "p.db")
    cfg = load_config({"APP_ENV": "development", "DATABASE_URL": str(tmp_path / "p.db")})
    app = create_wsgi_app(db, emails=DevEmailProvider(enabled=False), config=cfg)
    return app, db


def _get(app, path: str, host: str | None = None, headers: dict | None = None):
    import io

    environ = {
        "REQUEST_METHOD": "GET",
        "PATH_INFO": path,
        "QUERY_STRING": "",
        "REMOTE_ADDR": "127.0.0.1",
        "wsgi.input": io.BytesIO(b""),
        "CONTENT_LENGTH": "0",
    }
    if host:
        environ["HTTP_HOST"] = host
    if headers:
        environ.update(headers)
    captured: dict = {}

    def start_response(status: str, headers_list: list) -> None:
        captured["status"] = status.split(" ")[0]
        captured["headers"] = dict(headers_list)

    body = app(environ, start_response)
    return int(captured["status"]), b"".join(body), captured["headers"]


def test_liveness(app) -> None:
    a, _ = app
    status, body, _ = _get(a, "/health/live")
    assert status == 200
    assert b"alive" in body


def test_readiness(app) -> None:
    a, _ = app
    status, body, _ = _get(a, "/health/ready")
    assert status == 200
    assert b"ready" in body


def test_health_summary_is_safe(app) -> None:
    a, _ = app
    status, body, _ = _get(a, "/health")
    assert status == 200
    text = body.decode()
    assert "database_backend" in text
    assert "schema_version" in text
    # Never leak secrets / URLs.
    assert "DATABASE_URL" not in text
    assert "password" not in text.lower()


def test_health_returns_request_id_header(app) -> None:
    a, _ = app
    _status, _body, headers = _get(a, "/health")
    assert "X-Request-ID" in headers
    assert headers["X-Request-ID"].startswith("req_")


def test_request_id_respects_client_value(app) -> None:
    a, _ = app
    _status, _body, headers = _get(a, "/health", headers={"HTTP_X_REQUEST_ID": "client-trace-1"})
    assert headers["X-Request-ID"] == "client-trace-1"


def test_host_allowed_list_enforced(tmp_path: Path) -> None:
    from portal_server.app import create_wsgi_app
    from portal_server.emailing import DevEmailProvider

    db = Database(tmp_path / "h.db")
    cfg = load_config(
        {
            "APP_ENV": "production",
            "DATABASE_URL": str(tmp_path / "h.db"),
            "SESSION_SECRET": "a-long-random-secret-123",
            "EMAIL_PROVIDER": "smtp",
            "EMAIL_SMTP_HOST": "smtp.example.com",
            "PORTAL_SECURE_COOKIES": "true",
            "WEBAUTHN_RP_ID": "portal.example.com",
            "WEBAUTHN_ORIGIN": "https://portal.example.com",
            "ALLOWED_HOSTS": "portal.example.com",
        }
    )
    app = create_wsgi_app(db, emails=DevEmailProvider(enabled=False), config=cfg)
    status, _body, _ = _get(app, "/health", host="portal.example.com")
    assert status == 200
    status, _body, _ = _get(app, "/health", host="evil.example.com")
    assert status == 403
    status, _body, _ = _get(app, "/health", host=None)
    assert status == 403


def test_proxy_ip_only_trusted_when_configured(app) -> None:
    from portal_server.http import Request

    environ = {
        "REMOTE_ADDR": "10.1.1.1",
        "HTTP_X_FORWARDED_FOR": "203.0.113.9, 10.0.0.1",
        "REQUEST_METHOD": "GET",
        "PATH_INFO": "/",
    }
    # Untrusted: X-Forwarded-For ignored.
    req = Request(environ)
    assert req.client_ip() == "10.1.1.1"
    # Trusted proxy flagged: forwarded header honoured.
    environ["ghostlink.trusted_proxy"] = "1"
    req2 = Request(environ)
    assert req2.client_ip() == "203.0.113.9"
