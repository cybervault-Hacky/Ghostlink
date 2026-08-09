"""SQLite persistence for the Developer Portal (Phase 10B).

Uses the Python standard-library ``sqlite3``. The schema is written to be
PostgreSQL-ready (explicit primary keys, timestamps as ISO-8601 text,
foreign keys) so a later Postgres backend can reuse the model.

Sensitive columns store only *verifiers/hashes*, never plaintext secrets:
* ``users.password_hash`` — PBKDF2-HMAC-SHA256 output (see ``auth``).
* ``sessions.token_hash`` / ``tokens.token_hash`` — SHA-256 of a CSPRNG token.
* ``credentials.secret_hash`` / ``secret_salt`` — the Phase 10A salted
  HKDF-SHA256 verifier (see ``ghostlink.developer.keys``).
* ``mfa_recovery_codes.code_hash`` — SHA-256 of a recovery code.
"""

from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any


class DatabaseMigrationError(Exception):
    """Raised when the database schema cannot be safely migrated."""


SCHEMA_VERSION = 2

_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',        -- pending | active | disabled
    email_verified INTEGER NOT NULL DEFAULT 0,
    developer_id TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT
);

CREATE TABLE IF NOT EXISTS tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,                            -- email_verify | password_reset
    token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL,
    revoked_at TEXT,
    device TEXT,
    browser TEXT,
    os TEXT,
    ip TEXT,
    csrf_token TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    key_id TEXT NOT NULL UNIQUE,
    secret_hash TEXT NOT NULL,
    secret_salt TEXT NOT NULL,
    verifier TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',         -- active | revoked
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    rotated_at TEXT,
    revoked_at TEXT
);

CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',         -- active | archived
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(user_id, slug)
);

CREATE TABLE IF NOT EXISTS security_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',           -- JSON, metadata only
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mfa_totp (
    user_id INTEGER PRIMARY KEY REFERENCES users(id),
    secret TEXT NOT NULL,                          -- base32 secret, server-side
    enabled INTEGER NOT NULL DEFAULT 0,
    verified INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mfa_recovery_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    code_hash TEXT NOT NULL,
    used INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webauthn_credentials (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL REFERENCES users(id),
    credential_id TEXT NOT NULL,
    public_key TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    sign_count INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE(user_id, credential_id)
);
"""

# Production indexes (Phase 11): query-path acceleration without changing
# the schema shape. Added as a forward migration (v2).
_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_tokens_hash ON tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_credentials_user ON credentials(user_id);
CREATE INDEX IF NOT EXISTS idx_credentials_key_id ON credentials(key_id);
CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id);
CREATE INDEX IF NOT EXISTS idx_security_events_user_time ON security_events(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_recovery_user ON mfa_recovery_codes(user_id);
CREATE INDEX IF NOT EXISTS idx_webauthn_user ON webauthn_credentials(user_id);
"""

# Versioned, ordered migrations. Each entry is (version, [sql statements]).
# The base migration (v1) creates the full Phase 10B schema; v2 adds the
# production indexes. Migrations run inside a single transaction each, and
# a migration that would downgrade the schema fails clearly.
MIGRATIONS: list[tuple[int, list[str]]] = [
    (1, [_BASE_SCHEMA]),
    (2, [_INDEXES]),
]


class Database:
    """A minimal, thread-safe SQLite wrapper."""

    def __init__(self, path: Path | str) -> None:
        self._path = str(path)
        self._lock = threading.RLock()
        self._init_schema()

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_schema(self) -> None:
        """Apply pending migrations in order, transactionally.

        Fails closed: if the stored schema version is ahead of what this
        build understands (a downgrade), or a migration is corrupt, we raise
        rather than silently modifying the schema.
        """
        with self._lock:
            conn = self.connect()
            try:
                # Bootstrap: ensure the meta table exists so we can read the
                # current schema version on a fresh or existing database.
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_meta ("
                    "key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
                current = int(row["value"]) if row else 0
                if current > SCHEMA_VERSION:
                    raise DatabaseMigrationError(
                        f"Database schema v{current} is newer than this build "
                        f"(supports up to v{SCHEMA_VERSION}). Refusing to modify it."
                    )
                for version, statements in MIGRATIONS:
                    if version <= current:
                        continue
                    try:
                        conn.execute("BEGIN")
                        for statement in statements:
                            conn.executescript(statement)
                        conn.execute(
                            "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('version', ?)",
                            (str(version),),
                        )
                        conn.commit()
                    except sqlite3.DatabaseError as exc:
                        conn.rollback()
                        raise DatabaseMigrationError(
                            f"Migration to schema v{version} failed: {exc}"
                        ) from exc
            finally:
                conn.close()

    @property
    def schema_version(self) -> int:
        row = self.query_one("SELECT value FROM schema_meta WHERE key='version'")
        return int(row["value"]) if row else 0

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        with self._lock:
            conn = self.connect()
            try:
                conn.execute(sql, params)
                conn.commit()
            finally:
                conn.close()

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        with self._lock:
            conn = self.connect()
            try:
                rows = conn.execute(sql, params).fetchall()
                return [dict(row) for row in rows]
            finally:
                conn.close()

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute_many(
        self, sqls: list[tuple[str, tuple[Any, ...]]], *, lastrow: bool = False
    ) -> int | None:
        """Run several statements in one transaction; returns last rowid."""
        with self._lock:
            conn = self.connect()
            try:
                cur = conn.cursor()
                last_id: int | None = None
                for sql, params in sqls:
                    cur.execute(sql, params)
                    if lastrow:
                        last_id = int(cur.lastrowid or 0)
                conn.commit()
                return last_id
            finally:
                conn.close()

    def close(self) -> None:
        pass  # sqlite connections are opened/closed per call


__all__ = ["MIGRATIONS", "SCHEMA_VERSION", "Database", "DatabaseMigrationError"]
