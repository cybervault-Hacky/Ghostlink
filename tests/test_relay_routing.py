"""Relay rendezvous routing (protocol v2): ATTACH/DETACH/FORWARD/PEER."""

from __future__ import annotations

import asyncio

import pytest

from ghostlink.exceptions.transport import RelayError, TransportError
from ghostlink.models.room import generate_room_id
from ghostlink.transport.relay.client import RelayClient, RelayClientConfig
from ghostlink.transport.relay.endpoint import RelayEndpoint
from ghostlink.transport.relay.protocol import (
    attach_packet,
    forward_packet,
    hello_packet,
)
from ghostlink.transport.relay.server import RelayServer
from tests.conftest import run, running_relay

FAST = RelayClientConfig(
    connect_timeout_seconds=2.0,
    handshake_timeout_seconds=2.0,
    heartbeat_interval_seconds=3600.0,
    heartbeat_timeout_seconds=1.0,
    reconnect_attempts=0,
    reconnect_base_delay_seconds=0.05,
)


async def _client(server: RelayServer, name: str) -> RelayClient:
    client = RelayClient(RelayEndpoint.from_url(server.url), client_name=name, config=FAST)
    await client.connect()
    return client


async def _wait_for(predicate: object, timeout: float = 2.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within the deadline")


class TestAttach:
    def test_first_peer_gets_zero_second_gets_one(self) -> None:
        async def scenario() -> tuple[int, int]:
            async with running_relay() as server:
                channel = generate_room_id()
                host = await _client(server, "host")
                guest = await _client(server, "guest")
                try:
                    first = await host.attach(channel, "host")
                    second = await guest.attach(channel, "guest")
                    return first, second
                finally:
                    await host.disconnect()
                    await guest.disconnect()

        first, second = run(scenario())
        assert first == 0
        assert second == 1

    def test_attach_rejects_bad_role(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                client = await _client(server, "x")
                try:
                    with pytest.raises(RelayError, match="'host' or 'guest'"):
                        await client.attach(generate_room_id(), "admin")
                finally:
                    await client.disconnect()

        run(scenario())

    def test_attach_rejects_malformed_channel(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                client = await _client(server, "x")
                try:
                    with pytest.raises(RelayError, match="not a valid room identifier"):
                        await client.attach("gl-room-BAD!-XXXX-XXXX", "host")
                finally:
                    await client.disconnect()

        run(scenario())

    def test_attach_requires_connection(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                client = RelayClient(
                    RelayEndpoint.from_url(server.url), client_name="x", config=FAST
                )
                with pytest.raises(TransportError, match="Cannot attach"):
                    await client.attach(generate_room_id(), "host")

        run(scenario())

    def test_third_attachment_is_refused_with_scoped_error(self) -> None:
        async def scenario() -> str:
            async with running_relay() as server:
                channel = generate_room_id()
                host = await _client(server, "host")
                guest = await _client(server, "guest")
                third = await _client(server, "third")
                try:
                    await host.attach(channel, "host")
                    await guest.attach(channel, "guest")
                    with pytest.raises(RelayError, match="refused attachment"):
                        await third.attach(channel, "host")
                    assert server.channel_peers(channel) == ("guest", "host")
                    assert third.error_packets[-1].payload["code"] == "relay/channel-full"
                    return str(third.error_packets[-1].payload["channel"])
                finally:
                    await host.disconnect()
                    await guest.disconnect()
                    await third.disconnect()

        channel = run(scenario())
        assert channel.startswith("gl-room-")


class TestForwarding:
    def test_forward_round_trip_both_ways(self) -> None:
        async def scenario() -> tuple[list[tuple[str, str]], list[tuple[str, str]], str]:
            async with running_relay() as server:
                channel = generate_room_id()
                host = await _client(server, "host")
                guest = await _client(server, "guest")
                host_inbox: list[tuple[str, str]] = []
                guest_inbox: list[tuple[str, str]] = []
                host.set_forward_listener(lambda ch, body: host_inbox.append((ch, body)))
                guest.set_forward_listener(lambda ch, body: guest_inbox.append((ch, body)))
                try:
                    await host.attach(channel, "host")
                    await guest.attach(channel, "guest")
                    await host.send_forward(channel, "aGVsbG8=")  # "hello" b64
                    await guest.send_forward(channel, "d29ybGQ=")  # "world" b64
                    await _wait_for(lambda: len(guest_inbox) == 1 and len(host_inbox) == 1)
                    return host_inbox, guest_inbox, channel
                finally:
                    await host.disconnect()
                    await guest.disconnect()

        host_inbox, guest_inbox, channel = run(scenario())
        assert guest_inbox == [(channel, "aGVsbG8=")]
        assert host_inbox == [(channel, "d29ybGQ=")]

    def test_forward_without_attach_is_an_error(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                client = await _client(server, "x")
                try:
                    channel = generate_room_id()
                    await client.send_forward(channel, "aGk=")
                    await _wait_for(lambda: len(client.error_packets) == 1)
                    assert client.error_packets[-1].payload["code"] == "relay/not-attached"
                finally:
                    await client.disconnect()

        run(scenario())

    def test_forward_with_no_peer_is_an_error(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                client = await _client(server, "x")
                try:
                    channel = generate_room_id()
                    await client.attach(channel, "host")
                    await client.send_forward(channel, "aGk=")
                    await _wait_for(lambda: len(client.error_packets) == 1)
                    assert client.error_packets[-1].payload["code"] == "relay/no-peer"
                finally:
                    await client.disconnect()

        run(scenario())


class TestPeerEvents:
    def test_peer_join_and_leave_notifications(self) -> None:
        async def scenario() -> list[str]:
            async with running_relay() as server:
                channel = generate_room_id()
                host = await _client(server, "host")
                guest = await _client(server, "guest")
                events: list[str] = []
                host.set_peer_listener(lambda _ch, event: events.append(event))
                try:
                    await host.attach(channel, "host")
                    await guest.attach(channel, "guest")
                    await _wait_for(lambda: "joined" in events)
                    await guest.detach(channel)
                    await _wait_for(lambda: "left" in events)
                    return events
                finally:
                    await host.disconnect()
                    await guest.disconnect()

        assert run(scenario()) == ["joined", "left"]

    def test_peer_leave_on_disconnect_cleans_the_channel(self) -> None:
        async def scenario() -> tuple[list[str], int]:
            async with running_relay() as server:
                channel = generate_room_id()
                host = await _client(server, "host")
                guest = await _client(server, "guest")
                events: list[str] = []
                host.set_peer_listener(lambda _ch, event: events.append(event))
                try:
                    await host.attach(channel, "host")
                    await guest.attach(channel, "guest")
                    assert server.channel_count == 1
                    await guest.disconnect()
                    await _wait_for(lambda: "left" in events)
                    assert server.channel_peers(channel) == ("host",)
                    await host.disconnect()
                    await _wait_for(lambda: server.channel_count == 0)
                    return events, server.channel_count
                finally:
                    await host.disconnect()

        events, channels = run(scenario())
        assert events == ["joined", "left"]
        assert channels == 0


class TestRecovery:
    def test_attachments_are_reestablished_after_reconnect(self) -> None:
        resilient = RelayClientConfig(
            connect_timeout_seconds=2.0,
            handshake_timeout_seconds=2.0,
            heartbeat_interval_seconds=3600.0,
            heartbeat_timeout_seconds=1.0,
            reconnect_attempts=5,
            reconnect_base_delay_seconds=0.05,
        )

        async def scenario() -> tuple[tuple[str, ...], list[tuple[str, str]]]:
            server = RelayServer(host="127.0.0.1", port=0)
            await server.start()
            port = server.port

            def make_client(name: str) -> RelayClient:
                return RelayClient(
                    RelayEndpoint.from_url(server.url), client_name=name, config=resilient
                )

            host = make_client("host")
            guest = make_client("guest")
            await host.connect()
            await guest.connect()
            channel = generate_room_id()
            guest_inbox: list[tuple[str, str]] = []
            guest.set_forward_listener(lambda ch, body: guest_inbox.append((ch, body)))
            try:
                await host.attach(channel, "host")
                await guest.attach(channel, "guest")
                assert server.channel_peers(channel) == ("guest", "host")

                await server.aclose()  # simulate relay outage
                revived = RelayServer(host="127.0.0.1", port=port)
                await revived.start()
                try:
                    # both clients reconnect on their own policy and re-attach
                    await _wait_for(
                        lambda: revived.channel_peers(channel) == ("guest", "host"),
                        timeout=5.0,
                    )
                    await host.send_forward(channel, "cmVjb25uZWN0")  # "reconnect"
                    await _wait_for(lambda: len(guest_inbox) == 1, timeout=5.0)
                finally:
                    await revived.aclose()
                return revived.channel_peers(channel), guest_inbox
            finally:
                await host.disconnect()
                await guest.disconnect()

        _peers, guest_inbox = run(scenario())
        assert guest_inbox[0][1] == "cmVjb25uZWN0"


class TestValidation:
    def test_attach_packet_builders_validate(self) -> None:
        channel = generate_room_id()
        from ghostlink.transport.relay.protocol import encode_packet

        encode_packet(attach_packet(channel, "host"))
        encode_packet(forward_packet(channel, "aGk="))

    def test_forward_body_must_be_base64(self) -> None:
        from ghostlink.exceptions.transport import PacketValidationError
        from ghostlink.transport.relay.protocol import PacketType, validate_packet

        channel = generate_room_id()
        with pytest.raises(PacketValidationError, match="base64"):
            validate_packet(PacketType.FORWARD, {"channel": channel, "body": "!!!"})

    def test_peer_event_must_be_known(self) -> None:
        from ghostlink.exceptions.transport import PacketValidationError
        from ghostlink.transport.relay.protocol import PacketType, validate_packet

        channel = generate_room_id()
        with pytest.raises(PacketValidationError, match="'event'"):
            validate_packet(PacketType.PEER, {"channel": channel, "event": "vanished"})

    def test_hello_builder_still_works(self) -> None:
        from ghostlink.transport.relay.protocol import encode_packet

        encode_packet(hello_packet("GhostLink/test"))
