"""Terminal rendering for invites and peer verification (Phase 5).

Pure renderable builders (no side effects): the CLI commands and the chat
surface print them through the shared console. Everything degrades cleanly
on narrow Termux terminals — panels clamp to the live console width.
"""

from __future__ import annotations

from datetime import UTC, datetime

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ghostlink.invites.expiration import format_countdown, format_duration_words
from ghostlink.invites.models import InviteRecord, InviteState
from ghostlink.invites.tokens import is_valid_invite_id

_PANEL_MIN_WIDTH: int = 34


def _panel_width(console_width: int, preferred: int) -> int:
    return min(preferred, max(_PANEL_MIN_WIDTH, console_width))


def _state_style(state: InviteState) -> str:
    if state is InviteState.ACTIVE:
        return "gl.success"
    if state is InviteState.REVOKED:
        return "gl.error"
    if state in (InviteState.EXPIRED, InviteState.REDEEMED):
        return "gl.muted"
    return "gl.accent"


# ------------------------------------------------------------- invite created


def invite_created_panel(
    *,
    link: str,
    expires_seconds: float,
    max_redemptions: int,
    status: InviteState,
    console_width: int,
) -> Panel:
    """The bordered invite card shown right after ``invite create``."""

    grid = Table.grid(padding=(0, 1))
    grid.add_column(style="gl.muted", no_wrap=True)
    grid.add_column(style="gl.text")
    grid.add_row("Invite:", Text(link, style="gl.accent"))
    grid.add_row("Expires:", format_duration_words(expires_seconds))
    grid.add_row("Uses:", str(max_redemptions))
    grid.add_row("Status:", Text(status.value.upper(), style=_state_style(status)))
    return Panel(
        grid,
        title="[gl.title]SECURE GHOSTLINK INVITE[/]",
        box=box.DOUBLE,
        border_style="gl.accent",
        padding=(0, 2),
        width=_panel_width(console_width, 46),
        expand=False,
    )


def invite_waiting_line(seconds_left: float) -> Text:
    """The monotonic countdown line under an open invite."""

    return Text(
        f"Waiting for peer… expires in {format_countdown(seconds_left)}",
        style="gl.muted",
    )


# -------------------------------------------------------------- refusal cards


def invite_expired_panel(console_width: int) -> Panel:
    """The exact 'INVITE EXPIRED' card from the join flow."""

    body = Text("This invitation is no longer\nvalid.", style="gl.text")
    return Panel(
        body,
        title="[gl.error]INVITE EXPIRED[/]",
        box=box.DOUBLE,
        border_style="gl.error",
        padding=(0, 2),
        width=_panel_width(console_width, 34),
        expand=False,
    )


def invite_refused_panel(*, reason: str, detail: str, console_width: int) -> Panel:
    """Refusal card for 'already used' / 'revoked' / 'unknown' outcomes."""

    return Panel(
        Text(detail, style="gl.text"),
        title=f"[gl.error]{reason.upper()}[/]",
        box=box.DOUBLE,
        border_style="gl.error",
        padding=(0, 2),
        width=_panel_width(console_width, 40),
        expand=False,
    )


# ---------------------------------------------------------------- list / info


def invite_state_label(record: InviteRecord, *, now: datetime | None = None) -> str:
    """Uppercase effective state for tables (expiry folded in)."""

    moment = now if now is not None else datetime.now(UTC)
    return record.effective_state(moment).value.upper()


def invites_table(records: list[InviteRecord], *, now: datetime | None = None) -> Table:
    """`invite list`: id, state, created, expires, uses, remaining lifetime."""

    moment = now if now is not None else datetime.now(UTC)
    table = Table(
        box=box.SIMPLE_HEAVY,
        header_style="gl.muted",
        show_edge=False,
        pad_edge=False,
    )
    table.add_column("Invite ID", style="gl.accent", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Created", style="gl.muted", no_wrap=True)
    table.add_column("Expires", style="gl.muted", no_wrap=True)
    table.add_column("Uses", justify="right", no_wrap=True)
    table.add_column("Lifetime left", justify="right", no_wrap=True)
    for record in records:
        state = record.effective_state(moment)
        created = record.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M")
        remaining = record.remaining_seconds(moment)
        remaining_text = format_countdown(remaining) if state is InviteState.ACTIVE else "—"
        table.add_row(
            record.invite_id,
            Text(state.value.upper(), style=_state_style(state)),
            created,
            record.expires_at.astimezone(UTC).strftime("%H:%M:%S"),
            f"{record.redemptions}/{record.max_redemptions}",
            remaining_text,
        )
    return table


def invite_info_panel(record: InviteRecord, *, console_width: int) -> Panel:
    """`invite info <id>` — safe metadata only, never the token."""

    moment = datetime.now(UTC)
    state = record.effective_state(moment)
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="gl.muted", no_wrap=True)
    grid.add_column(style="gl.text")
    grid.add_row("Invite", record.invite_id)
    grid.add_row("Status", Text(state.value.upper(), style=_state_style(state)))
    grid.add_row("Created", record.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"))
    grid.add_row("Expires", record.expires_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S UTC"))
    grid.add_row("Uses", f"{record.redemptions} of {record.max_redemptions}")
    if state is InviteState.ACTIVE:
        grid.add_row("Lifetime left", format_countdown(record.remaining_seconds(moment)))
    grid.add_row("Room", record.room_id)
    if record.session_binding:
        grid.add_row("Bound to", record.session_binding)
    grid.add_row("Protocol", f"relay v{record.protocol_version}")
    return Panel(
        grid,
        title="[gl.title]Invite details[/]",
        subtitle="[gl.muted]The secret token is never shown again[/]",
        box=box.ROUNDED,
        border_style="gl.accent",
        padding=(0, 1),
        width=_panel_width(console_width, 52),
        expand=False,
    )


def invite_targets_hint() -> Text:
    return Text(
        "Resolve invites by id (gi_…) or paste a full gl://join/… link.",
        style="gl.muted",
    )


# --------------------------------------------------------- peer verification


def verification_panel(
    *,
    peer_name: str,
    peer_fingerprint: str | None,
    own_fingerprint: str,
    own_name: str,
    safety_code: str | None,
    identity_bound: bool,
    console_width: int,
) -> Panel:
    """`fingerprint` / post-join verification card.

    Honest framing: comparing fingerprints out-of-band authenticates the
    peer for this conversation. It does not provide anonymity or hide any
    network activity.
    """

    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="gl.muted", no_wrap=True)
    grid.add_column(style="gl.text")
    grid.add_row("You", f"{own_name}")
    grid.add_row("Your fingerprint", Text(own_fingerprint, style="gl.accent"))
    grid.add_row("Peer", peer_name)
    if peer_fingerprint is not None:
        grid.add_row("Peer fingerprint", Text(peer_fingerprint, style="gl.accent"))
    else:
        grid.add_row("Peer fingerprint", Text("not presented — unverified", style="gl.muted"))
    if safety_code is not None:
        grid.add_row("Safety code", Text(safety_code, style="gl.accent"))
    binding = (
        "transcript-bound" if identity_bound else "not bound — compare out-of-band before trusting"
    )
    grid.add_row("Binding", Text(binding, style="gl.muted"))
    note = Text.from_markup(
        "[gl.muted]Compare fingerprints with your peer through a trusted out-of-band\n"
        "channel (a call, another messenger) to be sure you are talking to the\n"
        "right person. Verification authenticates this conversation — it does\n"
        "not provide anonymity or hide network activity.[/]"
    )
    return Panel(
        Group(grid, Text(""), note),
        title="[gl.title]PEER VERIFICATION[/]",
        box=box.ROUNDED,
        border_style="gl.accent",
        padding=(0, 1),
        width=_panel_width(console_width, 62),
        expand=False,
    )


def resolve_invite_id_argument(argument: str, records: list[InviteRecord]) -> str | None:
    """Match a user-typed id/link against known records (prefix-friendly)."""

    candidate = argument.strip()
    if not candidate:
        return None
    for record in records:
        if record.invite_id == candidate.lower():
            return record.invite_id
    if is_valid_invite_id(candidate):
        matches = [r.invite_id for r in records if r.invite_id.startswith(candidate.lower())]
        if len(matches) == 1:
            return matches[0]
    return None
