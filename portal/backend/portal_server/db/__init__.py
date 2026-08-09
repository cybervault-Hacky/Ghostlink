"""GhostLink Developer Portal database layer (Phase 13).

Backend selection
-----------------

``Database(location)`` is a factory: a ``postgresql://`` / ``postgres://`` URL
selects the PostgreSQL backend (psycopg3 + psycopg_pool); any other value is
treated as a SQLite file path (the local/Termux default). Backend-specific
pooling and timeout knobs are read from configuration by the ops layer.

The public surface keeps backward compatibility: ``Database``, ``MIGRATIONS``
(SQLite), ``SCHEMA_VERSION`` (SQLite) and ``DatabaseMigrationError`` are all
re-exported here.
"""

from __future__ import annotations

from pathlib import Path

from portal_server.db.base import DatabaseBackend, DatabaseMigrationError, DBError
from portal_server.db.migrations import (
    MIGRATION_LOCK_KEY,
    PG_MIGRATIONS,
    PG_SCHEMA_CHECKSUM,
    PG_SCHEMA_VERSION,
    Migration,
    migration_checksum,
    migration_status,
    verify_checksums,
)
from portal_server.db.postgres import PostgresDatabase
from portal_server.db.sqlite import MIGRATIONS, SCHEMA_VERSION, SQLiteDatabase

DatabaseLocation = str | Path


def Database(
    location: DatabaseLocation,
    *,
    pool_min: int = 1,
    pool_max: int = 10,
    connect_timeout: int = 5,
    statement_timeout_ms: int = 15_000,
    idle_timeout_seconds: int = 300,
) -> DatabaseBackend:
    """Factory that selects a backend from the connection location.

    A ``postgresql://`` / ``postgres://`` URL selects the PostgreSQL backend;
    any other value is treated as a SQLite file path.
    """
    raw = str(location)
    lowered = raw.lower()
    if lowered.startswith("postgresql://") or lowered.startswith("postgres://"):
        return PostgresDatabase(
            raw,
            pool_min=pool_min,
            pool_max=pool_max,
            connect_timeout=connect_timeout,
            statement_timeout_ms=statement_timeout_ms,
            idle_timeout_seconds=idle_timeout_seconds,
        )
    return SQLiteDatabase(Path(raw))


__all__ = [
    "MIGRATIONS",
    "MIGRATION_LOCK_KEY",
    "PG_MIGRATIONS",
    "PG_SCHEMA_CHECKSUM",
    "PG_SCHEMA_VERSION",
    "SCHEMA_VERSION",
    "DBError",
    "Database",
    "DatabaseBackend",
    "DatabaseMigrationError",
    "Migration",
    "PostgresDatabase",
    "SQLiteDatabase",
    "migration_checksum",
    "migration_status",
    "verify_checksums",
]
