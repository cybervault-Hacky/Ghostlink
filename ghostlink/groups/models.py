"""Group membership models (Phase 6B) — the local, client-side record.

The local record mirrors the relay-authoritative roster: it is metadata
only (fingerprints, public keys, handles, epochs — never tokens, private
keys, or session keys). All mutations flow through verified signed events
(see ``ghostlink.groups.events``) applied via :meth:`LocalGroupRecord.
apply_verified_event`, which enforces:

* strict epoch monotonicity (+1 per committed mutation — no rollback, no
  gaps, no duplicates, no client-chosen epochs);
* signer authorization (owner signs join/removed/dissolved; the leaving
  member signs left);
* guarded lifecycle state transitions (fail-safe, illegal moves raise).

Display names are presentation data and are never an authorization input
(docs/GROUPS.md §8.1).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from ghostlink.constants.net import (
    DEFAULT_CRYPTO_SUITE,
    GROUP_CRYPTO_SUITES,
    GROUP_DISPLAY_NAME_MAX_LEN,
    GROUP_EVENTS_KEPT,
)
from ghostlink.exceptions.config import ConfigValidationError
from ghostlink.exceptions.groups import (
    GroupEpochError,
    GroupStateError,
    GroupValidationError,
)
from ghostlink.groups.events import GroupEvent, GroupEventKind, require_valid_fingerprint
from ghostlink.groups.ids import is_valid_group_id

MAX_GROUP_EVENTS_STORED: int = GROUP_EVENTS_KEPT


class GroupRole(str, Enum):
    """Roster roles; authorization gates on role, never on names."""

    OWNER = "owner"
    MEMBER = "member"


class LocalGroupState(str, Enum):
    """Client-side lifecycle states; all but ACTIVE are terminal archives."""

    ACTIVE = "active"
    LEFT = "left"
    REMOVED = "removed"
    DISSOLVED = "dissolved"
    DEFUNCT = "defunct"  # relay lost the record (e.g. relay restart)


TERMINAL_GROUP_STATES: frozenset[LocalGroupState] = frozenset(
    {
        LocalGroupState.LEFT,
        LocalGroupState.REMOVED,
        LocalGroupState.DISSOLVED,
        LocalGroupState.DEFUNCT,
    }
)


def validate_group_name(name: str) -> str:
    """A group name must be 1..48 printable, non-control characters."""

    from ghostlink.constants.net import GROUP_NAME_MAX_LEN

    cleaned = name.strip()
    if not (0 < len(cleaned) <= GROUP_NAME_MAX_LEN):
        raise GroupValidationError(
            f"A group name must be 1..{GROUP_NAME_MAX_LEN} characters, got {len(cleaned)}.",
            hint="Pick a short printable name, e.g. --name NightWatch.",
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        raise GroupValidationError(
            "A group name must not contain control characters.",
            hint="Use plain printable text only.",
        )
    return cleaned


def validate_group_display_name(name: str, *, field: str = "display name") -> str:
    """As with one-to-one chat: 1..24 printable characters, no controls."""

    cleaned = name.strip()
    if not (0 < len(cleaned) <= GROUP_DISPLAY_NAME_MAX_LEN):
        raise GroupValidationError(
            f"A {field} must be 1..{GROUP_DISPLAY_NAME_MAX_LEN} characters.",
            hint="Pick a short printable name.",
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        raise GroupValidationError(
            f"A {field} must not contain control characters.",
            hint="Use plain printable text only.",
        )
    return cleaned


class GroupMember:
    """One roster entry — authenticated identity data, never a secret."""

    __slots__ = (
        "display_name",
        "fingerprint",
        "handle",
        "joined_epoch",
        "public_key_hex",
        "role",
    )

    def __init__(
        self,
        *,
        fingerprint: str,
        handle: str,
        display_name: str,
        public_key_hex: str,
        role: GroupRole,
        joined_epoch: int,
    ) -> None:
        self.fingerprint = require_valid_fingerprint(fingerprint)
        if role is GroupRole.OWNER and joined_epoch != 1:
            raise GroupValidationError(
                "The owner must join at epoch 1.",
                hint="The group creator is the first member of the first epoch.",
            )
        if joined_epoch < 1:
            raise GroupValidationError(
                "joined_epoch must be ≥ 1.",
                hint="Epochs grow monotonically from 1.",
            )
        self.handle = handle
        self.display_name = validate_group_display_name(display_name or handle)
        self.public_key_hex = public_key_hex.lower()
        self.role = role
        self.joined_epoch = joined_epoch

    def is_owner(self) -> bool:
        return self.role is GroupRole.OWNER

    def to_dict(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "handle": self.handle,
            "display_name": self.display_name,
            "public_key_hex": self.public_key_hex,
            "role": self.role.value,
            "joined_epoch": self.joined_epoch,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> GroupMember:
        try:
            return cls(
                fingerprint=str(data["fingerprint"]),
                handle=str(data["handle"]),
                display_name=str(data.get("display_name") or data["handle"]),
                public_key_hex=str(data["public_key_hex"]),
                role=GroupRole(str(data["role"])),
                joined_epoch=int(str(data["joined_epoch"])),
            )
        except (KeyError, ValueError) as exc:
            raise ConfigValidationError(
                "A stored group member is malformed.",
                hint="The groups store is corrupt; remove the group and re-join.",
            ) from exc

    @classmethod
    def from_event_payload(cls, payload: dict[str, object]) -> GroupMember:
        """Build a member from the descriptor attached to a join event."""

        return cls.from_dict(payload)


class LocalGroupRecord:
    """The locally persisted view of one group (guarded, epoch-monotonic)."""

    __slots__ = (
        "created_at",
        "crypto_suite",
        "epoch",
        "epoch_leap_at",
        "events",
        "group_id",
        "members",
        "my_fingerprint",
        "name",
        "owner_fingerprint",
        "owner_public_key_hex",
        "relay_url",
        "state",
        "suspect",
        "updated_at",
    )

    def __init__(
        self,
        *,
        group_id: str,
        name: str,
        owner_fingerprint: str,
        owner_public_key_hex: str,
        my_fingerprint: str,
        epoch: int,
        members: dict[str, GroupMember],
        state: LocalGroupState,
        relay_url: str,
        created_at: datetime,
        updated_at: datetime,
        epoch_leap_at: datetime | None = None,
        events: list[GroupEvent] | None = None,
        suspect: bool = False,
        crypto_suite: str = DEFAULT_CRYPTO_SUITE,
    ) -> None:
        if not is_valid_group_id(group_id):
            raise GroupValidationError(
                f"Group id '{group_id}' is malformed.",
                hint="Group ids look like gl-group-XXXX-XXXX-XXXX.",
            )
        if epoch < 1:
            raise GroupValidationError(
                f"Group epoch must be ≥ 1, got {epoch}.",
                hint="Epochs start at 1 at creation.",
            )
        if crypto_suite not in GROUP_CRYPTO_SUITES:
            raise GroupValidationError(
                f"Unknown crypto suite '{crypto_suite}'.",
                hint="Choose one of: " + ", ".join(sorted(GROUP_CRYPTO_SUITES)) + ".",
            )
        self.group_id = group_id
        self.name = validate_group_name(name)
        self.owner_fingerprint = require_valid_fingerprint(owner_fingerprint)
        self.owner_public_key_hex = owner_public_key_hex.lower()
        self.my_fingerprint = require_valid_fingerprint(my_fingerprint, field="own fingerprint")
        self.epoch = epoch
        self.members = members
        self.state = state
        self.relay_url = relay_url
        self.created_at = self._aware(created_at, "created_at")
        self.updated_at = self._aware(updated_at, "updated_at")
        self.epoch_leap_at = (
            self._aware(epoch_leap_at, "epoch_leap_at") if epoch_leap_at is not None else None
        )
        self.events: list[GroupEvent] = list(events) if events else []
        self.suspect = suspect
        self.crypto_suite = crypto_suite

    @staticmethod
    def _aware(moment: datetime, field: str) -> datetime:
        if moment.tzinfo is None:
            raise ConfigValidationError(
                f"Group timestamp '{field}' must be timezone-aware.",
                hint="GhostLink stores group times in UTC.",
            )
        return moment

    # ------------------------------------------------------------ derived

    @property
    def my_role(self) -> GroupRole:
        member = self.members.get(self.my_fingerprint)
        if member is not None:
            return member.role
        return (
            GroupRole.OWNER if self.my_fingerprint == self.owner_fingerprint else (GroupRole.MEMBER)
        )

    def is_owner(self) -> bool:
        return (
            self.my_fingerprint == self.owner_fingerprint and self.state is LocalGroupState.ACTIVE
        )

    def member_count(self) -> int:
        return len(self.members)

    def member(self, fingerprint: str) -> GroupMember | None:
        return self.members.get(fingerprint.strip().upper())

    def require_active(self) -> None:
        if self.state is not LocalGroupState.ACTIVE:
            raise GroupStateError(
                f"Group {self.group_id} is {self.state.value}; it is no longer usable.",
                hint="Archived groups are read-only — create a new group to continue.",
            )

    def require_unsuspecting(self) -> None:
        """Owner/member mutations are refused while roster state is suspect."""

        if self.suspect:
            raise GroupStateError(
                f"Group {self.group_id} has unverified roster state.",
                hint="Re-sync with the relay before further membership operations.",
            )

    def require_terminal_for_archive(self) -> None:
        """Only terminal records may be discarded from the local store."""

        if self.state not in TERMINAL_GROUP_STATES:
            raise GroupStateError(
                f"Group {self.group_id} is still active.",
                hint="Leave or dissolve the group before discarding its local record.",
            )

    # ------------------------------------------------------- event application

    def apply_verified_event(
        self,
        event: GroupEvent,
        *,
        join_member: GroupMember | None = None,
        now: datetime | None = None,
    ) -> str:
        """Apply one *signature-verified* event; returns a display notice.

        Verification happens in the lifecycle manager before this call;
        here we enforce epochs, authorization shape, and transitions.
        """

        self.require_active()
        if event.group_id != self.group_id:
            raise GroupStateError(
                f"Event for {event.group_id} does not belong to this record.",
                hint="This is a bug — events are routed by group id.",
            )
        if event.epoch <= self.epoch:
            raise GroupEpochError(
                f"Stale epoch {event.epoch} (current {self.epoch}); refusing to roll back.",
                hint="Duplicate or replayed events are dropped, never re-applied.",
            )
        if event.epoch > self.epoch + 1:
            self.suspect = True
            raise GroupEpochError(
                f"Epoch gap {self.epoch} → {event.epoch}; roster state is unverifiable.",
                hint="Re-sync with the relay (group info) before trusting this roster.",
            )

        moment = now if now is not None else datetime.now(UTC)
        notice = self._apply_event_semantics(event, join_member)
        self.epoch = event.epoch
        self.epoch_leap_at = moment
        self.updated_at = moment
        self.events.append(event)
        del self.events[:-MAX_GROUP_EVENTS_STORED]
        return notice

    def _apply_event_semantics(self, event: GroupEvent, join_member: GroupMember | None) -> str:
        subject = event.subject_fingerprint
        if event.kind is GroupEventKind.JOIN:
            self._require_owner_signed(event, "admissions")
            if subject in self.members:
                raise GroupStateError(
                    f"{subject} is already on the roster.",
                    hint="Duplicate admissions are rejected by the state machine.",
                )
            if join_member is None or join_member.fingerprint != subject:
                raise GroupStateError(
                    "A join event lacks its member descriptor.",
                    hint="This is a relay protocol violation — treat the roster as suspect.",
                )
            self.members[subject] = join_member
            return f"{join_member.display_name} joined the group"
        if event.kind in (GroupEventKind.LEFT, GroupEventKind.REMOVED):
            member = self.members.get(subject)
            if member is None:
                raise GroupStateError(
                    f"{subject} is not on the roster.",
                    hint="The roster is inconsistent — re-sync with the relay.",
                )
            if event.kind is GroupEventKind.LEFT and event.signer_fingerprint != subject:
                raise GroupStateError(
                    "A leave event must be signed by the leaving member.",
                    hint="This is a relay protocol violation — treat the roster as suspect.",
                )
            if event.kind is GroupEventKind.REMOVED:
                self._require_owner_signed(event, "removals")
                if subject == self.owner_fingerprint:
                    raise GroupStateError(
                        "The owner cannot be removed from their own group.",
                        hint="Owner departure dissolves the group instead.",
                    )
            del self.members[subject]
            if subject == self.my_fingerprint:
                self.state = (
                    LocalGroupState.LEFT
                    if event.kind is GroupEventKind.LEFT
                    else LocalGroupState.REMOVED
                )
            verb = "left" if event.kind is GroupEventKind.LEFT else "was removed from"
            return f"{member.display_name} {verb} the group"
        if event.kind is GroupEventKind.DISSOLVED:
            self._require_owner_signed(event, "dissolution")
            if subject != self.owner_fingerprint:
                raise GroupStateError(
                    "Dissolution must reference the owner.",
                    hint="This is a relay protocol violation — treat the roster as suspect.",
                )
            self.state = LocalGroupState.DISSOLVED
            return "The owner dissolved the group"
        raise GroupStateError(  # pragma: no cover - enum exhaustiveness guard
            f"Unknown group event kind '{event.kind}'.",
            hint="This is a relay protocol violation — treat the roster as suspect.",
        )

    def _require_owner_signed(self, event: GroupEvent, operation: str) -> None:
        if event.signer_fingerprint != self.owner_fingerprint:
            raise GroupStateError(
                f"Only the owner may authorize {operation}.",
                hint="This event was not signed by the pinned owner key.",
            )

    def mark_suspect(self) -> None:
        self.suspect = True
        self.updated_at = datetime.now(UTC)

    def mark_defunct(self) -> None:
        """The relay no longer hosts this group (restart/unknown record)."""

        self.require_active()
        self.state = LocalGroupState.DEFUNCT
        self.updated_at = datetime.now(UTC)

    # ----------------------------------------------------------- serialization

    def to_dict(self) -> dict[str, object]:
        """Persisted form — metadata only; no secrets are ever stored."""

        return {
            "v": 1,
            "group_id": self.group_id,
            "name": self.name,
            "owner_fingerprint": self.owner_fingerprint,
            "owner_public_key_hex": self.owner_public_key_hex,
            "my_fingerprint": self.my_fingerprint,
            "epoch": self.epoch,
            "state": self.state.value,
            "suspect": self.suspect,
            "crypto_suite": self.crypto_suite,
            "relay_url": self.relay_url,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "epoch_leap_at": self.epoch_leap_at.isoformat() if self.epoch_leap_at else None,
            "members": {fp: member.to_dict() for fp, member in self.members.items()},
            "events": [event.to_dict() for event in self.events],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> LocalGroupRecord:
        expected = {
            "group_id",
            "name",
            "owner_fingerprint",
            "my_fingerprint",
            "epoch",
            "state",
            "created_at",
            "updated_at",
            "members",
        }
        missing = expected - set(data)
        if missing:
            raise ConfigValidationError(
                f"A stored group record is missing field(s): {', '.join(sorted(missing))}.",
                hint="The groups store is corrupt; remove the group and re-join.",
            )
        try:
            state = LocalGroupState(str(data["state"]))
        except ValueError as exc:
            raise ConfigValidationError(
                "A stored group holds an unknown lifecycle state.",
                hint="The groups store is corrupt; remove the group and re-join.",
            ) from exc
        raw_members = data.get("members")
        if not isinstance(raw_members, dict):
            raise ConfigValidationError(
                "A stored group roster is malformed.",
                hint="The groups store is corrupt; remove the group and re-join.",
            )
        members = {
            str(fp): GroupMember.from_dict(member)
            for fp, member in raw_members.items()
            if isinstance(member, dict)
        }
        raw_events = data.get("events", [])
        if not isinstance(raw_events, list):
            raise ConfigValidationError(
                "A stored group event history is malformed.",
                hint="The groups store is corrupt; remove the group and re-join.",
            )
        events = [GroupEvent.from_dict(event) for event in raw_events if isinstance(event, dict)]
        created_raw = datetime.fromisoformat(str(data["created_at"]))
        updated_raw = datetime.fromisoformat(str(data["updated_at"]))
        leap_raw = data.get("epoch_leap_at")
        return cls(
            group_id=str(data["group_id"]),
            name=str(data["name"]),
            owner_fingerprint=str(data["owner_fingerprint"]),
            owner_public_key_hex=str(data.get("owner_public_key_hex", "")),
            my_fingerprint=str(data["my_fingerprint"]),
            epoch=int(str(data["epoch"])),
            members=members,
            state=state,
            relay_url=str(data.get("relay_url", "")),
            crypto_suite=str(data.get("crypto_suite", DEFAULT_CRYPTO_SUITE)),
            created_at=created_raw,
            updated_at=updated_raw,
            epoch_leap_at=datetime.fromisoformat(str(leap_raw)) if leap_raw else None,
            events=events,
            suspect=bool(data.get("suspect", False)),
        )
