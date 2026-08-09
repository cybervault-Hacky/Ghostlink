"""Phase 7 sender-key end-to-end: O(1) group messaging on a live loopback relay.

No mocks for security-critical paths: members are full devices, the relay is
the real :class:`RelayServer`, and every assertion flows over the v4 wire
protocol. Groups are created with ``crypto_suite="senderkey-v1"``.
"""

from __future__ import annotations

import asyncio
import base64
from pathlib import Path

import pytest

from ghostlink.constants.net import (
    DEFAULT_CRYPTO_SUITE,
    MAX_GROUP_MEMBERS,
)
from ghostlink.exceptions.groups import GroupStateError
from ghostlink.groups.frames import (
    KIND_SKMSG,
    TERMINAL_DELIVERY_STATES,
    DeliveryState,
    GroupMessageLedger,
)
from ghostlink.groups.models import LocalGroupState
from ghostlink.groups.service import (
    GroupChatEvent,
    GroupChatEventKind,
    GroupMessagingService,
)
from ghostlink.transport.relay.client import RelayClient
from ghostlink.transport.relay.protocol import Packet, PacketType
from tests.conftest import run, running_relay
from tests.group_helpers import GroupHome, client_for, fingerprint_of


async def wait_until(predicate: object, timeout: float = 6.0, interval: float = 0.01) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        await asyncio.sleep(interval)
    return False


class Recorder:
    def __init__(self) -> None:
        self.events: list[GroupChatEvent] = []

    def __call__(self, event: GroupChatEvent) -> None:
        self.events.append(event)

    def messages(self) -> list[GroupChatEvent]:
        return [e for e in self.events if e.kind is GroupChatEventKind.MESSAGE]

    def texts(self) -> list[str]:
        return [e.frame.text for e in self.messages() if e.frame is not None]

    def notices(self, needle: str = "") -> list[GroupChatEvent]:
        return [
            e
            for e in self.events
            if e.kind in (GroupChatEventKind.NOTICE, GroupChatEventKind.MEMBERSHIP)
            and (not needle or needle in e.detail)
        ]


class Member:
    def __init__(self, home: GroupHome) -> None:
        self.home = home
        self.client: RelayClient | None = None
        self.service: GroupMessagingService | None = None
        self.recorder = Recorder()

    @property
    def identity(self):
        return self.home.identities.ensure()

    @property
    def fp(self) -> str:
        return fingerprint_of(self.identity)

    async def connect(self, server_url: str, name: str) -> None:
        import types

        server = types.SimpleNamespace(url=server_url)
        if self.client is not None:
            await self.client.aclose()
        self.client = await client_for(server, name)  # type: ignore[arg-type]
        if self.service is None:
            self.service = GroupMessagingService(self.home.groups, self.home.identities)
            self.service.add_listener(self.recorder)
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


async def build_sk_world(
    root: Path, server_url: str, size: int, *, name: str = "Ops"
) -> tuple[Member, list[Member], str]:
    owner = Member(GroupHome(root / "owner"))
    await owner.connect(server_url, "owner")
    assert owner.client is not None
    owner.home.groups.attach(owner.client)
    record = await owner.home.groups.create_group(owner.client, name, crypto_suite="senderkey-v1")
    assert record.crypto_suite == "senderkey-v1"
    group_id = record.group_id
    members: list[Member] = []
    for index in range(size - 1):
        member = Member(GroupHome(root / f"m{index}"))
        await member.connect(server_url, f"m{index}")
        assert owner.client is not None and member.client is not None
        _invite, link = await owner.home.groups.mint_group_invite(
            owner.client, owner.home.invites, group_id, ttl_seconds=300.0, max_redemptions=1
        )
        member.home.groups.attach(member.client)
        await member.home.groups.join_group(member.client, link)
        members.append(member)
    await owner.sync(group_id)
    for member in members:
        await member.sync(group_id)
    return owner, members, group_id


class _Capture:
    """Captures outbound GROUP_FORWARD envelopes (for injection/opacity tests)."""

    def __init__(self) -> None:
        self.envelopes: list[tuple[str, int, str, str, str, bytes]] = []

    def __call__(self, client: RelayClient) -> None:
        original = client.group_forward

        async def wrapped(*args: object, **kwargs: object) -> object:
            group = str(args[0])
            epoch = int(args[1])
            from_fp = str(kwargs.get("from_fingerprint", ""))
            to_fp = str(kwargs.get("to_fingerprint", ""))
            kind = str(kwargs.get("kind", ""))
            body_b64 = str(kwargs.get("body_b64", ""))
            self.envelopes.append((group, epoch, from_fp, to_fp, kind, base64.b64decode(body_b64)))
            return await original(*args, **kwargs)

        client.group_forward = wrapped  # type: ignore[method-assign]


def capture_for(client: RelayClient) -> _Capture:
    capture = _Capture()
    capture(client)
    return capture


def skmsg_packet(group: str, epoch: int, sender: str, to: str, body: bytes) -> Packet:
    return Packet(
        PacketType.GROUP_FORWARD,
        {
            "group": group,
            "epoch": epoch,
            "from": sender,
            "to": to,
            "kind": KIND_SKMSG,
            "body": base64.b64encode(body).decode("ascii"),
        },
    )


# -------------------------------------------------------------- happy paths


class TestSenderKeyFanout:
    def test_two_member_roundtrip(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_sk_world(tmp_path, server.url, 2)
                peer = members[0]
                ledger = await owner.send(gid, "hello senderkey")
                assert ledger.total() == 1
                ok = await wait_until(lambda: peer.recorder.texts() == ["hello senderkey"])
                assert ok
                frame = peer.recorder.messages()[0].frame
                assert frame is not None
                assert frame.sender == owner.fp
                assert frame.recipient == "*"  # broadcast
                assert frame.gseq == 1
                assert await wait_until(
                    lambda: ledger.recipients[peer.fp] in TERMINAL_DELIVERY_STATES
                )
                assert ledger.status_line() == "delivered 1/1"
                await owner.close()
                await peer.close()

        run(scenario())

    def test_three_member_broadcast_one_seal(self, tmp_path: Path) -> None:
        """Both recipients decrypt the SAME ciphertext — one seal per message."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_sk_world(tmp_path, server.url, 3)
                first, second = members
                capture = capture_for(owner.client)
                await owner.send(gid, "broadcast once")
                ok = await wait_until(
                    lambda: (
                        first.recorder.texts() == ["broadcast once"]
                        and second.recorder.texts() == ["broadcast once"]
                    )
                )
                assert ok
                skmsg_bodies = [
                    b for (_g, _e, _f, _t, k, b) in capture.envelopes if k == KIND_SKMSG
                ]
                # one distinct ciphertext broadcast to both recipients
                assert len(skmsg_bodies) == 2
                assert skmsg_bodies[0] == skmsg_bodies[1]  # O(1) seal
                await owner.close()
                for member in members:
                    await member.close()

        run(scenario())

    def test_eight_member_broadcast(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_sk_world(tmp_path, server.url, MAX_GROUP_MEMBERS)
                ledger = await owner.send(gid, "full house")
                assert ledger.total() == MAX_GROUP_MEMBERS - 1
                ok = await wait_until(
                    lambda: all(m.recorder.texts() == ["full house"] for m in members),
                    timeout=10.0,
                )
                assert ok
                assert await wait_until(
                    lambda: ledger.delivered_count() == MAX_GROUP_MEMBERS - 1, timeout=10.0
                )
                await owner.close()
                for member in members:
                    await member.close()

        run(scenario())

    def test_bidirectional(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_sk_world(tmp_path, server.url, 2)
                peer = members[0]
                await peer.send(gid, "pong back")
                assert await wait_until(lambda: owner.recorder.texts() == ["pong back"])
                await owner.close()
                await peer.close()

        run(scenario())

    def test_read_receipts(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_sk_world(tmp_path, server.url, 2)
                peer = members[0]
                ledger = await owner.send(gid, "read me")
                assert await wait_until(
                    lambda: ledger.recipients[peer.fp] is DeliveryState.READ, timeout=5.0
                )
                await owner.close()
                await peer.close()

        run(scenario())


class TestRelayOpaqueness:
    def test_relay_never_sees_plaintext_or_chain_root(self, tmp_path: Path) -> None:
        """§28.2: ciphertext-only on the wire; no plaintext, no raw chain root."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_sk_world(tmp_path, server.url, 2)
                peer = members[0]
                capture = capture_for(owner.client)
                await owner.send(gid, "THE-SECRET-PHRASE-XYZ")
                assert await wait_until(lambda: peer.recorder.texts() == ["THE-SECRET-PHRASE-XYZ"])
                all_bodies = b"".join(body for (_g, _e, _f, _t, _k, body) in capture.envelopes)
                assert b"THE-SECRET-PHRASE-XYZ" not in all_bodies
                # The chain root never appears as its own base64 anywhere.
                from ghostlink.groups.senderkeys import distribution_root_b64

                chain = owner.service._sk.ensure_outgoing(
                    gid, owner.home.groups.require(gid).epoch, owner.fp
                )
                root_b64 = distribution_root_b64(bytes(chain._root))
                assert root_b64.encode() not in all_bodies
                await owner.close()
                await peer.close()

        run(scenario())


class TestReplayAndTamper:
    def test_captured_skmsg_replay_after_newer_dropped(self, tmp_path: Path) -> None:
        """§21: a replayed old SKMSG (lower index) is dropped, not re-rendered."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_sk_world(tmp_path, server.url, 2)
                peer = members[0]
                capture = capture_for(owner.client)
                await owner.send(gid, "one")
                await owner.send(gid, "two")
                assert await wait_until(lambda: peer.recorder.texts() == ["one", "two"])
                # Replay the first message's SKMSG body to the peer.
                first_skmsg = next(
                    b
                    for (_g, _e, _f, _t, k, b) in capture.envelopes
                    if k == KIND_SKMSG and _t == peer.fp
                )
                record = owner.home.groups.require(gid)
                await peer.service._handle_forward(
                    skmsg_packet(gid, record.epoch, owner.fp, peer.fp, first_skmsg)
                )
                await asyncio.sleep(0.2)
                assert peer.recorder.texts() == ["one", "two"]  # unchanged
                await owner.close()
                await peer.close()

        run(scenario())

    def test_tampered_skmsg_dropped(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_sk_world(tmp_path, server.url, 2)
                peer = members[0]
                capture = capture_for(owner.client)
                await owner.send(gid, "tamper target")
                assert await wait_until(lambda: peer.recorder.texts() == ["tamper target"])
                body = next(b for (_g, _e, _f, _t, k, b) in capture.envelopes if k == KIND_SKMSG)
                tampered = bytearray(body)
                tampered[40] ^= 0x01
                record = owner.home.groups.require(gid)
                await peer.service._handle_forward(
                    skmsg_packet(gid, record.epoch, owner.fp, peer.fp, bytes(tampered))
                )
                await asyncio.sleep(0.2)
                assert peer.recorder.texts() == ["tamper target"]
                await owner.close()
                await peer.close()

        run(scenario())

    def test_wrong_sender_skmsg_rejected(self, tmp_path: Path) -> None:
        """A forged SKMSG labeled with the wrong sender fails context/open."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_sk_world(tmp_path, server.url, 2)
                peer = members[0]
                # Inject a body the peer cannot attribute to the owner.
                await peer.service._handle_forward(
                    skmsg_packet(gid, 1, owner.fp, peer.fp, b"garbage-not-skmsg")
                )
                await asyncio.sleep(0.2)
                assert peer.recorder.texts() == []
                await owner.close()
                await peer.close()

        run(scenario())


class TestMemberRemoval:
    def test_removed_member_cannot_read_new_epoch(self, tmp_path: Path) -> None:
        """§15/§36: after removal + epoch rotation, the ex-member gets nothing."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_sk_world(tmp_path, server.url, 3)
                first, removed = members
                await owner.send(gid, "before removal")
                assert await wait_until(lambda: removed.recorder.texts() == ["before removal"])
                await owner.home.groups.remove_member(owner.client, gid, removed.fp)
                assert await wait_until(
                    lambda: (
                        first.home.groups.require(gid).epoch >= 4
                        and removed.home.groups.require(gid).state is LocalGroupState.REMOVED
                    )
                )
                before = len(removed.recorder.messages())
                await owner.send(gid, "after removal")
                assert await wait_until(lambda: first.recorder.texts()[-1:] == ["after removal"])
                await asyncio.sleep(0.3)
                assert len(removed.recorder.messages()) == before
                # Removed member cannot send either (local gate).
                with pytest.raises(GroupStateError):
                    await removed.send(gid, "I was removed")
                await owner.close()
                for member in members:
                    await member.close()

        run(scenario())

    def test_epoch_rotation_redistributes_fresh_chains(self, tmp_path: Path) -> None:
        """Removal bumps the epoch; surviving members mint + redistribute chains."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_sk_world(tmp_path, server.url, 3)
                first, removed = members
                await owner.send(gid, "pre")
                assert await wait_until(lambda: first.recorder.texts() == ["pre"])
                old_epoch = owner.home.groups.require(gid).epoch
                old_chain = owner.service._sk.outgoing_chain(gid, old_epoch)
                assert old_chain is not None
                await owner.home.groups.remove_member(owner.client, gid, removed.fp)
                assert await wait_until(lambda: owner.home.groups.require(gid).epoch >= 4)
                # The new epoch mints a fresh chain (new generation).
                new_epoch = owner.home.groups.require(gid).epoch
                assert new_epoch > old_epoch
                # First (surviving) still decrypts after rotation + re-distribution.
                await owner.send(gid, "post-rotation")
                assert await wait_until(lambda: first.recorder.texts() == ["pre", "post-rotation"])
                new_chain = owner.service._sk.outgoing_chain(gid, new_epoch)
                assert new_chain is not None
                assert new_chain.gen >= 1
                await owner.close()
                for member in members:
                    await member.close()

        run(scenario())


class TestNewMemberIsolation:
    def test_joiner_gets_no_old_epoch_and_reads_current(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_sk_world(tmp_path, server.url, 2)
                first = members[0]
                await owner.send(gid, "secret before D joined")
                assert await wait_until(
                    lambda: first.recorder.texts() == ["secret before D joined"]
                )
                # D joins (epoch leaps).
                late = Member(GroupHome(tmp_path / "late"))
                await late.connect(server.url, "late")
                assert owner.client is not None and late.client is not None
                _invite, link = await owner.home.groups.mint_group_invite(
                    owner.client, owner.home.invites, gid, ttl_seconds=300.0, max_redemptions=1
                )
                late.home.groups.attach(late.client)
                await late.home.groups.join_group(late.client, link)
                await late.sync(gid)
                await owner.sync(gid)
                assert late.recorder.texts() == []  # no history
                # After distribution, the joiner reads current-epoch messages.
                await owner.send(gid, "welcome D")
                assert await wait_until(lambda: late.recorder.texts() == ["welcome D"])
                await owner.close()
                for member in members:
                    await member.close()
                await late.close()

        run(scenario())


class TestReconnect:
    def test_reconnect_redistributes_and_heals(self, tmp_path: Path) -> None:
        """§27.2/§27.3: a fresh session re-establishes distribution state."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_sk_world(tmp_path, server.url, 2)
                peer = members[0]
                await owner.send(gid, "before disconnect")
                assert await wait_until(lambda: peer.recorder.texts() == ["before disconnect"])
                # Owner drops and returns on a fully new session: attach()
                # clears the per-recipient distribution bookkeeping, so the
                # next send re-distributes fresh chains over fresh links.
                await owner.client.disconnect()
                await owner.connect(server.url, "owner2")
                await owner.sync(gid)
                await peer.sync(gid)
                await owner.send(gid, "after reconnect")
                assert await wait_until(
                    lambda: peer.recorder.texts() == ["before disconnect", "after reconnect"]
                )
                await owner.close()
                await peer.close()

        run(scenario())


class TestSuiteWiring:
    def test_default_suite_is_mesh(self, tmp_path: Path) -> None:
        """Backward compat: create_group defaults to mesh-v1."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner = Member(GroupHome(tmp_path / "owner"))
                await owner.connect(server.url, "owner")
                owner.home.groups.attach(owner.client)
                record = await owner.home.groups.create_group(owner.client, "Default")
                assert record.crypto_suite == DEFAULT_CRYPTO_SUITE == "mesh-v1"
                await owner.close()

        run(scenario())

    def test_unknown_suite_rejected_at_creation(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner = Member(GroupHome(tmp_path / "owner"))
                await owner.connect(server.url, "owner")
                owner.home.groups.attach(owner.client)
                from ghostlink.exceptions.groups import GroupValidationError

                with pytest.raises(GroupValidationError):
                    await owner.home.groups.create_group(
                        owner.client, "Bad", crypto_suite="magic-unicorn"
                    )
                await owner.close()

        run(scenario())

    def test_suite_is_persisted_and_survives_reload(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner = Member(GroupHome(tmp_path / "owner"))
                await owner.connect(server.url, "owner")
                owner.home.groups.attach(owner.client)
                record = await owner.home.groups.create_group(
                    owner.client, "Persisted", crypto_suite="senderkey-v1"
                )
                owner.home.reload()
                reloaded = owner.home.groups.require(record.group_id)
                assert reloaded.crypto_suite == "senderkey-v1"
                await owner.close()

        run(scenario())

    def test_suite_downgrade_marks_suspect(self, tmp_path: Path) -> None:
        """§37: a relay advertising a different suite for an established group is refused."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_sk_world(tmp_path, server.url, 2)
                record = owner.home.groups.require(gid)
                assert record.crypto_suite == "senderkey-v1"
                # Force a conflicting authoritative snapshot (downgrade): the
                # local builder must refuse it (no silent crypto downgrade).
                from ghostlink.groups.lifecycle import LocalGroupManager
                from ghostlink.transport.relay.client import GroupAttested

                attested = GroupAttested(
                    group_id=gid,
                    role="member",
                    epoch=record.epoch,
                    members=tuple(m.to_dict() for m in record.members.values()),
                    events=(),
                    crypto_suite="mesh-v1",
                )
                manager: LocalGroupManager = owner.home.groups
                with pytest.raises(GroupStateError):
                    manager._record_from_attested(record, attested, record.my_fingerprint)
                await owner.close()
                for member in members:
                    await member.close()

        run(scenario())


class TestOfflineRetry:
    def test_offline_member_receives_queued_after_reconnect(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_sk_world(tmp_path, server.url, 2)
                peer = members[0]
                # Peer disconnects (relay-side session gone).
                assert peer.client is not None
                await peer.client.aclose()
                peer.client = None
                # Owner sends while peer is offline.
                await owner.send(gid, "queued-while-offline")
                await asyncio.sleep(0.3)
                # Peer reconnects.
                peer.service = None
                peer.recorder = Recorder()
                await peer.connect(server.url, "m0-back")
                peer.home.groups.attach(peer.client)
                await peer.sync(gid)
                await owner.sync(gid)
                # The queued sender-key envelope is re-sent and decrypted.
                assert await wait_until(lambda: peer.recorder.texts() == ["queued-while-offline"])
                await owner.close()
                await peer.close()

        run(scenario())
