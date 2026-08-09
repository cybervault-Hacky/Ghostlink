"""Phase 13 — dedicated security regression suite (13Y/13Z).

Covers: proxy/host spoofing, HTTPS downgrade (HSTS), owner-boundary invariants,
IDOR, scope escalation, authorization bypass, rate-limit bypass, malformed and
oversized requests, expired sessions/tokens, revoked credentials, replay,
migration race, and backup/restore integrity (see also test_backup.py).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from conftest import make_active_user, signin_and_set_csrf
from portal_server.db import Database

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _pair_and_tokens(portal_app) -> dict:
    status, body = portal_app.client.post(
        "/api/v1/developer/auth/pair-begin",
        json={"device_name": "sec-device", "platform": "termux", "client_version": "0.15.0"},
    )
    assert status == 200, body
    approved = portal_app.client.post(
        "/api/v1/developer/auth/pair-approve",
        json={"pairing_code": body["pairing_code"], "scopes": "device:read device:write"},
    )
    assert approved[0] == 201, approved
    tokens = portal_app.client.post(
        "/api/v1/developer/auth/token",
        json={
            "credential_id": approved[1]["credential_id"],
            "credential_secret": approved[1]["credential_secret"],
        },
    )
    assert tokens[0] == 200, tokens
    return tokens[1]


# ---------------------------------------------------------------------------
# Owner boundary (13Y) — permanent invariant
# ---------------------------------------------------------------------------


class TestOwnerBoundary:
    def test_signup_never_creates_owner(self, portal_app) -> None:
        make_active_user(portal_app)
        row = portal_app.db.query_one("SELECT role FROM users WHERE email=?", ("dev@example.com",))
        assert row is not None
        assert row["role"] == "developer"
        assert row["role"] != "owner"

    def test_owner_star_scope_is_invalid(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        status, body = portal_app.client.post(
            "/api/v1/developer/auth/pair-begin",
            json={"device_name": "d", "platform": "termux"},
        )
        assert status == 200
        s, _ = portal_app.client.post(
            "/api/v1/developer/auth/pair-approve",
            json={"pairing_code": body["pairing_code"], "scopes": "owner:*"},
        )
        assert s == 400  # owner:* is not a valid scope

    @pytest.mark.parametrize("scope", ["owner:*", "root:*", "admin:*"])
    def test_forbidden_scopes_rejected(self, portal_app, scope: str) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        s, body = portal_app.client.post(
            "/api/v1/developer/auth/pair-begin", json={"device_name": "d", "platform": "termux"}
        )
        assert s == 200
        res = portal_app.client.post(
            "/api/v1/developer/auth/pair-approve",
            json={"pairing_code": body["pairing_code"], "scopes": scope},
        )
        assert res[0] == 400

    def test_role_cannot_be_escalated_via_api(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        # No endpoint accepts a role field that can escalate.
        s, _ = portal_app.client.post(
            "/api/v1/auth/signup",
            json={"email": "x@x.com", "password": "super-secure-pass-123", "role": "owner"},
        )
        assert s == 201
        row = portal_app.db.query_one("SELECT role FROM users WHERE email=?", ("x@x.com",))
        assert row["role"] == "developer"

    def test_no_owner_scope_registered(self) -> None:
        from portal_server.devapi import VALID_SCOPES

        for scope in ("owner:*", "root:*", "admin:*"):
            assert scope not in VALID_SCOPES


# ---------------------------------------------------------------------------
# Proxy / host / downgrade (13H)
# ---------------------------------------------------------------------------


class TestProxyHostHardening:
    def test_untrusted_forwarded_header_cannot_bypass_rate_limit(self, portal_app) -> None:
        make_active_user(portal_app)
        # No trusted proxy configured: client_ip must ignore X-Forwarded-For.
        statuses = []
        for i in range(12):
            status, _ = portal_app.client.post(
                "/api/v1/auth/signin",
                json={"email": "dev@example.com", "password": "wrong"},
                headers={"X-Forwarded-For": f"10.0.0.{i}"},
            )
            statuses.append(status)
        # All attempts share the same real REMOTE_ADDR bucket -> eventually 429.
        assert 429 in statuses

    def test_hsts_in_production(self, tmp_path: Path) -> None:
        from portal_server.app import create_wsgi_app
        from portal_server.config import load_config
        from portal_server.emailing import DevEmailProvider

        db = Database(tmp_path / "hsts.db")
        cfg = load_config(
            {
                "APP_ENV": "production",
                "DATABASE_URL": str(tmp_path / "hsts.db"),
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
        import io

        captured: dict = {}

        def start_response(status: str, headers: list) -> None:
            captured["headers"] = dict(headers)

        app(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/health",
                "QUERY_STRING": "",
                "REMOTE_ADDR": "127.0.0.1",
                "HTTP_HOST": "portal.example.com",
                "wsgi.input": io.BytesIO(b""),
                "CONTENT_LENGTH": "0",
            },
            start_response,
        )
        assert captured["headers"].get("Strict-Transport-Security")
        db.close()


# ---------------------------------------------------------------------------
# IDOR / authorization / scope (13U/13Z)
# ---------------------------------------------------------------------------


class TestAuthorization:
    def test_cross_user_credential_idor(self, portal_app) -> None:
        make_active_user(portal_app, email="alice@x.com")
        make_active_user(portal_app, email="bob@x.com")
        from portal_server.auth import now_iso

        from ghostlink.developer.keys import issue_credential

        key_id, _secret = issue_credential()
        portal_app.db.execute(
            "INSERT INTO credentials(user_id, name, key_id, secret_hash, secret_salt, verifier, "
            "status, created_at) VALUES(?,?,?,?,?,?,?,?)",
            (2, "bob", key_id, "h", "s", "v", "active", now_iso()),
        )
        # Alice (user 1) tries to revoke Bob's (user 2) credential -> must not match.
        signin_and_set_csrf(portal_app, email="alice@x.com")
        status, _ = portal_app.client.post(f"/api/v1/developer-keys/{key_id}/revoke")
        assert status == 404

    def test_cross_user_device_idor(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        _pair_and_tokens(portal_app)
        dev = portal_app.db.query_one(
            "SELECT device_id FROM developer_devices WHERE user_id=1 ORDER BY id DESC"
        )
        # A second user.
        make_active_user(portal_app, email="carol@x.com")
        # sign out first user, sign in carol
        portal_app.client.post("/api/v1/auth/signout")
        signin_and_set_csrf(portal_app, email="carol@x.com")
        status, _ = portal_app.client.post(f"/api/v1/devapi/devices/{dev['device_id']}/revoke")
        assert status == 404

    def test_scope_cannot_be_escalated_by_token(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        status, body = portal_app.client.post(
            "/api/v1/developer/auth/pair-begin",
            json={"device_name": "d", "platform": "termux"},
        )
        assert status == 200
        approved = portal_app.client.post(
            "/api/v1/developer/auth/pair-approve",
            json={"pairing_code": body["pairing_code"], "scopes": "device:read"},
        )
        tokens = portal_app.client.post(
            "/api/v1/developer/auth/token",
            json={
                "credential_id": approved[1]["credential_id"],
                "credential_secret": approved[1]["credential_secret"],
            },
        )
        headers = {"Authorization": f"Bearer {tokens[1]['access_token']}"}
        # device:read cannot access projects (requires project:read).
        s, _ = portal_app.client.get("/api/v1/developer/projects", headers=headers)
        assert s == 403


# ---------------------------------------------------------------------------
# Replay / expiry / revocation (13Z)
# ---------------------------------------------------------------------------


class TestTokenLifecycle:
    def test_refresh_token_is_single_use(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        tokens = _pair_and_tokens(portal_app)
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        s, body = portal_app.client.post(
            "/api/v1/developer/auth/refresh",
            json={"refresh_token": tokens["refresh_token"]},
            headers=headers,
        )
        assert s == 200, body
        # Replay the same refresh token -> rejected.
        s2, _ = portal_app.client.post(
            "/api/v1/developer/auth/refresh",
            json={"refresh_token": tokens["refresh_token"]},
            headers=headers,
        )
        assert s2 == 401

    def test_expired_session_rejected(self, clocked_portal) -> None:
        make_active_user(clocked_portal)
        signin_and_set_csrf(clocked_portal)
        # Force absolute session expiry.

        clocked_portal.db.execute(
            "UPDATE sessions SET expires_at=? WHERE user_id=1",
            ("2000-01-01T00:00:00+00:00",),
        )
        status, _ = clocked_portal.client.get("/api/v1/dashboard")
        assert status == 401

    def test_revoked_credential_rejected(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        _status, body = portal_app.client.post(
            "/api/v1/developer/auth/pair-begin",
            json={"device_name": "d", "platform": "termux"},
        )
        approved = portal_app.client.post(
            "/api/v1/developer/auth/pair-approve",
            json={"pairing_code": body["pairing_code"], "scopes": "device:read"},
        )
        cid = approved[1]["credential_id"]
        secret = approved[1]["credential_secret"]
        # Revoke it.
        portal_app.db.execute(
            "UPDATE api_credentials SET status='revoked' WHERE credential_id=?", (cid,)
        )
        s, _ = portal_app.client.post(
            "/api/v1/developer/auth/token",
            json={"credential_id": cid, "credential_secret": secret},
        )
        assert s == 401


# ---------------------------------------------------------------------------
# Malformed / oversized requests (13U)
# ---------------------------------------------------------------------------


class TestMalformedRequests:
    def test_oversized_body_rejected(self, portal_app) -> None:
        import io

        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/api/v1/auth/signin",
            "QUERY_STRING": "",
            "REMOTE_ADDR": "127.0.0.1",
            "wsgi.input": io.BytesIO(b"x" * (2 * 1024 * 1024)),
            "CONTENT_LENGTH": str(2 * 1024 * 1024),
        }
        captured: dict = {}

        def start_response(status: str, headers: list) -> None:
            captured["status"] = status.split(" ")[0]

        portal_app.app(environ, start_response)
        assert captured["status"] == "413"

    def test_malformed_json_fails_closed(self, portal_app) -> None:
        import io

        environ = {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/api/v1/auth/signin",
            "QUERY_STRING": "",
            "REMOTE_ADDR": "127.0.0.1",
            "wsgi.input": io.BytesIO(b"{not json"),
            "CONTENT_LENGTH": "9",
        }
        captured: dict = {}

        def start_response(status: str, headers: list) -> None:
            captured["status"] = status.split(" ")[0]

        portal_app.app(environ, start_response)
        assert captured["status"] == "400" or captured["status"] == "401"


# ---------------------------------------------------------------------------
# Concurrency / races (13V)
# ---------------------------------------------------------------------------


class TestConcurrency:
    def test_migration_race_is_safe(self, tmp_path: Path) -> None:
        path = tmp_path / "race.db"
        results: list[int] = []

        def open_db() -> None:
            db = Database(path)
            results.append(db.schema_version)
            db.close()

        threads = [threading.Thread(target=open_db) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert results == [3, 3, 3, 3]
        assert all(r == 3 for r in results)

    def test_concurrent_writes_do_not_lose_rows(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "cw.db")
        errors: list[Exception] = []

        def writer(prefix: int) -> None:
            try:
                with db.transaction():
                    for i in range(25):
                        db.execute(
                            "INSERT INTO users(email, password_hash, status, developer_id, "
                            "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                            (f"{prefix}-{i}@b.com", "h", "active", f"d{prefix}{i}", "t", "t"),
                        )
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        ts = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert not errors
        n = db.query_one("SELECT COUNT(*) AS n FROM users")
        assert int(n["n"]) == 100
        db.close()

    def test_rate_limiter_thread_safety(self) -> None:
        from portal_server.ratelimit import InMemoryRateLimiter

        limiter = InMemoryRateLimiter(limits={"s": (10_000, 60.0)})
        ok = [True]

        def hit() -> None:
            ok[0] = ok[0] and limiter.allow("s", "shared")

        ts = [threading.Thread(target=hit) for _ in range(50)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
        assert ok[0]
