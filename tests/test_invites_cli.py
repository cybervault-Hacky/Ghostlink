"""Phase 5 CLI: identity + invite commands, join-via-invite exit semantics.

Network-touching flows run against a live threaded relay — nothing is
mocked; the relay authority decides every verdict.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path

import pytest

from ghostlink.cli.arguments import parse_args
from ghostlink.cli.entrypoint import main
from ghostlink.constants.files import STATE_DIR_NAME
from ghostlink.exceptions.base import ExitCode
from ghostlink.invites.lifecycle import SecureInviteManager
from ghostlink.invites.models import InviteState
from ghostlink.invites.registry import LocalInviteRegistry
from ghostlink.invites.tokens import format_invite_link, generate_invite_token
from ghostlink.storage.manager import StorageManager
from ghostlink.transport.relay.client import RelayClientConfig
from ghostlink.transport.relay.endpoint import RelayEndpoint


def _eof_input(prompt: str = "") -> str:
    raise EOFError("scripted stdin finished")


FAST = RelayClientConfig(
    connect_timeout_seconds=1.5,
    handshake_timeout_seconds=1.5,
    heartbeat_interval_seconds=3600.0,
    heartbeat_timeout_seconds=1.0,
    reconnect_attempts=0,
    reconnect_base_delay_seconds=0.05,
)


def _seed_record(
    home: Path,
    *,
    state: InviteState = InviteState.ACTIVE,
    expires_seconds: float = 900.0,
) -> tuple[SecureInviteManager, str, str]:
    """Create one minted invite record inside the isolated data dir."""

    root = home / "gl-data"
    registry = LocalInviteRegistry(StorageManager(root / STATE_DIR_NAME))
    manager = SecureInviteManager(registry)
    record, link = manager.mint(ttl_seconds=expires_seconds)
    if state is InviteState.ACTIVE:
        record.transition(InviteState.ACTIVE)
    manager._registry.save(record)
    return manager, record.invite_id, link


def _data_args(home: Path) -> list[str]:
    return ["--data-dir", str(home / "gl-data")]


class TestArgumentParsing:
    def test_identity_actions(self) -> None:
        assert parse_args(["identity"]).identity_action is None
        assert parse_args(["identity", "show"]).identity_action == "show"
        assert parse_args(["identity", "fingerprint"]).identity_action == "fingerprint"
        nick = parse_args(["identity", "nickname", "ShadowUser"])
        assert nick.identity_action == "nickname"
        assert nick.nickname == "ShadowUser"

    def test_invite_create_options(self) -> None:
        options = parse_args(["invite", "create", "--expires", "2s", "--uses", "3", "--no-chat"])
        assert options.invite_action == "create"
        assert options.expires == "2s"
        assert options.uses == 3
        assert options.no_chat is True

    def test_invite_management_targets(self) -> None:
        options = parse_args(["invite", "revoke", "gi_0123456789"])
        assert options.invite_action == "revoke"
        assert options.invite_target == "gi_0123456789"
        options = parse_args(["invite", "list"])
        assert options.invite_action == "list"

    def test_join_accepts_invite_link(self) -> None:
        link = format_invite_link(generate_invite_token())
        options = parse_args(["join", link])
        assert options.command == "join"
        assert options.room_id == link


class TestIdentityCommand:
    def test_identity_show_renders_handle_and_fingerprint(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["identity"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "LOCAL GHOSTLINK IDENTITY" in output
        assert "GL-" in output
        assert "GLFP-" in output
        assert "Nickname" in output
        # Public material only: private hex must never appear.
        assert "private" not in output.lower()

    def test_fingerprint_action_shows_only_verification_bits(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["identity", "fingerprint"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "GLFP-" in output
        assert "Created:" not in output

    def test_nickname_round_trip_persists(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["identity", "nickname", "ShadowUser"]) == int(ExitCode.OK)
        capsys.readouterr()
        assert main(["identity", "nickname"]) == int(ExitCode.OK)
        assert "ShadowUser" in capsys.readouterr().out

    def test_nickname_validation_fails_cleanly(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["identity", "nickname", "x" * 40])
        assert code == int(ExitCode.INVITE)
        assert "✗" in capsys.readouterr().out


class TestInviteListInfoRevoke:
    def test_list_empty_state(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["invite", "list"])
        assert code == int(ExitCode.OK)
        assert "No invites yet" in capsys.readouterr().out

    def test_list_and_info_of_a_seeded_invite(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, invite_id, link = _seed_record(isolated_home)
        code = main([*_data_args(isolated_home), "invite", "list"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert invite_id in output
        assert "ACTIVE" in output
        # The list never re-prints the secret token.
        assert link.split("/")[-1] not in output

        code = main([*_data_args(isolated_home), "invite", "info", invite_id])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "Invite details" in output
        assert "Protocol" in output
        assert link.split("/")[-1] not in output

    def test_revoke_without_relay_marks_it_locally(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _, invite_id, _ = _seed_record(isolated_home)
        code = main([*_data_args(isolated_home), "invite", "revoke", invite_id])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "revoked" in output

        # …and any later redemption would fail: the record is terminal now.
        _seed_record(isolated_home)  # a second, live invite must survive the revoke
        listed = main([*_data_args(isolated_home), "invite", "list"])
        output = capsys.readouterr().out
        assert listed == int(ExitCode.OK)
        assert "REVOKED" in output

    def test_revoke_unknown_target_exits_with_invite_code(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["invite", "revoke", "gi_0000000000"])
        assert code == int(ExitCode.INVITE)
        assert "invite not found" in capsys.readouterr().out.lower()

    def test_retention_purges_old_terminal_records(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        manager, invite_id, _ = _seed_record(isolated_home, expires_seconds=1.0)
        record = manager.require(invite_id)
        old = record.expires_at - timedelta(hours=48)
        # Age the record beyond the configured retention window.
        record.expires_at = old
        record.created_at = old - timedelta(seconds=1)
        manager._registry.save(record)
        code = main([*_data_args(isolated_home), "invite", "list"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert invite_id not in output
        assert "No invites yet" in output


class TestInviteCreate:
    def test_create_without_relay_explains_the_requirement(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["invite", "create"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.NETWORK)
        assert "relay required" in output.lower()

    def test_create_runs_the_panel_and_countdown(
        self,
        isolated_home: Path,
        threaded_relay: object,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        with threaded_relay() as relay:  # type: ignore[attr-defined]
            code = main(["invite", "create", "--no-chat", "--expires", "1s", "--relay", relay.url])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "SECURE GHOSTLINK INVITE" in output
        assert "gl://join/" in output
        assert "1 second" in output
        assert "Waiting for peer" in output
        assert "INVITE EXPIRED" in output

    def test_create_rejects_out_of_range_expiry(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["invite", "create", "--no-chat", "--expires", "99999999s"])
        assert code == int(ExitCode.INVITE)
        assert "--expires" in capsys.readouterr().out

    def test_create_rejects_bad_room_binding(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["invite", "create", "--no-chat", "--room", "gl-room-AAAA"])
        assert code == int(ExitCode.CONFIGURATION)


class TestJoinViaInvite:
    def test_malformed_link_fails_local_first(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["join", "gl://join/SHORTY", "--relay", "ws://127.0.0.1:1/relay"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.INVITE)
        assert "invalid invite link" in output.lower()

    def test_wrong_scheme_fails_locally(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["join", "https://join/ABCDEFGHIJ23456789MN"])
        assert code == int(ExitCode.INVITE)

    def test_expired_invite_renders_the_brief_panel(
        self,
        isolated_home: Path,
        threaded_relay: object,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("builtins.input", _eof_input)
        with threaded_relay() as relay:  # type: ignore[attr-defined]
            from ghostlink.transport.relay.client import RelayClient

            link = format_invite_link(generate_invite_token())
            token = link.split("/")[-1]

            async def register() -> None:
                client = RelayClient(
                    RelayEndpoint.from_url(relay.url), client_name="ctl", config=FAST
                )
                await client.connect()
                from ghostlink.models.room import generate_room_id

                await client.create_invite(
                    token, room_id=generate_room_id(), ttl_seconds=1.0, max_redemptions=1
                )
                await asyncio.sleep(1.4)
                await client.disconnect()

            asyncio.run(register())
            code = main(["join", link, "--relay", relay.url])
        output = capsys.readouterr().out
        assert code == int(ExitCode.INVITE)
        assert "INVITE EXPIRED" in output
        assert "This invitation is no longer" in output

    def test_valid_invite_enters_the_chat(
        self,
        isolated_home: Path,
        threaded_relay: object,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("builtins.input", _eof_input)
        with threaded_relay() as relay:  # type: ignore[attr-defined]
            link = format_invite_link(generate_invite_token())
            token = link.split("/")[-1]

            async def register() -> str:
                from ghostlink.models.room import generate_room_id
                from ghostlink.transport.relay.client import RelayClient as Client

                room = generate_room_id()
                client = Client(RelayEndpoint.from_url(relay.url), client_name="ctl", config=FAST)
                await client.connect()
                await client.create_invite(token, room_id=room, ttl_seconds=60.0, max_redemptions=1)
                await client.disconnect()
                return room

            room = asyncio.run(register())
            code = main(["join", link, "--relay", relay.url])
            # One-time: a second redemption by anyone must fail.
            second = main(["join", link, "--relay", relay.url])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "Invite accepted" in output
        assert "GhostLink Secure Session" in output
        assert room in output
        assert second == int(ExitCode.INVITE)
        assert "INVITE ALREADY USED" in output
