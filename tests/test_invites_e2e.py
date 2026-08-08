"""Phase 5 end-to-end: a real invite between two local GhostLink sessions.

Two independent "users" (separate identities, separate invite registries,
separate relay connections) run the genuine Phase 5 code path over a live
loopback relay:

    host   — mint invite → register with the relay authority → open chat
    joiner — redeem gl://join/<token> → attach as guest → encrypted chat

No mocks anywhere: the authority enforces expiry and single redemption,
the handshake binds both identity keys, and the resulting session ids
stay linked to the invite records on both sides.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ghostlink.exceptions.invites import InviteAlreadyUsedError
from ghostlink.identity import LocalIdentity, identity_fingerprint
from ghostlink.invites.lifecycle import SecureInviteManager
from ghostlink.invites.models import InviteState
from ghostlink.invites.redemption import redeem_invite
from ghostlink.invites.registry import LocalInviteRegistry
from ghostlink.messaging.session.chat import ChatEventKind, ChatSession
from ghostlink.storage.manager import StorageManager
from ghostlink.transport.relay.client import RelayClient
from ghostlink.transport.relay.endpoint import RelayEndpoint
from tests.conftest import run, running_relay
from tests.test_chat_session import CHAT_FAST, FAST, _wait_for


def _manager(root: Path) -> SecureInviteManager:
    return SecureInviteManager(LocalInviteRegistry(StorageManager(root)))


async def _client(server_url: str, name: str) -> RelayClient:
    client = RelayClient(RelayEndpoint.from_url(server_url), client_name=name, config=FAST)
    await client.connect()
    return client


class TestInviteBetweenTwoLocalSessions:
    def test_full_lifecycle_host_and_joiner(self, tmp_path: Path) -> None:
        host_root = tmp_path / "host"
        joiner_root = tmp_path / "joiner"

        async def scenario() -> None:
            async with running_relay() as server:
                host_identity = LocalIdentity.generate(nickname="ShadowHost")
                guest_identity = LocalIdentity.generate(nickname="ShadowGuest")

                # ── host: mint + register with the relay authority ────────
                host_manager = _manager(host_root)
                record, link = host_manager.mint(
                    ttl_seconds=120.0,
                    max_redemptions=1,
                    relay_url=server.url,
                )
                assert link.startswith("gl://join/")
                host_client = await _client(server.url, "host")
                await host_manager.register_with_relay(host_client, record)
                assert record.state is InviteState.ACTIVE

                host_events: list = []
                host = ChatSession(
                    host_client,
                    channel=record.room_id,
                    role="host",
                    display_name=host_identity.nickname or "host",
                    config=CHAT_FAST,
                    identity_public_key_hex=host_identity.public_key_hex,
                )
                host.add_listener(host_events.append)
                await host.start()

                # ── joiner: redeem the one-time link ──────────────────────
                joiner_manager = _manager(joiner_root)
                outcome = await redeem_invite(link, relay_url=server.url, client_config=FAST)
                assert outcome.invite_id == record.invite_id
                assert outcome.room_id == record.room_id
                joiner_record = joiner_manager.record_incoming_redemption(
                    outcome.invite_id,
                    room_id=outcome.room_id,
                    expires_at=outcome.expires_at,
                    relay_url=server.url,
                )
                assert joiner_record.state is InviteState.REDEEMED

                # The redemption probe attached a guest, then disconnected —
                # wait for the slot to free before the chat guest attaches.
                await _wait_for(lambda: server.channel_peers(record.room_id) == ("host",))

                guest_client = await _client(server.url, "guest")
                guest_events: list = []
                guest = ChatSession(
                    guest_client,
                    channel=outcome.room_id,
                    role="guest",
                    display_name=guest_identity.nickname or "guest",
                    config=CHAT_FAST,
                    identity_public_key_hex=guest_identity.public_key_hex,
                )
                guest.add_listener(guest_events.append)
                await guest.start()

                try:
                    await host.wait_ready(timeout_seconds=5.0)
                    await guest.wait_ready(timeout_seconds=5.0)

                    # ── one secure session, same conversation both ways ────
                    assert host.conversation_id is not None
                    assert host.conversation_id == guest.conversation_id
                    assert host.identity_bound and guest.identity_bound
                    assert host.peer_identity_fingerprint == identity_fingerprint(
                        guest_identity.public_key_bytes
                    )
                    assert guest.peer_identity_fingerprint == identity_fingerprint(
                        host_identity.public_key_bytes
                    )

                    # ── encrypted messages round-trip in both directions ───
                    await host.send_message("welcome to the room")
                    await _wait_for(
                        lambda: any(
                            event.kind is ChatEventKind.MESSAGE
                            and event.message is not None
                            and event.message.text == "welcome to the room"
                            for event in guest_events
                        )
                    )
                    await guest.send_message("invite worked perfectly")
                    await _wait_for(
                        lambda: any(
                            event.kind is ChatEventKind.MESSAGE
                            and event.message is not None
                            and event.message.text == "invite worked perfectly"
                            for event in host_events
                        )
                    )

                    # ── session binding lands on the creator's record ─────
                    host_manager.note_local_redeemed(
                        record.invite_id,
                        session_binding=host.conversation_id or "",
                    )
                    bound = host_manager.require(record.invite_id)
                    assert bound.state is InviteState.REDEEMED
                    assert bound.session_binding == host.conversation_id

                    # ── the same invite can never start another session ───
                    with pytest.raises(InviteAlreadyUsedError):
                        await redeem_invite(link, relay_url=server.url, client_config=FAST)
                finally:
                    await host.close()
                    await guest.close()
                    await host_client.disconnect()
                    await guest_client.disconnect()

        run(scenario())

    def test_log_hygiene_holds_across_the_whole_flow(
        self, tmp_path: object, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No secret token, link, or private key material in any log line."""
        import logging
        import re

        async def scenario() -> str:
            async with running_relay() as server:
                manager = _manager(tmp_path / "host")
                record, link = manager.mint(ttl_seconds=30.0, relay_url=server.url)
                host = await _client(server.url, "host")
                await manager.register_with_relay(host, record)
                await redeem_invite(link, relay_url=server.url, client_config=FAST)
                with pytest.raises(InviteAlreadyUsedError):
                    await redeem_invite(link, relay_url=server.url, client_config=FAST)
                await host.disconnect()
                return link

        with caplog.at_level(logging.DEBUG):
            link = run(scenario())
        token = link.split("/")[-1]
        combined = "\n".join(caplog.messages)
        assert token not in combined
        assert "gl://join/" not in combined
        # No 20-char unambiguous-alphabet run anywhere (token shape).
        assert re.search(r"[A-Z2-9]{20}", combined) is None
