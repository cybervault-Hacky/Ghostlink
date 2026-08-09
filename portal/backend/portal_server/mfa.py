"""TOTP (RFC 6238) for Multi-Factor Authentication (Phase 10B).

Standard HMAC-SHA1 TOTP with a 30-second period and 6-digit codes, base32
secret. The server stores the base32 secret; recovery codes are stored as
SHA-256 hashes (never plaintext).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import time


def generate_totp_secret() -> str:
    """A random 32-byte secret base32-encoded (160 bits of entropy)."""
    raw = secrets.token_bytes(32)
    return base64.b32encode(raw).decode("ascii").rstrip("=")


def _hotp(secret_b32: str, counter: int, digits: int = 6) -> str:
    key = base64.b32decode(secret_b32 + "=" * (-len(secret_b32) % 8))
    message = struct.pack(">Q", counter)
    digest = hmac.new(key, message, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    return str(binary % (10**digits)).zfill(digits)


def current_totp(secret_b32: str, *, now: int | None = None) -> str:
    counter = int((now if now is not None else int(time.time())) // 30)
    return _hotp(secret_b32, counter)


def verify_totp(secret_b32: str, code: str, *, now: int | None = None, window: int = 1) -> bool:
    """Verify a TOTP code allowing ``window`` steps of clock drift."""
    if not code.isdigit() or len(code) != 6:
        return False
    current = int((now if now is not None else int(time.time())) // 30)
    for counter in range(current - window, current + window + 1):
        candidate = _hotp(secret_b32, counter)
        if hmac.compare_digest(candidate, code):
            return True
    return False


__all__ = ["current_totp", "generate_totp_secret", "verify_totp"]
