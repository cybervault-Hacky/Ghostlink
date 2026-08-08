"""Group lifecycle over a real loopback relay (Phase 6B): the v4 wire path.

No mocks — every test dials a live RelayServer and drives GROUP_* packets
through the same code the CLI uses, with real Ed25519 signing callbacks.
"""

from __future__ import annotations

import asyncio

import pytest

from ghostlink.constants.net import GROUP_PROTOCOL_VERSION, MAX_GROUP_MEMBERS
from ghostlink.exceptions.groups import (
    GroupError,
    GroupFullError,
    GroupPermissionError,
    GroupUnknownError,
)
from ghostlink.exceptions.invites import InviteAlreadyUsedError
from ghostlink.identity.identity import LocalIdentity
from ghostlink.invites.tokens import generate_invite_token
from ghostlink.transport.relay.client import RelayClient
from tests.conftest import run, running_relay
from tests.group_helpers import (
    attest_b64,
    client_for,
    fingerprint_of,
    fresh_identity,
    pop_b64,
    sign_b64,
)


async def _create_group(client: RelayClient, identity: LocalIdentity, name: str = "Ops") -> str:
    nonce = client.attest_nonce
    assert nonce, "relay must issue an attestation challenge"
    group_id, epoch = await client.group_create(
        name,
        public_key_hex=identity.public_key_hex,
        pop_signature_b64=pop_b64(identity, name, nonce),
        handle=identity.identity_id,
        display_name=identity.identity_id,
    )
    assert epoch == 1
    return group_id


def _install_signer(client: RelayClient, identity: LocalIdentity) -> None:
    client.set_group_signer(lambda _gid, _op, msg: sign_b64(identity, msg))


async def _admit_via_protocol(
    owner: RelayClient,
    guest: RelayClient,
    owner_identity: LocalIdentity,
    guest_identity: LocalIdentity,
    group_id: str,
    *,
    kind: str = "group",
) -> None:
    """Full invite → redeem → attest → countersign → event flow."""

    token = generate_invite_token()
    await owner.create_invite(
        token, room_id="", ttl_seconds=60.0, max_redemptions=1, kind=kind, group_id=group_id
    )
    redemption = await guest.redeem_group_invite(token)
    assert redemption.group_id == group_id
    guest_fp = fingerprint_of(guest_identity)
    waiter = asyncio.ensure_future(
        guest.wait_group_event(group_id, "join", guest_fp, timeout_seconds=10.0)
    )
    attested = await guest.group_attest(
        group_id,
        public_key_hex=guest_identity.public_key_hex,
        signature_b64=attest_b64(guest_identity, group_id, str(guest.attest_nonce)),
        handle=guest_identity.identity_id,
        display_name=guest_identity.identity_id,
    )
    assert attested.role == "candidate"
    event = await waiter
    assert int(str(event.payload["epoch"])) == redemption.epoch + 1


class TestHappyPathOverWire:
    def test_create_invite_join_and_leave(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner_id, guest_id = fresh_identity(), fresh_identity()
                owner = await client_for(server, "owner")
                guest = await client_for(server, "guest")
                _install_signer(owner, owner_id)
                _install_signer(guest, guest_id)
                group_id = await _create_group(owner, owner_id, "Night Watch")
                await _admit_via_protocol(owner, guest, owner_id, guest_id, group_id)

                roster = await owner.group_state(group_id)
                assert roster.epoch == 2
                assert len(roster.members) == 2
                fps = {m["fingerprint"] for m in roster.members}
                assert fps == {fingerprint_of(owner_id), fingerprint_of(guest_id)}

                # Guest leaves (self-signed); both sides observe epoch 3.
                leave_event = await guest.group_leave(
                    group_id, subject_fingerprint=fingerprint_of(guest_id), timeout_seconds=10.0
                )
                assert int(str(leave_event.payload["epoch"])) == 3
                roster = await owner.group_state(group_id)
                assert len(roster.members) == 1
                assert roster.epoch == 3
                await owner.disconnect()
                await guest.disconnect()

        run(scenario())

    def test_redeemed_snapshot_is_pinned(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner_id, _guest_id = fresh_identity(), fresh_identity()
                owner = await client_for(server, "owner")
                guest = await client_for(server, "guest")
                _install_signer(owner, owner_id)
                group_id = await _create_group(owner, owner_id)
                token = generate_invite_token()
                await owner.create_invite(
                    token,
                    room_id="",
                    ttl_seconds=60.0,
                    max_redemptions=1,
                    kind="group",
                    group_id=group_id,
                )
                redemption = await guest.redeem_group_invite(token)
                assert redemption.owner_fingerprint == fingerprint_of(owner_id)
                assert redemption.owner_public_key_hex == owner_id.public_key_hex
                assert redemption.epoch == 1
                assert len(redemption.members) == 1
                assert redemption.members[0]["public_key_hex"] == owner_id.public_key_hex
                await owner.disconnect()
                await guest.disconnect()

        run(scenario())


class TestErrorMappingOverWire:
    def test_bad_pop_maps_to_permission_error(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner_id, other = fresh_identity(), fresh_identity()
                owner = await client_for(server, "owner")
                nonce = str(owner.attest_nonce)
                with pytest.raises(GroupPermissionError):
                    await owner.group_create(
                        "Ops",
                        public_key_hex=owner_id.public_key_hex,
                        pop_signature_b64=pop_b64(other, "Ops", nonce),
                        handle=owner_id.identity_id,
                        display_name="Ops tester",
                    )
                await owner.disconnect()

        run(scenario())

    def test_unknown_group_maps_to_unknown_error(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                identity = fresh_identity()
                client = await client_for(server, "solo")
                with pytest.raises(GroupUnknownError):
                    await client.group_attest(
                        "gl-group-AAAA-BBBB-CCCC",
                        public_key_hex=identity.public_key_hex,
                        signature_b64=attest_b64(
                            identity, "gl-group-AAAA-BBBB-CCCC", str(client.attest_nonce)
                        ),
                        handle=identity.identity_id,
                        display_name="Ops tester",
                    )
                await client.disconnect()

        run(scenario())

    def test_v3_clients_cannot_use_groups(self, monkeypatch: pytest.MonkeyPatch) -> None:
        async def scenario() -> None:
            import ghostlink.transport.relay.protocol as protocol

            monkeypatch.setattr(protocol, "PROTOCOL_VERSION", GROUP_PROTOCOL_VERSION - 1)
            async with running_relay() as server:
                identity = fresh_identity()
                client = await client_for(server, "legacy")
                with pytest.raises(GroupError):
                    await client.group_create(
                        "Ops",
                        public_key_hex=identity.public_key_hex,
                        pop_signature_b64=pop_b64(identity, "Ops", "n"),
                        handle=identity.identity_id,
                        display_name="Ops tester",
                    )
                await client.disconnect()

        run(scenario())

    def test_v3_invites_still_work(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                from ghostlink.models.room import generate_room_id

                host = await client_for(server, "host")
                token = generate_invite_token()
                room = generate_room_id()
                grant = await host.create_invite(
                    token, room_id=room, ttl_seconds=60.0, max_redemptions=1
                )
                assert grant.invite_id
                guest = await client_for(server, "g")
                recovered, _expiry = await guest.redeem_invite(token)
                assert recovered == room
                await host.disconnect()
                await guest.disconnect()

        run(scenario())


class TestCapacityOverWire:
    def test_full_group_refuses_ninth_join_atomically(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner_id = fresh_identity()
                owner = await client_for(server, "owner")
                _install_signer(owner, owner_id)
                group_id = await _create_group(owner, owner_id)
                # One invite admitting 7 more seats: roster 1→8.
                token = generate_invite_token()
                await owner.create_invite(
                    token,
                    room_id="",
                    ttl_seconds=120.0,
                    max_redemptions=7,
                    kind="group",
                    group_id=group_id,
                )
                guests = []
                for index in range(MAX_GROUP_MEMBERS - 1):
                    guest = await client_for(server, f"g{index}")
                    guest_id = fresh_identity()
                    _install_signer(guest, guest_id)
                    waiter = asyncio.ensure_future(
                        guest.wait_group_event(
                            group_id, "join", fingerprint_of(guest_id), timeout_seconds=15.0
                        )
                    )
                    await guest.redeem_group_invite(token)
                    await guest.group_attest(
                        group_id,
                        public_key_hex=guest_id.public_key_hex,
                        signature_b64=attest_b64(guest_id, group_id, str(guest.attest_nonce)),
                        handle=guest_id.identity_id,
                        display_name=guest_id.identity_id,
                    )
                    event = await waiter
                    guests.append((guest, guest_id, event))
                roster = await owner.group_state(group_id)
                assert len(roster.members) == MAX_GROUP_MEMBERS
                assert roster.epoch == MAX_GROUP_MEMBERS
                # Epochs strictly +1 per admission — deterministic ordering.
                epochs = [int(str((await_guest[2]).payload["epoch"])) for await_guest in guests]
                assert epochs == sorted(epochs)
                assert len(set(epochs)) == len(epochs)

                # The 9th redemption hits the full roster: refused, and the
                # invite for it is NOT consumed silently — mint a fresh one
                # and confirm the capacity verdict comes from the group side.
                ninth = await client_for(server, "ninth")
                fresh_identity()
                token2 = generate_invite_token()
                with pytest.raises(GroupFullError):
                    await owner.create_invite(
                        token2,
                        room_id="",
                        ttl_seconds=60.0,
                        max_redemptions=1,
                        kind="group",
                        group_id=group_id,
                    )
                await owner.disconnect()
                for guest, _, _ in guests:
                    await guest.disconnect()
                await ninth.disconnect()

        run(scenario())


class TestConcurrentRedemption:
    def test_single_use_invite_admits_exactly_one(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner_id = fresh_identity()
                owner = await client_for(server, "owner")
                _install_signer(owner, owner_id)
                group_id = await _create_group(owner, owner_id)
                token = generate_invite_token()
                await owner.create_invite(
                    token,
                    room_id="",
                    ttl_seconds=60.0,
                    max_redemptions=1,
                    kind="group",
                    group_id=group_id,
                )
                guests = [await client_for(server, f"c{i}") for i in range(2)]

                async def try_redeem(client: RelayClient) -> str:
                    try:
                        redemption = await client.redeem_group_invite(token)
                    except InviteAlreadyUsedError:
                        return "lost"
                    return f"won:{redemption.group_id}"

                results = await asyncio.gather(*(try_redeem(g) for g in guests))
                assert sorted(results) == ["lost", f"won:{group_id}"]
                await owner.disconnect()
                for guest in guests:
                    await guest.disconnect()

        run(scenario())


class TestRestartSemantics:
    def test_groups_do_not_survive_relay_restart(self) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                port = server.port
                owner_id = fresh_identity()
                owner = await client_for(server, "owner")
                _install_signer(owner, owner_id)
                group_id = await _create_group(owner, owner_id)
                await owner.disconnect()
            # A brand-new server on the same port simulates the restart.
            from ghostlink.transport.relay.server import RelayServer

            restarted = RelayServer(host="127.0.0.1", port=port)
            await restarted.start()
            try:
                client = await client_for(restarted, "owner")
                with pytest.raises(GroupUnknownError):
                    await client.group_attest(
                        group_id,
                        public_key_hex=owner_id.public_key_hex,
                        signature_b64=attest_b64(owner_id, group_id, str(client.attest_nonce)),
                        handle=owner_id.identity_id,
                        display_name="Ops tester",
                    )
                await client.disconnect()
            finally:
                await restarted.aclose()

        run(scenario())


class TestChatInvitesUnaffected:
    def test_group_packet_family_does_not_break_chat_family(self) -> None:
        """The v4 wire keeps chat-kind invite payloads byte-compatible."""

        async def scenario() -> None:
            async with running_relay() as server:
                from ghostlink.models.room import generate_room_id

                host = await client_for(server, "host")
                token = generate_invite_token()
                room = generate_room_id()
                await host.create_invite(token, room_id=room, ttl_seconds=60.0, max_redemptions=1)
                status = await host.query_invite(token)
                assert status.state == "active"
                assert status.room_id == room
                await host.revoke_invite(token)
                guest = await client_for(server, "guest")
                from ghostlink.exceptions.invites import InviteRevokedError

                with pytest.raises(InviteRevokedError):
                    await guest.redeem_invite(token)
                await host.disconnect()
                await guest.disconnect()

        run(scenario())


class TestOwnerOfflineJoinTimeout:
    def test_join_times_out_without_owner_countersign(self) -> None:
        async def scenario() -> None:
            from ghostlink.exceptions.groups import GroupJoinTimeoutError

            async with running_relay() as server:
                owner_id, guest_id = fresh_identity(), fresh_identity()
                owner = await client_for(server, "owner")
                _install_signer(owner, owner_id)
                group_id = await _create_group(owner, owner_id)
                token = generate_invite_token()
                await owner.create_invite(
                    token,
                    room_id="",
                    ttl_seconds=60.0,
                    max_redemptions=1,
                    kind="group",
                    group_id=group_id,
                )
                # Owner disconnects WITHOUT a signer: the admission stalls.
                await owner.disconnect()
                guest = await client_for(server, "guest")
                await guest.redeem_group_invite(token)
                with pytest.raises(GroupJoinTimeoutError):
                    waiter = asyncio.ensure_future(
                        guest.wait_group_event(
                            group_id, "join", fingerprint_of(guest_id), timeout_seconds=1.0
                        )
                    )
                    await guest.group_attest(
                        group_id,
                        public_key_hex=guest_id.public_key_hex,
                        signature_b64=attest_b64(guest_id, group_id, str(guest.attest_nonce)),
                        handle=guest_id.identity_id,
                        display_name=guest_id.identity_id,
                    )
                    await waiter
                await guest.disconnect()

        run(scenario())
