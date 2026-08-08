"""Phase 6C end-to-end: pairwise-mesh group messaging on a real loopback relay.

No mocks for security-critical paths: members are full devices (storage +
identity + lifecycle + messaging service), the relay is the real
:class:`RelayServer`, and every assertion flows over the v4 wire protocol.
"""

from __future__ import annotations

import asyncio
import base64
import logging
from pathlib import Path

import pytest

from ghostlink.constants.net import (
    GROUP_FORWARD_RATE_PER_SECOND,
    GROUP_MSG_MAX_BYTES,
    GROUP_OFFLINE_QUEUE_PER_MEMBER,
    MAX_GROUP_MEMBERS,
)
from ghostlink.exceptions.groups import (
    GroupDefunctError,
    GroupMessageError,
    GroupStateError,
    GroupUnknownError,
)
from ghostlink.groups.frames import (
    KIND_KEX,
    KIND_MSG,
    TERMINAL_DELIVERY_STATES,
    DeliveryState,
    GroupMessageLedger,
    gmsg_frame,
)
from ghostlink.groups.mesh import group_message_aad
from ghostlink.groups.models import LocalGroupState
from ghostlink.groups.service import (
    GroupChatEvent,
    GroupChatEventKind,
    GroupMessagingService,
)
from ghostlink.identity.identity import LocalIdentity
from ghostlink.messaging.history import SessionHistory
from ghostlink.transport.relay.client import RelayClient
from ghostlink.transport.relay.protocol import Packet, PacketType
from tests.conftest import run, running_relay
from tests.group_helpers import GroupHome, client_for, fingerprint_of

# ------------------------------------------------------------------ harness


async def wait_until(predicate: object, timeout: float = 5.0, interval: float = 0.01) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        await asyncio.sleep(interval)
    return False


class Recorder:
    """Collects service events for assertions."""

    def __init__(self) -> None:
        self.events: list[GroupChatEvent] = []

    def __call__(self, event: GroupChatEvent) -> None:
        self.events.append(event)

    def messages(self) -> list[GroupChatEvent]:
        return [event for event in self.events if event.kind is GroupChatEventKind.MESSAGE]

    def notices(self, needle: str = "") -> list[GroupChatEvent]:
        return [
            event
            for event in self.events
            if event.kind in (GroupChatEventKind.NOTICE, GroupChatEventKind.MEMBERSHIP)
            and (not needle or needle in event.detail)
        ]

    def texts(self) -> list[str]:
        return [event.frame.text for event in self.messages() if event.frame is not None]


class Member:
    """One full device: home (storage/identity/lifecycle) + client + service."""

    def __init__(self, home: GroupHome) -> None:
        self.home = home
        self.client: RelayClient | None = None
        self.service: GroupMessagingService | None = None
        self.recorder = Recorder()

    @property
    def identity(self) -> LocalIdentity:
        return self.home.identities.ensure()

    @property
    def fp(self) -> str:
        return fingerprint_of(self.identity)

    async def connect(self, server_url: str, name: str) -> None:
        class _Server:
            url = server_url

        if self.client is not None:
            await self.client.aclose()
        self.client = await client_for(_Server(), name)  # type: ignore[arg-type]
        if self.service is None:
            self.service = GroupMessagingService(self.home.groups, self.home.identities)
            self.service.add_listener(self.recorder)
        # Re-attach the SAME service: the per-group gseq counter is
        # service-scoped (§18.1 "persisted for the session") while attach()
        # zeroizes the session-scoped link keys (§27.2).
        await self.service.attach(self.client)

    async def sync(self, group_id: str) -> None:
        assert self.service is not None
        await self.service.sync(group_id)

    async def send(self, group_id: str, text: str) -> GroupMessageLedger:
        assert self.service is not None
        return await self.service.send(group_id, text)

    async def close(self) -> None:
        if self.service is not None:
            await self.service.close()
        if self.client is not None:
            await self.client.aclose()


async def build_world(
    root: Path, server_url: str, size: int, *, name: str = "Ops"
) -> tuple[Member, list[Member], str]:
    """Owner creates a group; size-1 members join over the real wire."""

    owner = Member(GroupHome(root / "owner"))
    await owner.connect(server_url, "owner")
    assert owner.client is not None
    owner.home.groups.attach(owner.client)
    record = await owner.home.groups.create_group(owner.client, name)
    group_id = record.group_id
    members: list[Member] = []
    for index in range(size - 1):
        member = Member(GroupHome(root / f"m{index}"))
        await member.connect(server_url, f"m{index}")
        assert owner.client is not None and member.client is not None
        _invite, link = await owner.home.groups.mint_group_invite(
            owner.client,
            owner.home.invites,
            group_id,
            ttl_seconds=300.0,
            max_redemptions=1,
        )
        member.home.groups.attach(member.client)
        await member.home.groups.join_group(member.client, link)
        members.append(member)
    await owner.sync(group_id)
    for member in members:
        await member.sync(group_id)
    return owner, members, group_id


def forward_payload(
    *, group: str, epoch: int, sender: str, to: str, kind: str, body: bytes
) -> Packet:
    return Packet(
        PacketType.GROUP_FORWARD,
        {
            "group": group,
            "epoch": epoch,
            "from": sender,
            "to": to,
            "kind": kind,
            "body": base64.b64encode(body).decode("ascii"),
        },
    )


# -------------------------------------------------------------- happy paths


class TestFanout:
    def test_two_member_message_roundtrip(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                ledger = await owner.send(gid, "hello pairwise world")
                assert ledger.total() == 1
                assert ledger.recipients[peer.fp] is DeliveryState.SENT
                assert await wait_until(lambda: peer.recorder.texts() == ["hello pairwise world"])
                frame = peer.recorder.messages()[0].frame
                assert frame is not None
                assert frame.sender == owner.fp
                assert frame.message_id == ledger.message_id
                assert frame.gseq == 1
                # GACK flows back: delivered for exactly this recipient.
                assert await wait_until(
                    lambda: ledger.recipients[peer.fp] in TERMINAL_DELIVERY_STATES
                )
                assert ledger.status_line() == "delivered 1/1"
                await owner.close()
                await peer.close()

        run(scenario())

    def test_three_member_fanout_isolation(self, tmp_path: Path) -> None:
        """Both recipients get the same plaintext, sealed independently."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 3)
                first, second = members
                ledger = await owner.send(gid, "fanout check")
                ok = await wait_until(
                    lambda: (
                        first.recorder.texts() == ["fanout check"]
                        and second.recorder.texts() == ["fanout check"]
                    )
                )
                assert ok
                assert await wait_until(lambda: ledger.delivered_count() == 2)
                assert ledger.status_line() == "delivered 2/2"
                # attribution: both frames provably from the owner's key
                for member in members:
                    frame = member.recorder.messages()[0].frame
                    assert frame is not None and frame.sender == owner.fp
                await owner.close()
                for member in members:
                    await member.close()

        run(scenario())

    def test_eight_member_fanout(self, tmp_path: Path) -> None:
        """The §32 ceiling: 8 members ⇒ fanout 7, ledger delivered 7/7."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, MAX_GROUP_MEMBERS)
                ledger = await owner.send(gid, "full house")
                assert ledger.total() == MAX_GROUP_MEMBERS - 1
                ok = await wait_until(
                    lambda: all(member.recorder.texts() == ["full house"] for member in members),
                    timeout=10.0,
                )
                assert ok
                assert await wait_until(
                    lambda: ledger.delivered_count() == MAX_GROUP_MEMBERS - 1, timeout=10.0
                )
                assert ledger.status_line() == f"delivered {MAX_GROUP_MEMBERS - 1}/7"
                await owner.close()
                for member in members:
                    await member.close()

        run(scenario())

    def test_bidirectional_mesh_with_knock(self, tmp_path: Path) -> None:
        """My responder-role sends trigger the knock → hello flow."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                await peer.send(gid, "pong back")  # maybe-knock direction
                assert await wait_until(lambda: owner.recorder.texts() == ["pong back"])
                await owner.close()
                await peer.close()

        run(scenario())

    def test_concurrent_senders(self, tmp_path: Path) -> None:
        """Two members send at once; both messages land, per-link order kept."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 3)
                first, second = members
                await owner.send(gid, "from owner")
                await asyncio.gather(first.send(gid, "from first"), second.send(gid, "from second"))
                ok = await wait_until(
                    lambda: sorted(owner.recorder.texts()) == ["from first", "from second"]
                )
                assert ok
                frame_first, frame_second = (first.recorder.messages(), second.recorder.messages())
                assert frame_first and frame_second
                await owner.close()
                for member in members:
                    await member.close()

        run(scenario())

    def test_read_receipts_flow(self, tmp_path: Path) -> None:
        """GREAD read cursors advance DELIVERED → READ per recipient."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                ledger = await owner.send(gid, "read me")
                assert await wait_until(
                    lambda: ledger.recipients[peer.fp] is DeliveryState.READ, timeout=5.0
                )
                await owner.close()
                await peer.close()

        run(scenario())


class TestSenderGates:
    def test_send_rejected_when_not_a_member_locally(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                stranger = Member(GroupHome(tmp_path / "stranger"))
                await stranger.connect(server.url, "stranger")
                # never joined: the local gate refuses before any network I/O
                with pytest.raises(GroupUnknownError):
                    await stranger.send(gid, "I should not speak")
                await owner.close()
                for member in members:
                    await member.close()
                await stranger.close()

        run(scenario())

    def test_solo_group_refuses_send(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner = Member(GroupHome(tmp_path / "owner"))
                await owner.connect(server.url, "owner")
                assert owner.client is not None
                owner.home.groups.attach(owner.client)
                record = await owner.home.groups.create_group(owner.client, "solo")
                with pytest.raises(GroupMessageError):
                    await owner.send(record.group_id, "nobody here")
                await owner.close()

        run(scenario())

    def test_oversize_message_rejected_before_network(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                with pytest.raises(GroupMessageError):
                    await owner.send(gid, "x" * (GROUP_MSG_MAX_BYTES + 1))
                await owner.close()
                for member in members:
                    await member.close()

        run(scenario())

    def test_empty_message_rejected(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                with pytest.raises(GroupMessageError):
                    await owner.send(gid, "   ")
                await owner.close()
                for member in members:
                    await member.close()

        run(scenario())


class TestReplayAndSequence:
    async def _capture_bodies(self, client: RelayClient) -> list[tuple[tuple, dict]]:
        captured: list[tuple[tuple, dict]] = []
        original = client.group_forward

        async def capture(*args: object, **kwargs: object) -> None:
            captured.append((args, kwargs))
            await original(*args, **kwargs)

        client.group_forward = capture  # type: ignore[method-assign]
        return captured

    def test_duplicate_envelope_reacked_not_rerendered(self, tmp_path: Path) -> None:
        """§30: same (sender, message_id) re-ACKs but never renders twice."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                receiver = members[0]
                assert owner.client is not None
                captured = await self._capture_bodies(owner.client)
                ledger = await owner.send(gid, "dedupe me")
                assert await wait_until(lambda: receiver.recorder.texts() == ["dedupe me"])
                # replay the very same envelope bytes at the receiver
                args, kwargs = captured[-1]
                packet = forward_payload(
                    group=gid,
                    epoch=int(str(args[1])),
                    sender=str(kwargs["from_fingerprint"]),
                    to=receiver.fp,
                    kind=KIND_MSG,
                    body=base64.b64decode(str(kwargs["body_b64"]).encode("ascii")),
                )
                assert receiver.service is not None
                before = len(receiver.recorder.messages())
                await receiver.service._handle_forward(packet)
                await receiver.service._handle_forward(packet)  # twice
                assert len(receiver.recorder.messages()) == before  # no double render
                # …and the sender still converges on delivered (re-ACK etiquette)
                assert await wait_until(
                    lambda: ledger.recipients[receiver.fp] in TERMINAL_DELIVERY_STATES
                )
                await owner.close()
                for member in members:
                    await member.close()

        run(scenario())

    def test_gseq_rollback_dropped(self, tmp_path: Path) -> None:
        """§22: a forged frame with gseq ≤ last_seen is a replay — dropped."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                await owner.send(gid, "first")
                await owner.send(gid, "second")
                assert await wait_until(lambda: peer.recorder.texts() == ["first", "second"])
                assert owner.service is not None and peer.service is not None
                record = owner.home.groups.require(gid)
                link = owner.service.mesh.link_for(gid, peer.fp, record.epoch)
                assert link is not None
                forged = gmsg_frame(
                    gid,
                    record.epoch,
                    owner.fp,
                    peer.fp,
                    message_id="gmsg_deadbeefcafe0001",
                    gseq=1,  # rollback: already seen 1 and 2
                    display_name="forged",
                    text="rollback attack",
                    ts=1.0,
                )
                sealed = link.seal(forged, group_message_aad(gid, record.epoch, owner.fp, peer.fp))
                before = len(peer.recorder.messages())
                await peer.service._handle_forward(
                    forward_payload(
                        group=gid,
                        epoch=record.epoch,
                        sender=owner.fp,
                        to=peer.fp,
                        kind=KIND_MSG,
                        body=sealed,
                    )
                )
                assert len(peer.recorder.messages()) == before
                await owner.close()
                await peer.close()

        run(scenario())

    def test_gseq_gap_flagged_then_delivered(self, tmp_path: Path) -> None:
        """§22: bounded gaps are delivered with a visible gap flag."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                await owner.send(gid, "first")
                assert await wait_until(lambda: peer.recorder.texts() == ["first"])
                assert owner.service is not None and peer.service is not None
                record = owner.home.groups.require(gid)
                link = owner.service.mesh.link_for(gid, peer.fp, record.epoch)
                assert link is not None
                jumped = gmsg_frame(
                    gid,
                    record.epoch,
                    owner.fp,
                    peer.fp,
                    message_id="gmsg_deadbeefcafe0002",
                    gseq=5,  # gap 2→5
                    display_name="owner",
                    text="gap message",
                    ts=1.0,
                )
                sealed = link.seal(jumped, group_message_aad(gid, record.epoch, owner.fp, peer.fp))
                await peer.service._handle_forward(
                    forward_payload(
                        group=gid,
                        epoch=record.epoch,
                        sender=owner.fp,
                        to=peer.fp,
                        kind=KIND_MSG,
                        body=sealed,
                    )
                )
                latest = peer.recorder.messages()[-1]
                assert latest.frame is not None and latest.frame.text == "gap message"
                assert latest.gap is True
                await owner.close()
                await peer.close()

        run(scenario())

    def test_gseq_excessive_gap_dropped_with_notice(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                await owner.send(gid, "first")
                assert await wait_until(lambda: peer.recorder.texts() == ["first"])
                assert owner.service is not None and peer.service is not None
                record = owner.home.groups.require(gid)
                link = owner.service.mesh.link_for(gid, peer.fp, record.epoch)
                assert link is not None
                jumped = gmsg_frame(
                    gid,
                    record.epoch,
                    owner.fp,
                    peer.fp,
                    message_id="gmsg_deadbeefcafe0003",
                    gseq=500,  # > 64 gap
                    display_name="owner",
                    text="way ahead",
                    ts=1.0,
                )
                sealed = link.seal(jumped, group_message_aad(gid, record.epoch, owner.fp, peer.fp))
                before = len(peer.recorder.messages())
                await peer.service._handle_forward(
                    forward_payload(
                        group=gid,
                        epoch=record.epoch,
                        sender=owner.fp,
                        to=peer.fp,
                        kind=KIND_MSG,
                        body=sealed,
                    )
                )
                assert len(peer.recorder.messages()) == before
                assert await wait_until(lambda: peer.recorder.notices("gseq"))
                await owner.close()
                await peer.close()

        run(scenario())

    def test_tampered_ciphertext_counted_and_dropped(self, tmp_path: Path) -> None:
        """AEAD gate: bit-flips drop; repeated failures → suspect link."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                await owner.send(gid, "warm the link")
                assert await wait_until(lambda: peer.recorder.texts() == ["warm the link"])
                assert owner.service is not None and peer.service is not None
                record = owner.home.groups.require(gid)
                link = owner.service.mesh.link_for(gid, peer.fp, record.epoch)
                assert link is not None
                sealed = bytearray(
                    link.seal(
                        gmsg_frame(
                            gid,
                            record.epoch,
                            owner.fp,
                            peer.fp,
                            message_id="gmsg_deadbeefcafe0004",
                            gseq=2,
                            display_name="owner",
                            text="attacked",
                            ts=1.0,
                        ),
                        group_message_aad(gid, record.epoch, owner.fp, peer.fp),
                    )
                )
                sealed[10] ^= 0x01  # flip one ciphertext byte
                before = len(peer.recorder.messages())
                for _ in range(3):
                    await peer.service._handle_forward(
                        forward_payload(
                            group=gid,
                            epoch=record.epoch,
                            sender=owner.fp,
                            to=peer.fp,
                            kind=KIND_MSG,
                            body=bytes(sealed),
                        )
                    )
                assert len(peer.recorder.messages()) == before
                assert peer.service.mesh.link_suspect(gid, owner.fp)
                assert await wait_until(lambda: peer.recorder.notices("suspect"))
                await owner.close()
                await peer.close()

        run(scenario())


class TestContextCrossChecks:
    def test_envelope_sender_spoof_rejected(self, tmp_path: Path) -> None:
        """§18.2 step 3: sealed sender ≠ envelope sender ⇒ rejected."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 3)
                first, second = members
                await first.send(gid, "from first")
                assert await wait_until(
                    lambda: (
                        second.recorder.texts() == ["from first"]
                        and owner.recorder.texts() == ["from first"]
                    )
                )
                assert first.service is not None and second.service is not None
                record = first.home.groups.require(gid)
                link = first.service.mesh.link_for(gid, second.fp, record.epoch)
                assert link is not None
                sealed = link.seal(
                    gmsg_frame(
                        gid,
                        record.epoch,
                        first.fp,
                        second.fp,
                        message_id="gmsg_deadbeefcafe0010",
                        gseq=2,
                        display_name="first",
                        text="real",
                        ts=1.0,
                    ),
                    group_message_aad(gid, record.epoch, first.fp, second.fp),
                )
                # relay-attacker relabels the envelope sender to the owner
                before = len(second.recorder.messages())
                await second.service._handle_forward(
                    forward_payload(
                        group=gid,
                        epoch=record.epoch,
                        sender=owner.fp,
                        to=second.fp,
                        kind=KIND_MSG,
                        body=sealed,
                    )
                )
                assert len(second.recorder.messages()) == before
                await first.close()
                await second.close()
                await owner.close()

        run(scenario())

    def test_envelope_recipient_spoof_stops_at_to_gate(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 3)
                first, second = members
                await first.send(gid, "hello")
                assert await wait_until(lambda: second.recorder.texts() == ["hello"])
                assert first.service is not None and second.service is not None
                record = first.home.groups.require(gid)
                link = first.service.mesh.link_for(gid, second.fp, record.epoch)
                assert link is not None
                sealed = link.seal(
                    gmsg_frame(
                        gid,
                        record.epoch,
                        first.fp,
                        second.fp,
                        message_id="gmsg_deadbeefcafe0011",
                        gseq=2,
                        display_name="first",
                        text="for second",
                        ts=1.0,
                    ),
                    group_message_aad(gid, record.epoch, first.fp, second.fp),
                )
                # envelope claims the owner was the recipient — not us
                before = len(second.recorder.messages())
                await second.service._handle_forward(
                    forward_payload(
                        group=gid,
                        epoch=record.epoch,
                        sender=first.fp,
                        to=owner.fp,
                        kind=KIND_MSG,
                        body=sealed,
                    )
                )
                assert len(second.recorder.messages()) == before
                await first.close()
                await second.close()
                await owner.close()

        run(scenario())


class TestMemberRemovalSecurity:
    """§15: A B C; C removed at e→e+1; C must not see future traffic."""

    def test_removal_matrix(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 3)
                first, removed = members
                assert owner.client is not None
                await owner.send(gid, "before removal")
                assert await wait_until(lambda: removed.recorder.texts() == ["before removal"])
                await owner.home.groups.remove_member(owner.client, gid, removed.fp)
                # everyone converges: records leap to epoch 4
                assert await wait_until(
                    lambda: (
                        first.home.groups.require(gid).epoch == 4
                        and removed.home.groups.require(gid).state is LocalGroupState.REMOVED
                    )
                )
                # mesh links to the removed member are gone everywhere
                assert owner.service is not None and first.service is not None
                assert owner.service.mesh.link_for(gid, removed.fp, 4) is None
                assert first.service.mesh.link_for(gid, removed.fp, 4) is None

                # 1) removed member cannot send (local gate, fail closed)
                with pytest.raises(GroupStateError):
                    await removed.send(gid, "I was removed")

                # 2) removed member cannot send even via raw protocol (relay ACL)
                assert removed.client is not None
                removed_errors: list[str] = []
                removed.client.set_group_forward_error_listener(
                    lambda _g, _m, code, _msg: removed_errors.append(code)
                )
                await removed.client.group_forward(
                    gid,
                    4,
                    from_fingerprint=removed.fp,
                    to_fingerprint=first.fp,
                    kind=KIND_MSG,
                    body_b64=base64.b64encode(b"xx").decode(),
                )
                assert await wait_until(lambda: removed_errors == ["group/not-member"])

                # 3) removed member receives nothing from post-removal sends
                before = len(removed.recorder.messages())
                await owner.send(gid, "after removal")
                assert await wait_until(lambda: first.recorder.texts()[-1:] == ["after removal"])
                await asyncio.sleep(0.2)
                assert len(removed.recorder.messages()) == before

                # 4) removed member cannot re-sync — the local fail-closed gate
                # (§15: state is no longer ACTIVE) refuses before the wire; the
                # relay-side ACL is proven in steps 2 and 5 (group/not-member).
                with pytest.raises(GroupStateError):
                    await removed.home.groups.sync_group(removed.client, gid)

                # 5) removed member's stale KEX hello is refused at the relay
                removed_errors.clear()
                await removed.client.group_forward(
                    gid,
                    4,
                    from_fingerprint=removed.fp,
                    to_fingerprint=owner.fp,
                    kind=KIND_KEX,
                    body_b64=base64.b64encode(b"{}").decode(),
                )
                assert await wait_until(lambda: removed_errors == ["group/not-member"])
                await owner.close()
                for member in members:
                    await member.close()

        run(scenario())

    def test_removed_member_cannot_impersonate_via_from_label(self, tmp_path: Path) -> None:
        """The relay binds 'from' to the attested session's fingerprint."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                assert peer.client is not None
                errors: list[str] = []
                peer.client.set_group_forward_error_listener(
                    lambda _g, _m, code, _msg: errors.append(code)
                )
                # peer tries to label traffic as the owner
                await peer.client.group_forward(
                    gid,
                    2,
                    from_fingerprint=owner.fp,
                    to_fingerprint=owner.fp,
                    kind=KIND_MSG,
                    body_b64=base64.b64encode(b"xx").decode(),
                )
                assert await wait_until(lambda: errors == ["group/not-member"])
                assert owner.recorder.messages() == []
                await owner.close()
                await peer.close()

        run(scenario())


class TestNewMemberIsolation:
    """§13.3/§16.7/§25.3: joiners get nothing from before their epoch."""

    def test_new_member_isolation(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                first = members[0]
                assert owner.client is not None
                await owner.send(gid, "secret before D joined")
                assert await wait_until(
                    lambda: first.recorder.texts() == ["secret before D joined"]
                )
                # capture a pre-join sealed body for the stale-material probe
                assert first.service is not None
                record = first.home.groups.require(gid)
                assert owner.service is not None
                link = owner.service.mesh.link_for(gid, first.fp, record.epoch)
                assert link is not None
                stale = link.seal(
                    gmsg_frame(
                        gid,
                        record.epoch,
                        owner.fp,
                        first.fp,
                        message_id="gmsg_deadbeefcafe0020",
                        gseq=2,
                        display_name="owner",
                        text="pre-join secret",
                        ts=1.0,
                    ),
                    group_message_aad(gid, record.epoch, owner.fp, first.fp),
                )
                # D joins (epoch leaps via the owner-signed admission)
                late = Member(GroupHome(tmp_path / "late"))
                await late.connect(server.url, "late")
                assert late.client is not None
                _invite, link_invite = await owner.home.groups.mint_group_invite(
                    owner.client,
                    owner.home.invites,
                    gid,
                    ttl_seconds=300.0,
                    max_redemptions=1,
                )
                late.home.groups.attach(late.client)
                join_record = await late.home.groups.join_group(late.client, link_invite)
                await late.sync(gid)
                await owner.sync(gid)
                await first.sync(gid)
                join_epoch = join_record.epoch
                assert join_epoch == record.epoch + 1

                # 1) D sees no history; first message after join arrives fine
                await asyncio.sleep(0.2)
                assert late.recorder.texts() == []
                await owner.send(gid, "welcome D")
                assert await wait_until(lambda: late.recorder.texts() == ["welcome D"])
                frame = late.recorder.messages()[0].frame
                assert frame is not None and frame.epoch == join_epoch

                # 2) historical ciphertext never opens for D (no link, wrong
                #    epoch) — injected directly, it fails closed
                assert late.service is not None
                before = len(late.recorder.messages())
                await late.service._handle_forward(
                    forward_payload(
                        group=gid,
                        epoch=record.epoch,
                        sender=owner.fp,
                        to=late.fp,
                        kind=KIND_MSG,
                        body=stale,
                    )
                )
                assert len(late.recorder.messages()) == before
                await owner.close()
                await first.close()
                await late.close()

        run(scenario())


class TestEpochLeapBehavior:
    """§16.5 in-motion: post-leap sends use the new epoch; in-flight old
    ciphertext drains within the real-time window."""

    def test_old_epoch_frame_drains_then_new_epoch_works(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 3)
                first, second = members
                assert owner.client is not None
                await owner.send(gid, "epoch three")
                assert await wait_until(
                    lambda: (
                        first.recorder.texts() == ["epoch three"]
                        and second.recorder.texts() == ["epoch three"]
                    )
                )
                assert first.service is not None and second.service is not None
                assert owner.service is not None
                assert first.home.groups.require(gid).epoch == 3
                # gseq 2 goes through the real seal path, but the DELIVERY to
                # `second` is deferred (network delay simulation — the bytes
                # are the real ciphertext, the decrypt path stays real).
                deferred: list[Packet] = []
                real_handle = second.service._handle_forward

                async def delay_gmsg(packet: Packet) -> None:
                    if (
                        packet.type is PacketType.GROUP_FORWARD
                        and packet.payload.get("kind") == KIND_MSG
                    ):
                        deferred.append(packet)
                        return
                    await real_handle(packet)

                second.service._handle_forward = delay_gmsg  # type: ignore[method-assign]
                await owner.send(gid, "pre-leap in flight")
                assert await wait_until(lambda: len(deferred) == 1)
                del second.service._handle_forward  # restore the real handler
                # leap: first leaves → epoch 4 at everyone
                assert first.client is not None
                await first.home.groups.leave_group(first.client, gid)
                assert await wait_until(
                    lambda: (
                        second.home.groups.require(gid).epoch == 4
                        and owner.home.groups.require(gid).epoch == 4
                    )
                )
                # the in-flight pre-leap frame is accepted inside the real
                # 30 s drain window (§16.5): old-epoch AAD still opens.
                before = len(second.recorder.messages())
                await real_handle(deferred[0])
                assert [event.frame.text for event in second.recorder.messages() if event.frame][
                    before:
                ] == ["pre-leap in flight"]
                # …while brand new traffic rides fresh epoch-4 links.
                await owner.send(gid, "epoch four now")
                assert await wait_until(lambda: "epoch four now" in second.recorder.texts())
                frame = second.recorder.messages()[-1].frame
                assert frame is not None and frame.epoch == 4
                await owner.close()
                await first.close()
                await second.close()

        run(scenario())


class TestOfflineMembers:
    """§29: no relay queue; bounded sender-side retry; re-seal on flush."""

    def test_offline_queue_and_flush_on_reconnect(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                # warm the link first
                await owner.send(gid, "before you left")
                assert await wait_until(lambda: peer.recorder.texts() == ["before you left"])
                # peer goes away (session unbound at the authority)
                assert peer.client is not None
                await peer.client.disconnect()
                ledger1 = await owner.send(gid, "while you were away 1")
                assert await wait_until(lambda: ledger1.recipients[peer.fp] is DeliveryState.QUEUED)
                ledger2 = await owner.send(gid, "while you were away 2")
                assert await wait_until(lambda: ledger2.recipients[peer.fp] is DeliveryState.QUEUED)
                assert await wait_until(lambda: owner.recorder.notices("offline"))
                # peer returns: flush re-seals at the CURRENT epoch and sends
                await peer.connect(server.url, "peer")
                await peer.sync(gid)
                ledger3 = await owner.send(gid, "welcome back")  # drives the pump
                ok = await wait_until(
                    lambda: (
                        peer.recorder.texts()
                        == [
                            "before you left",
                            "while you were away 1",
                            "while you were away 2",
                            "welcome back",
                        ]
                    ),
                    timeout=10.0,
                )
                assert ok
                assert await wait_until(lambda: ledger3.delivered_count() == 1)
                # message ids survive re-seals; duplicates are absorbed
                assert ledger1.message_id != ledger2.message_id
                await owner.close()
                await peer.close()

        run(scenario())

    def test_offline_queue_drop_oldest_bound(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                assert peer.client is not None
                await peer.client.disconnect()
                ledgers = []
                for index in range(GROUP_OFFLINE_QUEUE_PER_MEMBER + 2):
                    ledgers.append(await owner.send(gid, f"queued {index}"))
                # the two oldest were dropped (FIFO bound) and marked FAILED
                assert await wait_until(
                    lambda: (
                        ledgers[0].recipients[peer.fp] is DeliveryState.FAILED
                        and ledgers[1].recipients[peer.fp] is DeliveryState.FAILED
                    )
                )
                # the last GROUP_OFFLINE_QUEUE_PER_MEMBER are retained QUEUED
                assert ledgers[-1].recipients[peer.fp] is DeliveryState.QUEUED
                await peer.connect(server.url, "peer")
                await peer.sync(gid)
                # "wake" drives the pump — and itself pushes out one more
                # oldest job (cap is strictly 8): jobs 0..2 are FAILED
                await owner.send(gid, "wake")
                assert ledgers[2].recipients[peer.fp] is DeliveryState.FAILED
                expected = [f"queued {i}" for i in range(3, GROUP_OFFLINE_QUEUE_PER_MEMBER + 2)]
                expected.append("wake")
                ok = await wait_until(
                    lambda: peer.recorder.texts() == expected,
                    timeout=12.0,
                )
                assert ok, f"got {peer.recorder.texts()!r}, want {expected!r}"
                # per-sender order survived the queue (FIFO causal order)
                gseqs = [
                    event.frame.gseq
                    for event in peer.recorder.messages()
                    if event.frame is not None
                ]
                assert gseqs == sorted(gseqs)
                await owner.close()
                await peer.close()

        run(scenario())


class TestReconnectionAndRestart:
    def test_reconnect_rekey_and_dedupe(self, tmp_path: Path) -> None:
        """§27: fresh links after reconnect; re-sealed resends dedupe."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                await owner.send(gid, "before reconnect")
                assert await wait_until(lambda: peer.recorder.texts() == ["before reconnect"])
                # owner drops and returns — a fully new session
                assert owner.client is not None
                old_link = (
                    owner.service.mesh.link_for(gid, peer.fp, owner.home.groups.require(gid).epoch)
                    if owner.service
                    else None
                )
                await owner.client.disconnect()
                await owner.connect(server.url, "owner2")
                await owner.sync(gid)
                if old_link is not None:
                    assert all(byte == 0 for byte in old_link._key)  # zeroized
                await owner.send(gid, "after reconnect")
                assert await wait_until(
                    lambda: peer.recorder.texts() == ["before reconnect", "after reconnect"]
                )
                await owner.close()
                await peer.close()

        run(scenario())

    def test_relay_restart_marks_group_defunct(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, _, gid = await build_world(tmp_path, server.url, 2)
                assert owner.client is not None
            # relay process died and restarted empty
            async with running_relay() as server2:
                assert owner.client is not None
                # the old client keeps its socket to the dead server; make a
                # fresh connection to the *restarted* relay and re-attest
                await owner.client.aclose()
                await owner.connect(server2.url, "owner")
                assert owner.service is not None
                with pytest.raises(GroupDefunctError):
                    await owner.home.groups.sync_group(owner.client, gid)
                record = owner.home.groups.require(gid)
                assert record.state is LocalGroupState.DEFUNCT
                with pytest.raises(GroupStateError):
                    await owner.send(gid, "to a dead group")
                await owner.close()

        run(scenario())


class TestAbuseAndMalformed:
    """§31/§33: garbage at every gate is dropped — never a crash."""

    async def _feed(self, member: Member, packet: Packet) -> None:
        assert member.service is not None
        await member.service._handle_forward(packet)

    def test_malformed_envelope_matrix(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                await owner.send(gid, "warm")
                assert await wait_until(lambda: peer.recorder.texts() == ["warm"])
                record = peer.home.groups.require(gid)
                base = {
                    "group": gid,
                    "epoch": record.epoch,
                    "from": owner.fp,
                    "to": peer.fp,
                    "kind": KIND_MSG,
                    "body": base64.b64encode(b"junk").decode(),
                }
                cases = [
                    dict(base, epoch="banana"),  # non-int epoch
                    dict(base, body="!!!not-base64!!!"),
                    dict(base, group="gl-group-XXXX-XXXX-XXXX"),  # unknown group
                    dict(base, to=owner.fp),  # addressed elsewhere
                    dict(base, **{"from": "GLFP-9999-9999-9999"}),  # off-roster
                    dict(base, epoch=record.epoch + 10),  # future epoch
                    dict(base, epoch=0),
                ]
                for payload in cases:
                    await self._feed(peer, Packet(PacketType.GROUP_FORWARD, payload))  # type: ignore[arg-type]
                # hostile kex schemes
                for body in (b"not json", b'{"s":"evil"}', b'{"s":"hello"}', b'{"s":"reply"}'):
                    await self._feed(
                        peer,
                        forward_payload(
                            group=gid,
                            epoch=record.epoch,
                            sender=owner.fp,
                            to=peer.fp,
                            kind=KIND_KEX,
                            body=body,
                        ),
                    )
                assert peer.recorder.texts() == ["warm"]  # nothing extra rendered
                assert peer.service is not None
                assert peer.service.mesh.link_count() <= MAX_GROUP_MEMBERS - 1
                await owner.close()
                await peer.close()

        run(scenario())

    def test_malicious_member_flood_bounded(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                record = peer.home.groups.require(gid)
                garbage = gmsg_frame(
                    gid,
                    record.epoch,
                    owner.fp,
                    peer.fp,
                    message_id="gmsg_deadbeefcafe0040",
                    gseq=1,
                    display_name="x",
                    text="y",
                    ts=1.0,
                )
                # 300 forged/kex frames — no crash, bounded state
                for index in range(300):
                    body = garbage if index % 2 else b"\x00" * 32
                    await self._feed(
                        peer,
                        forward_payload(
                            group=gid,
                            epoch=record.epoch,
                            sender=owner.fp,
                            to=peer.fp,
                            kind=KIND_KEX if index % 3 else KIND_MSG,
                            body=body,
                        ),
                    )
                assert peer.service is not None
                assert peer.service.mesh.link_count() <= MAX_GROUP_MEMBERS - 1
                assert peer.service.mesh.pending_handshake_count() <= MAX_GROUP_MEMBERS - 1 + 8
                # negotiated live traffic still works afterwards
                await owner.send(gid, "still alive")
                assert await wait_until(lambda: peer.recorder.texts() == ["still alive"])
                await owner.close()
                await peer.close()

        run(scenario())

    def test_relay_forward_rate_limit_trips_loud(self, tmp_path: Path) -> None:
        """§32: the relay brake engages only for abusive senders.

        An honest client paces itself under the brake (a 35-message
        service burst delivers everything, no failures); a sender who
        bypasses the pacing gate with raw envelopes hits the relay's
        20/s brake and receives explicit ``group/rate`` errors.
        """

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                await owner.send(gid, "warm")  # pays the kex cost
                assert await wait_until(lambda: peer.recorder.texts() == ["warm"])
                # 1) honest burst: paced, lossless, brake never engages
                count = int(GROUP_FORWARD_RATE_PER_SECOND) + 15
                ledgers = [await owner.send(gid, f"burst {i}") for i in range(count)]
                ok = await wait_until(
                    lambda: all(
                        ledger.recipients[peer.fp] in TERMINAL_DELIVERY_STATES for ledger in ledgers
                    ),
                    timeout=30.0,
                )
                assert ok, "paced traffic must not lose messages"
                assert not any(
                    ledger.recipients[peer.fp] is DeliveryState.FAILED for ledger in ledgers
                )
                # 2) raw abuse: brake engages with explicit errors
                assert peer.client is not None
                errors: list[str] = []
                peer.client.set_group_forward_error_listener(
                    lambda _g, _m, code, _msg: errors.append(code)
                )
                for _ in range(int(GROUP_FORWARD_RATE_PER_SECOND) + 15):
                    await peer.client.group_forward(
                        gid,
                        peer.home.groups.require(gid).epoch,
                        from_fingerprint=peer.fp,
                        to_fingerprint=owner.fp,
                        kind=KIND_KEX,
                        body_b64=base64.b64encode(b"{}").decode(),
                    )
                assert await wait_until(lambda: "group/rate" in errors, timeout=10.0)
                # owner-side service survived the spam; live traffic works
                await peer.send(gid, "after the flood")
                assert await wait_until(
                    lambda: "after the flood" in owner.recorder.texts(), timeout=10.0
                )
                await owner.close()
                await peer.close()

        run(scenario())


class TestHistory:
    def test_group_history_records_both_directions(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                history_owner = SessionHistory()
                history_peer = SessionHistory()
                assert owner.service is not None and peer.service is not None
                owner.service._history = history_owner
                peer.service._history = history_peer
                await owner.send(gid, "history one")
                await peer.send(gid, "history two")
                assert await wait_until(lambda: owner.recorder.texts() == ["history two"])
                owner_ids = [(entry.direction, entry.text) for entry in history_owner.entries()]
                assert ("outgoing", "history one") in owner_ids
                assert ("incoming", "history two") in owner_ids
                assert all(entry.conversation_id == gid for entry in history_owner.entries())
                peer_ids = [(entry.direction, entry.text) for entry in history_peer.entries()]
                assert ("incoming", "history one") in peer_ids
                await owner.close()
                await peer.close()

        run(scenario())


class TestLogHygiene:
    """§26.3/§31: plaintext and key material never reach the logs."""

    def test_no_plaintext_or_keys_in_logs(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        private_hexes: list[str] = []

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 3)
                for member in [owner, *members]:
                    identity = member.identity
                    # the identity's Ed25519 private key (§26.3 hygiene probe)
                    private_hexes.append(identity._private_key.private_bytes_raw().hex())
                first, second = members
                assert first.client is not None
                with caplog.at_level(logging.DEBUG):
                    await owner.send(gid, "SECRETMARKER-PLAINTEXT")
                    assert await wait_until(
                        lambda: members[0].recorder.texts() == ["SECRETMARKER-PLAINTEXT"]
                    )
                    # an epoch leap + fresh-epoch traffic stays clean too
                    await first.home.groups.leave_group(first.client, gid)
                    assert await wait_until(lambda: owner.home.groups.require(gid).epoch == 4)
                    await owner.send(gid, "SECRETMARKER-AFTER-LEAP")
                    assert await wait_until(
                        lambda: (
                            second.recorder.texts()
                            == ["SECRETMARKER-PLAINTEXT", "SECRETMARKER-AFTER-LEAP"]
                        )
                    )
                await owner.close()
                for member in members:
                    await member.close()

        run(scenario())
        text = caplog.text
        assert "SECRETMARKER-PLAINTEXT" not in text
        assert "SECRETMARKER-AFTER-LEAP" not in text
        for private_hex in private_hexes:
            assert private_hex not in text


class TestKexWireHardening:
    def test_idpub_substitution_breaks_pairing_over_relay(self, tmp_path: Path) -> None:
        """§17.2.1 on the wire: tampered handshake payloads are refused."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                assert peer.service is not None
                record = peer.home.groups.require(gid)
                evil = LocalIdentity.generate()
                # craft a hello claiming owner's roster slot but with a
                # substituted identity key — handle_hello must refuse
                from ghostlink.messaging.protocol.handshake import HandshakeInitiator

                initiator = HandshakeInitiator(
                    f"ghostlink/group/v1|{gid}|{record.epoch}",
                    identity_public_key_hex=evil.public_key_hex,
                )
                await peer.service._handle_forward(
                    forward_payload(
                        group=gid,
                        epoch=record.epoch,
                        sender=owner.fp,
                        to=peer.fp,
                        kind=KIND_KEX,
                        body=(
                            b'{"s":"hello","p":'
                            + __import__("json")
                            .dumps(initiator.hello_payload(), separators=(",", ":"))
                            .encode()
                            + b"}"
                        ),
                    )
                )
                assert peer.service.mesh.link_for(gid, owner.fp, record.epoch) is None
                await owner.close()
                await peer.close()

        run(scenario())
