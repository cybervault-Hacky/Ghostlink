"""Integrity and per-transfer key handling (Phase 4).

This module is deliberately thin: it *reuses* the Phase 3 primitives
(HKDF-SHA256 and ChaCha20-Poly1305 from :mod:`ghostlink.messaging.protocol.crypto`)
rather than inventing a second cryptographic system. Each transfer derives a
fresh sub-key from the conversation's session key — domain-separated by the
transfer id — so chunk ciphertexts never share a key context with chat
messages or with any other transfer.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ghostlink.exceptions.messaging import DecryptionError
from ghostlink.messaging.protocol.crypto import open_sealed, seal

_HASH_BUFFER: int = 64 * 1024
_TRANSFER_KEY_BYTES: int = 32
_TRANSFER_INFO_PREFIX: bytes = b"ghostlink/transfer/v1|"


def hash_file(path: Path) -> str:
    """Stream a file through SHA-256; O(1) memory regardless of size."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            block = handle.read(_HASH_BUFFER)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def hash_temp(path: Path, *, expected_size: int) -> str:
    """Stream-hash a just-written ``.part`` file for final verification."""

    stat = path.stat()
    if stat.st_size != expected_size:
        raise DecryptionError(
            "Resumed file size does not match the manifest.",
            hint="Delete the partial download and request the file again.",
        )
    return hash_file(path)


def derive_transfer_key(session_key: bytes, transfer_id: str) -> bytes:
    """HKDF-expand the conversation session key into a per-transfer key.

    Info binds the key to ``ghostlink/transfer/v1`` and the transfer id, so
    two transfers in one conversation never share chunk key material, and
    transfer keys change automatically whenever the session re-keys."""

    hkdf = HKDF(
        algorithm=SHA256(),
        length=_TRANSFER_KEY_BYTES,
        salt=None,
        info=_TRANSFER_INFO_PREFIX + transfer_id.encode("ascii"),
    )
    return hkdf.derive(session_key)


def seal_chunk(key: bytes, transfer_id: str, n: int, plaintext: bytes) -> bytes:
    """AEAD-seal one chunk, bound to ``transfer_id|n`` as associated data."""

    return seal(key, plaintext, aad=f"{transfer_id}|{n}".encode("ascii"))


def open_chunk(key: bytes, transfer_id: str, n: int, sealed: bytes) -> bytes:
    """Verify + open one sealed chunk; tampering raises DecryptionError."""

    return open_sealed(key, sealed, aad=f"{transfer_id}|{n}".encode("ascii"))


def seal_manifest(key: bytes, transfer_id: str, manifest_json: bytes) -> bytes:
    """AEAD-seal the manifest so the relay never sees filename or size."""

    return seal(key, manifest_json, aad=f"{transfer_id}|manifest".encode("ascii"))


def open_manifest(key: bytes, transfer_id: str, sealed: bytes) -> bytes:
    """Verify + open a sealed manifest."""

    return open_sealed(key, sealed, aad=f"{transfer_id}|manifest".encode("ascii"))
