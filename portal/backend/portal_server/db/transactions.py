"""Transaction helpers for the GhostLink Developer Portal (Phase 13C).

Application code must wrap security-sensitive multi-statement writes in a
transaction. ``transaction(db)`` commits on success and rolls back on failure
for either backend. ``with_transaction`` is a decorator form for service
methods.

Multi-process correctness comes from the backend, not Python locks: PostgreSQL
uses row locking and unique constraints; SQLite uses ``BEGIN IMMEDIATE``-style
transactions plus its file lock.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from functools import wraps
from typing import Any, TypeVar, cast

from portal_server.db.base import DatabaseBackend

_F = TypeVar("_F", bound=Callable[..., Any])


@contextmanager
def transaction(db: DatabaseBackend) -> Iterator[None]:
    """Run a block inside a database transaction.

    Usage::

        with transaction(db):
            db.execute("UPDATE credentials SET status='revoked' WHERE id=?", (cid,))
            db.execute("INSERT INTO security_events ...", (...))
    """
    with db.transaction() as _tx:
        yield


def with_transaction(func: _F) -> _F:
    """Decorator that wraps a method taking ``db`` as its first argument."""

    @wraps(func)
    def wrapper(db: DatabaseBackend, *args: Any, **kwargs: Any) -> Any:
        with db.transaction():
            return func(db, *args, **kwargs)

    return cast(_F, wrapper)


__all__ = ["transaction", "with_transaction"]
