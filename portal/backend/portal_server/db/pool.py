"""Database connection-pool configuration (Phase 13B).

PostgreSQL pool sizing and timeouts are environment-driven
(``DATABASE_POOL_MIN/MAX``, ``DATABASE_CONNECT_TIMEOUT``,
``DATABASE_STATEMENT_TIMEOUT``). The ``PoolConfig`` dataclass carries these
values; ``build_database`` uses them to construct the selected backend.
"""

from __future__ import annotations

from dataclasses import dataclass

from portal_server.db import Database, DatabaseBackend


@dataclass(frozen=True, slots=True)
class PoolConfig:
    pool_min: int = 1
    pool_max: int = 10
    connect_timeout: int = 5
    statement_timeout_ms: int = 15_000
    idle_timeout_seconds: int = 300


def build_database(db_url: str, pool: PoolConfig | None = None) -> DatabaseBackend:
    """Construct the database backend for ``db_url`` with pool settings."""
    p = pool or PoolConfig()
    return Database(
        db_url,
        pool_min=p.pool_min,
        pool_max=p.pool_max,
        connect_timeout=p.connect_timeout,
        statement_timeout_ms=p.statement_timeout_ms,
        idle_timeout_seconds=p.idle_timeout_seconds,
    )


__all__ = ["PoolConfig", "build_database"]
