"""Developer-account data models (Phase 10A).

Persisted documents are versioned and fail closed on a future schema
version (reusing the Phase 9 migration infrastructure). They are
metadata-only: the plaintext developer secret is never stored — only the
salted-HKDF verification material and public identifiers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from ghostlink.core.migration import validate_state_version

DEVELOPER_ACCOUNT_SCHEMA_VERSION = 1
DEVELOPER_CREDENTIAL_SCHEMA_VERSION = 1

_DATETIME_ISO = "%Y-%m-%dT%H:%M:%S+00:00"


def _now() -> datetime:
    return datetime.now(UTC)


def _fmt(moment: datetime | None) -> str | None:
    return moment.astimezone(UTC).isoformat() if moment is not None else None


def _parse(moment: str | None) -> datetime | None:
    if not moment:
        return None
    parsed = datetime.fromisoformat(moment)
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


@dataclass(slots=True)
class DeveloperCredential:
    """One developer API credential (metadata + verification material only)."""

    key_id: str
    status: str  # "active" | "revoked"
    created_at: datetime
    verifier: str
    salt_b64: str
    hash_b64: str
    last_used_at: datetime | None = None

    @property
    def is_active(self) -> bool:
        return self.status == "active"

    def to_dict(self) -> dict[str, object]:
        return {
            "v": DEVELOPER_CREDENTIAL_SCHEMA_VERSION,
            "key_id": self.key_id,
            "status": self.status,
            "created_at": _fmt(self.created_at),
            "last_used_at": _fmt(self.last_used_at),
            "verifier": self.verifier,
            "salt": self.salt_b64,
            "hash": self.hash_b64,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> DeveloperCredential:
        validate_state_version(data.get("v"))
        key_id = str(data.get("key_id", ""))
        status = str(data.get("status", ""))
        verifier = str(data.get("verifier", ""))
        salt = str(data.get("salt", ""))
        digest = str(data.get("hash", ""))
        # Validate shape without exposing anything secret.
        from ghostlink.developer.keys import is_valid_key_id

        if not is_valid_key_id(key_id) or status not in ("active", "revoked"):
            raise ValueError("credential metadata malformed")
        return cls(
            key_id=key_id,
            status=status,
            created_at=_parse(str(data.get("created_at") or "")) or _now(),
            last_used_at=_parse(str(data.get("last_used_at") or "")),
            verifier=verifier,
            salt_b64=salt,
            hash_b64=digest,
        )


@dataclass(slots=True)
class DeveloperAccount:
    """The local developer account (public identity metadata only)."""

    developer_id: str
    created_at: datetime
    status: str = "active"
    credentials: dict[str, DeveloperCredential] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "v": DEVELOPER_ACCOUNT_SCHEMA_VERSION,
            "developer_id": self.developer_id,
            "created_at": _fmt(self.created_at),
            "status": self.status,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> DeveloperAccount:
        validate_state_version(data.get("v"))
        developer_id = str(data.get("developer_id", ""))
        status = str(data.get("status", "active"))
        if not developer_id or status not in ("active", "disabled"):
            raise ValueError("account metadata malformed")
        return cls(
            developer_id=developer_id,
            created_at=_parse(str(data.get("created_at") or "")) or _now(),
            status=status,
        )


__all__ = [
    "DEVELOPER_ACCOUNT_SCHEMA_VERSION",
    "DEVELOPER_CREDENTIAL_SCHEMA_VERSION",
    "DeveloperAccount",
    "DeveloperCredential",
]
