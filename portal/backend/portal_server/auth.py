"""Authentication primitives for the Developer Portal (Phase 10B).

Password hashing (PBKDF2-HMAC-SHA256 with per-user salt and 600k
iterations), CSPRNG session/token values with hash-at-rest storage,
CSRF tokens, and constant-time comparisons. Nothing here stores a
plaintext password, token, or session secret.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import time

PBKDF2_ITERATIONS = 600_000
_SALT_BYTES = 16
_PREFIX = "pbkdf2_sha256"


def hash_password(password: str) -> str:
    """Return a self-describing verifier: ``pbkdf2_sha256$it$salt$hash``."""
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    salt_b64 = base64.b64encode(salt).decode()
    hash_b64 = base64.b64encode(digest).decode()
    return f"{_PREFIX}${PBKDF2_ITERATIONS}${salt_b64}${hash_b64}"


def verify_password(password: str, verifier: str) -> bool:
    """Constant-time check of ``password`` against a stored verifier."""
    try:
        prefix, iterations_s, salt_b64, hash_b64 = verifier.split("$")
        if prefix != _PREFIX:
            return False
        iterations = int(iterations_s)
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(hash_b64)
    except (ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def hash_secret(value: str) -> str:
    """SHA-256 of a secret (for session/token/recovery-code at-rest hashes)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(24)


def make_token() -> str:
    return secrets.token_urlsafe(32)


def constant_time_equal(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def generate_recovery_code() -> str:
    """A 10-character Crockford-ish recovery code with a checksum-ish prefix.

    8 random bytes -> 13 base32 chars; human-readable but high-entropy.
    """
    from ghostlink.developer.keys import _base32

    return _base32(secrets.token_bytes(8))


def now_iso() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat(timespec="seconds")


def _monotonic() -> float:
    return time.monotonic()


__all__ = [
    "PBKDF2_ITERATIONS",
    "constant_time_equal",
    "generate_recovery_code",
    "hash_password",
    "hash_secret",
    "make_token",
    "new_csrf_token",
    "new_session_token",
    "now_iso",
    "verify_password",
]
