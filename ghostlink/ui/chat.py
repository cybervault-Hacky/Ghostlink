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
* local ``/`` commands never touch the wire
"""

from __future__ import annotations

import asyncio
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
from ghostlink.exceptions.messaging import HistoryError, HistoryPassphraseError
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
from ghostlink.transport.relay.client import RelayClient, RelayClientConfig
from ghostlink.transport.relay.endpoint import RelayEndpoint
from ghostlink.ui.console import ConsoleManager

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
    ) -> None:
        self._console = console
        self._session = session
        self._history = history
        self._timestamp_format = timestamp_format
        self._notification_style = notification_style
        self._wrap = message_wrapping
        self._input = input_source or TerminalInputSource()
        self._latency_ms: float | None = None
        self._buffer: list[str] = []
        self._running = False
        self._event_log: list[str] = []

    # ------------------------------------------------------------------ run

    async def run(self) -> int:
        """Render the banner and drive the input loop until /exit or EOF."""

        session = self._session
        session.add_listener(self._on_event)
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
            ("/clear", "clear the screen and redraw the banner"),
            ("/history", "show messages retained this session"),
            ("/export [file]", "write retained history to a plaintext file"),
            ("/exit", "close the session and leave"),
            ("\\", "end a line with a backslash to keep writing multi-line messages"),
        ]
        for command, description in rows:
            table.add_row(command, description)
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
) -> int:
    """Connect, establish the secure session, and run the chat surface.

    Shared by the CLI commands and the interactive menu so every path into a
    conversation behaves identically. Owns the relay client, the chat
    session, and the history backend for the whole conversation."""

    chat = settings.chat
    name = (display_name or chat.display_name or DEFAULT_CHAT_NAME).strip()
    history = await _open_chat_history(settings, state_dir)
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
    )
    app = ChatApp(
        console,
        session,
        history=history,
        timestamp_format=chat.timestamp_format,
        notification_style=chat.notification_style,
        message_wrapping=chat.message_wrapping,
        input_source=input_source,
    )
    try:
        await session.start()
        return await app.run()
    finally:
        await session.close()
        await client.disconnect()
