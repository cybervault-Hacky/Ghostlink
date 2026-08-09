"""Phase 13 — database backend abstraction tests (SQLite runtime)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from portal_server.db import Database, DatabaseBackend, DatabaseMigrationError
from portal_server.db.base import _translate_placeholders
from portal_server.db.sqlite import SCHEMA_VERSION, SQLiteDatabase


@pytest.fixture
def db(tmp_path: Path) -> DatabaseBackend:
    d = Database(tmp_path / "p.db")
    assert isinstance(d, SQLiteDatabase)
    yield d
    d.close()


def test_backend_factory_selects_sqlite(tmp_path: Path) -> None:
    d = Database(tmp_path / "portal.db")
    assert d.backend_name == "sqlite"
    assert isinstance(d, SQLiteDatabase)
    d.close()


def test_backend_factory_selects_postgres_by_url(monkeypatch) -> None:
    from portal_server.db import PostgresDatabase

    called: dict[str, object] = {}

    def fake_init(self: object, url: str, **kwargs: object) -> None:
        called["url"] = url
        raise RuntimeError("connect refused (expected in unit test)")

    monkeypatch.setattr(PostgresDatabase, "__init__", fake_init)
    with pytest.raises(RuntimeError, match="connect refused"):
        Database("postgresql://user:pass@127.0.0.1:1/db", connect_timeout=1)
    assert called["url"] == "postgresql://user:pass@127.0.0.1:1/db"


def test_schema_migrated_to_latest(db: DatabaseBackend) -> None:
    assert db.schema_version == SCHEMA_VERSION
    assert db.schema_version >= 3


def test_insert_query_roundtrip(db: DatabaseBackend) -> None:
    uid = db.execute_many(
        [
            (
                "INSERT INTO users(email, password_hash, status, developer_id, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                ("a@b.com", "h", "active", "d1", "t", "t"),
            )
        ],
        lastrow=True,
    )
    row = db.query_one("SELECT * FROM users WHERE id=?", (uid,))
    assert row is not None
    assert row["email"] == "a@b.com"


def test_transaction_commits_atomically(db: DatabaseBackend) -> None:
    with db.transaction():
        db.execute(
            "INSERT INTO users(email, password_hash, status, developer_id, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?)",
            ("x@b.com", "h", "active", "d2", "t", "t"),
        )
        db.execute(
            "INSERT INTO users(email, password_hash, status, developer_id, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?)",
            ("y@b.com", "h", "active", "d3", "t", "t"),
        )
    n = db.query_one("SELECT COUNT(*) AS n FROM users WHERE email LIKE '%@b.com'")
    assert int(n["n"]) == 2


def test_transaction_rolls_back_on_error(db: DatabaseBackend) -> None:
    with pytest.raises(RuntimeError), db.transaction():
        db.execute(
            "INSERT INTO users(email, password_hash, status, developer_id, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?)",
            ("x@b.com", "h", "active", "d2", "t", "t"),
        )
        raise RuntimeError("boom")
    n = db.query_one("SELECT COUNT(*) AS n FROM users WHERE email='x@b.com'")
    assert int(n["n"]) == 0


def test_placeholder_translation() -> None:
    assert _translate_placeholders("SELECT * FROM t WHERE a=? AND b=?") == (
        "SELECT * FROM t WHERE a=%s AND b=%s"
    )
    # A literal ? inside a single-quoted string must be left untouched.
    assert _translate_placeholders("SELECT '?' AS q, x=? FROM t") == "SELECT '?' AS q, x=%s FROM t"


def test_placeholder_translation_handles_quotes() -> None:
    assert _translate_placeholders("INSERT INTO t(v) VALUES('a?b')") == (
        "INSERT INTO t(v) VALUES('a?b')"
    )


def test_future_schema_version_rejected(tmp_path: Path) -> None:
    path = tmp_path / "future.db"
    from portal_server.db import SQLiteDatabase

    _db = SQLiteDatabase(path)
    _db.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', '99')")
    _db.close()
    with pytest.raises(DatabaseMigrationError):
        SQLiteDatabase(path)


def test_concurrent_writers_are_consistent(tmp_path: Path) -> None:
    db = Database(tmp_path / "c.db")
    errors: list[Exception] = []

    def writer(start: int) -> None:
        try:
            for i in range(start, start + 20):
                db.execute(
                    "INSERT INTO users(email, password_hash, status, developer_id, "
                    "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                    (f"u{i}@b.com", "h", "active", f"d{i}", "t", "t"),
                )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(i * 1000,)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    n = db.query_one("SELECT COUNT(*) AS n FROM users")
    assert int(n["n"]) == 80
    db.close()


def test_execute_many_returns_last_rowid(db: DatabaseBackend) -> None:
    rid = db.execute_many(
        [
            (
                "INSERT INTO users(email, password_hash, status, developer_id, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                ("m@b.com", "h", "active", "d9", "t", "t"),
            )
        ],
        lastrow=True,
    )
    assert rid is not None and rid > 0
