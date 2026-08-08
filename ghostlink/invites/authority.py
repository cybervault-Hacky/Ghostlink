"""The relay-side invite authority (Phase 5).

This registry is the single source of truth for whether an invite may be
redeemed. It enforces, authoritatively:

* **expiration** — deadlines combine a monotonic clock (enforcement, immune
  to system-clock jumps) with an aware-UTC wall timestamp (display);
* **single redemption** — check-and-consume happens inside one synchronous
  call with no awaits in between, so two simultaneous attempts on one event
  loop cannot both win (atomic state transition);
* **revocation** — only the creating relay session may revoke;
* **session binding** — a successful redemption records the redeemer's
  relay session id, and an already-redeemed invite can never be consumed by
  any other (or the same) session again.

Failure is closed: unknown, expired, revoked, exhausted, or
not-yours-to-touch invites all answer with a typed refusal.

The secret token is held only long enough to hash it (SHA-256) — the
registry stores and compares digests, and every log line uses the public
``gi_…`` identifier.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import Enum

from ghostlink.constants.net import (
    INVITE_MAX_REDEMPTIONS,
    INVITE_MAX_TTL_SECONDS,
    INVITE_MIN_TTL_SECONDS,
)
from ghostlink.groups.ids import is_valid_group_id
from ghostlink.invites.tokens import invite_id_for_token, is_valid_invite_token
from ghostlink.models.room import is_valid_room_id

MAX_CREATOR_SESSION_LENGTH: int = 64
DEFAULT_RETENTION_HOURS: int = 24


class RedemptionVerdict(str, Enum):
    """Typed outcome of one redemption attempt."""

    REDEEMED = "redeemed"
    UNKNOWN = "unknown"
    EXPIRED = "expired"
    REVOKED = "revoked"
    ALREADY_USED = "already-used"


@dataclass(frozen=True, slots=True)
class InviteGrant:
    """Result of registering an invite with the authority."""

    invite_id: str
    expires_at: datetime
    max_redemptions: int


@dataclass(frozen=True, slots=True)
class RedemptionResult:
    """Outcome of a redemption attempt (success carries the room binding)."""

    verdict: RedemptionVerdict
    invite_id: str
    room_id: str | None = None
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class InviteStatus:
    """Safe invite metadata — never the token, never other clients' data."""

    invite_id: str
    state: str
    created_at: datetime
    expires_at: datetime
    max_redemptions: int
    redemptions: int
    remaining_seconds: float
    bound_session: str | None
    room_id: str | None = None  # creator-only view
    kind: str = "chat"  # "chat" | "group" (group kind is creator-only detail)
    group_id: str | None = None  # creator-only view, group-kind invites only


@dataclass(frozen=True, slots=True)
class InviteLookup:
    """Non-consuming introspection result: what kind of invite is this?"""

    invite_id: str
    kind: str
    group_id: str
    redeemed: bool


@dataclass(slots=True)
class _Entry:
    invite_id: str
    token_hash: str
    room_id: str
    created_at: datetime
    expires_at: datetime
    expires_monotonic: float
    max_redemptions: int
    redemptions: int
    creator_session: str
    kind: str = "chat"
    group_id: str = ""
    bound_session: str | None = None
    revoked: bool = False
    last_redeemed_at_mono: float | None = None


def _hash_token(normalized_token: str) -> str:
    return hashlib.sha256(normalized_token.encode("ascii")).hexdigest()


class InviteAuthority:
    """Authoritative, race-free invite registry for one relay process."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        retention_hours: float = DEFAULT_RETENTION_HOURS,
    ) -> None:
        self._monotonic = monotonic
        self._retention = timedelta(hours=retention_hours)
        self._entries: dict[str, _Entry] = {}  # token_hash → invite

    # -------------------------------------------------------------- properties

    @property
    def size(self) -> int:
        return len(self._entries)

    @property
    def retention(self) -> timedelta:
        return self._retention

    # ------------------------------------------------------------- validation

    def _validate_creation(
        self, token: str, room_id: str, ttl: float, uses: int, kind: str, group_id: str
    ) -> str:
        normalized = token.strip().upper()
        if not is_valid_invite_token(normalized):
            raise ValueError("invite token is malformed")
        if kind == "group":
            if not is_valid_group_id(group_id):
                raise ValueError("group id is malformed")
        elif not is_valid_room_id(room_id):
            raise ValueError("room id is malformed")
        if not (INVITE_MIN_TTL_SECONDS <= float(ttl) <= INVITE_MAX_TTL_SECONDS):
            raise ValueError("ttl out of range")
        if not isinstance(uses, int) or not 1 <= uses <= INVITE_MAX_REDEMPTIONS:
            raise ValueError("redemption limit out of range")
        return normalized

    # ---------------------------------------------------------------- register

    def register(
        self,
        token: str,
        *,
        room_id: str,
        ttl_seconds: float,
        max_redemptions: int,
        creator_session: str,
        kind: str = "chat",
        group_id: str = "",
    ) -> InviteGrant:
        """Register one invite. Re-registering the same token fails closed."""

        normalized = self._validate_creation(
            token, room_id, ttl_seconds, max_redemptions, kind, group_id
        )
        if len(creator_session) > MAX_CREATOR_SESSION_LENGTH:
            raise ValueError("creator session id too long")
        token_hash = _hash_token(normalized)
        if token_hash in self._entries:
            raise ValueError("invite token already registered")
        now = datetime.now(UTC)
        entry = _Entry(
            invite_id=invite_id_for_token(normalized),
            token_hash=token_hash,
            room_id=room_id,
            created_at=now,
            expires_at=now + timedelta(seconds=ttl_seconds),
            expires_monotonic=self._monotonic() + ttl_seconds,
            max_redemptions=max_redemptions,
            redemptions=0,
            creator_session=creator_session,
            kind=kind,
            group_id=group_id if kind == "group" else "",
        )
        self._entries[token_hash] = entry
        return InviteGrant(
            invite_id=entry.invite_id,
            expires_at=entry.expires_at,
            max_redemptions=max_redemptions,
        )

    # ------------------------------------------------------------------- core

    def _lookup(self, token: str) -> _Entry | None:
        normalized = token.strip().upper()
        if not is_valid_invite_token(normalized):
            return None
        candidate_hash = _hash_token(normalized)
        for stored_hash, entry in self._entries.items():
            if hmac.compare_digest(stored_hash, candidate_hash):
                return entry
        return None

    def _expired(self, entry: _Entry) -> bool:
        return self._monotonic() >= entry.expires_monotonic

    def lookup(self, token: str) -> InviteLookup | None:
        """Non-consuming introspection: kind/binding of one invite.

        Used by the relay to branch group-kind redemption *before* the
        atomic consume — the group capacity check and the consumption must
        happen in one await-free section (docs/GROUPS.md §12.3).
        """

        entry = self._lookup(token)
        if entry is None:
            return None
        return InviteLookup(
            invite_id=entry.invite_id,
            kind=entry.kind,
            group_id=entry.group_id,
            redeemed=entry.redemptions >= entry.max_redemptions,
        )

    def count_active_group_invites(self, group_id: str) -> int:
        """Active (unexpired, unrevoked, not exhausted) invites for one group."""

        count = 0
        for entry in self._entries.values():
            if (
                entry.kind == "group"
                and entry.group_id == group_id
                and not entry.revoked
                and not self._expired(entry)
                and entry.redemptions < entry.max_redemptions
            ):
                count += 1
        return count

    def redeem(self, token: str, *, redeem_session: str) -> RedemptionResult:
        """Atomically consume one invite redemption — fail-closed on any miss.

        The check-and-consume happens with no awaits in between: on a single
        event loop, two simultaneous REDEEM packets can never both win.
        """

        entry = self._lookup(token)
        if entry is None:
            token_hash = _hash_token(token.strip().upper())
            return RedemptionResult(RedemptionVerdict.UNKNOWN, invite_id=f"gi_{token_hash[:10]}")
        invite_id = entry.invite_id
        if entry.revoked:
            return RedemptionResult(RedemptionVerdict.REVOKED, invite_id)
        if self._expired(entry):
            return RedemptionResult(
                RedemptionVerdict.EXPIRED, invite_id, expires_at=entry.expires_at
            )
        if entry.redemptions >= entry.max_redemptions:
            return RedemptionResult(RedemptionVerdict.ALREADY_USED, invite_id)
        # Atomic consumption: count, then bind to this relay session.
        entry.redemptions += 1
        entry.bound_session = redeem_session
        entry.last_redeemed_at_mono = self._monotonic()
        return RedemptionResult(
            RedemptionVerdict.REDEEMED,
            invite_id,
            room_id=entry.room_id,
            expires_at=entry.expires_at,
        )

    def revoke(self, token: str, *, requester_session: str) -> InviteStatus | None:
        """Revoke an invite; only the creating relay session may do so."""

        entry = self._lookup(token)
        if entry is None:
            return None
        if entry.creator_session != requester_session:
            raise PermissionError("only the inviting session may revoke")
        entry.revoked = True
        return self._status_of(entry, creator_view=True)

    # ------------------------------------------------------------------ query

    def status(self, token: str, *, requester_session: str) -> InviteStatus | None:
        """Safe metadata; the room binding is visible to the creator only."""

        entry = self._lookup(token)
        if entry is None:
            return None
        return self._status_of(entry, creator_view=entry.creator_session == requester_session)

    def _status_of(self, entry: _Entry, *, creator_view: bool) -> InviteStatus:
        remaining = max(0.0, entry.expires_monotonic - self._monotonic())
        if entry.revoked:
            state = "revoked"
        elif entry.redemptions >= entry.max_redemptions:
            state = "redeemed"
        elif remaining <= 0.0:
            state = "expired"
        else:
            state = "active"
        return InviteStatus(
            invite_id=entry.invite_id,
            state=state,
            created_at=entry.created_at,
            expires_at=entry.expires_at,
            max_redemptions=entry.max_redemptions,
            redemptions=entry.redemptions,
            remaining_seconds=remaining,
            bound_session=entry.bound_session,
            room_id=entry.room_id if creator_view else None,
            kind=entry.kind if creator_view else "chat",
            group_id=(entry.group_id or None) if creator_view else None,
        )

    # ------------------------------------------------------------------ sweep

    def sweep(self) -> int:
        """Maintenance pass: purge terminal entries past retention."""

        return self.purge_terminal()

    def purge_terminal(self) -> int:
        """Remove revoked/fully-redeemed entries older than the retention."""

        now_mono = self._monotonic()
        cutoff = now_mono - self._retention.total_seconds()
        gone = 0
        for token_hash, entry in list(self._entries.items()):
            terminal = entry.revoked or entry.redemptions >= entry.max_redemptions
            expired = now_mono >= entry.expires_monotonic
            reference = entry.last_redeemed_at_mono or entry.expires_monotonic
            if (terminal or expired) and reference < cutoff:
                del self._entries[token_hash]
                gone += 1
        return gone
