"""Transfer domain model (Phase 4).

One :class:`Transfer` tracks a single file moving in one direction, end to
end. The lifecycle is explicit and validated like the message lifecycle::

    sender:   QUEUED → OFFERED → ACCEPTED → TRANSFERRING → COMPLETED
    receiver:          OFFERED → ACCEPTED → TRANSFERRING → COMPLETED

    any active state → PAUSED ⇄ TRANSFERRING
    any active state → REJECTED / CANCELLED / FAILED / EXPIRED  (terminal)

Everything UI or events need is exposed as immutable :class:`TransferSnapshot`
copies, so the manager's mutable bookkeeping never leaks.
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass, field
from enum import Enum

from ghostlink.exceptions.transfer import TransferStateError
from ghostlink.messaging.models.message import validate_display_name

TRANSFER_ID_PREFIX: str = "tf_"
TRANSFER_ID_HEX: int = 8

TRANSFER_PROTOCOL: str = "gf1"


class TransferDirection(str, Enum):
    SENDING = "sending"
    RECEIVING = "receiving"


class TransferState(str, Enum):
    OFFERED = "offered"
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    QUEUED = "queued"
    TRANSFERRING = "transferring"
    PAUSED = "paused"
    COMPLETED = "completed"
    CANCELLED = "cancelled"
    FAILED = "failed"
    EXPIRED = "expired"


TERMINAL_STATES: frozenset[TransferState] = frozenset(
    {
        TransferState.REJECTED,
        TransferState.COMPLETED,
        TransferState.CANCELLED,
        TransferState.FAILED,
        TransferState.EXPIRED,
    }
)

#: states that count against the concurrency limit
ACTIVE_STATES: frozenset[TransferState] = frozenset(
    {
        TransferState.OFFERED,
        TransferState.ACCEPTED,
        TransferState.TRANSFERRING,
        TransferState.PAUSED,
    }
)

_SENDER_TRANSITIONS: dict[TransferState, frozenset[TransferState]] = {
    TransferState.QUEUED: frozenset({TransferState.OFFERED, TransferState.CANCELLED}),
    TransferState.OFFERED: frozenset(
        {
            TransferState.ACCEPTED,
            TransferState.REJECTED,
            TransferState.CANCELLED,
            TransferState.EXPIRED,
            TransferState.FAILED,
        }
    ),
    TransferState.ACCEPTED: frozenset(
        {
            TransferState.TRANSFERRING,
            TransferState.CANCELLED,
            TransferState.FAILED,
            TransferState.EXPIRED,
        }
    ),
    TransferState.TRANSFERRING: frozenset(
        {
            TransferState.PAUSED,
            TransferState.COMPLETED,
            TransferState.CANCELLED,
            TransferState.FAILED,
            TransferState.EXPIRED,
        }
    ),
    TransferState.PAUSED: frozenset(
        {
            TransferState.TRANSFERRING,
            TransferState.CANCELLED,
            TransferState.FAILED,
            TransferState.EXPIRED,
        }
    ),
    TransferState.REJECTED: frozenset(),
    TransferState.COMPLETED: frozenset(),
    TransferState.CANCELLED: frozenset(),
    TransferState.FAILED: frozenset(),
    TransferState.EXPIRED: frozenset(),
}

_RECEIVER_TRANSITIONS: dict[TransferState, frozenset[TransferState]] = {
    TransferState.QUEUED: frozenset(),
    TransferState.OFFERED: frozenset(
        {
            TransferState.ACCEPTED,
            TransferState.REJECTED,
            TransferState.CANCELLED,
            TransferState.EXPIRED,
            TransferState.FAILED,
        }
    ),
    TransferState.ACCEPTED: frozenset(
        {
            TransferState.TRANSFERRING,
            TransferState.CANCELLED,
            TransferState.FAILED,
            TransferState.EXPIRED,
        }
    ),
    TransferState.TRANSFERRING: frozenset(
        {
            TransferState.PAUSED,
            TransferState.COMPLETED,
            TransferState.CANCELLED,
            TransferState.FAILED,
            TransferState.EXPIRED,
        }
    ),
    TransferState.PAUSED: frozenset(
        {
            TransferState.TRANSFERRING,
            TransferState.CANCELLED,
            TransferState.FAILED,
            TransferState.EXPIRED,
        }
    ),
    TransferState.REJECTED: frozenset(),
    TransferState.COMPLETED: frozenset(),
    TransferState.CANCELLED: frozenset(),
    TransferState.FAILED: frozenset(),
    TransferState.EXPIRED: frozenset(),
}


def generate_transfer_id() -> str:
    """Short, unique, command-line friendly: ``tf_`` + 8 lowercase hex."""

    return f"{TRANSFER_ID_PREFIX}{secrets.token_hex(TRANSFER_ID_HEX // 2)}"


def is_valid_transfer_id(candidate: str) -> bool:
    if not candidate.startswith(TRANSFER_ID_PREFIX):
        return False
    suffix = candidate[len(TRANSFER_ID_PREFIX) :]
    if len(suffix) != TRANSFER_ID_HEX:
        return False
    try:
        int(suffix, 16)
    except ValueError:
        return False
    return suffix == suffix.lower()


def _transitions_for(
    direction: TransferDirection,
) -> dict[TransferState, frozenset[TransferState]]:
    if direction is TransferDirection.SENDING:
        return _SENDER_TRANSITIONS
    return _RECEIVER_TRANSITIONS


@dataclass(frozen=True, slots=True)
class TransferSnapshot:
    """Immutable view of a transfer, handed to events and the UI."""

    transfer_id: str
    direction: TransferDirection
    state: TransferState
    filename: str
    size_bytes: int
    mime: str
    chunk_size: int
    total_chunks: int
    chunks_done: int
    bytes_done: int
    peer: str
    created_at: float
    last_activity_at: float
    expires_at: float
    error: str | None
    saved_path: str | None
    integrity: str

    @property
    def progress(self) -> float:
        if self.size_bytes <= 0:
            return 0.0
        return min(1.0, self.bytes_done / self.size_bytes)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES


@dataclass(slots=True)
class Transfer:
    """The manager's live bookkeeping for one transfer (mutable owner-side)."""

    transfer_id: str
    direction: TransferDirection
    state: TransferState
    filename: str
    size_bytes: int
    mime: str
    chunk_size: int
    total_chunks: int
    peer: str
    integrity: str  # sha256 hex of the complete file
    source_path: str | None = None  # sender only — never sent to the peer
    local_path: str | None = None  # local source file (sender) — never sent
    saved_path: str | None = None  # receiver: final destination after verify
    chunks_done: int = 0
    bytes_done: int = 0
    created_at: float = field(default_factory=time.time)
    last_activity_at: float = field(default_factory=time.time)
    expires_at: float = 0.0
    error: str | None = None
    resume_notified: bool = False

    def __post_init__(self) -> None:
        if not is_valid_transfer_id(self.transfer_id):
            raise TransferStateError(
                f"Transfer id '{self.transfer_id}' is malformed.",
                hint="Generate identifiers with generate_transfer_id().",
            )
        validate_display_name(self.peer, field="peer name")
        if self.size_bytes < 1:
            raise TransferStateError(
                "Transfers carry at least one byte.",
                hint="Refuse empty files at offer time.",
            )
        if self.chunk_size < 1 or self.total_chunks < 1:
            raise TransferStateError("Chunk size and chunk count must be positive.")

    # -------------------------------------------------------------- transitions

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def is_active(self) -> bool:
        return self.state in ACTIVE_STATES

    def transition_to(self, state: TransferState, *, error: str | None = None) -> None:
        allowed = _transitions_for(self.direction)[self.state]
        if state not in allowed:
            raise TransferStateError(
                f"{self.direction.value} transfer cannot move {self.state.value} → {state.value}.",
                hint="Follow the offered→accepted→transferring lifecycle.",
            )
        self.state = state
        if error is not None:
            self.error = error
        self.last_activity_at = time.time()

    def touch(self) -> None:
        self.last_activity_at = time.time()

    # ---------------------------------------------------------------- snapshots

    def snapshot(self) -> TransferSnapshot:
        return TransferSnapshot(
            transfer_id=self.transfer_id,
            direction=self.direction,
            state=self.state,
            filename=self.filename,
            size_bytes=self.size_bytes,
            mime=self.mime,
            chunk_size=self.chunk_size,
            total_chunks=self.total_chunks,
            chunks_done=self.chunks_done,
            bytes_done=self.bytes_done,
            peer=self.peer,
            created_at=self.created_at,
            last_activity_at=self.last_activity_at,
            expires_at=self.expires_at,
            error=self.error,
            saved_path=self.saved_path,
            integrity=self.integrity,
        )

    def redacted_for_log(self) -> str:
        return (
            f"transfer {self.transfer_id} {self.direction.value} "
            f"{self.state.value} {self.chunks_done}/{self.total_chunks} chunks"
        )
