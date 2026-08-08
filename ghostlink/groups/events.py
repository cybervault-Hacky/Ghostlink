"""Group membership events and their signatures (Phase 6B).

Per docs/GROUPS.md §9.5 every roster-mutating event carries an Ed25519
signature over a canonical form:

    ghostlink/group-event/v1 | group_id | epoch | kind | subject_fp | wall_ts

* join / removed / dissolved events are signed by the **owner** key;
* left events are signed by the **leaving member's** key.

The authority builds the canonical bytes, the signer signs those exact
bytes, and members verify signature *and* recompute the canonical form
from the advertised fields — a mismatch fails closed.

This module also defines the proof-of-possession and attestation forms:

    ghostlink/group-create/v1 | name | attest_nonce
    ghostlink/group-attest/v1 | group_id | fingerprint | attest_nonce

Only Ed25519 (already used for Phase 5 identity) is used — no new
cryptographic primitive and no new library.
"""

from __future__ import annotations

import base64
import binascii
from datetime import UTC, datetime
from enum import Enum

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ghostlink.exceptions.groups import GroupValidationError
from ghostlink.groups.ids import is_valid_group_id
from ghostlink.identity.fingerprint import (
    identity_fingerprint,
    is_valid_fingerprint,
)

_CREATE_PREFIX = "ghostlink/group-create/v1"
_ATTEST_PREFIX = "ghostlink/group-attest/v1"
_EVENT_PREFIX = "ghostlink/group-event/v1"


class GroupEventKind(str, Enum):
    """Roster-mutating event kinds (docs/GROUPS.md §16)."""

    JOIN = "join"
    LEFT = "left"
    REMOVED = "removed"
    DISSOLVED = "dissolved"


EVENT_KINDS: frozenset[str] = frozenset(kind.value for kind in GroupEventKind)


def canonical_create_form(name: str, attest_nonce: str) -> bytes:
    """Bytes an owner signs to prove key possession at group creation."""

    return f"{_CREATE_PREFIX}|{name}|{attest_nonce}".encode()


def canonical_attest_form(group_id: str, fingerprint: str, attest_nonce: str) -> bytes:
    """Bytes a member signs to bind their identity key to a relay session."""

    return f"{_ATTEST_PREFIX}|{group_id}|{fingerprint}|{attest_nonce}".encode()


def canonical_event_form(
    group_id: str,
    epoch: int,
    kind: str,
    subject_fingerprint: str,
    wall_ts: datetime,
) -> bytes:
    """Bytes a signer signs for one roster mutation (exact, unambiguous)."""

    moment = wall_ts if wall_ts.tzinfo is not None else wall_ts.replace(tzinfo=UTC)
    stamp = moment.astimezone(UTC).isoformat()
    return canonical_event_string(group_id, epoch, kind, subject_fingerprint, stamp).encode("utf-8")


def canonical_event_string(
    group_id: str, epoch: int, kind: str, subject_fingerprint: str, stamp: str
) -> str:
    """The canonical event string; the wire format carries it verbatim."""

    return f"{_EVENT_PREFIX}|{group_id}|{epoch}|{kind}|{subject_fingerprint}|{stamp}"


def verify_signature(public_key: bytes, signature: bytes, message: bytes) -> bool:
    """True iff ``signature`` is a valid Ed25519 signature of ``message``."""

    if len(public_key) != 32 or not signature:
        return False
    try:
        Ed25519PublicKey.from_public_bytes(public_key).verify(signature, message)
    except (InvalidSignature, ValueError):
        return False
    return True


def fingerprint_for_key_hex(public_key_hex: str) -> str:
    """GLFP-… for a hex public key, rejecting malformed input."""

    try:
        public_key = bytes.fromhex(public_key_hex)
    except ValueError as exc:
        raise GroupValidationError(
            "A member public key is not valid hex.",
            hint="Group keys are transmitted as 64 lowercase hex characters.",
        ) from exc
    try:
        return identity_fingerprint(public_key)
    except ValueError as exc:
        raise GroupValidationError(
            "A member public key must decode to exactly 32 bytes.",
            hint="Group member keys are Ed25519 public keys.",
        ) from exc


def require_valid_fingerprint(candidate: str, *, field: str = "fingerprint") -> str:
    """Validate and return a normalized GLFP-… fingerprint."""

    cleaned = candidate.strip().upper()
    if not is_valid_fingerprint(cleaned):
        raise GroupValidationError(
            f"The {field} '{candidate}' is malformed.",
            hint="Fingerprints look like GLFP-XXXX-XXXX-XXXX (hex groups).",
        )
    return cleaned


def require_valid_group_id(candidate: str, *, field: str = "group id") -> str:
    """Validate and return a group id, raising a typed error if malformed."""

    from ghostlink.groups.ids import normalize_group_id  # local alias for clarity

    cleaned = normalize_group_id(candidate)
    if not is_valid_group_id(cleaned):
        raise GroupValidationError(
            f"The {field} '{candidate}' is malformed.",
            hint="Group ids look like gl-group-XXXX-XXXX-XXXX.",
        )
    return cleaned


class GroupEvent:
    """One committed, signed roster mutation (metadata only)."""

    __slots__ = (
        "epoch",
        "group_id",
        "kind",
        "signature",
        "signer_fingerprint",
        "subject_fingerprint",
        "wall_ts",
    )

    def __init__(
        self,
        *,
        group_id: str,
        epoch: int,
        kind: GroupEventKind,
        subject_fingerprint: str,
        wall_ts: datetime,
        signer_fingerprint: str,
        signature: bytes,
    ) -> None:
        if not is_valid_group_id(group_id):
            raise GroupValidationError(
                f"Event group id '{group_id}' is malformed.",
                hint="Group ids look like gl-group-XXXX-XXXX-XXXX.",
            )
        if epoch < 1:
            raise GroupValidationError(
                f"Event epoch must be ≥ 1, got {epoch}.",
                hint="Epochs grow monotonically from 1 and never roll back.",
            )
        require_valid_fingerprint(subject_fingerprint, field="subject fingerprint")
        require_valid_fingerprint(signer_fingerprint, field="signer fingerprint")
        moment = wall_ts if wall_ts.tzinfo is not None else wall_ts.replace(tzinfo=UTC)
        self.group_id = group_id
        self.epoch = epoch
        self.kind = kind
        self.subject_fingerprint = subject_fingerprint.strip().upper()
        self.wall_ts = moment
        self.signer_fingerprint = signer_fingerprint.strip().upper()
        self.signature = bytes(signature)

    def canonical(self) -> bytes:
        """The exact signed bytes for this event."""

        return canonical_event_form(
            self.group_id, self.epoch, self.kind.value, self.subject_fingerprint, self.wall_ts
        )

    def canonical_string(self) -> str:
        """The canonical string carried on the wire inside event packets."""

        moment = self.wall_ts.astimezone(UTC)
        return canonical_event_string(
            self.group_id,
            self.epoch,
            self.kind.value,
            self.subject_fingerprint,
            moment.isoformat(),
        )

    # ----------------------------------------------------------- serialization

    def to_dict(self) -> dict[str, object]:
        return {
            "group_id": self.group_id,
            "epoch": self.epoch,
            "kind": self.kind.value,
            "subject": self.subject_fingerprint,
            "wall_ts": self.wall_ts.astimezone(UTC).isoformat(),
            "signer": self.signer_fingerprint,
            "signature": base64.b64encode(self.signature).decode("ascii"),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> GroupEvent:
        try:
            kind = GroupEventKind(str(data["kind"]))
            signature = base64.b64decode(str(data["signature"]), validate=True)
            epoch = int(str(data["epoch"]))
            wall = datetime.fromisoformat(str(data["wall_ts"]))
        except (KeyError, ValueError, binascii.Error) as exc:
            raise GroupValidationError(
                "A stored group event is malformed.",
                hint="The groups store is corrupt; remove the group and re-sync.",
            ) from exc
        return cls(
            group_id=str(data["group_id"]),
            epoch=epoch,
            kind=kind,
            subject_fingerprint=str(data["subject"]),
            wall_ts=wall,
            signer_fingerprint=str(data["signer"]),
            signature=signature,
        )
