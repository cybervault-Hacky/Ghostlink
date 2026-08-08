"""LocalGroupManager end to end (Phase 6B): real relay, real identities.

Includes the required concurrency, persistence, and hygiene coverage:
8-member joins racing the authority, the 9th being refused, restart
persistence, corrupt-state handling, success-only join persistence, and
secret/log hygiene assertions.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

import pytest

from ghostlink.constants.net import MAX_GROUP_MEMBERS
from ghostlink.exceptions.config import ConfigValidationError
from ghostlink.exceptions.groups import (
    GroupDefunctError,
    GroupFullError,
    GroupJoinTimeoutError,
    GroupNotMemberError,
    GroupPermissionError,
    GroupStateError,
    GroupUnknownError,
)
from ghostlink.exceptions.storage import StorageCorruptionError
from ghostlink.groups.models import LocalGroupState
from ghostlink.identity.identity import LocalIdentity
from tests.conftest import run, running_relay
from tests.group_helpers import GroupHome, client_for


class TestFullLifecycle:
    def test_create_invite_join_sync_leave_dissolve(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner_home = GroupHome(tmp_path / "owner")
                guest_home = GroupHome(tmp_path / "guest")
                owner = await client_for(server, "owner")
                guest = await client_for(server, "guest")
                notices: list[str] = []
                owner_home.groups.add_notice_listener(lambda _g, n: notices.append(n))

                record = await owner_home.groups.create_group(owner, "Night Watch")
                assert record.epoch == 1 and record.member_count() == 1
                group_id = record.group_id

                invite, link = await owner_home.groups.mint_group_invite(
                    owner,
                    owner_home.invites,
                    group_id,
                    ttl_seconds=120.0,
                    max_redemptions=1,
                )
                assert invite.kind == "group"

                joined = await asyncio.wait_for(
                    guest_home.groups.join_group(guest, link, display_name="Ghost"),
                    timeout=20,
                )
                assert joined.state is LocalGroupState.ACTIVE
                assert joined.epoch == 2
                assert joined.member_count() == 2
                assert joined.my_fingerprint in joined.members
                assert any("joined" in n for n in notices)

                # Both sides agree after sync.
                owner_view = await owner_home.groups.sync_group(owner, group_id)
                guest_view = await guest_home.groups.sync_group(guest, group_id)
                assert owner_view.epoch == guest_view.epoch == 2
                assert set(owner_view.members) == set(guest_view.members)

                # Guest leaves; both sides archive consistently.
                left = await asyncio.wait_for(
                    guest_home.groups.leave_group(guest, group_id), timeout=20
                )
                assert left.state is LocalGroupState.LEFT
                for _ in range(50):  # event delivery is async on the owner side
                    if owner_home.groups.require(group_id).epoch == 3:
                        break
                    await asyncio.sleep(0.05)
                assert owner_home.groups.require(group_id).member_count() == 1

                dissolved = await asyncio.wait_for(
                    owner_home.groups.dissolve_group(owner, group_id), timeout=20
                )
                assert dissolved.state is LocalGroupState.DISSOLVED
                await owner.disconnect()
                await guest.disconnect()

        run(scenario())


class TestAuthorizationFailures:
    def test_member_cannot_mint_invite(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner_home = GroupHome(tmp_path / "owner")
                guest_home = GroupHome(tmp_path / "guest")
                owner = await client_for(server, "owner")
                guest = await client_for(server, "guest")
                group_id = (await owner_home.groups.create_group(owner, "Ops")).group_id
                _invite, link = await owner_home.groups.mint_group_invite(
                    owner,
                    owner_home.invites,
                    group_id,
                    ttl_seconds=60.0,
                    max_redemptions=1,
                )
                await asyncio.wait_for(guest_home.groups.join_group(guest, link), timeout=20)
                with pytest.raises(GroupPermissionError):
                    await guest_home.groups.mint_group_invite(
                        guest,
                        guest_home.invites,
                        group_id,
                        ttl_seconds=60.0,
                        max_redemptions=1,
                    )
                await owner.disconnect()
                await guest.disconnect()

        run(scenario())

    def test_member_cannot_remove_or_dissolve(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner_home = GroupHome(tmp_path / "owner")
                guest_home = GroupHome(tmp_path / "guest")
                owner = await client_for(server, "owner")
                guest = await client_for(server, "guest")
                group_id = (await owner_home.groups.create_group(owner, "Ops")).group_id
                _invite, link = await owner_home.groups.mint_group_invite(
                    owner,
                    owner_home.invites,
                    group_id,
                    ttl_seconds=60.0,
                    max_redemptions=1,
                )
                await asyncio.wait_for(guest_home.groups.join_group(guest, link), timeout=20)
                owner_fp = guest_home.groups.require(group_id).owner_fingerprint
                with pytest.raises(GroupPermissionError):
                    await guest_home.groups.remove_member(guest, group_id, owner_fp)
                with pytest.raises(GroupPermissionError):
                    await guest_home.groups.dissolve_group(guest, group_id)
                await owner.disconnect()
                await guest.disconnect()

        run(scenario())

    def test_owner_cannot_leave(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner_home = GroupHome(tmp_path / "owner")
                owner = await client_for(server, "owner")
                group_id = (await owner_home.groups.create_group(owner, "Ops")).group_id
                with pytest.raises(GroupPermissionError):
                    await owner_home.groups.leave_group(owner, group_id)
                await owner.disconnect()

        run(scenario())

    def test_remove_unknown_member(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner_home = GroupHome(tmp_path / "owner")
                owner = await client_for(server, "owner")
                group_id = (await owner_home.groups.create_group(owner, "Ops")).group_id
                stranger = LocalIdentity.generate()
                from tests.group_helpers import fingerprint_of

                with pytest.raises(GroupNotMemberError):
                    await owner_home.groups.remove_member(owner, group_id, fingerprint_of(stranger))
                await owner.disconnect()

        run(scenario())

    def test_operations_on_unknown_group(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                home = GroupHome(tmp_path / "home")
                client = await client_for(server, "c")
                with pytest.raises(GroupUnknownError):
                    await home.groups.leave_group(client, "gl-group-AAAA-BBBB-CCCC")
                with pytest.raises(GroupUnknownError):
                    await home.groups.sync_group(client, "gl-group-AAAA-BBBB-CCCC")
                await client.disconnect()

        run(scenario())


class TestTerminalSemantics:
    def test_repeated_leave_refused(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner_home = GroupHome(tmp_path / "owner")
                guest_home = GroupHome(tmp_path / "guest")
                owner = await client_for(server, "owner")
                guest = await client_for(server, "guest")
                group_id = (await owner_home.groups.create_group(owner, "Ops")).group_id
                _invite, link = await owner_home.groups.mint_group_invite(
                    owner,
                    owner_home.invites,
                    group_id,
                    ttl_seconds=60.0,
                    max_redemptions=1,
                )
                await asyncio.wait_for(guest_home.groups.join_group(guest, link), timeout=20)
                await asyncio.wait_for(guest_home.groups.leave_group(guest, group_id), timeout=20)
                with pytest.raises(GroupStateError):
                    await guest_home.groups.leave_group(guest, group_id)
                record = guest_home.groups.require(group_id)
                assert record.state is LocalGroupState.LEFT
                await owner.disconnect()
                await guest.disconnect()

        run(scenario())

    def test_archived_terminal_record_can_be_discarded(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                home = GroupHome(tmp_path / "home")
                owner = await client_for(server, "owner")
                group_id = (await home.groups.create_group(owner, "Ops")).group_id
                with pytest.raises(GroupStateError):
                    home.groups.archive_locally(group_id)  # still active
                await asyncio.wait_for(home.groups.dissolve_group(owner, group_id), timeout=20)
                home.groups.archive_locally(group_id)
                with pytest.raises(GroupUnknownError):
                    home.groups.require(group_id)
                await owner.disconnect()

        run(scenario())


class TestConcurrencyToCapacity:
    def test_seven_concurrent_joins_fill_deterministically(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner_home = GroupHome(tmp_path / "owner")
                owner = await client_for(server, "owner")
                group_id = (await owner_home.groups.create_group(owner, "Full House")).group_id
                _invite, link = await owner_home.groups.mint_group_invite(
                    owner,
                    owner_home.invites,
                    group_id,
                    ttl_seconds=120.0,
                    max_redemptions=MAX_GROUP_MEMBERS - 1,
                )
                homes = [GroupHome(tmp_path / f"g{i}") for i in range(MAX_GROUP_MEMBERS - 1)]
                clients = [await client_for(server, f"g{i}") for i in range(len(homes))]

                async def join(home: GroupHome, client) -> tuple[int, str]:
                    record = await asyncio.wait_for(
                        home.groups.join_group(client, link), timeout=30
                    )
                    return record.epoch, record.my_fingerprint

                results = await asyncio.gather(
                    *(join(home, client) for home, client in zip(homes, clients, strict=True))
                )
                # Adopted epochs are ≥ the admission epoch and converge to 8.
                assert all(2 <= epoch <= MAX_GROUP_MEMBERS for epoch, _fp in results)
                assert len({fp for _epoch, fp in results}) == len(results)  # no dup members
                final = await owner_home.groups.sync_group(owner, group_id)
                assert final.member_count() == MAX_GROUP_MEMBERS
                assert final.epoch == MAX_GROUP_MEMBERS
                assert not final.suspect
                # Every member converges to the identical roster + epoch once
                # the event stream finishes delivering.
                for home, _client in zip(homes, clients, strict=True):
                    for _ in range(100):
                        local = home.groups.require(group_id)
                        if local.epoch == final.epoch:
                            break
                        await asyncio.sleep(0.05)
                    assert local.epoch == final.epoch
                    assert local.member_count() == MAX_GROUP_MEMBERS
                    assert set(local.members) == set(final.members)
                    assert not local.suspect
                await owner.disconnect()
                for client in clients:
                    await client.disconnect()

        run(scenario())

    def test_ninth_member_refused_at_capacity(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner_home = GroupHome(tmp_path / "owner")
                owner = await client_for(server, "owner")
                group_id = (await owner_home.groups.create_group(owner, "Packed")).group_id
                homes: list[GroupHome] = []
                clients: list = []
                for index in range(MAX_GROUP_MEMBERS - 1):
                    _invite, link = await owner_home.groups.mint_group_invite(
                        owner,
                        owner_home.invites,
                        group_id,
                        ttl_seconds=60.0,
                        max_redemptions=1,
                    )
                    home = GroupHome(tmp_path / f"g{index}")
                    client = await client_for(server, f"g{index}")
                    await asyncio.wait_for(home.groups.join_group(client, link), timeout=20)
                    homes.append(home)
                    clients.append(client)
                record = await owner_home.groups.sync_group(owner, group_id)
                assert record.member_count() == MAX_GROUP_MEMBERS
                # No invite headroom remains — minting one more must fail.
                with pytest.raises(GroupFullError):
                    await owner_home.groups.mint_group_invite(
                        owner,
                        owner_home.invites,
                        group_id,
                        ttl_seconds=60.0,
                        max_redemptions=1,
                    )
                await owner.disconnect()
                for client in clients:
                    await client.disconnect()

        run(scenario())


class TestJoinTimeout:
    def test_failed_join_persists_nothing(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner_home = GroupHome(tmp_path / "owner")
                guest_home = GroupHome(tmp_path / "guest")
                owner = await client_for(server, "owner")
                group_id = (await owner_home.groups.create_group(owner, "Ops")).group_id
                _invite, link = await owner_home.groups.mint_group_invite(
                    owner,
                    owner_home.invites,
                    group_id,
                    ttl_seconds=60.0,
                    max_redemptions=1,
                )
                guest = await client_for(server, "guest")
                # Owner is connected but never signs (replace its signer with
                # a declining one) — the admission stalls client-side.
                owner_home.groups.attach(owner)
                owner.set_group_signer(lambda _g, _o, _m: None)
                with pytest.raises(GroupJoinTimeoutError):
                    await guest_home.groups.join_group(guest, link, timeout_seconds=1.5)
                assert guest_home.groups.list_all() == []  # success-only persistence
                await owner.disconnect()
                await guest.disconnect()

        run(scenario())


class TestPersistence:
    def test_records_survive_app_restart(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner_home = GroupHome(tmp_path / "owner")
                guest_home = GroupHome(tmp_path / "guest")
                owner = await client_for(server, "owner")
                guest = await client_for(server, "guest")
                group_id = (await owner_home.groups.create_group(owner, "Durable")).group_id
                _invite, link = await owner_home.groups.mint_group_invite(
                    owner,
                    owner_home.invites,
                    group_id,
                    ttl_seconds=60.0,
                    max_redemptions=1,
                )
                await asyncio.wait_for(guest_home.groups.join_group(guest, link), timeout=20)
                # Simulate app restarts: fresh managers over the same storage.
                owner_home.reload()
                guest_home.reload()
                owner_view = owner_home.groups.require(group_id)
                guest_view = guest_home.groups.require(group_id)
                assert owner_view.epoch == 2 and guest_view.epoch == 2
                assert owner_view.member_count() == 2
                assert guest_view.my_fingerprint in owner_view.members
                assert owner_view.state is LocalGroupState.ACTIVE
                assert not owner_view.suspect and not guest_view.suspect
                await owner.disconnect()
                await guest.disconnect()

        run(scenario())

    def test_defunct_on_relay_restart(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            from ghostlink.transport.relay.server import RelayServer

            server = RelayServer(host="127.0.0.1", port=0)
            await server.start()
            port = server.port
            try:
                home = GroupHome(tmp_path / "home")
                owner = await client_for(server, "owner")
                group_id = (await home.groups.create_group(owner, "Ephemeral")).group_id
                await owner.disconnect()
            finally:
                await server.aclose()
            restarted = RelayServer(host="127.0.0.1", port=port)
            await restarted.start()
            try:
                client = await client_for(restarted, "owner")
                with pytest.raises(GroupDefunctError):
                    await home.groups.sync_group(client, group_id)
                assert home.groups.require(group_id).state is LocalGroupState.DEFUNCT
                await client.disconnect()
            finally:
                await restarted.aclose()

        run(scenario())

    def test_corrupt_group_store_surfaces_typed_error(self, tmp_path: Path) -> None:
        home = GroupHome(tmp_path / "home")
        home.groups.attach  # noqa: B018 - touch attribute for sanity
        target = tmp_path / "home" / "groups.json"
        target.write_text("{ corrupt", encoding="utf-8")
        with pytest.raises(StorageCorruptionError):
            home.groups.list_all()
        target.write_text(json.dumps({"gl-group-AAAA-BBBB-CCCC": {"name": "x"}}))
        with pytest.raises(ConfigValidationError):
            home.groups.require("gl-group-AAAA-BBBB-CCCC")


class TestHygiene:
    def test_no_secrets_in_logs_or_store(self, tmp_path: Path, caplog) -> None:
        async def scenario() -> list[str]:
            async with running_relay() as server:
                owner_home = GroupHome(tmp_path / "owner")
                guest_home = GroupHome(tmp_path / "guest")
                owner = await client_for(server, "owner")
                guest = await client_for(server, "guest")
                group_id = (await owner_home.groups.create_group(owner, "Hygiene")).group_id
                _invite, link = await owner_home.groups.mint_group_invite(
                    owner,
                    owner_home.invites,
                    group_id,
                    ttl_seconds=60.0,
                    max_redemptions=1,
                )
                await asyncio.wait_for(guest_home.groups.join_group(guest, link), timeout=20)
                await asyncio.wait_for(guest_home.groups.leave_group(guest, group_id), timeout=20)
                await owner.disconnect()
                await guest.disconnect()
                return [link]

        with caplog.at_level(logging.DEBUG):
            links = run(scenario())
        token = links[0].removeprefix("gl://join/")
        haystack = "\n".join(record.getMessage() for record in caplog.records)
        assert token not in haystack, "invite token leaked into logs"
        # No private keys or raw signature material in any log line either.
        owner_identity = (tmp_path / "owner" / "identity.json").read_text(encoding="utf-8")
        private_hex = json.loads(owner_identity)["local"]["private_key"]
        assert private_hex not in haystack
        for record in caplog.records:
            message = record.getMessage()
            assert "private_key" not in message.lower()

    def test_group_store_is_metadata_only(self, tmp_path: Path) -> None:
        async def scenario() -> GroupHome:
            async with running_relay() as server:
                owner_home = GroupHome(tmp_path / "owner")
                guest_home = GroupHome(tmp_path / "guest")
                owner = await client_for(server, "owner")
                guest = await client_for(server, "guest")
                group_id = (await owner_home.groups.create_group(owner, "Meta")).group_id
                _invite, link = await owner_home.groups.mint_group_invite(
                    owner,
                    owner_home.invites,
                    group_id,
                    ttl_seconds=60.0,
                    max_redemptions=1,
                )
                await asyncio.wait_for(guest_home.groups.join_group(guest, link), timeout=20)
                await owner.disconnect()
                await guest.disconnect()
                return guest_home

        run(scenario())
        store = (tmp_path / "guest" / "groups.json").read_text(encoding="utf-8")
        for marker in ("token", "private_key", "secret", "session_key"):
            assert marker not in store, f"{marker} leaked into groups.json"

    def test_exceptions_carry_no_secrets(self, tmp_path: Path) -> None:
        async def scenario() -> tuple[str, str]:
            async with running_relay() as server:
                owner_home = GroupHome(tmp_path / "owner")
                owner = await client_for(server, "owner")
                group_id = (await owner_home.groups.create_group(owner, "Ops")).group_id
                _invite, link = await owner_home.groups.mint_group_invite(
                    owner,
                    owner_home.invites,
                    group_id,
                    ttl_seconds=60.0,
                    max_redemptions=1,
                )
                try:
                    await owner_home.groups.leave_group(owner, group_id)
                except GroupPermissionError as exc:
                    await owner.disconnect()
                    return link, f"{exc.message} {exc.hint}"
                raise AssertionError("leave should have failed")

        link, text = run(scenario())
        token = link.removeprefix("gl://join/")
        assert token not in text
