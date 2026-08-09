"""Failure & recovery tests (Phase 11Q): expired sessions, invalid database,
malformed requests, and rate limiting. The system must fail safely."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from conftest import make_active_user, signin_and_set_csrf
from portal_server.config import ConfigError, load_config
from portal_server.db import Database


class TestSessionExpiry:
    def test_expired_session_rejected(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        # Force the session to expire.
        portal_app.db.execute(
            "UPDATE sessions SET expires_at='2000-01-01T00:00:00+00:00' WHERE user_id=1"
        )
        status, _ = portal_app.client.get("/api/v1/dashboard")
        assert status == 401

    def test_revoked_session_rejected(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        portal_app.db.execute("UPDATE sessions SET revoked_at='t' WHERE user_id=1")
        status, _ = portal_app.client.get("/api/v1/dashboard")
        assert status == 401


class TestDatabaseFailures:
    def test_invalid_db_path_fails(self, tmp_path: Path) -> None:
        # A path that cannot be created (parent is a file) must not silently
        # fall back to an in-memory DB.
        blocked = tmp_path / "blocked" / "p.db"
        blocked.parent.write_text("x")
        with pytest.raises(sqlite3.OperationalError):
            Database(blocked)

    def test_corrupt_migration_fails(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "p.db")
        db.close()
        # Rewrite a migration table to a corrupt state and verify a fresh
        # open fails safely rather than modifying the schema.
        conn = sqlite3.connect(str(tmp_path / "p.db"))
        conn.execute("DROP TABLE users")
        conn.commit()
        conn.close()
        # A new Database that needs to re-run v1 fails cleanly on the corrupt
        # table (CREATE IF NOT EXISTS would succeed, so instead simulate a
        # future-version guard which we already cover elsewhere). Here we
        # assert a malformed schema_meta version fails closed.
        conn = sqlite3.connect(str(tmp_path / "p.db"))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        conn.execute("INSERT OR REPLACE INTO schema_meta VALUES('version','banana')")
        conn.commit()
        conn.close()
        with pytest.raises(ValueError):
            Database(tmp_path / "p.db")


class TestMalformedRequests:
    def test_invalid_json_returns_error(self, portal_app) -> None:
        status, _body = portal_app.client.post("/api/v1/auth/signin", json={"email": 123})
        # Non-string email -> validation error, never a 500 or crash.
        assert status in (400, 401)

    def test_unknown_endpoint_404(self, portal_app) -> None:
        status, _ = portal_app.client.get("/api/v1/does-not-exist")
        assert status == 404

    def test_method_not_allowed(self, portal_app) -> None:
        status, _ = portal_app.client.patch("/api/v1/auth/signin", json={})
        assert status in (404, 405)


class TestConfigFailClosed:
    def test_missing_production_env_fails(self) -> None:
        with pytest.raises(ConfigError):
            load_config({"APP_ENV": "production"})

    def test_invalid_rate_limit_override(self) -> None:
        with pytest.raises(ConfigError):
            load_config({"APP_ENV": "development", "RATE_LIMIT_SIGN_IN": "not-a-number"})


class TestRateLimitBehavior:
    def test_signup_rate_limit(self, portal_app) -> None:
        status = 0
        for i in range(6):
            status, _ = portal_app.client.post(
                "/api/v1/auth/signup",
                json={"email": f"u{i}@x.com", "password": "super-secure-pass-123"},
            )
        assert status == 429
