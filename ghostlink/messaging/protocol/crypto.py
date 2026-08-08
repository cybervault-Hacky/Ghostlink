"""End-to-end encryption primitives (Phase 3).

One fresh X25519 ephemeral key pair per conversation side, one ECDH, one
HKDF — every conversation runs on a fresh session key with forward secrecy
at conversation granularity. Messages are sealed with ChaCha20-Poly1305
(AEAD), giving confidentiality *and* tamper detection in one primitive.

Keys live only in process memory. Nothing in this module touches disk,
and ciphertext/plaintext are never logged — only byte counts.
"""

from __future__ import annotations

import hashlib
import secrets

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from ghostlink.exceptions.messaging import DecryptionError

HKDF_INFO_SESSION: bytes = b"ghostlink/session/v1"
NONCE_BYTES: int = 12
PUBLIC_KEY_BYTES: int = 32
SESSION_KEY_BYTES: int = 32


def generate_ephemeral_keypair() -> tuple[X25519PrivateKey, bytes]:
    """A fresh ephemeral X25519 key pair (public key as raw bytes)."""

    private_key = X25519PrivateKey.generate()
    public_raw = private_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return private_key, public_raw


def generate_handshake_nonce() -> bytes:
    """Handshake nonce contributing freshness to the session transcript."""

    return secrets.token_bytes(16)


def derive_session_key(
    private_key: X25519PrivateKey,
    peer_public: bytes,
    transcript: bytes,
) -> bytes:
    """X25519 ECDH, then HKDF-SHA256 bound to the handshake transcript.

    ``transcript`` must commit to everything both sides observed (channel id,
    both public keys, both nonces) so the derived key is bound to *this*
    conversation and no other. Raises :class:`HandshakeFailedError`-shaped
    :class:`DecryptionError` only via callers; invalid public keys raise
    ``ValueError`` from ``cryptography`` here and are wrapped by the caller.
    """

    if len(peer_public) != PUBLIC_KEY_BYTES:
        raise ValueError(f"peer public key must be {PUBLIC_KEY_BYTES} bytes")
    peer_key = X25519PublicKey.from_public_bytes(peer_public)
    shared_secret = private_key.exchange(peer_key)
    hkdf = HKDF(
        algorithm=SHA256(),
        length=SESSION_KEY_BYTES,
        salt=hashlib.sha256(transcript).digest(),
        info=HKDF_INFO_SESSION,
    )
    return hkdf.derive(shared_secret)


def seal(key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """Encrypt ``plaintext`` with a fresh random nonce; returns nonce+CT."""

    nonce = secrets.token_bytes(NONCE_BYTES)
    ciphertext = ChaCha20Poly1305(key).encrypt(nonce, plaintext, aad)
    return nonce + ciphertext


def open_sealed(key: bytes, sealed: bytes, aad: bytes) -> bytes:
    """Verify and decrypt ``nonce+CT``. Tampering raises DecryptionError."""

    if len(sealed) <= NONCE_BYTES:
        raise DecryptionError(
            "Encrypted payload is too short to contain a nonce and tag.",
            hint="The frame was truncated or corrupted before verification.",
        )
    nonce, ciphertext = sealed[:NONCE_BYTES], sealed[NONCE_BYTES:]
    try:
        return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise DecryptionError(
            "Message integrity check failed — the ciphertext was modified "
            "or the keys do not match.",
            hint="Drop the frame, notify the user, and re-run the handshake "
            "if it repeats. Do not retry decryption with the same input.",
        ) from exc


def transcript_fingerprint(transcript: bytes) -> str:
    """Human-comparable safety code: 8 groups of 4 hex chars.

    Peers compare this out-of-band (voice, note) to rule out a relay-level
    MITM. It commits to the full handshake transcript, never to key material.
    """

    digest = hashlib.sha256(transcript).hexdigest()[:32].upper()
    return " ".join(digest[index : index + 4] for index in range(0, 32, 4))
