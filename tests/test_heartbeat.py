"""Heartbeat monitor: scheduling, RTT measurement, and missed-ping recovery."""

from __future__ import annotations

import asyncio
import time

from ghostlink.transport.heartbeat import HeartbeatMonitor, HeartbeatStats
from tests.conftest import run


class ScriptedPeer:
    """Records heartbeats/pings the monitor asks to send; never replies."""

    def __init__(self) -> None:
        self.heartbeats: list[int] = []
        self.pings: list[str] = []
        self.missed_calls = 0
        self.monitor = HeartbeatMonitor(
            interval_seconds=3600.0,  # automatic ticks effectively off; drive manually
            timeout_seconds=0.05,
            send_heartbeat=self._send_heartbeat,
            send_ping=self._send_ping,
            on_missed=self._on_missed,
            missed_limit=2,
        )

    async def _send_heartbeat(self, sequence: int) -> None:
        self.heartbeats.append(sequence)

    async def _send_ping(self, nonce: str, sent_at: float) -> None:
        self.pings.append(nonce)

    def _on_missed(self) -> None:
        self.missed_calls += 1


class TestStats:
    def test_empty_stats(self) -> None:
        stats = HeartbeatStats()
        assert stats.latest_rtt_ms is None
        assert stats.average_rtt_ms is None
        assert stats.min_rtt_ms is None
        assert stats.max_rtt_ms is None

    def test_sample_aggregation(self) -> None:
        stats = HeartbeatStats()
        for sample in (10.0, 20.0, 30.0):
            stats.record_rtt(sample)
        assert stats.latest_rtt_ms == 30.0
        assert stats.average_rtt_ms == 20.0
        assert stats.min_rtt_ms == 10.0
        assert stats.max_rtt_ms == 30.0

    def test_samples_are_bounded(self) -> None:
        stats = HeartbeatStats()
        for index in range(400):
            stats.record_rtt(float(index))
        assert len(stats.rtt_samples_ms) == 256
        assert stats.rtt_samples_ms[-1] == 399.0


class TestTicking:
    def test_tick_sends_one_heartbeat_and_one_ping(self) -> None:
        async def scenario() -> ScriptedPeer:
            peer = ScriptedPeer()
            await peer.monitor.tick_now()
            await peer.monitor.tick_now()
            return peer

        peer = run(scenario())
        assert peer.heartbeats == [1, 2]
        assert len(peer.pings) == 2 and peer.pings[0] != peer.pings[1]
        assert peer.monitor.stats.heartbeats_sent == 2
        assert peer.monitor.stats.pings_sent == 2
        assert peer.monitor.pending_pings == 2

    def test_failing_sender_does_not_count_as_sent(self) -> None:
        async def scenario() -> HeartbeatMonitor:
            async def broken_heartbeat(sequence: int) -> None:
                raise OSError("link down")

            async def broken_ping(nonce: str, sent_at: float) -> None:
                raise OSError("link down")

            monitor = HeartbeatMonitor(
                interval_seconds=3600.0,
                timeout_seconds=1.0,
                send_heartbeat=broken_heartbeat,
                send_ping=broken_ping,
                on_missed=lambda: None,
            )
            await monitor.tick_now()
            return monitor

        monitor = run(scenario())
        assert monitor.stats.heartbeats_sent == 0
        assert monitor.stats.pings_sent == 0
        assert monitor.pending_pings == 0


class TestAcknowledgements:
    def test_ack_pong_measures_rtt(self) -> None:
        async def scenario() -> HeartbeatMonitor:
            peer = ScriptedPeer()
            monitor = peer.monitor
            await monitor.tick_now()
            rtt = monitor.ack_pong(peer.pings[0])
            assert rtt is not None and 0.0 <= rtt < 1000.0
            return monitor

        monitor = run(scenario())
        assert monitor.stats.pongs_received == 1
        assert len(monitor.stats.rtt_samples_ms) == 1
        assert monitor.pending_pings == 0

    def test_ack_pong_with_unknown_nonce_is_ignored(self) -> None:
        peer = ScriptedPeer()
        run(peer.monitor.tick_now())
        assert peer.monitor.ack_pong("ffffffffffffffff") is None  # synchronous API
        assert peer.monitor.stats.pongs_received == 0

    def test_ack_heartbeat_matches_pending_sequences_only(self) -> None:
        peer = ScriptedPeer()
        run(peer.monitor.tick_now())
        assert peer.monitor.ack_heartbeat(1) is True
        assert peer.monitor.ack_heartbeat(1) is False
        assert peer.monitor.ack_heartbeat(999) is False
        assert peer.monitor.stats.heartbeats_acked == 1


class TestMissedPings:
    def test_missed_limit_fires_on_missed_once_per_streak(self) -> None:
        async def scenario() -> ScriptedPeer:
            peer = ScriptedPeer()
            monitor = peer.monitor
            await monitor.tick_now()  # ping A pending
            await asyncio.sleep(0.06)
            await monitor.tick_now()  # reaps A (streak 1), ping B pending
            await asyncio.sleep(0.06)
            await monitor.tick_now()  # reaps B (streak 2 → limit), ping C pending
            return peer

        peer = run(scenario())
        assert peer.missed_calls == 1
        assert peer.monitor.stats.missed_pings == 2
        assert peer.monitor.stats.missed_streak == 0  # reset after the limit fired

    def test_answered_pings_reset_the_streak(self) -> None:
        async def scenario() -> tuple[ScriptedPeer, HeartbeatMonitor]:
            peer = ScriptedPeer()
            monitor = peer.monitor
            await monitor.tick_now()  # ping A
            await asyncio.sleep(0.06)
            await monitor.tick_now()  # reaps A (streak 1), ping B
            monitor.ack_pong(peer.pings[1])  # answer B before it expires
            assert monitor.stats.missed_streak == 0
            return peer, monitor

        peer, monitor = run(scenario())
        assert peer.missed_calls == 0
        assert monitor.stats.missed_pings == 1
        assert monitor.stats.pongs_received == 1


class TestLifecycle:
    def test_start_ticks_immediately_and_stop_settles(self) -> None:
        async def scenario() -> tuple[ScriptedPeer, bool]:
            peer = ScriptedPeer()
            peer.monitor.start()
            await asyncio.sleep(0.05)
            running = peer.monitor.running
            await peer.monitor.stop()
            return peer, running

        peer, was_running = run(scenario())
        assert was_running is True
        assert peer.monitor.running is False
        assert peer.heartbeats == [1]  # exactly the startup tick
        assert peer.monitor.stats.heartbeats_sent == 1

    def test_start_is_idempotent(self) -> None:
        async def scenario() -> HeartbeatMonitor:
            peer = ScriptedPeer()
            peer.monitor.start()
            first_task = peer.monitor._task
            peer.monitor.start()
            assert peer.monitor._task is first_task
            await peer.monitor.stop()
            return peer.monitor

        run(scenario())

    def test_periodic_ticks_follow_the_interval(self) -> None:
        async def scenario() -> ScriptedPeer:
            peer = ScriptedPeer()
            peer.monitor._interval = 0.05  # narrow the interval for the test
            peer.monitor.start()
            started = time.monotonic()
            while len(peer.heartbeats) < 3 and time.monotonic() - started < 2.0:
                await asyncio.sleep(0.01)
            await peer.monitor.stop()
            return peer

        peer = run(scenario())
        assert len(peer.heartbeats) >= 3
