"""Delivery and read receipt bookkeeping (Phase 3).

Delivery acknowledgements are always on — without them nothing could ever
leave the SENT state. *Read* receipts are a privacy-sensitive courtesy the
user can switch off; this tracker is the single decision point for both:

* outbound read receipts are coalesced — catching up on a backlog emits one
  receipt for the highest read sequence, never one per message
* the last cursors in both directions are remembered so the UI can show
  exactly how far the peer has read
"""

from __future__ import annotations

from ghostlink.messaging.models.message import Message, MessageStatus


class ReceiptTracker:
    """Policy + cursors for delivery ACKs and optional read receipts."""

    def __init__(self, *, read_receipts_enabled: bool = True) -> None:
        self._read_receipts = read_receipts_enabled
        self._last_read_sent = 0
        self._last_read_received = 0
        self._acks_sent = 0
        self._acks_received = 0

    # ------------------------------------------------------------------ policy

    @property
    def read_receipts_enabled(self) -> bool:
        return self._read_receipts

    def note_ack_sent(self) -> None:
        self._acks_sent += 1

    def note_ack_received(self) -> None:
        self._acks_received += 1

    @property
    def acks_sent(self) -> int:
        return self._acks_sent

    @property
    def acks_received(self) -> int:
        return self._acks_received

    # ------------------------------------------------------------ read cursors

    def read_to_emit(self, cursor: int) -> int | None:
        """Decide whether a READ_RECEIPT should be sent for ``cursor``.

        Returns the cursor to send, or ``None`` when receipts are disabled
        or nothing new has been read since the last emission."""

        if not self._read_receipts:
            return None
        if cursor <= self._last_read_sent:
            return None
        return cursor

    def note_read_sent(self, cursor: int) -> None:
        self._last_read_sent = max(self._last_read_sent, cursor)

    def note_read_received(self, cursor: int) -> None:
        self._last_read_received = max(self._last_read_received, cursor)

    @property
    def last_read_received(self) -> int:
        """Highest outgoing sequence the peer has confirmed as read."""

        return self._last_read_received

    # ------------------------------------------------------------------ counts

    @staticmethod
    def delivery_counts(messages: list[Message]) -> dict[MessageStatus, int]:
        """Per-status message counts for the status dashboard."""

        counts: dict[MessageStatus, int] = {status: 0 for status in MessageStatus}
        for message in messages:
            counts[message.status] += 1
        return counts
