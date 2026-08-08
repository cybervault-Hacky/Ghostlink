"""End-to-end group messaging service (Phase 6C — docs/GROUPS.md §17-§33).

One service per device (all groups share it). It owns the client side of
the pairwise mesh: per-recipient fanout, lazy link handshakes (with the
responder-side "knock" demand signal), offline queues, replay/sequence
gates, delivery ledgers, GACK/GREAD acknowledgements, and the roster/
epoch gates of §18.2 — every authorization decision happens here, never
in the UI.

Flow (sender, §18.1): verify ACTIVE + unsuspecting membership → assign
``message_id`` (``gmsg_``+16 hex CSPRNG) and the session-scoped sender
``gseq`` → for each other roster member enqueue the job (bounded offline
FIFO) and pump: ensure a live pairwise link (handshaking when needed),
seal the inner ``GMSG`` frame with the §20.2 AAD at the *current* epoch
(§27.2 — jobs are never pre-sealed to an epoch), transmit one addressed
GROUP_FORWARD envelope per recipient, track per-recipient states.

Flow (recipient, §18.2): envelope gates (``to == me``; sender on-roster;
group ACTIVE, unsuspecting; epoch via the mesh's §16.5 closed form) →
AEAD open (failures counted; suspect-link notices at the §31 threshold)
→ sealed-context == envelope-context cross-check → inner schema →
dedupe ``(sender, message_id)`` LRU → gseq monotonic-accept with the
§22 gap bound → render attributed to the *authenticated fingerprint*
(display name is decoration only) → GACK (re-ACK duplicates per §30).

Nothing plaintext or key-shaped is logged; all limits in ``net.py`` are
enforced here.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import json
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING

from ghostlink.constants.net import (
    GROUP_FANOUT_MAX,
    GROUP_GSEQ_MAX_GAP,
    GROUP_KEX_TIMEOUT_SECONDS,
    GROUP_LEDGER_KEPT,
    GROUP_MSG_MAX_BYTES,
    GROUP_OFFLINE_QUEUE_PER_MEMBER,
    GROUP_SEEN_IDS_PER_SENDER,
)
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.groups import (
    GroupError,
    GroupMessageError,
    GroupNotMemberError,
    GroupOfflineError,
    GroupRateLimitError,
)
from ghostlink.exceptions.messaging import DecryptionError, HandshakeFailedError
from ghostlink.exceptions.transport import TransportError
from ghostlink.groups.events import GroupEvent, GroupEventKind
from ghostlink.groups.frames import (
    KEX_HELLO,
    KEX_REPLY,
    KIND_KEX,
    KIND_MSG,
    DeliveryState,
    FrameType,
    GroupInnerFrame,
    GroupMessageLedger,
    GseqTracker,
    SeenMessageIds,
    gack_frame,
    generate_message_id,
    gmsg_frame,
    gread_frame,
    parse_inner_frame,
)
from ghostlink.groups.lifecycle import LocalGroupManager
from ghostlink.groups.mesh import GroupMeshManager, PairwiseLink, group_message_aad
from ghostlink.groups.models import LocalGroupRecord, LocalGroupState
from ghostlink.identity.lifecycle import IdentityManager, fingerprint_for_hex
from ghostlink.messaging.history import BaseHistory

if TYPE_CHECKING:
    from ghostlink.transport.relay.client import RelayClient
    from ghostlink.transport.relay.protocol import Packet

_logger = get_logger("groups.service")

_KEX_KNOCK: str = "knock"
_TX_BUCKET_CAPACITY: float = 20.0  # mirror of the relay burst (§32)
_TX_REFILL_PER_SECOND: float = 18.0  # sustained rate just inside the relay brake
_SWEEP_SECONDS: float = 5.0
_RESYNC_MIN_INTERVAL_SECONDS: float = 5.0


class GroupChatEventKind(str, Enum):
    """Event surfaces for UI layers (never contain secrets or keys)."""

    MESSAGE = "message"  # one delivered GMSG (attributed, authenticated)
    DELIVERY = "delivery"  # a ledger's per-recipient state changed
    NOTICE = "notice"  # security-relevant or availability information
    MEMBERSHIP = "membership"  # committed roster event notice text


@dataclass(frozen=True, slots=True)
class GroupChatEvent:
    kind: GroupChatEventKind
    group_id: str
    detail: str = ""
    frame: GroupInnerFrame | None = None
    ledger: GroupMessageLedger | None = None
    gap: bool = False


@dataclass(slots=True)
class _QueuedMessage:
    """One outbound job — deliberately *unsealed* and unpinned to an epoch.

    Pumped jobs seal against the *current* epoch at actual send time
    (§27.2); ``message_id``/``gseq`` stay stable across re-seals so
    receivers dedupe (§21).
    """

    message_id: str
    gseq: int
    text: str
    ts: float


@dataclass(slots=True)
class _MemberOutbox:
    queue: deque[_QueuedMessage] = field(default_factory=deque)
    pump: asyncio.Task[None] | None = None


class _GroupRuntime:
    """Per-group session state — memory only, by design (§21.5, §22)."""

    __slots__ = ("gseq", "ledgers", "next_gseq", "outboxes", "read_cursors", "seen")

    def __init__(self) -> None:
        self.next_gseq: int = 1
        self.seen = SeenMessageIds(GROUP_SEEN_IDS_PER_SENDER)
        self.gseq = GseqTracker(GROUP_GSEQ_MAX_GAP)
        self.ledgers: OrderedDict[str, GroupMessageLedger] = OrderedDict()
        self.read_cursors: dict[str, int] = {}
        self.outboxes: dict[str, _MemberOutbox] = {}

    def keep_ledger(self, ledger: GroupMessageLedger) -> None:
        self.ledgers[ledger.message_id] = ledger
        self.ledgers.move_to_end(ledger.message_id)
        while len(self.ledgers) > GROUP_LEDGER_KEPT:
            self.ledgers.popitem(last=False)


class GroupMessagingService:
    """The client-side group messaging engine (Phase 6C)."""

    def __init__(
        self,
        groups: LocalGroupManager,
        identities: IdentityManager,
        *,
        mesh: GroupMeshManager | None = None,
        history: BaseHistory | None = None,
        read_receipts: bool = True,
    ) -> None:
        self._groups = groups
        self._identities = identities
        self._mesh = mesh if mesh is not None else GroupMeshManager()
        self._history = history
        self._read_receipts = read_receipts
        self._client: RelayClient | None = None
        self._runtimes: dict[str, _GroupRuntime] = {}
        self._listeners: list[Callable[[GroupChatEvent], None]] = []
        self._link_waiters: dict[tuple[str, str], asyncio.Future[PairwiseLink]] = {}
        self._resync_at: dict[str, float] = {}
        self._tx_lock = asyncio.Lock()
        self._tx_tokens = _TX_BUCKET_CAPACITY
        self._tx_stamp = time.monotonic()
        self._sweeper: asyncio.Task[None] | None = None
        self._closed = False
        groups.add_state_listener(self._on_record_state)

    async def _regulated(self) -> None:
        """Client-side forward pacing (§32): a token bucket shaped just
        inside the relay's brake (burst 20, refill 20/s at the relay;
        refill 18/s here so the client can never exceed it sustained).

        A cold fanout bursts up to ~21 envelopes at the sender (two kex
        legs per responder-role peer plus one message per recipient),
        which would trip the brake on an unlucky role draw; with this
        bucket the first 20 are instant and the remainder rides the
        refill — sparse traffic pays zero added latency, continuous
        bursts mode-lock below the brake, and abusive pacing is still
        capped server-side.
        """

        async with self._tx_lock:
            now = time.monotonic()
            tokens = min(
                _TX_BUCKET_CAPACITY,
                self._tx_tokens + (now - self._tx_stamp) * _TX_REFILL_PER_SECOND,
            )
            if tokens < 1.0:
                await asyncio.sleep((1.0 - tokens) / _TX_REFILL_PER_SECOND)
                self._tx_tokens = 0.0
                self._tx_stamp = time.monotonic()
                return
            self._tx_tokens = tokens - 1.0
            self._tx_stamp = now

    # ------------------------------------------------------------- wiring

    @property
    def mesh(self) -> GroupMeshManager:
        return self._mesh

    def add_listener(self, listener: Callable[[GroupChatEvent], None]) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[GroupChatEvent], None]) -> None:
        with contextlib.suppress(ValueError):
            self._listeners.remove(listener)

    def _emit(self, event: GroupChatEvent) -> None:
        for listener in self._listeners:
            try:
                listener(event)
            except Exception:  # pragma: no cover - listener isolation
                _logger.debug("group chat listener failed", exc_info=True)

    async def attach(self, client: RelayClient) -> None:
        """Bind to a relay client; all prior links die with the old session."""

        self._mesh.teardown_all()  # §27.2 — links were tied to the dead session
        self._fail_link_waiters(
            GroupMessageError(
                "The relay connection was replaced — handshakes restart.",
                hint="Links are re-established lazily on next use.",
            )
        )
        client.set_group_forward_listener(self._on_forward)
        client.set_group_forward_error_listener(self._on_forward_error)
        self._client = client
        self._closed = False
        self._ensure_sweeper()

    async def close(self) -> None:
        """Detach, cancel background work, and zeroize every mesh key."""

        self._closed = True
        if self._client is not None:
            self._client.set_group_forward_listener(None)
            self._client.set_group_forward_error_listener(None)
        if self._sweeper is not None:
            self._sweeper.cancel()
            self._sweeper = None
        for runtime in self._runtimes.values():
            for outbox in runtime.outboxes.values():
                pump = outbox.pump
                if pump is not None and not pump.done():
                    pump.cancel()
        self._fail_link_waiters(
            GroupMessageError(
                "The messaging service closed.",
                hint="Links were zeroized; re-attach to continue.",
            )
        )
        self._mesh.teardown_all()

    # ------------------------------------------------------------- records

    def _my_identity_material(self) -> tuple[str, str]:
        identity = self._identities.ensure()
        fingerprint = fingerprint_for_hex(identity.public_key_hex)
        if fingerprint is None:  # pragma: no cover - identities are well-formed
            raise GroupMessageError("The local identity key is malformed.", hint="")
        return fingerprint, identity.public_key_hex

    def _require_record(self, group_id: str) -> LocalGroupRecord:
        record = self._groups.require(group_id)
        record.require_active()
        record.require_unsuspecting()
        return record

    def _require_sendable(self, group_id: str) -> LocalGroupRecord:
        """§18.1 step 1/§20.3: sender must be ACTIVE in the current epoch."""

        record = self._require_record(group_id)
        my_fingerprint, _ = self._my_identity_material()
        if record.my_fingerprint != my_fingerprint:
            raise GroupNotMemberError(
                "This group's record does not belong to the active identity.",
                hint="Switch identity or re-join under the current one.",
            )
        if record.member(my_fingerprint) is None:
            raise GroupNotMemberError(
                "You are not on this group's authoritative roster.",
                hint="Removed or departed members cannot send to the group.",
            )
        return record

    def _runtime(self, group_id: str) -> _GroupRuntime:
        return self._runtimes.setdefault(group_id, _GroupRuntime())

    # ---------------------------------------------------------------- sync

    async def sync(self, group_id: str, *, display_name: str = "") -> LocalGroupRecord:
        """Re-attest + reconcile with the relay (§27.3), then align epochs."""

        client = self._require_client()
        record = await self._groups.sync_group(client, group_id, display_name=display_name)
        self._mesh.observe_epoch(record.group_id, record.epoch)
        self._touch_members_online(record)
        return record

    def _require_client(self) -> RelayClient:
        if self._client is None:
            raise TransportError(
                "The messaging service is not attached to a relay client.",
                hint="Attach a connected RelayClient first.",
            )
        return self._client

    # ---------------------------------------------------------------- send

    async def send(self, group_id: str, text: str) -> GroupMessageLedger:
        """Fan out one group message, encrypted per recipient (§18.1).

        Returns the ledger once every reachable recipient's envelope is on
        the wire; DELIVERED/READ arrive asynchronously via GACK/GREAD.
        Offline members keep their job in the bounded queue (§29).
        """

        record = self._require_sendable(group_id)
        if not text or not text.strip():
            raise GroupMessageError(
                "Group message text must not be empty.",
                hint="Type a message and press Enter.",
            )
        if len(text.encode("utf-8")) > GROUP_MSG_MAX_BYTES:
            raise GroupMessageError(
                f"Group message must be ≤ {GROUP_MSG_MAX_BYTES} UTF-8 bytes.",
                hint="Split the message or shorten it.",
            )
        my_fingerprint, _ = self._my_identity_material()
        recipients = sorted(fp for fp in record.members if fp != my_fingerprint)
        if not recipients:
            raise GroupMessageError(
                "You are the only member — there is nobody to deliver to.",
                hint="Invite members first (ghostlink group invite).",
            )
        if len(recipients) > GROUP_FANOUT_MAX:  # pragma: no cover - roster cap
            raise GroupMessageError("Fanout would exceed the member cap.", hint="")
        runtime = self._runtime(group_id)
        message_id = generate_message_id()
        gseq = runtime.next_gseq
        runtime.next_gseq += 1
        ledger = GroupMessageLedger(
            message_id=message_id,
            group_id=group_id,
            gseq=gseq,
            text=text,
            ts=datetime.now(UTC).timestamp(),
        )
        ledger.recipients = {fp: DeliveryState.QUEUED for fp in recipients}
        runtime.keep_ledger(ledger)
        for fingerprint in recipients:
            self._enqueue(group_id, fingerprint, _QueuedMessage(message_id, gseq, text, ledger.ts))
        pumps = [self._ensure_pump(group_id, fp) for fp in recipients]
        await asyncio.gather(*pumps)  # pumps never raise; they mark ledgers
        self._record_history(record, ledger, outgoing=True)
        return ledger

    def _enqueue(self, group_id: str, fingerprint: str, job: _QueuedMessage) -> None:
        """Append to the per-member FIFO — bounded, drop-oldest (§29/§32)."""

        runtime = self._runtime(group_id)
        outbox = runtime.outboxes.setdefault(fingerprint, _MemberOutbox())
        while len(outbox.queue) >= GROUP_OFFLINE_QUEUE_PER_MEMBER:
            dropped = outbox.queue.popleft()
            ledger = runtime.ledgers.get(dropped.message_id)
            if ledger is not None and fingerprint in ledger.recipients:
                ledger.recipients[fingerprint] = DeliveryState.FAILED
            self._emit(
                GroupChatEvent(
                    GroupChatEventKind.NOTICE,
                    group_id,
                    f"Offline queue for {self._name_of(group_id, fingerprint)} is full — "
                    "the oldest undelivered message was dropped (FIFO bound).",
                    ledger=ledger,
                )
            )
        outbox.queue.append(job)

    # ------------------------------------------------------------ the pump

    def _ensure_pump(self, group_id: str, fingerprint: str) -> asyncio.Task[None]:
        runtime = self._runtime(group_id)
        outbox = runtime.outboxes.setdefault(fingerprint, _MemberOutbox())
        pump = outbox.pump
        if pump is None or pump.done():
            pump = asyncio.ensure_future(self._pump(group_id, fingerprint))
            pump.add_done_callback(self._observe_pump)
            outbox.pump = pump
        return pump

    @staticmethod
    def _observe_pump(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        failure = task.exception()
        if failure is not None:
            _logger.debug("group message pump failed: %s", failure)

    async def _pump(self, group_id: str, fingerprint: str) -> None:
        """Drain one member's queue: link → seal at current epoch → wire."""

        runtime = self._runtime(group_id)
        outbox = runtime.outboxes[fingerprint]
        client = self._require_client()
        while outbox.queue and not self._closed:
            job = outbox.queue[0]
            ledger = runtime.ledgers.get(job.message_id)
            try:
                record = self._require_sendable(group_id)
            except GroupError:
                self._fail_queue(group_id, fingerprint, "group no longer usable")
                return
            member = record.member(fingerprint)
            if member is None:
                # Removed/departed between compose and pump — never send to
                # a non-roster member (§18.1 step 4).
                self._fail_queue(group_id, fingerprint, "no longer a roster member")
                return
            if ledger is not None:
                ledger.recipients[fingerprint] = DeliveryState.SENDING
            try:
                link = await self._ensure_link(record, fingerprint)
                record = self._require_sendable(group_id)  # epoch may have leaped
            except GroupOfflineError:
                if ledger is not None:
                    ledger.recipients[fingerprint] = DeliveryState.QUEUED
                self._emit_delivery(ledger)
                self._emit(
                    GroupChatEvent(
                        GroupChatEventKind.NOTICE,
                        group_id,
                        f"{self._name_of(group_id, fingerprint)} is offline — "
                        "undelivered (retrying within the bounded queue).",
                    )
                )
                return  # retried by inbound triggers / sweeper / next send
            except GroupError as exc:
                if ledger is not None:
                    ledger.recipients[fingerprint] = DeliveryState.FAILED
                    label = f" — {exc.message}" if exc.message else ""
                    self._emit(
                        GroupChatEvent(
                            GroupChatEventKind.NOTICE,
                            group_id,
                            f"Delivery to {self._name_of(group_id, fingerprint)} failed{label}.",
                            ledger=ledger,
                        )
                    )
                outbox.queue.popleft()
                self._emit_delivery(ledger)
                continue
            if link.epoch != record.epoch:
                continue  # epoch leaped mid-pairing; re-ensure at the new epoch
            epoch = record.epoch  # sealed at the *current* epoch (§27.2)
            try:
                frame = gmsg_frame(
                    group_id,
                    epoch,
                    record.my_fingerprint,
                    fingerprint,
                    message_id=job.message_id,
                    gseq=job.gseq,
                    display_name=self._name_of(group_id, record.my_fingerprint),
                    text=job.text,
                    ts=job.ts,
                )
            except GroupError:
                outbox.queue.popleft()
                if ledger is not None:
                    ledger.recipients[fingerprint] = DeliveryState.FAILED
                self._emit_delivery(ledger)
                continue
            sealed = link.seal(
                frame, group_message_aad(group_id, epoch, record.my_fingerprint, fingerprint)
            )
            try:
                await self._regulated()
                await client.group_forward(
                    group_id,
                    epoch,
                    from_fingerprint=record.my_fingerprint,
                    to_fingerprint=fingerprint,
                    kind=KIND_MSG,
                    body_b64=base64.b64encode(sealed).decode("ascii"),
                )
            except TransportError as exc:
                if ledger is not None:
                    ledger.recipients[fingerprint] = DeliveryState.FAILED
                self._emit(
                    GroupChatEvent(
                        GroupChatEventKind.NOTICE,
                        group_id,
                        f"Delivery to {self._name_of(group_id, fingerprint)} failed "
                        f"— {exc.message}",
                        ledger=ledger,
                    )
                )
                self._emit_delivery(ledger)
                return  # connection-level failure: stop pumping this round
            outbox.queue.popleft()
            if ledger is not None:
                ledger.recipients[fingerprint] = DeliveryState.SENT
            self._emit_delivery(ledger)

    def _fail_queue(self, group_id: str, fingerprint: str, reason: str) -> None:
        runtime = self._runtime(group_id)
        outbox = runtime.outboxes.get(fingerprint)
        if outbox is None or not outbox.queue:
            return  # nothing ever queued for them — nothing to fail
        while outbox.queue:
            job = outbox.queue.popleft()
            ledger = runtime.ledgers.get(job.message_id)
            if ledger is not None and fingerprint in ledger.recipients:
                ledger.recipients[fingerprint] = DeliveryState.FAILED
            self._emit_delivery(ledger)
        self._emit(
            GroupChatEvent(
                GroupChatEventKind.NOTICE,
                group_id,
                f"Delivery to {self._name_of(group_id, fingerprint)} failed — {reason}.",
            )
        )

    # ------------------------------------------------------ link management

    async def _ensure_link(self, record: LocalGroupRecord, peer: str) -> PairwiseLink:
        """Live pairwise link for the current epoch, handshaking lazily.

        Deterministic initiator (§17.2.3): the smaller identity key sends
        the hello; the larger key sends a *knock* asking the peer to
        start — then waits. Both directions converge on one link.
        """

        group_id = record.group_id
        epoch = record.epoch
        link = self._mesh.link_for(group_id, peer, epoch)
        if link is not None:
            return link
        member = record.member(peer)
        if member is None:
            raise GroupNotMemberError(
                "That member is no longer on the roster.",
                hint="Links are only established with roster members.",
            )
        _, my_pubkey_hex = self._my_identity_material()
        key = (group_id, peer)
        waiter = self._link_waiters.get(key)
        if waiter is None or waiter.done():
            waiter = asyncio.get_running_loop().create_future()
            self._link_waiters[key] = waiter
            if self._mesh.initiates(my_pubkey_hex, member.public_key_hex):
                payload = self._mesh.hello_for(
                    group_id=group_id,
                    epoch=epoch,
                    peer_fingerprint=peer,
                    peer_pubkey_hex=member.public_key_hex,
                    own_pubkey_hex=my_pubkey_hex,
                )
                if payload is not None:
                    await self._send_kex(record, peer, KEX_HELLO, payload)
            else:
                await self._send_kex(record, peer, _KEX_KNOCK, None)
        try:
            return await asyncio.wait_for(asyncio.shield(waiter), timeout=GROUP_KEX_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            self._link_waiters.pop(key, None)
            raise GroupMessageError(
                f"Pairing with {member.display_name} timed out — link unavailable.",
                hint="The member may be offline or on a stale roster; try again.",
            ) from exc

    def _resolve_link_waiter(self, group_id: str, peer: str, link: PairwiseLink) -> None:
        waiter = self._link_waiters.pop((group_id, peer), None)
        if waiter is not None and not waiter.done():
            waiter.set_result(link)

    def _fail_link_waiter(self, group_id: str, peer: str, error: Exception) -> None:
        waiter = self._link_waiters.pop((group_id, peer), None)
        if waiter is not None and not waiter.done():
            waiter.set_exception(error)

    def _fail_link_waiters(self, error: Exception) -> None:
        for waiter in self._link_waiters.values():
            if not waiter.done():
                waiter.set_exception(error)
        self._link_waiters.clear()

    async def _send_kex(
        self, record: LocalGroupRecord, peer: str, scheme: str, payload: dict[str, str] | None
    ) -> None:
        client = self._require_client()
        body: dict[str, object] = {"s": scheme}
        if payload is not None:
            body["p"] = payload
        try:
            await self._regulated()
            await client.group_forward(
                record.group_id,
                record.epoch,
                from_fingerprint=record.my_fingerprint,
                to_fingerprint=peer,
                kind=KIND_KEX,
                body_b64=base64.b64encode(
                    json.dumps(body, separators=(",", ":")).encode("utf-8")
                ).decode("ascii"),
            )
        except TransportError as exc:
            raise GroupMessageError(
                f"Could not reach {peer} for pairing: {exc.message}", hint=""
            ) from exc

    # ------------------------------------------------------ receiving (§18.2)

    def _on_forward(self, packet: Packet) -> None:
        """Client listener (sync): hand off into the loop, never crash."""

        task = asyncio.ensure_future(self._handle_forward(packet))
        task.add_done_callback(self._observe_pump)

    async def _handle_forward(self, packet: Packet) -> None:
        payload = packet.payload
        group_id = str(payload["group"])
        sender = str(payload["from"])
        recipient = str(payload["to"])
        kind = str(payload["kind"])
        try:
            epoch = int(str(payload["epoch"]))
            body = base64.b64decode(str(payload["body"]).encode("ascii"), validate=True)
        except (ValueError, binascii.Error):
            _logger.info("group envelope for %s malformed — dropped", group_id)
            return
        my_fingerprint, _ = self._my_identity_material()
        if recipient != my_fingerprint:
            _logger.info(
                "group %s — envelope addressed elsewhere dropped (relay ACL violation)",
                group_id,
            )
            return
        record = self._groups.get(group_id)
        if record is None:
            self._emit_notice_once(
                group_id,
                f"Traffic arrived for unknown group {group_id} — dropped.",
            )
            return
        member = record.member(sender)
        if member is None:
            # Roster gate (§15.2 layer 2): off-roster senders are dropped
            # BEFORE any decryption attempt.
            _logger.info("group %s — frame from off-roster sender dropped", group_id)
            return
        if record.state is not LocalGroupState.ACTIVE:
            return  # archived groups are inert
        if record.suspect:
            self._emit_notice_once(
                group_id,
                f"{record.name}: roster state is unverified — a frame was dropped "
                "(re-sync before trusting).",
            )
            return
        if kind == KIND_KEX:
            await self._handle_kex(record, member.public_key_hex, sender, epoch, body)
        else:
            await self._handle_message(record, sender, epoch, body)

    def _emit_notice_once(self, group_id: str, detail: str) -> None:
        """Notice-list spam control: identical one-shot notices per group."""

        record = self._groups.get(group_id)
        if record is None:
            self._emit(GroupChatEvent(GroupChatEventKind.NOTICE, group_id, detail))
            return
        now = time.monotonic()
        if len(self._resync_at) > 128:
            self._resync_at.clear()  # bound the throttle map
        key = f"{group_id}:{detail}"
        if now - self._resync_at.get(key, 0.0) < _RESYNC_MIN_INTERVAL_SECONDS:
            return
        self._resync_at[key] = now
        self._emit(GroupChatEvent(GroupChatEventKind.NOTICE, group_id, detail))

    async def _handle_kex(
        self, record: LocalGroupRecord, peer_pubkey_hex: str, sender: str, epoch: int, body: bytes
    ) -> None:
        group_id = record.group_id
        if epoch != record.epoch:
            # KEX pins the *current* epoch only — the drain window is for
            # message frames, never for handshakes (§16.5).
            if epoch > record.epoch:
                self._trigger_resync(record, f"epoch {epoch} > local {record.epoch}")
            _logger.info("group %s — stale-epoch kex dropped", group_id)
            return
        try:
            document = json.loads(body.decode("utf-8"))
            scheme = str(document.get("s", ""))
            kex_payload = document.get("p")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            _logger.info("group %s — malformed kex body dropped", group_id)
            return
        _, my_pubkey_hex = self._my_identity_material()
        if scheme == _KEX_KNOCK:
            # The peer needs us to initiate (they hold the larger key).
            if not self._mesh.initiates(my_pubkey_hex, peer_pubkey_hex):
                _logger.info("group %s — knock from the initiating side dropped", group_id)
                return
            try:
                payload = self._mesh.hello_for(
                    group_id=group_id,
                    epoch=epoch,
                    peer_fingerprint=sender,
                    peer_pubkey_hex=peer_pubkey_hex,
                    own_pubkey_hex=my_pubkey_hex,
                    force=True,  # §27.2 — a knock means the peer lost its key
                )
            except GroupError as exc:
                _logger.info("group %s — knock refused: %s", group_id, exc.message)
                return
            if payload is not None:
                try:
                    await self._send_kex(record, sender, KEX_HELLO, payload)
                except GroupError as exc:
                    _logger.debug("group %s — knock-hello could not be sent: %s", group_id, exc)
            return
        if scheme == KEX_HELLO:
            if not isinstance(kex_payload, dict):
                _logger.info("group %s — kex hello without payload dropped", group_id)
                return
            try:
                reply = self._mesh.handle_hello(
                    group_id=group_id,
                    epoch=epoch,
                    peer_fingerprint=sender,
                    peer_pubkey_hex=peer_pubkey_hex,
                    own_pubkey_hex=my_pubkey_hex,
                    hello_payload=kex_payload,
                )
            except (HandshakeFailedError, GroupMessageError, ValueError) as exc:
                _logger.info(
                    "group %s — kex hello refused: %s",
                    group_id,
                    getattr(exc, "message", str(exc)),
                )
                return
            link = self._mesh.link_for(group_id, sender, epoch)
            if link is not None:
                self._resolve_link_waiter(group_id, sender, link)
                self._flush_member(record.group_id, sender)
            try:
                await self._send_kex(record, sender, KEX_REPLY, reply)
            except GroupError as exc:
                _logger.debug("group %s — kex reply could not be sent: %s", group_id, exc)
            return
        if scheme == KEX_REPLY:
            if not isinstance(kex_payload, dict):
                _logger.info("group %s — kex reply without payload dropped", group_id)
                return
            try:
                link = self._mesh.handle_reply(
                    group_id=group_id,
                    epoch=epoch,
                    peer_fingerprint=sender,
                    reply_payload=kex_payload,
                )
            except (HandshakeFailedError, GroupMessageError, ValueError) as exc:
                _logger.info(
                    "group %s — kex reply refused: %s",
                    group_id,
                    getattr(exc, "message", str(exc)),
                )
                self._fail_link_waiter(
                    group_id,
                    sender,
                    GroupMessageError(
                        "The pairwise handshake was refused.",
                        hint="Identity substitution or stale state — delivery fails closed.",
                    ),
                )
                return
            self._resolve_link_waiter(group_id, sender, link)
            self._flush_member(record.group_id, sender)
            return
        _logger.info("group %s — unknown kex scheme dropped: %.24r", group_id, scheme)

    async def _handle_message(
        self, record: LocalGroupRecord, sender: str, epoch: int, sealed: bytes
    ) -> None:
        group_id = record.group_id
        my_fingerprint, _ = self._my_identity_material()
        try:
            plaintext = self._mesh.open_inbound(
                group_id=group_id,
                epoch=epoch,
                sender=sender,
                recipient=my_fingerprint,
                sealed=sealed,
            )
        except DecryptionError:
            # AEAD gate (§31/§33): drop + count; suspect notice at threshold.
            if self._mesh.link_suspect(group_id, sender):
                self._emit_notice_once(
                    group_id,
                    f"{record.name}: link with {self._name_of(group_id, sender)} is suspect "
                    "— repeated integrity failures (dropped silently).",
                )
            return
        except GroupMessageError:
            if epoch > record.epoch:
                self._trigger_resync(record, f"epoch {epoch} > local {record.epoch}")
            # Post-drain old-epoch frames are rejects, not duplicates (§21.4).
            return
        try:
            frame = parse_inner_frame(plaintext)
        except GroupMessageError:
            _logger.info("group %s — sealed payload failed schema validation", group_id)
            return
        # §18.2 step 3: sealed context must equal the envelope context.
        if (
            frame.group_id != group_id
            or frame.epoch != epoch
            or frame.sender != sender
            or frame.recipient != my_fingerprint
        ):
            _logger.info("group %s — sealed context ≠ envelope context — rejected", group_id)
            return
        if frame.frame_type is FrameType.GACK:
            self._apply_gack(record, sender, frame)
            self._flush_member(group_id, sender)
            return
        if frame.frame_type is FrameType.GREAD:
            self._apply_gread(record, sender, frame)
            self._flush_member(group_id, sender)
            return
        runtime = self._runtime(group_id)
        if runtime.seen.seen(sender, frame.message_id):
            # §30: duplicates re-ACK but never double-render.
            await self._send_gack(record, sender, frame)
            return
        verdict = runtime.gseq.observe(sender, frame.gseq)
        if verdict == "duplicate":
            _logger.info("group %s — gseq rollback from a member dropped", group_id)
            return
        if verdict == "excessive-gap":
            self._emit_notice_once(
                group_id,
                f"{record.name}: {self._name_of(group_id, sender)} jumped gseq by more "
                f"than {GROUP_GSEQ_MAX_GAP} — frame dropped (suspect).",
            )
            return
        self._record_history_frame(record, frame)
        self._emit(
            GroupChatEvent(
                GroupChatEventKind.MESSAGE,
                group_id,
                frame=frame,
                gap=verdict == "gap",
            )
        )
        await self._send_gack(record, sender, frame)
        self._flush_member(group_id, sender)

    # ------------------------------------------------------- ack machinery

    async def _send_gack(
        self, record: LocalGroupRecord, sender: str, frame: GroupInnerFrame
    ) -> None:
        """Delivery ack, sealed to the very link that carried the message."""

        group_id = record.group_id
        link = self._mesh.accepting_link(group_id, frame.epoch, sender)
        if link is None:
            return  # link already gone — the sender's ledger just stays pending
        my_fingerprint, _ = self._my_identity_material()
        sealed = link.seal(
            gack_frame(group_id, frame.epoch, my_fingerprint, sender, message_id=frame.message_id),
            group_message_aad(group_id, frame.epoch, my_fingerprint, sender),
        )
        try:
            await self._regulated()
            await self._require_client().group_forward(
                group_id,
                frame.epoch,
                from_fingerprint=my_fingerprint,
                to_fingerprint=sender,
                kind=KIND_MSG,
                body_b64=base64.b64encode(sealed).decode("ascii"),
            )
        except TransportError as exc:
            _logger.debug("group %s — GACK could not be sent: %s", group_id, exc)
        await self._maybe_send_gread(record, sender, link)

    async def _maybe_send_gread(
        self, record: LocalGroupRecord, sender: str, link: PairwiseLink
    ) -> None:
        """Cumulative read cursor per sender gseq — optional (§18.4)."""

        if not self._read_receipts:
            return
        group_id = record.group_id
        runtime = self._runtime(group_id)
        cursor = runtime.gseq.cursor(sender)
        if cursor <= runtime.read_cursors.get(sender, 0):
            return
        current = self._mesh.link_for(group_id, sender, record.epoch)
        if current is None or current is not link:
            return  # lazily skipped; the next message's link may carry it
        my_fingerprint, _ = self._my_identity_material()
        sealed = link.seal(
            gread_frame(group_id, record.epoch, my_fingerprint, sender, upto_gseq=cursor),
            group_message_aad(group_id, record.epoch, my_fingerprint, sender),
        )
        try:
            await self._regulated()
            await self._require_client().group_forward(
                group_id,
                record.epoch,
                from_fingerprint=my_fingerprint,
                to_fingerprint=sender,
                kind=KIND_MSG,
                body_b64=base64.b64encode(sealed).decode("ascii"),
            )
        except TransportError:
            return
        runtime.read_cursors[sender] = cursor

    def _apply_gack(self, record: LocalGroupRecord, sender: str, frame: GroupInnerFrame) -> None:
        runtime = self._runtime(record.group_id)
        ledger = runtime.ledgers.get(frame.message_id)
        if ledger is None or sender not in ledger.recipients:
            return  # unknown/pruned message — silently absorbed
        if ledger.recipients[sender] is DeliveryState.SENT:
            ledger.recipients[sender] = DeliveryState.DELIVERED
        self._emit_delivery(ledger)

    def _apply_gread(self, record: LocalGroupRecord, sender: str, frame: GroupInnerFrame) -> None:
        runtime = self._runtime(record.group_id)
        touched: list[GroupMessageLedger] = []
        for ledger in runtime.ledgers.values():
            if sender in ledger.recipients and ledger.gseq <= frame.gseq:
                before = ledger.recipients[sender]
                ledger.apply_read_cursor(frame.gseq)
                if ledger.recipients[sender] is not before:
                    touched.append(ledger)
        for ledger in touched:
            self._emit_delivery(ledger)

    def _emit_delivery(self, ledger: GroupMessageLedger | None) -> None:
        if ledger is not None:
            self._emit(GroupChatEvent(GroupChatEventKind.DELIVERY, ledger.group_id, ledger=ledger))

    # ------------------------------------------------- forward-error intake

    def _on_forward_error(self, group_id: str, member: str, code: str, message: str) -> None:
        """Relay refusal for one addressed envelope (member-scoped)."""

        runtime = self._runtime(group_id)
        if code == "group/offline":
            # The member's relay session is gone — every pairwise link to
            # them died with it (§27.2). Tear it down now so the next send
            # re-handshakes fresh keys instead of reusing a dead link whose
            # frames the reconnected peer could never open (fail-closed).
            self._mesh.teardown_link(group_id, member)
            self._fail_link_waiter(
                group_id,
                member,
                GroupOfflineError(message or "Member offline.", hint=""),
            )
            self._requeue_sent(group_id, member)
            return
        if code == "group/rate":
            # Transient brake (§32): the envelope never reached the member,
            # so requeue it like offline work. The bounded queue still caps
            # retries (drop-oldest marks FAILED), a persistent abuser ends
            # FAILED, and a momentary burst resolves itself.
            self._fail_link_waiter(
                group_id,
                member,
                GroupRateLimitError(message or "Rate limited.", hint=""),
            )
            self._requeue_sent(group_id, member, "relay rate limit")
            return
        error: GroupError
        notice = ""
        if code == "group/not-member":
            error = GroupNotMemberError(message or "Not a member.", hint="")
            notice = f"Delivery to {self._name_of(group_id, member)} failed — not a roster member."
        elif code == "group/unknown":
            self._mark_defunct(group_id)
            error = GroupMessageError(message or "Group unknown at relay.", hint="")
        else:
            error = GroupMessageError(message or f"Envelope refused ({code}).", hint="")
        self._fail_link_waiter(group_id, member, error)
        # Anything in flight to this member failed its verdict.
        flipped = False
        for ledger in runtime.ledgers.values():
            state = ledger.recipients.get(member)
            if state in (DeliveryState.SENDING, DeliveryState.SENT):
                ledger.recipients[member] = DeliveryState.FAILED
                flipped = True
                self._emit_delivery(ledger)
        if notice and flipped:
            self._emit(GroupChatEvent(GroupChatEventKind.NOTICE, group_id, notice))

    def _requeue_sent(self, group_id: str, member: str, reason: str = "offline") -> None:
        """Re-queued offline: flip unread SENT envelopes back to QUEUED.

        Offline ERRORs mirror our forwards FIFO, so flipping every not-yet-
        delivered ledger of this member preserves order; the member's pump
        resends them re-sealed (§27.2, §29).
        """

        runtime = self._runtime(group_id)
        outbox = runtime.outboxes.setdefault(member, _MemberOutbox())
        queued_jobs: list[_QueuedMessage] = []
        for ledger in runtime.ledgers.values():
            state = ledger.recipients.get(member)
            if state is not DeliveryState.SENT:
                continue
            if any(job.message_id == ledger.message_id for job in outbox.queue):
                ledger.recipients[member] = DeliveryState.QUEUED
                continue
            ledger.recipients[member] = DeliveryState.QUEUED
            queued_jobs.append(
                _QueuedMessage(ledger.message_id, ledger.gseq, ledger.text, ledger.ts)
            )
        for job in reversed(sorted(queued_jobs, key=lambda item: item.gseq)):
            outbox.queue.appendleft(job)
            while len(outbox.queue) > GROUP_OFFLINE_QUEUE_PER_MEMBER:
                trimmed = outbox.queue.pop()  # newest-first requeue; trim far end
                trimmed_ledger = runtime.ledgers.get(trimmed.message_id)
                if trimmed_ledger is not None and member in trimmed_ledger.recipients:
                    trimmed_ledger.recipients[member] = DeliveryState.FAILED
                self._emit_delivery(trimmed_ledger)
        if queued_jobs:
            if reason == "offline":
                detail = (
                    f"{self._name_of(group_id, member)} is offline — "
                    "held in the bounded retry queue."
                )
            else:
                detail = (
                    f"Delivery to {self._name_of(group_id, member)} slowed by the relay "
                    f"({reason}) — held in the bounded retry queue."
                )
            self._emit_notice_once(group_id, detail)

    def _mark_defunct(self, group_id: str) -> None:
        try:
            record = self._groups.mark_defunct(group_id)
        except GroupError:
            return
        self._mesh.teardown_group(group_id)
        self._emit(
            GroupChatEvent(
                GroupChatEventKind.NOTICE,
                group_id,
                f"{record.name}: the relay lost this group (restart) — it is now defunct. "
                "The owner may re-create it.",
            )
        )

    # ------------------------------------------------- membership reactions

    def _on_record_state(self, record: LocalGroupRecord, event: GroupEvent) -> None:
        """Committed-event reactions: epoch leaps + departures (§16.5/§23)."""

        group_id = record.group_id
        dropped = self._mesh.observe_epoch(group_id, record.epoch)
        for group, peer in dropped:
            self._fail_link_waiter(
                group,
                peer,
                GroupMessageError(
                    "The epoch leaped mid-handshake — pairing restarts.",
                    hint="Links are re-established at the current epoch.",
                ),
            )
        subject = event.subject_fingerprint
        if event.kind in (GroupEventKind.LEFT, GroupEventKind.REMOVED):
            self._mesh.teardown_link(group_id, subject)
            runtime = self._runtimes.get(group_id)
            if runtime is not None and subject != record.my_fingerprint:
                self._fail_queue(group_id, subject, "no longer a roster member")
        if event.kind is GroupEventKind.DISSOLVED or subject == record.my_fingerprint:
            self._mesh.teardown_group(group_id)
        self._emit(
            GroupChatEvent(
                GroupChatEventKind.MEMBERSHIP,
                group_id,
                self._notice_for(record, event),
            )
        )

    @staticmethod
    def _notice_for(record: LocalGroupRecord, event: GroupEvent) -> str:
        name = record.name
        if event.kind is GroupEventKind.JOIN:
            return f"{name}: a member joined — now at epoch {record.epoch}"
        if event.kind is GroupEventKind.LEFT:
            return f"{name}: a member left — now at epoch {record.epoch}"
        if event.kind is GroupEventKind.REMOVED:
            return f"{name}: a member was removed — now at epoch {record.epoch}"
        return f"{name}: the group was dissolved (epoch {record.epoch})"

    def _flush_member(self, group_id: str, fingerprint: str) -> None:
        """Any live traffic from a member proves reachability — retry."""

        runtime = self._runtimes.get(group_id)
        if runtime is None:
            return
        outbox = runtime.outboxes.get(fingerprint)
        if outbox is not None and outbox.queue:
            self._ensure_pump(group_id, fingerprint)

    def _touch_members_online(self, record: LocalGroupRecord) -> None:
        for fingerprint in record.members:
            if fingerprint != record.my_fingerprint:
                self._flush_member(record.group_id, fingerprint)

    def _trigger_resync(self, record: LocalGroupRecord, reason: str) -> None:
        group_id = record.group_id
        now = time.monotonic()
        if now - self._resync_at.get(f"sync:{group_id}", 0.0) < _RESYNC_MIN_INTERVAL_SECONDS:
            return
        self._resync_at[f"sync:{group_id}"] = now
        self._emit(
            GroupChatEvent(
                GroupChatEventKind.NOTICE,
                group_id,
                f"{record.name}: roster may be stale ({reason}) — re-syncing.",
            )
        )
        task = asyncio.ensure_future(self._resync(group_id))
        task.add_done_callback(self._observe_pump)

    async def _resync(self, group_id: str) -> None:
        try:
            record = await self.sync(group_id)
        except GroupError as exc:
            self._emit(
                GroupChatEvent(
                    GroupChatEventKind.NOTICE,
                    group_id,
                    f"Re-sync failed: {exc.message}",
                )
            )
            return
        self._emit(
            GroupChatEvent(
                GroupChatEventKind.NOTICE,
                group_id,
                f"{record.name}: re-synced — epoch {record.epoch}.",
            )
        )

    # ------------------------------------------------------------- sweeper

    def _ensure_sweeper(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return
            self._sweeper = loop.create_task(self._sweep())
            self._sweeper.add_done_callback(self._observe_pump)

    async def _sweep(self) -> None:
        try:
            while not self._closed:
                await asyncio.sleep(_SWEEP_SECONDS)
                for _group_id, peer in self._mesh.expire():
                    self._emit(
                        GroupChatEvent(
                            GroupChatEventKind.NOTICE,
                            _group_id,
                            f"Drain window closed for the link to {peer} — old-epoch "
                            "keys zeroized.",
                        )
                    )
                for group_id, runtime in self._runtimes.items():
                    for fingerprint, outbox in runtime.outboxes.items():
                        if outbox.queue:
                            self._ensure_pump(group_id, fingerprint)
        except asyncio.CancelledError:
            raise

    # ------------------------------------------------------------- helpers

    def _name_of(self, group_id: str, fingerprint: str) -> str:
        """Display name *decoration* for humans; never authorization (§8.1)."""

        record = self._groups.get(group_id)
        member = record.member(fingerprint) if record is not None else None
        if member is not None:
            return member.display_name
        return f"GLFP-{fingerprint[5:9]}…" if fingerprint.startswith("GLFP-") else "member"

    def _record_history(
        self, record: LocalGroupRecord, ledger: GroupMessageLedger, *, outgoing: bool
    ) -> None:
        if self._history is None:
            return
        self._history.record_group(
            message_id=ledger.message_id,
            group_id=record.group_id,
            direction="outgoing",
            author=self._name_of(record.group_id, record.my_fingerprint),
            text=ledger.text,
            sent_at=ledger.ts,
            status=ledger.status_line(),
        )

    def _record_history_frame(self, record: LocalGroupRecord, frame: GroupInnerFrame) -> None:
        if self._history is None:
            return
        self._history.record_group(
            message_id=frame.message_id,
            group_id=record.group_id,
            direction="incoming",
            author=self._name_of(record.group_id, frame.sender),
            text=frame.text,
            sent_at=frame.ts or datetime.now(UTC).timestamp(),
            status="delivered",
        )

    # ------------------------------------------------------------ query API

    def ledger_for(self, group_id: str, message_id: str) -> GroupMessageLedger | None:
        runtime = self._runtimes.get(group_id)
        return runtime.ledgers.get(message_id) if runtime is not None else None

    def ledgers_for(self, group_id: str) -> list[GroupMessageLedger]:
        runtime = self._runtimes.get(group_id)
        return list(runtime.ledgers.values()) if runtime is not None else []

    def link_states(self, group_id: str) -> dict[str, str]:
        """Per-member link state for UI status lines (metadata only)."""

        record = self._groups.get(group_id)
        if record is None:
            return {}
        states: dict[str, str] = {}
        for fingerprint in record.members:
            if fingerprint == record.my_fingerprint:
                continue
            if self._mesh.link_for(group_id, fingerprint, record.epoch) is not None:
                states[fingerprint] = "linked"
            elif self._mesh.is_handshake_pending(group_id, fingerprint):
                states[fingerprint] = "pairing"
            else:
                states[fingerprint] = "idle"
        return states


__all__ = [
    "GroupChatEvent",
    "GroupChatEventKind",
    "GroupMessagingService",
]
