"""Database backend abstraction for the GhostLink Developer Portal (Phase 13).

Route handlers and services must only depend on the ``DatabaseBackend``
interface in this module. The concrete backend (SQLite or PostgreSQL) is
selected by the ``db`` package factory from the configured ``DATABASE_URL``,
so no raw database implementation detail leaks into application code.

Placeholder dialect
-------------------

The application SQL is written once with ``?`` placeholders. SQLite uses
``?`` natively; the PostgreSQL adapter translates ``?`` to ``%s`` (psycopg)
before execution. The translation is applied *outside* single-quoted string
literals so a literal ``?`` inside a string (e.g. an application name) is
never rewritten. ``execute_many(..., lastrow=True)`` returns the inserted row
id: SQLite uses ``cursor.lastrowid``; PostgreSQL appends ``RETURNING id``.

Transactions
------------

Security-sensitive writes must run inside a transaction. Both backends expose
``transaction()`` as a context manager that ``BEGIN``/``COMMIT``/``ROLLBACK``
around the block, and ``connection()`` for low-level operations. Multi-process
correctness is provided by the backend (PostgreSQL row locking / unique
constraints) rather than Python locks alone.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import AbstractContextManager
from typing import Any

__all__ = [
    "DBError",
    "DatabaseBackend",
    "DatabaseMigrationError",
]


class DBError(Exception):
    """Base class for database-layer failures (not migration related)."""


class DatabaseMigrationError(DBError):
    """Raised when the database schema cannot be safely migrated."""


class DatabaseBackend(ABC):
    """The persistence interface exposed to application code.

    All methods are thread-safe. ``query*`` return plain ``dict`` rows so
    callers never see backend-specific row objects.
    """

    @property
    @abstractmethod
    def backend_name(self) -> str:
        """One of ``"sqlite"`` or ``"postgresql"``."""

    @property
    @abstractmethod
    def schema_version(self) -> int:
        """Current applied schema version from ``schema_meta``."""

    @abstractmethod
    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> None:
        """Execute a single statement in its own transaction."""

    @abstractmethod
    def query(self, sql: str, params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
        """Run a SELECT (or any read) and return a list of dict rows."""

    @abstractmethod
    def query_one(self, sql: str, params: tuple[Any, ...] = ()) -> dict[str, Any] | None:
        """Return the first row as a dict, or ``None``."""

    @abstractmethod
    def execute_many(
        self, sqls: list[tuple[str, tuple[Any, ...]]], *, lastrow: bool = False
    ) -> int | None:
        """Run several statements in one transaction; returns the last row id."""

    @abstractmethod
    def transaction(self) -> AbstractContextManager[None]:
        """A context manager that commits on success and rolls back on error."""

    @abstractmethod
    def connection(self) -> AbstractContextManager[Any]:
        """A low-level connection/transaction context manager.

        Yields a backend-specific connection object. Used by migration and
        pool code, not by route handlers.
        """

    @abstractmethod
    def close(self) -> None:
        """Release pooled resources (idempotent)."""


def _translate_placeholders(sql: str) -> str:
    """Rewrite ``?`` placeholders to ``%s``, skipping single-quoted literals."""
    out: list[str] = []
    i = 0
    n = len(sql)
    in_str = False
    while i < n:
        ch = sql[i]
        if ch == "'":
            # Toggle string literal state (handles doubled '' escape simply:
            # we stay in_str across the pair).
            in_str = not in_str
            out.append(ch)
            i += 1
            continue
        if ch == "?" and not in_str:
            out.append("%s")
            i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _is_insert_returning_missing(sql: str) -> bool:
    head = " ".join(sql.split()).lower()
    if not head.startswith("insert"):
        return False
    return "returning" not in head
