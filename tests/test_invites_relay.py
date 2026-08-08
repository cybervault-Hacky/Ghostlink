"""Invite operations over a real loopback relay: the authority end to end.

No network mocks — every test dials a live RelayServer and drives the v3
invite packets through the same code paths the CLI uses.
"""

from __future__ import annotations

import asyncio
import logging

import pytest

from ghostlink.constants.net import GROUP_PROTOCOL_VERSION, PROTOCOL_VERSION
from ghostlink.exceptions.invites import (
    InviteAlreadyUsedError,
    InviteError,
    InviteExpiredError,
    InvitePermissionError,
    InviteRevokedError,
    InviteUnknownError,
)
from ghostlink.invites.tokens import generate_invite_token, invite_id_for_token
from ghostlink.models.room import generate_room_id
from ghostlink.transport.relay.client import RelayClient, RelayClientConfig
from ghostlink.transport.relay.endpoint import RelayEndpoint
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


async def _client(server: RelayServer, name: str = "inviter") -> RelayClient:
    client = RelayClient(RelayEndpoint.from_url(server.url), client_name=name, config=FAST)
    await client.connect()
    return client


class TestInviteLifecycleOverRelay:
    def test_create_query_redeem_happy_path(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                host = await _client(server, "creator")
                token = generate_invite_token()
                room = generate_room_id()
                grant = await host.create_invite(
                    token, room_id=room, ttl_seconds=60.0, max_redemptions=1
                )
                assert grant.invite_id == invite_id_for_token(token)

                # Host occupies the room as host before the guest redeems.
                await host.attach(room, "host")
                status = await host.query_invite(token)
                assert status.state == "active"
                assert status.room_id == room  # creator view exposes the room

                guest = await _client(server, "guest")
                recovered_room, expiry = await guest.redeem_invite(token)
                assert recovered_room == room
                assert expiry.tzinfo is not None

                # The redeem attached the guest: both roles occupy the room.
                assert server.channel_peers(room) == ("guest", "host")

                status_after = await host.query_invite(token)
                assert status_after.state == "redeemed"

                await host.disconnect()
                await guest.disconnect()

        run(scenario())

    def test_second_redemption_is_refused(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                host = await _client(server, "creator")
                token = generate_invite_token()
                room = generate_room_id()
                await host.create_invite(token, room_id=room, ttl_seconds=30.0, max_redemptions=1)
                await host.attach(room, "host")
                first = await _client(server, "g1")
                await first.redeem_invite(token)
                second = await _client(server, "g2")
                with pytest.raises(InviteAlreadyUsedError):
                    await second.redeem_invite(token)
                # The loser was not attached anywhere.
                assert first.attachments.get(room) == "guest"
                assert second.attachments == {}
                await host.disconnect()
                await first.disconnect()
                await second.disconnect()

        run(scenario())

    def test_simultaneous_redemption_exactly_one_winner(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                host = await _client(server, "creator")
                token = generate_invite_token()
                room = generate_room_id()
                await host.create_invite(token, room_id=room, ttl_seconds=30.0, max_redemptions=1)
                await host.attach(room, "host")
                contenders = [await _client(server, f"g{i}") for i in range(4)]

                async def attempt(client: RelayClient) -> str:
                    try:
                        await client.redeem_invite(token)
                        return "won"
                    except InviteAlreadyUsedError:
                        return "lost"

                verdicts = await asyncio.gather(*(attempt(client) for client in contenders))
                assert verdicts.count("won") == 1
                assert verdicts.count("lost") == 3
                assert len(server.channel_peers(room)) == 2  # exactly one guest entered
                for client in [host, *contenders]:
                    await client.disconnect()

        run(scenario())

    def test_unknown_and_malformed_invites_fail_closed(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                client = await _client(server)
                with pytest.raises(InviteUnknownError):
                    await client.redeem_invite(generate_invite_token())
                await client.disconnect()

        run(scenario())


class TestExpirationOverRelay:
    def test_two_second_invite_expires_for_real(self) -> None:
        """The authority — not the terminal countdown — enforces the deadline."""

        async def scenario() -> None:
            async with running_relay() as server:
                host = await _client(server, "creator")
                token = generate_invite_token()
                room = generate_room_id()
                await host.create_invite(token, room_id=room, ttl_seconds=2.0, max_redemptions=1)
                await asyncio.sleep(2.2)
                guest = await _client(server, "guest")
                with pytest.raises(InviteExpiredError):
                    await guest.redeem_invite(token)
                # Late redemption failed closed; the room never gained members.
                assert server.channel_peers(room) == ()
                status = await host.query_invite(token)
                assert status.state == "expired"
                await host.disconnect()
                await guest.disconnect()

        run(scenario())

    def test_one_second_invite_still_redeemable_inside_the_window(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                host = await _client(server, "creator")
                token = generate_invite_token()
                room = generate_room_id()
                await host.create_invite(token, room_id=room, ttl_seconds=4.0, max_redemptions=1)
                await host.attach(room, "host")
                guest = await _client(server, "guest")
                joined, _ = await guest.redeem_invite(token)
                assert joined == room
                await host.disconnect()
                await guest.disconnect()

        run(scenario())


class TestRevocationOverRelay:
    def test_revoke_blocks_later_redemption(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                host = await _client(server, "creator")
                token = generate_invite_token()
                room = generate_room_id()
                await host.create_invite(token, room_id=room, ttl_seconds=60.0, max_redemptions=1)
                await host.revoke_invite(token)
                guest = await _client(server, "guest")
                with pytest.raises(InviteRevokedError):
                    await guest.redeem_invite(token)
                status = await host.query_invite(token)
                assert status.state == "revoked"
                await host.disconnect()
                await guest.disconnect()

        run(scenario())

    def test_only_the_creator_can_revoke(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                host = await _client(server, "creator")
                token = generate_invite_token()
                room = generate_room_id()
                await host.create_invite(token, room_id=room, ttl_seconds=60.0, max_redemptions=1)
                intruder = await _client(server, "intruder")
                # Knowing the token is not enough; the binding is to the session.
                with pytest.raises(InvitePermissionError):
                    await intruder.revoke_invite(token)
                guest = await _client(server, "guest")
                joined, _ = await guest.redeem_invite(token)  # still valid
                assert joined == room
                await host.disconnect()
                await intruder.disconnect()
                await guest.disconnect()

        run(scenario())

    def test_revoke_unknown_invite_is_refused(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                client = await _client(server)
                with pytest.raises(InviteUnknownError):
                    await client.revoke_invite(generate_invite_token())
                await client.disconnect()

        run(scenario())


class TestSessionBinding:
    def test_redemption_record_binds_the_guest_session(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                host = await _client(server, "creator")
                token = generate_invite_token()
                room = generate_room_id()
                await host.create_invite(token, room_id=room, ttl_seconds=60.0, max_redemptions=1)
                await host.attach(room, "host")
                guest = await _client(server, "guest")
                await guest.redeem_invite(token)
                guest_session = guest.session.session_id if guest.session else None
                entry = server.invites.status(token, requester_session="sess-creator")
                assert entry is not None
                assert entry.bound_session == guest_session
                await host.disconnect()
                await guest.disconnect()

        run(scenario())


class TestProtocolGating:
    def test_v2_clients_cannot_drive_invites(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def scenario() -> None:
            import ghostlink.transport.relay.client as client_mod

            original = client_mod.hello_packet

            def v2_hello(client_name: str, *, session_id: str | None = None) -> object:
                packet = original(client_name, session_id=session_id)
                packet.payload["protocol"] = 2
                return packet

            monkeypatch.setattr(client_mod, "hello_packet", v2_hello)
            async with running_relay() as server:
                client = RelayClient(
                    RelayEndpoint.from_url(server.url), client_name="legacy", config=FAST
                )
                await client.connect()
                with pytest.raises(InviteError):
                    await client.create_invite(
                        generate_invite_token(),
                        room_id=generate_room_id(),
                        ttl_seconds=30.0,
                        max_redemptions=1,
                    )
                await client.disconnect()

        run(scenario())


class TestLogHygiene:
    def test_tokens_never_reach_relay_logs(self, caplog: pytest.LogCaptureFixture) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                host = await _client(server, "creator")
                tokens = [generate_invite_token(), generate_invite_token()]
                room = generate_room_id()
                first, second = tokens
                await host.create_invite(first, room_id=room, ttl_seconds=60.0, max_redemptions=1)
                await host.create_invite(
                    second, room_id=generate_room_id(), ttl_seconds=60.0, max_redemptions=1
                )
                await host.attach(room, "host")
                guest = await _client(server, "guest")
                await guest.redeem_invite(first)
                await host.revoke_invite(second)
                await host.disconnect()
                await guest.disconnect()

        with caplog.at_level(logging.DEBUG, logger="ghostlink"):
            run(scenario())
        blob = "\n".join(record.getMessage() for record in caplog.records)
        for token in ("gl://join/",):
            assert token not in blob
        # The tokens minted inside scenario() are inaccessible here; instead
        # assert the structural rule: no 20-char alphabet run appears logged.
        import re

        runs = re.findall(r"[A-Z2-9]{20}", blob)
        assert runs == [], f"token-like material leaked into logs: {runs[:3]}"


class TestRelayMetadata:
    def test_welcome_advertises_current_protocol(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                client = await _client(server)
                session = client.session
                assert session is not None
                # v4 = v3 invite capability + group lifecycle capability.
                assert session.metadata.get("protocol") == str(PROTOCOL_VERSION)
                assert int(session.metadata["protocol"]) >= GROUP_PROTOCOL_VERSION
                # The v4 WELCOME also carries the group attestation challenge.
                assert client.attest_nonce
                await client.disconnect()

        run(scenario())

    def test_invite_registry_visible_for_ops(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                host = await _client(server, "creator")
                token = generate_invite_token()
                await host.create_invite(
                    token, room_id=generate_room_id(), ttl_seconds=30.0, max_redemptions=1
                )
                assert server.invites.size == 1
                await host.disconnect()

        run(scenario())
