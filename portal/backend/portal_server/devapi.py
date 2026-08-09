"""Developer API platform (Phase 12).

A narrowly scoped, authenticated, auditable, revocable developer API at
``/api/v1/developer/*``. It lets an authenticated Termux developer
installation communicate with the portal without exposing permanent
secrets.

Key properties:
* **Scopes** — least-privilege, checked server-side, never trusted from the
  client. No owner-scope ever exists.
* **Tokens** — short-lived opaque access tokens + longer-lived rotating
  refresh tokens, stored only as hashes. Never in URLs or logs.
* **Devices** — cryptographically random device IDs (no hardware
  identifiers), revocable, fail-closed.
* **Pairing** — short-lived, single-use, random pairing codes; never the
  permanent credential.
* **Project binding** — credentials/tokens may be bound to a project; the
  server resolves ownership and enforces it on every call (no client-
  supplied project identity).
* **Owner rule** — exactly one Owner. Developer accounts can never become
  Owner or transfer ownership. No endpoint grants the owner role.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from typing import Any

from portal_server.http import Response, error_response

# Owner is a role on the users table; exactly one user may hold it, and no
# developer-facing endpoint can set it.
ROLE_OWNER = "owner"
ROLE_DEVELOPER = "developer"
_VALID_ROLES = {ROLE_OWNER, ROLE_DEVELOPER}

# Least-privilege scopes. No owner/root scope exists by design.
VALID_SCOPES: frozenset[str] = frozenset(
    {
        "project:read",
        "project:write",
        "device:read",
        "device:write",
        "credential:read",
        "credential:rotate",
        "security:read",
    }
)

ACCESS_TOKEN_TTL_SECONDS = 15 * 60  # 15 minutes
REFRESH_TOKEN_TTL_SECONDS = 30 * 24 * 3600  # 30 days
PAIRING_CODE_TTL_SECONDS = 10 * 60  # 10 minutes

_DEVICE_ID_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
_DEVICE_ID_CHARS = 20  # ~ 100 bits
# public credential id prefix (matches Phase 10A style)
CREDENTIAL_ID_PREFIX = "dk_"
_CREDENTIAL_ID_CHARS = 8


def _b32(value: bytes) -> str:
    bits = 0
    accum = 0
    out: list[str] = []
    for byte in value:
        accum = (accum << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append(_DEVICE_ID_ALPHABET[(accum >> bits) & 0x1F])
    if bits:
        out.append(_DEVICE_ID_ALPHABET[(accum << (5 - bits)) & 0x1F])
    return "".join(out)


def new_device_id() -> str:
    """A cryptographically random device identifier (no hardware id)."""
    return _b32(secrets.token_bytes(_DEVICE_ID_CHARS * 5 // 8))


def new_credential_id() -> str:
    return CREDENTIAL_ID_PREFIX + _b32(secrets.token_bytes(5))[:_CREDENTIAL_ID_CHARS]


def new_pairing_code() -> str:
    """A short human-friendly, high-entropy pairing code."""
    return "GL-" + "-".join(_b32(secrets.token_bytes(3)) for _ in range(4))


def new_access_token() -> str:
    return secrets.token_urlsafe(32)


def new_refresh_token() -> str:
    return secrets.token_urlsafe(32)


def normalize_scopes(scopes: str) -> frozenset[str]:
    """Validate and normalize a space-separated scope list; rejects unknown."""
    requested = {s for s in scopes.split() if s}
    if not requested:
        return frozenset()
    for scope in requested:
        if scope not in VALID_SCOPES:
            raise ValueError(f"Unknown scope: {scope}")
    return frozenset(requested)


class ScopedCredentialError(Exception):
    """A scoped developer-API error (never carries secret material)."""

    def __init__(self, message: str, *, code: str = "error", status: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def _scope_ok(token_scopes: frozenset[str], required: str) -> bool:
    return required in token_scopes


class DeveloperAPI:
    """Holds shared state for developer-API operations (per app instance)."""

    def __init__(self, db: Any, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.db = db
        self.clock = clock
        self.pairing_attempts: dict[str, list[float]] = {}  # ip -> timestamps


def api_error_response(exc: ScopedCredentialError, request_id: str = "") -> Response:
    payload = {"error": {"code": exc.code, "message": exc.message}}
    if request_id:
        payload["error"]["request_id"] = request_id
    return error_response(exc.status, exc.code, exc.message)


__all__ = [
    "ACCESS_TOKEN_TTL_SECONDS",
    "CREDENTIAL_ID_PREFIX",
    "PAIRING_CODE_TTL_SECONDS",
    "REFRESH_TOKEN_TTL_SECONDS",
    "ROLE_DEVELOPER",
    "ROLE_OWNER",
    "VALID_SCOPES",
    "DeveloperAPI",
    "ScopedCredentialError",
    "api_error_response",
    "new_access_token",
    "new_credential_id",
    "new_device_id",
    "new_pairing_code",
    "new_refresh_token",
    "normalize_scopes",
]
