"""The interactive terminal group-chat surface (Phase 6C).

:class:`GroupChatApp` renders one secure group conversation::
    ╔════════════════════════════════════════════════╗
    ║           GhostLink Secure Group               ║
    ║  Status: Connected   Encryption: E2E pairwise  ║
    ║  Epoch: 3            Members: 3/8              ║
    ╚════════════════════════════════════════════════╝

    [22:10]
    Ravi:
      Hello from the mesh

    > your message here

Design notes:

* it mirrors :class:`ghostlink.ui.chat.ChatApp` (same input sources,
  same rendering vocabulary, same never-crash discipline) but every
  security decision lives in :class:`GroupMessagingService` — this layer
  only renders and forwards keystrokes;
* delivery is fanout-honest: per-recipient states render as
  ``delivered k/m, failed n, offline q`` — never a blanket "delivered"
  (§18.3);
* membership changes, offline members, suspect links and drain events
  surface as system notices; nothing key-shaped or plaintext-internal
  is ever shown beyond the decrypted messages themselves.
"""

from __future__ import annotations

import asyncio
import getpass
import sys
import textwrap
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ghostlink.core.logging import get_logger
from ghostlink.exceptions.base import ExitCode
from ghostlink.exceptions.groups import GroupError
from ghostlink.exceptions.messaging import HistoryError, HistoryPassphraseError
from ghostlink.groups.frames import DeliveryState
from ghostlink.groups.models import LocalGroupRecord, LocalGroupState
from ghostlink.groups.service import (
    GroupChatEvent,
    GroupChatEventKind,
    GroupMessagingService,
)
from ghostlink.invites.expiration import parse_duration_seconds
from ghostlink.messaging.history import (
    BaseHistory,
    HistoryMode,
    export_file_name,
    format_timestamp,
    open_history,
)
from ghostlink.models.settings import AppSettings
from ghostlink.transport.relay.client import RelayClient
from ghostlink.ui.chat import InputSource, TerminalInputSource
from ghostlink.ui.console import ConsoleManager

_logger = get_logger("ui.group_chat")

_PROMPT = "> "
_LATENCY_TIMEOUT_SECONDS = 1.5
_DEFAULT_INVITE_TTL_SECONDS = 3600.0


class GroupChatApp:
    """Drives one group conversation inside the terminal until exit."""

    def __init__(
        self,
        console: ConsoleManager,
        service: GroupMessagingService,
        *,
        group_id: str,
        history: BaseHistory,
        input_source: InputSource | None = None,
        timestamp_format: str = "24h",
        notification_style: str = "banner",
        message_wrapping: bool = True,
        invite_ttl_seconds: float = _DEFAULT_INVITE_TTL_SECONDS,
        invite_manager: Any = None,
    ) -> None:
        self._console = console
        self._service = service
        self._group_id = group_id
        self._history = history
        self._input = input_source or TerminalInputSource(_PROMPT)
        self._timestamp_format = timestamp_format
        self._notification_style = notification_style
        self._wrap = message_wrapping
        self._invite_ttl_seconds = invite_ttl_seconds
        self._invites = invite_manager
        self._latency_ms: float | None = None
        self._running = False
        self._printed_delivery: dict[str, str] = {}

    # ---------------------------------------------------------------- helpers

    def _record(self) -> LocalGroupRecord:
        record = self._service._groups.get(self._group_id)
        if record is None:
            raise GroupError(
                "This group is not known locally.",
                hint="See ghostlink group list.",
            )
        return record

    def _client(self) -> RelayClient:
        return self._service._require_client()

    # ------------------------------------------------------------------- run

    async def run(self) -> int:
        """Render the banner and drive input until /quit, /leave, or EOF."""

        self._service.add_listener(self._on_event)
        record = self._record()
        await self._refresh_latency()
        self._print_banner(record)
        self._console.newline()
        self._running = True
        try:
            while self._running:
                line = await self._input.read_line()
                if line is None:  # EOF or Ctrl+C — leave gracefully
                    break
                await self._handle_line(line)
        finally:
            self._input.stop()
            self._service.remove_listener(self._on_event)
            self._console.newline()
            self._print_system(
                "Group session closed — pairwise keys wiped from memory. Stay private. 👻"
            )
        return int(ExitCode.OK)

    async def _refresh_latency(self) -> None:
        try:
            self._latency_ms = await asyncio.wait_for(
                self._client().ping(), timeout=_LATENCY_TIMEOUT_SECONDS
            )
        except Exception:
            self._latency_ms = None

    # ------------------------------------------------------------ input path

    async def _handle_line(self, line: str) -> None:
        stripped = line.strip()
        if not stripped:
            return
        if stripped.startswith("/"):
            await self._command(stripped)
            return
        await self._send(line)

    async def _send(self, text: str) -> None:
        try:
            ledger = await self._service.send(self._group_id, text)
        except Exception as exc:  # GhostLinkError family — surface, never crash
            self._print_error(str(getattr(exc, "message", exc)))
            return
        self._print_own_block(text, ledger.ts)
        if ledger.failed_count():
            self._print_delivery_line(ledger.status_line(), failed=True)
        elif ledger.delivered_count() == 0 and ledger.total() > 0:
            queued = ledger.counts().get(DeliveryState.QUEUED.value, 0)
            if queued:
                self._print_delivery_line(f"queued for {queued} offline member(s)")
        self._printed_delivery[ledger.message_id] = ledger.status_line()

    # --------------------------------------------------------------- events

    def _on_event(self, event: GroupChatEvent) -> None:
        if event.group_id != self._group_id:
            return
        if event.kind is GroupChatEventKind.MESSAGE and event.frame is not None:
            frame = event.frame
            assert frame is not None  # narrowing for mypy
            author = self._service._name_of(self._group_id, frame.sender)
            self._print_message_block(author, frame.text, frame.ts)
            if event.gap:
                self._print_system(
                    "⚠ message arrived with a sequence gap — earlier messages from "
                    "this member may have been missed or reordered.",
                    style="gl.warning",
                    loud=True,
                )
        elif event.kind is GroupChatEventKind.DELIVERY and event.ledger is not None:
            ledger = event.ledger
            line = ledger.status_line()
            if self._printed_delivery.get(ledger.message_id) == line:
                return
            self._printed_delivery[ledger.message_id] = line
            if ledger.failed_count() or "offline" in line:
                self._print_delivery_line(line, failed=ledger.failed_count() > 0)
            elif ledger.delivered_count() == ledger.total() and ledger.total() > 0:
                read = any(state is DeliveryState.READ for state in ledger.recipients.values())
                tail = line.split(" ", 1)[1] if " " in line else line
                verb = "read by" if read else "delivered"
                self._print_delivery_line(f"✓✓ {verb} {tail}", good=True)
        elif event.kind is GroupChatEventKind.NOTICE:
            self._print_system(f"⚠ {event.detail}", style="gl.warning", loud=True)
        elif event.kind is GroupChatEventKind.MEMBERSHIP:
            self._print_system(event.detail, style="gl.info", loud=True)

    def _schedule(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task: asyncio.Task[Any] = loop.create_task(coroutine)
        task.add_done_callback(self._observe_task)

    @staticmethod
    def _observe_task(task: asyncio.Task[Any]) -> None:
        if task.cancelled():
            return
        failure = task.exception()
        if failure is not None:
            _logger.debug("group chat UI background task failed: %s", failure)

    # -------------------------------------------------------------- rendering

    def _print_banner(self, record: LocalGroupRecord) -> None:
        latency = "—" if self._latency_ms is None else f"{self._latency_ms:.0f} ms"
        state = "connected" if record.state is LocalGroupState.ACTIVE else record.state.value
        link_states = self._service.link_states(self._group_id)
        linked = sum(1 for status in link_states.values() if status == "linked")
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="gl.muted", no_wrap=True)
        grid.add_column(style="gl.text", no_wrap=True)
        grid.add_column(style="gl.muted", no_wrap=True)
        grid.add_column(style="gl.text", no_wrap=True)
        grid.add_row("Status:", state, "Latency:", latency)
        encryption = (
            "E2E sender-key" if record.crypto_suite == "senderkey-v1" else "E2E pairwise mesh"
        )
        grid.add_row("Encryption:", encryption, "Epoch:", str(record.epoch))
        grid.add_row(
            "Group:",
            f"{record.name} ({record.group_id})",
            "Members:",
            f"{record.member_count()}/8",
        )
        grid.add_row(
            "Mesh links:", f"{linked}/{len(link_states)} live", "Role:", record.my_role.value
        )
        panel = Panel(
            grid,
            title="[gl.title]⚡ GhostLink Secure Group[/]",
            subtitle="[gl.muted]/help lists commands · /quit leaves the chat[/]",
            box=box.DOUBLE,
            border_style="gl.accent",
            padding=(0, 2),
            expand=True,
        )
        self._console.print(panel)
        self._console.print(
            Text(
                "  🔒 Every message is encrypted separately for each recipient; the relay "
                "routes opaque ciphertext only.",
                style="gl.muted",
            )
        )

    def _print_message_block(self, author: str, text: str, ts: float) -> None:
        stamp = format_timestamp(ts, self._timestamp_format)
        lines = [Text(stamp, style="gl.muted"), Text(f"{author}:", style="gl.accent")]
        if self._wrap:
            width = max(20, self._console.width - 4)
            for paragraph in text.split("\n"):
                wrapped = textwrap.wrap(
                    paragraph, width=width, break_long_words=True, break_on_hyphens=False
                ) or [""]
                lines.extend(Text(f"  {chunk}", style="gl.text") for chunk in wrapped)
        else:
            lines.extend(
                Text(f"  {paragraph}", style="gl.text", overflow="fold")
                for paragraph in text.split("\n")
            )
        self._console.print(Group(*lines))

    def _print_own_block(self, text: str, ts: float) -> None:
        stamp = format_timestamp(ts, self._timestamp_format)
        lines = [Text(stamp, style="gl.muted"), Text("You:", style="gl.success")]
        width = max(20, self._console.width - 4)
        for paragraph in text.split("\n"):
            wrapped = textwrap.wrap(
                paragraph, width=width, break_long_words=True, break_on_hyphens=False
            ) or [""]
            lines.extend(Text(f"  {chunk}", style="gl.text") for chunk in wrapped)
        self._console.print(Group(*lines))

    def _print_delivery_line(self, line: str, *, failed: bool = False, good: bool = False) -> None:
        if self._notification_style == "muted":
            return
        if failed:
            self._console.print(Text(f"  ✗ {line}", style="gl.error"))
        elif good:
            self._console.print(Text(f"  {line}", style="gl.success"))
        else:
            self._console.print(Text(f"  ✓ {line}", style="gl.muted"))

    def _print_system(self, text: str, *, style: str = "gl.muted", loud: bool = False) -> None:
        if self._notification_style == "muted" and not loud:
            return
        self._console.print(Text(f"── {text}", style=style))

    def _print_error(self, text: str) -> None:
        self._console.print(Text(f"✗ {text}", style="gl.error"))

    # --------------------------------------------------------------- commands

    async def _command(self, line: str) -> None:
        command, _, argument = line.partition(" ")
        command = command.lower()
        argument = argument.strip()
        if command == "/help":
            self._show_help()
        elif command == "/info":
            await self._show_info()
        elif command == "/members":
            self._show_members()
        elif command == "/fingerprint":
            self._show_fingerprints()
        elif command == "/delivery":
            self._show_delivery()
        elif command == "/invite":
            await self._cmd_invite(argument)
        elif command == "/leave":
            await self._cmd_leave()
        elif command == "/security":
            self._show_security()
        elif command == "/history":
            self._show_history()
        elif command == "/export":
            self._export_history(argument)
        elif command in ("/quit", "/exit"):
            self._running = False
        else:
            self._print_error(f"Unknown command '{command}'. Type /help.")

    def _show_help(self) -> None:
        table = Table(box=None, show_header=False, show_edge=False, padding=(0, 2))
        table.add_column(style="gl.accent", no_wrap=True)
        table.add_column(style="gl.text")
        rows = [
            ("/help", "show this command list"),
            ("/info", "group, epoch, encryption and delivery statistics"),
            ("/members", "roster with per-member pairwise-link state"),
            ("/fingerprint", "member verification fingerprints (compare out-of-band)"),
            ("/delivery", "recent per-recipient delivery states"),
            ("/invite", "mint a group invite link (owner only)"),
            ("/leave", "leave this group permanently"),
            ("/security", "encryption mode, sender-key and delivery state"),
            ("/history", "show messages retained this session"),
            ("/export [file]", "write retained history to a plaintext file"),
            ("/quit", "close the chat (you stay a member)"),
        ]
        for command, description in rows:
            table.add_row(Text(command, style="gl.accent"), Text(description, style="gl.text"))
        self._console.print(
            Panel(
                table,
                title="[gl.title]Commands — local to your terminal only[/]",
                box=box.ROUNDED,
                border_style="gl.border",
                padding=(0, 1),
            )
        )

    async def _show_info(self) -> None:
        record = self._record()
        await self._refresh_latency()
        latency = "—" if self._latency_ms is None else f"{self._latency_ms:.0f} ms"
        ledgers = self._service.ledgers_for(self._group_id)
        delivered = sum(ledger.delivered_count() for ledger in ledgers)
        failed = sum(ledger.failed_count() for ledger in ledgers)
        pending = sum(len(ledger.pending_recipients()) for ledger in ledgers)
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="gl.muted", no_wrap=True)
        grid.add_column(style="gl.text")
        grid.add_row("Group", f"{record.name} ({record.group_id})")
        grid.add_row("State", record.state.value + (" (suspect)" if record.suspect else ""))
        grid.add_row("Epoch", str(record.epoch))
        grid.add_row("Members", f"{record.member_count()}/8")
        grid.add_row("Encryption", "E2E pairwise mesh — per-recipient ChaCha20-Poly1305")
        grid.add_row("Latency", latency)
        grid.add_row(
            "Delivery",
            f"{len(ledgers)} tracked · delivered {delivered} · pending {pending} · failed {failed}",
        )
        history = f"{len(self._history)} retained" if self._history.enabled else "off"
        grid.add_row("History", history)
        grid.add_row(
            "Relay",
            "routes opaque ciphertext; cannot read messages or keys; can observe "
            "IP addresses, timing, traffic volume; can refuse or delay service.",
        )
        self._console.print(
            Panel(
                grid,
                title="[gl.title]Group session info[/]",
                box=box.DOUBLE,
                border_style="gl.accent",
                padding=(0, 2),
            )
        )

    def _show_security(self) -> None:
        """Security-relevant metadata (never any key material)."""
        record = self._record()
        suite = record.crypto_suite
        if suite == "senderkey-v1":
            mode = "E2E sender-key (O(1) per message)"
            receiver_keys = self._service._sk.incoming_count()
            detail = (
                "Each sender broadcasts one ciphertext per message; the relay "
                "routes it opaque. Sender keys are epoch-scoped and rotate on "
                "every roster change; a removed member loses the new epoch's "
                "keys, a joiner gains no past epoch."
            )
            key_line = f"{receiver_keys} incoming chain(s) held in memory"
        else:
            mode = "E2E pairwise mesh (per-recipient seal)"
            receiver_keys = 0
            detail = (
                "Each message is sealed separately per recipient over an "
                "identity-bound pairwise link; the relay routes opaque "
                "ciphertext only."
            )
            key_line = "pairwise session keys only (no sender keys)"
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="gl.muted", no_wrap=True)
        grid.add_column(style="gl.text")
        grid.add_row("Group", f"{record.name} ({record.group_id})")
        grid.add_row("Epoch", str(record.epoch))
        grid.add_row("Encryption", mode)
        grid.add_row("Crypto suite", suite)
        grid.add_row("Members", f"{record.member_count()}/8")
        grid.add_row("Key state", key_line)
        self._console.print(
            Panel(
                grid,
                title="[gl.title]Security status — no secrets shown[/]",
                box=box.DOUBLE,
                border_style="gl.accent",
                padding=(0, 2),
            )
        )
        self._print_system(detail, style="gl.muted")

    def _show_members(self) -> None:
        record = self._record()
        link_states = self._service.link_states(self._group_id)
        table = Table(title="Roster", header_style="gl.accent", expand=False)
        table.add_column("Display name", style="gl.text")
        table.add_column("Role", style="gl.text", no_wrap=True)
        table.add_column("Fingerprint", style="gl.text", no_wrap=True)
        table.add_column("Link", style="gl.text", no_wrap=True)
        for member in sorted(record.members.values(), key=lambda m: m.joined_epoch):
            marker = " (you)" if member.fingerprint == record.my_fingerprint else ""
            link = (
                "self"
                if member.fingerprint == record.my_fingerprint
                else link_states.get(member.fingerprint, "idle")
            )
            table.add_row(
                member.display_name + marker,
                member.role.value,
                member.fingerprint,
                link,
            )
        self._console.print(table)

    def _show_fingerprints(self) -> None:
        record = self._record()
        table = Table(
            title="Member fingerprints — compare out-of-band before trusting",
            header_style="gl.accent",
            expand=False,
        )
        table.add_column("Member", style="gl.text")
        table.add_column("Fingerprint", style="gl.highlight", no_wrap=True)
        for member in sorted(record.members.values(), key=lambda member: member.joined_epoch):
            marker = " (you)" if member.fingerprint == record.my_fingerprint else ""
            table.add_row(member.display_name + marker, member.fingerprint)
        self._console.print(table)
        self._print_system(
            "Messages are provably from the holder of each listed identity key; "
            "display names are decoration only."
        )

    def _show_delivery(self) -> None:
        ledgers = self._service.ledgers_for(self._group_id)
        if not ledgers:
            self._print_system("No group messages tracked yet.")
            return
        table = Table(title="Recent delivery states", header_style="gl.accent", expand=False)
        table.add_column("Message", style="gl.muted", no_wrap=True)
        table.add_column("Status", style="gl.text")
        table.add_column("Pending", style="gl.text")
        table.add_column("Failed", style="gl.text")
        for ledger in ledgers[-10:]:
            table.add_row(
                ledger.message_id,
                ledger.status_line(),
                ", ".join(
                    self._service._name_of(self._group_id, fp) for fp in ledger.pending_recipients()
                )
                or "—",
                ", ".join(
                    self._service._name_of(self._group_id, fp) for fp in ledger.failed_recipients()
                )
                or "—",
            )
        self._console.print(table)

    async def _cmd_invite(self, argument: str) -> None:
        record = self._record()
        if not record.is_owner():
            self._print_error("Only the owner may mint invites for this group.")
            return
        if self._invites is None:
            self._print_error("Invite management is unavailable in this session.")
            return
        ttl = self._invite_ttl_seconds
        if argument:
            try:
                ttl = float(parse_duration_seconds(argument))
            except Exception as exc:
                self._print_error(str(getattr(exc, "message", exc)))
                return
        try:
            invite, link = await self._service._groups.mint_group_invite(
                self._client(),
                self._invites,
                self._group_id,
                ttl_seconds=ttl,
                max_redemptions=1,
            )
        except Exception as exc:
            self._print_error(str(getattr(exc, "message", exc)))
            return
        self._print_system(f"Invite {invite.invite_id} minted (admit 1 member):", loud=True)
        self._console.print(Text(f"  {link}", style="gl.highlight"))
        self._print_system("Share it privately — the link admits whoever redeems it first.")

    async def _cmd_leave(self) -> None:
        record = self._record()
        if record.is_owner():
            self._print_error(
                "The owner cannot leave their own group. Use ghostlink group dissolve "
                "from the CLI to end it for everyone."
            )
            return
        groups = self._service._groups
        try:
            await groups.leave_group(self._client(), self._group_id)
        except Exception as exc:
            self._print_error(str(getattr(exc, "message", exc)))
            return
        self._print_system(f"You left {record.name}.", loud=True)
        self._running = False

    def _show_history(self) -> None:
        if not self._history.enabled:
            self._print_system(
                "History is disabled — set chat.history_mode to 'session' or "
                "'encrypted' to retain messages."
            )
            return
        text = self._history.export_text(timestamp_format=self._timestamp_format)
        if not text:
            self._print_system("No messages retained yet.")
            return
        self._console.print(text)

    def _export_history(self, argument: str) -> None:
        if not self._history.enabled:
            self._print_system("History is disabled — nothing to export.")
            return
        target = Path(argument).expanduser() if argument else Path.cwd() / export_file_name()
        try:
            path = self._history.write_export(target, timestamp_format=self._timestamp_format)
        except Exception as exc:
            self._print_error(str(getattr(exc, "message", exc)))
            return
        self._print_system(
            f"Exported to {path} — plaintext file, handle with care.",
            style="gl.warning",
            loud=True,
        )


# ------------------------------------------------------------------ runner


async def _open_group_history(settings: AppSettings, state_dir: Path) -> BaseHistory:
    """Build the history backend, prompting for a passphrase when needed."""

    mode = HistoryMode(settings.chat.history_mode)
    if mode is not HistoryMode.ENCRYPTED:
        return open_history(mode, state_dir=state_dir)
    if not sys.stdin.isatty():
        raise HistoryError(
            "Encrypted history needs an interactive terminal for the passphrase.",
            hint="Run from a real terminal, or set chat.history_mode to 'session' or 'disabled'.",
        )
    from ghostlink.messaging.history import HISTORY_FILE_NAME

    path = state_dir / "history" / HISTORY_FILE_NAME
    if path.exists():
        passphrase = await asyncio.to_thread(getpass.getpass, "History passphrase: ")
    else:
        first = await asyncio.to_thread(getpass.getpass, "Choose a history passphrase: ")
        if not first:
            raise HistoryPassphraseError(
                "Encrypted history needs a passphrase.",
                hint="Set chat.history_mode to 'session' or 'disabled' to skip passphrases.",
            )
        second = await asyncio.to_thread(getpass.getpass, "Confirm passphrase: ")
        if first != second:
            raise HistoryPassphraseError(
                "The passphrases did not match.",
                hint="Run the command again and repeat the passphrase carefully.",
            )
        passphrase = first
    return open_history(mode, state_dir=state_dir, passphrase=passphrase)


async def run_group_chat_session(
    console: ConsoleManager,
    *,
    settings: AppSettings,
    state_dir: Path,
    service: GroupMessagingService,
    group_id: str,
    input_source: InputSource | None = None,
    invite_manager: Any = None,
) -> int:
    """Open history, wire the app, and run the group chat surface.

    The caller owns the (already attached + synced) service and its relay
    client; this runner owns only rendering and user input.
    """

    history = await _open_group_history(settings, state_dir)
    ttl = (
        float(settings.invites.default_expiry_seconds)
        if invite_manager is not None
        else _DEFAULT_INVITE_TTL_SECONDS
    )
    app = GroupChatApp(
        console,
        service,
        group_id=group_id,
        history=history,
        input_source=input_source,
        timestamp_format=settings.chat.timestamp_format,
        notification_style=settings.chat.notification_style,
        message_wrapping=settings.chat.message_wrapping,
        invite_ttl_seconds=ttl,
        invite_manager=invite_manager,
    )
    return await app.run()


__all__ = ["GroupChatApp", "run_group_chat_session"]
