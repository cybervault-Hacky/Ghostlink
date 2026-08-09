"""Phase 13 — PostgreSQL adapter static tests + gated integration tests.

Static/unit tests cover the adapter's pure logic (placeholder translation,
RETURNING detection, DSN redaction, migration checksums, status/verify, fail
closed) without a live server. A real PostgreSQL integration test runs only
when ``PORTAL_TEST_POSTGRES_URL`` is set (the CI ``postgres-integration`` job
provides one). When no server is available these integration tests skip and
this is reported honestly.
"""

from __future__ import annotations

import os

import pytest
from portal_server.db.base import _is_insert_returning_missing, _translate_placeholders
from portal_server.db.migrations import (
    PG_MIGRATIONS,
    PG_SCHEMA_VERSION,
    migration_checksum,
    migration_status,
    verify_checksums,
)
from portal_server.db.postgres import _redact_dsn


def test_pg_migrations_numbered_and_ordered() -> None:
    versions = [m.version for m in PG_MIGRATIONS]
    assert versions == sorted(versions)
    assert len(set(versions)) == len(versions)
    assert PG_MIGRATIONS[-1].version == PG_SCHEMA_VERSION


def test_checksum_is_deterministic() -> None:
    m1 = PG_MIGRATIONS[0]
    assert m1.checksum == migration_checksum(m1.sql)
    assert m1.checksum != migration_checksum(m1.sql + " -- trailing")


def test_verify_checksums() -> None:
    good = {m.version: m.checksum for m in PG_MIGRATIONS}
    assert verify_checksums(good) == []
    bad = dict(good)
    bad[2] = "deadbeef"
    problems = verify_checksums(bad)
    assert any("version 2" in p for p in problems)
    unknown = verify_checksums({99: "x"})
    assert unknown


def test_migration_status() -> None:
    good = {m.version: m.checksum for m in PG_MIGRATIONS}
    status = migration_status(PG_SCHEMA_VERSION, good)
    assert status["healthy"] is True
    assert status["up_to_date"] is True
    stale = migration_status(1, good)
    assert stale["healthy"] is False
    assert 2 in stale["missing"]


def test_placeholder_translation_pg() -> None:
    assert _translate_placeholders("UPDATE users SET email=? WHERE id=?") == (
        "UPDATE users SET email=%s WHERE id=%s"
    )
    assert _translate_placeholders("SELECT '?' ") == "SELECT '?' "


def test_returning_detection() -> None:
    assert _is_insert_returning_missing("INSERT INTO t(c) VALUES(?)")
    assert not _is_insert_returning_missing("INSERT INTO t(c) VALUES(?) RETURNING id")
    assert not _is_insert_returning_missing("UPDATE t SET c=? WHERE id=?")


def test_dsn_redaction() -> None:
    safe = _redact_dsn("postgresql://user:s3cret@dbhost:5432/ghostlink")
    assert "s3cret" not in safe
    assert "user" not in safe
    assert "dbhost" in safe
    assert "ghostlink" in safe


def test_postgres_url_is_not_treated_as_sqlite(monkeypatch) -> None:
    # A postgres URL must never be silently opened as a SQLite file.
    from portal_server.db import Database, PostgresDatabase

    def fake_init(self: object, url: str, **kwargs: object) -> None:
        raise RuntimeError("pg connect refused (expected)")

    monkeypatch.setattr(PostgresDatabase, "__init__", fake_init)
    with pytest.raises(RuntimeError, match="connect refused"):
        Database("postgresql://u:p@127.0.0.1:1/ghostlink", connect_timeout=1)


@pytest.mark.skipif(
    not os.environ.get("PORTAL_TEST_POSTGRES_URL"),
    reason="PostgreSQL runtime integration not available in this environment.",
)
def test_postgres_runtime_migrations() -> None:
    from portal_server.db import Database

    url = os.environ["PORTAL_TEST_POSTGRES_URL"]
    db = Database(url, pool_min=1, pool_max=2, connect_timeout=3)
    try:
        assert db.backend_name == "postgresql"
        assert db.schema_version >= 3
        row = db.query_one("SELECT 1 AS ok")
        assert row is not None
        # transaction safety
        with db.transaction():
            db.execute(
                "INSERT INTO users(email, password_hash, status, developer_id, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                ("pg@b.com", "h", "active", "pgd1", "t", "t"),
            )
        n = db.query_one("SELECT COUNT(*) AS n FROM users WHERE email=?", ("pg@b.com",))
        assert int(n["n"]) == 1
    finally:
        db.close()


@pytest.mark.skipif(
    not os.environ.get("PORTAL_TEST_POSTGRES_URL"),
    reason="PostgreSQL runtime integration not available in this environment.",
)
def test_postgres_rate_limiter_runtime() -> None:
    from portal_server.db import Database
    from portal_server.ratelimit import PostgresRateLimiter

    url = os.environ["PORTAL_TEST_POSTGRES_URL"]
    db = Database(url, pool_min=1, pool_max=2, connect_timeout=3)
    try:
        limiter = PostgresRateLimiter(db, limits={"test_scope": (3, 60.0)})
        assert limiter.allow("test_scope", "k") is True
        assert limiter.allow("test_scope", "k") is True
        assert limiter.allow("test_scope", "k") is True
        assert limiter.allow("test_scope", "k") is False
        limiter.reset("test_scope", "k")
    finally:
        db.close()
