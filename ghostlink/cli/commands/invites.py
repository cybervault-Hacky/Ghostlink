"""``ghostlink invite`` — create and manage one-time join invites.

``invite create`` mints a token, registers it with the relay authority
(which enforces expiration and single redemption), prints the invite card,
and either enters the chat as host or runs a standalone countdown
(``--no-chat``). ``list`` / ``info`` / ``revoke`` manage local records and
the authoritative relay state.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from datetime import timedelta

from rich.text import Text

from ghostlink.cli.arguments import CLIOptions
from ghostlink.cli.commands.base import (
    CommandRuntime,
    build_runtime,
    relay_config_from,
    resolve_relay_url,
)
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.base import ExitCode
from ghostlink.invites.expiration import parse_duration_seconds
from ghostlink.invites.formatter import (
    invite_created_panel,
    invite_expired_panel,
    invite_info_panel,
    invite_refused_panel,
    invite_waiting_line,
    invites_table,
    resolve_invite_id_argument,
)
from ghostlink.invites.lifecycle import SecureInviteManager
from ghostlink.invites.models import InviteRecord, InviteState
from ghostlink.models.room import is_valid_room_id
from ghostlink.transport.relay.client import RelayClient
from ghostlink.transport.relay.endpoint import RelayEndpoint
from ghostlink.ui.chat import run_chat_session


def run_invite(options: CLIOptions) -> int:
    """Dispatch the invite subcommands."""

    runtime = build_runtime(options)
    logger = get_logger("cli.invite")
    action = options.invite_action or "create"

    if action == "create":
        return asyncio.run(_run_create(runtime, options, logger))
    if action == "list":
        return _run_list(runtime)
    if action in ("info", "revoke"):
        return asyncio.run(_run_inspect(runtime, options, logger, revoke=action == "revoke"))
    # argparse choices make this unreachable.
    raise ValueError(f"Unknown invite action {action!r}")


# -------------------------------------------------------------------- create


async def _run_create(runtime: CommandRuntime, options: CLIOptions, logger: logging.Logger) -> int:
    settings = runtime.settings

    # Validate invite inputs before touching the network: a bad --expires,
    # --uses, or --room must fail fast and locally.
    expires_seconds = _resolve_expiry(runtime, options)
    if expires_seconds is None:
        return int(ExitCode.INVITE)
    uses = _resolve_uses(runtime, options)
    if uses is None:
        return int(ExitCode.INVITE)

    room_id = None
    if options.invite_room is not None:
        if not is_valid_room_id(options.invite_room):
            runtime.console.print(
                Text("✗ --room must look like gl-room-XXXX-XXXX-XXXX.", style="gl.error")
            )
            return int(ExitCode.CONFIGURATION)
        room_id = options.invite_room

    relay_url = resolve_relay_url(settings, options)
    if relay_url is None:
        runtime.console.print(
            invite_refused_panel(
                reason="Relay required",
                detail=(
                    "Invites are enforced by the relay authority — there is no "
                    "fallback that weakens expiry or one-time use. Configure a "
                    "relay first."
                ),
                console_width=runtime.console.width,
            )
        )
        runtime.console.print(
            Text("Pass --relay ws://… or set [relay] url in the config.", style="gl.muted")
        )
        runtime.console.newline()
        return int(ExitCode.NETWORK)

    manager = runtime.invites
    record, link = manager.mint(
        ttl_seconds=expires_seconds,
        max_redemptions=uses,
        room_id=room_id,
        relay_url=relay_url,
    )
    logger.info("invite minted — %s", record.invite_id)

    client_config = relay_config_from(settings, options)
    endpoint = RelayEndpoint.from_url(relay_url)
    client = RelayClient(endpoint, client_name="ghostlink-host", config=client_config)
    try:
        await client.connect()
        await manager.register_with_relay(client, record)
    except Exception:
        await _quiet_disconnect(client)
        raise
    logger.info("invite %s registered with %s", record.invite_id, relay_url)

    runtime.console.print(
        invite_created_panel(
            link=link,
            expires_seconds=expires_seconds,
            max_redemptions=uses,
            status=InviteState.ACTIVE,
            console_width=runtime.console.width,
        )
    )
    runtime.console.print(
        Text("Share the link out of band — type it, don't post it publicly.", style="gl.muted")
    )
    runtime.console.newline()

    if options.no_chat:
        return await _run_countdown(runtime, record, manager, client)

    runtime.console.print(invite_waiting_line(record.remaining_seconds()))
    logger.info("entering chat as inviter on %s", record.room_id)
    exit_code = await run_chat_session(
        runtime.console,
        settings=settings,
        state_dir=runtime.config.state_dir,
        role="host",
        channel=record.room_id,
        relay_url=relay_url,
        client_config=client_config,
        display_name=options.chat_name,
        identity_manager=runtime.identities,
        invite_manager=manager,
        existing_client=client,
        session_invite_id=record.invite_id,
    )
    await _quiet_disconnect(client)
    return exit_code


def _resolve_expiry(runtime: CommandRuntime, options: CLIOptions) -> float | None:
    invites_cfg = runtime.settings.invites
    if options.expires is None:
        return float(invites_cfg.default_expiry_seconds)
    try:
        seconds = float(parse_duration_seconds(options.expires))
    except Exception as exc:
        runtime.console.print(Text(f"✗ {exc}", style="gl.error"))
        return None
    if seconds < 1 or seconds > invites_cfg.max_expiry_seconds:
        runtime.console.print(
            Text(
                f"✗ --expires must be between 1 and "
                f"{invites_cfg.max_expiry_seconds} seconds (your configured cap).",
                style="gl.error",
            )
        )
        return None
    return seconds


def _resolve_uses(runtime: CommandRuntime, options: CLIOptions) -> int | None:
    if options.uses is None:
        return 1
    if options.uses < 1 or options.uses > 16:
        runtime.console.print(Text("✗ --uses must be between 1 and 16.", style="gl.error"))
        return None
    return options.uses


async def _run_countdown(
    runtime: CommandRuntime,
    record: InviteRecord,
    manager: SecureInviteManager,
    client: RelayClient,
) -> int:
    """Standalone countdown (--no-chat): tick to expiry on the monotonic clock."""

    deadline = time.monotonic() + max(0.0, record.remaining_seconds())
    try:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            runtime.console.print(invite_waiting_line(left))
            await asyncio.sleep(min(1.0, left))
    except KeyboardInterrupt:
        runtime.console.newline()
        runtime.console.print(
            Text("Interrupted — revoking the invite so it cannot be used.", style="gl.muted")
        )
        with contextlib.suppress(Exception):
            await manager.revoke_via_relay(client, record.invite_id)
        await _quiet_disconnect(client)
        return int(ExitCode.INTERRUPTED)
    await _quiet_disconnect(client)
    with contextlib.suppress(Exception):
        manager.revoke_local(record.invite_id)
    runtime.console.newline()
    runtime.console.print(invite_expired_panel(runtime.console.width))
    runtime.console.newline()
    return int(ExitCode.OK)


async def _quiet_disconnect(client: RelayClient) -> None:
    with contextlib.suppress(Exception):
        await client.disconnect(reason="invite flow complete")


# ---------------------------------------------------------------------- list


def _run_list(runtime: CommandRuntime) -> int:
    manager = runtime.invites
    manager.apply_retention(timedelta(hours=runtime.settings.invites.retention_hours))
    records = manager.list()
    runtime.console.newline()
    if not records:
        runtime.console.print(
            Text(
                "No invites yet — create one: ghostlink invite create --expires 5m",
                style="gl.muted",
            )
        )
        runtime.console.newline()
        return int(ExitCode.OK)
    runtime.console.print(invites_table(records))
    runtime.console.newline()
    runtime.console.print(
        Text(
            "Secret tokens are only shown once, at creation. "
            "Revoke with: ghostlink invite revoke <id>",
            style="gl.muted",
        )
    )
    runtime.console.newline()
    return int(ExitCode.OK)


# -------------------------------------------------------------- info/revoke


async def _run_inspect(
    runtime: CommandRuntime, options: CLIOptions, logger: logging.Logger, *, revoke: bool
) -> int:
    manager = runtime.invites
    manager.apply_retention(timedelta(hours=runtime.settings.invites.retention_hours))
    target = options.invite_target or ""
    invite_id = resolve_invite_id_argument(target, manager.list())
    if invite_id is None:
        runtime.console.print(
            invite_refused_panel(
                reason="Invite not found",
                detail=f"No local invite matches '{target or '∅'}'.",
                console_width=runtime.console.width,
            )
        )
        runtime.console.newline()
        return int(ExitCode.INVITE)

    record = manager.require(invite_id)
    if not revoke:
        runtime.console.print(invite_info_panel(record, console_width=runtime.console.width))
        runtime.console.newline()
        return int(ExitCode.OK)

    if record.state is InviteState.REDEEMED:
        runtime.console.print(
            invite_refused_panel(
                reason="Already redeemed",
                detail=f"Invite {invite_id} was already consumed — nothing to revoke.",
                console_width=runtime.console.width,
            )
        )
        runtime.console.newline()
        return int(ExitCode.INVITE)
    if record.state is InviteState.REVOKED:
        runtime.console.print(Text(f"Invite {invite_id} is already revoked.", style="gl.muted"))
        return int(ExitCode.OK)

    relay_url = record.relay_url or resolve_relay_url(runtime.settings, options)
    token = manager.token_for(invite_id)
    if relay_url and token is not None:
        client = RelayClient(
            RelayEndpoint.from_url(relay_url),
            client_name="ghostlink-host",
            config=relay_config_from(runtime.settings, options),
        )
        try:
            await client.connect()
            await manager.revoke_via_relay(client, invite_id)
        finally:
            await _quiet_disconnect(client)
        logger.info("invite %s revoked at %s", invite_id, relay_url)
    else:
        manager.revoke_local(invite_id)
        logger.info("invite %s revoked locally (relay/token unavailable)", invite_id)

    runtime.console.print(
        Text(f"✓ Invite {invite_id} revoked — redemption will now fail.", style="gl.success")
    )
    runtime.console.newline()
    return int(ExitCode.OK)
