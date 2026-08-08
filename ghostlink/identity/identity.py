"""Local ephemeral identity (Phase 5).

Every installation owns one local identity: an Ed25519 keypair created on
first launch, stored locally with owner-only permissions, plus an optional
display nickname the user picks. There is no account, no email, no phone
number and no server-side registration — the identity is just key material
on this device.

The private key never leaves the device: it is never logged, never
displayed, never embedded in invite links, and never written into chat
history. Only the *public* key may travel (inside the end-to-end handshake)
so peers can derive the human-readable verification fingerprint.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from ghostlink.exceptions.invites import InviteValidationError
from ghostlink.exceptions.storage import StorageCorruptionError
from ghostlink.identity.fingerprint import identity_id_for

MAX_NICKNAME_LENGTH: int = 24

_STORAGE_VERSION: int = 1


def validate_nickname(nickname: str, *, field: str = "nickname") -> str:
    """A nickname must be 1..24 printable, non-control characters."""

    cleaned = nickname.strip()
    if not (0 < len(cleaned) <= MAX_NICKNAME_LENGTH):
        raise InviteValidationError(
            f"A {field} must be 1..{MAX_NICKNAME_LENGTH} characters, got {len(cleaned)}.",
            hint="Pick a short printable pseudonym, e.g. ShadowUser.",
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        raise InviteValidationError(
            f"A {field} must not contain control characters.",
            hint="Use plain printable text only.",
        )
    return cleaned


def _decode_private_key(private_hex: str) -> Ed25519PrivateKey:
    try:
        raw = bytes.fromhex(private_hex)
    except ValueError as exc:
        raise StorageCorruptionError(
            "The stored identity private key is not valid hex.",
            hint="Remove the identity store to start over with a fresh identity.",
        ) from exc
    try:
        return Ed25519PrivateKey.from_private_bytes(raw)
    except ValueError as exc:
        raise StorageCorruptionError(
            "The stored identity private key is malformed.",
            hint="Remove the identity store to start over with a fresh identity.",
        ) from exc


@dataclass(frozen=True, slots=True)
class LocalIdentity:
    """One installation-local identity (Ed25519 keypair + nickname)."""

    _private_key: Ed25519PrivateKey
    nickname: str
    created_at: datetime

    # ------------------------------------------------------------------ create

    @classmethod
    def generate(cls, *, nickname: str = "", now: datetime | None = None) -> LocalIdentity:
        """Mint a fresh identity with a cryptographically random keypair."""

        return cls(
            _private_key=Ed25519PrivateKey.generate(),
            nickname=validate_nickname(nickname) if nickname else "",
            created_at=now if now is not None else datetime.now(UTC),
        )

    # ------------------------------------------------------------- public view

    @property
    def _public_key(self) -> Ed25519PublicKey:
        return self._private_key.public_key()

    @property
    def public_key_bytes(self) -> bytes:
        return self._public_key.public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )

    @property
    def public_key_hex(self) -> str:
        """Hex-encoded public key — the only key material ever transmitted."""

        return self.public_key_bytes.hex()

    @property
    def identity_id(self) -> str:
        """Short public handle derived from the key, e.g. ``GL-7K3M``."""

        return identity_id_for(self.public_key_bytes)

    def sign(self, message: bytes) -> bytes:
        """Sign ``message`` with the identity key (Ed25519).

        Only the *signature* leaves this object — the private key itself
        never does. Used by the Phase 6B group lifecycle for proof of
        possession, session attestation, and membership-event countersigns.
        """

        return self._private_key.sign(message)

    def renamed(self, nickname: str) -> LocalIdentity:
        """Return a copy with a new nickname (the keypair is untouched)."""

        return LocalIdentity(
            _private_key=self._private_key,
            nickname=validate_nickname(nickname),
            created_at=self.created_at,
        )

    def with_nickname_cleared(self) -> LocalIdentity:
        return LocalIdentity(
            _private_key=self._private_key,
            nickname="",
            created_at=self.created_at,
        )

    # ------------------------------------------------------------ serialization

    def to_storage_dict(self) -> dict[str, object]:
        """Persisted form. Lives exclusively in the owner-only identity store."""

        private_hex = self._private_key.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        ).hex()
        return {
            "v": _STORAGE_VERSION,
            "private_key": private_hex,
            "nickname": self.nickname,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_storage_dict(cls, data: dict[str, object]) -> LocalIdentity:
        """Rebuild an identity from storage, rejecting corrupt documents."""

        expected = {"private_key", "nickname", "created_at"}
        missing = expected - set(data)
        if missing:
            raise StorageCorruptionError(
                f"The stored identity is missing field(s): {', '.join(sorted(missing))}.",
                hint="Remove the identity store to start over with a fresh identity.",
            )
        private_hex = str(data["private_key"])
        nickname_raw = data["nickname"]
        nickname = validate_nickname(str(nickname_raw)) if str(nickname_raw).strip() else ""
        try:
            created_at = datetime.fromisoformat(str(data["created_at"]))
        except ValueError as exc:
            raise StorageCorruptionError(
                "The stored identity creation timestamp is malformed.",
                hint="Remove the identity store to start over with a fresh identity.",
            ) from exc
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        return cls(
            _private_key=_decode_private_key(private_hex),
            nickname=nickname,
            created_at=created_at,
        )
