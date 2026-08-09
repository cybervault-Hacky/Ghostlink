"""Phase 13 — rate limiting: in-memory + PostgreSQL backend logic."""

from __future__ import annotations

from pathlib import Path

import pytest
from portal_server.db import Database
from portal_server.ratelimit import (
    DEFAULT_LIMITS,
    InMemoryRateLimiter,
    PostgresRateLimiter,
    RateLimitBackendError,
    build_rate_limiter,
)


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def test_in_memory_limits_and_window() -> None:
    clock = _Clock()
    limiter = InMemoryRateLimiter(limits={"s": (3, 10.0)}, monotonic=clock)
    assert limiter.allow("s", "k") is True
    assert limiter.allow("s", "k") is True
    assert limiter.allow("s", "k") is True
    assert limiter.allow("s", "k") is False
    clock.advance(11.0)
    assert limiter.allow("s", "k") is True
    limiter.reset("s", "k")


def test_in_memory_keys_are_independent() -> None:
    limiter = InMemoryRateLimiter(limits={"s": (1, 60.0)})
    assert limiter.allow("s", "a") is True
    assert limiter.allow("s", "a") is False
    assert limiter.allow("s", "b") is True


def test_default_limits_present() -> None:
    for scope in (
        "sign_in",
        "sign_up",
        "password_reset_request",
        "mfa_verify",
        "pair_begin",
        "pair_approve",
        "token_issue",
        "token_refresh",
        "credential_rotate",
        "credential_verify",
    ):
        assert scope in DEFAULT_LIMITS


def test_postgres_limiter_requires_pg_backend(tmp_path: Path) -> None:
    db = Database(tmp_path / "p.db")  # sqlite
    with pytest.raises(RateLimitBackendError):
        PostgresRateLimiter(db)
    db.close()


def test_build_rate_limiter_memory() -> None:
    db = Database(Path("/tmp") / "rl.db")
    limiter = build_rate_limiter("memory", db)
    assert isinstance(limiter, InMemoryRateLimiter)
    db.close()


def test_build_rate_limiter_unknown_backend() -> None:
    db = Database(Path("/tmp") / "rl2.db")
    with pytest.raises(RateLimitBackendError):
        build_rate_limiter("redis", db)
    db.close()
