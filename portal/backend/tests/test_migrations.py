"""Database migration tests (Phase 11B)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from portal_server.db import MIGRATIONS, SCHEMA_VERSION, Database, DatabaseMigrationError


class TestMigrations:
    def test_fresh_db_reaches_current_schema(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "p.db")
        assert db.schema_version == SCHEMA_VERSION
        # All base tables exist.
        names = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='table'")}
        for table in (
            "users",
            "tokens",
            "sessions",
            "credentials",
            "projects",
            "security_events",
            "mfa_totp",
            "mfa_recovery_codes",
            "webauthn_credentials",
        ):
            assert table in names
        db.close()

    def test_migrations_are_ordered_and_monotonic(self) -> None:
        versions = [v for v, _ in MIGRATIONS]
        assert versions == sorted(versions)
        assert len(set(versions)) == len(versions)
        assert versions[-1] == SCHEMA_VERSION

    def test_indexes_exist(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "p.db")
        names = {r["name"] for r in db.query("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_sessions_user" in names
        assert "idx_credentials_key_id" in names
        assert "idx_security_events_user_time" in names
        db.close()

    def test_future_schema_fails_closed(self, tmp_path: Path) -> None:
        # Simulate a database written by a newer version.
        path = tmp_path / "p.db"
        db = Database(path)
        db.close()
        conn = sqlite3.connect(str(path))
        conn.execute("UPDATE schema_meta SET value='999' WHERE key='version'")
        conn.commit()
        conn.close()
        with pytest.raises(DatabaseMigrationError):
            Database(path)

    def test_fk_enforcement(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "p.db")
        # Inserting a session with a non-existent user must fail (FK on).
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO sessions(user_id, token_hash, created_at, expires_at, "
                "last_active_at, csrf_token) "
                "VALUES(9999,'h','t','t','t','c')"
            )
        db.close()

    def test_uniqueness_constraints(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "p.db")
        db.execute(
            "INSERT INTO users(email, password_hash, status, developer_id, created_at, updated_at) "
            "VALUES('a@b.com','h','active','dev_x','t','t')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute(
                "INSERT INTO users(email, password_hash, status, developer_id, "
                "created_at, updated_at) "
                "VALUES('a@b.com','h','active','dev_y','t','t')"
            )
        db.close()
