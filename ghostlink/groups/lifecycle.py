"""Client-side group lifecycle manager (Phase 6B).

Owns the local side of every group operation, always delegating authority
to the relay (which is the race-free source of truth for rosters, epochs,
and authorization — docs/GROUPS.md §8):

* **create** — proof-of-possession over the relay-issued attestation
  challenge; the owner record is persisted only after the relay grants;
* **invite** — owner mints group-kind invites; capacity and ownership are
  enforced locally *and* re-checked authoritatively at the relay;
* **join** — redeem (atomic capacity reservation), pin the roster
  snapshot per §12.2 (public-key ↔ fingerprint bindings), attest, then
  await the owner-signed admission event; the local record is persisted
  **only on success**, so a failed or timed-out join leaves no partial
  state;
* **leave / remove / dissolve** — send the operation over an attested
  session and let the committed, signature-verified event (delivered via
  the registered listener) mutate the stored record;
* **sync** — re-attest and reconcile with the authoritative snapshot:
  epoch rollback marks the record suspect, never silently regresses;
* **signing** — the owner (or a leaving member) signs exactly the
  canonical event bytes the relay presents, and only when the local
  record agrees the signer is authorized for that op at the next epoch.

No tokens, private keys, or session keys ever appear here: exceptions and
logs carry public identifiers only.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from collections.abc import Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ghostlink.constants.net import (
    DEFAULT_CRYPTO_SUITE,
    GROUP_CRYPTO_SUITES,
    GROUP_JOIN_PENDING_SECONDS,
    MAX_GROUP_MEMBERS,
)
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.groups import (
    GroupDefunctError,
    GroupEpochError,
    GroupError,
    GroupFullError,
    GroupNotMemberError,
    GroupPermissionError,
    GroupStateError,
    GroupUnknownError,
    GroupValidationError,
)
from ghostlink.groups.events import (
    GroupEvent,
    GroupEventKind,
    canonical_attest_form,
    canonical_create_form,
    fingerprint_for_key_hex,
    require_valid_fingerprint,
    verify_signature,
)
from ghostlink.groups.models import (
    MAX_GROUP_EVENTS_STORED,
    GroupMember,
    GroupRole,
    LocalGroupRecord,
    LocalGroupState,
    validate_group_name,
)
from ghostlink.groups.registry import LocalGroupRegistry
from ghostlink.identity.fingerprint import identity_id_for
from ghostlink.identity.lifecycle import IdentityManager, fingerprint_for_hex
from ghostlink.invites.tokens import parse_invite_link

if TYPE_CHECKING:
    from ghostlink.identity.identity import LocalIdentity
    from ghostlink.invites.lifecycle import SecureInviteManager
    from ghostlink.invites.models import InviteRecord
    from ghostlink.transport.relay.client import GroupAttested, GroupRedemption, RelayClient
    from ghostlink.transport.relay.protocol import Packet

_logger = get_logger("groups.lifecycle")

_EVENT_PREFIX = "ghostlink/group-event/v1"
_OWNER_SIGNED_KINDS = frozenset(
    {GroupEventKind.JOIN.value, GroupEventKind.REMOVED.value, GroupEventKind.DISSOLVED.value}
)


class LocalGroupManager:
    """The client-side engine for the Phase 6B group lifecycle."""

    def __init__(self, registry: LocalGroupRegistry, identities: IdentityManager) -> None:
        self._registry = registry
        self._identities = identities
        self._attached_client: RelayClient | None = None
        self._apply_errors: dict[str, GroupError] = {}
        self._notice_listeners: list[Callable[[str, str], None]] = []
        self._state_listeners: list[Callable[[LocalGroupRecord, GroupEvent], None]] = []

    # ---------------------------------------------------------------- wiring

    def attach(self, client: RelayClient) -> None:
        """Route this client's group events/sign requests through the manager.

        Idempotent and cheap; every mutating operation re-asserts it so the
        record can never miss a committed event or a signature request.
        """

        if self._attached_client is client:
            return
        client.set_group_event_listener(self._on_group_event)
        client.set_group_signer(self._sign_for_relay)
        self._attached_client = client

    def add_notice_listener(self, listener: Callable[[str, str], None]) -> None:
        """Register ``listener(group_id, notice)`` for committed events."""

        self._notice_listeners.append(listener)

    def _emit_notice(self, group_id: str, notice: str) -> None:
        for listener in self._notice_listeners:
            try:
                listener(group_id, notice)
            except Exception:  # pragma: no cover - listener isolation
                _logger.debug("group notice listener failed", exc_info=True)

    def add_state_listener(self, listener: Callable[[LocalGroupRecord, GroupEvent], None]) -> None:
        """Register ``listener(record, event)`` for committed events (6C).

        Fires after a verified event has been applied and persisted — the
        messaging service uses it to leap mesh epochs and tear down links
        to departed members (docs/GROUPS.md §16.5, §23).
        """

        self._state_listeners.append(listener)

    def _emit_state(self, record: LocalGroupRecord, event: GroupEvent) -> None:
        for listener in self._state_listeners:
            try:
                listener(record, event)
            except Exception:  # pragma: no cover - listener isolation
                _logger.debug("group state listener failed", exc_info=True)

    # ---------------------------------------------------------------- reads

    def get(self, group_id: str) -> LocalGroupRecord | None:
        return self._registry.get(group_id)

    def require(self, group_id: str) -> LocalGroupRecord:
        return self._registry.require(group_id)

    def list_all(self) -> list[LocalGroupRecord]:
        return self._registry.list_all()

    def archive_locally(self, group_id: str) -> None:
        """Drop the local record (terminal or unwanted groups only, offline)."""

        record = self._registry.require(group_id)
        record.require_terminal_for_archive()
        self._registry.delete(group_id)
        _logger.info("group %s local record archived away", group_id)

    def mark_defunct(self, group_id: str) -> LocalGroupRecord:
        """The relay lost this group (restart/unknown): archive as defunct."""

        record = self._registry.require(group_id)
        record.mark_defunct()
        self._registry.save(record)
        _logger.info("group %s marked defunct (relay no longer hosts it)", group_id)
        return record

    # ---------------------------------------------------------------- create

    async def create_group(
        self,
        client: RelayClient,
        name: str,
        *,
        display_name: str = "",
        crypto_suite: str = DEFAULT_CRYPTO_SUITE,
    ) -> LocalGroupRecord:
        """Create a group on the relay; the caller becomes owner at epoch 1.

        ``crypto_suite`` selects the message-encryption path — ``mesh-v1``
        (Phase 6C pairwise fanout) or ``senderkey-v1`` (Phase 7 O(1)
        sender-key hardening). It is relay-authoritative and advertised to
        every member on join/sync.
        """

        self.attach(client)
        clean_name = validate_group_name(name)
        if crypto_suite not in GROUP_CRYPTO_SUITES:
            raise GroupValidationError(
                f"Unknown crypto suite '{crypto_suite}'.",
                hint="Choose one of: " + ", ".join(sorted(GROUP_CRYPTO_SUITES)) + ".",
            )
        identity = self._identities.ensure()
        nonce = self._require_nonce(client)
        pop = base64.b64encode(identity.sign(canonical_create_form(clean_name, nonce))).decode(
            "ascii"
        )
        shown = display_name or identity.nickname or identity.identity_id
        group_id, epoch = await client.group_create(
            clean_name,
            public_key_hex=identity.public_key_hex,
            pop_signature_b64=pop,
            handle=identity.identity_id,
            display_name=shown,
            crypto_suite=crypto_suite,
        )
        my_fingerprint = self._identity_fingerprint(identity)
        owner = GroupMember(
            fingerprint=my_fingerprint,
            handle=identity.identity_id,
            display_name=shown,
            public_key_hex=identity.public_key_hex,
            role=GroupRole.OWNER,
            joined_epoch=1,
        )
        now = datetime.now(UTC)
        record = LocalGroupRecord(
            group_id=group_id,
            name=clean_name,
            owner_fingerprint=my_fingerprint,
            owner_public_key_hex=identity.public_key_hex,
            my_fingerprint=my_fingerprint,
            epoch=epoch,
            members={my_fingerprint: owner},
            state=LocalGroupState.ACTIVE,
            relay_url=client.endpoint.display,
            created_at=now,
            updated_at=now,
            crypto_suite=crypto_suite,
        )
        self._registry.assert_metadata_only(record)
        self._registry.save(record)
        _logger.info("group created — %s (%d member)", group_id, record.member_count())
        return record

    # ---------------------------------------------------------------- invite

    async def mint_group_invite(
        self,
        client: RelayClient,
        invites: SecureInviteManager,
        group_id: str,
        *,
        ttl_seconds: float,
        max_redemptions: int,
    ) -> tuple[InviteRecord, str]:
        """Owner-only: mint + relay-register a group invite; returns the link."""

        self.attach(client)
        record = self._registry.require(group_id)
        record.require_active()
        record.require_unsuspecting()
        identity = self._identities.ensure()
        if self._identity_fingerprint(identity) != record.owner_fingerprint:
            raise GroupPermissionError(
                "Only the owner may mint invites for this group.",
                hint="Ask the group owner to create the invitation.",
            )
        headroom = MAX_GROUP_MEMBERS - record.member_count()
        if max_redemptions > headroom:
            raise GroupFullError(
                f"This group can admit at most {headroom} more member(s).",
                hint="Groups hold at most 8 members — lower --uses.",
            )
        invite, link = invites.mint(
            ttl_seconds=ttl_seconds,
            max_redemptions=max_redemptions,
            relay_url=record.relay_url,
            kind="group",
            group_id=group_id,
        )
        # The relay re-checks ownership *and* capacity authoritatively.
        await invites.register_with_relay(client, invite)
        _logger.info("group %s — invite %s minted", group_id, invite.invite_id)
        return invite, link

    # ------------------------------------------------------------------ join

    async def join_group(
        self,
        client: RelayClient,
        link: str,
        *,
        display_name: str = "",
        timeout_seconds: float = GROUP_JOIN_PENDING_SECONDS,
    ) -> LocalGroupRecord:
        """Redeem a group invite and await the owner-signed admission.

        The record is built in memory from the pinned redemption snapshot
        and persisted atomically — only once the admission event arrives
        and verifies. A refused or timed-out join leaves nothing behind.
        """

        self.attach(client)
        token = parse_invite_link(link)
        identity = self._identities.ensure()
        my_fingerprint = self._identity_fingerprint(identity)
        redemption = await client.redeem_group_invite(token)
        snapshot = self._record_from_redemption(redemption, identity, client)
        waiter = asyncio.ensure_future(
            client.wait_group_event(
                redemption.group_id,
                GroupEventKind.JOIN.value,
                my_fingerprint,
                timeout_seconds=timeout_seconds,
            )
        )
        try:
            attested = await self._attest(
                client, redemption.group_id, identity, display_name=display_name
            )
        except BaseException:
            waiter.cancel()
            raise
        if attested.role in ("owner", "member"):
            # Already a member (e.g. re-join after restart): adopt the
            # authoritative snapshot instead of awaiting a new admission.
            waiter.cancel()
            record = self._record_from_attested(snapshot, attested, my_fingerprint)
            self._registry.assert_metadata_only(record)
            self._registry.save(record)
            return record
        packet = await waiter  # raises typed timeout/conflict on failure
        event = self._event_from_packet(packet)
        self._verify_event_signature(snapshot, event)
        # Other admissions may have committed between our redemption-pinned
        # snapshot and ours; the pinned snapshot alone can be behind the
        # event. Re-attest as the now-active member and adopt the
        # authoritative roster (the same verified path as re-sync).
        re_attested = await self._attest(
            client, redemption.group_id, identity, display_name=display_name
        )
        if re_attested.role not in ("owner", "member"):
            raise GroupStateError(
                "The admission event arrived but the relay still lists us as a candidate.",
                hint="Treat this relay as untrustworthy — do not trust the roster.",
            )
        if re_attested.epoch < event.epoch:
            raise GroupEpochError(
                f"Relay epoch {re_attested.epoch} is behind the admission event {event.epoch}.",
                hint="Never regress an epoch — the join was refused.",
            )
        record = self._record_from_attested(snapshot, re_attested, my_fingerprint)
        self._registry.assert_metadata_only(record)
        self._registry.save(record)
        _logger.info("group joined — %s at epoch %d", record.group_id, record.epoch)
        return record

    # ------------------------------------------------------- leave/remove/etc.

    async def leave_group(self, client: RelayClient, group_id: str) -> LocalGroupRecord:
        """Leave a group (the committed left event archives the record)."""

        self.attach(client)
        record = self._require_usable_record(group_id)
        identity = self._identities.ensure()
        if record.is_owner() and self._identity_fingerprint(identity) == record.owner_fingerprint:
            raise GroupPermissionError(
                "The owner cannot leave their own group.",
                hint="Dissolve the group instead (ghostlink group dissolve).",
            )
        await self._attest(client, group_id, identity)
        self._apply_errors.pop(group_id, None)
        await client.group_leave(group_id, subject_fingerprint=record.my_fingerprint)
        record = self._after_committed(group_id)
        _logger.info("group %s — left at epoch %d", group_id, record.epoch)
        return record

    async def remove_member(
        self, client: RelayClient, group_id: str, subject_fingerprint: str
    ) -> LocalGroupRecord:
        """Owner-only: remove a member (committed event updates everyone)."""

        self.attach(client)
        record = self._require_usable_record(group_id)
        identity = self._identities.ensure()
        self._require_owner(record, identity)
        target = require_valid_fingerprint(subject_fingerprint, field="member fingerprint")
        if target == record.owner_fingerprint:
            raise GroupPermissionError(
                "The owner cannot remove themself.",
                hint="Owner departure dissolves the group instead.",
            )
        if record.member(target) is None:
            raise GroupNotMemberError(
                f"{target} is not a member of this group.",
                hint="Run ghostlink group info to see the current roster.",
            )
        await self._attest(client, group_id, identity)
        self._apply_errors.pop(group_id, None)
        await client.group_remove(group_id, target)
        record = self._after_committed(group_id)
        _logger.info("group %s — removed %s at epoch %d", group_id, target, record.epoch)
        return record

    async def dissolve_group(self, client: RelayClient, group_id: str) -> LocalGroupRecord:
        """Owner-only: dissolve the group for everyone (terminal)."""

        self.attach(client)
        record = self._require_usable_record(group_id)
        identity = self._identities.ensure()
        self._require_owner(record, identity)
        await self._attest(client, group_id, identity)
        self._apply_errors.pop(group_id, None)
        await client.group_dissolve(group_id, owner_fingerprint=record.owner_fingerprint)
        record = self._after_committed(group_id)
        _logger.info("group %s — dissolved at epoch %d", group_id, record.epoch)
        return record

    # ------------------------------------------------------------------ sync

    async def sync_group(
        self, client: RelayClient, group_id: str, *, display_name: str = ""
    ) -> LocalGroupRecord:
        """Re-attest and reconcile with the authoritative roster snapshot."""

        self.attach(client)
        record = self._registry.require(group_id)
        record.require_active()
        identity = self._identities.ensure()
        try:
            attested = await self._attest(client, group_id, identity, display_name=display_name)
        except GroupUnknownError:
            record.mark_defunct()
            self._registry.save(record)
            raise GroupDefunctError(
                f"The relay no longer hosts group {group_id}.",
                hint="Relays hold groups in memory; after a restart the group is gone.",
            ) from None
        if attested.epoch < record.epoch:
            record.mark_suspect()
            self._registry.save(record)
            raise GroupEpochError(
                f"Relay epoch {attested.epoch} is behind the local epoch {record.epoch}.",
                hint="Never regress an epoch — the record was marked suspect.",
            )
        try:
            merged = self._record_from_attested(record, attested, record.my_fingerprint)
        except GroupError:
            record.mark_suspect()
            self._registry.save(record)
            raise
        self._registry.assert_metadata_only(merged)
        self._registry.save(merged)
        _logger.info("group %s — synced at epoch %d", group_id, merged.epoch)
        return merged

    # ------------------------------------------------------------ event intake

    def _on_group_event(self, packet: Packet) -> None:
        """Committed-event listener: verify, apply, persist — or mark suspect."""

        payload = packet.payload
        group_id = str(payload.get("group", ""))
        record = self._registry.get(group_id)
        if record is None:
            return  # not our group (e.g. our own mid-join admission)
        try:
            event = self._event_from_packet(packet)
        except GroupError as exc:
            _logger.info("group %s — dropping unparseable event: %s", group_id, exc.message)
            self._flag_suspect(record, exc)
            return
        if event.epoch <= record.epoch:
            _logger.debug("group %s — replayed/stale event epoch %d dropped", group_id, event.epoch)
            return
        if record.state is not LocalGroupState.ACTIVE:
            return  # archived groups are immutable
        try:
            self._verify_event_signature(record, event)
            member = None
            if event.kind is GroupEventKind.JOIN:
                member = self._join_member_from_packet(record, packet, event)
            notice = record.apply_verified_event(event, join_member=member)
        except GroupError as exc:
            self._flag_suspect(record, exc)
            return
        self._apply_errors.pop(group_id, None)
        self._registry.save(record)
        _logger.info("group %s — %s (epoch %d)", group_id, notice, record.epoch)
        self._emit_notice(group_id, notice)
        self._emit_state(record, event)

    def _flag_suspect(self, record: LocalGroupRecord, error: GroupError) -> None:
        record.mark_suspect()
        self._registry.save(record)
        self._apply_errors[record.group_id] = error
        _logger.info("group %s marked suspect — %s", record.group_id, error.message)

    # --------------------------------------------------------------- signing

    def _sign_for_relay(self, group_id: str, op_id: str, message: bytes) -> str | None:
        """Sign one relay-presented canonical event — only when authorized.

        Signs iff the local record is ACTIVE and unsuspecting, the event is
        for exactly the next epoch, and the signer is: the leaving member
        for ``left``, or the pinned owner for join/removed/dissolved.
        Anything else is declined without revealing why on the wire.
        """

        record = self._registry.get(group_id)
        if record is None or record.state is not LocalGroupState.ACTIVE or record.suspect:
            _logger.info("sign request for unusable group %s — declined", group_id)
            return None
        try:
            text = message.decode("utf-8")
            parts = text.split("|")
            if len(parts) != 6 or parts[0] != _EVENT_PREFIX or parts[1] != group_id:
                raise GroupValidationError(
                    "The sign request is not a canonical group event.",
                    hint="Only ghostlink/group-event/v1 forms are signed.",
                )
            epoch = int(parts[2])
            kind, subject = parts[3], parts[4].strip().upper()
            require_valid_fingerprint(subject, field="subject fingerprint")
            if kind not in {event.value for event in GroupEventKind}:
                raise GroupValidationError(
                    f"Unknown group event kind '{kind}'.",
                    hint="Only join/left/removed/dissolved events exist.",
                )
        except (UnicodeDecodeError, ValueError, GroupValidationError) as exc:
            _logger.info("malformed sign request for %s — declined (%s)", group_id, exc)
            return None
        if epoch != record.epoch + 1:
            _logger.info(
                "sign request epoch %d ≠ next epoch %d for %s — declined",
                epoch,
                record.epoch + 1,
                group_id,
            )
            return None
        identity = self._identities.load()
        if identity is None or self._identity_fingerprint(identity) != record.my_fingerprint:
            _logger.info("sign request beyond local identity for %s — declined", group_id)
            return None
        authorized = (kind == GroupEventKind.LEFT.value and subject == record.my_fingerprint) or (
            kind in _OWNER_SIGNED_KINDS and record.is_owner()
        )
        if not authorized:
            _logger.info(
                "not authorized to sign op %s (%s/%s) for %s — declined",
                op_id,
                kind,
                subject,
                group_id,
            )
            return None
        _logger.info("signing op %s (%s %s) for %s", op_id, kind, subject, group_id)
        return base64.b64encode(identity.sign(message)).decode("ascii")

    # -------------------------------------------------------------- attesting

    async def _attest(
        self,
        client: RelayClient,
        group_id: str,
        identity: LocalIdentity,
        *,
        display_name: str = "",
    ) -> GroupAttested:
        nonce = self._require_nonce(client)
        fingerprint = self._identity_fingerprint(identity)
        signature = identity.sign(canonical_attest_form(group_id, fingerprint, nonce))
        shown = display_name or identity.nickname or identity.identity_id
        return await client.group_attest(
            group_id,
            public_key_hex=identity.public_key_hex,
            signature_b64=base64.b64encode(signature).decode("ascii"),
            handle=identity.identity_id,
            display_name=shown,
        )

    @staticmethod
    def _require_nonce(client: RelayClient) -> str:
        nonce = client.attest_nonce
        if not nonce:
            raise GroupError(
                "The relay did not issue an attestation challenge.",
                hint="Group lifecycle needs protocol v4 — check relay-status.",
            )
        return nonce

    # ------------------------------------------------------- snapshot rebuild

    def _record_from_redemption(
        self, redemption: GroupRedemption, identity: LocalIdentity, client: RelayClient
    ) -> LocalGroupRecord:
        """In-memory record from the §12.2 pinned redemption snapshot."""

        owner_fp = require_valid_fingerprint(
            redemption.owner_fingerprint, field="owner fingerprint"
        )
        if fingerprint_for_key_hex(redemption.owner_public_key_hex) != owner_fp:
            raise GroupStateError(
                "The relay's owner key does not match the owner fingerprint.",
                hint="Treat this relay as untrustworthy — do not join.",
            )
        members = self._verified_members(redemption.members, redemption.group_id)
        owner = members.get(owner_fp)
        if (
            owner is None
            or not owner.is_owner()
            or owner.public_key_hex != (redemption.owner_public_key_hex.lower())
        ):
            raise GroupStateError(
                "The roster snapshot has no consistent owner entry.",
                hint="Treat this relay as untrustworthy — do not join.",
            )
        now = datetime.now(UTC)
        my_fingerprint = self._identity_fingerprint(identity)
        return LocalGroupRecord(
            group_id=redemption.group_id,
            name=redemption.name,
            owner_fingerprint=owner_fp,
            owner_public_key_hex=redemption.owner_public_key_hex,
            my_fingerprint=my_fingerprint,
            epoch=redemption.epoch,
            members=members,
            state=LocalGroupState.ACTIVE,
            relay_url=client.endpoint.display,
            created_at=now,
            updated_at=now,
            crypto_suite=redemption.crypto_suite,
        )

    def _record_from_attested(
        self,
        base: LocalGroupRecord,
        attested: GroupAttested,
        my_fingerprint: str,
    ) -> LocalGroupRecord:
        """Rebuild a record from the authoritative attested snapshot.

        Pure builder: callers decide what an adoption failure means (a
        failed join saves nothing; a failed sync marks the stored record
        suspect).
        """

        if attested.epoch < base.epoch:
            raise GroupEpochError(
                f"Relay epoch {attested.epoch} is behind the pinned epoch {base.epoch}.",
                hint="Never regress an epoch — treat this relay as untrustworthy.",
            )
        if base.crypto_suite and attested.crypto_suite != base.crypto_suite:
            # No silent crypto downgrade (§37): a relay that advertises a
            # weaker suite for an established group is refused.
            raise GroupStateError(
                f"Relay crypto suite '{attested.crypto_suite}' differs from the "
                f"pinned '{base.crypto_suite}'.",
                hint="A crypto-suite change is an owner action, not a roster answer.",
            )
        members = self._verified_members(attested.members, base.group_id)
        owner = members.get(base.owner_fingerprint)
        if owner is None or not owner.is_owner():
            raise GroupStateError(
                "The relay roster has no consistent owner entry.",
                hint="Treat this relay as untrustworthy — the record stays unchanged.",
            )
        if my_fingerprint not in members:
            raise GroupNotMemberError(
                "The relay roster no longer contains your identity.",
                hint="You may have been removed while offline.",
            )
        now = datetime.now(UTC)
        merged = LocalGroupRecord(
            group_id=base.group_id,
            name=base.name,
            owner_fingerprint=base.owner_fingerprint,
            owner_public_key_hex=base.owner_public_key_hex,
            my_fingerprint=my_fingerprint,
            epoch=attested.epoch,
            members=members,
            state=LocalGroupState.ACTIVE,
            relay_url=base.relay_url,
            created_at=base.created_at,
            updated_at=now,
            epoch_leap_at=now if attested.epoch > base.epoch else base.epoch_leap_at,
            events=list(base.events),
            suspect=False,
            crypto_suite=base.crypto_suite,
        )
        known = {
            (event.epoch, event.kind.value, event.subject_fingerprint) for event in merged.events
        }
        for raw in attested.events:
            try:
                event = GroupEvent.from_dict(raw)
            except GroupValidationError:
                continue  # malformed history rows are dropped, not trusted
            key = (event.epoch, event.kind.value, event.subject_fingerprint)
            if event.epoch > base.epoch and key not in known:
                # Owner-signed tail rows verify against the pinned owner key;
                # LEFT rows verify only when the subject is still known locally.
                self._verify_tail_event(base, merged, event)
                merged.events.append(event)
                known.add(key)
        del merged.events[:-MAX_GROUP_EVENTS_STORED]
        return merged

    @staticmethod
    def _verify_tail_event(
        old: LocalGroupRecord, merged: LocalGroupRecord, event: GroupEvent
    ) -> None:
        """Best-effort signature check for an unseen roster-history row."""

        if event.kind is GroupEventKind.LEFT:
            member = old.member(event.subject_fingerprint) or merged.member(
                event.subject_fingerprint
            )
            if member is None:
                return  # departed member's key unknowable; relay is authority here
            key_hex = member.public_key_hex
            if event.signer_fingerprint != member.fingerprint:
                raise GroupPermissionError(
                    "A leave event was not signed by the leaving member.",
                    hint="This relay is misbehaving — the record stays unchanged.",
                )
        else:
            if event.signer_fingerprint != merged.owner_fingerprint:
                raise GroupPermissionError(
                    "A roster-history row was not signed by the pinned owner.",
                    hint="This relay is misbehaving — the record stays unchanged.",
                )
            key_hex = merged.owner_public_key_hex
        if not verify_signature(bytes.fromhex(key_hex), event.signature, event.canonical()):
            raise GroupPermissionError(
                "A roster-history signature does not verify.",
                hint="This relay is misbehaving — the record stays unchanged.",
            )

    def _verified_members(
        self, payloads: tuple[dict[str, object], ...] | list[dict[str, object]], group_id: str
    ) -> dict[str, GroupMember]:
        """Verify every §12.2 binding of a relay-presented roster."""

        members: dict[str, GroupMember] = {}
        for payload in payloads:
            member = GroupMember.from_event_payload(payload)
            if fingerprint_for_key_hex(member.public_key_hex) != member.fingerprint:
                raise GroupStateError(
                    "A roster member's key does not match their fingerprint.",
                    hint="Treat this relay as untrustworthy — do not trust the roster.",
                )
            try:
                handle = identity_id_for(bytes.fromhex(member.public_key_hex))
            except ValueError as exc:
                raise GroupStateError(
                    "A roster member's public key is malformed.",
                    hint="Treat this relay as untrustworthy — do not trust the roster.",
                ) from exc
            if member.handle != handle:
                raise GroupStateError(
                    "A roster member's handle does not match their key.",
                    hint="Treat this relay as untrustworthy — do not trust the roster.",
                )
            members[member.fingerprint] = member
        if not members or len(members) > MAX_GROUP_MEMBERS:
            raise GroupStateError(
                f"A group roster must hold 1..{MAX_GROUP_MEMBERS} members.",
                hint="Treat this relay as untrustworthy — do not trust the roster.",
            )
        _ = group_id
        return members

    # --------------------------------------------------------------- helpers

    def _require_usable_record(self, group_id: str) -> LocalGroupRecord:
        record = self._registry.require(group_id)
        record.require_active()
        record.require_unsuspecting()
        return record

    def _require_owner(self, record: LocalGroupRecord, identity: LocalIdentity) -> None:
        if not record.is_owner() or self._identity_fingerprint(identity) != (
            record.owner_fingerprint
        ):
            raise GroupPermissionError(
                "Only the owner may perform that operation.",
                hint="Membership authorization is pinned to the owner's identity key.",
            )

    def _after_committed(self, group_id: str) -> LocalGroupRecord:
        error = self._apply_errors.pop(group_id, None)
        if error is not None:
            raise error
        return self._registry.require(group_id)

    @staticmethod
    def _identity_fingerprint(identity: LocalIdentity) -> str:
        fingerprint = fingerprint_for_hex(identity.public_key_hex)
        if fingerprint is None:  # pragma: no cover - identities are well-formed
            raise GroupStateError(
                "The local identity key is malformed.",
                hint="Recreate the identity store.",
            )
        return fingerprint

    # ------------------------------------------------------ packet conversion

    @staticmethod
    def _event_from_packet(packet: Packet) -> GroupEvent:
        payload = packet.payload
        try:
            signature = base64.b64decode(str(payload["sig"]), validate=True)
            return GroupEvent(
                group_id=str(payload["group"]),
                epoch=int(str(payload["epoch"])),
                kind=GroupEventKind(str(payload["kind"])),
                subject_fingerprint=str(payload["subject"]),
                wall_ts=datetime.fromisoformat(str(payload["wall_ts"])),
                signer_fingerprint=str(payload["signer"]),
                signature=signature,
            )
        except (KeyError, ValueError, binascii.Error) as exc:
            if isinstance(exc, GroupValidationError):
                raise
            raise GroupValidationError(
                "A group event packet is malformed.",
                hint="This is a relay protocol violation — the event was dropped.",
            ) from exc

    @staticmethod
    def _verify_event_signature(record: LocalGroupRecord, event: GroupEvent) -> None:
        if event.kind is GroupEventKind.LEFT:
            if event.signer_fingerprint != event.subject_fingerprint:
                raise GroupPermissionError(
                    "A leave event must be signed by the leaving member.",
                    hint="This is a relay protocol violation — treat the roster as suspect.",
                )
            member = record.member(event.subject_fingerprint)
            if member is None:
                raise GroupStateError(
                    "A leave event references an identity not on the roster.",
                    hint="This is a relay protocol violation — treat the roster as suspect.",
                )
            key_hex = member.public_key_hex
            if fingerprint_for_key_hex(key_hex) != member.fingerprint:
                raise GroupStateError(
                    "The leaving member's stored key is inconsistent.",
                    hint="The roster is corrupt — re-sync from a trusted relay.",
                )
        else:
            if event.signer_fingerprint != record.owner_fingerprint:
                raise GroupPermissionError(
                    "That event was not signed by the pinned owner.",
                    hint="This is a relay protocol violation — treat the roster as suspect.",
                )
            key_hex = record.owner_public_key_hex
        if not verify_signature(bytes.fromhex(key_hex), event.signature, event.canonical()):
            raise GroupPermissionError(
                "The group event signature does not verify.",
                hint="This is a relay protocol violation — treat the roster as suspect.",
            )

    def _join_member_from_packet(
        self, record: LocalGroupRecord, packet: Packet, event: GroupEvent
    ) -> GroupMember:
        raw = packet.payload.get("member")
        if not isinstance(raw, dict):
            raise GroupStateError(
                "A join event lacks its member descriptor.",
                hint="This is a relay protocol violation — treat the roster as suspect.",
            )
        member = self._verified_members([raw], record.group_id).get(event.subject_fingerprint)
        if member is None:
            raise GroupStateError(
                "A join event's member descriptor does not match its subject.",
                hint="This is a relay protocol violation — treat the roster as suspect.",
            )
        return member


__all__ = ["LocalGroupManager"]
