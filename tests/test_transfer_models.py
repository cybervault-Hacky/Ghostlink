"""Transfer domain model: ids, lifecycle transitions, snapshots (Phase 4)."""

from __future__ import annotations

import pytest

from ghostlink.exceptions.transfer import TransferStateError
from ghostlink.transfer.models import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    Transfer,
    TransferDirection,
    TransferState,
    generate_transfer_id,
    is_valid_transfer_id,
)


def _transfer(
    *,
    direction: TransferDirection = TransferDirection.SENDING,
    state: TransferState = TransferState.OFFERED,
) -> Transfer:
    return Transfer(
        transfer_id="tf_ab12cd34",
        direction=direction,
        state=state,
        filename="report.pdf",
        size_bytes=10_000,
        mime="application/pdf",
        chunk_size=1024,
        total_chunks=10,
        peer="Ravi",
        integrity="ab" * 32,
    )


class TestTransferIds:
    def test_generate_transfer_id_shape(self) -> None:
        transfer_id = generate_transfer_id()
        assert transfer_id.startswith("tf_")
        assert len(transfer_id) == 11
        assert is_valid_transfer_id(transfer_id)

    def test_ids_are_unique(self) -> None:
        assert len({generate_transfer_id() for _ in range(200)}) == 200

    @pytest.mark.parametrize(
        "candidate",
        [
            "",
            "ab12cd34",
            "tf_",
            "tf_ab12cd3",  # too short
            "tf_ab12cd345",  # too long
            "tf_AB12CD34",  # uppercase
            "tf_ab12cd3g",  # not hex
            "tf_ab12cd3/",  # separator injection
            "tf_../..",
        ],
    )
    def test_invalid_ids_rejected(self, candidate: str) -> None:
        assert not is_valid_transfer_id(candidate)

    def test_malformed_id_rejected_at_construction(self) -> None:
        transfer = _transfer()
        transfer.transfer_id = "bogus"
        with pytest.raises(TransferStateError):
            transfer.__post_init__()


class TestLifecycle:
    def test_sender_happy_path(self) -> None:
        transfer = _transfer(state=TransferState.QUEUED)
        transfer.transition_to(TransferState.OFFERED)
        transfer.transition_to(TransferState.ACCEPTED)
        transfer.transition_to(TransferState.TRANSFERRING)
        transfer.transition_to(TransferState.PAUSED)
        transfer.transition_to(TransferState.TRANSFERRING)
        transfer.transition_to(TransferState.COMPLETED)
        assert transfer.is_terminal

    def test_receiver_happy_path(self) -> None:
        transfer = _transfer(direction=TransferDirection.RECEIVING)
        transfer.transition_to(TransferState.ACCEPTED)
        transfer.transition_to(TransferState.TRANSFERRING)
        transfer.transition_to(TransferState.COMPLETED)
        assert transfer.is_terminal

    @pytest.mark.parametrize(
        ("initial", "forbidden"),
        [
            (TransferState.QUEUED, TransferState.TRANSFERRING),
            (TransferState.QUEUED, TransferState.COMPLETED),
            (TransferState.OFFERED, TransferState.TRANSFERRING),
            (TransferState.OFFERED, TransferState.COMPLETED),
            (TransferState.OFFERED, TransferState.PAUSED),
            (TransferState.ACCEPTED, TransferState.OFFERED),
            (TransferState.ACCEPTED, TransferState.PAUSED),
            (TransferState.ACCEPTED, TransferState.COMPLETED),
            (TransferState.TRANSFERRING, TransferState.OFFERED),
            (TransferState.TRANSFERRING, TransferState.ACCEPTED),
            (TransferState.PAUSED, TransferState.OFFERED),
            (TransferState.PAUSED, TransferState.COMPLETED),
        ],
    )
    def test_invalid_sender_transitions_rejected(
        self, initial: TransferState, forbidden: TransferState
    ) -> None:
        transfer = _transfer(state=initial)
        with pytest.raises(TransferStateError, match="cannot move"):
            transfer.transition_to(forbidden)

    @pytest.mark.parametrize("terminal", sorted(TERMINAL_STATES, key=str))
    def test_terminal_states_are_final(self, terminal: TransferState) -> None:
        transfer = _transfer(state=terminal)
        for target in TransferState:
            with pytest.raises(TransferStateError):
                transfer.transition_to(target)

    def test_receiver_cannot_queue_or_resume_from_offered(self) -> None:
        transfer = _transfer(direction=TransferDirection.RECEIVING)
        with pytest.raises(TransferStateError):
            transfer.transition_to(TransferState.TRANSFERRING)
        with pytest.raises(TransferStateError):
            transfer.transition_to(TransferState.QUEUED)

    def test_transition_records_error_and_activity(self) -> None:
        transfer = _transfer(state=TransferState.TRANSFERRING)
        before = transfer.last_activity_at
        transfer.transition_to(TransferState.FAILED, error="chunk 9 failed verification")
        assert transfer.error == "chunk 9 failed verification"
        assert transfer.last_activity_at >= before

    def test_state_sets_partition_correctly(self) -> None:
        assert TERMINAL_STATES.isdisjoint(ACTIVE_STATES)
        assert {
            TransferState.REJECTED,
            TransferState.COMPLETED,
            TransferState.CANCELLED,
            TransferState.FAILED,
            TransferState.EXPIRED,
        } == set(TERMINAL_STATES)


class TestSnapshots:
    def test_snapshot_is_immutable_and_copies_state(self) -> None:
        transfer = _transfer(state=TransferState.TRANSFERRING)
        transfer.chunks_done = 4
        snapshot = transfer.snapshot()
        transfer.chunks_done = 9
        assert snapshot.chunks_done == 4
        with pytest.raises(AttributeError):
            snapshot.chunks_done = 5  # type: ignore[misc]

    def test_progress_clamped(self) -> None:
        transfer = _transfer(state=TransferState.TRANSFERRING)
        transfer.bytes_done = 4_096
        assert transfer.snapshot().progress == pytest.approx(0.4096)
        transfer.bytes_done = 99_999
        assert transfer.snapshot().progress == 1.0

    def test_empty_progress_is_zero(self) -> None:
        transfer = _transfer()
        assert transfer.snapshot().progress == 0.0

    def test_redacted_log_line_has_no_sensitive_data(self) -> None:
        transfer = _transfer(state=TransferState.TRANSFERRING)
        line = transfer.redacted_for_log()
        assert "tf_ab12cd34" in line
        assert "transferring" in line
        assert "report.pdf" not in line  # filename stays out of the redaction
        assert transfer.integrity not in line

    def test_construction_validates_positive_geometry(self) -> None:
        _transfer()  # valid baseline constructs without complaint
        transfer = _transfer()
        transfer.size_bytes = 0
        with pytest.raises(TransferStateError):
            transfer.__post_init__()
        starving = _transfer()
        starving.chunk_size = 0
        with pytest.raises(TransferStateError):
            starving.__post_init__()
