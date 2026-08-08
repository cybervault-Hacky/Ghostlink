"""Identity keys in the chat handshake: transcript binding + peer GLFP."""

from __future__ import annotations

import pytest

from ghostlink.identity import LocalIdentity, identity_fingerprint
from ghostlink.messaging.packets.frames import (
    Frame,
    FrameType,
    decode_frame,
    encode_frame,
    kex_hello_frame,
)
from ghostlink.messaging.protocol.handshake import HandshakeInitiator, HandshakeResponder
from ghostlink.messaging.session.chat import ChatEventKind, ChatSession
from ghostlink.models.room import generate_room_id
from ghostlink.transport.relay.client import RelayClient, RelayClientConfig
from ghostlink.transport.relay.endpoint import RelayEndpoint
from ghostlink.transport.relay.server import RelayServer
from tests.conftest import run, running_relay
from tests.test_chat_session import CHAT_FAST, FAST, Pair, _wait_for

HOST_IDENTITY = LocalIdentity.generate(nickname="Nova")
GUEST_IDENTITY = LocalIdentity.generate(nickname="Ravi")


async def _client(server: RelayServer, name: str, config: RelayClientConfig = FAST) -> RelayClient:
    client = RelayClient(RelayEndpoint.from_url(server.url), client_name=name, config=config)
    await client.connect()
    return client


async def _make_identity_pair(
    server: RelayServer,
    *,
    host_identity: LocalIdentity | None = HOST_IDENTITY,
    guest_identity: LocalIdentity | None = GUEST_IDENTITY,
) -> Pair:
    channel = generate_room_id()
    host_client = await _client(server, "host")
    guest_client = await _client(server, "guest")
    host_events: list = []
    guest_events: list = []
    host = ChatSession(
        host_client,
        channel=channel,
        role="host",
        display_name="Nova",
        config=CHAT_FAST,
        identity_public_key_hex=(
            host_identity.public_key_hex if host_identity is not None else None
        ),
    )
    guest = ChatSession(
        guest_client,
        channel=channel,
        role="guest",
        display_name="Ravi",
        config=CHAT_FAST,
        identity_public_key_hex=(
            guest_identity.public_key_hex if guest_identity is not None else None
        ),
    )
    host.add_listener(host_events.append)
    guest.add_listener(guest_events.append)
    await host.start()
    await guest.start()
    await host.wait_ready(timeout_seconds=5.0)
    await guest.wait_ready(timeout_seconds=5.0)
    return Pair(host, guest, host_client, guest_client, host_events, guest_events, channel)


class TestHandshakeIdentity:
    def test_idpub_enters_payload_only_when_configured(self) -> None:
        with_id = HandshakeInitiator(
            "gl-room-AAAA-BBBB-CCCC", display_name="a", identity_public_key_hex="ab" * 32
        )
        without_id = HandshakeInitiator("gl-room-AAAA-BBBB-CCCC", display_name="a")
        assert with_id.hello_payload()["idpub"] == "ab" * 32
        assert "idpub" not in without_id.hello_payload()

    def test_identity_bound_flag_requires_both_sides(self) -> None:
        channel = "gl-room-AAAA-BBBB-CCCC"
        initiator = HandshakeInitiator(channel, identity_public_key_hex="ab" * 32)
        responder = HandshakeResponder(channel)  # no identity on this side
        reply, secure_guest = responder.answer(initiator.hello_payload())
        secure_host = initiator.complete(reply)
        assert secure_host.identity_bound is False
        assert secure_guest.identity_bound is False
        assert secure_host.session_key == secure_guest.session_key

    def test_both_sides_bound_then_match(self) -> None:
        channel = "gl-room-AAAA-BBBB-CCCC"
        initiator = HandshakeInitiator(channel, identity_public_key_hex="ab" * 32)
        responder = HandshakeResponder(channel, identity_public_key_hex="cd" * 32)
        reply, secure_guest = responder.answer(initiator.hello_payload())
        secure_host = initiator.complete(reply)
        assert secure_host.identity_bound is True
        assert secure_guest.identity_bound is True
        assert secure_host.transcript == secure_guest.transcript

    def test_tampered_idpub_breaks_the_proof(self) -> None:
        """A relay swapping an identity key flips the transcript → loud failure."""
        channel = "gl-room-AAAA-BBBB-CCCC"
        initiator = HandshakeInitiator(channel, identity_public_key_hex="ab" * 32)
        responder = HandshakeResponder(channel, identity_public_key_hex="cd" * 32)
        hello = initiator.hello_payload()
        hello["idpub"] = "ef" * 32  # MITM substitute
        reply, _ = responder.answer(hello)
        from ghostlink.exceptions.messaging import HandshakeFailedError

        with pytest.raises(HandshakeFailedError):
            initiator.complete(reply)

    def test_frames_accept_a_valid_idpub(self) -> None:
        frame = decode_frame(
            encode_frame(
                kex_hello_frame({"pub": "aa" * 32, "nonce": "bb" * 16, "idpub": "cc" * 32})
            )
        )
        assert frame.type is FrameType.KEX_HELLO
        assert frame.data["idpub"] == "cc" * 32

    def test_frames_reject_a_short_idpub(self) -> None:
        from ghostlink.exceptions.messaging import MessageValidationError

        with pytest.raises(MessageValidationError):
            frame = Frame(
                FrameType.KEX_HELLO, {"pub": "aa" * 32, "nonce": "bb" * 16, "idpub": "cc" * 31}
            )
            encode_frame(frame)


class TestSessionIdentity:
    def test_peers_report_each_others_fingerprint(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                pair = await _make_identity_pair(server)
                try:
                    assert pair.host.identity_bound
                    assert pair.guest.identity_bound
                    assert pair.host.peer_identity_fingerprint == identity_fingerprint(
                        GUEST_IDENTITY.public_key_bytes
                    )
                    assert pair.guest.peer_identity_fingerprint == identity_fingerprint(
                        HOST_IDENTITY.public_key_bytes
                    )
                    # The UI notice line appears on both consoles.
                    notices = [
                        event.detail
                        for event in pair.host_events
                        if event.kind is ChatEventKind.NOTICE
                    ]
                    assert any("Peer identity:" in line for line in notices)
                finally:
                    await pair.close()

        run(scenario())

    def test_without_identity_keys_nothing_changes(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                pair = await _make_identity_pair(server, host_identity=None, guest_identity=None)
                try:
                    assert not pair.host.identity_bound
                    assert pair.host.peer_identity_fingerprint is None
                    assert pair.guest.peer_identity_fingerprint is None
                    # …and messaging keeps working, byte for byte.
                    await pair.host.send_message("identity-free hello")
                    await _wait_for(
                        lambda: any(
                            event.kind is ChatEventKind.MESSAGE
                            and event.message is not None
                            and event.message.text == "identity-free hello"
                            for event in pair.guest_events
                        )
                    )
                finally:
                    await pair.close()

        run(scenario())

    def test_identity_notice_uses_peer_display_name(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                pair = await _make_identity_pair(server)
                try:
                    notices = [
                        event.detail
                        for event in pair.guest_events
                        if event.kind is ChatEventKind.NOTICE
                    ]
                    assert any("(Nova)" in line for line in notices)
                finally:
                    await pair.close()

        run(scenario())
