"""RelayClient against a real, in-process relay server (live loopback)."""

from __future__ import annotations

import asyncio

import pytest

from ghostlink.constants.net import PROTOCOL_VERSION
from ghostlink.exceptions.transport import TransportError
from ghostlink.transport.connection import ConnectionState
from ghostlink.transport.relay.client import (
    RelayClient,
    RelayClientConfig,
    probe_relay,
)
from ghostlink.transport.relay.endpoint import RelayEndpoint
from ghostlink.transport.relay.protocol import hello_packet
from ghostlink.transport.relay.server import RelayServer
from ghostlink.transport.session import SessionStatus
from tests.conftest import run, running_relay

FAST = RelayClientConfig(
    connect_timeout_seconds=2.0,
    handshake_timeout_seconds=2.0,
    heartbeat_interval_seconds=3600.0,  # scheduled ticks effectively off in tests
    heartbeat_timeout_seconds=1.0,
    reconnect_attempts=0,
    reconnect_base_delay_seconds=0.05,
)


def _client(server: RelayServer, *, config: RelayClientConfig = FAST) -> RelayClient:
    return RelayClient(
        RelayEndpoint.from_url(server.url), client_name="GhostLink/test", config=config
    )


async def _wait_until(predicate: object, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    check = predicate  # type: ignore[operator]
    while loop.time() < deadline:
        if check():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within the deadline")


class TestConnectHandshake:
    def test_connect_establishes_a_live_session(self) -> None:
        async def scenario() -> tuple[RelayClient, str]:
            async with running_relay() as server:
                client = _client(server)
                session = await client.connect()
                try:
                    assert client.state is ConnectionState.CONNECTED
                    assert session.session_id.startswith("sess_")
                    assert session.metadata["server"] == "ghostlink-relay"
                    assert session.metadata["remote"].startswith("ws://127.0.0.1:")
                    assert session.metadata["client"] == "GhostLink/test"
                    assert session.status is SessionStatus.ACTIVE
                    assert client.manager.stats.successful_connects == 1
                    await _wait_until(lambda: server.sessions.count() == 1)
                    return client, session.session_id
                finally:
                    await client.disconnect()

        client, session_id = run(scenario())
        assert client.state is ConnectionState.CLOSED
        assert session_id.startswith("sess_")

    def test_connect_to_unreachable_relay_raises(self) -> None:
        async def scenario() -> RelayClient:
            client = RelayClient(
                RelayEndpoint.from_url("ws://127.0.0.1:1/relay"),
                client_name="GhostLink/test",
                config=FAST,
            )
            with pytest.raises(TransportError, match="Could not reach"):
                await client.connect()
            return client

        client = run(scenario())
        assert client.state is ConnectionState.DISCONNECTED
        assert client.session is None


class TestPingAndHeartbeat:
    def test_manual_ping_measures_real_rtt(self) -> None:
        async def scenario() -> dict[str, float]:
            async with running_relay() as server:
                client = _client(server)
                await client.connect()
                try:
                    samples = [await client.ping() for _ in range(3)]
                    return {"min": min(samples), "max": max(samples)}
                finally:
                    await client.disconnect()

        result = run(scenario())
        assert 0.0 <= result["min"] <= result["max"] < 1000.0

    def test_heartbeat_tick_is_acknowledged(self) -> None:
        async def scenario() -> int:
            async with running_relay() as server:
                client = _client(server)
                await client.connect()
                try:
                    acked_before = client.heartbeat_stats.heartbeats_acked
                    await client.heartbeat_tick()
                    await _wait_until(
                        lambda: client.heartbeat_stats.heartbeats_acked >= acked_before + 1
                    )
                    return client.heartbeat_stats.heartbeats_acked
                finally:
                    await client.disconnect()

        acked = run(scenario())
        assert acked >= 1

    def test_disconnect_is_graceful_and_final(self) -> None:
        async def scenario() -> tuple[RelayServer, RelayClient]:
            async with running_relay() as server:
                client = _client(server)
                await client.connect()
                await _wait_until(lambda: server.sessions.count() == 1)
                await _wait_until(lambda: client.heartbeat_stats.pings_sent >= 1)
                await client.disconnect()
                await _wait_until(lambda: server.client_count == 0)
                await _wait_until(lambda: server.sessions.count() == 0)
                return server, client

        server, client = run(scenario())
        assert client.state is ConnectionState.CLOSED
        assert client.session is not None
        assert client.session.status is SessionStatus.CLOSED
        assert client.heartbeat_stats.pings_sent >= 1  # startup tick sent a ping
        assert server.sessions.count() == 0


class TestProtocolEnforcement:
    def test_invalid_inbound_bytes_are_dropped_silently(self) -> None:
        async def scenario() -> RelayClient:
            async with running_relay() as server:
                client = _client(server)
                await client.connect()
                try:
                    # a malformed packet reaching *our* reader must not kill it
                    assert client.manager.transport is not None
                    await client.send_packet(
                        hello_packet("GhostLink/test")
                    )  # unexpected mid-session
                    await _wait_until(lambda: len(client.error_packets) == 1)
                    assert client.error_packets[0].payload["code"] == "protocol/unexpected-type"
                    assert client.state is ConnectionState.CONNECTED
                    return client
                finally:
                    await client.disconnect()

        run(scenario())

    def test_server_flags_garbage_bytes_with_an_error_packet(self) -> None:
        async def scenario() -> str:
            async with running_relay() as server:
                client = _client(server)
                await client.connect()
                try:
                    transport = client.manager.transport
                    assert transport is not None
                    await transport.send(b"{ definitely not json")
                    await _wait_until(lambda: len(client.error_packets) == 1)
                    return str(client.error_packets[0].payload["code"])
                finally:
                    await client.disconnect()

        assert run(scenario()) == "protocol/invalid-packet"


class TestRecovery:
    def test_reconnects_after_the_relay_restarts(self) -> None:
        async def scenario() -> tuple[RelayClient, str, str]:
            server = RelayServer(host="127.0.0.1", port=0)
            await server.start()
            port = server.port
            client = _client(
                server,
                config=RelayClientConfig(
                    connect_timeout_seconds=2.0,
                    handshake_timeout_seconds=2.0,
                    heartbeat_interval_seconds=3600.0,
                    heartbeat_timeout_seconds=1.0,
                    reconnect_attempts=5,
                    reconnect_base_delay_seconds=0.05,
                ),
            )
            first_session = (await client.connect()).session_id
            await server.aclose()

            # the reader sees the close and starts supervised recovery; bring
            # the relay back on the same port before attempts run out
            revived = RelayServer(host="127.0.0.1", port=port)
            await revived.start()
            try:
                await _wait_until(
                    lambda: client.manager.stats.successful_connects >= 2, timeout=5.0
                )
                assert client.state is ConnectionState.CONNECTED
                assert client.session is not None
                second_session = client.session.session_id
                rtt = await client.ping()
                assert rtt >= 0.0
            finally:
                await client.disconnect()
                await revived.aclose()
            return client, first_session, second_session

        client, first_session, second_session = run(scenario())
        assert first_session != second_session
        states = [change.state for change in client.manager.stats.state_history]
        assert states[0] is ConnectionState.CONNECTING
        reasons = [change.reason for change in client.state_changes]
        assert any("recovered" in reason for reason in reasons)

    def test_forced_reconnect_cycle_keeps_one_reader(self) -> None:
        async def scenario() -> RelayClient:
            async with running_relay() as server:
                client = _client(
                    server,
                    config=RelayClientConfig(
                        connect_timeout_seconds=2.0,
                        handshake_timeout_seconds=2.0,
                        heartbeat_interval_seconds=3600.0,
                        heartbeat_timeout_seconds=1.0,
                        reconnect_attempts=2,
                        reconnect_base_delay_seconds=0.02,
                    ),
                )
                await client.connect()
                try:
                    await client.reconnect()
                    assert client.state is ConnectionState.CONNECTED
                    assert client.manager.stats.successful_connects == 2
                    assert await client.ping() >= 0.0
                    return client
                finally:
                    await client.disconnect()

        run(scenario())


class TestProbe:
    def test_probe_report_captures_the_full_picture(self) -> None:
        async def scenario() -> tuple[str, int, bool, int]:
            async with running_relay(server_name="relay-under-test") as server:
                report = await probe_relay(
                    server.url,
                    client_name="GhostLink/probe",
                    config=FAST,
                    pings=3,
                    ping_interval_seconds=0.01,
                )
                assert report.endpoint == server.url
                assert report.secure is False
                assert report.session_id.startswith("sess_")
                assert report.server_name == "relay-under-test"
                assert report.protocol_version == PROTOCOL_VERSION
                assert len(report.rtt_samples_ms) == 3
                assert all(sample >= 0.0 for sample in report.rtt_samples_ms)
                assert report.heartbeats_acked >= 1
                assert report.packets_sent >= 5  # HELLO + heartbeat tick + pings
                assert report.packets_received >= 3  # WELCOME + pongs (+ heartbeat ack)
                assert report.bytes_sent > 0 and report.bytes_received > 0
                assert report.duration_seconds > 0
                states = [state for _, state in report.state_timeline]
                assert states[:2] == ["connecting", "connected"]
                assert "disconnected" in states and states[-1] == "closed"
                assert report.graceful_disconnect is True
                return (
                    report.endpoint,
                    len(report.rtt_samples_ms),
                    report.secure,
                    report.packets_sent,
                )

        endpoint, sample_count, secure, packets_sent = run(scenario())
        assert endpoint.startswith("ws://127.0.0.1:")
        assert sample_count == 3
        assert secure is False
        assert packets_sent >= 5

    def test_probe_requires_at_least_one_ping(self) -> None:
        from ghostlink.exceptions.transport import RelayError

        async def scenario() -> None:
            with pytest.raises(RelayError, match="at least one ping"):
                await probe_relay("ws://127.0.0.1:1/relay", client_name="x", pings=0)

        run(scenario())

    def test_probe_reports_unreachable_relays_as_network_errors(self) -> None:
        async def scenario() -> None:
            with pytest.raises(TransportError, match="Could not reach"):
                await probe_relay(
                    "ws://127.0.0.1:1/relay",
                    client_name="GhostLink/probe",
                    config=FAST,
                    pings=1,
                )

        run(scenario())
