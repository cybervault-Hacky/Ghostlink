"""Network dashboards render their live data (rooms, relay, sessions)."""

from __future__ import annotations

from datetime import UTC, datetime

from ghostlink.models.invite import Invite
from ghostlink.models.room import Room
from ghostlink.models.session import SessionInfo
from ghostlink.transport.relay.client import RelayProbeReport
from ghostlink.ui.console import ConsoleManager
from ghostlink.ui.dashboards import (
    relay_unconfigured_panel,
    render_join_result,
    render_relay_dashboard,
    render_room_dashboard,
    render_session_overview,
)

NOW = datetime(2026, 8, 7, 9, 30, 0, tzinfo=UTC)


def _probe_report() -> RelayProbeReport:
    return RelayProbeReport(
        endpoint="ws://127.0.0.1:8787/relay",
        secure=False,
        session_id="sess_probe123",
        server_name="ghostlink-relay",
        protocol_version=2,
        rtt_samples_ms=(0.9, 1.4, 1.1, 1.0),
        heartbeats_acked=2,
        packets_sent=9,
        packets_received=8,
        bytes_sent=640,
        bytes_received=712,
        duration_seconds=2.34,
        state_timeline=(
            (0.0, "connecting"),
            (0.06, "connected"),
            (2.30, "disconnected"),
            (2.31, "closed"),
        ),
        graceful_disconnect=True,
    )


class TestRoomDashboard:
    def test_renders_room_and_invite_sections(self, console_manager: ConsoleManager) -> None:
        room = Room.create(name="Midnight Lounge", lifetime_minutes=60, host_id="host_ab12cd")
        invite = Invite.create(room_id=room.room_id, lifetime_minutes=15)
        render_room_dashboard(console_manager, room, invite, relay_note="Relay verified.")
        output = console_manager.export_text()
        assert "Room Hosted" in output
        assert room.room_id in output
        assert "Midnight Lounge" in output
        assert "host_ab12cd" in output
        assert invite.token in output
        assert "Relay verified." in output
        assert "Unnamed room" not in output

    def test_anonymous_room_uses_the_fallback_name(self, console_manager: ConsoleManager) -> None:
        room = Room.create()
        invite = Invite.create(room_id=room.room_id, lifetime_minutes=15)
        render_room_dashboard(console_manager, room, invite)
        output = console_manager.export_text()
        assert "Unnamed room" in output
        assert "encrypted chat" in output  # current-capabilities note


class TestJoinResult:
    def test_valid_result_shows_canonical_form(self, console_manager: ConsoleManager) -> None:
        render_join_result(
            console_manager,
            candidate="gl-room-abcd-efgh-jkmn",
            valid=True,
            canonical="gl-room-ABCD-EFGH-JKMN",
        )
        output = console_manager.export_text()
        assert "Join Room" in output
        assert "gl-room-ABCD-EFGH-JKMN" in output
        assert "Valid" in output

    def test_invalid_result_shows_format_guidance(self, console_manager: ConsoleManager) -> None:
        render_join_result(console_manager, candidate="nope", valid=False, canonical=None)
        output = console_manager.export_text()
        assert "Invalid" in output
        assert "gl-room-XXXX-XXXX-XXXX" in output


class TestRelayDashboard:
    def test_renders_every_panel(self, console_manager: ConsoleManager) -> None:
        render_relay_dashboard(console_manager, _probe_report())
        output = console_manager.export_text()
        assert "Relay Status" in output
        assert "Connected and measured" in output
        assert "ws://127.0.0.1:8787/relay" in output
        assert "sess_probe123" in output
        assert "plain WebSocket" in output
        assert "Graceful" in output
        assert "Latency" in output
        assert "Heartbeat" in output
        assert "Packets sent" in output
        assert "Connection timeline" in output
        assert "connecting" in output and "closed" in output


class TestSessionOverview:
    def test_empty_history(self, console_manager: ConsoleManager) -> None:
        render_session_overview(console_manager, session=None, rooms=[], invites=[])
        output = console_manager.export_text()
        assert "Session Overview" in output
        assert "No session history recorded yet." in output
        assert "Hosted rooms (0)" in output
        assert "Issued invites (0)" in output

    def test_populated_overview(self, console_manager: ConsoleManager) -> None:
        room = Room.create(name="Vault", lifetime_minutes=30)
        invite = Invite.create(room_id=room.room_id, lifetime_minutes=10).redeem()
        session = SessionInfo(started_at=NOW, first_launch_at=NOW, launch_count=7, pid=4242)
        render_session_overview(console_manager, session=session, rooms=[room], invites=[invite])
        output = console_manager.export_text()
        assert "Launch count" in output
        assert "4242" in output
        assert room.room_id in output
        assert "Vault" in output
        assert "Hosted rooms (1)" in output
        assert "Issued invites (1)" in output
        assert "Redeemed" in output


class TestRelayUnconfigured:
    def test_guidance_includes_both_configuration_paths(
        self, console_manager: ConsoleManager
    ) -> None:
        relay_unconfigured_panel(console_manager)
        output = console_manager.export_text()
        assert "Relay not configured" in output
        assert "relay-status --relay" in output
        assert "[relay]" in output
        assert "python -m ghostlink.transport.relay.server" in output
