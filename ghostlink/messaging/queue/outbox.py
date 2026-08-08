"""Outgoing message queue (Phase 3).

The outbox owns the outgoing message lifecycle: FIFO order for the send
pump, strict status transitions, duplicate-id protection, waiters for
delivery acknowledgements, and re-queueing of unacknowledged messages when
the connection drops (a fresh session key re-seals them before resend).

Nothing here performs I/O — the chat session drives it.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Iterator

from ghostlink.core.logging import get_logger
from ghostlink.exceptions.messaging import MessageValidationError
from ghostlink.messaging.models.message import Message, MessageStatus

_logger = get_logger("messaging.outbox")


class Outbox:
    """Ordered queue of outgoing messages with delivery waiters."""

    def __init__(self) -> None:
        self._queued: deque[str] = deque()
        self._messages: dict[str, Message] = {}
        self._ack_waiters: dict[str, asyncio.Future[Message]] = {}
        self._delivered_sequences: set[int] = set()

    # -------------------------------------------------------------- inspection

    def __len__(self) -> int:
        return len(self._messages)

    def __iter__(self) -> Iterator[Message]:
        return iter(self._messages.values())

    @property
    def queued_count(self) -> int:
        return len(self._queued)

    @property
    def highest_delivered_sequence(self) -> int:
        return max(self._delivered_sequences, default=0)

    def get(self, message_id: str) -> Message | None:
        return self._messages.get(message_id)

    def pending(self) -> list[Message]:
        """Messages not yet in a terminal state (QUEUED/SENDING/SENT)."""

        return [
            message
            for message in self._messages.values()
            if message.status in (MessageStatus.QUEUED, MessageStatus.SENDING, MessageStatus.SENT)
        ]

    # ----------------------------------------------------------------- enqueue

    def enqueue(self, message: Message) -> None:
        """Add a QUEUED message; duplicate ids are rejected loudly."""

        if message.message_id in self._messages:
            raise MessageValidationError(
                f"Duplicate outgoing message id {message.message_id}.",
                hint="Message identifiers are generated uniquely per message.",
            )
        if message.status is not MessageStatus.QUEUED:
            raise MessageValidationError(
                "Only queued messages can enter the outbox.",
                hint="Create outgoing messages with status QUEUED.",
            )
        self._messages[message.message_id] = message
        self._queued.append(message.message_id)

    # ------------------------------------------------------------- lifecycle

    def next_queued(self) -> Message | None:
        """Pop the oldest queued message and move it to SENDING."""

        while self._queued:
            message_id = self._queued.popleft()
            message = self._messages[message_id]
            if message.status is MessageStatus.QUEUED:
                message = message.with_status(MessageStatus.SENDING)
                self._messages[message_id] = message
                return message
            # Requeued messages keep one queue slot; stale slots are skipped.
        return None

    def restore(self, message: Message) -> Message:
        """Roll a SENDING message straight back to the head of the queue.

        Used when the connection drops between dequeue and send."""

        if message.status is not MessageStatus.SENDING:
            raise MessageValidationError(
                "Only sending messages can be restored to the outbox.",
                hint="Lifecycle states advance through next_queued().",
            )
        updated = message.with_status(MessageStatus.QUEUED)
        self._messages[message.message_id] = updated
        self._queued.appendleft(message.message_id)
        return updated

    def to_queued(self, message_id: str) -> Message | None:
        """Roll an in-flight message (SENDING/SENT) back to QUEUED."""

        message = self._messages.get(message_id)
        if message is None:
            return None
        if message.status in (MessageStatus.SENDING, MessageStatus.SENT):
            message = message.with_status(MessageStatus.QUEUED)
            self._messages[message_id] = message
            self._queued.appendleft(message_id)
            return message
        return None

    def requeue_unacked(self) -> list[Message]:
        """Roll every non-terminal message back to QUEUED (reconnect path).

        The queue is rebuilt in ascending sequence order so the pump resends
        the backlog exactly in the order the peer expects it."""

        pending_sorted = sorted(self.pending(), key=lambda msg: msg.sequence)
        requeued: list[Message] = []
        rebuilt: deque[str] = deque()
        for pending_message in pending_sorted:
            message = pending_message
            if message.status is not MessageStatus.QUEUED:
                message = message.with_status(MessageStatus.QUEUED)
                self._messages[message.message_id] = message
            requeued.append(message)
            rebuilt.append(message.message_id)
        self._queued = rebuilt
        if requeued:
            _logger.info("requeued %d unacknowledged message(s)", len(requeued))
        return requeued

    def mark_sent(self, message_id: str) -> Message | None:
        message = self._messages.get(message_id)
        # Tolerate the ack-before-mark race: DELIVERED/READ already advanced.
        if message is None or message.status is not MessageStatus.SENDING:
            return message
        return self._transition(message_id, MessageStatus.SENT)

    def mark_delivered(self, message_id: str) -> Message | None:
        """Record a peer delivery acknowledgement; resolves any waiter."""

        message = self._messages.get(message_id)
        if message is None:
            return None
        if message.status is MessageStatus.READ or message.status is MessageStatus.DELIVERED:
            return message  # duplicate acknowledgement
        if message.is_terminal:
            return None
        if message.status is MessageStatus.SENDING:
            # The ack beat mark_sent — chain through the lifecycle in order.
            message = message.with_status(MessageStatus.SENT)
            self._messages[message_id] = message
        message = self._transition(message_id, MessageStatus.DELIVERED)
        if message is not None:
            self._delivered_sequences.add(message.sequence)
            waiter = self._ack_waiters.pop(message_id, None)
            if waiter is not None and not waiter.done():
                waiter.set_result(message)
        return message

    def mark_read_up_to(self, sequence: int) -> list[Message]:
        """Advance all delivered messages with seq ≤ ``sequence`` to READ."""

        updated: list[Message] = []
        for message in list(self._messages.values()):
            if message.sequence <= sequence and message.status is MessageStatus.DELIVERED:
                updated.append(self._transition(message.message_id, MessageStatus.READ) or message)
        return updated

    def mark_failed(self, message_id: str, *, error: str) -> Message | None:
        message = self._transition(message_id, MessageStatus.FAILED, error=error)
        if message is not None:
            waiter = self._ack_waiters.pop(message_id, None)
            if waiter is not None and not waiter.done():
                waiter.set_result(message)
        return message

    def _transition(
        self, message_id: str, status: MessageStatus, *, error: str | None = None
    ) -> Message | None:
        message = self._messages.get(message_id)
        if message is None or message.is_terminal:
            return None
        try:
            updated = message.with_status(status, error=error)
        except MessageValidationError:
            return None
        self._messages[message_id] = updated
        return updated

    # ------------------------------------------------------------------ acks

    def register_ack_waiter(self, message_id: str, future: asyncio.Future[Message]) -> None:
        self._ack_waiters[message_id] = future

    def drop_ack_waiter(self, message_id: str) -> None:
        """Remove a waiter after timeout/cancel (a fresh attempt re-arms)."""

        waiter = self._ack_waiters.pop(message_id, None)
        if waiter is not None and not waiter.done():
            waiter.cancel()

    def resolve_ack_waiters_on_disconnect(self, *, error: str) -> None:
        """Fail waiters whose transport vanished; messages requeue next."""

        for message_id, waiter in list(self._ack_waiters.items()):
            if not waiter.done():
                waiter.cancel()
            self._ack_waiters.pop(message_id, None)
        del error  # documented by callers; waiters re-arm after resend

    # ----------------------------------------------------------------- cleanup

    def clear(self) -> None:
        """Drop all state; cancelled waiters are released first."""

        for waiter in self._ack_waiters.values():
            if not waiter.done():
                waiter.cancel()
        self._ack_waiters.clear()
        self._queued.clear()
        self._messages.clear()
        self._delivered_sequences.clear()
