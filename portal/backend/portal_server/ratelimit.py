"""Rate limiting abstraction for the GhostLink portal (Phase 13F).

``RateLimiter`` is an interface. Two implementations exist:

* ``InMemoryRateLimiter`` — sliding-window, per-process. Default for local
  development / single-process deployments.
* ``PostgresRateLimiter`` — fixed-window counters stored in PostgreSQL with
  atomic upserts (``ON CONFLICT ... RETURNING``), correct across many
  processes/workers. Chosen via ``RATE_LIMIT_BACKEND``.

Fail-closed policy: ``build_rate_limiter`` refuses to silently pick a
per-process limiter for a multi-process PostgreSQL deployment — if
``RATE_LIMIT_BACKEND=memory`` is requested while ``DATABASE_URL`` is
PostgreSQL in production, configuration loading already rejects it (13P). The
Postgres limiter raises ``RateLimitBackendError`` if the database is
unreachable so a failure never silently widens the rate limit.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections import defaultdict, deque
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from portal_server.db.base import DatabaseBackend, _translate_placeholders

DEFAULT_LIMITS: dict[str, tuple[int, float]] = {
    "sign_in": (10, 60.0),
    "sign_up": (5, 3600.0),
    "password_reset_request": (5, 3600.0),
    "email_verify": (10, 300.0),
    "mfa_verify": (5, 60.0),
    "credential_create": (10, 300.0),
    "credential_rotate": (10, 300.0),
    "credential_revoke": (20, 300.0),
    "session_action": (20, 300.0),
    "webauthn_register": (10, 300.0),
    # Phase 12 developer-API platform limits.
    "pair_begin": (10, 300.0),
    "pair_approve": (10, 300.0),
    "token_issue": (20, 300.0),
    "token_refresh": (20, 300.0),
    "credential_verify": (20, 300.0),
    "device_register": (10, 300.0),
}


class RateLimitBackendError(Exception):
    """Raised when a distributed limiter cannot reach its backing store."""


class RateLimiter(ABC):
    """Common rate-limiter interface."""

    @abstractmethod
    def allow(self, scope: str, key: str) -> bool:
        """Return True if the request is within the configured limit."""

    @abstractmethod
    def reset(self, scope: str, key: str) -> None:
        """Clear the counter for a scope/key (testing/ops)."""


class InMemoryRateLimiter(RateLimiter):
    """Sliding-window in-memory rate limiter with an injectable clock."""

    def __init__(
        self,
        *,
        limits: dict[str, tuple[int, float]] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        max_buckets: int = 4096,
    ) -> None:
        self._limits = limits if limits is not None else DEFAULT_LIMITS
        self._clock = monotonic
        self._buckets: dict[str, deque[float]] = defaultdict(deque)
        self._max_buckets = max_buckets

    def allow(self, scope: str, key: str) -> bool:
        limit, window = self._limits.get(scope, (0, 1.0))
        if limit <= 0:
            return True
        bucket_key = f"{scope}:{key}"
        now = self._clock()
        bucket = self._buckets[bucket_key]
        while bucket and now - bucket[0] > window:
            bucket.popleft()
        if len(bucket) >= limit:
            return False
        bucket.append(now)
        self._trim()
        return True

    def reset(self, scope: str, key: str) -> None:
        self._buckets.pop(f"{scope}:{key}", None)

    def _trim(self) -> None:
        if len(self._buckets) <= self._max_buckets:
            return
        while len(self._buckets) > self._max_buckets // 2:
            self._buckets.pop(next(iter(self._buckets)))


class PostgresRateLimiter(RateLimiter):
    """Fixed-window counters in PostgreSQL, atomic across processes.

    The counter table holds one row per ``scope:key``. Each check atomically
    increments (or resets) the counter via ``ON CONFLICT ... RETURNING`` and
    compares the new count against the limit.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS rate_limit_counters (
        scope_key TEXT PRIMARY KEY,
        window_start TEXT NOT NULL,
        count BIGINT NOT NULL,
        updated_at TEXT NOT NULL
    );
    """

    def __init__(
        self,
        db: DatabaseBackend,
        *,
        limits: dict[str, tuple[int, float]] | None = None,
    ) -> None:
        if db.backend_name != "postgresql":
            raise RateLimitBackendError("PostgresRateLimiter requires the PostgreSQL backend.")
        self._db = db
        self._limits = limits if limits is not None else DEFAULT_LIMITS
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        self._db.execute(self._SCHEMA)

    def _window_start(self, window: float, now: float) -> str:
        epoch = int(now // window) * int(window)
        return datetime.fromtimestamp(epoch, tz=UTC).isoformat(timespec="seconds")

    def allow(self, scope: str, key: str) -> bool:
        limit, window = self._limits.get(scope, (0, 1.0))
        if limit <= 0:
            return True
        now = time.time()
        window_start = self._window_start(window, now)
        updated = datetime.now(UTC).isoformat(timespec="seconds")
        try:
            sql = _translate_placeholders(
                "INSERT INTO rate_limit_counters(scope_key, window_start, count, updated_at) "
                "VALUES(?, ?, 1, ?) "
                "ON CONFLICT (scope_key) DO UPDATE SET "
                "count = CASE WHEN rate_limit_counters.window_start = EXCLUDED.window_start "
                "THEN rate_limit_counters.count + 1 ELSE 1 END, "
                "window_start = CASE WHEN rate_limit_counters.window_start = EXCLUDED.window_start "
                "THEN rate_limit_counters.window_start ELSE EXCLUDED.window_start END, "
                "updated_at = EXCLUDED.updated_at "
                "RETURNING count"
            )
            row = self._db.query_one(sql, (f"{scope}:{key}", window_start, updated))
        except Exception as exc:
            raise RateLimitBackendError("Rate-limit store unavailable; failing closed.") from exc
        count = int(row["count"]) if row else 1
        return count <= limit

    def reset(self, scope: str, key: str) -> None:
        self._db.execute(
            "DELETE FROM rate_limit_counters WHERE scope_key=?",
            (f"{scope}:{key}",),
        )

    def cleanup(self, *, max_age_seconds: float = 86400.0) -> int:
        """Remove stale counter rows (bounded, idempotent)."""
        cutoff = datetime.now(UTC) - timedelta(seconds=max_age_seconds)
        self._db.execute(
            "DELETE FROM rate_limit_counters WHERE updated_at < ?",
            (cutoff.isoformat(timespec="seconds"),),
        )
        return 0


def build_rate_limiter(
    backend: str, db: DatabaseBackend, *, overrides: dict[str, tuple[int, float]] | None = None
) -> RateLimiter:
    """Construct the configured limiter, failing closed on misuse."""
    limits = overrides if overrides else DEFAULT_LIMITS
    if backend == "postgresql":
        return PostgresRateLimiter(db, limits=limits)
    if backend == "memory":
        return InMemoryRateLimiter(limits=limits)
    raise RateLimitBackendError(f"Unknown RATE_LIMIT_BACKEND: {backend!r}")


# Backward-compatible alias.
RateLimiterInMemory = InMemoryRateLimiter


__all__ = [
    "DEFAULT_LIMITS",
    "InMemoryRateLimiter",
    "PostgresRateLimiter",
    "RateLimitBackendError",
    "RateLimiter",
    "build_rate_limiter",
]
