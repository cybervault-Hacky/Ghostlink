"""Phase 8 recovery-coordinator tests.

The coordinator guarantees exactly-one in-flight recovery operation per
key (no competing reconnect/resync/install loops) and enforces a closed
state-transition table. These are pure unit tests — no network.
"""

from __future__ import annotations

import pytest

from ghostlink.core.recovery import (
    IllegalRecoveryTransition,
    RecoveryCoordinator,
    RecoveryState,
)


class TestStateMachine:
    def test_initial_state_is_ready(self) -> None:
        coordinator = RecoveryCoordinator()
        assert coordinator.state is RecoveryState.READY

    def test_legal_transition(self) -> None:
        coordinator = RecoveryCoordinator()
        coordinator.transition(RecoveryState.CONNECTED, reason="attach")
        assert coordinator.state is RecoveryState.CONNECTED

    def test_illegal_transition_raises(self) -> None:
        coordinator = RecoveryCoordinator()
        # READY -> RECOVERING is not allowed (must pass through RESYNC_REQUIRED).
        with pytest.raises(IllegalRecoveryTransition):
            coordinator.transition(RecoveryState.RECOVERING)

    def test_recovery_flow(self) -> None:
        coordinator = RecoveryCoordinator()
        coordinator.transition(RecoveryState.CONNECTED)
        coordinator.transition(RecoveryState.DEGRADED, reason="epoch gap")
        coordinator.transition(RecoveryState.RESYNC_REQUIRED, reason="stale")
        coordinator.transition(RecoveryState.RECOVERING)
        coordinator.transition(RecoveryState.READY)

    def test_terminal_closed_cannot_leave(self) -> None:
        coordinator = RecoveryCoordinator()
        coordinator.transition(RecoveryState.CLOSED)
        for state in RecoveryState:
            if state is RecoveryState.CLOSED:
                continue
            with pytest.raises(IllegalRecoveryTransition):
                coordinator.transition(state)

    def test_listener_notified(self) -> None:
        coordinator = RecoveryCoordinator()
        seen: list[tuple[RecoveryState, RecoveryState, str]] = []
        coordinator.add_listener(lambda old, new, reason: seen.append((old, new, reason)))
        coordinator.transition(RecoveryState.CONNECTED, reason="attach")
        assert seen and seen[0][1] is RecoveryState.CONNECTED

    def test_same_state_is_noop(self) -> None:
        coordinator = RecoveryCoordinator()
        assert coordinator.state is RecoveryState.READY
        coordinator.transition(RecoveryState.READY)  # legal, no-op
        assert coordinator.state is RecoveryState.READY


class TestExclusiveLeases:
    def test_exactly_one_in_flight_per_key(self) -> None:
        coordinator = RecoveryCoordinator()
        first = coordinator.begin("resync:g")
        assert first is not None
        assert coordinator.has_in_flight("resync:g")
        # A competing operation for the same key is refused.
        assert coordinator.begin("resync:g") is None
        # A different key is allowed.
        assert coordinator.begin("resync:other") is not None
        assert coordinator.in_flight_count() == 2

    def test_release_permits_next(self) -> None:
        coordinator = RecoveryCoordinator()
        lease = coordinator.begin("resync:g")
        assert lease is not None
        lease.release()
        assert not coordinator.has_in_flight("resync:g")
        assert coordinator.begin("resync:g") is not None

    def test_context_manager_releases(self) -> None:
        coordinator = RecoveryCoordinator()
        with coordinator.begin("resync:g") as lease:
            assert lease is not None and coordinator.has_in_flight("resync:g")
        assert not coordinator.has_in_flight("resync:g")

    def test_double_release_is_safe(self) -> None:
        coordinator = RecoveryCoordinator()
        lease = coordinator.begin("resync:g")
        assert lease is not None
        lease.release()
        lease.release()  # no-op, must not raise
        assert coordinator.in_flight_count() == 0

    def test_reset_clears_leases(self) -> None:
        coordinator = RecoveryCoordinator()
        coordinator.begin("resync:g")
        coordinator.begin("install:g")
        assert coordinator.in_flight_count() == 2
        coordinator.reset()
        assert coordinator.in_flight_count() == 0
        assert coordinator.state is RecoveryState.READY


class TestRaceGuarantee:
    """A simulated race: two coroutines try to resync the same group."""

    def test_no_competing_resync(self) -> None:
        coordinator = RecoveryCoordinator()
        started: list[str] = []

        # While worker-a holds the lease, worker-b must be refused.
        lease_a = coordinator.begin("resync:g")
        assert lease_a is not None
        started.append("worker-a")
        assert coordinator.begin("resync:g") is None
        assert started == ["worker-a"]
        # After release, the next worker can proceed.
        lease_a.release()
        lease_b = coordinator.begin("resync:g")
        assert lease_b is not None
        lease_b.release()

    def test_bounded_memory(self) -> None:
        coordinator = RecoveryCoordinator()
        for index in range(300):
            coordinator.begin(f"op:{index}")
            coordinator.transition(RecoveryState.DEGRADED, reason="tick")
            coordinator.transition(RecoveryState.READY)
        # Transition log is bounded, not unbounded.
        assert len(coordinator._transition_log) <= 256  # type: ignore[attr-defined]
