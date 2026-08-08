"""Terminal transfer UX: offer panels, progress, commands (Phase 4).

A real :class:`TransferManager` rides a stub chat session (no network) and
drives a real :class:`ChatApp` — so commands, keyboard shortcuts, and event
rendering are all exercised the way the terminal sees them.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from ghostlink.messaging.history import NullHistory
from ghostlink.messaging.models.message import (
    Message,
    MessageDirection,
    MessageStatus,
    generate_message_id,
)
from ghostlink.messaging.session.chat import ChatStats
from ghostlink.transfer import packets
from ghostlink.transfer.integrity import derive_transfer_key, seal_manifest
from ghostlink.transfer.manager import (
    TransferEvent,
    TransferEventKind,
    TransferLimits,
    TransferManager,
)
from ghostlink.transfer.manifest import FileManifest
from ghostlink.transfer.models import TransferDirection, TransferSnapshot, TransferState
from ghostlink.transfer.storage import TransferStorage
from ghostlink.ui.chat import ChatApp, ScriptedInputSource
from ghostlink.ui.console import ConsoleManager
from tests.conftest import run

SESSION_KEY = bytes(range(32))


class _StubRelay:
    async def ping(self) -> float:
        return 9.0


class TransferStubSession:
    """The session surface consumed by ChatApp + TransferManager, offline."""

    def __init__(self) -> None:
        self._listeners: list = []
        self.delegate = None
        self.sent: list[str] = []
        self.sent_frames: list = []
        self.typing_calls: list[bool] = []
        self.read_calls = 0
        self.channel = "gl-room-ABCD-EFGH-JKMN"
        self.display_name = "Nova"
        self.peer_name = "Ravi"
        self.is_ready = True
        self.peer_present = True
        self.fingerprint = "A1B2 C3D4"
        self.conversation_id = "conv_0123456789ab"
        self.unread_count = 0
        self.peer_typing = False
        self.relay_client = _StubRelay()
        self.outbox: list = []
        self.stats = ChatStats()

    def add_listener(self, listener) -> None:  # type: ignore[no-untyped-def]
        self._listeners.append(listener)

    def remove_listener(self, listener) -> None:  # type: ignore[no-untyped-def]
        if listener in self._listeners:
            self._listeners.remove(listener)

    def set_transfer_delegate(self, delegate) -> None:  # type: ignore[no-untyped-def]
        self.delegate = delegate

    def session_key_copy(self) -> bytes:
        return SESSION_KEY

    async def send_channel_frame(self, frame) -> None:  # type: ignore[no-untyped-def]
        self.sent_frames.append(frame)

    async def send_message(self, text: str) -> Message:
        self.sent.append(text)
        return Message(
            message_id=generate_message_id(),
            conversation_id=self.conversation_id,
            direction=MessageDirection.OUTGOING,
            sender="Nova",
            recipient="Ravi",
            text=text,
            sequence=len(self.sent),
            status=MessageStatus.QUEUED,
        )

    async def notify_typing(self, *, keystroke: bool) -> None:
        self.typing_calls.append(keystroke)

    async def mark_read(self) -> int:
        self.read_calls += 1
        return 0


def _limits(**overrides: object) -> TransferLimits:
    base: dict[str, object] = {
        "max_file_size_bytes": 8 * 1024 * 1024,
        "max_concurrent": 3,
        "chunk_size_bytes": 1024,
        "ack_timeout_seconds": 0.5,
        "retry_limit": 3,
        "temp_limit_bytes": 16 * 1024 * 1024,
        "transfer_expiry_seconds": 3600.0,
        "offer_timeout_seconds": 30.0,
        "terminal_ttl_seconds": 300.0,
    }
    base.update(overrides)
    return TransferLimits(**base)  # type: ignore[arg-type]


def _manager(stub: TransferStubSession, tmp_path: Path) -> TransferManager:
    storage = TransferStorage(tmp_path / "state", download_dir=str(tmp_path / "dl"))
    return TransferManager(stub, storage, _limits())  # type: ignore[arg-type]


def _app(
    console_manager: ConsoleManager,
    stub: TransferStubSession,
    manager: TransferManager,
    source: ScriptedInputSource,
) -> ChatApp:
    return ChatApp(
        console_manager,
        stub,  # type: ignore[arg-type]
        history=NullHistory(),
        input_source=source,
        transfer_manager=manager,
    )


def _offer_frame(
    *,
    transfer_id: str = "tf_c0ffee42",
    name: str = "photo.jpg",
    size: int = 500_000,
) -> object:
    """A peer-shaped, correctly sealed FILE_OFFER for the stub's key."""

    key = derive_transfer_key(SESSION_KEY, transfer_id)
    manifest = FileManifest(
        protocol="gf1",
        transfer_id=transfer_id,
        filename=name,
        size_bytes=size,
        mime="image/jpeg",
        chunk_size=1024,
        total_chunks=(size + 1023) // 1024,
        sha256="ab" * 32,
    )
    sealed = seal_manifest(key, transfer_id, manifest.to_json())
    return packets.file_offer_frame(transfer_id, sealed)


def _snapshot(**overrides: object) -> TransferSnapshot:
    fields: dict[str, object] = {
        "transfer_id": "tf_c0ffee42",
        "direction": TransferDirection.RECEIVING,
        "state": TransferState.TRANSFERRING,
        "filename": "photo.jpg",
        "size_bytes": 512_000,
        "mime": "image/jpeg",
        "chunk_size": 1024,
        "total_chunks": 100,
        "chunks_done": 50,
        "bytes_done": 256_000,
        "peer": "Ravi",
        "created_at": time.time(),
        "last_activity_at": time.time(),
        "expires_at": time.time() + 3600,
        "error": None,
        "saved_path": None,
        "integrity": "ab" * 32,
    }
    fields.update(overrides)
    return TransferSnapshot(**fields)  # type: ignore[arg-type]


async def _drive_offer_answer(
    console_manager: ConsoleManager,
    stub: TransferStubSession,
    manager: TransferManager,
    answer: str,
) -> None:
    """Run the app, deliver one sealed offer, answer it from the keyboard."""

    source = ScriptedInputSource([])
    app = _app(console_manager, stub, manager, source)
    task = asyncio.create_task(app.run())
    await asyncio.sleep(0.05)
    await manager.handle_frame(_offer_frame())
    await asyncio.sleep(0.05)
    source.push(answer)
    await asyncio.sleep(0.2)
    source.push(None)
    await task


class TestOfferFlow:
    def test_offer_panel_and_accept_with_Y(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        stub = TransferStubSession()

        async def scenario() -> None:
            manager = _manager(stub, tmp_path)
            await manager.start()
            try:
                await _drive_offer_answer(console_manager, stub, manager, "Y")
            finally:
                await manager.close()

        run(scenario())
        output = console_manager.export_text()
        assert "Incoming File" in output
        assert "photo.jpg" in output
        assert "488.3 KB" in output  # 500_000 bytes, humanized
        assert "Accept transfer?" in output
        assert "Accepted" in output
        assert any(frame.type.value == "FILE_ACCEPT" for frame in stub.sent_frames)

    def test_offer_rejected_with_N(self, console_manager: ConsoleManager, tmp_path: Path) -> None:
        stub = TransferStubSession()

        async def scenario() -> None:
            manager = _manager(stub, tmp_path)
            await manager.start()
            try:
                await _drive_offer_answer(console_manager, stub, manager, "n")
            finally:
                await manager.close()

        run(scenario())
        output = console_manager.export_text()
        assert "Declined" in output
        assert any(frame.type.value == "FILE_REJECT" for frame in stub.sent_frames)

    def test_y_without_pending_offer_is_a_plain_message(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        stub = TransferStubSession()

        async def scenario() -> None:
            manager = _manager(stub, tmp_path)
            await manager.start()
            try:
                app = _app(console_manager, stub, manager, ScriptedInputSource(["y", None]))
                await app.run()
            finally:
                await manager.close()

        run(scenario())
        assert stub.sent == ["y"]
        assert stub.sent_frames == []


class TestCommands:
    def test_transfers_table_lists_offers(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        stub = TransferStubSession()

        async def scenario() -> None:
            manager = _manager(stub, tmp_path)
            await manager.start()
            try:
                await manager.handle_frame(_offer_frame())
                app = _app(
                    console_manager,
                    stub,
                    manager,
                    ScriptedInputSource(["/transfers", None]),
                )
                await app.run()
            finally:
                await manager.close()

        run(scenario())
        output = console_manager.export_text()
        assert "Transfers" in output
        assert "photo.jpg" in output
        assert "waiting for approval" in output

    def test_transfer_detail_by_id_prefix(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        stub = TransferStubSession()

        async def scenario() -> None:
            manager = _manager(stub, tmp_path)
            await manager.start()
            try:
                await manager.handle_frame(_offer_frame())
                app = _app(
                    console_manager,
                    stub,
                    manager,
                    ScriptedInputSource(["/transfer tf_c0f", None]),
                )
                await app.run()
            finally:
                await manager.close()

        run(scenario())
        output = console_manager.export_text()
        assert "Transfer tf_c0ffee42" in output
        assert "image/jpeg" in output
        assert "SHA-256 abababababababab" in output

    def test_help_lists_transfer_commands(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        stub = TransferStubSession()

        async def scenario() -> None:
            manager = _manager(stub, tmp_path)
            await manager.start()
            try:
                app = _app(console_manager, stub, manager, ScriptedInputSource(["/help", None]))
                await app.run()
            finally:
                await manager.close()

        run(scenario())
        output = console_manager.export_text()
        for command in (
            "/send <file>",
            "/transfers",
            "/transfer <id>",
            "/accept [id]",
            "/reject [id]",
            "/pause <id>",
            "/resume <id>",
            "/cancel <id>",
        ):
            assert command in output, command

    def test_send_missing_file_errors(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        stub = TransferStubSession()

        async def scenario() -> None:
            manager = _manager(stub, tmp_path)
            await manager.start()
            try:
                app = _app(
                    console_manager,
                    stub,
                    manager,
                    ScriptedInputSource(["/send /no/such/file.bin", None]),
                )
                await app.run()
            finally:
                await manager.close()

        run(scenario())
        output = console_manager.export_text()
        assert "not a regular file" in output
        assert stub.sent_frames == []

    def test_send_real_file_emits_offer(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        stub = TransferStubSession()
        payload = tmp_path / "song.mp3"
        payload.write_bytes(b"audio-bytes" * 400)

        async def scenario() -> None:
            manager = _manager(stub, tmp_path)
            await manager.start()
            try:
                app = _app(
                    console_manager,
                    stub,
                    manager,
                    ScriptedInputSource([f"/send {payload}", None]),
                )
                await app.run()
            finally:
                await manager.close()

        run(scenario())
        output = console_manager.export_text()
        assert "Waiting for peer approval" in output
        offers = [f for f in stub.sent_frames if f.type.value == "FILE_OFFER"]
        assert len(offers) == 1
        # The wire never carries the filename — it is sealed inside ct.
        assert "song.mp3" not in offers[0].data.get("ct", "")

    def test_transfer_action_errors_are_clear(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        stub = TransferStubSession()

        async def scenario() -> None:
            manager = _manager(stub, tmp_path)
            await manager.start()
            try:
                app = _app(
                    console_manager,
                    stub,
                    manager,
                    ScriptedInputSource(
                        [
                            "/pause nope",
                            "/cancel",
                            "/transfer",
                            "/accept",
                            "/resume nope",
                            None,
                        ]
                    ),
                )
                await app.run()
            finally:
                await manager.close()

        run(scenario())
        output = console_manager.export_text()
        assert "No transfer matches 'nope'" in output
        assert "A transfer id is required" in output
        assert "No incoming offer is waiting" in output


class TestEventRendering:
    def test_progress_and_saved_lines(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        stub = TransferStubSession()

        async def scenario() -> None:
            manager = _manager(stub, tmp_path)
            await manager.start()
            try:
                source = ScriptedInputSource([])
                app = _app(console_manager, stub, manager, source)
                task = asyncio.create_task(app.run())
                await asyncio.sleep(0.05)
                app._on_transfer_event(  # mid-transfer progress
                    TransferEvent(kind=TransferEventKind.PROGRESS, transfer=_snapshot())
                )
                app._on_transfer_event(
                    TransferEvent(
                        kind=TransferEventKind.SAVED,
                        transfer=_snapshot(
                            state=TransferState.COMPLETED,
                            saved_path="/home/u/Download/GhostLink/photo.jpg",
                        ),
                        detail="/home/u/Download/GhostLink/photo.jpg",
                    )
                )
                await asyncio.sleep(0.05)
                source.push(None)
                await task
            finally:
                await manager.close()

        run(scenario())
        output = console_manager.export_text()
        assert "250.0 KB / 500.0 KB" in output
        assert "50%" in output
        assert "Saved to: /home/u/Download/GhostLink/photo.jpg" in output

    def test_failed_and_completed_states_render_loudly(
        self, console_manager: ConsoleManager, tmp_path: Path
    ) -> None:
        stub = TransferStubSession()

        async def scenario() -> None:
            manager = _manager(stub, tmp_path)
            await manager.start()
            try:
                source = ScriptedInputSource([])
                app = _app(console_manager, stub, manager, source)
                task = asyncio.create_task(app.run())
                await asyncio.sleep(0.05)
                app._on_transfer_event(
                    TransferEvent(
                        kind=TransferEventKind.STATE,
                        transfer=_snapshot(
                            direction=TransferDirection.SENDING,
                            state=TransferState.COMPLETED,
                            chunks_done=100,
                            bytes_done=500_000,
                        ),
                        detail="Transfer completed ✓",
                    )
                )
                app._on_transfer_event(
                    TransferEvent(
                        kind=TransferEventKind.STATE,
                        transfer=_snapshot(
                            state=TransferState.FAILED, error="chunk 7 failed verification"
                        ),
                        detail="Failed — chunk 7 failed verification",
                    )
                )
                app._on_transfer_event(
                    TransferEvent(
                        kind=TransferEventKind.NOTICE,
                        detail="photo.jpg: chunk 7 failed verification — "
                        "the partial download was deleted.",
                    )
                )
                await asyncio.sleep(0.05)
                source.push(None)
                await task
            finally:
                await manager.close()

        run(scenario())
        output = console_manager.export_text()
        assert "Transfer completed ✓" in output
        assert "Failed — chunk 7 failed verification" in output
        assert "partial download was deleted" in output

    def test_narrow_terminal_progress_line(self, tmp_path: Path) -> None:
        from ghostlink.ui.themes import ThemeEngine

        console = ConsoleManager(ThemeEngine().get("phantom"), record=True, width=40)
        stub = TransferStubSession()

        async def scenario() -> None:
            manager = _manager(stub, tmp_path)
            await manager.start()
            try:
                source = ScriptedInputSource([])
                app = _app(console, stub, manager, source)
                task = asyncio.create_task(app.run())
                await asyncio.sleep(0.05)
                app._on_transfer_event(
                    TransferEvent(kind=TransferEventKind.PROGRESS, transfer=_snapshot())
                )
                await asyncio.sleep(0.05)
                source.push(None)
                await task
            finally:
                await manager.close()

        run(scenario())
        output = console.export_text()
        assert "50%" in output  # bar + percentage survive narrow widths
