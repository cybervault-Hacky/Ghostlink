"""Security headers, CSRF helpers, and backward-compatible rate limiter re-exports.

The rate limiter implementations moved to ``portal_server.ratelimit`` (Phase
13F) which adds a distributed PostgreSQL backend. ``RateLimiter`` and
``DEFAULT_LIMITS`` are re-exported here so Phase 10-12 imports keep working.
"""

from __future__ import annotations

from portal_server.ratelimit import DEFAULT_LIMITS, InMemoryRateLimiter, RateLimiter

# Backward-compatible alias: the historical in-memory limiter.
RateLimiterInMemory = InMemoryRateLimiter

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
