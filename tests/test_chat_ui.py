"""Terminal chat surface: banner, blocks, commands, notices (Phase 3)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from ghostlink.messaging.history import NullHistory, SessionHistory
from ghostlink.messaging.models.message import (
    Message,
    MessageDirection,
    MessageStatus,
    generate_message_id,
)
from ghostlink.messaging.session.chat import (
    ChatEvent,
    ChatEventKind,
    ChatStats,
)
from ghostlink.ui.chat import ChatApp, ScriptedInputSource
from ghostlink.ui.console import ConsoleManager
from tests.conftest import run


class _StubRelayClient:
    async def ping(self) -> float:
        return 12.0


class StubSession:
    """The ChatSession surface ChatApp consumes, without any networking."""

    def __init__(self) -> None:
        self._listeners: list = []
        self.sent: list[str] = []
        self.read_calls = 0
        self.typing_calls: list[bool] = []
        self.channel = "gl-room-ABCD-EFGH-JKMN"
        self.display_name = "Nova"
        self.peer_name = "Ravi"
        self.is_ready = True
        self.fingerprint = "A1B2 C3D4 E5F6 7890 A1B2 C3D4 E5F6 7890"
        self.conversation_id = "conv_0123456789ab"
        self.unread_count = 0
        self.peer_typing = False
        self.relay_client = _StubRelayClient()
        self.outbox: list[Message] = []
        self.stats = ChatStats(messages_sent=3, messages_received=2, rekeys=1)

    def add_listener(self, listener) -> None:  # type: ignore[no-untyped-def]
        self._listeners.append(listener)

    def remove_listener(self, listener) -> None:  # type: ignore[no-untyped-def]
        self._listeners.remove(listener)

    def emit(self, event: ChatEvent) -> None:
        for listener in list(self._listeners):
            listener(event)

    async def send_message(self, text: str) -> Message:
        self.sent.append(text)
        return _message(text, outgoing=True, sequence=len(self.sent))

    async def notify_typing(self, *, keystroke: bool) -> None:
        self.typing_calls.append(keystroke)

    async def mark_read(self) -> int:
        self.read_calls += 1
        return 0


def _message(
    text: str, *, outgoing: bool, sequence: int, status: MessageStatus | None = None
) -> Message:
    if outgoing:
        return Message(
            message_id=generate_message_id(),
            conversation_id="conv_0123456789ab",
            direction=MessageDirection.OUTGOING,
            sender="Nova",
            recipient="Ravi",
            text=text,
            sequence=sequence,
            status=status or MessageStatus.QUEUED,
        )
    return Message(
        message_id=generate_message_id(),
        conversation_id="conv_0123456789ab",
        direction=MessageDirection.INCOMING,
        sender="Ravi",
        recipient="Nova",
        text=text,
        sequence=sequence,
        status=status or MessageStatus.DELIVERED,
    )


def _app(
    console_manager: ConsoleManager,
    stub: StubSession,
    lines: list[str | None],
    history: SessionHistory | NullHistory | None = None,
    **chat_kwargs: object,
) -> ChatApp:
    return ChatApp(
        console_manager,
        stub,  # type: ignore[arg-type]
        history=history if history is not None else NullHistory(),
        input_source=ScriptedInputSource(lines),
        **chat_kwargs,  # type: ignore[arg-type]
    )


def _paced(
    console_manager: ConsoleManager,
    stub: StubSession,
    **chat_kwargs: object,
) -> tuple[ChatApp, ScriptedInputSource]:
    """An app whose input arrives later via ``source.push`` — for event tests."""

    source = ScriptedInputSource([])
    app = ChatApp(
        console_manager,
        stub,  # type: ignore[arg-type]
        history=NullHistory(),
        input_source=source,
        **chat_kwargs,  # type: ignore[arg-type]
    )
    return app, source


async def _play(
    app: ChatApp,
    stub: StubSession,
    source: ScriptedInputSource,
    *events: ChatEvent,
) -> None:
    """Start the app, fire session events into it, then end input with EOF."""

    task = asyncio.create_task(app.run())
    await asyncio.sleep(0.05)  # let run() register its listener
    for event in events:
        stub.emit(event)
        await asyncio.sleep(0.02)
    source.push(None)
    await task


class TestBannerAndLifecycle:
    def test_banner_and_graceful_eof(self, console_manager: ConsoleManager) -> None:
        stub = StubSession()
        app = _app(console_manager, stub, [None])
        code = run(app.run())
        assert code == 0
        output = console_manager.export_text()
        assert "GhostLink Secure Session" in output
        assert "gl-room-ABCD-EFGH-JKMN" in output
        assert "Session closed" in output

    def test_pending_banner_before_ready(self, console_manager: ConsoleManager) -> None:
        stub = StubSession()
        stub.is_ready = False
        app = _app(console_manager, stub, [None])
        run(app.run())
        output = console_manager.export_text()
        assert "Handshake in progress" in output
        assert "Connecting" in output

    def test_exit_command_stops_promptly(self, console_manager: ConsoleManager) -> None:
        stub = StubSession()
        app = _app(console_manager, stub, ["/exit"])
        code = run(app.run())
        assert code == 0
        assert stub.sent == []


class TestMessageBlocks:
    def test_outgoing_message_flow(self, console_manager: ConsoleManager) -> None:
        stub = StubSession()
        app = _app(console_manager, stub, ["hello Ravi", None])
        run(app.run())
        assert stub.sent == ["hello Ravi"]
        assert stub.typing_calls == [False]  # typing stopped after send
        output = console_manager.export_text()
        assert "You:" in output
        assert "hello Ravi" in output

    def test_multiline_messages_join(self, console_manager: ConsoleManager) -> None:
        stub = StubSession()
        app = _app(console_manager, stub, ["first line\\", "second line", None])
        run(app.run())
        assert stub.sent == ["first line\nsecond line"]

    def test_narrow_terminal_wraps_messages(self) -> None:
        from ghostlink.ui.themes import ThemeEngine

        console = ConsoleManager(ThemeEngine().get("phantom"), record=True, width=40)
        stub = StubSession()
        long_text = "wrapme " * 20
        app = _app(console, stub, [long_text.strip(), None], message_wrapping=True)
        run(app.run())
        for line in console.export_text().splitlines():
            assert len(line) <= 42  # width + box edges tolerance

    def test_incoming_message_renders_and_reads(self, console_manager: ConsoleManager) -> None:
        stub = StubSession()
        app, source = _paced(console_manager, stub)
        run(
            _play(
                app,
                stub,
                source,
                ChatEvent(
                    kind=ChatEventKind.MESSAGE,
                    message=_message("hi Nova", outgoing=False, sequence=1),
                ),
            )
        )
        output = console_manager.export_text()
        assert "Ravi:" in output
        assert "hi Nova" in output
        assert stub.read_calls >= 1

    def test_delivery_failure_is_loud(self, console_manager: ConsoleManager) -> None:
        stub = StubSession()
        failed = _message("lost", outgoing=True, sequence=1).with_status(MessageStatus.SENDING)
        failed = failed.with_status(MessageStatus.FAILED, error="no ack")
        app, source = _paced(console_manager, stub)
        run(_play(app, stub, source, ChatEvent(kind=ChatEventKind.DELIVERY, message=failed)))
        assert "delivery failed" in console_manager.export_text()

    def test_read_receipt_note(self, console_manager: ConsoleManager) -> None:
        stub = StubSession()
        message = _message("seen", outgoing=True, sequence=1)
        message = message.with_status(MessageStatus.SENDING)
        message = message.with_status(MessageStatus.SENT)
        message = message.with_status(MessageStatus.DELIVERED)
        message = message.with_status(MessageStatus.READ)
        app, source = _paced(console_manager, stub)
        run(_play(app, stub, source, ChatEvent(kind=ChatEventKind.DELIVERY, message=message)))
        assert "read" in console_manager.export_text()


class TestNotices:
    def test_typing_and_peer_and_connection_lines(self, console_manager: ConsoleManager) -> None:
        stub = StubSession()
        app, source = _paced(console_manager, stub)
        run(
            _play(
                app,
                stub,
                source,
                ChatEvent(kind=ChatEventKind.TYPING, detail="Ravi is typing…"),
                ChatEvent(kind=ChatEventKind.PEER, detail="Ravi left the conversation"),
                ChatEvent(kind=ChatEventKind.CONNECTION, detail="Connection lost — reconnecting…"),
                ChatEvent(
                    kind=ChatEventKind.CONNECTION,
                    detail="Reconnected — refreshing encryption…",
                ),
            )
        )
        output = console_manager.export_text()
        assert "Ravi is typing" in output
        assert "left the conversation" in output
        assert "Connection lost" in output
        assert "Reconnected" in output

    def test_muted_style_suppresses_quiet_notices(self, console_manager: ConsoleManager) -> None:
        stub = StubSession()
        app, source = _paced(console_manager, stub, notification_style="muted")
        run(
            _play(
                app,
                stub,
                source,
                ChatEvent(kind=ChatEventKind.PEER, detail="Ravi joined"),
                ChatEvent(
                    kind=ChatEventKind.NOTICE,
                    detail="A message failed its integrity check and was discarded",
                ),
            )
        )
        output = console_manager.export_text()
        assert "Ravi joined" not in output
        assert "integrity check" in output  # loud ones survive muted style


class TestCommands:
    def test_help_lists_all_commands(self, console_manager: ConsoleManager) -> None:
        stub = StubSession()
        app = _app(console_manager, stub, ["/help", None])
        run(app.run())
        output = console_manager.export_text()
        for command in ("/help", "/info", "/clear", "/history", "/export", "/exit"):
            assert command in output

    def test_info_renders_session_stats(self, console_manager: ConsoleManager) -> None:
        stub = StubSession()
        app = _app(console_manager, stub, ["/info", None])
        run(app.run())
        output = console_manager.export_text()
        assert "Session info" in output
        assert "A1B2 C3D4" in output  # safety code
        assert "conv_0123456789ab" in output
        assert "sent 3" in output
        assert "12 ms" in output

    def test_history_disabled_guides_user(self, console_manager: ConsoleManager) -> None:
        stub = StubSession()
        app = _app(console_manager, stub, ["/history", "/export", None])
        run(app.run())
        output = console_manager.export_text()
        assert "history_mode" in output
        assert "nothing to export" in output

    def test_history_command_renders_entries(self, console_manager: ConsoleManager) -> None:
        stub = StubSession()
        history = SessionHistory()
        history.record(_message("kept for later", outgoing=True, sequence=1))
        app = _app(console_manager, stub, ["/history", None], history=history)
        run(app.run())
        assert "kept for later" in console_manager.export_text()

    def test_export_writes_a_plaintext_file(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        stub = StubSession()
        history = SessionHistory()
        history.record(_message("export me", outgoing=True, sequence=1))
        target = tmp_path / "transcript.txt"
        app = _app(console_manager, stub, [f"/export {target}", None], history=history)
        run(app.run())
        content = target.read_text(encoding="utf-8")
        assert "export me" in content
        assert "plaintext" in console_manager.export_text()

    def test_unknown_command_hint(self, console_manager: ConsoleManager) -> None:
        stub = StubSession()
        app = _app(console_manager, stub, ["/dance", None])
        run(app.run())
        assert "Unknown command" in console_manager.export_text()

    def test_clear_redraws_banner(self, console_manager: ConsoleManager) -> None:
        stub = StubSession()
        app = _app(console_manager, stub, ["/clear", None])
        run(app.run())
        output = console_manager.export_text()
        assert output.count("GhostLink Secure Session") >= 2
