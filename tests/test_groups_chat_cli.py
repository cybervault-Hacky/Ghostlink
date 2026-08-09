"""Phase 6C terminal surface: the group chat TUI and ``ghostlink group chat``.

UI tests run the real :class:`GroupMessagingService` over a real loopback
relay and drive :class:`GroupChatApp` with scripted input — no mocks on the
messaging path; the CLI test launches the real command against a threaded
relay with captured stdin.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path

import pytest

from ghostlink.cli.entrypoint import main
from ghostlink.exceptions.base import ExitCode
from ghostlink.messaging.history import NullHistory, SessionHistory
from ghostlink.transport.relay.protocol import PacketType
from ghostlink.ui.chat import ScriptedInputSource
from ghostlink.ui.console import ConsoleManager
from ghostlink.ui.group_chat import GroupChatApp
from ghostlink.ui.themes import ThemeEngine
from tests.conftest import run, running_relay
from tests.test_groups_messaging_e2e import (
    Member,
    build_world,
    wait_until,
)


def _app(
    console_manager: ConsoleManager,
    member: Member,
    group_id: str,
    lines: list[str | None],
    **kwargs: object,
) -> GroupChatApp:
    return GroupChatApp(
        console_manager,
        member.service,  # type: ignore[arg-type]
        group_id=group_id,
        history=NullHistory(),
        input_source=ScriptedInputSource(lines),
        **kwargs,  # type: ignore[arg-type]
    )


async def _drive(
    app: GroupChatApp,
    source: ScriptedInputSource,
    steps: list[object],
) -> int:
    """Run the app; each step is a coroutine to await (or None to end)."""

    task = asyncio.create_task(app.run())
    await asyncio.sleep(0.05)  # let run() register its listener
    for step in steps:
        await step  # type: ignore[misc]
    source.push(None)
    return await task


class TestBannerAndCommands:
    def test_banner_renders_full_status(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 3)
                await owner.send(gid, "link warmup")
                assert await wait_until(lambda: len(members[0].recorder.texts()) == 1)
                app = _app(console_manager, owner, gid, [None])
                code = await app.run()
                assert code == int(ExitCode.OK)
                await owner.close()
                for member in members:
                    await member.close()

        run(scenario())
        output = console_manager.export_text()
        record_name = "Ops"
        assert record_name in output
        assert "E2E pairwise mesh" in output  # encryption status
        assert "Epoch:" in output
        assert "3/8" in output  # member count
        assert "Mesh links:" in output
        assert "owner" in output  # role
        assert "Latency:" in output
        assert re.search(r"gl-group-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", output)
        # parting line wipes keys — and never prints them
        assert "pairwise keys wiped" in output

    def test_commands_render(self, console_manager: ConsoleManager, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                await owner.send(gid, "one")
                assert await wait_until(lambda: members[0].recorder.texts() == ["one"])
                app = _app(
                    console_manager,
                    owner,
                    gid,
                    ["/members", "/info", "/delivery", "/fingerprint", "/help", "/unknown", None],
                )
                code = await app.run()
                assert code == int(ExitCode.OK)
                await owner.close()
                await members[0].close()

        run(scenario())
        output = console_manager.export_text()
        assert "/help" in output and "/members" in output  # help lists commands
        assert "Roster" in output or "owner" in output  # /members table
        assert "Epoch" in output  # /info
        assert "delivered 1/1" in output  # /delivery
        assert "GLFP-" in output  # /fingerprint
        assert "Unknown command" in output  # /unknown hint

    def test_members_command_lists_all(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 3)
                app = _app(console_manager, owner, gid, ["/members", None])
                await app.run()
                await owner.close()
                for member in members:
                    await member.close()

        run(scenario())
        output = console_manager.export_text()
        expected = 2  # both peers listed by fingerprint
        matches = re.findall(r"GLFP-[0-9A-F]{4}", output)
        assert len(set(matches)) >= expected

    def test_security_command_shows_suite_no_secrets(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        """Phase 7 /security renders suite state without ever leaking keys."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                app = _app(console_manager, owner, gid, ["/security", None])
                await app.run()
                await owner.close()
                await members[0].close()

        run(scenario())
        output = console_manager.export_text()
        assert "Crypto suite" in output
        assert "mesh-v1" in output  # default suite on a mesh group
        assert "Security status" in output
        # no secret material ever renders
        assert "chain_root" not in output and "message_key" not in output

    def test_narrow_terminal_renders(self, tmp_path: Path) -> None:
        console = ConsoleManager(ThemeEngine().get("phantom"), record=True, width=44)

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                app = _app(console, owner, gid, ["/members", "hello", None], message_wrapping=True)
                await app.run()
                assert await wait_until(
                    lambda: members[0].recorder.texts() == ["hello"], timeout=2.0
                )
                await owner.close()
                await members[0].close()

        run(scenario())
        for line in console.export_text().splitlines():
            assert len(line) <= 50  # panel width + tolerance


class TestMessagingFlow:
    def test_outgoing_and_incoming_with_delivery_line(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        async def scenario() -> int:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 3)
                member = members[0]
                source = ScriptedInputSource([])
                app = GroupChatApp(
                    console_manager,
                    owner.service,  # type: ignore[arg-type]
                    group_id=gid,
                    history=NullHistory(),
                    input_source=source,
                )
                assert owner.service is not None

                async def send_out() -> None:
                    source.push("hello group")
                    ok = await wait_until(lambda: "hello group" in member.recorder.texts())
                    assert ok
                    ok = await wait_until(
                        lambda: any(
                            ledger.total() == 2 and ledger.delivered_count() == 2
                            for ledger in owner.service.ledgers_for(gid)  # type: ignore[union-attr]
                        )
                    )
                    assert ok

                async def receive_back() -> None:
                    await member.send(gid, "re: hello")
                    ok = await wait_until(
                        lambda: any(
                            event.frame is not None and event.frame.text == "re: hello"
                            for event in owner.recorder.messages()
                        )
                    )
                    assert ok
                    await asyncio.sleep(0.05)  # let the UI render the event

                code = await _drive(app, source, [send_out(), receive_back()])
                await owner.close()
                for member_ in members:
                    await member_.close()
                return code

        assert run(scenario()) == int(ExitCode.OK)
        output = console_manager.export_text()
        assert "hello group" in output
        assert "re: hello" in output
        assert re.search(r"✓✓ (delivered|read by) 2/2", output)  # full-fanout line, deduped

    def test_offline_member_delivery_shows_pending(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 3)
                online, offline = members
                await offline.close()  # never heard from again
                source = ScriptedInputSource([])
                app = GroupChatApp(
                    console_manager,
                    owner.service,  # type: ignore[arg-type]
                    group_id=gid,
                    history=NullHistory(),
                    input_source=source,
                )

                async def send_one() -> None:
                    source.push("partial delivery test")
                    ok = await wait_until(
                        lambda: "partial delivery test" in online.recorder.texts()
                    )
                    assert ok
                    assert owner.service is not None
                    ok = await wait_until(
                        lambda: (
                            bool(owner.recorder.notices("offline"))
                            or any(
                                ledger.failed_count() > 0
                                for ledger in owner.service.ledgers_for(gid)
                            )
                        )
                    )
                    assert ok

                code = await _drive(app, source, [send_one()])
                assert code == int(ExitCode.OK)
                await owner.close()
                await online.close()

        run(scenario())
        output = console_manager.export_text()
        assert "partial delivery test" in output
        assert "delivered 2/2" not in output  # never overclaims full delivery

    def test_membership_notice_renders_on_leave(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 3)
                leaver = members[1]
                source = ScriptedInputSource([])
                app = GroupChatApp(
                    console_manager,
                    owner.service,  # type: ignore[arg-type]
                    group_id=gid,
                    history=NullHistory(),
                    input_source=source,
                )
                assert leaver.client is not None

                async def leave() -> None:
                    await leaver.home.groups.leave_group(leaver.client, gid)
                    ok = await wait_until(lambda: bool(owner.recorder.notices("left")))
                    assert ok
                    await asyncio.sleep(0.05)  # let the UI render the notice

                code = await _drive(app, source, [leave()])
                assert code == int(ExitCode.OK)
                await owner.close()
                await members[0].close()
                await leaver.close()

        run(scenario())
        output = console_manager.export_text()
        assert "a member left" in output  # roster event rendered by the notice line

    def test_gap_notice_renders(self, console_manager: ConsoleManager, tmp_path: Path) -> None:
        """Injected frames with a bounded gseq gap render the warning line."""

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                await owner.send(gid, "seq one")
                assert await wait_until(lambda: peer.recorder.texts() == ["seq one"])
                source = ScriptedInputSource([])
                app = GroupChatApp(
                    console_manager,
                    peer.service,  # type: ignore[arg-type]
                    group_id=gid,
                    history=NullHistory(),
                    input_source=source,
                )
                assert owner.service is not None and peer.service is not None

                async def inject_gap() -> None:
                    # craft + inject a real gseq-4 frame over the live link:
                    # gseq 2/3 never arrive → rendered with a gap notice (§22)
                    from ghostlink.groups.frames import KIND_MSG, gmsg_frame
                    from ghostlink.groups.mesh import group_message_aad
                    from tests.test_groups_messaging_e2e import forward_payload

                    record = peer.home.groups.require(gid)
                    link = owner.service.mesh.link_for(gid, peer.fp, record.epoch)  # type: ignore[union-attr]
                    assert link is not None
                    frame = gmsg_frame(
                        gid,
                        record.epoch,
                        owner.fp,
                        peer.fp,
                        message_id="gmsg_00aa11bb22cc33dd",
                        gseq=4,
                        display_name="owner",
                        text="jumped ahead",
                        ts=1.0,
                    )
                    await peer.service._handle_forward(
                        forward_payload(
                            group=gid,
                            epoch=record.epoch,
                            sender=owner.fp,
                            to=peer.fp,
                            kind=KIND_MSG,
                            body=link.seal(
                                frame,
                                group_message_aad(gid, record.epoch, owner.fp, peer.fp),
                            ),
                        )
                    )
                    ok = await wait_until(
                        lambda: any(event.gap for event in peer.recorder.messages()), timeout=2.0
                    )
                    assert ok
                    await asyncio.sleep(0.05)  # let the UI render the gap line

                code = await _drive(app, source, [inject_gap()])
                assert code == int(ExitCode.OK)
                await owner.close()
                await peer.close()

        run(scenario())
        output = console_manager.export_text()
        assert "jumped ahead" in output
        assert "sequence gap" in output

    def test_history_records_and_slash_history(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        history = SessionHistory()

        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                assert peer.service is not None
                peer.service._history = history
                await owner.send(gid, "remember me")
                assert await wait_until(lambda: peer.recorder.texts() == ["remember me"])
                source = ScriptedInputSource(["/history", None])
                app = GroupChatApp(
                    console_manager,
                    peer.service,
                    group_id=gid,
                    history=history,
                    input_source=source,
                )
                await app.run()
                await owner.close()
                await peer.close()

        run(scenario())
        output = console_manager.export_text()
        assert "remember me" in output  # rendered by /history from the store


class TestLeaveAndInvite:
    def test_leave_removes_member_and_closes_app(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                app = GroupChatApp(
                    console_manager,
                    peer.service,  # type: ignore[arg-type]
                    group_id=gid,
                    history=NullHistory(),
                    input_source=ScriptedInputSource(["/leave"]),
                )
                code = await app.run()
                assert code == int(ExitCode.OK)
                # the leave committed: the local record leaped an epoch and
                # is now archived (lifecycle mechanics asserted in 6B tests)
                assert peer.home.groups.require(gid).epoch == 3
                await owner.close()
                await peer.close()

        run(scenario())
        output = console_manager.export_text()
        assert "left the group" in output or "Group session closed" in output

    def test_invite_rejected_for_non_owner(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        async def scenario() -> None:
            async with running_relay() as server:
                owner, members, gid = await build_world(tmp_path, server.url, 2)
                peer = members[0]
                app = GroupChatApp(
                    console_manager,
                    peer.service,  # type: ignore[arg-type]
                    group_id=gid,
                    history=NullHistory(),
                    input_source=ScriptedInputSource(["/invite", None]),
                    invite_manager=peer.home.invites,
                )
                await app.run()
                await owner.close()
                await peer.close()

        run(scenario())
        output = console_manager.export_text().lower()
        assert "owner" in output  # permission failure is explicit


class TestCliSmoke:
    def test_group_chat_command_over_threaded_relay(
        self,
        isolated_home: Path,
        threaded_relay: object,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``ghostlink group chat`` attaches, syncs, prints the banner, and
        exits cleanly on captured (EOF) stdin — a real relay round-trip."""

        data = ["--data-dir", str(isolated_home / "gl-data")]
        with threaded_relay() as relay:  # type: ignore[attr-defined]
            args = [*data, "group", "create", "--name", "Ops", "--relay", relay.url]
            assert main(args) == int(ExitCode.OK)
            created = capsys.readouterr().out
            match = re.search(r"gl-group-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", created)
            assert match
            gid = match.group(0)
            code = main([*data, "group", "chat", gid, "--relay", relay.url])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK), output
        assert "GhostLink Secure Group" in output
        assert "E2E pairwise mesh" in output
        assert "Group session closed" in output

    def test_group_chat_unknown_group_errors(
        self,
        isolated_home: Path,
        threaded_relay: object,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        data = ["--data-dir", str(isolated_home / "gl-data")]
        with threaded_relay() as relay:  # type: ignore[attr-defined]
            code = main(
                [
                    *data,
                    "group",
                    "chat",
                    "gl-group-AAAA-BBBB-CCCC",
                    "--relay",
                    relay.url,
                ]
            )
        output = capsys.readouterr().out
        assert code != int(ExitCode.OK)
        assert "unknown" in output.lower() or "not" in output


class TestRelayNeverSeesPlaintext:
    def test_relay_observes_no_message_content(self, tmp_path: Path) -> None:
        """§28: the relay's own logs hold only opaque routing metadata."""

        import logging

        records: list[str] = []

        class _Trap(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record.getMessage())

        trap = _Trap()
        logger = logging.getLogger("ghostlink.transport.relay.server")
        previous = logger.level
        logger.addHandler(trap)
        logger.setLevel(logging.DEBUG)
        try:

            async def scenario() -> None:
                async with running_relay() as server:
                    owner, members, gid = await build_world(tmp_path, server.url, 3)
                    await owner.send(gid, "TOP-SECRET-PLAINTEXT")
                    ok = await wait_until(
                        lambda: "TOP-SECRET-PLAINTEXT" in members[0].recorder.texts()
                    )
                    assert ok
                    await owner.close()
                    for member in members:
                        await member.close()

            run(scenario())
        finally:
            logger.removeHandler(trap)
            logger.setLevel(previous)
        text = "\n".join(records)
        assert "TOP-SECRET-PLAINTEXT" not in text
        assert PacketType.GROUP_FORWARD.value in text  # routing metadata only
