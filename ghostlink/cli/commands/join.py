"""``ghostlink join`` — validate a room identifier and join its secure chat."""

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
from ghostlink.models.room import is_valid_room_id, normalize_room_id
from ghostlink.ui.chat import run_chat_session
from ghostlink.ui.components.dialogs import notice_dialog
from ghostlink.ui.dashboards import render_join_result


def run_join(options: CLIOptions) -> int:
    """Validate the room id, then drop into the encrypted conversation.

    A relay is required for the live chat: ``--relay`` first, then the
    configured ``[relay] url``. Without one, the command reports on the room
    identifier only (Phase 2 behaviour)."""

    runtime = build_runtime(options)
    logger = get_logger("cli.join")
    candidate = options.room_id or ""
    canonical = normalize_room_id(candidate)
    valid = is_valid_room_id(candidate) or is_valid_room_id(canonical)
    room_id = candidate if is_valid_room_id(candidate) else canonical

    relay_url = resolve_relay_url(runtime.settings, options)
    logger.info("join — room=%r valid=%s relay=%s", candidate, valid, bool(relay_url))

    if not valid or relay_url is None:
        render_join_result(
            runtime.console,
            candidate=candidate,
            valid=valid,
            canonical=canonical if valid else None,
        )
        if valid and relay_url is None:
            runtime.console.newline()
            runtime.console.print(
                notice_dialog(
                    "Relay required for chat",
                    "The room identifier is valid, but joining the live, "
                    "end-to-end encrypted chat needs a relay endpoint.",
                    hint="Pass --relay ws://… or set url under [relay] in the "
                    "configuration, then run join again.",
                )
            )
        runtime.console.newline()
        return int(ExitCode.OK if valid else ExitCode.CONFIGURATION)

    logger.info("entering chat as guest in %s via %s", room_id, relay_url)
    return asyncio.run(
        run_chat_session(
            runtime.console,
            settings=runtime.settings,
            state_dir=runtime.config.state_dir,
            role="guest",
            channel=room_id,
            relay_url=relay_url,
            client_config=relay_config_from(runtime.settings, options),
            display_name=options.chat_name,
        )
    )
