"""Chat session end-to-end: handshake, lifecycle, ordering, duplicates,
encryption failure handling, reconnect resend, and cleanup (Phase 3).

Every test runs against a real relay server on loopback — the network is
never mocked.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from ghostlink.exceptions.messaging import (
    PeerUnavailableError,
    SecureChannelError,
)
from ghostlink.messaging.history import SessionHistory
from ghostlink.messaging.models.message import MessageStatus
from ghostlink.messaging.packets.frames import decode_frame, encode_frame, message_frame
from ghostlink.messaging.session.chat import (
    ChatEvent,
    ChatEventKind,
    ChatSession,
    ChatSessionConfig,
)
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

RECONNECTING = RelayClientConfig(
    connect_timeout_seconds=2.0,
    handshake_timeout_seconds=2.0,
    heartbeat_interval_seconds=3600.0,
    heartbeat_timeout_seconds=1.0,
    reconnect_attempts=12,
    reconnect_base_delay_seconds=0.05,
)

CHAT_FAST = ChatSessionConfig(
    handshake_timeout_seconds=5.0,
    ack_timeout_seconds=2.0,
    resend_attempts=3,
    attach_timeout_seconds=3.0,
    reorder_window_seconds=0.2,
)


async def _client(server: RelayServer, name: str, config: RelayClientConfig = FAST) -> RelayClient:
    client = RelayClient(RelayEndpoint.from_url(server.url), client_name=name, config=config)
    await client.connect()
    return client


async def _wait_for(predicate: object, timeout: float = 3.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within the deadline")


class Pair:
    def __init__(
        self,
        host: ChatSession,
        guest: ChatSession,
        host_client: RelayClient,
        guest_client: RelayClient,
        host_events: list[ChatEvent],
        guest_events: list[ChatEvent],
        channel: str,
    ) -> None:
        self.host = host
        self.guest = guest
        self.host_client = host_client
        self.guest_client = guest_client
        self.host_events = host_events
        self.guest_events = guest_events
        self.channel = channel

    async def close(self) -> None:
        await self.host.close()
        await self.guest.close()
        await self.host_client.disconnect()
        await self.guest_client.disconnect()


async def make_pair(
    server: RelayServer,
    *,
    config: ChatSessionConfig = CHAT_FAST,
    client_config: RelayClientConfig = FAST,
    host_history: SessionHistory | None = None,
    guest_history: SessionHistory | None = None,
    guest_config: ChatSessionConfig | None = None,
) -> Pair:
    channel = generate_room_id()
    host_client = await _client(server, "host", client_config)
    guest_client = await _client(server, "guest", client_config)
    host_events: list[ChatEvent] = []
    guest_events: list[ChatEvent] = []
    host = ChatSession(
        host_client,
        channel=channel,
        role="host",
        display_name="Nova",
        config=config,
        history=host_history,
    )
    guest = ChatSession(
        guest_client,
        channel=channel,
        role="guest",
        display_name="Ravi",
        config=guest_config or config,
        history=guest_history,
    )
    host.add_listener(host_events.append)
    guest.add_listener(guest_events.append)
    await host.start()
    await guest.start()
    await host.wait_ready(timeout_seconds=5.0)
    await guest.wait_ready(timeout_seconds=5.0)
    return Pair(host, guest, host_client, guest_client, host_events, guest_events, channel)


def _events(events: list[ChatEvent], kind: ChatEventKind) -> list[ChatEvent]:
    return [event for event in events if event.kind is kind]


# ------------------------------------------------------------------ handshake


class TestSecureChannel:
    def test_handshake_establishes_matching_sessions(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                pair = await make_pair(server)
                try:
                    assert pair.host.fingerprint == pair.guest.fingerprint
                    assert pair.host.conversation_id == pair.guest.conversation_id
                    conversation = pair.host.conversation_id
                    assert conversation and conversation.startswith("conv_")
                    assert pair.host.is_ready and pair.guest.is_ready
                    assert pair.host.peer_name == "Ravi"
                    assert pair.guest.peer_name == "Nova"
                    ready_events = _events(pair.host_events, ChatEventKind.SESSION)
                    assert any("Encryption: Active" in e.detail for e in ready_events)
                finally:
                    await pair.close()

        run(scenario())

    def test_send_before_ready_raises_clear_error(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                client = await _client(server, "lonely")
                session = ChatSession(
                    client,
                    channel=generate_room_id(),
                    role="guest",
                    display_name="Nova",
                    config=CHAT_FAST,
                )
                await session.start()
                try:
                    with pytest.raises(SecureChannelError, match="not ready"):
                        await session.send_message("too early")
                    with pytest.raises(PeerUnavailableError, match="not established"):
                        await session.wait_ready(timeout_seconds=0.3)
                finally:
                    await session.close()
                    await client.disconnect()

        run(scenario())


# --------------------------------------------------------------- lifecycle


class TestMessageLifecycle:
    def test_host_to_guest_full_state_flow(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                pair = await make_pair(server)
                try:
                    message = await pair.host.send_message("hello Ravi")
                    assert message.status is MessageStatus.QUEUED
                    await _wait_for(
                        lambda: (
                            pair.host.outbox.get(message.message_id) is not None
                            and pair.host.outbox.get(message.message_id).status  # type: ignore[union-attr]
                            is MessageStatus.DELIVERED
                        )
                    )
                    received = _events(pair.guest_events, ChatEventKind.MESSAGE)
                    assert len(received) == 1
                    assert received[0].message is not None
                    assert received[0].message.text == "hello Ravi"
                    assert received[0].message.sender == "Nova"
                    assert received[0].message.recipient == "Ravi"
                    assert pair.guest.unread_count == 1

                    await pair.guest.mark_read()
                    await _wait_for(
                        lambda: (
                            pair.host.outbox.get(message.message_id) is not None
                            and pair.host.outbox.get(message.message_id).status  # type: ignore[union-attr]
                            is MessageStatus.READ
                        )
                    )
                    assert pair.guest.unread_count == 0
                    states = [
                        e.message.status
                        for e in _events(pair.host_events, ChatEventKind.DELIVERY)
                        if e.message is not None
                    ]
                    assert MessageStatus.SENT in states
                    assert MessageStatus.DELIVERED in states
                    assert MessageStatus.READ in states
                finally:
                    await pair.close()

        run(scenario())

    def test_guest_to_host_reply(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                pair = await make_pair(server)
                try:
                    await pair.host.send_message("ping")
                    await _wait_for(
                        lambda: len(_events(pair.guest_events, ChatEventKind.MESSAGE)) == 1
                    )
                    await pair.guest.send_message("pong")
                    await _wait_for(
                        lambda: len(_events(pair.host_events, ChatEventKind.MESSAGE)) == 1
                    )
                    reply = _events(pair.host_events, ChatEventKind.MESSAGE)[0].message
                    assert reply is not None and reply.text == "pong"
                    assert reply.sender == "Ravi"
                finally:
                    await pair.close()

        run(scenario())

    def test_messages_arrive_in_order(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                pair = await make_pair(server)
                try:
                    count = 12
                    for index in range(count):
                        await pair.host.send_message(f"message {index}")
                    await _wait_for(
                        lambda: len(_events(pair.guest_events, ChatEventKind.MESSAGE)) == count,
                        timeout=10.0,
                    )
                    received = _events(pair.guest_events, ChatEventKind.MESSAGE)
                    texts = [e.message.text for e in received if e.message is not None]
                    assert texts == [f"message {index}" for index in range(count)]
                    sequences = [e.message.sequence for e in received if e.message is not None]
                    assert sequences == list(range(1, count + 1))
                finally:
                    await pair.close()

        run(scenario())

    def test_failed_delivery_after_resend_budget(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                config = ChatSessionConfig(
                    handshake_timeout_seconds=5.0,
                    ack_timeout_seconds=0.3,
                    resend_attempts=2,
                    attach_timeout_seconds=3.0,
                )
                pair = await make_pair(server, config=config)
                try:
                    # Peer stays attached but stops answering: frames arrive,
                    # nothing is ever acked.
                    pair.guest_client.set_forward_listener(None)
                    message = await pair.host.send_message("are you there")
                    await _wait_for(
                        lambda: (
                            pair.host.outbox.get(message.message_id) is not None
                            and pair.host.outbox.get(message.message_id).status  # type: ignore[union-attr]
                            is MessageStatus.FAILED
                        ),
                        timeout=5.0,
                    )
                    failed = pair.host.outbox.get(message.message_id)
                    assert failed is not None and failed.error
                    notices = _events(pair.host_events, ChatEventKind.NOTICE)
                    assert any("could not be delivered" in e.detail for e in notices)
                    assert pair.host.stats.resends >= 1
                finally:
                    await pair.close()

        run(scenario())


# --------------------------------------------------------- duplicates & crypto


class TestDuplicatesAndCrypto:
    def test_duplicate_frame_is_reacked_not_redelivered(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                pair = await make_pair(server)
                try:
                    captured: list[str] = []
                    original = pair.host_client.send_forward

                    async def spy(channel: str, body: str) -> None:
                        captured.append(body)
                        await original(channel, body)

                    pair.host_client.send_forward = spy  # type: ignore[method-assign]
                    message = await pair.host.send_message("send once")
                    await _wait_for(
                        lambda: len(_events(pair.guest_events, ChatEventKind.MESSAGE)) == 1
                    )
                    message_bodies = [
                        body
                        for body in captured
                        if decode_frame(base64.b64decode(body)).data.get("id") == message.message_id
                    ]
                    assert len(message_bodies) == 1

                    # Replay the exact bytes — as a flaky relay or attacker would.
                    await original(pair.channel, message_bodies[0])
                    await asyncio.sleep(0.3)
                    assert len(_events(pair.guest_events, ChatEventKind.MESSAGE)) == 1
                    assert pair.guest.stats.messages_received == 1
                    await _wait_for(lambda: pair.host.stats.acks_received == 2)
                finally:
                    await pair.close()

        run(scenario())

    def test_tampered_ciphertext_fails_loudly_both_sides(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                pair = await make_pair(server)
                try:
                    forged_ct = base64.b64encode(b"x" * 64).decode()
                    forged = message_frame("msg_forgedforged12", 99, forged_ct)
                    body = base64.b64encode(encode_frame(forged)).decode("ascii")
                    await pair.host_client.send_forward(pair.channel, body)
                    await _wait_for(lambda: pair.guest.stats.decrypt_failures == 1)
                    notices = _events(pair.guest_events, ChatEventKind.NOTICE)
                    assert any("integrity check" in e.detail for e in notices)
                    assert not _events(pair.guest_events, ChatEventKind.MESSAGE)
                    # The peer is told clearly via an ERROR frame.
                    await _wait_for(
                        lambda: any(
                            "encryption problem" in e.detail
                            for e in _events(pair.host_events, ChatEventKind.NOTICE)
                        )
                    )
                finally:
                    await pair.close()

        run(scenario())

    def test_plaintext_never_crosses_the_relay(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                pair = await make_pair(server)
                try:
                    captured: list[str] = []
                    original = pair.host_client.send_forward

                    async def spy(channel: str, body: str) -> None:
                        captured.append(body)
                        await original(channel, body)

                    pair.host_client.send_forward = spy  # type: ignore[method-assign]
                    secret = "top secret rendezvous"
                    await pair.host.send_message(secret)
                    await _wait_for(
                        lambda: len(_events(pair.guest_events, ChatEventKind.MESSAGE)) == 1
                    )
                    blob = " ".join(captured)
                    assert secret not in blob
                    assert base64.b64encode(secret.encode()).decode() not in blob
                finally:
                    await pair.close()

        run(scenario())


# -------------------------------------------------------------- reconnection


class TestReconnection:
    def test_queued_message_resends_after_relay_restart(self) -> None:
        async def scenario() -> None:
            # A restartable relay on a fixed port.
            import socket

            with socket.socket() as probe:
                probe.bind(("127.0.0.1", 0))
                port = probe.getsockname()[1]

            first = RelayServer(host="127.0.0.1", port=port)
            await first.start()
            pair: Pair | None = None
            try:
                channel = generate_room_id()
                host_client = await _client(first, "host", RECONNECTING)
                guest_client = await _client(first, "guest", RECONNECTING)
                host = ChatSession(
                    host_client,
                    channel=channel,
                    role="host",
                    display_name="Nova",
                    config=CHAT_FAST,
                )
                guest = ChatSession(
                    guest_client,
                    channel=channel,
                    role="guest",
                    display_name="Ravi",
                    config=CHAT_FAST,
                )
                host_events: list[ChatEvent] = []
                guest_events: list[ChatEvent] = []
                host.add_listener(host_events.append)
                guest.add_listener(guest_events.append)
                pair = Pair(
                    host,
                    guest,
                    host_client,
                    guest_client,
                    host_events,
                    guest_events,
                    channel,
                )

                await host.start()
                await guest.start()
                await host.wait_ready(timeout_seconds=5.0)
                fingerprint_before = host.fingerprint

                await first.aclose()
                await _wait_for(
                    lambda: any(
                        "Connection lost" in e.detail
                        for e in _events(host_events, ChatEventKind.CONNECTION)
                    ),
                    timeout=5.0,
                )
                # Typed while the link is down: queued now, sealed with the
                # NEXT session key once the relay is back.
                message = await host.send_message("offline hello")
                assert not host.is_ready
                second = RelayServer(host="127.0.0.1", port=port)
                await second.start()
                await host.wait_ready(timeout_seconds=10.0)
                await guest.wait_ready(timeout_seconds=10.0)

                try:
                    await _wait_for(
                        lambda: any(
                            e.message is not None and e.message.text == "offline hello"
                            for e in _events(guest_events, ChatEventKind.MESSAGE)
                        ),
                        timeout=10.0,
                    )
                    # A fresh key was negotiated for the new link.
                    assert host.fingerprint == guest.fingerprint
                    assert host.fingerprint != fingerprint_before
                    assert host.stats.rekeys >= 1
                    delivered = host.outbox.get(message.message_id)
                    await _wait_for(
                        lambda: (
                            delivered is not None
                            and host.outbox.get(message.message_id) is not None
                            and host.outbox.get(message.message_id).status  # type: ignore[union-attr]
                            in (MessageStatus.DELIVERED, MessageStatus.READ)
                        )
                    )
                finally:
                    await second.aclose()
            finally:
                if pair is not None:
                    await pair.close()

        run(scenario())


# --------------------------------------------------------------- typing/read


class TestSignals:
    def test_typing_indicators_throttled_and_stopped(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                pair = await make_pair(server)
                try:
                    await pair.host.notify_typing(keystroke=True)
                    await pair.host.notify_typing(keystroke=True)  # throttled
                    await _wait_for(lambda: pair.guest.peer_typing)
                    typing_events = [
                        e for e in _events(pair.guest_events, ChatEventKind.TYPING) if e.detail
                    ]
                    assert len(typing_events) == 1
                    assert "Nova is typing" in typing_events[0].detail
                    await pair.host.notify_typing(keystroke=False)
                    await _wait_for(lambda: not pair.guest.peer_typing)
                finally:
                    await pair.close()

        run(scenario())

    def test_typing_disabled_sends_nothing(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                config = ChatSessionConfig(handshake_timeout_seconds=5.0, typing_indicators=False)
                pair = await make_pair(server, config=config)
                try:
                    await pair.host.notify_typing(keystroke=True)
                    await asyncio.sleep(0.3)
                    assert not _events(pair.guest_events, ChatEventKind.TYPING)
                finally:
                    await pair.close()

        run(scenario())

    def test_read_receipts_disabled_keeps_delivered(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                guest_config = ChatSessionConfig(handshake_timeout_seconds=5.0, read_receipts=False)
                pair = await make_pair(server, guest_config=guest_config)
                try:
                    message = await pair.host.send_message("no receipts please")
                    await _wait_for(
                        lambda: len(_events(pair.guest_events, ChatEventKind.MESSAGE)) == 1
                    )
                    await pair.guest.mark_read()
                    await asyncio.sleep(0.3)
                    current = pair.host.outbox.get(message.message_id)
                    assert current is not None
                    assert current.status is MessageStatus.DELIVERED
                    assert pair.host.receipts.last_read_received == 0
                finally:
                    await pair.close()

        run(scenario())


# ------------------------------------------------------------------ history


class TestHistoryIntegration:
    def test_session_history_records_and_wipes(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                host_history = SessionHistory()
                guest_history = SessionHistory()
                pair = await make_pair(
                    server, host_history=host_history, guest_history=guest_history
                )
                try:
                    await pair.host.send_message("remember me")
                    await _wait_for(lambda: len(guest_history) == 1)
                    await _wait_for(lambda: len(host_history) == 1)
                    assert guest_history.entries()[0].text == "remember me"
                    assert guest_history.entries()[0].author == "Nova"
                    assert "remember me" in host_history.export_text()
                finally:
                    await pair.close()
                assert len(host_history) == 0  # wiped on close
                assert len(guest_history) == 0

        run(scenario())


# ------------------------------------------------------------------ cleanup


class TestCleanup:
    def test_close_zeroizes_and_clears_everything(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                pair = await make_pair(server)
                key_ref = pair.host._session_key  # security property: inspect material
                assert key_ref is not None and any(key_ref)
                await pair.host.send_message("bye")
                await _wait_for(lambda: len(_events(pair.guest_events, ChatEventKind.MESSAGE)) == 1)
                await pair.host.close()
                try:
                    assert pair.host._session_key is None
                    assert all(byte == 0 for byte in key_ref)  # zeroized in place
                    assert not pair.host.is_ready
                    assert pair.host.conversation_id is None
                    assert len(pair.host.outbox) == 0
                    assert pair.host.unread_count == 0
                    with pytest.raises(SecureChannelError):
                        await pair.host.send_message("after close")
                finally:
                    await pair.guest.close()
                    await pair.host_client.disconnect()
                    await pair.guest_client.disconnect()

        run(scenario())

    def test_close_is_idempotent(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                pair = await make_pair(server)
                try:
                    await pair.host.close()
                    await pair.host.close()  # no error, no double detach crash
                finally:
                    await pair.guest.close()
                    await pair.host_client.disconnect()
                    await pair.guest_client.disconnect()

        run(scenario())

    def test_peer_leave_and_rejoin_rekeys(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                pair = await make_pair(server)
                try:
                    first_fingerprint = pair.host.fingerprint
                    await pair.guest.close()  # detaches → peer-left at host
                    await _wait_for(lambda: not pair.host.peer_present)
                    leave_events = _events(pair.host_events, ChatEventKind.PEER)
                    assert any("left" in e.detail for e in leave_events)

                    # A "restarted" peer: new client, new session, same room.
                    guest_client2 = await _client(server, "guest2")
                    guest_events2: list[ChatEvent] = []
                    guest2 = ChatSession(
                        guest_client2,
                        channel=pair.channel,
                        role="guest",
                        display_name="Ravi",
                        config=CHAT_FAST,
                    )
                    guest2.add_listener(guest_events2.append)
                    await guest2.start()
                    await pair.host.wait_ready(timeout_seconds=5.0)
                    await guest2.wait_ready(timeout_seconds=5.0)
                    assert pair.host.fingerprint != first_fingerprint
                    assert pair.host.fingerprint == guest2.fingerprint
                    assert pair.host.stats.rekeys >= 1

                    await guest2.send_message("i am back")
                    await _wait_for(
                        lambda: len(_events(pair.host_events, ChatEventKind.MESSAGE)) == 1
                    )
                    await guest2.close()
                    await guest_client2.disconnect()
                finally:
                    await pair.host.close()
                    await pair.host_client.disconnect()
                    await pair.guest_client.disconnect()

        run(scenario())
