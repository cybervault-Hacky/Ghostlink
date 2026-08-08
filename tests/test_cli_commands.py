"""CLI subcommands end to end: host, join, relay-status, session, doctor."""

from __future__ import annotations

from pathlib import Path

import pytest

from ghostlink.cli.arguments import parse_args
from ghostlink.cli.entrypoint import main
from ghostlink.exceptions.base import ExitCode


class TestHostCommand:
    def test_creates_room_and_shows_dashboard(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["host", "--name", "Midnight Lounge"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "Room Hosted" in output
        assert "gl-room-" in output
        assert "Midnight Lounge" in output
        assert "gli_" in output
        assert "No relay configured" in output

    def test_anonymous_host_with_lifetime(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["host", "--lifetime", "0", "--multi-use"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "Unnamed room" in output
        assert "never" in output  # no expiry

    def test_host_with_relay_enters_the_chat(
        self,
        isolated_home: Path,
        threaded_relay: object,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("builtins.input", _eof_input)  # stdin-less run exits cleanly
        factory = threaded_relay
        with factory() as relay:  # type: ignore[attr-defined]
            code = main(["host", "--relay", relay.url])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "Relay" in output
        assert "GhostLink Secure Session" in output  # chat banner
        assert "Session closed" in output
        assert "No relay configured" not in output

    def test_host_fails_cleanly_when_the_relay_is_down(
        self,
        isolated_home: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        code = main(["host", "--relay", "ws://127.0.0.1:1/relay", "--lifetime", "5"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.NETWORK)
        assert "Hint" in output  # unreachable relays produce actionable guidance


def _eof_input(prompt: str = "") -> str:
    raise EOFError("scripted stdin finished")


class TestJoinCommand:
    def test_valid_room_id_without_relay_guides_to_relay(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["join", "gl-room-ABCD-EFGH-JKMN"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "Join Room" in output
        assert "Valid" in output
        assert "Relay required" in output  # Phase 3: chat needs a relay

    def test_mixed_case_input_is_canonicalized(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["join", "GL-ROOM-abcd-efgh-JKMN"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "gl-room-ABCD-EFGH-JKMN" in output

    def test_invalid_room_id_rejected_with_format_guidance(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["join", "not-a-room"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.CONFIGURATION)
        assert "Invalid" in output
        assert "gl-room-XXXX-XXXX-XXXX" in output

    def test_missing_room_id_is_an_argument_error(self, isolated_home: Path) -> None:
        with pytest.raises(SystemExit) as captured:
            main(["join"])
        assert captured.value.code == 2

    def test_join_with_relay_enters_the_chat(
        self,
        isolated_home: Path,
        threaded_relay: object,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr("builtins.input", _eof_input)
        factory = threaded_relay
        with factory() as relay:  # type: ignore[attr-defined]
            code = main(["join", "gl-room-ABCD-EFGH-JKMN", "--relay", relay.url])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "GhostLink Secure Session" in output
        assert "guest" in output.lower() or "Peer" in output


class TestChatEndToEnd:
    """Two scripted chat runners over a live relay — the full Phase 3 flow."""

    def test_two_users_chat_end_to_end(
        self,
        isolated_home: Path,
        threaded_relay: object,
        tmp_path: Path,
    ) -> None:
        import asyncio

        from ghostlink.models.room import generate_room_id
        from ghostlink.models.settings import AppSettings
        from ghostlink.transport.relay.client import RelayClientConfig
        from ghostlink.ui.chat import ScriptedInputSource, run_chat_session
        from ghostlink.ui.console import ConsoleManager
        from ghostlink.ui.themes import ThemeEngine
        from tests.conftest import run

        factory = threaded_relay
        with factory() as relay:  # type: ignore[attr-defined]
            channel = generate_room_id()
            settings = AppSettings.defaults()
            theme = ThemeEngine().get("phantom")
            host_console = ConsoleManager(theme, record=True, width=100)
            guest_console = ConsoleManager(theme, record=True, width=100)
            config = RelayClientConfig(
                connect_timeout_seconds=3.0,
                handshake_timeout_seconds=3.0,
                heartbeat_interval_seconds=3600.0,
            )

            async def both() -> tuple[int, int]:
                host_task = asyncio.create_task(
                    run_chat_session(
                        host_console,
                        settings=settings,
                        state_dir=tmp_path / "host-state",
                        role="host",
                        channel=channel,
                        relay_url=relay.url,
                        client_config=config,
                        display_name="Nova",
                        input_source=ScriptedInputSource(
                            ["/info", "hello from the host", None],
                            line_delay_seconds=0.5,
                        ),
                    )
                )
                guest_task = asyncio.create_task(
                    run_chat_session(
                        guest_console,
                        settings=settings,
                        state_dir=tmp_path / "guest-state",
                        role="guest",
                        channel=channel,
                        relay_url=relay.url,
                        client_config=config,
                        display_name="Ravi",
                        input_source=ScriptedInputSource(
                            ["hi from the guest", None],
                            line_delay_seconds=0.9,
                        ),
                    )
                )
                return await asyncio.gather(host_task, guest_task)

            host_code, guest_code = run(both())

        assert host_code == int(ExitCode.OK)
        assert guest_code == int(ExitCode.OK)
        host_view = host_console.export_text()
        guest_view = guest_console.export_text()
        # Each side rendered its own banner and the peer's messages.
        assert "GhostLink Secure Session" in host_view
        assert "GhostLink Secure Session" in guest_view
        assert "Ravi:" in host_view
        assert "hi from the guest" in host_view
        assert "Nova:" in guest_view
        assert "hello from the host" in guest_view
        # Delivery feedback and session info surfaced for the host.
        assert "✓✓" in host_view
        assert "Session info" in host_view
        assert "Safety code" in host_view
        # Graceful shutdown on both sides.
        assert "Session closed" in host_view
        assert "Session closed" in guest_view


class TestRelayStatusCommand:
    def test_live_probe_renders_the_dashboard(
        self,
        isolated_home: Path,
        threaded_relay: object,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        factory = threaded_relay
        with factory() as relay:  # type: ignore[attr-defined]
            code = main(["relay-status", "--relay", relay.url, "--pings", "2"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "Relay Status" in output
        assert "Connected and measured" in output
        assert "Latency" in output
        assert "Heartbeat" in output
        assert "Connection timeline" in output
        assert "Graceful" in output

    def test_unconfigured_relay_shows_guidance(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["relay-status"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "Relay not configured" in output
        assert "ghostlink.transport.relay.server" in output  # wraps as a single token

    def test_unreachable_relay_maps_to_network_exit_code(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["relay-status", "--relay", "ws://127.0.0.1:1/relay", "--timeout", "0.5"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.NETWORK)
        assert "Hint" in output

    def test_out_of_range_pings_rejected(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["relay-status", "--relay", "ws://127.0.0.1:8787", "--pings", "42"])
        assert code == int(ExitCode.NETWORK)


class TestSessionCommand:
    def test_empty_state_overview(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["session"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "Session Overview" in output
        assert "No session history recorded yet." in output
        assert "Hosted rooms (0)" in output

    def test_hosted_room_appears_in_the_overview(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert main(["host", "--name", "Vault"]) == int(ExitCode.OK)
        capsys.readouterr()  # drop the hosting output
        code = main(["session"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "Hosted rooms (1)" in output
        assert "Issued invites (1)" in output
        assert "Vault" in output


class TestDoctorCommand:
    def test_doctor_subcommand_matches_flag(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["doctor"])
        output = capsys.readouterr().out
        assert "GhostLink Doctor" in output
        assert code in (int(ExitCode.OK), int(ExitCode.ENVIRONMENT))


class TestSubcommandParsing:
    def test_host_options(self) -> None:
        options = parse_args(
            [
                "host",
                "--name",
                "Lounge",
                "--lifetime",
                "30",
                "--invite-lifetime",
                "10",
                "--multi-use",
                "--relay",
                "ws://127.0.0.1:8787",
            ]
        )
        assert options.command == "host"
        assert options.name == "Lounge"
        assert options.lifetime == 30
        assert options.invite_lifetime == 10
        assert options.multi_use is True
        assert options.relay_url == "ws://127.0.0.1:8787"

    def test_join_position_arg(self) -> None:
        options = parse_args(["join", "gl-room-ABCD-EFGH-JKMN"])
        assert options.command == "join"
        assert options.room_id == "gl-room-ABCD-EFGH-JKMN"

    def test_relay_status_options(self) -> None:
        options = parse_args(
            ["relay-status", "--relay", "wss://r.example.org", "--pings", "7", "--timeout", "2.5"]
        )
        assert options.command == "relay-status"
        assert options.pings == 7
        assert options.timeout == 2.5

    def test_session_and_doctor_commands(self) -> None:
        assert parse_args(["session"]).command == "session"
        assert parse_args(["doctor"]).command == "doctor"

    @pytest.mark.parametrize(
        "argv",
        [
            ["host", "--lifetime", "-1"],
            ["host", "--invite-lifetime", "0"],
            ["relay-status", "--timeout", "0"],
        ],
    )
    def test_invalid_numeric_options_rejected(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit) as captured:
            parse_args(argv)
        assert captured.value.code == 2
