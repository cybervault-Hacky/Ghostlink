"""Human-readable identity fingerprints (Phase 5).

A fingerprint is ``SHA-256(public key)`` formatted as ``GLFP-XXXX-XXXX-XXXX``
(48 bits shown). It exists for one purpose: two people can compare it
through a trusted out-of-band channel (a phone call, another messenger) to
confirm they are looking at the same public identity key.

Derived handles and fingerprints are computed from *public* material only —
the private key never participates.
"""

from __future__ import annotations

import hashlib
import re

from ghostlink.constants.net import ROOM_ID_ALPHABET

FINGERPRINT_PREFIX: str = "GLFP"
FINGERPRINT_PATTERN: re.Pattern[str] = re.compile(r"^GLFP(?:-[0-9A-F]{4}){3}$")


def identity_fingerprint(public_key: bytes) -> str:
    """``GLFP-7A92-31CF-88B4``-style fingerprint of a 32-byte public key."""

    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise ValueError("An Ed25519 public key must be exactly 32 bytes.")
    digest = hashlib.sha256(public_key).hexdigest().upper()
    return f"{FINGERPRINT_PREFIX}-{digest[0:4]}-{digest[4:8]}-{digest[8:12]}"


def is_valid_fingerprint(candidate: str) -> bool:
    """Format check for user-typed fingerprints."""

    return bool(FINGERPRINT_PATTERN.fullmatch(candidate.strip().upper()))


def identity_id_for(public_key: bytes) -> str:
    """Short public handle like ``GL-7K3M``, derived from the key digest."""

    if not isinstance(public_key, bytes) or len(public_key) != 32:
        raise ValueError("An Ed25519 public key must be exactly 32 bytes.")
    digest = hashlib.sha256(public_key).digest()
    chars = []
    # 20 digest bits → 4 alphabet characters (5 bits each).
    value = int.from_bytes(digest[:3], "big") & 0xFFFFF
    for shift in (15, 10, 5, 0):
        chars.append(ROOM_ID_ALPHABET[(value >> shift) & 0x1F])
    return "GL-" + "".join(chars)


def is_valid_identity_id(candidate: str) -> bool:
    """Format check for ``GL-XXXX`` handles."""

    cleaned = candidate.strip().upper()
    return (
        len(cleaned) == 7
        and cleaned.startswith("GL-")
        and all(char in ROOM_ID_ALPHABET for char in cleaned[3:])
    )
