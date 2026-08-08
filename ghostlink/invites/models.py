"""Invite records and the invite state machine (Phase 5).

Lifecycle:

    CREATED → ACTIVE → REDEEMING → REDEEMED
                  ↘         ↘
                 EXPIRED    EXPIRED / REVOKED

* CREATED    — minted locally, not yet registered with the relay authority
* ACTIVE     — registered; redeemable until expiry/revocation
* REDEEMING  — a redemption request is in flight at the relay
* REDEEMED   — consumed; bound to the resulting session (terminal)
* EXPIRED    — past its deadline (terminal)
* REVOKED    — cancelled by its creator (terminal)

The record persisted to disk is metadata-only: **the secret token never
leaves memory / the shared link**. Lookups happen through the token's
public hash identifier (``gi_…``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum

from ghostlink.exceptions.config import ConfigValidationError
from ghostlink.exceptions.invites import InviteStateError
from ghostlink.groups.ids import is_valid_group_id
from ghostlink.invites.tokens import is_valid_invite_id
from ghostlink.models.room import is_valid_room_id

MAX_SESSION_BINDING_LENGTH: int = 32


class InviteState(str, Enum):
    """Persisted lifecycle states of a one-time invite."""

    CREATED = "created"
    ACTIVE = "active"
    REDEEMING = "redeeming"
    REDEEMED = "redeemed"
    EXPIRED = "expired"
    REVOKED = "revoked"


TERMINAL_STATES: frozenset[InviteState] = frozenset(
    {InviteState.REDEEMED, InviteState.EXPIRED, InviteState.REVOKED}
)

TRANSITIONS: dict[InviteState, frozenset[InviteState]] = {
    InviteState.CREATED: frozenset({InviteState.ACTIVE, InviteState.EXPIRED, InviteState.REVOKED}),
    InviteState.ACTIVE: frozenset(
        {InviteState.REDEEMING, InviteState.EXPIRED, InviteState.REVOKED}
    ),
    InviteState.REDEEMING: frozenset(
        {InviteState.REDEEMED, InviteState.ACTIVE, InviteState.EXPIRED, InviteState.REVOKED}
    ),
    InviteState.REDEEMED: frozenset(),
    # Revoking an already-expired invite is a supported cleanup gesture;
    # a REDEEMED invite is consumed forever and must never move again.
    InviteState.EXPIRED: frozenset({InviteState.REVOKED}),
    InviteState.REVOKED: frozenset(),
}


def can_transition(current: InviteState, target: InviteState) -> bool:
    """True when ``current → target`` is a legal lifecycle move."""

    return target in TRANSITIONS.get(current, frozenset())


def _expect_aware(moment: datetime, *, field: str) -> datetime:
    if moment.tzinfo is None:
        raise ConfigValidationError(
            f"Invite timestamp '{field}' must be timezone-aware.",
            hint="GhostLink stores invite times in UTC.",
        )
    return moment


class InviteRecord:
    """A mutable invite record with a guarded state machine.

    Metadata only — never the secret token. ``token_hash`` lets the local
    registry and the relay address the invite without ever storing it.
    """

    __slots__ = (
        "created_at",
        "expires_at",
        "group_id",
        "invite_id",
        "kind",
        "max_redemptions",
        "protocol_version",
        "redemptions",
        "relay_url",
        "room_id",
        "session_binding",
        "state",
        "token_hash",
    )

    def __init__(
        self,
        *,
        invite_id: str,
        room_id: str,
        token_hash: str,
        created_at: datetime,
        expires_at: datetime,
        max_redemptions: int,
        redemptions: int = 0,
        state: InviteState = InviteState.CREATED,
        session_binding: str = "",
        relay_url: str = "",
        protocol_version: int = 1,
        kind: str = "chat",
        group_id: str = "",
    ) -> None:
        if not is_valid_invite_id(invite_id):
            raise ConfigValidationError(
                f"Invite id '{invite_id}' is malformed.",
                hint="Invite ids look like gi_<10 lowercase hex>.",
            )
        if kind == "group":
            if not is_valid_group_id(group_id):
                raise ConfigValidationError(
                    f"Invite group '{group_id}' is not a valid group id.",
                    hint="Groups use the gl-group-XXXX-XXXX-XXXX format.",
                )
            room_id = ""
        elif not is_valid_room_id(room_id):
            raise ConfigValidationError(
                f"Invite room '{room_id}' is not a valid room id.",
                hint="Rooms use the gl-room-XXXX-XXXX-XXXX format.",
            )
        self.invite_id = invite_id
        self.room_id = room_id
        self.token_hash = token_hash
        self.created_at = _expect_aware(created_at, field="created_at")
        self.expires_at = _expect_aware(expires_at, field="expires_at")
        if expires_at <= created_at:
            raise ConfigValidationError(
                "An invite's deadline must be after its creation time.",
                hint="Create invites with a positive lifetime.",
            )
        if not isinstance(max_redemptions, int) or max_redemptions < 1:
            raise ConfigValidationError(
                "Redemption limit must be an integer ≥ 1.",
                hint="One-time invites use a limit of 1.",
            )
        if not isinstance(redemptions, int) or redemptions < 0 or redemptions > max_redemptions:
            raise ConfigValidationError(
                "Redemption count must fit within the invite's limit.",
                hint="This invite record is inconsistent — remove it and recreate.",
            )
        if session_binding and (
            len(session_binding) > MAX_SESSION_BINDING_LENGTH
            or any(ord(char) < 33 for char in session_binding)
        ):
            raise ConfigValidationError(
                f"Session binding must be ≤ {MAX_SESSION_BINDING_LENGTH} visible characters.",
                hint="GhostLink binds invites to conversation or session identifiers only.",
            )
        self.max_redemptions = max_redemptions
        self.redemptions = redemptions
        self.state = state
        self.session_binding = session_binding
        self.relay_url = relay_url
        self.protocol_version = protocol_version
        self.kind = kind
        self.group_id = group_id if kind == "group" else ""

    # ------------------------------------------------------------------ state

    def transition(self, target: InviteState) -> None:
        """Move to ``target``; illegal moves fail safely with a clear error."""

        if not can_transition(self.state, target):
            raise InviteStateError(
                f"Invite {self.invite_id} cannot move {self.state.value} → {target.value}.",
                hint="This invite has already been consumed, expired, or revoked."
                if self.state in TERMINAL_STATES
                else "Check the invite lifecycle order.",
            )
        self.state = target

    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def effective_state(self, now: datetime | None = None) -> InviteState:
        """Fold the wall-clock deadline into the current state (view only)."""

        if self.state is InviteState.ACTIVE and self.is_due(now):
            return InviteState.EXPIRED
        return self.state

    def is_due(self, now: datetime | None = None) -> bool:
        """True once the wall-clock deadline passed (expiry check)."""

        moment = now if now is not None else datetime.now(UTC)
        return moment >= self.expires_at

    def remaining_seconds(self, now: datetime | None = None) -> float:
        """Seconds until the deadline (0 once elapsed) — display helper."""

        moment = now if now is not None else datetime.now(UTC)
        return max(0.0, (self.expires_at - moment).total_seconds())

    def remaining_uses(self) -> int:
        return max(0, self.max_redemptions - self.redemptions)

    def copy(self) -> InviteRecord:
        return self._clone()

    def _clone(self) -> InviteRecord:
        return InviteRecord(
            invite_id=self.invite_id,
            room_id=self.room_id,
            token_hash=self.token_hash,
            created_at=self.created_at,
            expires_at=self.expires_at,
            max_redemptions=self.max_redemptions,
            redemptions=self.redemptions,
            state=self.state,
            session_binding=self.session_binding,
            relay_url=self.relay_url,
            protocol_version=self.protocol_version,
            kind=self.kind,
            group_id=self.group_id,
        )

    # ----------------------------------------------------------- serialisation

    def to_dict(self) -> dict[str, object]:
        """Persisted form — metadata only, never the secret token."""

        return {
            "v": 1,
            "invite_id": self.invite_id,
            "room_id": self.room_id,
            "token_hash": self.token_hash,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "max_redemptions": self.max_redemptions,
            "redemptions": self.redemptions,
            "state": self.state.value,
            "session_binding": self.session_binding,
            "relay_url": self.relay_url,
            "protocol_version": self.protocol_version,
            "kind": self.kind,
            "group_id": self.group_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> InviteRecord:
        """Rebuild a record from storage, rejecting malformed documents."""

        expected = {
            "invite_id",
            "room_id",
            "token_hash",
            "created_at",
            "expires_at",
            "max_redemptions",
        }
        missing = expected - set(data)
        if missing:
            raise ConfigValidationError(
                f"A stored invite record is missing field(s): {', '.join(sorted(missing))}.",
                hint="The invites store is corrupt; clear the secure_invites store to reset.",
            )
        try:
            state = InviteState(str(data.get("state", "created")))
        except ValueError as exc:
            raise ConfigValidationError(
                "A stored invite holds an unknown lifecycle state.",
                hint="The invites store is corrupt; clear the secure_invites store to reset.",
            ) from exc
        created_raw = datetime.fromisoformat(str(data["created_at"]))
        expires_raw = datetime.fromisoformat(str(data["expires_at"]))
        if created_raw.tzinfo is None:
            created_raw = created_raw.replace(tzinfo=UTC)
        if expires_raw.tzinfo is None:
            expires_raw = expires_raw.replace(tzinfo=UTC)
        try:
            max_redemptions = int(str(data["max_redemptions"]))
            redemptions = int(str(data.get("redemptions", 0)))
            protocol_version = int(str(data.get("protocol_version", 1)))
        except ValueError as exc:
            raise ConfigValidationError(
                "A stored invite holds non-integer counters.",
                hint="The invites store is corrupt; clear the secure_invites store to reset.",
            ) from exc
        kind = str(data.get("kind", "chat"))
        if kind not in ("chat", "group"):
            raise ConfigValidationError(
                "A stored invite holds an unknown kind.",
                hint="The invites store is corrupt; clear the secure_invites store to reset.",
            )
        return cls(
            invite_id=str(data["invite_id"]),
            room_id=str(data["room_id"]),
            token_hash=str(data["token_hash"]),
            created_at=created_raw,
            expires_at=expires_raw,
            max_redemptions=max_redemptions,
            redemptions=redemptions,
            state=state,
            session_binding=str(data.get("session_binding", "")),
            relay_url=str(data.get("relay_url", "")),
            protocol_version=protocol_version,
            kind=kind,
            group_id=str(data.get("group_id", "")),
        )


def is_terminal_state(state: InviteState) -> bool:
    return state in TERMINAL_STATES
