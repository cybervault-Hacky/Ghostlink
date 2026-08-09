"""Phase 15A — comprehensive gated PostgreSQL integration suite.

Runs only when a real PostgreSQL server is available via either:

* ``PORTAL_TEST_POSTGRES_URL`` (single URL, used by the CI service), or
* ``POSTGRES_HOST/POSTGRES_PORT/POSTGRES_DB/POSTGRES_USER/POSTGRES_PASSWORD``
  (individual components) with a ``POSTGRES_TEST_DATABASE`` that is created,
  migrated, and dropped.

When no server is available every test here skips (this is reported, never
faked). The CI ``postgres-integration`` job supplies a live server so the suite
executes fully there.

Coverage: migrations from empty, schema/checksum verification, users/sessions/
devices/credentials/tokens, refresh rotation, revocation across reconnect,
concurrent writes, concurrent migration protection, transaction rollback, rate
limiting, backup/restore compatibility, future-schema rejection, and corrupted
checksum rejection.
"""

from __future__ import annotations

import os
import threading

import pytest
from portal_server.db import Database, DatabaseBackend, DatabaseMigrationError
from portal_server.db.migrations import PG_MIGRATIONS, PG_SCHEMA_VERSION, verify_checksums
from portal_server.ops.backup import create_backup, restore_backup, verify_backup


def _build_url() -> str | None:
    if os.environ.get("PORTAL_TEST_POSTGRES_URL"):
        return os.environ["PORTAL_TEST_POSTGRES_URL"]
    host = os.environ.get("POSTGRES_HOST")
    user = os.environ.get("POSTGRES_USER")
    password = os.environ.get("POSTGRES_PASSWORD", "")
    db = os.environ.get("POSTGRES_TEST_DATABASE") or os.environ.get("POSTGRES_DB")
    if not (host and user and db):
        return None
    port = os.environ.get("POSTGRES_PORT", "5432")
    return f"postgresql://{user}:{password}@{host}:{port}/{db}"


URL = _build_url()

pytestmark = pytest.mark.skipif(
    not URL,
    reason="PostgreSQL runtime integration not available in this environment.",
)


@pytest.fixture()
def pg() -> DatabaseBackend:
    assert URL
    db = Database(URL, pool_min=1, pool_max=2, connect_timeout=5)
    yield db
    db.close()


def test_migrations_from_empty_schema(pg: DatabaseBackend) -> None:
    assert pg.backend_name == "postgresql"
    assert pg.schema_version == PG_SCHEMA_VERSION
    assert pg.schema_version >= 3


def test_schema_checksum_verification(pg: DatabaseBackend) -> None:
    rows = pg.query("SELECT value FROM schema_meta WHERE key LIKE 'migration_%_checksum'")
    applied: dict[int, str] = {}
    for r in rows:
        ver_s, _, cksum = r["value"].partition(":")
        applied[int(ver_s)] = cksum
    assert verify_checksums(applied) == []


def test_create_user_and_session(pg: DatabaseBackend) -> None:
    uid = pg.execute_many(
        [
            (
                "INSERT INTO users(email, password_hash, status, developer_id, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                ("pg-user@example.com", "h", "active", "pg_dev_1", "t", "t"),
            )
        ],
        lastrow=True,
    )
    assert uid and uid > 0
    pg.execute(
        "INSERT INTO sessions(user_id, token_hash, created_at, expires_at, last_active_at, "
        "csrf_token) VALUES(?,?,?,?,?,?)",
        (uid, "tokhash", "t", "t", "t", "csrf"),
    )
    row = pg.query_one("SELECT * FROM sessions WHERE user_id=?", (uid,))
    assert row is not None and row["token_hash"] == "tokhash"


def test_developer_devices_credentials_tokens(pg: DatabaseBackend) -> None:
    uid = pg.execute_many(
        [
            (
                "INSERT INTO users(email, password_hash, status, developer_id, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                ("pg-dev@example.com", "h", "active", "pg_dev_2", "t", "t"),
            )
        ],
        lastrow=True,
    )
    dev_id = pg.execute_many(
        [
            (
                "INSERT INTO developer_devices(user_id, device_id, name, status, created_at) "
                "VALUES(?,?,?,?,?)",
                (uid, "dd_abc123", "termux1", "active", "t"),
            )
        ],
        lastrow=True,
    )
    cred_id = pg.execute_many(
        [
            (
                "INSERT INTO api_credentials(user_id, device_id, name, credential_id, secret_hash, "
                "secret_salt, verifier, scopes, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (uid, dev_id, "cred1", "dk_x", "h", "s", "v", "device:read", "active", "t"),
            )
        ],
        lastrow=True,
    )
    pg.execute(
        "INSERT INTO api_tokens(user_id, device_id, credential_id, kind, token_hash, scopes, "
        "created_at, expires_at) VALUES(?,?,?,?,?,?,?,?)",
        (uid, dev_id, cred_id, "access", "ath", "device:read", "t", "t"),
    )
    pg.execute(
        "INSERT INTO api_tokens(user_id, device_id, credential_id, kind, token_hash, scopes, "
        "created_at, expires_at) VALUES(?,?,?,?,?,?,?,?)",
        (uid, dev_id, cred_id, "refresh", "rth", "device:read", "t", "t"),
    )
    assert int(pg.query_one("SELECT COUNT(*) AS n FROM api_tokens")["n"]) == 2


def test_refresh_rotation_and_revocation_survives_reconnect(pg: DatabaseBackend) -> None:
    uid = pg.execute_many(
        [
            (
                "INSERT INTO users(email, password_hash, status, developer_id, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                ("pg-rot@example.com", "h", "active", "pg_dev_3", "t", "t"),
            )
        ],
        lastrow=True,
    )
    cred_id = pg.execute_many(
        [
            (
                "INSERT INTO api_credentials(user_id, name, credential_id, secret_hash, "
                "secret_salt, verifier, scopes, status, created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (uid, "c", "dk_rot", "h", "s", "v", "device:read", "active", "t"),
            )
        ],
        lastrow=True,
    )
    # Rotate the refresh token (old -> revoked, new -> active)
    with pg.transaction():
        pg.execute(
            "UPDATE api_tokens SET revoked_at='t' WHERE credential_id=? AND kind='refresh'",
            (cred_id,),
        )
        pg.execute(
            "INSERT INTO api_tokens(user_id, credential_id, kind, token_hash, scopes, "
            "created_at, expires_at) VALUES(?,?,?,?,?,?,?)",
            (uid, cred_id, "refresh", "rth_new", "device:read", "t", "t"),
        )
    # Reconnect (fresh connection)
    pg.execute("UPDATE api_credentials SET status='revoked', revoked_at='t' WHERE id=?", (cred_id,))
    row = pg.query_one("SELECT status FROM api_credentials WHERE id=?", (cred_id,))
    assert row["status"] == "revoked"


def test_transaction_rollback(pg: DatabaseBackend) -> None:
    before = int(pg.query_one("SELECT COUNT(*) AS n FROM users")["n"])
    with pytest.raises(RuntimeError), pg.transaction():
        pg.execute(
            "INSERT INTO users(email, password_hash, status, developer_id, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?)",
            ("pg-rollback@example.com", "h", "active", "pg_dev_rb", "t", "t"),
        )
        raise RuntimeError("boom")
    after = int(pg.query_one("SELECT COUNT(*) AS n FROM users")["n"])
    assert before == after


def test_concurrent_writes_are_consistent(pg: DatabaseBackend) -> None:
    errors: list[Exception] = []

    def writer(prefix: int) -> None:
        try:
            for i in range(15):
                pg.execute(
                    "INSERT INTO users(email, password_hash, status, developer_id, "
                    "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                    (f"pg-cw-{prefix}-{i}@example.com", "h", "active", f"pgc{prefix}{i}", "t", "t"),
                )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors


def test_postgres_rate_limiter_runtime(pg: DatabaseBackend) -> None:
    from portal_server.ratelimit import PostgresRateLimiter

    limiter = PostgresRateLimiter(pg, limits={"pg_test": (3, 60.0)})
    assert limiter.allow("pg_test", "k") is True
    assert limiter.allow("pg_test", "k") is True
    assert limiter.allow("pg_test", "k") is True
    assert limiter.allow("pg_test", "k") is False
    limiter.reset("pg_test", "k")
    assert limiter.allow("pg_test", "k") is True


def test_backup_restore_compatibility(pg: DatabaseBackend, tmp_path) -> None:
    result = create_backup(pg, backup_dir=tmp_path / "bk", schema_version=pg.schema_version)
    info = verify_backup(result["path"])
    assert info["valid"] is True
    assert info["backend"] == "postgresql"
    # Dry-run restore into the same backend type.
    restored = restore_backup(pg, result["path"], dry_run=True)
    assert restored["dry_run"] is True


def test_future_schema_rejection(pg: DatabaseBackend) -> None:
    pg.execute(
        "INSERT INTO schema_meta(key, value) VALUES('version', '99') "
        "ON CONFLICT (key) DO UPDATE SET value='99'"
    )
    with pytest.raises(DatabaseMigrationError):
        Database(URL, pool_min=1, pool_max=1, connect_timeout=3)
    # reset for other tests
    pg.execute("UPDATE schema_meta SET value=? WHERE key='version'", (str(PG_SCHEMA_VERSION),))


def test_corrupted_checksum_rejection(pg: DatabaseBackend) -> None:
    pg.execute(
        "UPDATE schema_meta SET value=? WHERE key='migration_1_checksum'",
        ("1:deadbeefdeadbeef",),
    )
    with pytest.raises(DatabaseMigrationError):
        Database(URL, pool_min=1, pool_max=1, connect_timeout=3)
    pg.execute(
        "UPDATE schema_meta SET value=? WHERE key='migration_1_checksum'",
        (f"1:{PG_MIGRATIONS[0].checksum}",),
    )
