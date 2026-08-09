"""Rate limiting, security headers, and CSRF helpers (Phase 10B).

In-memory, deterministic, and clock-injectable for tests. Rate limits are
both IP-based and account-based where appropriate; email-existence is never
revealed by password-reset endpoints (generic responses).
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable

# (scope, key) -> deque[timestamps]; window-based.
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
    # Phase 12 developer-API platform limits.
    "pair_begin": (10, 300.0),
    "pair_approve": (10, 300.0),
    "token_issue": (20, 300.0),
    "token_refresh": (20, 300.0),
}


class RateLimiter:
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

    def _trim(self) -> None:
        if len(self._buckets) <= self._max_buckets:
            return
        # Drop oldest buckets to bound memory.
        while len(self._buckets) > self._max_buckets // 2:
            self._buckets.pop(next(iter(self._buckets)))


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "X-Frame-Options": "DENY",
    "Permissions-Policy": ("camera=(), microphone=(), geolocation=(), payment=(), usb=()"),
    "Content-Security-Policy": (
        "default-src 'self'; "
        "script-src 'self'; "
        "style-src 'self' 'unsafe-inline'; "
        "img-src 'self' data:; "
        "font-src 'self'; "
        "connect-src 'self'; "
        "frame-ancestors 'none'; "
        "base-uri 'self'; "
        "form-action 'self'"
    ),
}

# Strict-Transport-Security is applied in production only (over HTTPS).
HSTS_HEADER = "Strict-Transport-Security: max-age=31536000; includeSubDomains"


__all__ = [
    "DEFAULT_LIMITS",
    "HSTS_HEADER",
    "SECURITY_HEADERS",
    "RateLimiter",
]
