"""Outbox and inbox: ordering, lifecycle, duplicates, waiters."""

from __future__ import annotations

import asyncio

import pytest

from ghostlink.exceptions.messaging import MessageValidationError
from ghostlink.messaging.models.message import (
    Message,
    MessageDirection,
    MessageStatus,
    generate_message_id,
)
from ghostlink.messaging.queue.inbox import (
    MAX_REORDER_BUFFER,
    Inbox,
    InboxVerdict,
)
from ghostlink.messaging.queue.outbox import Outbox

CONV = "conv_0123456789ab"


def outgoing(sequence: int, **overrides: object) -> Message:
    fields: dict[str, object] = {
        "message_id": generate_message_id(),
        "conversation_id": CONV,
        "direction": MessageDirection.OUTGOING,
        "sender": "Nova",
        "recipient": "Peer",
        "text": f"out {sequence}",
        "sequence": sequence,
    }
    fields.update(overrides)
    return Message(**fields)  # type: ignore[arg-type]


def incoming(sequence: int, **overrides: object) -> Message:
    fields: dict[str, object] = {
        "message_id": generate_message_id(),
        "conversation_id": CONV,
        "direction": MessageDirection.INCOMING,
        "sender": "Peer",
        "recipient": "Nova",
        "text": f"in {sequence}",
        "sequence": sequence,
        "status": MessageStatus.DELIVERED,
    }
    fields.update(overrides)
    return Message(**fields)  # type: ignore[arg-type]


class TestOutbox:
    def test_fifo_with_sending_transition(self) -> None:
        outbox = Outbox()
        first, second = outgoing(1), outgoing(2)
        outbox.enqueue(first)
        outbox.enqueue(second)
        popped = outbox.next_queued()
        assert popped is not None and popped.message_id == first.message_id
        assert popped.status is MessageStatus.SENDING
        popped = outbox.next_queued()
        assert popped is not None and popped.message_id == second.message_id
        assert outbox.next_queued() is None
        assert outbox.queued_count == 0

    def test_duplicate_id_rejected(self) -> None:
        outbox = Outbox()
        message = outgoing(1)
        outbox.enqueue(message)
        with pytest.raises(MessageValidationError, match="Duplicate"):
            outbox.enqueue(message)

    def test_only_queued_enters(self) -> None:
        outbox = Outbox()
        with pytest.raises(MessageValidationError, match="queued"):
            outbox.enqueue(outgoing(1, status=MessageStatus.SENDING))

    def test_full_lifecycle(self) -> None:
        outbox = Outbox()
        message = outgoing(1)
        outbox.enqueue(message)
        popped = outbox.next_queued()
        assert popped is not None
        sent = outbox.mark_sent(popped.message_id)
        assert sent is not None and sent.status is MessageStatus.SENT
        delivered = outbox.mark_delivered(popped.message_id)
        assert delivered is not None and delivered.status is MessageStatus.DELIVERED
        assert outbox.highest_delivered_sequence == 1
        read = outbox.mark_read_up_to(1)
        assert [m.status for m in read] == [MessageStatus.READ]

    def test_ack_before_mark_sent_chains_through(self) -> None:
        outbox = Outbox()
        message = outgoing(1)
        outbox.enqueue(message)
        popped = outbox.next_queued()
        assert popped is not None
        delivered = outbox.mark_delivered(popped.message_id)
        assert delivered is not None and delivered.status is MessageStatus.DELIVERED
        again = outbox.mark_sent(popped.message_id)
        assert again is not None and again.status is MessageStatus.DELIVERED

    def test_restore_only_for_sending(self) -> None:
        outbox = Outbox()
        message = outgoing(1)
        outbox.enqueue(message)
        popped = outbox.next_queued()
        assert popped is not None
        outbox.restore(popped)
        again = outbox.next_queued()
        assert again is not None and again.message_id == message.message_id

    def test_requeue_unacked_rolls_back_in_order(self) -> None:
        outbox = Outbox()
        one, two, three = outgoing(1), outgoing(2), outgoing(3)
        for message in (one, two, three):
            outbox.enqueue(message)
        first = outbox.next_queued()
        assert first is not None
        outbox.mark_sent(first.message_id)
        second = outbox.next_queued()
        assert second is not None
        requeued = outbox.requeue_unacked()
        statuses = {m.sequence: m.status for m in requeued}
        expected = {1: MessageStatus.QUEUED, 2: MessageStatus.QUEUED, 3: MessageStatus.QUEUED}
        assert statuses == expected
        order = []
        while True:
            popped = outbox.next_queued()
            if popped is None:
                break
            order.append(popped.sequence)
        assert order == [1, 2, 3]

    def test_delivered_messages_not_requeued(self) -> None:
        outbox = Outbox()
        message = outgoing(1)
        outbox.enqueue(message)
        popped = outbox.next_queued()
        assert popped is not None
        outbox.mark_sent(popped.message_id)
        outbox.mark_delivered(popped.message_id)
        assert outbox.requeue_unacked() == []

    def test_mark_failed_resolves_waiter(self) -> None:
        outbox = Outbox()
        message = outgoing(1)
        outbox.enqueue(message)
        popped = outbox.next_queued()
        assert popped is not None
        loop = asyncio.new_event_loop()
        try:
            future = loop.create_future()
            outbox.register_ack_waiter(popped.message_id, future)
            failed = outbox.mark_failed(popped.message_id, error="boom")
            assert failed is not None and failed.status is MessageStatus.FAILED
            assert future.done() and future.result().status is MessageStatus.FAILED
        finally:
            loop.close()

    def test_delivered_waiter_resolves(self) -> None:
        outbox = Outbox()
        message = outgoing(1)
        outbox.enqueue(message)
        popped = outbox.next_queued()
        assert popped is not None
        loop = asyncio.new_event_loop()
        try:
            future = loop.create_future()
            outbox.register_ack_waiter(popped.message_id, future)
            outbox.mark_delivered(popped.message_id)
            assert future.result().status is MessageStatus.DELIVERED
        finally:
            loop.close()

    def test_clear_cancels_waiters(self) -> None:
        outbox = Outbox()
        message = outgoing(1)
        outbox.enqueue(message)
        popped = outbox.next_queued()
        assert popped is not None
        loop = asyncio.new_event_loop()
        try:
            future = loop.create_future()
            outbox.register_ack_waiter(popped.message_id, future)
            outbox.clear()
            assert future.cancelled()
            assert len(outbox) == 0
        finally:
            loop.close()


class TestInbox:
    def test_in_order_delivery(self) -> None:
        inbox = Inbox()
        result = inbox.accept(incoming(1))
        assert result.verdict == InboxVerdict.DELIVER
        assert [m.sequence for m in result.deliverable] == [1]
        assert inbox.unread_count == 1
        assert inbox.expected_sequence == 2

    def test_duplicate_by_sequence_dropped(self) -> None:
        inbox = Inbox()
        inbox.accept(incoming(1))
        result = inbox.accept(incoming(1))
        assert result.verdict == InboxVerdict.DUPLICATE
        assert result.deliverable == ()

    def test_duplicate_by_id_dropped_even_with_future_seq(self) -> None:
        inbox = Inbox()
        message = incoming(1)
        inbox.accept(message)
        result = inbox.accept(message)
        assert result.verdict == InboxVerdict.DUPLICATE

    def test_out_of_order_buffers_then_drains(self) -> None:
        inbox = Inbox()
        buffered = inbox.accept(incoming(3))
        assert buffered.verdict == InboxVerdict.BUFFERED
        assert inbox.accept(incoming(2)).verdict == InboxVerdict.BUFFERED
        result = inbox.accept(incoming(1))
        assert result.verdict == InboxVerdict.DELIVER
        assert [m.sequence for m in result.deliverable] == [1, 2, 3]
        assert inbox.expected_sequence == 4

    def test_buffer_cap_forces_progress(self) -> None:
        inbox = Inbox()
        last = None
        for sequence in range(2, MAX_REORDER_BUFFER + 2):
            last = inbox.accept(incoming(sequence))
        assert last is not None and last.verdict == InboxVerdict.BUFFERED
        forced = inbox.accept(incoming(MAX_REORDER_BUFFER + 2))
        assert forced.verdict == InboxVerdict.DELIVER
        sequences = [m.sequence for m in forced.deliverable]
        assert sequences == sorted(sequences)
        assert sequences[0] == 2

    def test_starvation_timeout_activates_buffer(self) -> None:
        inbox = Inbox(reorder_window_seconds=0.01)
        import time

        inbox.accept(incoming(2))
        assert inbox.check_starvation() == ()
        time.sleep(0.02)
        deliverable = inbox.check_starvation()
        assert [m.sequence for m in deliverable] == [2]
        assert inbox.expected_sequence == 3

    def test_unread_counter_and_read_cursor(self) -> None:
        inbox = Inbox()
        inbox.accept(incoming(1))
        inbox.accept(incoming(2))
        assert inbox.unread_count == 2
        cursor = inbox.mark_all_read()
        assert cursor == 2
        assert inbox.unread_count == 0
        assert inbox.highest_read_sequence == 2

    def test_clear_resets_everything(self) -> None:
        inbox = Inbox()
        inbox.accept(incoming(1))
        inbox.clear()
        assert inbox.expected_sequence == 1
        assert inbox.unread_count == 0
        assert len(inbox) == 0

    def test_invalid_window_rejected(self) -> None:
        with pytest.raises(ValueError, match="positive"):
            Inbox(reorder_window_seconds=0)
