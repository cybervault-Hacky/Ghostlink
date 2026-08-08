"""Ephemeral local identity (Phase 5).

One Ed25519 keypair per installation, an optional user-chosen nickname,
and short public handles/fingerprints for peer verification. No accounts,
no email, no phone numbers — identity is local key material.
"""

from ghostlink.identity.fingerprint import (
    identity_fingerprint,
    identity_id_for,
    is_valid_fingerprint,
    is_valid_identity_id,
)
from ghostlink.identity.identity import (
    MAX_NICKNAME_LENGTH,
    LocalIdentity,
    validate_nickname,
)
from ghostlink.identity.lifecycle import IdentityManager, fingerprint_for_hex
from ghostlink.identity.storage import IdentityStore

__all__ = [
    "MAX_NICKNAME_LENGTH",
    "IdentityManager",
    "IdentityStore",
    "LocalIdentity",
    "fingerprint_for_hex",
    "identity_fingerprint",
    "identity_id_for",
    "is_valid_fingerprint",
    "is_valid_identity_id",
    "validate_nickname",
]
