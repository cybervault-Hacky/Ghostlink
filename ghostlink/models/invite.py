"""Invite foundation model (Phase 2).

Invites grant entry to a room. They support one-time use, custom lifetimes,
explicit revocation, and strict format validation. Distribution UI is a later
phase — this model captures the semantics only.
"""

from __future__ import annotations

import re
import secrets
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from enum import Enum

from ghostlink.constants.net import INVITE_TOKEN_PREFIX
from ghostlink.exceptions.config import ConfigValidationError

INVITE_TOKEN_PATTERN = re.compile(rf"^{INVITE_TOKEN_PREFIX}[A-Za-z0-9_-]{{32}}$")


class InviteState(str, Enum):
    """Persisted lifecycle states of an invite."""

    ACTIVE = "active"
    REDEEMED = "redeemed"
    REVOKED = "revoked"


class InviteEffectiveState(str, Enum):
    """Derived state returned by :meth:`Invite.effective_state`."""

    ACTIVE = "active"
    EXPIRED = "expired"
    REDEEMED = "redeemed"
    REVOKED = "revoked"


def generate_invite_token() -> str:
    """Create an invite token: ``gli_`` + 32 URL-safe characters."""

    return f"{INVITE_TOKEN_PREFIX}{secrets.token_urlsafe(24)}"


def is_valid_invite_token(candidate: str) -> bool:
    """Strict format validation for invite tokens."""

    return bool(INVITE_TOKEN_PATTERN.fullmatch(candidate.strip()))


@dataclass(frozen=True, slots=True)
class Invite:
    """An immutable invite record bound to a room."""

    token: str
    room_id: str
    created_at: datetime
    expires_at: datetime
    one_time: bool
    state: InviteState
    uses: int = 0

    @classmethod
    def create(
        cls,
        *,
        room_id: str,
        lifetime_minutes: int,
        one_time: bool = True,
        now: datetime | None = None,
    ) -> Invite:
        """Create a fresh ACTIVE invite. ``lifetime_minutes`` must be ≥ 1."""

        if lifetime_minutes < 1:
            raise ConfigValidationError(
                f"Invite lifetime must be at least 1 minute, got {lifetime_minutes}.",
                hint="Invites always expire; choose a longer lifetime.",
            )
        moment = now if now is not None else datetime.now(UTC)
        return cls(
            token=generate_invite_token(),
            room_id=room_id,
            created_at=moment,
            expires_at=moment + timedelta(minutes=lifetime_minutes),
            one_time=one_time,
            state=InviteState.ACTIVE,
        )

    # ------------------------------------------------------------------ state

    def is_expired(self, now: datetime | None = None) -> bool:
        moment = now if now is not None else datetime.now(UTC)
        return moment >= self.expires_at

    def effective_state(self, now: datetime | None = None) -> InviteEffectiveState:
        """Fold persisted state and expiry into a single derived state."""

        if self.state is InviteState.REVOKED:
            return InviteEffectiveState.REVOKED
        if self.state is InviteState.REDEEMED:
            return InviteEffectiveState.REDEEMED
        if self.is_expired(now):
            return InviteEffectiveState.EXPIRED
        return InviteEffectiveState.ACTIVE

    def is_redeemable(self, now: datetime | None = None) -> bool:
        return self.effective_state(now) is InviteEffectiveState.ACTIVE

    def redeem(self) -> Invite:
        """Consume the invite once. One-time invites become REDEEMED."""

        effective = self.effective_state()
        if effective is not InviteEffectiveState.ACTIVE:
            raise ConfigValidationError(
                f"Invite {self.token[:12]}… is not redeemable (state: {effective.value}).",
                hint="Ask the host to issue a fresh invite.",
            )
        uses = self.uses + 1
        new_state = InviteState.REDEEMED if self.one_time else InviteState.ACTIVE
        return replace(self, uses=uses, state=new_state)

    def revoke(self) -> Invite:
        """Return a copy of this invite permanently revoked."""

        return replace(self, state=InviteState.REVOKED)

    # ----------------------------------------------------------- serialization

    def to_dict(self) -> dict[str, object]:
        return {
            "v": 1,
            "token": self.token,
            "room_id": self.room_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "one_time": self.one_time,
            "state": self.state.value,
            "uses": self.uses,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> Invite:
        """Rebuild an invite from storage, rejecting malformed documents."""

        expected = {"token", "room_id", "created_at", "expires_at", "one_time", "state"}
        missing = expected - set(data)
        if missing:
            raise ConfigValidationError(
                f"Stored invite document is missing field(s): {', '.join(sorted(missing))}.",
                hint="The invites store is corrupt; remove state/invites.json to reset.",
            )
        token = str(data["token"])
        if not is_valid_invite_token(token):
            raise ConfigValidationError(
                "A stored invite token is malformed.",
                hint="The invites store is corrupt; remove state/invites.json to reset.",
            )
        uses_raw = data.get("uses", 0)
        uses = uses_raw if isinstance(uses_raw, int) and not isinstance(uses_raw, bool) else 0
        return cls(
            token=token,
            room_id=str(data["room_id"]),
            created_at=datetime.fromisoformat(str(data["created_at"])),
            expires_at=datetime.fromisoformat(str(data["expires_at"])),
            one_time=bool(data["one_time"]),
            state=InviteState(str(data["state"])),
            uses=uses,
        )
