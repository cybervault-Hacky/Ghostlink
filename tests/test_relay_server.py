"""RelayServer: lifecycle, session tracking, expiry, protocol enforcement."""

from __future__ import annotations

import asyncio

from ghostlink.transport.relay.client import RelayClient, RelayClientConfig
from ghostlink.transport.relay.endpoint import RelayEndpoint
from ghostlink.transport.relay.protocol import (
    PacketType,
    decode_packet,
    encode_packet,
    ping_packet,
)
from ghostlink.transport.relay.server import RelayServer, main
from ghostlink.transport.session import SessionStatus
from ghostlink.transport.websocket.transport import WebSocketTransport
from tests.conftest import run, running_relay

FAST = RelayClientConfig(
    connect_timeout_seconds=2.0,
    handshake_timeout_seconds=2.0,
    heartbeat_interval_seconds=3600.0,
    heartbeat_timeout_seconds=1.0,
    reconnect_attempts=0,
    reconnect_base_delay_seconds=0.05,
)


class TestLifecycle:
    def test_start_binds_and_reports_url(self) -> None:
        async def scenario() -> tuple[str, int]:
            server = RelayServer(host="127.0.0.1", port=0)
            await server.start()
            try:
                assert server.url.startswith("ws://127.0.0.1:")
                assert server.url.endswith("/relay")
                assert server.port > 0
                assert server.client_count == 0
                return server.url, server.client_count
            finally:
                await server.aclose()

        url, count = run(scenario())
        assert "/relay" in url and count == 0

    def test_context_manager_starts_and_stops(self) -> None:
        async def scenario() -> int:
            async with RelayServer(host="127.0.0.1", port=0) as server:
                return server.client_count

        assert run(scenario()) == 0


class TestSessions:
    def test_attached_clients_register_sessions_with_metadata(self) -> None:
        async def scenario() -> dict[str, str]:
            async with running_relay(server_name="relay-a") as server:
                client = RelayClient(
                    RelayEndpoint.from_url(server.url),
                    client_name="GhostLink/inspect",
                    config=FAST,
                )
                await client.connect()
                try:
                    assert server.sessions.count() == 1
                    session = server.sessions.sessions()[0]
                    assert session.status is SessionStatus.ACTIVE
                    assert session.metadata["client"] == "GhostLink/inspect"
                    assert session.metadata["server"] == "relay-a"
                    assert session.ttl_seconds is not None
                    return session.metadata
                finally:
                    await client.disconnect()

        metadata = run(scenario())
        assert metadata["client"] == "GhostLink/inspect"

    def test_two_concurrent_clients_tracked_independently(self) -> None:
        async def scenario() -> list[str]:
            async with running_relay() as server:
                first = RelayClient(
                    RelayEndpoint.from_url(server.url), client_name="GhostLink/a", config=FAST
                )
                second = RelayClient(
                    RelayEndpoint.from_url(server.url), client_name="GhostLink/b", config=FAST
                )
                first_session = await first.connect()
                second_session = await second.connect()
                try:
                    assert server.client_count == 2
                    assert server.sessions.count() == 2
                    assert first_session.session_id != second_session.session_id
                    return [session.session_id for session in server.sessions.sessions()]
                finally:
                    await first.disconnect()
                    await second.disconnect()

        registered = run(scenario())
        assert len(set(registered)) == 2

    def test_idle_sessions_are_expired_by_the_sweeper(self) -> None:
        async def scenario() -> tuple[int, int]:
            async with running_relay(session_ttl_seconds=0.05) as server:
                client = RelayClient(
                    RelayEndpoint.from_url(server.url),
                    client_name="GhostLink/idle",
                    config=FAST,
                )
                await client.connect()
                try:
                    assert server.sessions.count() == 1
                    await asyncio.sleep(0.08)  # session now exceeds its TTL
                    swept = server.sessions.sweep_expired()
                    return len(swept), server.sessions.total_expired
                finally:
                    await client.disconnect()

        swept, total = run(scenario())
        assert swept == 1
        assert total == 1

    def test_activity_keeps_sessions_alive(self) -> None:
        async def scenario() -> int:
            async with running_relay(session_ttl_seconds=0.10) as server:
                client = RelayClient(
                    RelayEndpoint.from_url(server.url),
                    client_name="GhostLink/active",
                    config=FAST,
                )
                await client.connect()
                try:
                    assert server.sessions.count() == 1
                    await asyncio.sleep(0.06)
                    await client.ping()  # refreshes the server-side activity clock
                    await asyncio.sleep(0.06)
                    return len(server.sessions.sweep_expired())
                finally:
                    await client.disconnect()

        assert run(scenario()) == 0


class TestProtocolEnforcement:
    def test_first_packet_must_be_hello(self) -> None:
        async def scenario() -> PacketType | None:
            async with running_relay() as server:
                endpoint = RelayEndpoint.from_url(server.url)
                transport = WebSocketTransport.dial(
                    endpoint.host,
                    endpoint.port,
                    resource=endpoint.resource,
                    secure=False,
                    timeout_seconds=2.0,
                )
                await transport.open()
                try:
                    await transport.send(encode_packet(ping_packet("skip-hello")))
                    raw = await asyncio.wait_for(transport.receive(), timeout=2.0)
                    assert raw is not None
                    packet = decode_packet(raw, peer="server")
                    assert packet.type is PacketType.ERROR
                    assert packet.payload["code"] == "protocol/expected-hello"
                    return packet.type
                finally:
                    await transport.close()

        assert run(scenario()) is PacketType.ERROR

    def test_server_disconnects_politely_on_client_goodbye(self) -> None:
        async def scenario() -> int:
            async with running_relay() as server:
                client = RelayClient(
                    RelayEndpoint.from_url(server.url),
                    client_name="GhostLink/goodbye",
                    config=FAST,
                )
                await client.connect()
                assert server.client_count == 1
                await client.disconnect()
                loop = asyncio.get_running_loop()
                deadline = loop.time() + 2.0
                while server.client_count and loop.time() < deadline:
                    await asyncio.sleep(0.01)
                return server.client_count

        assert run(scenario()) == 0


class TestModuleEntry:
    def test_parser_rejects_invalid_port(self) -> None:
        import pytest

        with pytest.raises(SystemExit) as captured:
            main(["--port", "not-a-number"])
        assert captured.value.code == 2
