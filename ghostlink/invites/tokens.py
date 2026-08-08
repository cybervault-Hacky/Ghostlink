"""Invite tokens and ``gl://join/…`` links (Phase 5).

A token is 20 characters from the room alphabet (no ``0/O/1/I``), drawn
with :mod:`secrets` — about 103 bits of entropy. Tokens are never derived
from timestamps or counters, so they are unguessable and non-sequential.

The ``gl://join/<token>`` link is only a convenient *representation* of an
invite for GhostLink users: it is typed or pasted into this terminal app.
It deliberately contains no IP addresses, keys, passwords, or paths. If a
link ends up in a normal browser, nothing usable happens there — invites
are redeemed by GhostLink only.
"""

from __future__ import annotations

import hashlib
import re
import secrets

from ghostlink.constants.net import (
    INVITE_ID_PREFIX,
    INVITE_LINK_HOST,
    INVITE_LINK_SCHEME,
    INVITE_LINK_TOKEN_LENGTH,
    ROOM_ID_ALPHABET,
)
from ghostlink.exceptions.invites import InviteValidationError

_TOKEN_PATTERN: re.Pattern[str] = re.compile(
    rf"^[{re.escape(ROOM_ID_ALPHABET)}]{{{INVITE_LINK_TOKEN_LENGTH}}}$"
)
_LINK_PATTERN: re.Pattern[str] = re.compile(
    rf"^{re.escape(INVITE_LINK_SCHEME)}://{re.escape(INVITE_LINK_HOST)}/"
    rf"([{re.escape(ROOM_ID_ALPHABET)}]{{{INVITE_LINK_TOKEN_LENGTH}}})$",
    re.IGNORECASE,
)


def generate_invite_token() -> str:
    """A fresh invite token: 20 unambiguous characters from a CSPRNG."""

    return "".join(secrets.choice(ROOM_ID_ALPHABET) for _ in range(INVITE_LINK_TOKEN_LENGTH))


def is_valid_invite_token(candidate: str) -> bool:
    """Strict format validation for join-link tokens."""

    return bool(_TOKEN_PATTERN.fullmatch(candidate.strip().upper()))


def normalize_invite_token(candidate: str) -> str:
    """Uppercase a token-after-validation; raises when malformed."""

    cleaned = candidate.strip().upper()
    if not _TOKEN_PATTERN.fullmatch(cleaned):
        raise InviteValidationError(
            "That does not look like a GhostLink invite token.",
            hint="Tokens are 20 letters/digits, e.g. gl://join/8F7K2MQ3W2J4X6B9DZP4.",
        )
    return cleaned


def token_hash_for(token: str) -> str:
    """Full SHA-256 digest of a token — storage-safe token stand-in."""

    return hashlib.sha256(normalize_invite_token(token).encode("ascii")).hexdigest()


def invite_id_for_token(token: str) -> str:
    """Public, log-safe identifier derived from the token (never the token).

    ``gi_`` + 10 hex of SHA-256 — safe for logs, ``invite list`` output,
    and relay bookkeeping. Knowing it grants nothing.
    """

    return f"{INVITE_ID_PREFIX}{token_hash_for(token)[:10]}"


def is_valid_invite_id(candidate: str) -> bool:
    """Format check for the public ``gi_…`` identifier."""

    cleaned = candidate.strip().lower()
    return (
        cleaned.startswith(INVITE_ID_PREFIX)
        and len(cleaned) == len(INVITE_ID_PREFIX) + 10
        and all(char in "0123456789abcdef" for char in cleaned[len(INVITE_ID_PREFIX) :])
    )


def format_invite_link(token: str) -> str:
    """``gl://join/<token>`` — the terminal-shareable invite representation."""

    return f"{INVITE_LINK_SCHEME}://{INVITE_LINK_HOST}/{normalize_invite_token(token)}"


def looks_like_invite_link(candidate: str) -> bool:
    """Cheap pre-check: starts with the gl://join prefix (case-insensitive)."""

    return candidate.strip().lower().startswith(f"{INVITE_LINK_SCHEME}://{INVITE_LINK_HOST}/")


def parse_invite_link(candidate: str) -> str:
    """Extract the token from a ``gl://join/<token>`` link.

    Validates scheme, host and token format; raises
    :class:`InviteValidationError` with a precise reason otherwise.
    """

    cleaned = candidate.strip()
    scheme_part, separator, remainder = cleaned.partition("://")
    if not separator or scheme_part.lower() != INVITE_LINK_SCHEME:
        raise InviteValidationError(
            "Invite links must start with 'gl://'.",
            hint="Paste the full link your peer shared, e.g. gl://join/8F7K2MQ3…",
        )
    host_part, separator, _token_part = remainder.partition("/")
    if not separator or host_part.lower() != INVITE_LINK_HOST:
        raise InviteValidationError(
            "Invite links must read gl://join/<token>.",
            hint="Check that no characters were lost when copying the link.",
        )
    match = _LINK_PATTERN.fullmatch(cleaned)
    if match is None:
        raise InviteValidationError(
            "The invite token inside the link is malformed.",
            hint="Tokens are exactly 20 unambiguous letters/digits —"
            " re-copy the link from the source.",
        )
    return match.group(1).upper()
