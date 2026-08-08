"""Incoming message queue (Phase 3).

The inbox turns the peer's sequence numbers into a strictly ordered,
duplicate-free stream of deliverable messages:

* messages arrive with the sender's monotonically increasing sequence
* duplicates (by id, or by sequence already consumed) are dropped — the
  caller still ACKs them so the peer stops resending
* gaps open a small reorder buffer; the buffer self-heals because peers
  resend unacknowledged messages in ascending order, and a starvation
  timeout guarantees progress even after unrecoverable loss
* a bounded LRU of seen ids keeps duplicate detection honest across
  sequence rewinds
"""

from __future__ import annotations

import time
from collections import OrderedDict
from collections.abc import Iterator
from dataclasses import dataclass

from ghostlink.core.logging import get_logger
from ghostlink.messaging.models.message import Message

_logger = get_logger("messaging.inbox")

MAX_REORDER_BUFFER: int = 100
DEFAULT_REORDER_WINDOW_SECONDS: float = 5.0
MAX_SEEN_IDS: int = 4096


class InboxVerdict:
    """What :meth:`Inbox.accept` decided about one inbound message."""

    DELIVER = "deliver"  # brand new, in order → delivered now
    BUFFERED = "buffered"  # brand new, out of order → held in the buffer
    DUPLICATE = "duplicate"  # seen before → drop, but ACK again


@dataclass(frozen=True, slots=True)
class InboxResult:
    verdict: str
    deliverable: tuple[Message, ...]  # may hold drained buffered messages


class Inbox:
    """Ordering + duplicate detection for one incoming message stream."""

    def __init__(self, *, reorder_window_seconds: float = DEFAULT_REORDER_WINDOW_SECONDS) -> None:
        if reorder_window_seconds <= 0:
            raise ValueError("reorder window must be positive")
        self._reorder_window = reorder_window_seconds
        self._expected_sequence = 1
        self._buffer: dict[int, tuple[Message, float]] = {}
        self._seen_ids: OrderedDict[str, None] = OrderedDict()
        self._delivered: list[Message] = []
        self._unread_count = 0
        self._highest_read_sequence = 0

    # -------------------------------------------------------------- inspection

    def __len__(self) -> int:
        return len(self._delivered)

    def __iter__(self) -> Iterator[Message]:
        return iter(self._delivered)

    @property
    def expected_sequence(self) -> int:
        return self._expected_sequence

    @property
    def buffered_count(self) -> int:
        return len(self._buffer)

    @property
    def unread_count(self) -> int:
        return self._unread_count

    @property
    def highest_read_sequence(self) -> int:
        return self._highest_read_sequence

    @property
    def highest_sequence_seen(self) -> int:
        highest = self._expected_sequence - 1
        if self._buffer:
            highest = max(highest, max(self._buffer))
        return highest

    def delivered(self) -> list[Message]:
        return list(self._delivered)

    # ----------------------------------------------------------------- accept

    def accept(self, message: Message) -> InboxResult:
        """Classify one decrypted inbound message and maybe deliver it."""

        if message.message_id in self._seen_ids:
            return InboxResult(InboxVerdict.DUPLICATE, ())
        if message.sequence < self._expected_sequence:
            self._remember(message.message_id)
            return InboxResult(InboxVerdict.DUPLICATE, ())

        self._remember(message.message_id)
        if message.sequence == self._expected_sequence:
            deliverable = [message]
            self._expected_sequence += 1
            deliverable.extend(self._drain_contiguous())
            self._mark_delivered(deliverable)
            return InboxResult(InboxVerdict.DELIVER, tuple(deliverable))

        if len(self._buffer) >= MAX_REORDER_BUFFER:
            # Cap reached: jump to the oldest buffered message so progress
            # is guaranteed; the jump is logged, never silent.
            _logger.info(
                "reorder buffer full — fast-forwarding from seq %d", self._expected_sequence
            )
            return self._force_progress(message)
        self._buffer[message.sequence] = (message, time.monotonic())
        return InboxResult(InboxVerdict.BUFFERED, ())

    def check_starvation(self) -> tuple[Message, ...]:
        """Deliver buffered messages whose wait exceeded the reorder window."""

        if not self._buffer:
            return ()
        oldest_sequence, (_, arrived) = min(self._buffer.items())
        if time.monotonic() - arrived < self._reorder_window:
            return ()
        _logger.info(
            "reorder window elapsed — accepting seq %d (expected %d)",
            oldest_sequence,
            self._expected_sequence,
        )
        self._expected_sequence = max(self._expected_sequence, oldest_sequence)
        drained = self._drain_contiguous()
        self._mark_delivered(drained)
        return tuple(drained)

    def _force_progress(self, message: Message) -> InboxResult:
        self._buffer[message.sequence] = (message, time.monotonic())
        self._expected_sequence = min(self._buffer)
        drained = self._drain_contiguous()
        self._mark_delivered(drained)
        return InboxResult(InboxVerdict.DELIVER, tuple(drained))

    def _drain_contiguous(self) -> list[Message]:
        drained: list[Message] = []
        while self._expected_sequence in self._buffer:
            message, _ = self._buffer.pop(self._expected_sequence)
            drained.append(message)
            self._expected_sequence += 1
        return drained

    def _mark_delivered(self, messages: list[Message]) -> None:
        self._delivered.extend(messages)
        self._unread_count += len(messages)

    def _remember(self, message_id: str) -> None:
        self._seen_ids[message_id] = None
        self._seen_ids.move_to_end(message_id)
        while len(self._seen_ids) > MAX_SEEN_IDS:
            self._seen_ids.popitem(last=False)

    # -------------------------------------------------------------------- read

    def mark_all_read(self) -> int:
        """Mark everything delivered as read; returns the read cursor."""

        self._unread_count = 0
        self._highest_read_sequence = max(self._highest_read_sequence, self.highest_sequence_seen)
        return self._highest_read_sequence

    # ----------------------------------------------------------------- cleanup

    def clear(self) -> None:
        self._buffer.clear()
        self._seen_ids.clear()
        self._delivered.clear()
        self._unread_count = 0
        self._highest_read_sequence = 0
        self._expected_sequence = 1
