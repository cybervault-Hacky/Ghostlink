"""``ghostlink host`` — create a room, share it, and host the secure chat."""

from __future__ import annotations

import asyncio

from ghostlink.cli.arguments import CLIOptions
from ghostlink.cli.commands.base import (
    build_runtime,
    relay_config_from,
    resolve_relay_url,
)
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.base import ExitCode
from ghostlink.ui.chat import run_chat_session
from ghostlink.ui.components.progress import StepProgress
from ghostlink.ui.dashboards import render_room_dashboard


def run_host(options: CLIOptions) -> int:
    """Create the room, display the invite, then enter the chat as host.

    Without a relay (flag or configuration) the room is recorded locally and
    the command explains how to go live — mirroring Phase 2 behaviour."""

    runtime = build_runtime(options)
    logger = get_logger("cli.host")

    relay_url = resolve_relay_url(runtime.settings, options)

    with (
        StepProgress(runtime.console, title="Hosting room…") as progress,
        progress.step("Creating room and invite"),
    ):
        room, invite = runtime.rooms.host_room(
            name=options.name,
            room_lifetime_minutes=options.lifetime,
            invite_one_time=False if options.multi_use else None,
            invite_lifetime_minutes=options.invite_lifetime,
        )

    logger.info("hosted room %s (invite %s…)", room.room_id, invite.token[:12])
    if relay_url is None:
        render_room_dashboard(
            runtime.console,
            room,
            invite,
            relay_note=(
                "No relay configured — room is recorded locally. Re-run with "
                "--relay ws://… or set [relay] url to host the live chat."
            ),
        )
        runtime.console.newline()
        return int(ExitCode.OK)

    render_room_dashboard(
        runtime.console,
        room,
        invite,
        relay_note=(f"Share the room ID with your peer — then wait here. Relay: {relay_url}"),
    )
    runtime.console.newline()
    logger.info("entering chat as host on %s", relay_url)
    return asyncio.run(
        run_chat_session(
            runtime.console,
            settings=runtime.settings,
            state_dir=runtime.config.state_dir,
            role="host",
            channel=room.room_id,
            relay_url=relay_url,
            client_config=relay_config_from(runtime.settings, options),
            display_name=options.chat_name,
        )
    )
