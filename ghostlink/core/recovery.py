"""Deterministic recovery coordinator (Phase 8).

GhostLink must behave correctly even when the network, relay, peers, and
delivery order are hostile. A recurring failure class in distributed
systems is **competing recovery loops** — two reconnect tasks, two resyncs,
two sender-key installs or two transfer resumes racing for the same
session/group. This module provides the single authority that serializes
them.

:class:`RecoveryState` enumerates the observable recovery phases a session
or group can be in. :class:`RecoveryCoordinator` enforces:

* one authoritative state with a closed transition table (no illegal
  jumps, deterministic failure);
* **exactly one in-flight operation per key** — :meth:`begin` hands out a
  :class:`RecoveryLease` only when no operation for that key is already
  running, so duplicate reconnect/resync/install tasks are refused, not
  merely throttled.

The coordinator is memory-only, lock-free under asyncio's single-threaded
model, and carries no secrets. Nothing here touches disk or logs key
material.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from enum import Enum


class RecoveryState(str, Enum):
    """The authoritative recovery phase for one session or group.

    Transitions are a subset of the graph below (see
    :class:`RecoveryCoordinator`):

        READY ──▶ CONNECTED ──▶ DEGRADED ──▶ RECONNECTING ──▶ CONNECTED
          │          │            │              │                │
          │          └────────────┴──▶ RESYNC_REQUIRED ──▶ RECOVERING ──▶ READY
          │                                │                  │
          └──────────── FAILED ◀───────────┴──────────────────┘
          └──────────── CLOSED (terminal)
    """

    CONNECTED = "connected"
    DEGRADED = "degraded"
    RECONNECTING = "reconnecting"
    RESYNC_REQUIRED = "resync_required"
    RECOVERING = "recovering"
    READY = "ready"
    FAILED = "failed"
    CLOSED = "closed"


_TERMINAL: frozenset[RecoveryState] = frozenset({RecoveryState.CLOSED})

_ALLOWED_TRANSITIONS: dict[RecoveryState, frozenset[RecoveryState]] = {
    RecoveryState.READY: frozenset(
        {
            RecoveryState.CONNECTED,
            RecoveryState.DEGRADED,
            RecoveryState.RESYNC_REQUIRED,
            RecoveryState.CLOSED,
        }
    ),
    RecoveryState.CONNECTED: frozenset(
        {
            RecoveryState.DEGRADED,
            RecoveryState.RECONNECTING,
            RecoveryState.RESYNC_REQUIRED,
            RecoveryState.READY,
            RecoveryState.CLOSED,
        }
    ),
    RecoveryState.DEGRADED: frozenset(
        {
            RecoveryState.CONNECTED,
            RecoveryState.RECONNECTING,
            RecoveryState.RESYNC_REQUIRED,
            RecoveryState.RECOVERING,
            RecoveryState.READY,
            RecoveryState.FAILED,
            RecoveryState.CLOSED,
        }
    ),
    RecoveryState.RECONNECTING: frozenset(
        {
            RecoveryState.CONNECTED,
            RecoveryState.DEGRADED,
            RecoveryState.FAILED,
            RecoveryState.CLOSED,
        }
    ),
    RecoveryState.RESYNC_REQUIRED: frozenset(
        {
            RecoveryState.RECOVERING,
            RecoveryState.READY,
            RecoveryState.FAILED,
            RecoveryState.CLOSED,
        }
    ),
    RecoveryState.RECOVERING: frozenset(
        {RecoveryState.READY, RecoveryState.DEGRADED, RecoveryState.FAILED, RecoveryState.CLOSED}
    ),
    RecoveryState.FAILED: frozenset(
        {
            RecoveryState.RECONNECTING,
            RecoveryState.RESYNC_REQUIRED,
            RecoveryState.READY,
            RecoveryState.CLOSED,
        }
    ),
    RecoveryState.CLOSED: frozenset(),  # terminal
}


class IllegalRecoveryTransition(Exception):
    """Raised on a state change that the transition table forbids."""


class RecoveryLease:
    """An exclusive lease on one recovery operation; release to finish.

    Use as a context manager::

        lease = coordinator.begin("resync:gl-group-…")
        if lease is None:
            return  # another resync for this key is already running
        try:
            ... recover ...
        finally:
            lease.release()
    """

    __slots__ = ("_active", "_coordinator", "op_key")

    def __init__(self, coordinator: RecoveryCoordinator, op_key: str) -> None:
        self._coordinator = coordinator
        self.op_key = op_key
        self._active = True

    def release(self) -> None:
        if self._active:
            self._active = False
            self._coordinator._release(self.op_key)

    def __enter__(self) -> RecoveryLease:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.release()

    @property
    def active(self) -> bool:
        return self._active


class RecoveryCoordinator:
    """One authoritative recovery state + exactly-one in-flight ops.

    Not thread-safe by design: asyncio coroutines run on a single thread,
    and every mutation happens synchronously between ``await`` points, so
    the in-flight set needs no lock. If called from multiple threads,
    guard access externally.
    """

    def __init__(
        self,
        *,
        initial: RecoveryState = RecoveryState.READY,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._monotonic = monotonic
        self._state = initial
        self._in_flight: set[str] = set()
        self._transition_log: list[tuple[float, RecoveryState, str]] = []
        self._listeners: list[Callable[[RecoveryState, RecoveryState, str], None]] = []

    # ------------------------------------------------------------ state

    @property
    def state(self) -> RecoveryState:
        return self._state

    def can_transition(self, new_state: RecoveryState) -> bool:
        return new_state in _ALLOWED_TRANSITIONS[self._state]

    def transition(self, new_state: RecoveryState, *, reason: str = "") -> RecoveryState:
        """Advance to ``new_state`` if legal; raise ``IllegalRecoveryTransition``."""
        previous = self._state
        if previous is new_state:
            return self._state  # re-entrant/no-op transition is always safe
        allowed = _ALLOWED_TRANSITIONS[self._state]
        if new_state not in allowed:
            raise IllegalRecoveryTransition(
                f"Illegal recovery transition {self._state.value} → {new_state.value}."
            )
        self._state = new_state
        self._transition_log.append((self._monotonic(), new_state, reason))
        if len(self._transition_log) > 256:  # bounded memory
            del self._transition_log[: len(self._transition_log) - 256]
        for listener in self._listeners:
            try:
                listener(previous, new_state, reason)
            except Exception:
                continue
        return self._state

    def add_listener(self, listener: Callable[[RecoveryState, RecoveryState, str], None]) -> None:
        self._listeners.append(listener)

    # -------------------------------------------------------- in-flight ops

    def begin(self, op_key: str) -> RecoveryLease | None:
        """Acquire the exclusive lease for ``op_key``, or None if held."""
        if op_key in self._in_flight:
            return None
        self._in_flight.add(op_key)
        return RecoveryLease(self, op_key)

    def _release(self, op_key: str) -> None:
        self._in_flight.discard(op_key)

    def has_in_flight(self, op_key: str) -> bool:
        return op_key in self._in_flight

    def in_flight_count(self) -> int:
        return len(self._in_flight)

    def in_flight_ops(self) -> frozenset[str]:
        return frozenset(self._in_flight)

    def reset(self) -> None:
        """Clear all leases and return to READY (e.g. after full reattach)."""
        self._in_flight.clear()
        self._state = RecoveryState.READY


__all__ = [
    "IllegalRecoveryTransition",
    "RecoveryCoordinator",
    "RecoveryLease",
    "RecoveryState",
]
