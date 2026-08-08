"""Chat session orchestration (Phase 3).

:class:`ChatSession` is the heart of one-to-one messaging. It owns, for one
room channel on a live relay client:

* the authenticated key exchange (fresh session key per conversation, fresh
  key again on every reconnect — forward secrecy per session)
* the outgoing pump: seal → FORWARD → wait for ACK → deliver, with bounded
  resends and requeue-on-reconnect
* the incoming path: strict frame validation → AEAD open (tampering raises
  clear errors) → duplicate detection → in-order delivery
* delivery acknowledgements, optional read receipts, typing indicators
* peer presence and connection lifecycle events for the UI
* best-effort key zeroization and full state cleanup on close

The relay never sees plaintext; this module never logs it either.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ghostlink.core.logging import get_logger
from ghostlink.exceptions.base import GhostLinkError
from ghostlink.exceptions.messaging import (
    DecryptionError,
    HandshakeFailedError,
    MessageValidationError,
    PeerUnavailableError,
    SecureChannelError,
)
from ghostlink.exceptions.transport import TransportError
from ghostlink.identity.fingerprint import identity_fingerprint
from ghostlink.messaging.history import BaseHistory
from ghostlink.messaging.models.message import (
    EncryptedPayload,
    Message,
    MessageDirection,
    MessageStatus,
    generate_message_id,
    message_aad,
    validate_display_name,
)
from ghostlink.messaging.packets.frames import (
    FILE_FRAME_TYPES,
    Frame,
    FrameType,
    decode_frame,
    encode_frame,
    error_frame,
    kex_hello_frame,
    kex_reply_frame,
    message_ack_frame,
    message_frame,
    read_receipt_frame,
    typing_start_frame,
    typing_stop_frame,
)
from ghostlink.messaging.protocol.crypto import open_sealed, seal
from ghostlink.messaging.protocol.handshake import (
    HandshakeInitiator,
    HandshakeResponder,
    SecureSession,
)
from ghostlink.messaging.queue.inbox import Inbox, InboxVerdict
from ghostlink.messaging.queue.outbox import Outbox
from ghostlink.messaging.receipts import ReceiptTracker
from ghostlink.messaging.typing import TypingIndicator
from ghostlink.transport.connection import ConnectionState, StateChange
from ghostlink.transport.relay.client import RelayClient

_logger = get_logger("messaging.session")

PEER_DEFAULT_NAME: str = "Peer"
_RECONNECT_SETTLE_SECONDS: float = 0.25
_WATCHDOG_TICK_SECONDS: float = 1.0

#: Async consumer of validated FILE_* frames — the transfer manager registers
#: one while a session lives, so the messaging layer never imports transfer code.
TransferFrameHandler = Callable[[Frame], Coroutine[Any, Any, None]]


class ChatEventKind(str, Enum):
    """Everything the UI can observe about a chat session."""

    SESSION = "session"  # handshake progress / secure session ready
    MESSAGE = "message"  # incoming message, decrypted and in order
    DELIVERY = "delivery"  # outgoing message state changed
    TYPING = "typing"  # peer typing state changed
    PEER = "peer"  # peer joined / left
    CONNECTION = "connection"  # relay connectivity lost / recovered / closed
    NOTICE = "notice"  # operational notices (never message content)


@dataclass(frozen=True, slots=True)
class ChatEvent:
    kind: ChatEventKind
    detail: str = ""
    message: Message | None = None
    at: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class ChatSessionConfig:
    """Tuning knobs for one chat session (settings + defaults)."""

    handshake_timeout_seconds: float = 30.0
    ack_timeout_seconds: float = 8.0
    resend_attempts: int = 4
    attach_timeout_seconds: float = 10.0
    reorder_window_seconds: float = 5.0
    read_receipts: bool = True
    typing_indicators: bool = True


@dataclass(slots=True)
class ChatStats:
    """Operational counters (ids, sizes, states — never content)."""

    messages_sent: int = 0
    messages_received: int = 0
    resends: int = 0
    rekeys: int = 0
    decrypt_failures: int = 0
    frames_dropped: int = 0
    acks_sent: int = 0
    acks_received: int = 0
    started_at: float = field(default_factory=time.time)


class ChatSession:
    """A live one-to-one encrypted conversation over a relay channel."""

    def __init__(
        self,
        client: RelayClient,
        *,
        channel: str,
        role: str,
        display_name: str,
        config: ChatSessionConfig | None = None,
        history: BaseHistory | None = None,
        peer_name: str | None = None,
        identity_public_key_hex: str | None = None,
    ) -> None:
        if role not in ("host", "guest"):
            raise MessageValidationError(
                f"Chat role must be 'host' or 'guest', got '{role}'.",
                hint="Hosts create rooms, guests join them.",
            )
        self._client = client
        self._channel = channel
        self._role = role
        self._display_name = validate_display_name(display_name)
        self._config = config or ChatSessionConfig()
        self._history = history
        self._peer_name = validate_display_name(peer_name) if peer_name else PEER_DEFAULT_NAME
        self._identity_public_key_hex = identity_public_key_hex
        self._peer_identity_key_hex: str | None = None

        self._secure: SecureSession | None = None
        self._session_key: bytearray | None = None
        self._initiator: HandshakeInitiator | None = None

        self._outbox = Outbox()
        self._inbox = Inbox(reorder_window_seconds=self._config.reorder_window_seconds)
        self._typing = TypingIndicator()
        self._receipts = ReceiptTracker(read_receipts_enabled=self._config.read_receipts)
        self._stats = ChatStats()

        self._next_sequence = 1
        self._listeners: list[Callable[[ChatEvent], None]] = []
        self._transfer_delegate: TransferFrameHandler | None = None
        self._ready = asyncio.Event()
        self._peer_present = asyncio.Event()
        self._closing_event = asyncio.Event()
        self._transport_usable = asyncio.Event()
        self._pump_task: asyncio.Task[None] | None = None
        self._watchdog_task: asyncio.Task[None] | None = None
        self._background: set[asyncio.Task[None]] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._started = False
        self._closing = False

    # ------------------------------------------------------------- properties

    @property
    def relay_client(self) -> RelayClient:
        return self._client

    @property
    def channel(self) -> str:
        return self._channel

    @property
    def role(self) -> str:
        return self._role

    @property
    def display_name(self) -> str:
        return self._display_name

    @property
    def peer_name(self) -> str:
        return self._peer_name

    @property
    def is_ready(self) -> bool:
        return self._ready.is_set()

    @property
    def peer_present(self) -> bool:
        return self._peer_present.is_set()

    @property
    def conversation_id(self) -> str | None:
        return self._secure.conversation_id if self._secure else None

    @property
    def fingerprint(self) -> str | None:
        return self._secure.fingerprint if self._secure else None

    @property
    def identity_bound(self) -> bool:
        """True when both peers presented transcript-bound identity keys."""

        return self._secure.identity_bound if self._secure else False

    @property
    def peer_identity_key_hex(self) -> str | None:
        """The peer's identity public key as presented in the handshake."""

        return self._peer_identity_key_hex

    @property
    def own_identity_key_hex(self) -> str | None:
        """The identity public key presented by this side of the session."""

        return self._identity_public_key_hex

    @property
    def unread_count(self) -> int:
        return self._inbox.unread_count

    @property
    def peer_typing(self) -> bool:
        return self._typing.peer_typing()

    @property
    def stats(self) -> ChatStats:
        return self._stats

    @property
    def receipts(self) -> ReceiptTracker:
        return self._receipts

    @property
    def outbox(self) -> Outbox:
        return self._outbox

    @property
    def inbox(self) -> Inbox:
        return self._inbox

    # --------------------------------------------------------------- events

    def add_listener(self, listener: Callable[[ChatEvent], None]) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: Callable[[ChatEvent], None]) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    # ------------------------------------------------------ sub-protocols (Phase 4)

    def set_transfer_delegate(self, delegate: TransferFrameHandler | None) -> None:
        """Attach (or detach) the transfer manager as the FILE_* frame consumer."""

        self._transfer_delegate = delegate

    def session_key_copy(self) -> bytes | None:
        """A copy of the live session key for in-process sub-protocols.

        The transfer layer HKDF-derives per-transfer keys from it; returns
        None before the handshake completes or after close/wipe.
        """

        return self._key_bytes()

    async def send_channel_frame(self, frame: Frame) -> None:
        """Send one validated frame for a layered sub-protocol (transfer)."""

        await self._send_frame(frame)

    def _emit(self, kind: ChatEventKind, detail: str = "", message: Message | None = None) -> None:
        event = ChatEvent(kind=kind, detail=detail, message=message)
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception as exc:  # a UI bug must not break the session
                _logger.debug("chat event listener failed: %s", exc)

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> int:
        """Attach to the channel and begin the handshake dance.

        Returns the number of peers already present. The secure session
        itself completes asynchronously — :meth:`wait_ready` or the SESSION
        event tell callers when encryption is live.
        """

        if self._started:
            raise SecureChannelError(
                "Chat sessions are single-use.",
                hint="Create a fresh ChatSession for every conversation.",
            )
        self._started = True
        self._loop = asyncio.get_running_loop()
        self._client.set_forward_listener(self._on_forward)
        self._client.set_peer_listener(self._on_peer)
        self._client.manager.add_state_listener(self._on_connection_state)
        peers = await self._client.attach(
            self._channel, self._role, timeout_seconds=self._config.attach_timeout_seconds
        )
        if peers > 0:
            self._peer_present.set()
        self._transport_usable.set()
        self._emit(
            ChatEventKind.PEER,
            f"{self._peer_name} is online" if peers else "Waiting for the peer to join…",
        )
        if peers > 0 and self._role == "host":
            await self._initiate_handshake()
        self._watchdog_task = asyncio.create_task(self._watchdog())
        _logger.info(
            "chat session started — channel=%s role=%s peers=%d",
            self._channel,
            self._role,
            peers,
        )
        return peers

    async def wait_ready(self, *, timeout_seconds: float | None = None) -> SecureSession:
        """Block until the secure session is live (clear error otherwise)."""

        timeout = (
            self._config.handshake_timeout_seconds if timeout_seconds is None else timeout_seconds
        )
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=timeout)
        except TimeoutError as exc:
            raise PeerUnavailableError(
                f"Secure session was not established within {timeout:.0f}s.",
                hint="Check that the peer is online and running the same "
                "GhostLink protocol, then try again.",
            ) from exc
        assert self._secure is not None
        return self._secure

    async def close(self) -> None:
        """Tear down: stop tasks, wipe key material, detach, clear queues."""

        if self._closing:
            return
        self._closing = True
        self._closing_event.set()
        for task in (self._pump_task, self._watchdog_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._pump_task, self._watchdog_task):
            if task is not None and not task.done():
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        background = [task for task in self._background if not task.done()]
        for task in background:
            task.cancel()
        if background:
            await asyncio.gather(*background, return_exceptions=True)
        self._outbox.resolve_ack_waiters_on_disconnect(error="session closed")
        self._zeroize()
        self._secure = None
        self._ready.clear()
        try:
            await self._client.detach(self._channel)
        except Exception as exc:
            _logger.debug("detach during close failed: %s", exc)
        self._outbox.clear()
        self._inbox.clear()
        self._typing.reset()
        if self._history is not None:
            self._history.close()
        self._client.set_forward_listener(None)
        self._client.set_peer_listener(None)
        self._client.manager.remove_state_listener(self._on_connection_state)
        _logger.info("chat session closed — channel=%s", self._channel)

    # ----------------------------------------------------------- sending API

    async def send_message(self, text: str) -> Message:
        """Queue one outgoing message; returns it in the QUEUED state.

        Requires one completed handshake at minimum — messages typed while
        the link is down simply wait in the queue and are sealed with the
        *next* session key once the session re-establishes."""

        if self._secure is None:
            raise SecureChannelError(
                "The secure session is not ready yet.",
                hint="Wait for 'Encryption: Active' before sending, or run wait_ready() first.",
            )
        message = Message(
            message_id=generate_message_id(),
            conversation_id=self._require_conversation_id(),
            direction=MessageDirection.OUTGOING,
            sender=self._display_name,
            recipient=self._peer_name,
            text=text,  # validated inside the model
            sequence=self._next_sequence,
            status=MessageStatus.QUEUED,
        )
        self._next_sequence += 1
        self._outbox.enqueue(message)
        self._ensure_pump()
        return message

    async def notify_typing(self, *, keystroke: bool) -> None:
        """Throttled outgoing typing notifications (setting-gated)."""

        if not self._config.typing_indicators or not self.is_ready:
            return
        if keystroke:
            if self._typing.note_keystroke():
                await self._send_frame_quiet(typing_start_frame())
        elif self._typing.note_sent_or_stopped():
            await self._send_frame_quiet(typing_stop_frame())

    async def mark_read(self) -> int:
        """Everything displayed is read; maybe emit one coalesced receipt."""

        cursor = self._inbox.mark_all_read()
        emit_cursor = self._receipts.read_to_emit(cursor)
        if emit_cursor is not None and self.is_ready:
            await self._send_frame_quiet(read_receipt_frame(emit_cursor))
            self._receipts.note_read_sent(emit_cursor)
        return cursor

    # --------------------------------------------------------------- send pump

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        """Track one background task so close() can drain it deterministically."""

        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(self._observe_task_failure)

    def _ensure_pump(self) -> None:
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = asyncio.create_task(self._pump())
            self._pump_task.add_done_callback(self._observe_task_failure)

    async def _pump(self) -> None:
        while not self._closing:
            message = self._outbox.next_queued()
            if message is None:
                return
            await self._deliver(message)

    async def _deliver(self, message: Message) -> None:
        for attempt in range(1, self._config.resend_attempts + 1):
            if not await self._wait_sendable():
                self._outbox.to_queued(message.message_id)
                return
            key = self._key_bytes()
            if key is None:
                continue
            aad = message_aad(message.message_id, message.sequence)
            sealed = seal(key, message.text.encode("utf-8"), aad)
            payload = EncryptedPayload(sealed=sealed, aad=aad)
            message = message.with_payload(payload)
            frame = message_frame(message.message_id, message.sequence, payload.to_b64())
            waiter: asyncio.Future[Message] = asyncio.get_running_loop().create_future()
            self._outbox.register_ack_waiter(message.message_id, waiter)
            try:
                await self._send_frame(frame)
            except TransportError as exc:
                self._outbox.drop_ack_waiter(message.message_id)
                _logger.info("send failed (%s) — retrying when the link recovers", exc.message)
                continue
            sent = self._outbox.mark_sent(message.message_id)
            if attempt == 1:
                self._stats.messages_sent += 1
            else:
                self._stats.resends += 1
            if sent is not None:
                self._emit(ChatEventKind.DELIVERY, message=sent)
                if attempt == 1 and self._history is not None:
                    self._history.record(sent)
            try:
                result = await asyncio.wait_for(waiter, timeout=self._config.ack_timeout_seconds)
            except TimeoutError:
                self._outbox.drop_ack_waiter(message.message_id)
                _logger.debug("ack timeout for %s (attempt %d)", message.message_id, attempt)
                continue
            except asyncio.CancelledError:
                # The waiter was cancelled by a disconnect (resend after
                # recovery) or the pump itself is shutting down (propagate).
                self._outbox.drop_ack_waiter(message.message_id)
                if self._closing:
                    raise
                continue
            self._emit(ChatEventKind.DELIVERY, message=result)
            return
        self._outbox.drop_ack_waiter(message.message_id)
        failed = self._outbox.mark_failed(
            message.message_id, error="no delivery acknowledgement from the peer"
        )
        if failed is not None:
            self._emit(ChatEventKind.DELIVERY, message=failed)
            self._emit(
                ChatEventKind.NOTICE,
                f"Message could not be delivered after {self._config.resend_attempts} attempts.",
            )

    async def _wait_sendable(self) -> bool:
        """Wait until session ready + peer present + transport usable."""

        while not self._closing:
            if (
                self._ready.is_set()
                and self._peer_present.is_set()
                and self._transport_usable.is_set()
            ):
                return True
            pending = {
                asyncio.create_task(event.wait())
                for event in (
                    self._ready,
                    self._peer_present,
                    self._transport_usable,
                    self._closing_event,
                )
            }
            _, waiters = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for task in waiters:
                task.cancel()
        return False

    # ----------------------------------------------------------- handshake

    async def _initiate_handshake(self) -> None:
        """Host side: send a fresh KEX_HELLO for a new session key."""

        if self._closing:
            return
        self._initiator = HandshakeInitiator(
            self._channel,
            display_name=self._display_name,
            identity_public_key_hex=self._identity_public_key_hex,
        )
        if self._secure is not None:
            self._stats.rekeys += 1
            self._ready.clear()
        _logger.info("initiating handshake — channel=%s", self._channel)
        self._emit(ChatEventKind.SESSION, "Establishing secure session…")
        await self._send_frame_quiet(kex_hello_frame(self._initiator.hello_payload()))

    def _set_session(self, secure: SecureSession) -> None:
        self._zeroize()
        self._session_key = bytearray(secure.session_key)
        self._secure = secure
        self._ready.set()
        _logger.info(
            "secure session ready — conversation=%s rekeys=%d",
            secure.conversation_id,
            self._stats.rekeys,
        )
        self._emit(
            ChatEventKind.SESSION,
            f"Encryption: Active — safety code {secure.fingerprint}",
        )
        if secure.identity_bound and self.peer_identity_fingerprint is not None:
            self._emit(
                ChatEventKind.NOTICE,
                f"Peer identity: {self.peer_identity_fingerprint} "
                f"({self._peer_name}) — verify it with /fingerprint",
            )

    def _adopt_peer_name(self, payload: dict[str, object]) -> None:
        candidate = payload.get("name")
        if not isinstance(candidate, str):
            return
        try:
            self._peer_name = validate_display_name(candidate, field="peer name")
        except MessageValidationError:
            _logger.debug("peer sent an unusable display name — keeping default")

    def _adopt_peer_identity(self, payload: dict[str, object]) -> None:
        """Remember the identity public key the peer presented, when valid."""

        candidate = payload.get("idpub")
        if not isinstance(candidate, str):
            return
        try:
            raw = bytes.fromhex(candidate)
        except ValueError:
            _logger.debug("peer sent an unusable identity key — ignoring it")
            return
        if len(raw) != 32:
            _logger.debug("peer sent an unusable identity key — ignoring it")
            return
        self._peer_identity_key_hex = candidate

    @property
    def peer_identity_fingerprint(self) -> str | None:
        """The peer's ``GLFP-…`` fingerprint, when an identity key was shown.

        When :attr:`identity_bound` is true the key is committed to the
        handshake transcript, so a relay cannot swap it without breaking
        key-confirmation.
        """

        if self._peer_identity_key_hex is None:
            return None
        return identity_fingerprint(bytes.fromhex(self._peer_identity_key_hex))

    # --------------------------------------------------------------- inbound

    def _on_forward(self, channel: str, body: str) -> None:
        """Reader-loop callback — schedules safe async handling."""

        if channel != self._channel:
            return
        if self._loop is None or self._closing:
            return
        self._spawn(self._dispatch(body))

    async def _dispatch(self, body: str) -> None:
        try:
            raw = base64.b64decode(body.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError):
            self._stats.frames_dropped += 1
            _logger.debug("inbound body was not base64 — dropped")
            return
        try:
            frame = decode_frame(raw, peer=self._channel)
        except MessageValidationError as exc:
            self._stats.frames_dropped += 1
            _logger.info("inbound frame failed validation — %s", exc.message)
            return

        if frame.type is FrameType.KEX_HELLO:
            await self._handle_kex_hello(frame)
        elif frame.type is FrameType.KEX_REPLY:
            self._handle_kex_reply(frame)
        elif frame.type is FrameType.MESSAGE:
            await self._handle_message(frame)
        elif frame.type is FrameType.MESSAGE_ACK:
            self._handle_ack(frame)
        elif frame.type is FrameType.READ_RECEIPT:
            self._handle_read_receipt(frame)
        elif frame.type is FrameType.TYPING_START:
            if self._config.typing_indicators:
                self._typing.peer_started()
                self._emit(ChatEventKind.TYPING, f"{self._peer_name} is typing…")
        elif frame.type is FrameType.TYPING_STOP:
            if self._config.typing_indicators:
                self._typing.peer_stopped()
                self._emit(ChatEventKind.TYPING, "")
        elif frame.type is FrameType.ERROR:
            code = str(frame.data.get("code", "peer_error"))
            _logger.info("peer reported an error — %s", code)
            self._emit(
                ChatEventKind.NOTICE,
                "The peer reported an encryption problem with a recent "
                "message; it will be resent automatically."
                if code == "decrypt_failed"
                else f"Peer error: {frame.data.get('message', code)}",
            )
        elif frame.type in FILE_FRAME_TYPES:
            await self._dispatch_file_frame(frame)

    async def _dispatch_file_frame(self, frame: Frame) -> None:
        """Hand a validated FILE_* frame to the transfer manager (Phase 4)."""

        delegate = self._transfer_delegate
        if delegate is None:
            self._stats.frames_dropped += 1
            _logger.info("file frame %s without a transfer manager — dropped", frame.type.value)
            return
        try:
            await delegate(frame)
        except Exception as exc:  # the manager must never break the session
            _logger.info("transfer delegate failed for %s — %s", frame.type.value, exc)

    async def _handle_kex_hello(self, frame: Frame) -> None:
        if self._role != "guest":
            self._stats.frames_dropped += 1
            return
        self._adopt_peer_name(frame.data)
        self._adopt_peer_identity(frame.data)
        responder = HandshakeResponder(
            self._channel,
            display_name=self._display_name,
            identity_public_key_hex=self._identity_public_key_hex,
        )
        try:
            reply, secure = responder.answer(frame.data)
        except HandshakeFailedError as exc:
            _logger.info("handshake answer failed — %s", exc.message)
            self._emit(ChatEventKind.NOTICE, f"Secure handshake failed: {exc.message}")
            return
        if self._secure is not None:
            self._stats.rekeys += 1
        await self._send_frame_quiet(kex_reply_frame(reply))
        self._set_session(secure)

    def _handle_kex_reply(self, frame: Frame) -> None:
        initiator = self._initiator
        if initiator is None:
            self._stats.frames_dropped += 1
            return
        self._initiator = None
        try:
            secure = initiator.complete(frame.data)
        except HandshakeFailedError as exc:
            _logger.info("handshake completion failed — %s", exc.message)
            self._emit(
                ChatEventKind.NOTICE,
                f"Secure handshake failed: {exc.message} {exc.hint}",
            )
            return
        self._adopt_peer_name(frame.data)
        self._adopt_peer_identity(frame.data)
        self._set_session(secure)

    async def _handle_message(self, frame: Frame) -> None:
        key = self._key_bytes()
        message_id = str(frame.data["id"])
        if key is None or self._secure is None:
            self._stats.frames_dropped += 1
            _logger.debug("message frame before session ready — dropped")
            return
        aad = message_aad(message_id, frame.sequence)
        try:
            payload = EncryptedPayload.from_b64(str(frame.data["ct"]), aad=aad)
            plaintext = open_sealed(key, payload.sealed, aad)
            text = plaintext.decode("utf-8")
            message = Message(
                message_id=message_id,
                conversation_id=self._secure.conversation_id,
                direction=MessageDirection.INCOMING,
                sender=self._peer_name,
                recipient=self._display_name,
                text=text,
                sequence=frame.sequence,
                status=MessageStatus.DELIVERED,
            )
        except DecryptionError as exc:
            self._stats.decrypt_failures += 1
            _logger.info("inbound message failed integrity check (seq=%d)", frame.sequence)
            self._emit(
                ChatEventKind.NOTICE,
                "A message failed its integrity check and was discarded — "
                "it may have been tampered with in transit.",
            )
            await self._send_frame_quiet(error_frame("decrypt_failed", exc.message[:120]))
            return
        except (UnicodeDecodeError, MessageValidationError) as exc:
            self._stats.frames_dropped += 1
            _logger.info("inbound message rejected — %s", exc)
            await self._send_frame_quiet(error_frame("invalid_message", "message rejected"))
            return

        result = self._inbox.accept(message)
        # ACK every accepted frame (duplicates included) so the peer can stop
        # resending; only genuinely new messages are surfaced or recorded.
        await self._send_frame_quiet(message_ack_frame(message_id, message.sequence))
        self._stats.acks_sent += 1
        if result.verdict is InboxVerdict.DUPLICATE:
            _logger.debug("duplicate inbound message %s — re-acked", message_id)
            return
        for deliverable in result.deliverable:
            self._stats.messages_received += 1
            if self._history is not None:
                self._history.record(deliverable)
            self._emit(ChatEventKind.MESSAGE, message=deliverable)

    def _handle_ack(self, frame: Frame) -> None:
        # The pump owns DELIVERY events: it resolves on the waiter this call
        # completes. Emitting here too would double-report every delivery.
        message = self._outbox.mark_delivered(str(frame.data["id"]))
        if message is not None:
            self._stats.acks_received += 1
            self._receipts.note_ack_received()

    def _handle_read_receipt(self, frame: Frame) -> None:
        cursor = int(frame.data["up_to"])
        self._receipts.note_read_received(cursor)
        for message in self._outbox.mark_read_up_to(cursor):
            self._emit(ChatEventKind.DELIVERY, message=message)

    # ------------------------------------------------------------------ peer + link

    def _on_peer(self, channel: str, event: str) -> None:
        if channel != self._channel or self._closing:
            return
        loop = self._loop
        if loop is None:
            return
        if event == "joined":
            self._peer_present.set()
            self._emit(ChatEventKind.PEER, f"{self._peer_name} joined the conversation")
            if self._role == "host" and loop is not None:
                self._spawn(self._initiate_handshake())
        elif event == "left":
            self._peer_present.clear()
            self._typing.peer_stopped()
            self._emit(ChatEventKind.PEER, f"{self._peer_name} left the conversation")

    def _on_connection_state(self, change: StateChange) -> None:
        if self._closing:
            return
        loop = self._loop
        if change.state is ConnectionState.RECONNECTING:
            self._ready.clear()
            self._transport_usable.clear()
            self._outbox.resolve_ack_waiters_on_disconnect(error="connection lost")
            self._outbox.requeue_unacked()
            self._emit(
                ChatEventKind.CONNECTION,
                "Connection lost — reconnecting…",
            )
        elif change.state is ConnectionState.CONNECTED:
            self._transport_usable.set()
            if loop is not None and self._started:
                # The relay client re-attached the channel; let the wire settle
                # before a fresh handshake (fresh key for the new link).
                self._spawn(self._post_reconnect())
        elif change.state is ConnectionState.DISCONNECTED:
            self._transport_usable.clear()
            self._emit(
                ChatEventKind.CONNECTION,
                "Disconnected from the relay.",
            )
        elif change.state is ConnectionState.CLOSED:
            self._ready.clear()
            self._transport_usable.clear()
            self._emit(ChatEventKind.CONNECTION, "Connection closed.")

    async def _post_reconnect(self) -> None:
        if self._closing:
            return
        await asyncio.sleep(_RECONNECT_SETTLE_SECONDS)
        if self._closing or not self._transport_usable.is_set():
            return
        self._emit(ChatEventKind.CONNECTION, "Reconnected — refreshing encryption…")
        if self._role == "host":
            await self._initiate_handshake()

    # ---------------------------------------------------------------- watchdog

    async def _watchdog(self) -> None:
        """Idle typing stops + inbox starvation relief, once per second."""

        while not self._closing:
            await asyncio.sleep(_WATCHDOG_TICK_SECONDS)
            if (
                self._config.typing_indicators
                and self._typing.idle_elapsed()
                and self._typing.note_sent_or_stopped()
            ):
                await self._send_frame_quiet(typing_stop_frame())
            if self._ready.is_set():
                deliverable = self._inbox.check_starvation()
                for message in deliverable:
                    self._stats.messages_received += 1
                    if self._history is not None:
                        self._history.record(message)
                    self._emit(ChatEventKind.MESSAGE, message=message)

    # ------------------------------------------------------------------ helpers

    def _require_conversation_id(self) -> str:
        conversation_id = self.conversation_id
        if conversation_id is None:
            raise SecureChannelError(
                "The secure session is not ready yet.",
                hint="Wait for the handshake to complete before sending.",
            )
        return conversation_id

    def _key_bytes(self) -> bytes | None:
        if self._session_key is None:
            return None
        return bytes(self._session_key)

    def _zeroize(self) -> None:
        """Best-effort overwrite of key material before releasing it."""

        if self._session_key is not None:
            for index in range(len(self._session_key)):
                self._session_key[index] = 0
            self._session_key = None

    async def _send_frame(self, frame: Frame) -> None:
        body = base64.b64encode(encode_frame(frame)).decode("ascii")
        await self._client.send_forward(self._channel, body)

    async def _send_frame_quiet(self, frame: Frame) -> None:
        """Best-effort send (acks, receipts, typing) — failures are logged."""

        try:
            await self._send_frame(frame)
        except (TransportError, GhostLinkError) as exc:
            _logger.debug("best-effort frame %s was not sent: %s", frame.type.value, exc)

    @staticmethod
    def _observe_task_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        failure = task.exception()
        if failure is not None:
            _logger.debug("background chat task failed: %s", failure)


def chat_config_from_settings(
    *,
    read_receipts: bool = True,
    typing_indicators: bool = True,
) -> ChatSessionConfig:
    """Project the [chat] settings section onto a session config."""

    return ChatSessionConfig(
        read_receipts=read_receipts,
        typing_indicators=typing_indicators,
    )
