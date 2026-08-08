"""``ghostlink group`` — secure group lifecycle commands (Phase 6B).

Thin CLI over :class:`ghostlink.groups.lifecycle.LocalGroupManager`: the
command layer parses arguments, renders panels, and maps domain outcomes
to exit codes — every security decision (ownership, capacity, epochs,
signatures) happens in the domain/service layer, never here.
"""

from __future__ import annotations

import asyncio
import contextlib

from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ghostlink.cli.arguments import CLIOptions
from ghostlink.cli.commands.base import (
    CommandRuntime,
    build_runtime,
    relay_config_from,
    resolve_relay_url,
)
from ghostlink.constants.net import MAX_GROUP_MEMBERS
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.base import ExitCode
from ghostlink.exceptions.groups import GroupValidationError
from ghostlink.groups.ids import is_valid_group_id, normalize_group_id
from ghostlink.groups.models import LocalGroupRecord, LocalGroupState
from ghostlink.groups.service import GroupMessagingService
from ghostlink.invites.expiration import parse_duration_seconds
from ghostlink.transport.relay.client import RelayClient
from ghostlink.transport.relay.endpoint import RelayEndpoint
from ghostlink.ui.group_chat import run_group_chat_session

_logger = get_logger("cli.group")


def run_group(options: CLIOptions) -> int:
    """Dispatch the group subcommands."""

    runtime = build_runtime(options)
    action = options.group_action or "list"
    if action == "list":
        return _run_list(runtime)
    if action == "info":
        return _run_info(runtime, options)
    if action == "create":
        return asyncio.run(_run_create(runtime, options))
    if action == "invite":
        return asyncio.run(_run_invite(runtime, options))
    if action == "join":
        return asyncio.run(_run_join(runtime, options))
    if action in ("leave", "remove", "dissolve", "sync", "host"):
        return asyncio.run(_run_membership(runtime, options, action))
    if action == "chat":
        return asyncio.run(_run_chat(runtime, options))
    raise ValueError(f"Unknown group action {action!r}")  # argparse guards


# ------------------------------------------------------------------ helpers


def _require_group_id(target: str | None) -> str:
    if not target:
        raise GroupValidationError(
            "This action needs a group id.",
            hint="ghostlink group <action> gl-group-XXXX-XXXX-XXXX",
        )
    cleaned = normalize_group_id(target)
    if not is_valid_group_id(cleaned):
        raise GroupValidationError(
            f"'{target}' is not a group id.",
            hint="Group ids look like gl-group-XXXX-XXXX-XXXX — see group list.",
        )
    return cleaned


def _require_relay(runtime: CommandRuntime, options: CLIOptions) -> str:
    relay_url = resolve_relay_url(runtime.settings, options)
    if relay_url is None:
        raise GroupValidationError(
            "A relay is required — groups are enforced by the relay authority.",
            hint="Pass --relay ws://… or set [relay] url in the config.",
        )
    return relay_url


def _connect(runtime: CommandRuntime, options: CLIOptions, *, name: str) -> RelayClient:
    endpoint = RelayEndpoint.from_url(_require_relay(runtime, options))
    return RelayClient(
        endpoint, client_name=name, config=relay_config_from(runtime.settings, options)
    )


def _resolve_expiry(runtime: CommandRuntime, options: CLIOptions) -> float:
    invites_cfg = runtime.settings.invites
    if options.expires is None:
        return float(invites_cfg.default_expiry_seconds)
    seconds = float(parse_duration_seconds(options.expires))
    if seconds < 1 or seconds > invites_cfg.max_expiry_seconds:
        raise GroupValidationError(
            f"--expires must be between 1 and {invites_cfg.max_expiry_seconds} seconds.",
            hint="Your configured invite lifetime cap applies to group invites too.",
        )
    return seconds


def _resolve_uses(options: CLIOptions) -> int:
    if options.uses is None:
        return 1
    if options.uses < 1 or options.uses > MAX_GROUP_MEMBERS - 1:
        raise GroupValidationError(
            f"--uses must be between 1 and {MAX_GROUP_MEMBERS - 1}.",
            hint="A group invite can admit at most the remaining free seats.",
        )
    return options.uses


def _state_style(record: LocalGroupRecord) -> str:
    if record.state is LocalGroupState.ACTIVE:
        return "gl.warning" if record.suspect else "gl.success"
    return "gl.muted"


def _role_of(record: LocalGroupRecord) -> str:
    if record.my_fingerprint == record.owner_fingerprint:
        return "owner"
    return record.my_role.value


# -------------------------------------------------------------------- list


def _run_list(runtime: CommandRuntime) -> int:
    records = runtime.groups.list_all()
    console = runtime.console
    if not records:
        console.print(
            Panel(
                Text(
                    "No groups yet.\n\n"
                    "Create one:  ghostlink group create --name NightWatch\n"
                    "Join one:    ghostlink group join gl://join/…",
                    style="gl.muted",
                ),
                title="[gl.title]Groups[/]",
                border_style="gl.border",
                expand=False,
                width=min(console.width, 64),
            )
        )
        return int(ExitCode.OK)
    table = Table(title="Your groups", header_style="gl.accent", expand=False)
    table.add_column("Group", style="gl.text", no_wrap=True)
    table.add_column("Name", style="gl.text")
    table.add_column("State", style="gl.text", no_wrap=True)
    table.add_column("Epoch", justify="right", style="gl.text")
    table.add_column("Members", justify="right", style="gl.text")
    table.add_column("Role", style="gl.text", no_wrap=True)
    for record in records:
        state = record.state.value + (" (suspect)" if record.suspect else "")
        table.add_row(
            record.group_id,
            record.name,
            state,
            str(record.epoch),
            f"{record.member_count()}/{MAX_GROUP_MEMBERS}",
            _role_of(record),
        )
    console.print(table)
    return int(ExitCode.OK)


# -------------------------------------------------------------------- info


def _run_info(runtime: CommandRuntime, options: CLIOptions) -> int:
    group_id = _require_group_id(options.group_target)
    record = runtime.groups.require(group_id)
    console = runtime.console
    lines = Text()
    seen_at = (
        (record.epoch_leap_at or record.updated_at)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S %Z")
        .strip()
    )
    lines.append(f"{record.name}\n", style="gl.title")
    lines.append(f"id:      {record.group_id}\n", style="gl.text")
    lines.append(f"state:   {record.state.value}", style=_state_style(record))
    if record.suspect:
        lines.append("  (suspect — re-sync before mutating)", style="gl.warning")
    lines.append("\n", style="gl.text")
    lines.append(f"epoch:   {record.epoch}\n", style="gl.text")
    lines.append(f"members: {record.member_count()}/{MAX_GROUP_MEMBERS}\n", style="gl.text")
    lines.append(f"owner:   {record.owner_fingerprint}\n", style="gl.text")
    lines.append(f"you:     {record.my_fingerprint} ({_role_of(record)})\n", style="gl.text")
    if record.relay_url:
        lines.append(f"relay:   {record.relay_url}\n", style="gl.muted")
    lines.append(f"updated: {seen_at}\n", style="gl.muted")
    console.print(
        Panel(
            lines,
            title="[gl.title]Group[/]",
            border_style="gl.border",
            expand=False,
            width=min(console.width, 72),
        )
    )
    roster = Table(title="Roster", header_style="gl.accent", expand=False)
    roster.add_column("Fingerprint", style="gl.text", no_wrap=True)
    roster.add_column("Handle", style="gl.text", no_wrap=True)
    roster.add_column("Display name", style="gl.text")
    roster.add_column("Role", style="gl.text", no_wrap=True)
    roster.add_column("Joined@", justify="right", style="gl.text")
    for member in sorted(record.members.values(), key=lambda m: m.joined_epoch):
        roster.add_row(
            member.fingerprint,
            member.handle,
            member.display_name,
            member.role.value,
            str(member.joined_epoch),
        )
    console.print(roster)
    return int(ExitCode.OK)


# ------------------------------------------------------------------ create


async def _run_create(runtime: CommandRuntime, options: CLIOptions) -> int:
    name = (options.group_name or "").strip()
    if not name:
        raise GroupValidationError(
            "Group creation needs a name.",
            hint="ghostlink group create --name NightWatch",
        )
    client = _connect(runtime, options, name="ghostlink-group-owner")
    try:
        await client.connect()
        record = await runtime.groups.create_group(
            client, name, display_name=options.chat_name or ""
        )
    finally:
        await client.aclose()
    runtime.console.print(
        Panel(
            Text.assemble(
                (f"{record.name}\n\n", "gl.title"),
                (f"id:      {record.group_id}\n", "gl.highlight"),
                ("epoch:   1\nmembers: 1/8 (you, owner)\n\n", "gl.text"),
                ("Invite:  ghostlink group invite ", "gl.muted"),
                (record.group_id, "gl.text"),
                ("\nAdmit:   ghostlink group host ", "gl.muted"),
                (record.group_id, "gl.text"),
                ("\n         (the owner must host to countersign joins)", "gl.muted"),
            ),
            title="[gl.success]✓ Group created[/]",
            border_style="gl.success",
            expand=False,
            width=min(runtime.console.width, 72),
        )
    )
    _logger.info("group created via cli — %s", record.group_id)
    return int(ExitCode.OK)


# ------------------------------------------------------------------ invite


async def _run_invite(runtime: CommandRuntime, options: CLIOptions) -> int:
    group_id = _require_group_id(options.group_target)
    runtime.groups.require(group_id)  # local existence before any network I/O
    ttl_seconds = _resolve_expiry(runtime, options)
    uses = _resolve_uses(options)
    client = _connect(runtime, options, name="ghostlink-group-owner")
    try:
        await client.connect()
        await runtime.groups.sync_group(client, group_id)  # fresh headroom check
        invite, link = await runtime.groups.mint_group_invite(
            client,
            runtime.invites,
            group_id,
            ttl_seconds=ttl_seconds,
            max_redemptions=uses,
        )
    finally:
        await client.aclose()
    runtime.console.print(
        Panel(
            Text.assemble(
                ("Share this link — it admits ", "gl.text"),
                (
                    f"{invite.max_redemptions} member(s)\n\n",
                    "gl.highlight" if invite.max_redemptions > 1 else "gl.text",
                ),
                (link, "gl.highlight"),
                (f"\n\nexpires: {invite.expires_at.astimezone():%Y-%m-%d %H:%M:%S %Z}", "gl.muted"),
                (
                    "\nhint:    run 'ghostlink group host " + group_id + "' to countersign joins",
                    "gl.muted",
                ),
            ),
            title="[gl.success]✓ Group invite created[/]",
            border_style="gl.success",
            expand=False,
            width=min(runtime.console.width, 72),
        )
    )
    _logger.info(
        "group invite minted via cli — %s (link shown once, never logged)", invite.invite_id
    )
    return int(ExitCode.OK)


# -------------------------------------------------------------------- join


async def _run_join(runtime: CommandRuntime, options: CLIOptions) -> int:
    link = (options.group_target or "").strip()
    if not link:
        raise GroupValidationError(
            "Joining needs an invite link.",
            hint="ghostlink group join gl://join/XXXX…",
        )
    client = _connect(runtime, options, name="ghostlink-group-member")
    runtime.console.print(
        Text("Redeeming invite; the owner must countersign your admission…", style="gl.muted")
    )
    try:
        await client.connect()
        record = await runtime.groups.join_group(client, link, display_name=options.chat_name or "")
    finally:
        await client.aclose()
    runtime.console.print(
        Panel(
            Text.assemble(
                (f"{record.name}\n\n", "gl.title"),
                (f"id:      {record.group_id}\n", "gl.highlight"),
                (f"epoch:   {record.epoch}\n", "gl.text"),
                (f"members: {record.member_count()}/{MAX_GROUP_MEMBERS}\n", "gl.text"),
                (f"owner:   {record.owner_fingerprint}", "gl.text"),
            ),
            title="[gl.success]✓ Joined group[/]",
            border_style="gl.success",
            expand=False,
            width=min(runtime.console.width, 72),
        )
    )
    _logger.info("joined group via cli — %s", record.group_id)
    return int(ExitCode.OK)


# ------------------------------------------------------ membership mutations


async def _run_membership(runtime: CommandRuntime, options: CLIOptions, action: str) -> int:
    group_id = _require_group_id(options.group_target)
    client = _connect(runtime, options, name="ghostlink-group-member")
    try:
        await client.connect()
        if action == "leave":
            record = await runtime.groups.leave_group(client, group_id)
            runtime.console.print(
                Text(
                    f"✓ Left {record.name} ({group_id}) at epoch {record.epoch}.",
                    style="gl.success",
                )
            )
            return int(ExitCode.OK)
        if action == "remove":
            subject = (options.group_subject or "").strip()
            if not subject:
                raise GroupValidationError(
                    "remove needs the member's fingerprint.",
                    hint="ghostlink group remove gl-group-… GLFP-XXXX-XXXX-XXXX",
                )
            record = await runtime.groups.remove_member(client, group_id, subject)
            runtime.console.print(
                Text(f"✓ Removed {subject.upper()} at epoch {record.epoch}.", style="gl.success")
            )
            return int(ExitCode.OK)
        if action == "dissolve":
            record = await runtime.groups.dissolve_group(client, group_id)
            runtime.console.print(
                Text(
                    f"✓ Dissolved {record.name} ({group_id}) at epoch {record.epoch}.",
                    style="gl.success",
                )
            )
            return int(ExitCode.OK)
        if action == "sync":
            record = await runtime.groups.sync_group(
                client, group_id, display_name=options.chat_name or ""
            )
            runtime.console.print(
                Text(
                    f"✓ {record.name} synced — epoch {record.epoch}, "
                    f"{record.member_count()}/{MAX_GROUP_MEMBERS} members.",
                    style="gl.success",
                )
            )
            return int(ExitCode.OK)
        return await _run_host(runtime, client, group_id)
    finally:
        await client.aclose()


async def _run_host(runtime: CommandRuntime, client: RelayClient, group_id: str) -> int:
    """Owner listener: rebind, then countersign admissions until Ctrl+C."""

    manager = runtime.groups
    record = await manager.sync_group(client, group_id)
    if not record.is_owner():
        raise GroupValidationError(
            "Only the owner hosts a group.",
            hint="Hosting countersigns admissions — an owner duty.",
        )
    console = runtime.console

    def _notice(notice_group: str, notice: str) -> None:
        if notice_group == group_id:
            console.print(Text(f"• {notice}", style="gl.info"))

    manager.add_notice_listener(_notice)
    console.print(
        Panel(
            Text.assemble(
                (f"{record.name}  ", "gl.title"),
                (f"({group_id})\n", "gl.highlight"),
                (
                    f"epoch {record.epoch} — {record.member_count()}/"
                    f"{MAX_GROUP_MEMBERS} members\n\n",
                    "gl.text",
                ),
                (
                    "Countersigning admissions while this runs. Ctrl+C to stop.",
                    "gl.muted",
                ),
            ),
            title="[gl.success]✓ Hosting group[/]",
            border_style="gl.success",
            expand=False,
            width=min(console.width, 72),
        )
    )
    _logger.info("hosting group %s as owner", group_id)
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.Event().wait()  # until Ctrl+C cancels us
    return int(ExitCode.OK)


# ------------------------------------------------------------------- chat


async def _run_chat(runtime: CommandRuntime, options: CLIOptions) -> int:
    """Open the end-to-end group conversation (Phase 6C).

    Attestation + authoritative re-sync happen first (§27.3); the chat
    itself lives in the UI layer while every security decision stays in
    the messaging service.
    """

    group_id = _require_group_id(options.group_target)
    record0 = runtime.groups.require(group_id)
    record0.require_active()
    service = GroupMessagingService(
        runtime.groups,
        runtime.identities,
        read_receipts=bool(runtime.settings.chat.read_receipts),
    )
    client = _connect(runtime, options, name="ghostlink-group-member")
    try:
        await client.connect()
        await service.attach(client)
        record = await service.sync(group_id, display_name=options.chat_name or "")
        _logger.info(
            "group chat opened — %s at epoch %d (%d members)",
            group_id,
            record.epoch,
            record.member_count(),
        )
        return await run_group_chat_session(
            runtime.console,
            settings=runtime.settings,
            state_dir=runtime.config.state_dir,
            service=service,
            group_id=group_id,
            invite_manager=runtime.invites,
        )
    finally:
        with contextlib.suppress(Exception):
            await service.close()
        await client.aclose()


__all__ = ["run_group"]
