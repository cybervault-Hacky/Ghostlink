"""``ghostlink identity`` — inspect and manage the local ephemeral identity."""

from __future__ import annotations

import logging
from datetime import UTC

from rich import box
from rich.console import RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ghostlink.cli.arguments import CLIOptions
from ghostlink.cli.commands.base import CommandRuntime, build_runtime
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.base import ExitCode
from ghostlink.exceptions.invites import InviteValidationError
from ghostlink.identity.fingerprint import identity_fingerprint

_TITLE: str = "[gl.title]LOCAL GHOSTLINK IDENTITY[/]"


def _panel(runtime: CommandRuntime, body: RenderableType, *, subtitle: str) -> Panel:
    return Panel(
        body,
        title=_TITLE,
        subtitle=f"[gl.muted]{subtitle}[/]",
        box=box.DOUBLE,
        border_style="gl.accent",
        padding=(0, 2),
        width=min(58, max(34, runtime.console.width)),
        expand=False,
    )


def _fingerprint_panel(
    runtime: CommandRuntime, *, nickname: str, identity_id: str, fingerprint: str
) -> Panel:
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="gl.muted", no_wrap=True)
    grid.add_column(style="gl.text")
    grid.add_row("Nickname:", nickname or Text("not set", style="gl.muted"))
    grid.add_row("Identity:", Text(identity_id, style="gl.accent"))
    grid.add_row("Fingerprint:", Text(fingerprint, style="gl.accent"))
    return _panel(runtime, grid, subtitle="compare with your peer out-of-band")


def run_identity(options: CLIOptions) -> int:
    """Show the identity, its fingerprint, or manage the nickname.

    Everything rendered here is public material: the handle, nickname and
    fingerprint derive from the public key. The private key never leaves
    the owner-only identity store and is never displayed or logged.
    """

    runtime = build_runtime(options)
    logger = get_logger("cli.identity")
    action = options.identity_action or "show"

    if action == "nickname":
        return _run_nickname(runtime, options, logger)

    identity = runtime.identities.ensure()
    fingerprint = identity_fingerprint(identity.public_key_bytes)

    if action == "fingerprint":
        runtime.console.print(
            _fingerprint_panel(
                runtime,
                nickname=identity.nickname,
                identity_id=identity.identity_id,
                fingerprint=fingerprint,
            )
        )
        runtime.console.newline()
        return int(ExitCode.OK)

    created = identity.created_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="gl.muted", no_wrap=True)
    grid.add_column(style="gl.text")
    grid.add_row("Nickname:", identity.nickname or Text("not set", style="gl.muted"))
    grid.add_row("Identity:", Text(identity.identity_id, style="gl.accent"))
    grid.add_row("Fingerprint:", Text(fingerprint, style="gl.accent"))
    grid.add_row("Created:", created)
    runtime.console.print(
        _panel(runtime, grid, subtitle="local only — no account, nothing uploaded")
    )
    runtime.console.newline()
    hint = Text(
        "Share your fingerprint only with people you chat with, and compare it "
        "out-of-band when authenticity matters. Set a nickname with "
        "'ghostlink identity nickname <name>'. Choose any pseudonym — "
        "real-world details are never requested.",
        style="gl.muted",
    )
    runtime.console.print(hint)
    runtime.console.newline()
    return int(ExitCode.OK)


def _run_nickname(runtime: CommandRuntime, options: CLIOptions, logger: logging.Logger) -> int:
    nickname = options.nickname
    if nickname is None:
        identity = runtime.identities.ensure()
        current = identity.nickname or "not set"
        runtime.console.print(
            Text(
                f"Current nickname: {current} — set one with 'ghostlink identity nickname <name>'."
            )
        )
        runtime.console.newline()
        return int(ExitCode.OK)
    try:
        identity = runtime.identities.set_nickname(nickname)
    except InviteValidationError as exc:
        runtime.console.print(Text(f"✗ {exc.message}", style="gl.error"))
        if exc.hint:
            runtime.console.print(Text(f"  {exc.hint}", style="gl.muted"))
        return int(ExitCode.INVITE)
    logger.info("nickname updated for %s", identity.identity_id)
    runtime.console.print(
        Text(f"✓ Nickname set to {identity.nickname} ({identity.identity_id}).", style="gl.success")
    )
    runtime.console.newline()
    return int(ExitCode.OK)
