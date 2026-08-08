"""Networking dashboards (Phase 2).

Composer functions shared by the CLI commands and the interactive menu: the
room-hosting dashboard, the invite-validation result, the relay status
report, and the session overview. Each takes live data and returns nothing —
rendering only, no state mutation.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from rich.console import Group, RenderableType
from rich.table import Table
from rich.text import Text

from ghostlink.models.invite import Invite, InviteEffectiveState
from ghostlink.models.room import Room, RoomEffectiveState
from ghostlink.models.session import SessionInfo
from ghostlink.transport.relay.client import RelayProbeReport
from ghostlink.ui.components.badges import BadgeTone, badge
from ghostlink.ui.components.charts import latency_panel
from ghostlink.ui.components.panels import app_panel, section_panel
from ghostlink.ui.components.tables import info_table, kv_grid
from ghostlink.ui.console import ConsoleManager
from ghostlink.utils.text import format_duration

_TONE_FOR_ROOM_STATE = {
    RoomEffectiveState.OPEN: BadgeTone.SUCCESS,
    RoomEffectiveState.EXPIRED: BadgeTone.WARNING,
    RoomEffectiveState.CLOSED: BadgeTone.MUTED,
}

_TONE_FOR_INVITE_STATE = {
    InviteEffectiveState.ACTIVE: BadgeTone.SUCCESS,
    InviteEffectiveState.EXPIRED: BadgeTone.WARNING,
    InviteEffectiveState.REDEEMED: BadgeTone.MUTED,
    InviteEffectiveState.REVOKED: BadgeTone.ERROR,
}


def _room_state_badge(room: Room, console: ConsoleManager) -> Text:
    effective = room.effective_state()
    return badge(effective.value.title(), _TONE_FOR_ROOM_STATE[effective], theme=console.theme)


def _invite_state_badge(invite: Invite, console: ConsoleManager) -> Text:
    effective = invite.effective_state()
    return badge(effective.value.title(), _TONE_FOR_INVITE_STATE[effective], theme=console.theme)


def _format_expiry(at: datetime | None) -> str:
    if at is None:
        return "never"
    remaining = (at - datetime.now(UTC)).total_seconds()
    if remaining <= 0:
        return "expired"
    return f"in {format_duration(remaining)}"


# ------------------------------------------------------------------- hosting


def render_room_dashboard(
    console: ConsoleManager,
    room: Room,
    invite: Invite,
    *,
    relay_note: str | None = None,
) -> None:
    """The dashboard shown after a room is hosted (CLI and menu share this)."""

    room_rows = kv_grid(
        [
            ("Room ID", Text(room.room_id, style="gl.highlight")),
            ("Name", Text(room.display_name)),
            ("State", _room_state_badge(room, console)),
            ("Created", Text(room.created_at.strftime("%Y-%m-%d %H:%M:%S UTC"))),
            ("Expires", Text(_format_expiry(room.expires_at))),
            ("Host", Text(room.host_id)),
            ("Guests", Text("one-to-one — the peer joins via the chat", style="gl.muted")),
        ]
    )

    invite_rows = kv_grid(
        [
            ("Token", Text(invite.token, style="gl.highlight")),
            (
                "One-time",
                badge("Yes" if invite.one_time else "No", BadgeTone.ACCENT, theme=console.theme),
            ),
            ("State", _invite_state_badge(invite, console)),
            ("Expires", Text(_format_expiry(invite.expires_at))),
        ]
    )

    footer_lines: list[Text] = []
    if relay_note:
        footer_lines.append(Text(relay_note, style="gl.text"))
    footer_lines.append(
        Text(
            "End-to-end encrypted chat — share the room ID and start talking.",
            style="gl.muted",
        )
    )

    console.newline()
    console.print(
        section_panel(
            "Room Hosted",
            Group(
                app_panel(room_rows, title="Room", padding=(1, 2), expand=False),
                Text(""),
                app_panel(invite_rows, title="Invite", padding=(1, 2), expand=False),
                Text(""),
                *footer_lines,
            ),
            subtitle=room.room_id,
        )
    )


# -------------------------------------------------------------------- join


def render_join_result(
    console: ConsoleManager,
    *,
    candidate: str,
    valid: bool,
    canonical: str | None,
) -> None:
    """Result panel for room-identifier validation (join flow)."""

    if valid and canonical is not None:
        body = Group(
            kv_grid(
                [
                    ("Input", Text(candidate)),
                    ("Canonical", Text(canonical, style="gl.highlight")),
                    ("Format", badge("Valid", BadgeTone.SUCCESS, theme=console.theme)),
                ]
            ),
            Text(""),
            Text(
                "Room session establishment arrives with Phase 3 messaging.",
                style="gl.muted",
            ),
        )
        console.newline()
        console.print(app_panel(body, title="Join Room", tone=BadgeTone.SUCCESS, expand=False))
    else:
        body = Group(
            kv_grid(
                [
                    ("Input", Text(candidate)),
                    ("Format", badge("Invalid", BadgeTone.ERROR, theme=console.theme)),
                ]
            ),
            Text(""),
            Text(
                "Expected format: gl-room-XXXX-XXXX-XXXX using unambiguous "
                "characters (no 0/O, 1/I).",
                style="gl.muted",
            ),
        )
        console.newline()
        console.print(app_panel(body, title="Join Room", tone=BadgeTone.ERROR, expand=False))


# ------------------------------------------------------------------ relay


def render_relay_dashboard(console: ConsoleManager, report: RelayProbeReport) -> None:
    """The full relay status dashboard after a live probe."""

    security = "TLS protected" if report.secure else "plain WebSocket"
    overview = kv_grid(
        [
            ("Endpoint", Text(report.endpoint)),
            ("Security", Text(security)),
            ("Session", Text(report.session_id, style="gl.highlight")),
            ("Server", Text(f"{report.server_name} · protocol v{report.protocol_version}")),
            (
                "Disconnect",
                badge(
                    "Graceful" if report.graceful_disconnect else "Abrupt",
                    BadgeTone.SUCCESS if report.graceful_disconnect else BadgeTone.WARNING,
                    theme=console.theme,
                ),
            ),
        ]
    )

    heartbeat_rows = kv_grid(
        [
            ("Heartbeats acked", Text(str(report.heartbeats_acked))),
            ("Ping samples", Text(str(len(report.rtt_samples_ms)))),
            (
                "Round trips ok",
                badge(
                    "Yes" if report.rtt_samples_ms else "No",
                    BadgeTone.SUCCESS if report.rtt_samples_ms else BadgeTone.ERROR,
                    theme=console.theme,
                ),
            ),
        ]
    )

    stats_rows = info_table(
        [
            ("Packets sent", str(report.packets_sent)),
            ("Packets received", str(report.packets_received)),
            ("Bytes sent", str(report.bytes_sent)),
            ("Bytes received", str(report.bytes_received)),
            ("Probe duration", f"{report.duration_seconds:.2f}s"),
        ],
        label_header="Statistic",
        value_header="Value",
    )

    timeline = Table(box=None, show_header=True, header_style="gl.accent", show_edge=False)
    timeline.add_column("Elapsed", style="gl.muted", no_wrap=True)
    timeline.add_column("State", style="gl.text")
    for offset, state in report.state_timeline:
        timeline.add_row(f"+{offset:0.3f}s", state)

    console.newline()
    console.print(
        section_panel(
            "Relay Status",
            Group(
                Text.assemble(("● ", "gl.success"), ("Connected and measured", "gl.text")),
                Text(""),
                overview,
                Text(""),
                latency_panel(report.rtt_samples_ms, theme=console.theme),
                Text(""),
                app_panel(heartbeat_rows, title="Heartbeat", padding=(0, 2), expand=False),
                Text(""),
                stats_rows,
                Text(""),
                Text("Connection timeline", style="gl.accent"),
                timeline,
            ),
            subtitle="live measurement",
        )
    )


# ----------------------------------------------------------------- sessions


def render_session_overview(
    console: ConsoleManager,
    *,
    session: SessionInfo | None,
    rooms: Sequence[Room],
    invites: Sequence[Invite],
) -> None:
    """The `ghostlink session` dashboard: app session + hosted artifacts."""

    parts: list[RenderableType] = []

    if session is None:
        parts.append(Text("No session history recorded yet.", style="gl.muted"))
    else:
        parts.append(
            kv_grid(
                [
                    ("Launch count", Text(str(session.launch_count))),
                    ("Session started", Text(session.started_at.strftime("%Y-%m-%d %H:%M:%S UTC"))),
                    (
                        "First launch",
                        Text(session.first_launch_at.strftime("%Y-%m-%d %H:%M:%S UTC")),
                    ),
                    ("Process ID", Text(str(session.pid))),
                ]
            )
        )

    rooms_table = Table(box=None, show_header=True, header_style="gl.accent", show_edge=False)
    rooms_table.add_column("Room", style="gl.text", no_wrap=True)
    rooms_table.add_column("Name", style="gl.muted")
    rooms_table.add_column("State", no_wrap=True)
    for room in rooms:
        rooms_table.add_row(room.room_id, room.display_name, _room_state_badge(room, console))

    invites_table = Table(box=None, show_header=True, header_style="gl.accent", show_edge=False)
    invites_table.add_column("Invite", style="gl.text", no_wrap=True)
    invites_table.add_column("Room", style="gl.muted", no_wrap=True)
    invites_table.add_column("Uses", justify="right")
    invites_table.add_column("State", no_wrap=True)
    for invite in invites:
        invites_table.add_row(
            f"{invite.token[:14]}…",
            invite.room_id,
            str(invite.uses),
            _invite_state_badge(invite, console),
        )

    parts.append(Text(""))
    parts.append(Text(f"Hosted rooms ({len(rooms)})", style="gl.accent"))
    parts.append(rooms_table if rooms else Text("none yet", style="gl.muted"))
    parts.append(Text(""))
    parts.append(Text(f"Issued invites ({len(invites)})", style="gl.accent"))
    parts.append(invites_table if invites else Text("none yet", style="gl.muted"))

    console.newline()
    console.print(section_panel("Session Overview", Group(*parts)))


def relay_unconfigured_panel(console: ConsoleManager) -> None:
    """Guidance when no relay is configured and none was passed."""

    body = Group(
        Text("No relay endpoint is configured for this installation.", style="gl.text"),
        Text(""),
        Text("Point GhostLink at a relay in one of two ways:", style="gl.text"),
        Text("  ·  relay-status --relay ws://127.0.0.1:8787", style="gl.accent"),
        Text("  ·  set 'url' in the [relay] section of your config file", style="gl.accent"),
        Text(""),
        Text(
            "A development relay ships with GhostLink: python -m ghostlink.transport.relay.server",
            style="gl.muted",
        ),
    )
    console.newline()
    console.print(app_panel(body, title="Relay not configured", tone=BadgeTone.INFO, expand=False))
