"""Connection manager: state machine, reader loop, timeout and recovery."""

from __future__ import annotations

import asyncio

import pytest

from ghostlink.exceptions.transport import ConnectionTimeoutError, TransportError
from ghostlink.transport.connection import (
    ConnectionManager,
    ConnectionState,
    ReconnectPolicy,
)
from ghostlink.transport.transport import Transport, TransportState, TransportStats
from tests.conftest import run


class ScriptedTransport(Transport):
    """A :class:`Transport` double fed by an in-memory queue.

    ``feed`` queues inbound messages; ``None`` simulates a clean peer close.
    """

    def __init__(
        self, *, remote: str = "script://peer", open_error: Exception | None = None
    ) -> None:
        self._remote = remote
        self._open_error = open_error
        self._state = TransportState.INITIALIZING
        self._stats = TransportStats()
        self._incoming: asyncio.Queue[bytes | None] = asyncio.Queue()
        self.sent: list[bytes] = []

    # test controls ---------------------------------------------------------
    def feed(self, data: bytes | None) -> None:
        self._incoming.put_nowait(data)

    # contract ---------------------------------------------------------------
    @property
    def name(self) -> str:
        return "scripted"

    @property
    def state(self) -> TransportState:
        return self._state

    @property
    def stats(self) -> TransportStats:
        return self._stats

    @property
    def remote_description(self) -> str:
        return self._remote

    async def open(self) -> None:
        if self._open_error is not None:
            self._state = TransportState.FAILED
            raise self._open_error
        self._state = TransportState.OPEN

    async def close(self) -> None:
        self._state = TransportState.CLOSED

    async def send(self, data: bytes) -> None:
        if self._state is not TransportState.OPEN:
            raise TransportError("scripted transport is closed")
        self.sent.append(data)

    async def receive(self) -> bytes | None:
        return await self._incoming.get()


def _manager(
    transports: list[ScriptedTransport],
    inbox: list[bytes],
    *,
    policy: ReconnectPolicy | None = None,
) -> ConnectionManager:
    queue = list(transports)

    async def factory() -> Transport:
        return queue.pop(0)

    return ConnectionManager(
        transport_factory=factory,
        on_bytes=inbox.append,
        reconnect_policy=policy,
        name="test",
    )


async def _wait_for_state(manager: ConnectionManager, state: ConnectionState) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + 2.0
    while manager.state is not state and loop.time() < deadline:
        await asyncio.sleep(0.005)
    assert manager.state is state, f"state stuck at {manager.state.value}"


class TestOpen:
    def test_open_connects_and_tracks_history(self) -> None:
        async def scenario() -> tuple[ConnectionManager, ScriptedTransport]:
            transport = ScriptedTransport()
            manager = _manager([transport], [])
            await manager.open(timeout_seconds=1.0)
            assert manager.state is ConnectionState.CONNECTED
            assert manager.is_usable
            states = [change.state for change in manager.stats.state_history]
            assert states == [ConnectionState.CONNECTING, ConnectionState.CONNECTED]
            await manager.close()
            return manager, transport

        manager, transport = run(scenario())
        assert manager.state is ConnectionState.CLOSED
        assert manager.stats.connect_count == 1
        assert manager.stats.successful_connects == 1
        assert transport.state is TransportState.CLOSED

    def test_open_twice_is_rejected(self) -> None:
        async def scenario() -> None:
            manager = _manager([ScriptedTransport()], [])
            await manager.open()
            try:
                with pytest.raises(TransportError, match="cannot open"):
                    await manager.open()
            finally:
                await manager.close()

        run(scenario())

    def test_handshake_failure_disconnects_and_propagates(self) -> None:
        async def scenario() -> ConnectionManager:
            manager = _manager([ScriptedTransport()], [])

            async def failing_handshake(transport: Transport) -> None:
                raise TransportError("handshake blew up")

            with pytest.raises(TransportError, match="handshake blew up"):
                await manager.open(handshake=failing_handshake)
            return manager

        manager = run(scenario())
        assert manager.state is ConnectionState.DISCONNECTED
        assert manager.stats.last_error == "handshake blew up"

    def test_open_timeout_transitions_through_timed_out(self) -> None:
        async def scenario() -> ConnectionManager:
            class SlowTransport(ScriptedTransport):
                async def open(self) -> None:
                    await asyncio.sleep(30)

            manager = _manager([SlowTransport()], [])
            with pytest.raises(ConnectionTimeoutError):
                await manager.open(timeout_seconds=0.05)
            return manager

        manager = run(scenario())
        assert manager.state is ConnectionState.DISCONNECTED
        states = [change.state for change in manager.stats.state_history]
        assert ConnectionState.TIMED_OUT in states
        assert manager.stats.last_error == "timed out"

    def test_transport_open_failure_propagates(self) -> None:
        async def scenario() -> ConnectionManager:
            broken = ScriptedTransport(open_error=OSError("connection refused"))
            manager = _manager([broken], [])
            with pytest.raises(OSError, match="connection refused"):
                await manager.open()
            return manager

        manager = run(scenario())
        assert manager.state is ConnectionState.DISCONNECTED
        assert "connection refused" in manager.stats.last_error


class TestSendAndReceive:
    def test_send_requires_connection(self) -> None:
        manager = _manager([], [])
        with pytest.raises(TransportError, match="not connected"):
            run(manager.send(b"nope"))

    def test_send_round_trips_bytes(self) -> None:
        async def scenario() -> ScriptedTransport:
            transport = ScriptedTransport()
            manager = _manager([transport], [])
            await manager.open()
            try:
                await manager.send(b"payload")
            finally:
                await manager.close()
            return transport

        transport = run(scenario())
        assert transport.sent == [b"payload"]

    def test_inbound_bytes_reach_the_callback(self) -> None:
        async def scenario() -> list[bytes]:
            transport = ScriptedTransport()
            inbox: list[bytes] = []
            manager = _manager([transport], inbox)
            await manager.open()
            transport.feed(b'{"hello":1}')
            transport.feed(b'{"world":2}')
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2.0
            while len(inbox) < 2 and loop.time() < deadline:
                await asyncio.sleep(0.005)
            await manager.close()
            return inbox

        assert run(scenario()) == [b'{"hello":1}', b'{"world":2}']


class TestUnexpectedLoss:
    def test_clean_peer_close_ends_closed_without_reconnect(self) -> None:
        async def scenario() -> ConnectionManager:
            transport = ScriptedTransport()
            manager = _manager([transport], [])
            await manager.open()
            transport.feed(None)  # peer closed cleanly
            await _wait_for_state(manager, ConnectionState.CLOSED)
            return manager

        manager = run(scenario())
        changes = {change.state: change.reason for change in manager.stats.state_history}
        assert "peer closed" in changes[ConnectionState.DISCONNECTED]
        assert manager.stats.successful_connects == 1

    def test_transport_error_records_last_error(self) -> None:
        async def scenario() -> ConnectionManager:
            class ExplodingTransport(ScriptedTransport):
                async def receive(self) -> bytes | None:
                    raise TransportError("read failed")

            manager = _manager([ExplodingTransport()], [])
            await manager.open()
            await _wait_for_state(manager, ConnectionState.CLOSED)
            return manager

        manager = run(scenario())
        assert manager.stats.last_error == "read failed"


class TestReconnection:
    def test_reconnects_after_loss_and_continues(self) -> None:
        async def scenario() -> tuple[ConnectionManager, ScriptedTransport]:
            first = ScriptedTransport(remote="script://first")
            second = ScriptedTransport(remote="script://second")
            manager = _manager(
                [first, second],
                [],
                policy=ReconnectPolicy(attempts=2, base_delay_seconds=0.02, jitter=0.0),
            )
            await manager.open()
            first.feed(None)  # simulate loss of the first transport
            loop = asyncio.get_running_loop()
            deadline = loop.time() + 2.0
            while manager.stats.successful_connects < 2 and loop.time() < deadline:
                await asyncio.sleep(0.005)
            assert manager.stats.successful_connects == 2
            await _wait_for_state(manager, ConnectionState.CONNECTED)
            assert manager.transport is second
            await manager.close()
            return manager, second

        manager, second = run(scenario())
        assert manager.stats.successful_connects == 2
        assert manager.stats.reconnect_attempts_made >= 1
        assert second.state is TransportState.CLOSED
        reasons = [change.reason for change in manager.stats.state_history]
        assert any("recovered" in reason for reason in reasons)

    def test_reconnect_exhaustion_closes_permanently(self) -> None:
        async def scenario() -> ConnectionManager:
            first = ScriptedTransport()
            broken = [
                ScriptedTransport(open_error=OSError("refused")),
                ScriptedTransport(open_error=OSError("refused")),
            ]
            manager = _manager(
                [first, *broken],
                [],
                policy=ReconnectPolicy(attempts=2, base_delay_seconds=0.02, jitter=0.0),
            )
            await manager.open()
            first.feed(None)
            await asyncio.wait_for(manager.wait_closed(), timeout=2.0)
            return manager

        manager = run(scenario())
        assert manager.state is ConnectionState.CLOSED
        assert manager.stats.reconnect_attempts_made == 2
        changes = [change.state for change in manager.stats.state_history]
        assert ConnectionState.RECONNECTING in changes

    def test_close_during_reconnect_stops_recovery(self) -> None:
        async def scenario() -> ConnectionManager:
            first = ScriptedTransport()
            manager = _manager(
                [first],
                [],
                policy=ReconnectPolicy(attempts=3, base_delay_seconds=60.0, jitter=0.0),
            )
            await manager.open()
            first.feed(None)  # triggers a reconnect pending a 60s backoff
            await _wait_for_state(manager, ConnectionState.RECONNECTING)
            await manager.close()
            return manager

        manager = run(scenario())
        assert manager.state is ConnectionState.CLOSED
        assert manager.stats.reconnect_attempts_made <= 1

    def test_forced_reconnect_requires_policy(self) -> None:
        async def scenario() -> None:
            manager = _manager([ScriptedTransport()], [], policy=ReconnectPolicy(attempts=0))
            await manager.open()
            try:
                with pytest.raises(TransportError, match="reconnection disabled"):
                    await manager.reconnect()
            finally:
                await manager.close()

        run(scenario())

    def test_forced_reconnect_cycle(self) -> None:
        async def scenario() -> ConnectionManager:
            first = ScriptedTransport()
            second = ScriptedTransport()
            manager = _manager(
                [first, second],
                [],
                policy=ReconnectPolicy(attempts=1, base_delay_seconds=0.01, jitter=0.0),
            )
            await manager.open()
            await manager.reconnect()
            assert manager.state is ConnectionState.CONNECTED
            assert manager.transport is second
            await manager.close()
            return manager

        manager = run(scenario())
        assert manager.stats.successful_connects == 2


class TestClose:
    def test_close_without_open_marks_closed(self) -> None:
        manager = _manager([], [])
        run(manager.close())
        assert manager.state is ConnectionState.CLOSED
        run(manager.close())  # idempotent
        assert manager.state is ConnectionState.CLOSED

    def test_context_manager_closes(self) -> None:
        async def scenario() -> ConnectionManager:
            async with _manager([ScriptedTransport()], []) as manager:
                await manager.open()
            return manager

        assert run(scenario()).state is ConnectionState.CLOSED


class TestReconnectPolicy:
    def test_backoff_grows_and_respects_bounds(self) -> None:
        policy = ReconnectPolicy(attempts=5, base_delay_seconds=1.0, jitter=0.25)
        first = policy.delay_for(1)
        second = policy.delay_for(2)
        third = policy.delay_for(3)
        assert 0.75 <= first <= 1.25
        assert 1.5 <= second <= 2.5
        assert 3.0 <= third <= 5.0

    def test_max_delay_caps_growth(self) -> None:
        policy = ReconnectPolicy(
            attempts=10, base_delay_seconds=1.0, max_delay_seconds=4.0, jitter=0.0
        )
        assert policy.delay_for(10) == 4.0
