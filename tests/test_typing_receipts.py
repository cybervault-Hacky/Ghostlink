"""Typing throttling/expiry and receipt policy (Phase 3)."""

from __future__ import annotations

import pytest

from ghostlink.exceptions.messaging import MessageValidationError
from ghostlink.messaging.models.message import (
    Message,
    MessageDirection,
    MessageStatus,
    generate_message_id,
)
from ghostlink.messaging.receipts import ReceiptTracker
from ghostlink.messaging.typing import TypingIndicator

T0 = 1_000.0


class TestTypingOutgoing:
    def test_first_keystroke_emits_start(self) -> None:
        typing = TypingIndicator()
        assert typing.note_keystroke(at=T0) is True

    def test_start_is_throttled_within_interval(self) -> None:
        typing = TypingIndicator(start_interval_seconds=3.0)
        assert typing.note_keystroke(at=T0) is True
        assert typing.note_keystroke(at=T0 + 1.0) is False
        assert typing.note_keystroke(at=T0 + 2.9) is False

    def test_start_repeats_after_interval_while_active(self) -> None:
        typing = TypingIndicator(start_interval_seconds=3.0)
        assert typing.note_keystroke(at=T0) is True
        assert typing.note_keystroke(at=T0 + 3.1) is True  # still typing

    def test_stop_after_send_and_restart(self) -> None:
        typing = TypingIndicator()
        typing.note_keystroke(at=T0)
        assert typing.note_sent_or_stopped() is True
        assert typing.note_sent_or_stopped() is False  # already stopped
        assert typing.note_keystroke(at=T0 + 1.0) is True

    def test_idle_detection(self) -> None:
        typing = TypingIndicator(idle_seconds=4.0)
        typing.note_keystroke(at=T0)
        assert typing.idle_elapsed(at=T0 + 3.9) is False
        assert typing.idle_elapsed(at=T0 + 4.1) is True
        typing.note_sent_or_stopped()
        assert typing.idle_elapsed(at=T0 + 9.0) is False

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"start_interval_seconds": 0},
            {"idle_seconds": -1},
            {"peer_expiry_seconds": 0},
        ],
    )
    def test_nonpositive_timings_rejected(self, kwargs: dict[str, float]) -> None:
        with pytest.raises(MessageValidationError, match="positive"):
            TypingIndicator(**kwargs)


class TestTypingIncoming:
    def test_peer_state_lifecycle(self) -> None:
        typing = TypingIndicator()
        assert typing.peer_typing(at=T0) is False
        typing.peer_started(at=T0)
        assert typing.peer_typing(at=T0) is True
        typing.peer_stopped()
        assert typing.peer_typing(at=T0) is False

    def test_peer_state_expires_without_stop(self) -> None:
        typing = TypingIndicator(peer_expiry_seconds=6.0)
        typing.peer_started(at=T0)
        assert typing.peer_typing(at=T0 + 5.9) is True
        assert typing.peer_typing(at=T0 + 6.1) is False

    def test_reset_clears_everything(self) -> None:
        typing = TypingIndicator()
        typing.note_keystroke(at=T0)
        typing.peer_started(at=T0)
        typing.reset()
        assert typing.peer_typing(at=T0) is False
        assert typing.note_sent_or_stopped() is False


def _message(status: MessageStatus, sequence: int) -> Message:
    return Message(
        message_id=generate_message_id(),
        conversation_id="conv_0123456789ab",
        direction=MessageDirection.OUTGOING,
        sender="Nova",
        recipient="Peer",
        text=f"m{sequence}",
        sequence=sequence,
        status=status,
    )


class TestReceipts:
    def test_read_receipt_policy_coalesces(self) -> None:
        receipts = ReceiptTracker(read_receipts_enabled=True)
        assert receipts.read_to_emit(1) == 1
        receipts.note_read_sent(1)
        assert receipts.read_to_emit(1) is None
        assert receipts.read_to_emit(5) == 5
        receipts.note_read_sent(5)
        assert receipts.read_to_emit(3) is None  # backwards cursor never emits

    def test_disabled_receipts_never_emit(self) -> None:
        receipts = ReceiptTracker(read_receipts_enabled=False)
        assert receipts.read_receipts_enabled is False
        assert receipts.read_to_emit(1) is None

    def test_received_cursor_is_monotonic(self) -> None:
        receipts = ReceiptTracker()
        receipts.note_read_received(4)
        receipts.note_read_received(2)
        assert receipts.last_read_received == 4

    def test_ack_counters(self) -> None:
        receipts = ReceiptTracker()
        receipts.note_ack_sent()
        receipts.note_ack_sent()
        receipts.note_ack_received()
        assert receipts.acks_sent == 2
        assert receipts.acks_received == 1

    def test_delivery_counts(self) -> None:
        messages = [
            _message(MessageStatus.QUEUED, 1),
            _message(MessageStatus.SENT, 2),
            _message(MessageStatus.SENT, 3),
            _message(MessageStatus.READ, 4),
        ]
        counts = ReceiptTracker.delivery_counts(messages)
        assert counts[MessageStatus.SENT] == 2
        assert counts[MessageStatus.QUEUED] == 1
        assert counts[MessageStatus.READ] == 1
        assert counts[MessageStatus.FAILED] == 0
