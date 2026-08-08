"""Phase 6B CLI: ``ghostlink group`` lifecycle commands.

Relay-touching flows run against a live threaded relay — nothing mocks
the authority; the CLI layer only parses, renders, and maps exit codes.
"""

from __future__ import annotations

import asyncio
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ghostlink.cli.arguments import parse_args
from ghostlink.cli.entrypoint import main
from ghostlink.constants.files import STATE_DIR_NAME
from ghostlink.exceptions.base import ExitCode
from ghostlink.groups.models import (
    GroupMember,
    GroupRole,
    LocalGroupRecord,
    LocalGroupState,
)
from ghostlink.groups.registry import LocalGroupRegistry
from ghostlink.identity.identity import LocalIdentity
from ghostlink.storage.manager import StorageManager
from tests.group_helpers import GroupHome, fingerprint_of

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _data_args(home: Path) -> list[str]:
    return ["--data-dir", str(home / "gl-data")]


def _state_dir(home: Path) -> Path:
    return home / "gl-data" / STATE_DIR_NAME


def _seed_record(home: Path, *, name: str = "Night Watch") -> LocalGroupRecord:
    identity = LocalIdentity.generate()
    fp = fingerprint_of(identity)
    owner = GroupMember(
        fingerprint=fp,
        handle=identity.identity_id,
        display_name="Boss",
        public_key_hex=identity.public_key_hex,
        role=GroupRole.OWNER,
        joined_epoch=1,
    )
    record = LocalGroupRecord(
        group_id="gl-group-AAAA-BBBB-CCCC",
        name=name,
        owner_fingerprint=fp,
        owner_public_key_hex=identity.public_key_hex,
        my_fingerprint=fp,
        epoch=1,
        members={fp: owner},
        state=LocalGroupState.ACTIVE,
        relay_url="ws://127.0.0.1:8787/relay",
        created_at=T0,
        updated_at=T0,
    )
    LocalGroupRegistry(StorageManager(_state_dir(home))).save(record)
    return record


class TestArgumentParsing:
    def test_group_defaults_to_list(self) -> None:
        options = parse_args(["group"])
        assert options.command == "group"
        assert options.group_action == "list"

    def test_group_actions_and_targets(self) -> None:
        options = parse_args(["group", "create", "--name", "Ops"])
        assert options.group_action == "create"
        assert options.group_name == "Ops"
        options = parse_args(["group", "info", "gl-group-AAAA-BBBB-CCCC"])
        assert options.group_action == "info"
        assert options.group_target == "gl-group-AAAA-BBBB-CCCC"
        options = parse_args(["group", "remove", "gl-group-AAAA-BBBB-CCCC", "GLFP-AAAA-BBBB-CCCC"])
        assert options.group_action == "remove"
        assert options.group_subject == "GLFP-AAAA-BBBB-CCCC"

    def test_group_invite_options(self) -> None:
        options = parse_args(
            ["group", "invite", "gl-group-AAAA-BBBB-CCCC", "--expires", "5m", "--uses", "3"]
        )
        assert options.group_action == "invite"
        assert options.expires == "5m"
        assert options.uses == 3


class TestListAndInfo:
    def test_list_empty_is_guidance_not_error(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main([*_data_args(isolated_home), "group", "list"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "no groups yet" in output.lower()

    def test_list_and_info_render_seeded_records(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        record = _seed_record(isolated_home)
        code = main([*_data_args(isolated_home), "group", "list"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert record.group_id in output
        assert "Night Watch" in output
        assert "1/8" in output

        code = main([*_data_args(isolated_home), "group", "info", record.group_id])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert record.owner_fingerprint in output
        assert "Roster" in output
        assert "owner" in output

    def test_info_on_unknown_group_exits_group_code(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main([*_data_args(isolated_home), "group", "info", "gl-group-ZZZZ-YYYY-XXXX"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.GROUP)
        assert "no local record" in output.lower()

    def test_info_accepts_messy_user_input(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        record = _seed_record(isolated_home)
        messy = record.group_id.upper().replace("GL-GROUP", "gl-group").replace("-AAAA", "-aaaa")
        code = main([*_data_args(isolated_home), "group", "info", messy])
        assert code == int(ExitCode.OK), capsys.readouterr().out

    def test_narrow_terminal_does_not_crash(
        self,
        isolated_home: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _seed_record(isolated_home)
        monkeypatch.setenv("COLUMNS", "34")
        monkeypatch.setenv("LINES", "20")
        code = main([*_data_args(isolated_home), "group", "list"])
        assert code == int(ExitCode.OK)
        code = main([*_data_args(isolated_home), "group", "info", "gl-group-AAAA-BBBB-CCCC"])
        assert code == int(ExitCode.OK)
        output = capsys.readouterr().out
        assert "Traceback" not in output


class TestCreateCommand:
    def test_create_without_name_fails_fast(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main([*_data_args(isolated_home), "group", "create"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.GROUP)
        assert "needs a name" in output.lower()

    def test_create_without_relay_fails(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main([*_data_args(isolated_home), "group", "create", "--name", "Ops"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.GROUP)
        assert "relay is required" in output.lower()

    def test_create_over_relay(
        self,
        isolated_home: Path,
        threaded_relay: object,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with threaded_relay() as relay:  # type: ignore[attr-defined]
            code = main(
                [
                    *_data_args(isolated_home),
                    "group",
                    "create",
                    "--name",
                    "Ops",
                    "--relay",
                    relay.url,
                ]
            )
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK), output
        assert re.search(r"gl-group-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", output)
        assert "Group created" in output
        # The record persisted under the isolated data dir.
        records = LocalGroupRegistry(StorageManager(_state_dir(isolated_home))).list_all()
        assert len(records) == 1
        assert records[0].epoch == 1
        assert records[0].my_role is GroupRole.OWNER


class TestInviteCommand:
    def test_group_invite_over_relay_and_log_hygiene(
        self,
        isolated_home: Path,
        threaded_relay: object,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with threaded_relay() as relay:  # type: ignore[attr-defined]
            args = [
                *_data_args(isolated_home),
                "group",
                "create",
                "--name",
                "Ops",
                "--relay",
                relay.url,
            ]
            assert main(args) == int(ExitCode.OK)
            created = capsys.readouterr().out
            group_id = re.search(r"gl-group-[A-Z2-9]{4}-[A-Z2-9]{4}-[A-Z2-9]{4}", created)
            assert group_id
            code = main(
                [
                    *_data_args(isolated_home),
                    "group",
                    "invite",
                    group_id.group(0),
                    "--relay",
                    relay.url,
                    "--expires",
                    "5m",
                    "--uses",
                    "2",
                ]
            )
            output = capsys.readouterr().out
            assert code == int(ExitCode.OK), output
            match = re.search(r"gl://join/[A-Z2-9]+", output)
            assert match, "invite link missing from output"
            token = match.group(0).removeprefix("gl://join/")
            # The interactive link is written to the terminal — never to
            # the persistent log file.
            logs_dir = isolated_home / "gl-data" / "logs"
            logged = "\n".join(path.read_text(encoding="utf-8") for path in logs_dir.glob("*.log"))
            assert token not in logged, "invite token leaked into persistent logs"

    def test_invite_with_bad_uses_fails_fast(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _seed_record(isolated_home)
        code = main(
            [
                *_data_args(isolated_home),
                "group",
                "invite",
                "gl-group-AAAA-BBBB-CCCC",
                "--uses",
                "99",
                "--relay",
                "ws://127.0.0.1:1/relay",
            ]
        )
        output = capsys.readouterr().out
        assert code == int(ExitCode.GROUP)
        assert "--uses" in output

    def test_invite_unknown_group(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(
            [
                *_data_args(isolated_home),
                "group",
                "invite",
                "gl-group-ZZZZ-YYYY-XXXX",
                "--relay",
                "ws://127.0.0.1:1/relay",
            ]
        )
        output = capsys.readouterr().out
        assert code == int(ExitCode.GROUP)
        assert "no local record" in output.lower()


class _OwnerThread:
    """An owner device living in a background asyncio loop (signs joins)."""

    def __init__(self, root: Path, relay_url: str) -> None:
        self.home = GroupHome(root)
        self.relay_url = relay_url
        self.group_id = ""
        self.link = ""
        self.epoch = 0
        self.ready = threading.Event()
        self.error: BaseException | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread = threading.Thread(target=self._drive, daemon=True)

    def start(self) -> None:
        self._thread.start()
        assert self.ready.wait(timeout=30), "owner thread did not get ready"

    def _drive(self) -> None:
        async def setup() -> None:
            from ghostlink.transport.relay.client import RelayClient
            from ghostlink.transport.relay.endpoint import RelayEndpoint
            from tests.group_helpers import FAST

            self._loop = asyncio.get_running_loop()
            client = RelayClient(
                RelayEndpoint.from_url(self.relay_url), client_name="owner", config=FAST
            )
            await client.connect()
            self._client = client
            record = await self.home.groups.create_group(client, "Ops")
            self.group_id = record.group_id
            _invite, link = await self.home.groups.mint_group_invite(
                client,
                self.home.invites,
                self.group_id,
                ttl_seconds=300.0,
                max_redemptions=4,
            )
            self.link = link
            self.ready.set()
            while True:  # keep signing while the test runs
                await asyncio.sleep(0.1)
                self.epoch = self.home.groups.require(self.group_id).epoch

        async def guarded() -> None:
            try:
                await setup()
            except BaseException as exc:
                self.error = exc
                self.ready.set()

        try:
            asyncio.run(guarded())
        except BaseException as exc:
            self.error = exc
            self.ready.set()


class TestJoinLeaveOverRelay:
    def test_join_info_leave_and_remove_flow(
        self,
        tmp_path: Path,
        isolated_home: Path,
        threaded_relay: object,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with threaded_relay() as relay:  # type: ignore[attr-defined]
            owner = _OwnerThread(tmp_path / "owner", relay.url)
            owner.start()
            assert owner.error is None, owner.error
            link = owner.link
            group_id = owner.group_id

            # The joiner uses the CLI with its own isolated home.
            code = main([*_data_args(isolated_home), "group", "join", link, "--relay", relay.url])
            output = capsys.readouterr().out
            assert code == int(ExitCode.OK), output
            assert "Joined group" in output

            # Local record reflects the membership.
            records = LocalGroupRegistry(StorageManager(_state_dir(isolated_home))).list_all()
            assert len(records) == 1
            assert records[0].epoch >= 2
            assert records[0].member_count() == 2

            code = main([*_data_args(isolated_home), "group", "info", group_id])
            output = capsys.readouterr().out
            assert code == int(ExitCode.OK)
            assert "2/8" in output

            code = main(
                [*_data_args(isolated_home), "group", "leave", group_id, "--relay", relay.url]
            )
            output = capsys.readouterr().out
            assert code == int(ExitCode.OK), output
            assert "Left" in output
            record = LocalGroupRegistry(StorageManager(_state_dir(isolated_home))).require(group_id)
            assert record.state is LocalGroupState.LEFT

            # A second leave is refused locally.
            code = main(
                [*_data_args(isolated_home), "group", "leave", group_id, "--relay", relay.url]
            )
            output = capsys.readouterr().out
            assert code == int(ExitCode.GROUP)
            assert "no longer usable" in output.lower()

    def test_join_bad_link_fails_locally(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(
            [
                *_data_args(isolated_home),
                "group",
                "join",
                "gl://join/SHORTY",
                "--relay",
                "ws://127.0.0.1:1/relay",
            ]
        )
        output = capsys.readouterr().out
        assert code != int(ExitCode.OK)
        assert "invite" in output.lower()

    def test_join_without_target_fails(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main([*_data_args(isolated_home), "group", "join"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.GROUP)
        assert "invite link" in output.lower()
