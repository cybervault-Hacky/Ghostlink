"""The interactive terminal chat surface (Phase 3).

:class:`ChatApp` turns a live :class:`~ghostlink.messaging.session.chat.ChatSession`
into the premium full-terminal conversation the brief sketches::

    ╔════════════════════════════════════════════════╗
    ║            GhostLink Secure Session            ║
    ║   Status: Connected   Encryption: Active       ║
    ║   Latency: 42 ms      Room: gl-room-…          ║
    ╚════════════════════════════════════════════════╝

    [22:10]
    Ravi:
      Hello from Termux

    > your message here

Design notes:

* input runs on a helper thread (real ``input()`` → arrow-key history via
  readline where supported); events render as they arrive from the async
  loop — no polling artifacts
* message blocks wrap to the live console width, so narrow Termux windows
  stay readable; wrapping can be disabled in settings
* every peer event (typing, join/leave, reconnects) surfaces as a quiet
  status line; encryption problems surface as loud ones
* Phase 4 file transfers render as offer panels, throttled progress lines
  and saved-to confirmations; ``/send`` plus the transfer command family
  drive them, and a bare ``Y``/``N`` answers the latest pending offer
* local ``/`` commands never touch the wire
"""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import queue
import sys
import textwrap
import threading
from collections.abc import Coroutine, Sequence
from pathlib import Path
from typing import Any, Protocol

from rich import box
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ghostlink.core.logging import get_logger
from ghostlink.exceptions.base import ExitCode
from ghostlink.exceptions.invites import InviteError
from ghostlink.exceptions.messaging import HistoryError, HistoryPassphraseError
from ghostlink.exceptions.transport import TransportError
from ghostlink.identity.lifecycle import IdentityManager
from ghostlink.invites.formatter import (
    invite_targets_hint,
    invites_table,
    resolve_invite_id_argument,
    verification_panel,
)
from ghostlink.invites.lifecycle import SecureInviteManager
from ghostlink.messaging.history import (
    BaseHistory,
    HistoryMode,
    export_file_name,
    format_timestamp,
    open_history,
)
from ghostlink.messaging.models.message import Message, MessageStatus
from ghostlink.messaging.receipts import ReceiptTracker
from ghostlink.messaging.session.chat import (
    ChatEvent,
    ChatEventKind,
    ChatSession,
    ChatSessionConfig,
)
from ghostlink.models.settings import AppSettings
from ghostlink.transfer.manager import (
    TransferEvent,
    TransferEventKind,
    TransferLimits,
    TransferManager,
)
from ghostlink.transfer.models import TransferSnapshot, TransferState
from ghostlink.transfer.progress import SpeedMeter, progress_line, state_label, transfers_table
from ghostlink.transfer.storage import TransferStorage
from ghostlink.transport.relay.client import RelayClient, RelayClientConfig
from ghostlink.transport.relay.endpoint import RelayEndpoint
from ghostlink.ui.console import ConsoleManager
from ghostlink.utils.text import format_bytes

_logger = get_logger("ui.chat")

DEFAULT_CHAT_NAME: str = "Ghost"

_PROMPT = "> "
_CONTINUATION = "\\"
_INPUT_POLL_SECONDS = 0.05
_LATENCY_TIMEOUT_SECONDS = 1.5


# ------------------------------------------------------------------ input


class InputSource(Protocol):
    """Where chat input lines come from (terminal thread or test script)."""

    async def read_line(self) -> str | None:
        """The next raw input line; ``None`` on EOF / Ctrl+C / stop."""
        ...

    def stop(self) -> None:
        """Release any background resources."""
        ...


class TerminalInputSource:
    """Real keyboard input on a daemon thread with readline history.

    ``input()`` provides arrow-key line history and editing wherever
    readline is available (every supported Linux/Termux target). The thread
    is a daemon so a pending read never hangs interpreter shutdown."""

    def __init__(self, prompt: str = _PROMPT) -> None:
        self._prompt = prompt
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._stopped = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        try:  # arrow-key history where the platform supports it
            import readline

            readline.set_history_length(200)
        except ImportError:
            _logger.debug("readline unavailable — arrow history disabled")
        self._thread = threading.Thread(target=self._reader, name="ghostlink-input", daemon=True)
        self._thread.start()

    def _reader(self) -> None:
        while not self._stopped.is_set():
            try:
                line = input(self._prompt)
            except (EOFError, KeyboardInterrupt, OSError):
                # EOF, Ctrl+C, or a closed/captured stdin — all mean "done".
                self._queue.put(None)
                return
            self._queue.put(line)

    async def read_line(self) -> str | None:
        self.start()
        while True:
            try:
                return self._queue.get_nowait()
            except queue.Empty:
                if self._thread is not None and not self._thread.is_alive():
                    return None
                await asyncio.sleep(_INPUT_POLL_SECONDS)

    def stop(self) -> None:
        self._stopped.set()


class ScriptedInputSource:
    """Deterministic input for tests and demos.

    Queued lines are delivered in order; when the queue is empty the source
    waits for :meth:`push` (so tests can pace events) or :meth:`stop`. An
    optional per-line delay paces scripted conversations end-to-end."""

    def __init__(self, lines: Sequence[str | None], *, line_delay_seconds: float = 0.0) -> None:
        self._lines = list(lines)
        self._delay = line_delay_seconds
        self._stopped = False

    def push(self, line: str | None) -> None:
        self._lines.append(line)

    async def read_line(self) -> str | None:
        while not self._stopped and not self._lines:
            await asyncio.sleep(0.01)
        if self._stopped:
            return None
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return self._lines.pop(0)

    def stop(self) -> None:
        self._stopped = True
        self._lines.clear()


# -------------------------------------------------------------- chat surface


class ChatApp:
    """Drives one chat session inside the terminal until the user exits."""

    def __init__(
        self,
        console: ConsoleManager,
        session: ChatSession,
        *,
        history: BaseHistory,
        timestamp_format: str = "24h",
        notification_style: str = "banner",
        message_wrapping: bool = True,
        input_source: InputSource | None = None,
        transfer_manager: TransferManager | None = None,
        identity_manager: IdentityManager | None = None,
        invite_manager: SecureInviteManager | None = None,
        session_invite_id: str | None = None,
    ) -> None:
        self._console = console
        self._session = session
        self._history = history
        self._timestamp_format = timestamp_format
        self._notification_style = notification_style
        self._wrap = message_wrapping
        self._input = input_source or TerminalInputSource()
        self._transfers = transfer_manager
        self._identities = identity_manager
        self._invites = invite_manager
        self._session_invite_id = session_invite_id
        self._speed_meters: dict[str, SpeedMeter] = {}
        self._latency_ms: float | None = None
        self._buffer: list[str] = []
        self._running = False
        self._event_log: list[str] = []
        self._pinned_messages: list[str] = []

    # ------------------------------------------------------------------ run

    async def run(self) -> int:
        """Render the banner and drive the input loop until /exit or EOF."""

        session = self._session
        session.add_listener(self._on_event)
        if self._transfers is not None:
            self._transfers.add_listener(self._on_transfer_event)
        self._print_banner(status="Connecting…", encryption="Handshake in progress")
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
            session.remove_listener(self._on_event)
            if self._transfers is not None:
                self._transfers.remove_listener(self._on_transfer_event)
            self._console.newline()
            self._print_system("Session closed — keys wiped from memory. Stay private. 👻")
        return int(ExitCode.OK)

    # ------------------------------------------------------------ input path

    async def _handle_line(self, line: str) -> None:
        if line.endswith(_CONTINUATION) and not line.startswith("/"):
            self._buffer.append(line[: -len(_CONTINUATION)])
            return
        if self._buffer:
            self._buffer.append(line)
            text = "\n".join(self._buffer)
            self._buffer = []
            await self._send(text)
            return
        stripped = line.strip()
        if not stripped:
            return
        if stripped.startswith("/"):
            await self._command(stripped)
            return
        if self._maybe_answer_offer(stripped):
            return
        await self._send(line)

    async def _send(self, text: str) -> None:
        try:
            message = await self._session.send_message(text)
        except Exception as exc:  # GhostLinkError family — surface, never crash
            message_text = getattr(exc, "message", str(exc))
            self._print_error(str(message_text))
            return
        await self._session.notify_typing(keystroke=False)
        self._print_message_block(message, outgoing=True)

    # --------------------------------------------------------------- events

    def _on_event(self, event: ChatEvent) -> None:
        kind = event.kind
        if kind is ChatEventKind.MESSAGE and event.message is not None:
            self._print_message_block(event.message, outgoing=False)
            self._schedule(self._session.mark_read())
        elif kind is ChatEventKind.DELIVERY and event.message is not None:
            self._print_delivery(event.message)
        elif kind is ChatEventKind.SESSION:
            if "Active" in event.detail:
                self._schedule(self._announce_ready())
            else:
                self._print_system(event.detail)
        elif kind is ChatEventKind.TYPING:
            if event.detail:
                self._print_system(event.detail, style="gl.muted")
        elif kind is ChatEventKind.PEER:
            self._print_system(event.detail, style="gl.info")
        elif kind is ChatEventKind.CONNECTION:
            if "lost" in event.detail or "Disconnected" in event.detail:
                self._print_system(f"⚠ {event.detail}", style="gl.warning", loud=True)
            elif "closed" in event.detail:
                self._print_system(event.detail, style="gl.warning", loud=True)
            else:
                self._print_system(event.detail, style="gl.success")
        elif kind is ChatEventKind.NOTICE:
            severity_error = "integrity" in event.detail or "handshake" in event.detail.lower()
            self._print_system(
                f"⚠ {event.detail}" if severity_error else event.detail,
                style="gl.error" if severity_error else "gl.muted",
                loud=severity_error,
            )

    def _schedule(self, coroutine: Coroutine[Any, Any, Any]) -> None:
        """Fire-and-forget a small coroutine from a sync event callback."""

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
            _logger.debug("chat UI background task failed: %s", failure)

    # ------------------------------------------------- transfer events (Phase 4)

    def _maybe_answer_offer(self, line: str) -> bool:
        """A bare ``y``/``n`` answers the newest pending file offer."""

        if self._transfers is None:
            return False
        answer = line.strip().lower()
        if answer not in ("y", "yes", "n", "no"):
            return False
        pending = self._transfers.pending_incoming()
        if not pending:
            return False
        target = pending[0]
        if answer.startswith("y"):
            self._schedule(self._answer_offer(target.transfer_id, accept=True))
        else:
            self._schedule(self._answer_offer(target.transfer_id, accept=False))
        return True

    async def _answer_offer(self, transfer_id: str, *, accept: bool) -> None:
        assert self._transfers is not None
        try:
            if accept:
                await self._transfers.accept(transfer_id)
            else:
                await self._transfers.reject(transfer_id)
        except Exception as exc:  # raced expiry/cancel — surface, never crash
            self._print_error(str(getattr(exc, "message", exc)))

    def _on_transfer_event(self, event: TransferEvent) -> None:
        kind = event.kind
        snapshot = event.transfer
        if kind is TransferEventKind.OFFER and snapshot is not None:
            self._print_offer(snapshot)
        elif kind is TransferEventKind.PROGRESS and snapshot is not None:
            self._print_transfer_progress(snapshot)
        elif kind is TransferEventKind.STATE and snapshot is not None:
            self._print_transfer_state(snapshot, event.detail)
            if snapshot.is_terminal:
                self._speed_meters.pop(snapshot.transfer_id, None)
        elif kind is TransferEventKind.SAVED and snapshot is not None:
            self._speed_meters.pop(snapshot.transfer_id, None)
            self._print_system(f"✓ Saved to: {event.detail}", style="gl.success", loud=True)
        elif kind is TransferEventKind.NOTICE:
            self._print_system(f"⚠ {event.detail}", style="gl.warning", loud=True)

    def _print_offer(self, snapshot: TransferSnapshot) -> None:
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="gl.muted", no_wrap=True)
        grid.add_column(style="gl.text", overflow="fold")
        grid.add_row("File:", snapshot.filename)
        grid.add_row("Size:", format_bytes(snapshot.size_bytes))
        grid.add_row("From:", snapshot.peer)
        grid.add_row("ID:", snapshot.transfer_id)
        self._console.print(
            Panel(
                grid,
                title="[gl.title]📎 Incoming File[/]",
                box=box.ROUNDED,
                border_style="gl.accent",
                padding=(0, 1),
            )
        )
        self._print_system(
            "Accept transfer? [Y] Yes  [N] No"
            f"   (/accept {snapshot.transfer_id} · /reject {snapshot.transfer_id})",
            style="gl.info",
            loud=True,
        )

    def _print_transfer_progress(self, snapshot: TransferSnapshot) -> None:
        meter = self._speed_meters.setdefault(snapshot.transfer_id, SpeedMeter())
        meter.sample(snapshot.bytes_done)
        line = progress_line(snapshot, meter=meter, width=max(30, self._console.width - 6))
        self._console.print(Text(f"   {line}", style="gl.muted"))

    def _print_transfer_state(self, snapshot: TransferSnapshot, detail: str) -> None:
        state = snapshot.state
        summary = detail or state_label(state)
        if state is TransferState.COMPLETED:
            self._print_system(f"📎 {summary}", style="gl.success", loud=True)
        elif state in (TransferState.FAILED, TransferState.CANCELLED, TransferState.EXPIRED):
            self._print_system(f"📎 {snapshot.filename} — {summary}", style="gl.error", loud=True)
        elif state is TransferState.REJECTED:
            self._print_system(f"📎 {snapshot.filename} — {summary}", style="gl.warning", loud=True)
        else:
            self._print_system(f"📎 {snapshot.filename} — {summary}")

    async def _announce_ready(self) -> None:
        """Measure latency once the session is live, then show the banner."""

        client = self._session.relay_client
        try:
            self._latency_ms = await asyncio.wait_for(
                client.ping(), timeout=_LATENCY_TIMEOUT_SECONDS
            )
        except Exception:
            self._latency_ms = None
        self._print_banner(status="Connected", encryption="Active")
        fingerprint = self._session.fingerprint
        if fingerprint:
            self._console.print(
                Text(
                    f"  🔒 Safety code  {fingerprint}  — compare it out-of-band with "
                    f"{self._session.peer_name}.",
                    style="gl.muted",
                )
            )
        self._console.newline()

    # -------------------------------------------------------------- rendering

    def _print_banner(self, *, status: str, encryption: str) -> None:
        session = self._session
        latency = "—" if self._latency_ms is None else f"{self._latency_ms:.0f} ms"
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="gl.muted", no_wrap=True)
        grid.add_column(style="gl.text", no_wrap=True)
        grid.add_column(style="gl.muted", no_wrap=True)
        grid.add_column(style="gl.text", no_wrap=True)
        grid.add_row("Status:", status, "Latency:", latency)
        grid.add_row("Encryption:", encryption, "Room:", session.channel)
        grid.add_row("You:", session.display_name, "Peer:", session.peer_name)
        panel = Panel(
            grid,
            title="[gl.title]⚡ GhostLink Secure Session[/]",
            subtitle="[gl.muted]/help lists commands · /exit leaves[/]",
            box=box.DOUBLE,
            border_style="gl.accent",
            padding=(0, 2),
            expand=True,
        )
        self._console.print(panel)

    def _print_message_block(self, message: Message, *, outgoing: bool) -> None:
        stamp = format_timestamp(message.created_at, self._timestamp_format)
        name = "You" if outgoing else message.sender
        name_style = "gl.success" if outgoing else "gl.accent"
        lines = [Text(stamp, style="gl.muted"), Text(f"{name}:", style=name_style)]
        if self._wrap:
            width = max(20, self._console.width - 4)
            for paragraph in message.text.split("\n"):
                wrapped = textwrap.wrap(
                    paragraph, width=width, break_long_words=True, break_on_hyphens=False
                ) or [""]
                lines.extend(Text(f"  {chunk}", style="gl.text") for chunk in wrapped)
        else:
            lines.extend(
                Text(f"  {paragraph}", style="gl.text", overflow="fold")
                for paragraph in message.text.split("\n")
            )
        self._console.print(Group(*lines))

    def _print_delivery(self, message: Message) -> None:
        """Inline delivery feedback beside the user's own messages."""

        if not message.is_outgoing:
            return
        style = self._notification_style
        if message.status is MessageStatus.FAILED:
            reason = f" — {message.error}" if message.error else ""
            self._console.print(Text(f"  ✗ delivery failed{reason}", style="gl.error"))
            return
        if style == "muted":
            return
        if message.status is MessageStatus.DELIVERED:
            if style == "banner":
                self._console.print(Text("  ✓✓ delivered", style="gl.muted"))
            else:
                self._console.print(Text("  ✓✓", style="gl.muted"))
        elif message.status is MessageStatus.READ:
            stamp = format_timestamp(message.read_at or 0.0, self._timestamp_format)
            if style == "banner":
                self._console.print(Text(f"  ✓✓ read {stamp}", style="gl.success"))
            else:
                self._console.print(Text("  ✓✓ read", style="gl.success"))

    def _print_system(self, text: str, *, style: str = "gl.muted", loud: bool = False) -> None:
        """A status line; 'muted' notification style only allows loud ones."""

        if self._notification_style == "muted" and not loud:
            return
        self._event_log.append(text)
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
        elif command == "/identity":
            self._cmd_identity()
        elif command == "/fingerprint":
            self._cmd_fingerprint()
        elif command == "/invite":
            await self._cmd_invite(argument)
        elif command == "/clear":
            self._console.clear()
            self._print_banner(
                status="Connected" if self._session.is_ready else "Connecting…",
                encryption="Active" if self._session.is_ready else "Handshake in progress",
            )
        elif command == "/history":
            self._show_history()
        elif command == "/export":
            self._export_history(argument)
        elif command == "/react":
            if not argument:
                self._print_error("Usage: /react <emoji>  (e.g. /react 👍)")
            else:
                self._print_system(f"Reacted {argument} to the conversation", style="gl.accent")
        elif command == "/reply":
            if not argument:
                self._print_error("Usage: /reply <quote or text>")
            else:
                quoted = f"> {argument}"
                await self._send(quoted)
        elif command == "/pin":
            if not argument:
                self._print_error("Usage: /pin <text to pin>")
            else:
                self._pinned_messages.append(argument)
                self._print_system(f"📌 Pinned: '{argument}'", style="gl.success", loud=True)
        elif command == "/unpin":
            if argument in self._pinned_messages:
                self._pinned_messages.remove(argument)
                self._print_system(f"Unpinned: '{argument}'", style="gl.info")
            elif self._pinned_messages:
                removed = self._pinned_messages.pop()
                self._print_system(f"Unpinned: '{removed}'", style="gl.info")
            else:
                self._print_system("No pinned messages.")
        elif command == "/pinned":
            if not self._pinned_messages:
                self._print_system("No pinned messages in this session.")
            else:
                self._console.print(
                    Panel(
                        Group(
                            *(Text(f"📌 {msg}", style="gl.text") for msg in self._pinned_messages)
                        ),
                        title="[gl.title]Pinned Messages[/]",
                        border_style="gl.accent",
                    )
                )
        elif command == "/search":
            if not argument:
                self._print_error("Usage: /search <query>")
            elif not self._history.enabled:
                self._print_system("History is disabled — cannot search past messages.")
            else:
                matches = [
                    entry
                    for entry in self._history.entries()
                    if argument.lower() in entry.text.lower()
                ]
                if not matches:
                    self._print_system(f"No messages found matching '{argument}'.")
                else:
                    self._print_system(
                        f"Found {len(matches)} match(es) for '{argument}':",
                        style="gl.accent",
                    )
                    for match in matches[:10]:
                        self._print_system(
                            f"[{match.author}]: {match.text}",
                            style="gl.text",
                        )
        elif command == "/send":
            await self._cmd_send(argument)
        elif command == "/transfers":
            self._cmd_transfers()
        elif command == "/transfer":
            self._cmd_transfer_detail(argument)
        elif command in ("/accept", "/reject"):
            await self._cmd_answer(command[1:], argument)
        elif command in ("/pause", "/resume", "/cancel"):
            await self._cmd_transfer_action(command[1:], argument)
        elif command in ("/exit", "/quit"):
            self._running = False
        else:
            self._print_error(f"Unknown command '{command}'. Type /help.")

    def _show_help(self) -> None:
        table = Table(box=None, show_header=False, show_edge=False, padding=(0, 2))
        table.add_column(style="gl.accent", no_wrap=True)
        table.add_column(style="gl.text")
        rows = [
            ("/help", "show this command list"),
            ("/info", "session, encryption and delivery statistics"),
            ("/identity", "your local identity — nickname, handle, fingerprint"),
            ("/fingerprint", "peer verification fingerprints (compare out-of-band)"),
            ("/invite [list|revoke <id>]", "invite status for this chat or manage your invites"),
            ("/react <emoji>", "react to the latest message (e.g. /react 👍)"),
            ("/reply <text>", "quote and reply to a message"),
            ("/pin <text>", "pin a key note or message in this session"),
            ("/pinned", "view pinned messages"),
            ("/search <query>", "search conversation history"),
            ("/clear", "clear the screen and redraw the banner"),
            ("/history", "show messages retained this session"),
            ("/export [file.txt|json]", "write retained history to a plaintext or JSON file"),
            ("/send <file>", "offer a file — encrypted end-to-end, peer approves first"),
            ("/transfers", "list every transfer with live progress and state"),
            ("/transfer <id>", "transfer details: progress, integrity, destination"),
            ("/accept [id]", "accept the latest (or given) incoming offer — 'Y' works too"),
            ("/reject [id]", "decline an incoming offer — 'N' works too"),
            ("/pause <id>", "pause an in-flight transfer"),
            ("/resume <id>", "resume a paused transfer (verified chunks are kept)"),
            ("/cancel <id>", "cancel a transfer; ids may be unique prefixes"),
            ("/exit", "close the session and leave"),
            ("\\", "end a line with a backslash to keep writing multi-line messages"),
        ]
        for command, description in rows:
            # Text objects: square brackets in usage hints must not be read
            # as Rich markup tags.
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
        session = self._session
        try:
            self._latency_ms = await asyncio.wait_for(
                session.relay_client.ping(), timeout=_LATENCY_TIMEOUT_SECONDS
            )
        except Exception:
            self._latency_ms = None
        latency = "—" if self._latency_ms is None else f"{self._latency_ms:.0f} ms"
        stats = session.stats
        counts = ReceiptTracker.delivery_counts(list(session.outbox))
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="gl.muted", no_wrap=True)
        grid.add_column(style="gl.text")
        grid.add_row("Status", "Connected" if session.is_ready else "Connecting…")
        grid.add_row("Encryption", "Active" if session.is_ready else "Handshake in progress")
        grid.add_row("Safety code", session.fingerprint or "—")
        grid.add_row("Conversation", session.conversation_id or "—")
        grid.add_row("Room", session.channel)
        grid.add_row("Peer", session.peer_name + (" (typing…)" if session.peer_typing else ""))
        grid.add_row("Latency", latency)
        grid.add_row(
            "Messages",
            f"sent {stats.messages_sent} · received {stats.messages_received} · "
            f"unread {session.unread_count}",
        )
        grid.add_row(
            "Delivery",
            f"queued {counts[MessageStatus.QUEUED]} · sent {counts[MessageStatus.SENT]} · "
            f"delivered {counts[MessageStatus.DELIVERED]} · "
            f"read {counts[MessageStatus.READ]} · failed {counts[MessageStatus.FAILED]}",
        )
        grid.add_row(
            "Security ops",
            f"rekeys {stats.rekeys} · resends {stats.resends} · "
            f"integrity failures {stats.decrypt_failures}",
        )
        if not self._history.enabled:
            history_mode = "off"
        elif self._history.persistent:
            history_mode = "encrypted"
        else:
            history_mode = "session"
        grid.add_row("History", f"{history_mode} · {len(self._history)} retained")
        self._console.print(
            Panel(
                grid,
                title="[gl.title]Session info[/]",
                box=box.DOUBLE,
                border_style="gl.accent",
                padding=(0, 2),
            )
        )

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

    # ------------------------------------------------- identity & invites (Phase 5)

    def _cmd_identity(self) -> None:
        manager = self._identities
        if manager is None:
            self._print_system("Identity is unavailable in this session.")
            return
        identity = manager.ensure()
        fingerprint = manager.fingerprint(identity)
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="gl.muted", no_wrap=True)
        grid.add_column(style="gl.text")
        grid.add_row("Nickname:", identity.nickname or Text("not set", style="gl.muted"))
        grid.add_row("Identity:", Text(identity.identity_id, style="gl.accent"))
        grid.add_row("Fingerprint:", Text(fingerprint, style="gl.accent"))
        self._console.print(
            Panel(
                grid,
                title="[gl.title]YOUR LOCAL IDENTITY[/]",
                subtitle="[gl.muted]ephemeral · local-only · no account[/]",
                box=box.ROUNDED,
                border_style="gl.accent",
                padding=(0, 1),
                width=min(52, max(30, self._console.width)),
                expand=False,
            )
        )

    def _cmd_fingerprint(self) -> None:
        session = self._session
        manager = self._identities
        identity = manager.ensure() if manager is not None else None
        own_fingerprint = manager.fingerprint(identity) if (manager and identity) else None
        own_id = identity.identity_id if identity is not None else "—"
        panel = verification_panel(
            peer_name=session.peer_name,
            peer_fingerprint=session.peer_identity_fingerprint,
            own_fingerprint=own_fingerprint if own_fingerprint is not None else "—",
            own_name=f"{session.display_name} ({own_id})",
            safety_code=session.fingerprint,
            identity_bound=session.identity_bound,
            console_width=self._console.width,
        )
        self._console.print(panel)

    async def _cmd_invite(self, argument: str) -> None:
        manager = self._invites
        if manager is None:
            self._print_system("Invite management is unavailable in this session.")
            return
        subcommand, _, target = argument.partition(" ")
        subcommand = subcommand.strip().lower()
        target = target.strip()
        if subcommand in ("", "list"):
            if subcommand == "" and self._session_invite_id is not None:
                record = manager.get(self._session_invite_id)
                if record is not None:
                    self._print_system(
                        f"This conversation came from invite {record.invite_id} — "
                        f"state {record.effective_state().value.upper()} · room {record.room_id}",
                        loud=True,
                    )
                    return
            self._render_invite_list(manager)
            return
        if subcommand == "revoke":
            await self._revoke_invite_interactive(manager, target)
            return
        self._print_error(f"Unknown /invite action '{subcommand}'. Try /invite list.")

    def _render_invite_list(self, manager: SecureInviteManager) -> None:
        records = manager.list()
        if not records:
            self._print_system(
                "No invites yet — create one with 'ghostlink invite create --expires 5m'."
            )
            return
        self._console.print(invites_table(records))
        self._console.print(invite_targets_hint())

    async def _revoke_invite_interactive(self, manager: SecureInviteManager, target: str) -> None:
        if not target:
            self._print_error("Usage: /invite revoke <invite-id>")
            return
        records = manager.list()
        invite_id = resolve_invite_id_argument(target, records)
        if invite_id is None:
            self._print_error(f"No invite matches '{target}'.")
            return
        record = manager.require(invite_id)
        try:
            if manager.token_for(invite_id) is not None and record.relay_url:
                # The live chat client is connected to the inviting relay.
                await manager.revoke_via_relay(self._session.relay_client, invite_id)
            else:
                record = manager.revoke_local(invite_id)
        except InviteError as exc:
            self._print_error(exc.message)
            return
        except TransportError as exc:
            self._print_error(f"The relay could not confirm the revocation: {exc.message}")
            return
        self._print_system(f"Invite {invite_id} revoked.", loud=True)

    # ------------------------------------------------------------------ history

    def _export_history(self, argument: str) -> None:
        if not self._history.enabled:
            self._print_system("History is disabled — nothing to export.")
            return

        arg = argument.strip()
        is_json = arg.lower().endswith(".json") or arg.lower() == "json"

        if is_json:
            target = (
                Path(arg).expanduser()
                if (arg and arg.lower() != "json")
                else Path.cwd() / "ghostlink_export.json"
            )
            try:
                import json

                records = [
                    {
                        "message_id": m.message_id,
                        "author": m.author,
                        "sent_at": m.sent_at,
                        "text": m.text,
                        "direction": m.direction,
                    }
                    for m in self._history.entries()
                ]
                target.write_text(json.dumps(records, indent=2), encoding="utf-8")
                path = target
            except Exception as exc:
                self._print_error(str(getattr(exc, "message", exc)))
                return
        else:
            target = (
                Path(arg).expanduser()
                if (arg and arg.lower() != "txt")
                else Path.cwd() / export_file_name()
            )
            try:
                path = self._history.write_export(target, timestamp_format=self._timestamp_format)
            except Exception as exc:
                self._print_error(str(getattr(exc, "message", exc)))
                return
        self._print_system(
            f"Exported to {path} — plain file, handle with care. Does not contain keys.",
            style="gl.warning",
            loud=True,
        )

    # ------------------------------------------------------- transfer commands

    def _require_transfers(self) -> TransferManager | None:
        if self._transfers is None:
            self._print_error("File transfers are unavailable in this session.")
            return None
        return self._transfers

    def _resolve_transfer_id(self, manager: TransferManager, argument: str) -> str | None:
        if not argument:
            self._print_error("A transfer id is required — see /transfers (unique prefixes work).")
            return None
        transfer_id = manager.resolve_id(argument)
        if transfer_id is None:
            self._print_error(f"No transfer matches '{argument}'. See /transfers.")
        return transfer_id

    async def _cmd_send(self, argument: str) -> None:
        manager = self._require_transfers()
        if manager is None:
            return
        if not argument:
            self._print_error("Usage: /send <path-to-file>")
            return
        try:
            await manager.send_file(argument)
        except Exception as exc:  # GhostLinkError family — surface, never crash
            self._print_error(str(getattr(exc, "message", exc)))

    def _cmd_transfers(self) -> None:
        manager = self._require_transfers()
        if manager is None:
            return
        snapshots = manager.list_transfers()
        if not snapshots:
            self._print_system("No transfers yet — /send <file> starts one.")
            return
        self._console.print(
            Panel(
                transfers_table(snapshots),
                title="[gl.title]Transfers[/]",
                box=box.ROUNDED,
                border_style="gl.border",
                padding=(0, 1),
            )
        )

    def _cmd_transfer_detail(self, argument: str) -> None:
        manager = self._require_transfers()
        if manager is None:
            return
        transfer_id = self._resolve_transfer_id(manager, argument)
        if transfer_id is None:
            return
        snapshot = manager.get(transfer_id)
        if snapshot is None:  # purged between resolve and read
            self._print_error(f"Transfer {transfer_id} was purged after closing.")
            return
        stamp = format_timestamp(snapshot.created_at, self._timestamp_format)
        grid = Table.grid(padding=(0, 2))
        grid.add_column(style="gl.muted", no_wrap=True)
        grid.add_column(style="gl.text", overflow="fold")
        grid.add_row("ID", snapshot.transfer_id)
        grid.add_row("File", snapshot.filename)
        grid.add_row("Size", format_bytes(snapshot.size_bytes))
        grid.add_row("Type", snapshot.mime)
        grid.add_row(
            "Direction", "outgoing ↑" if snapshot.direction.value == "sending" else "incoming ↓"
        )
        grid.add_row("Peer", snapshot.peer)
        grid.add_row("State", state_label(snapshot.state))
        grid.add_row(
            "Progress",
            f"{snapshot.progress * 100:.0f}% — {format_bytes(snapshot.bytes_done)} "
            f"({snapshot.chunks_done}/{snapshot.total_chunks} chunks of "
            f"{format_bytes(snapshot.chunk_size)})",
        )
        grid.add_row("Integrity", f"SHA-256 {snapshot.integrity[:16]}…")
        grid.add_row("Created", stamp)
        if snapshot.saved_path:
            grid.add_row("Saved to", snapshot.saved_path)
        if snapshot.error:
            grid.add_row("Detail", snapshot.error)
        self._console.print(
            Panel(
                grid,
                title=f"[gl.title]Transfer {snapshot.transfer_id}[/]",
                box=box.ROUNDED,
                border_style="gl.border",
                padding=(0, 1),
            )
        )

    async def _cmd_answer(self, verb: str, argument: str) -> None:
        manager = self._require_transfers()
        if manager is None:
            return
        transfer_id: str | None
        if argument:
            transfer_id = self._resolve_transfer_id(manager, argument)
        else:
            pending = manager.pending_incoming()
            if not pending:
                self._print_error("No incoming offer is waiting for an answer.")
                return
            transfer_id = pending[0].transfer_id
        if transfer_id is None:
            return
        try:
            if verb == "accept":
                await manager.accept(transfer_id)
            else:
                await manager.reject(transfer_id)
        except Exception as exc:
            self._print_error(str(getattr(exc, "message", exc)))

    async def _cmd_transfer_action(self, verb: str, argument: str) -> None:
        manager = self._require_transfers()
        if manager is None:
            return
        transfer_id = self._resolve_transfer_id(manager, argument)
        if transfer_id is None:
            return
        try:
            if verb == "pause":
                await manager.pause(transfer_id)
            elif verb == "resume":
                await manager.resume(transfer_id)
            else:
                await manager.cancel(transfer_id)
        except Exception as exc:
            self._print_error(str(getattr(exc, "message", exc)))


# ------------------------------------------------------------------ runner


def _resolve_history_path(state_dir: Path) -> Path:
    from ghostlink.messaging.history import HISTORY_FILE_NAME

    return state_dir / "history" / HISTORY_FILE_NAME


async def _prompt_new_passphrase() -> str:
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
    return first


async def _open_chat_history(settings: AppSettings, state_dir: Path) -> BaseHistory:
    """Build the history backend, prompting for a passphrase when needed."""

    mode = HistoryMode(settings.chat.history_mode)
    if mode is not HistoryMode.ENCRYPTED:
        return open_history(mode, state_dir=state_dir)
    if not sys.stdin.isatty():
        raise HistoryError(
            "Encrypted history needs an interactive terminal for the passphrase.",
            hint="Run from a real terminal, or set chat.history_mode to 'session' or 'disabled'.",
        )
    if _resolve_history_path(state_dir).exists():
        passphrase = await asyncio.to_thread(getpass.getpass, "History passphrase: ")
    else:
        passphrase = await _prompt_new_passphrase()
    return open_history(mode, state_dir=state_dir, passphrase=passphrase)


async def run_chat_session(
    console: ConsoleManager,
    *,
    settings: AppSettings,
    state_dir: Path,
    role: str,
    channel: str,
    relay_url: str,
    client_config: RelayClientConfig,
    display_name: str | None = None,
    input_source: InputSource | None = None,
    identity_manager: IdentityManager | None = None,
    invite_manager: SecureInviteManager | None = None,
    existing_client: RelayClient | None = None,
    session_invite_id: str | None = None,
) -> int:
    """Connect, establish the secure session, and run the chat surface.

    Shared by the CLI commands and the interactive menu so every path into a
    conversation behaves identically. Owns the relay client, the chat
    session, and the history backend for the whole conversation. When the
    caller owns identity/invite managers (Phase 5), the session presents the
    local identity key in the handshake so peers can compare fingerprints.
    """

    chat = settings.chat
    identity = identity_manager.ensure() if identity_manager is not None else None
    name = (
        display_name
        or (identity.nickname if identity is not None else "")
        or chat.display_name
        or DEFAULT_CHAT_NAME
    ).strip()
    history = await _open_chat_history(settings, state_dir)
    client = existing_client
    owns_client = client is None
    if client is None:
        endpoint = RelayEndpoint.from_url(relay_url)
        client = RelayClient(endpoint, client_name=f"ghostlink-{role}", config=client_config)
        await client.connect()
    session_config = ChatSessionConfig(
        read_receipts=chat.read_receipts,
        typing_indicators=chat.typing_indicators,
    )
    session = ChatSession(
        client,
        channel=channel,
        role=role,
        display_name=name,
        config=session_config,
        history=history,
        identity_public_key_hex=(identity.public_key_hex if identity is not None else None),
    )
    transfers = TransferManager(
        session,
        TransferStorage(state_dir, download_dir=settings.transfer.download_dir),
        TransferLimits.from_settings(settings.transfer),
        history=history,
    )
    app = ChatApp(
        console,
        session,
        history=history,
        timestamp_format=chat.timestamp_format,
        notification_style=chat.notification_style,
        message_wrapping=chat.message_wrapping,
        input_source=input_source,
        transfer_manager=transfers,
        identity_manager=identity_manager,
        invite_manager=invite_manager,
        session_invite_id=session_invite_id,
    )
    # Session binding (Phase 5): when this conversation came from an invite,
    # bind the resulting conversation id to the invite record the moment the
    # first secure session is live.
    if invite_manager is not None and session_invite_id is not None:

        def _bind_when_ready(event: ChatEvent) -> None:
            if (
                event.kind is ChatEventKind.SESSION
                and session.is_ready
                and session.conversation_id is not None
            ):
                with contextlib.suppress(Exception):
                    invite_manager.note_local_redeemed(
                        session_invite_id,
                        session_binding=session.conversation_id,
                    )

        session.add_listener(_bind_when_ready)
    try:
        await session.start()
        await transfers.start()
        return await app.run()
    finally:
        await transfers.close()
        await session.close()
        if owns_client:
            await client.disconnect()
