"""Encryption protocol: key exchange and AEAD primitives (Phase 3)."""

from ghostlink.messaging.protocol.crypto import (
    derive_session_key,
    generate_ephemeral_keypair,
    open_sealed,
    seal,
    transcript_fingerprint,
)
from ghostlink.messaging.protocol.handshake import (
    HandshakeInitiator,
    HandshakeResponder,
    SecureSession,
)

__all__ = [
    "HandshakeInitiator",
    "HandshakeResponder",
    "SecureSession",
    "derive_session_key",
    "generate_ephemeral_keypair",
    "open_sealed",
    "seal",
    "transcript_fingerprint",
]
