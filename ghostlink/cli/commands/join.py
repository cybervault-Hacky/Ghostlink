"""``ghostlink join`` — redeem an invite or join a room's secure chat."""

from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING

from rich.text import Text

from ghostlink.cli.arguments import CLIOptions
from ghostlink.cli.commands.base import (
    build_runtime,
    relay_config_from,
    resolve_relay_url,
)
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.base import ExitCode
from ghostlink.exceptions.invites import (
    InviteAlreadyUsedError,
    InviteError,
    InviteExpiredError,
    InviteRevokedError,
    InviteUnknownError,
    InviteValidationError,
)
from ghostlink.invites.formatter import (
    invite_expired_panel,
    invite_refused_panel,
)
from ghostlink.invites.redemption import redeem_invite
from ghostlink.invites.tokens import looks_like_invite_link, parse_invite_link
from ghostlink.models.room import is_valid_room_id, normalize_room_id
from ghostlink.ui.chat import run_chat_session
from ghostlink.ui.components.dialogs import notice_dialog
from ghostlink.ui.dashboards import render_join_result

if TYPE_CHECKING:
    from ghostlink.cli.commands.base import CommandRuntime


def run_join(options: CLIOptions) -> int:
    """Join via invite link (gl://join/…) or a plain room identifier.

    A relay is required for the live chat: ``--relay`` first, then the
    configured ``[relay] url``. Without one, the command reports on the
    room identifier only (Phase 2 behaviour). Invite links always require
    the relay authority — there is no offline redemption.
    """

    candidate = options.room_id or ""
    # Anything URL-shaped is treated as an invite-link attempt so the
    # scheme is validated properly — a mistyped https://… link must not
    # be silently reinterpreted as a room identifier.
    if looks_like_invite_link(candidate) or "://" in candidate:
        return asyncio.run(_run_invite_join(options, candidate))

    runtime = build_runtime(options)
    logger = get_logger("cli.join")
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
            identity_manager=runtime.identities,
            invite_manager=runtime.invites,
        )
    )


async def _run_invite_join(options: CLIOptions, link: str) -> int:
    """Redeem ``gl://join/<token>`` at the relay authority, then chat."""

    runtime = build_runtime(options)
    logger = get_logger("cli.join")

    # Fail-closed local validation before any network contact: scheme,
    # host, and token format. A malformed or foreign-scheme link dies here.
    try:
        parse_invite_link(link)
    except InviteValidationError as exc:
        return _render_invite_refusal(runtime, "Invalid invite link", exc.message, exc.hint)

    relay_url = resolve_relay_url(runtime.settings, options)
    if relay_url is None:
        return _render_invite_refusal(
            runtime,
            "Relay required",
            "Redeeming an invite needs the relay authority that issued it. "
            "Configure the same relay your peer used.",
            "Pass --relay ws://… or set [relay] url in the config.",
            exit_code=int(ExitCode.NETWORK),
        )

    try:
        outcome = await redeem_invite(
            link,
            relay_url=relay_url,
            client_config=relay_config_from(runtime.settings, options),
        )
    except InviteValidationError as exc:
        return _render_invite_refusal(runtime, "Invalid invite link", exc.message, exc.hint)
    except InviteExpiredError as exc:
        runtime.console.print(invite_expired_panel(runtime.console.width))
        if exc.hint:
            runtime.console.print(Text(f"  {exc.hint}", style="gl.muted"))
        runtime.console.newline()
        return int(ExitCode.INVITE)
    except InviteAlreadyUsedError as exc:
        return _render_invite_refusal(
            runtime,
            "Invite already used",
            "This one-time invite was already consumed.",
            exc.hint,
        )
    except InviteRevokedError as exc:
        return _render_invite_refusal(
            runtime,
            "Invite revoked",
            "The creator revoked this invite.",
            exc.hint,
        )
    except InviteUnknownError as exc:
        return _render_invite_refusal(
            runtime,
            "Invite not found",
            "The relay has no record of this invite.",
            exc.hint,
        )
    except InviteError as exc:
        return _render_invite_refusal(runtime, "Invite refused", exc.message, exc.hint)
    logger.info("invite %s redeemed → %s", outcome.invite_id, outcome.room_id)
    runtime.console.print(
        Text(
            f"✓ Invite accepted — joining {outcome.room_id} as guest.",
            style="gl.success",
        )
    )
    runtime.console.newline()
    with contextlib.suppress(Exception):
        runtime.invites.record_incoming_redemption(
            outcome.invite_id,
            room_id=outcome.room_id,
            expires_at=outcome.expires_at,
            relay_url=relay_url,
        )
    return await run_chat_session(
        runtime.console,
        settings=runtime.settings,
        state_dir=runtime.config.state_dir,
        role="guest",
        channel=outcome.room_id,
        relay_url=relay_url,
        client_config=relay_config_from(runtime.settings, options),
        display_name=options.chat_name,
        identity_manager=runtime.identities,
        invite_manager=runtime.invites,
        session_invite_id=outcome.invite_id,
    )


def _render_invite_refusal(
    runtime: CommandRuntime,
    reason: str,
    detail: str,
    hint: str | None,
    *,
    exit_code: int = int(ExitCode.INVITE),
) -> int:
    """Consistent screen for every invite failure: panel, hint, exit 7."""

    runtime.console.print(
        invite_refused_panel(reason=reason, detail=detail, console_width=runtime.console.width)
    )
    if hint:
        runtime.console.print(Text(f"  {hint}", style="gl.muted"))
    runtime.console.newline()
    return exit_code
