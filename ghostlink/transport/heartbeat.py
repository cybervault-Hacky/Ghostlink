"""Heartbeat scheduling and round-trip-time measurement.

The monitor performs two distinct liveness functions on a shared interval:

* ``HEARTBEAT`` — an application keepalive tick the peer acknowledges, proving
  the *protocol* is alive both ways.
* ``PING``/``PONG`` — a nonce-stamped probe that measures real round-trip
  latency.

Unanswered pings are reaped after ``timeout_seconds``; enough consecutive
misses fire ``on_missed`` exactly once per streak, letting the connection
layer declare the peer dead and begin recovery.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field

from ghostlink.core.logging import get_logger

MAX_RTT_SAMPLES = 256
MISSED_PING_LIMIT = 3


@dataclass(slots=True)
class HeartbeatStats:
    """Mutable counters maintained by a :class:`HeartbeatMonitor`."""

    heartbeats_sent: int = 0
    heartbeats_acked: int = 0
    pings_sent: int = 0
    pongs_received: int = 0
    missed_pings: int = 0
    missed_streak: int = 0
    rtt_samples_ms: list[float] = field(default_factory=list)

    def record_rtt(self, rtt_ms: float) -> None:
        self.rtt_samples_ms.append(rtt_ms)
        if len(self.rtt_samples_ms) > MAX_RTT_SAMPLES:
            del self.rtt_samples_ms[: len(self.rtt_samples_ms) - MAX_RTT_SAMPLES]

    @property
    def latest_rtt_ms(self) -> float | None:
        return self.rtt_samples_ms[-1] if self.rtt_samples_ms else None

    @property
    def average_rtt_ms(self) -> float | None:
        if not self.rtt_samples_ms:
            return None
        return sum(self.rtt_samples_ms) / len(self.rtt_samples_ms)

    @property
    def min_rtt_ms(self) -> float | None:
        return min(self.rtt_samples_ms) if self.rtt_samples_ms else None

    @property
    def max_rtt_ms(self) -> float | None:
        return max(self.rtt_samples_ms) if self.rtt_samples_ms else None


class HeartbeatMonitor:
    """Drives periodic keepalive traffic for one connection."""

    def __init__(
        self,
        *,
        interval_seconds: float,
        timeout_seconds: float,
        send_heartbeat: Callable[[int], Awaitable[None]],
        send_ping: Callable[[str, float], Awaitable[None]],
        on_missed: Callable[[], None],
        missed_limit: int = MISSED_PING_LIMIT,
    ) -> None:
        self._interval = interval_seconds
        self._timeout = timeout_seconds
        self._send_heartbeat = send_heartbeat
        self._send_ping = send_ping
        self._on_missed = on_missed
        self._missed_limit = max(1, missed_limit)
        self._logger = get_logger("transport.heartbeat")
        self._task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()
        self._sequence = 0
        self._pending_pings: dict[str, float] = {}
        self._pending_heartbeats: set[int] = set()
        self.stats = HeartbeatStats()

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @property
    def pending_pings(self) -> int:
        return len(self._pending_pings)

    # -------------------------------------------------------------- lifecycle

    def start(self) -> None:
        """Launch the heartbeat loop. Idempotent."""

        if self.running:
            return
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run(), name="ghostlink-heartbeat")

    async def stop(self) -> None:
        """Cancel the loop and wait for it to settle."""

        task = self._task
        self._task = None
        if task is None:
            return
        self._stop_event.set()
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    # -------------------------------------------------------------- scheduling

    async def tick_now(self) -> None:
        """Send one heartbeat + ping cycle immediately (probes and tests)."""

        await self._tick()

    async def _run(self) -> None:
        try:
            while not self._stop_event.is_set():
                await self._tick()
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=self._interval)
                except TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise

    async def _tick(self) -> None:
        self._reap_expired_pings()

        self._sequence += 1
        sequence = self._sequence
        self._pending_heartbeats.add(sequence)
        try:
            await self._send_heartbeat(sequence)
            self.stats.heartbeats_sent += 1
            self._logger.debug("heartbeat sent — seq=%d", sequence)
        except Exception as exc:  # transport is failing; connection layer decides
            self._pending_heartbeats.discard(sequence)
            self._logger.debug("heartbeat send failed: %s", exc)

        nonce = secrets.token_hex(8)
        sent_at = time.monotonic()
        self._pending_pings[nonce] = sent_at
        try:
            await self._send_ping(nonce, sent_at)
            self.stats.pings_sent += 1
            self._logger.debug("ping sent — nonce=%s", nonce)
        except Exception as exc:
            self._pending_pings.pop(nonce, None)
            self._logger.debug("ping send failed: %s", exc)

    def _reap_expired_pings(self) -> None:
        if not self._pending_pings:
            return
        cutoff = time.monotonic() - self._timeout
        expired = [nonce for nonce, sent_at in self._pending_pings.items() if sent_at < cutoff]
        for nonce in expired:
            del self._pending_pings[nonce]
        if expired:
            self.stats.missed_pings += len(expired)
            self.stats.missed_streak += 1
            self._logger.debug(
                "%d ping(s) unanswered (streak=%d)", len(expired), self.stats.missed_streak
            )
            if self.stats.missed_streak >= self._missed_limit:
                self.stats.missed_streak = 0
                self._logger.info("heartbeat missed limit reached — peer considered dead")
                self._on_missed()

    # ------------------------------------------------------------ acknowledgements

    def ack_pong(self, nonce: str) -> float | None:
        """Resolve a PONG by nonce; returns the measured RTT in milliseconds."""

        sent_at = self._pending_pings.pop(nonce, None)
        if sent_at is None:
            self._logger.debug("pong with unknown nonce=%s ignored", nonce)
            return None
        rtt_ms = (time.monotonic() - sent_at) * 1000.0
        self.stats.pongs_received += 1
        self.stats.missed_streak = 0
        self.stats.record_rtt(rtt_ms)
        self._logger.debug("pong received — nonce=%s rtt=%.1fms", nonce, rtt_ms)
        return rtt_ms

    def ack_heartbeat(self, sequence: int) -> bool:
        """Resolve a HEARTBEAT ack by sequence number."""

        if sequence not in self._pending_heartbeats:
            return False
        self._pending_heartbeats.discard(sequence)
        self.stats.heartbeats_acked += 1
        return True
