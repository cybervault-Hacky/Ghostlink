"""Developer-key generation, formatting, and verification (Phase 10A).

A developer key is a *local API credential* for a GhostLink developer. It
is entirely distinct from the E2E messaging identity (Ed25519), session
keys, group keys, sender keys, and one-time invite tokens — it lives in its
own namespace (``gl_dev_…``) and is never derived from any of those, nor
from any predictable user/device identifier.

Credential format (documented in docs/DEVELOPER_ACCOUNTS.md)::

    gl_dev_<key_id>_<secret>

* ``key_id`` is ``dk_`` + 8 base32 chars (Crockford alphabet) from 5 random
  bytes = **40 bits** of identifier entropy. It is public, non-secret
  metadata used to look up, list, revoke, and audit a credential.
* ``secret`` is base64url (no padding) of **32 random bytes = 256 bits** of
  CSPRNG entropy. It is the authentication secret.

Both are generated with ``secrets.token_bytes`` (the Python standard-library
CSPRNG). ``random.random`` / ``uuid4`` are never used as the entropy source.
The raw secret is returned from the generator exactly once and is never
persisted.

Verification model
------------------
We do **not** store the plaintext secret. For each credential we store
verification material: a random 16-byte salt and a 32-byte HKDF-SHA256
digest::

    hash = HKDF-SHA256(salt=salt, info="ghostlink/developer-key/v1", ikm=secret)

The random salt binds each stored hash to that credential and prevents
precomputation/reuse across credentials. Comparison uses constant-time
``hmac.compare_digest``. Because the secret carries 256 bits of entropy, a
leaked hash does not enable offline guessing, but the salted-HKDF
construction is still the standard, versioned choice rather than a bare
``SHA-256(key)``.

A ``verifier`` version field ("v1") is stored alongside so a future,
incompatible verification scheme fails closed instead of being
misinterpreted.
"""

from __future__ import annotations

import base64
import binascii
import hmac
import re
import secrets

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ghostlink.developer.errors import (
    DeveloperCredentialInvalidError,
    DeveloperVerificationError,
)

# --- identifiers -----------------------------------------------------------

KEY_ID_PREFIX = "dk_"
CREDENTIAL_PREFIX = "gl_dev_"
ACCOUNT_PREFIX = "dev_"

# Crockford base32: unambiguous, no I/L/O/U.
_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

KEY_ID_BODY_CHARS = 8  # 5 random bytes -> 8 base32 chars (40 bits)
SECRET_BYTES = 32  # 256 bits of CSPRNG entropy
SALT_BYTES = 16
HASH_BYTES = 32
VERIFIER_V1 = "v1"
_HKDF_INFO = b"ghostlink/developer-key/v1"

# Full credential: gl_dev_<key_id>_<secret>
_CREDENTIAL_RE = re.compile(r"^gl_dev_(dk_[0-9A-Z]{8})_([A-Za-z0-9_-]{43})$")
KEY_ID_RE = re.compile(r"^dk_[0-9A-Z]{8}$")


def _base32(value: bytes) -> str:
    """Encode bytes to a Crockford-base32 uppercase string (no padding)."""
    bits = 0
    accum = 0
    out: list[str] = []
    for byte in value:
        accum = (accum << 8) | byte
        bits += 8
        while bits >= 5:
            bits -= 5
            out.append(_ALPHABET[(accum >> bits) & 0x1F])
    if bits:
        out.append(_ALPHABET[(accum << (5 - bits)) & 0x1F])
    return "".join(out)


def _base64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _base64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    try:
        return base64.urlsafe_b64decode((text + padding).encode("ascii"))
    except (binascii.Error, ValueError) as exc:
        raise DeveloperCredentialInvalidError(
            "The developer key secret is not valid base64url.",
            hint="Copy the full key exactly as it was shown once at creation.",
        ) from exc


def generate_key_id() -> str:
    """A fresh, unique, non-secret public key identifier (``dk_`` + 8 chars)."""
    return KEY_ID_PREFIX + _base32(secrets.token_bytes(5))


def generate_secret() -> str:
    """A fresh 256-bit secret as unpadded base64url (43 chars)."""
    return _base64url_encode(secrets.token_bytes(SECRET_BYTES))


def generate_developer_id() -> str:
    """A fresh public account identifier (``dev_`` + 13 base32 chars = 64 bits)."""
    return ACCOUNT_PREFIX + _base32(secrets.token_bytes(8))


def issue_credential() -> tuple[str, str]:
    """Generate a brand-new (key_id, credential) pair.

    Returns ``(key_id, credential)`` where ``credential`` is the full
    ``gl_dev_…`` string. The caller displays ``credential`` exactly once; the
    plaintext must never be persisted or logged.
    """
    key_id = generate_key_id()
    secret = generate_secret()
    credential = f"{CREDENTIAL_PREFIX}{key_id}_{secret}"
    return key_id, credential


# --- parsing / validation --------------------------------------------------


def parse_credential(credential: str) -> tuple[str, str]:
    """Split a credential into ``(key_id, secret)``; any defect raises.

    Fails closed on empty/whitespace/Unicode/malformed input.
    """
    if not isinstance(credential, str):
        raise DeveloperCredentialInvalidError(
            "The developer key must be a string.",
            hint="Paste the full gl_dev_… key.",
        )
    cleaned = credential.strip()
    match = _CREDENTIAL_RE.fullmatch(cleaned)
    if match is None:
        raise DeveloperCredentialInvalidError(
            "The developer key does not match the gl_dev_… format.",
            hint="Copy the full key exactly as it was shown once at creation.",
        )
    key_id, secret = match.group(1), match.group(2)
    # Re-decode to guarantee the secret is exactly SECRET_BYTES.
    if len(_base64url_decode(secret)) != SECRET_BYTES:
        raise DeveloperCredentialInvalidError(
            "The developer key secret has the wrong length.",
            hint="Copy the full key exactly as it was shown once at creation.",
        )
    return key_id, secret


def is_valid_key_id(key_id: str) -> bool:
    return isinstance(key_id, str) and bool(KEY_ID_RE.fullmatch(key_id.strip()))


# --- verification material -------------------------------------------------


def make_verifier(secret: str) -> tuple[str, str, str]:
    """Create verification material for a secret.

    Returns ``(verifier_version, salt_b64, hash_b64)``. ``secret`` here is
    the parsed secret portion of a credential (the caller already validated
    it).
    """
    salt = secrets.token_bytes(SALT_BYTES)
    digest = _hkdf(salt, secret)
    return VERIFIER_V1, _base64url_encode(salt), _base64url_encode(digest)


def verify_secret(secret: str, verifier: str, salt_b64: str, hash_b64: str) -> bool:
    """Constant-time verify ``secret`` against stored verification material.

    Returns True on success; raises ``DeveloperCredentialInvalidError`` on a
    malformed stored blob, and ``DeveloperVerificationError`` on a mismatch.
    """
    if verifier != VERIFIER_V1:
        # Future/incompatible verification scheme — fail closed.
        raise DeveloperVerificationError(
            "This credential uses an unsupported verification scheme.",
            hint="Upgrade GhostLink before verifying this credential.",
        )
    try:
        salt = _base64url_decode(salt_b64)
        expected = _base64url_decode(hash_b64)
    except DeveloperCredentialInvalidError:
        raise
    if len(salt) != SALT_BYTES or len(expected) != HASH_BYTES:
        raise DeveloperCredentialInvalidError(
            "The stored credential verification material is malformed.",
            hint="The credential metadata is corrupt; rotate or recreate it.",
        )
    actual = _hkdf(salt, secret)
    if not hmac.compare_digest(actual, expected):
        raise DeveloperVerificationError(
            "The developer key did not verify.",
            hint="Check the key and try again; a revoked key is always rejected.",
        )
    return True


def _hkdf(salt: bytes, secret: str) -> bytes:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=HASH_BYTES,
        salt=salt,
        info=_HKDF_INFO,
    )
    return hkdf.derive(secret.encode("utf-8"))


__all__ = [
    "ACCOUNT_PREFIX",
    "CREDENTIAL_PREFIX",
    "HASH_BYTES",
    "KEY_ID_PREFIX",
    "SALT_BYTES",
    "SECRET_BYTES",
    "VERIFIER_V1",
    "generate_developer_id",
    "generate_key_id",
    "generate_secret",
    "is_valid_key_id",
    "issue_credential",
    "make_verifier",
    "parse_credential",
    "verify_secret",
]
