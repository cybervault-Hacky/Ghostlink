"""PostgreSQL persistence backend for the GhostLink Developer Portal (Phase 13B).

Implements ``DatabaseBackend`` on psycopg3 + ``psycopg_pool``. Responsibilities:

* connection pooling (min/max, connect timeout, idle timeout)
* per-checkout ``statement_timeout``
* ``?`` → ``%s`` placeholder translation (see ``db.base``)
* ``RETURNING id`` for ``execute_many(lastrow=True)``
* transactional migrations guarded by a Postgres advisory lock, with
  per-version checksum validation and future-version rejection (fail closed)
* graceful shutdown via ``pool.close()``

No database credentials are ever logged or printed: ``__repr__`` and the
exception path redact the DSN.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from portal_server.db.base import (
    DatabaseBackend,
    DatabaseMigrationError,
    _is_insert_returning_missing,
    _translate_placeholders,
)
from portal_server.db.migrations import (
    MIGRATION_LOCK_KEY,
    PG_MIGRATIONS,
    PG_SCHEMA_VERSION,
    verify_checksums,
)

# Optional heavy dependency — imported lazily so the module can be imported in
# environments (Termux, tests) where psycopg is not installed.
try:  # pragma: no cover - import guard
    import psycopg
    from psycopg.rows import dict_row

    _PSYCOPG_OK = True
except Exception:  # pragma: no cover
    _PSYCOPG_OK = False
    psycopg = None
    dict_row = None

try:  # pragma: no cover - import guard
    from psycopg_pool import ConnectionPool

    _POOL_OK = True
except Exception:  # pragma: no cover
    _POOL_OK = False
    ConnectionPool = None


def _redact_dsn(dsn: str) -> str:
    """Return a safe description of a DSN with credentials removed."""
    if dsn.startswith("postgres") and "://" in dsn:
        head, _, tail = dsn.partition("://")
        rest = tail.split("@", 1)
        host_part = rest[-1]
        return f"{head}://***@{host_part}"
    return "<database-url-redacted>"


class PostgresDatabase(DatabaseBackend):
    """A PostgreSQL backend implementing ``DatabaseBackend`` via psycopg3."""

    backend_name = "postgresql"

    def __init__(
        self,
        url: str,
        *,
        pool_min: int = 1,
        pool_max: int = 10,
        connect_timeout: int = 5,
        statement_timeout_ms: int = 15_000,
        idle_timeout_seconds: int = 300,
    ) -> None:
        self._url = url
        self._safe_url = _redact_dsn(url)
        if not _PSYCOPG_OK or not _POOL_OK:
            raise RuntimeError(
                "psycopg[binary] and psycopg_pool are required for the PostgreSQL backend."
            )
        self._pool_min = pool_min
        self._pool_max = max(pool_min, pool_max)
        self._connect_timeout = connect_timeout
        self._statement_timeout_ms = statement_timeout_ms
        self._idle_timeout = idle_timeout_seconds
        self._pool: ConnectionPool | None = None
        self._tlocal = threading.local()
        self._init_schema()

    # -- connection pool --------------------------------------------------
    def _get_pool(self) -> ConnectionPool:
        if self._pool is None:
            self._pool = ConnectionPool(
                conninfo=self._url,
                min_size=self._pool_min,
                max_size=self._pool_max,
                kwargs={"connect_timeout": self._connect_timeout},
                open=True,
                timeout=self._connect_timeout,
            )
        return self._pool

    def _active(self) -> Any | None:
        """Transaction-scoped connection for this thread, if any."""
        return getattr(self._tlocal, "conn", None)

    def _new_conn(self) -> tuple[Any, Any]:
        """Check out a fresh pool connection (with statement timeout)."""
        pool = self._get_pool()
        cm = pool.connection()
        conn = cm.__enter__()
        if self._statement_timeout_ms > 0:
            conn.execute(f"SET statement_timeout = {int(self._statement_timeout_ms)}")
        return conn, cm

    @contextmanager
    def connection(self) -> Iterator[Any]:
        active = self._active()
        if active is not None:
            yield active
            return
        conn, cm = self._new_conn()
        try:
            yield conn
        finally:
            cm.__exit__(None, None, None)

    # -- migrations -------------------------------------------------------
    def _init_schema(self) -> None:
        with self.connection() as conn:
            conn.execute("SELECT pg_advisory_lock(%s)", (MIGRATION_LOCK_KEY,))
            try:
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS schema_meta "
                    "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
                )
                conn.commit()
                row = conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()
                current = int(row[0]) if row else 0
                if current > PG_SCHEMA_VERSION:
                    raise DatabaseMigrationError(
                        f"Database schema v{current} is newer than this build "
                        f"(supports up to v{PG_SCHEMA_VERSION}). Refusing to modify it."
                    )
                # Verify checksums of already-applied migrations.
                applied: dict[int, str] = {}
                rows = conn.execute(
                    "SELECT value FROM schema_meta WHERE key LIKE 'migration_%_checksum'"
                ).fetchall()
                for r in rows:
                    # value format "version:checksum"
                    try:
                        ver_s, _, cksum = r[0].partition(":")
                        applied[int(ver_s)] = cksum
                    except (ValueError, AttributeError) as exc:
                        raise DatabaseMigrationError("Corrupt migration checksum record.") from exc
                problems = verify_checksums(applied)
                if problems:
                    raise DatabaseMigrationError(
                        "Migration checksum verification failed: " + "; ".join(problems)
                    )
                for migration in PG_MIGRATIONS:
                    if migration.version <= current:
                        continue
                    conn.execute("BEGIN")
                    try:
                        conn.execute(migration.sql)
                        conn.execute(
                            "INSERT INTO schema_meta(key, value) VALUES('version', %s) "
                            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                            (str(migration.version),),
                        )
                        conn.execute(
                            "INSERT INTO schema_meta(key, value) VALUES(%s, %s) "
                            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value",
                            (
                                f"migration_{migration.version}_checksum",
                                f"{migration.version}:{migration.checksum}",
                            ),
                        )
                        conn.commit()
                    except Exception as exc:
                        conn.rollback()
                        raise DatabaseMigrationError(
                            f"Migration to schema v{migration.version} failed: {exc}"
                        ) from exc
            finally:
                try:
                    conn.execute("SELECT pg_advisory_unlock(%s)", (MIGRATION_LOCK_KEY,))
                    conn.commit()
                finally:
                    pass

    @property
    def schema_version(self) -> int:
        row = self.query_one("SELECT value FROM schema_meta WHERE key='version'")
        return int(row["value"]) if row else 0

    # -- generic helpers --------------------------------------------------
    def _run(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        translated = _translate_placeholders(sql)
        active = self._active()
        if active is not None:
            active.execute(translated, params)
            return
        conn, cm = self._new_conn()
        try:
            conn.execute(translated, params)
            conn.commit()
        finally:
            cm.__exit__(None, None, None)

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        self._run(sql, params)

    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        translated = _translate_placeholders(sql)
        active = self._active()
        if active is not None:
            cur = active.cursor(row_factory=dict_row)
            cur.execute(translated, params)
            return list(cur.fetchall())
        conn, cm = self._new_conn()
        try:
            cur = conn.cursor(row_factory=dict_row)
            cur.execute(translated, params)
            return list(cur.fetchall())
        finally:
            cm.__exit__(None, None, None)

    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    def execute_many(
        self, sqls: list[tuple[str, tuple[Any, ...]]], *, lastrow: bool = False
    ) -> int | None:
        active = self._active()
        conn: Any
        cm: Any
        if active is not None:
            conn = active
            cm = None
        else:
            conn, cm = self._new_conn()
        try:
            last_id: int | None = None
            for sql, params in sqls:
                translated = _translate_placeholders(sql)
                if lastrow and _is_insert_returning_missing(translated):
                    translated = translated.rstrip(";") + " RETURNING id"
                cur = conn.execute(translated, params)
                if lastrow:
                    row = cur.fetchone()
                    last_id = int(row[0]) if row else None
            if cm is not None:
                conn.commit()
            return last_id
        finally:
            if cm is not None:
                cm.__exit__(None, None, None)

    @contextmanager
    def transaction(self) -> Iterator[None]:
        conn, cm = self._new_conn()
        self._tlocal.conn = conn
        try:
            yield
        except BaseException:
            conn.rollback()
            cm.__exit__(*sys.exc_info())
            self._tlocal.conn = None
            raise
        else:
            conn.commit()
            cm.__exit__(None, None, None)
            self._tlocal.conn = None

    def close(self) -> None:
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    def __repr__(self) -> str:  # pragma: no cover - repr, not tested for content
        return f"<PostgresDatabase backend_name='postgresql' url={self._safe_url}>"


__all__ = ["PostgresDatabase"]
