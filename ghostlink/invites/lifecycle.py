"""Client-side invite manager (Phase 5).

Owns the full lifecycle from the creator's chair and the joiner's:

* mint tokens + derive the public ``gi_…`` id,
* register with the relay authority (which enforces expiry and one-time
  semantics), registering a fresh private room unless --room pinned one,
* persist metadata-only records through the local registry,
* revoke via the relay, fold expiry in, apply the retention policy,
* bind redeemed invites to the resulting conversation.

Tokens live in an in-memory table only — they are written to the terminal
once, as the shareable link, and never to disk or logs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from ghostlink.constants.net import (
    INVITE_DEFAULT_MAX_REDEMPTIONS,
    INVITE_DEFAULT_TTL_SECONDS,
    INVITE_PROTOCOL_VERSION,
)
from ghostlink.core.logging import get_logger, register_secret
from ghostlink.exceptions.invites import InviteStateError, InviteUnknownError
from ghostlink.invites.authority import InviteGrant
from ghostlink.invites.expiration import expiry_from_now
from ghostlink.invites.models import InviteRecord, InviteState
from ghostlink.invites.registry import LocalInviteRegistry
from ghostlink.invites.tokens import (
    format_invite_link,
    generate_invite_token,
    invite_id_for_token,
    token_hash_for,
)
from ghostlink.models.room import generate_room_id

if TYPE_CHECKING:
    from ghostlink.transport.relay.client import RelayClient

_logger = get_logger("invites.lifecycle")


class SecureInviteManager:
    """Creates, tracks and retires the user's own invites."""

    def __init__(self, registry: LocalInviteRegistry) -> None:
        self._registry = registry
        self._tokens: dict[str, str] = {}  # invite_id → in-memory token

    # ------------------------------------------------------------------ create

    def mint(
        self,
        *,
        ttl_seconds: float = INVITE_DEFAULT_TTL_SECONDS,
        max_redemptions: int = INVITE_DEFAULT_MAX_REDEMPTIONS,
        room_id: str | None = None,
        relay_url: str = "",
        kind: str = "chat",
        group_id: str = "",
    ) -> tuple[InviteRecord, str]:
        """Mint a fresh (CREATED) invite; returns the record and the link."""

        token = generate_invite_token()
        invite_id = invite_id_for_token(token)
        if kind == "group":
            bound_room = ""
        else:
            bound_room = room_id if room_id is not None else generate_room_id()
        record = InviteRecord(
            invite_id=invite_id,
            room_id=bound_room,
            token_hash=token_hash_for(token),
            created_at=datetime.now(UTC),
            expires_at=expiry_from_now(ttl_seconds),
            max_redemptions=max_redemptions,
            relay_url=relay_url,
            protocol_version=INVITE_PROTOCOL_VERSION,
            kind=kind,
            group_id=group_id,
        )
        self._tokens[invite_id] = token
        link = format_invite_link(token)
        register_secret(token)  # Phase 8: scrub this token from any log output
        register_secret(link)
        target = group_id if kind == "group" else bound_room
        _logger.info(
            "invite minted — %s for %s (kind=%s ttl=%ds uses=%d)",
            invite_id,
            target,
            kind,
            int(ttl_seconds),
            max_redemptions,
        )
        return record, link

    def token_for(self, invite_id: str) -> str | None:
        """The in-memory token for ``invite_id`` (memory only, never disk)."""

        return self._tokens.get(invite_id)

    # ------------------------------------------------------------- relay side

    async def register_with_relay(self, client: RelayClient, record: InviteRecord) -> InviteGrant:
        """Register the invite with the relay authority; record → ACTIVE."""

        token = self._tokens.get(record.invite_id)
        if token is None:
            raise InviteStateError(
                f"No token held for invite {record.invite_id}.",
                hint="Tokens stay in memory — recreated invites need new mints.",
            )
        grant = await client.create_invite(
            token,
            room_id=record.room_id,
            ttl_seconds=max(1.0, (record.expires_at - record.created_at).total_seconds()),
            max_redemptions=record.max_redemptions,
            kind=record.kind,
            group_id=record.group_id,
        )
        record.transition(InviteState.ACTIVE)
        record.expires_at = grant.expires_at
        self._registry.save(record)
        _logger.info("invite registered with the relay — %s", record.invite_id)
        return grant

    async def revoke_via_relay(self, client: RelayClient, invite_id: str) -> InviteRecord:
        """Revoke one of our invites at the relay authority and locally."""

        record = self.require(invite_id)
        token = self._tokens.get(record.invite_id)
        if token is not None:
            await client.revoke_invite(token)
        if record.state is not InviteState.REVOKED:
            # EXPIRED→REVOKED is a legal cleanup move; REDEEMED stays put.
            if record.state is InviteState.REDEEMED:
                pass
            else:
                record.transition(InviteState.REVOKED)
        self._registry.save(record)
        self._tokens.pop(record.invite_id, None)
        _logger.info("invite revoked — %s", record.invite_id)
        return record

    def mark_redeemed(self, invite_id: str, *, session_binding: str) -> InviteRecord:
        """Record local redemption + session binding (joiner's side)."""

        record = self.require(invite_id)
        if record.state is InviteState.ACTIVE:
            record.transition(InviteState.REDEEMING)
        record.transition(InviteState.REDEEMED)
        record.session_binding = session_binding
        self._registry.save(record)
        _logger.info("invite redeemed — %s bound to session %s", record.invite_id, session_binding)
        return record

    def record_incoming_redemption(
        self,
        invite_id: str,
        *,
        room_id: str,
        expires_at: datetime,
        relay_url: str = "",
    ) -> InviteRecord:
        """Joiner's side: persist a REDEEMED ledger entry for a consumed invite."""

        record = InviteRecord(
            invite_id=invite_id,
            room_id=room_id,
            token_hash="",
            created_at=datetime.now(UTC),
            expires_at=expires_at,
            max_redemptions=1,
            redemptions=1,
            state=InviteState.REDEEMED,
            relay_url=relay_url,
            protocol_version=INVITE_PROTOCOL_VERSION,
        )
        self._registry.save(record)
        _logger.info("incoming redemption recorded — %s for %s", invite_id, room_id)
        return record

    def bind_session(self, invite_id: str, session_binding: str) -> None:
        """Attach the resulting conversation id to a local invite record."""

        record = self.get(invite_id)
        if record is None:
            return
        record.session_binding = session_binding
        self._registry.save(record)

    def note_local_redeemed(self, invite_id: str, *, session_binding: str) -> None:
        """Creator's side: redeeming peer consumed the invite (state update)."""

        record = self.get(invite_id)
        if record is None:
            return
        if record.state is InviteState.ACTIVE:
            record.transition(InviteState.REDEEMING)
        record.transition(InviteState.REDEEMED)
        record.redemptions = record.max_redemptions
        record.session_binding = session_binding
        self._registry.save(record)

    # ------------------------------------------------------------------- read

    def get(self, invite_id: str) -> InviteRecord | None:
        record = self._registry.get(invite_id)
        if record is not None:
            self._fold_expiry(record)
        return record

    def require(self, invite_id: str) -> InviteRecord:
        record = self.get(invite_id)
        if record is None:
            raise InviteUnknownError(
                f"No invite found for '{invite_id}'.",
                hint="Run 'ghostlink invite list' to see your invites.",
            )
        return record

    def list(self) -> list[InviteRecord]:
        records = self._registry.list_all()
        for record in records:
            self._fold_expiry(record)
        return records

    def _fold_expiry(self, record: InviteRecord) -> None:
        if record.state is InviteState.ACTIVE and record.is_due():
            record.transition(InviteState.EXPIRED)
            self._registry.save(record)

    # -------------------------------------------------------------- retention

    def revoke_local(self, invite_id: str) -> InviteRecord:
        """Revoke offline (no relay reachable): local terminal state only.

        Authoritative revocation still requires the relay; this path is for
        invites whose token we no longer hold (nothing redeemable remains)
        and for bookkeeping consistency.
        """

        record = self.require(invite_id)
        if record.state is InviteState.REVOKED:
            return record
        if record.state is InviteState.REDEEMED:
            raise InviteStateError(
                f"Invite {invite_id} was already redeemed — revocation is moot.",
                hint="A consumed invite cannot be used again anyway.",
            )
        record.transition(InviteState.REVOKED)
        self._registry.save(record)
        _logger.info("invite revoked locally — %s", invite_id)
        return record

    def apply_retention(self, retention: timedelta) -> int:
        """Purge terminal records older than the retention window.

        Overdue ACTIVE records are folded to EXPIRED first — expiry is
        terminal, so expired records fall under the same retention rule
        as redeemed and revoked ones.
        """

        self._registry.refresh_expired()
        return self._registry.purge_terminal(retention)
