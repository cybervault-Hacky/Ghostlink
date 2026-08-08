"""Progress rendering helpers for file transfers (Phase 4).

Pure-text formatters plus one Rich table builder — everything the terminal
UI needs to show bars, percentages, speeds, ETAs and multi-transfer lists.
The helpers are deliberately stateless apart from :class:`SpeedMeter`, a
small sliding-window meter usable for one transfer.

Every function degrades gracefully on narrow Termux windows: callers pass
the available width and the line is composed from right to left, dropping
the least important segments first.
"""

from __future__ import annotations

import time
from collections import deque

from rich import box
from rich.table import Table

from ghostlink.transfer.models import TransferSnapshot, TransferState
from ghostlink.utils.text import format_bytes

_SPEED_WINDOW_SECONDS: float = 5.0
_PROGRESS_EMIT_MIN_INTERVAL: float = 0.5  # shared by manager + UI
_FILL = "█"
_EMPTY = "░"


def format_speed(bytes_per_second: float) -> str:
    """``2.8 MB/s`` style throughput."""

    return f"{format_bytes(bytes_per_second)}/s"


class SpeedMeter:
    """Sliding-window throughput meter fed with cumulative byte counts."""

    def __init__(self, *, window_seconds: float = _SPEED_WINDOW_SECONDS) -> None:
        self._window = window_seconds
        self._samples: deque[tuple[float, int]] = deque()

    def sample(self, bytes_done: int, *, at: float | None = None) -> None:
        now = time.monotonic() if at is None else at
        self._samples.append((now, max(0, bytes_done)))
        cutoff = now - self._window
        while len(self._samples) > 2 and self._samples[0][0] < cutoff:
            self._samples.popleft()

    @property
    def bytes_per_second(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        first_t, first_b = self._samples[0]
        last_t, last_b = self._samples[-1]
        elapsed = last_t - first_t
        if elapsed <= 0:
            return 0.0
        return max(0.0, (last_b - first_b) / elapsed)

    def eta_seconds(self, *, remaining_bytes: int) -> float | None:
        speed = self.bytes_per_second
        if speed <= 0 or remaining_bytes <= 0:
            return 0.0 if remaining_bytes <= 0 else None
        return remaining_bytes / speed


def render_bar(fraction: float, *, width: int = 20) -> str:
    """A ``[████████████████░░░░]`` bar; ``width`` counts the block cells."""

    width = max(5, width)
    fraction = min(1.0, max(0.0, fraction))
    filled = round(fraction * width)
    return f"[{_FILL * filled}{_EMPTY * (width - filled)}]"


def _format_eta(eta: float | None) -> str | None:
    if eta is None:
        return None
    if eta < 60:
        return f"ETA: {max(0.1, eta):.1f}s"
    minutes, seconds = divmod(int(eta), 60)
    return f"ETA: {minutes}m{seconds:02d}s"


def progress_line(
    snapshot: TransferSnapshot,
    *,
    meter: SpeedMeter | None = None,
    width: int = 80,
) -> str:
    """One live status line for a transfer, width-aware.

    Composed as: bar, percentage, bytes, speed, ETA — the least valuable
    segments (ETA, then speed, then bytes) drop first when the terminal is
    narrow, and the bar itself shrinks to fit what remains.
    """

    fraction = snapshot.progress
    percent = f"{fraction * 100:.0f}%"
    candidates: list[str | None] = [
        f"{format_bytes(snapshot.bytes_done)} / {format_bytes(snapshot.size_bytes)}",
        None,  # speed
        None,  # eta
    ]
    if meter is not None and snapshot.state is TransferState.TRANSFERRING:
        speed = meter.bytes_per_second
        if speed > 0:
            candidates[1] = f"Speed: {format_speed(speed)}"
        candidates[2] = _format_eta(
            meter.eta_seconds(remaining_bytes=snapshot.size_bytes - snapshot.bytes_done)
        )

    # Greedily keep segments (bytes first, then speed, then ETA) while at
    # least a minimal bar still fits. [bar]=width+2 cells; two separators.
    kept: list[str] = []
    budget = width - len(percent) - 4  # bar brackets + two single spaces
    for candidate in candidates:
        if candidate is None:
            continue
        if budget - len(candidate) - 1 >= 7:  # minimal "[█████]" + space
            kept.append(candidate)
            budget -= len(candidate) + 1
    bar_cells = max(5, min(20, budget - 2))
    return " ".join([render_bar(fraction, width=bar_cells), percent, *kept])


def state_label(state: TransferState) -> str:
    """Short, presentation-friendly label used in tables and detail views."""

    return {
        TransferState.OFFERED: "waiting for approval",
        TransferState.ACCEPTED: "accepted",
        TransferState.REJECTED: "rejected",
        TransferState.QUEUED: "queued",
        TransferState.TRANSFERRING: "transferring",
        TransferState.PAUSED: "paused",
        TransferState.COMPLETED: "completed ✓",
        TransferState.CANCELLED: "cancelled",
        TransferState.FAILED: "failed ✗",
        TransferState.EXPIRED: "expired",
    }[state]


def transfers_table(snapshots: list[TransferSnapshot]) -> Table:
    """The ``/transfers`` overview: one row per known transfer."""

    table = Table(
        box=box.SIMPLE_HEAVY,
        show_header=True,
        header_style="gl.accent",
        expand=False,
        pad_edge=False,
    )
    table.add_column("ID", style="gl.muted", no_wrap=True)
    table.add_column("File", style="gl.text", overflow="fold", ratio=2)
    table.add_column("Dir", style="gl.muted", no_wrap=True)
    table.add_column("Size", style="gl.text", no_wrap=True, justify="right")
    table.add_column("Progress", style="gl.text", no_wrap=True)
    table.add_column("State", style="gl.text", no_wrap=True)
    for snapshot in snapshots:
        direction = "↑ out" if snapshot.direction.value == "sending" else "↓ in"
        percent = f"{snapshot.progress * 100:3.0f}%"
        table.add_row(
            snapshot.transfer_id,
            snapshot.filename,
            direction,
            format_bytes(snapshot.size_bytes),
            f"{percent} ({snapshot.chunks_done}/{snapshot.total_chunks})",
            state_label(snapshot.state),
        )
    return table
