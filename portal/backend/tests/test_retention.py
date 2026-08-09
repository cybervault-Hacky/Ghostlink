"""Phase 13 — bounded, idempotent data retention cleanup (13O)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from portal_server.db import Database, DatabaseBackend
from portal_server.ops.retention import RetentionPolicy, run_retention


@pytest.fixture
def db(tmp_path: Path) -> DatabaseBackend:
    d = Database(tmp_path / "ret.db")
    yield d
    d.close()


def _iso(days_ago: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).isoformat(timespec="seconds")


def test_retention_removes_old_security_events(db: DatabaseBackend) -> None:
    db.execute(
        "INSERT INTO users(email, password_hash, status, developer_id, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?)",
        ("u@b.com", "h", "active", "d", "t", "t"),
    )
    db.execute(
        "INSERT INTO security_events(user_id, action, actor, metadata, created_at) "
        "VALUES(1, 'old', 'x', '{}', ?)",
        (_iso(400),),
    )
    db.execute(
        "INSERT INTO security_events(user_id, action, actor, metadata, created_at) "
        "VALUES(1, 'new', 'x', '{}', ?)",
        (_iso(1),),
    )
    policy = RetentionPolicy(
        sessions_days=0,
        expired_tokens_days=0,
        security_events_days=365,
        api_activity_days=0,
        pairing_days=0,
        webauthn_challenges_days=0,
        verification_tokens_days=0,
    )
    counts = run_retention(db, policy)
    assert counts["security_events_old"] == 1
    rows = db.query("SELECT action FROM security_events")
    assert [r["action"] for r in rows] == ["new"]


def test_retention_removes_expired_tokens(db: DatabaseBackend) -> None:
    db.execute(
        "INSERT INTO users(email, password_hash, status, developer_id, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?)",
        ("u@b.com", "h", "active", "d", "t", "t"),
    )
    db.execute(
        "INSERT INTO tokens(user_id, kind, token_hash, expires_at, created_at) "
        "VALUES(1, 'email_verify', 'h1', ?, ?)",
        (_iso(30), _iso(30)),
    )
    policy = RetentionPolicy(
        sessions_days=0,
        expired_tokens_days=7,
        security_events_days=0,
        api_activity_days=0,
        pairing_days=0,
        webauthn_challenges_days=0,
        verification_tokens_days=7,
    )
    run_retention(db, policy)
    assert int(db.query_one("SELECT COUNT(*) AS n FROM tokens")["n"]) == 0


def test_retention_is_idempotent(db: DatabaseBackend) -> None:
    db.execute(
        "INSERT INTO users(email, password_hash, status, developer_id, created_at, updated_at) "
        "VALUES(?,?,?,?,?,?)",
        ("u@b.com", "h", "active", "d", "t", "t"),
    )
    db.execute(
        "INSERT INTO security_events(user_id, action, actor, metadata, created_at) "
        "VALUES(1, 'old', 'x', '{}', ?)",
        (_iso(400),),
    )
    policy = RetentionPolicy(1, 1, 365, 1, 1, 1, 1)
    run_retention(db, policy)
    counts = run_retention(db, policy)  # second run deletes nothing new
    assert counts["security_events_old"] == 0
