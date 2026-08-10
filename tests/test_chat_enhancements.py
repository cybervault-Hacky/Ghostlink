"""Tests for chat experience enhancements: reactions, replies, pins, search, and export."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from ghostlink.messaging.history import SessionHistory
from ghostlink.messaging.models.message import (
    Message,
    MessageDirection,
    MessageStatus,
    generate_message_id,
)
from ghostlink.messaging.session.chat import ChatSession, ChatSessionConfig
from ghostlink.models.room import generate_room_id
from ghostlink.transport.relay.server import RelayServer
from ghostlink.ui.chat import ChatApp, ScriptedInputSource
from ghostlink.ui.console import ConsoleManager
from ghostlink.ui.themes import ThemeEngine
from tests.test_chat_session import _client


class TestChatEnhancements:
    def test_reactions_replies_pins_and_export(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            async with RelayServer(port=0) as server:
                client = await _client(server, "host")
                history = SessionHistory()
                session = ChatSession(
                    client,
                    channel=generate_room_id(),
                    role="host",
                    display_name="Tester",
                    config=ChatSessionConfig(),
                    history=history,
                )
                await session.start()

                # Seed some history
                msg1 = Message(
                    message_id=generate_message_id(),
                    conversation_id="conv_test01",
                    direction=MessageDirection.INCOMING,
                    sender="Alice",
                    recipient="Tester",
                    text="Welcome to the secret channel",
                    sequence=1,
                    status=MessageStatus.DELIVERED,
                    created_at=1700000000.0,
                )
                msg2 = Message(
                    message_id=generate_message_id(),
                    conversation_id="conv_test01",
                    direction=MessageDirection.OUTGOING,
                    sender="Tester",
                    recipient="Alice",
                    text="Testing secure terminal features",
                    sequence=2,
                    status=MessageStatus.DELIVERED,
                    created_at=1700000005.0,
                )
                history.record(msg1)
                history.record(msg2)

                json_export_file = tmp_path / "chat_export.json"
                txt_export_file = tmp_path / "chat_export.txt"

                input_source = ScriptedInputSource(
                    [
                        "/react 🔥",
                        "/reply Welcome to the secret channel",
                        "/pin Important safety code: GLFP-TEST",
                        "/pinned",
                        "/search secure",
                        f"/export {json_export_file}",
                        f"/export {txt_export_file}",
                        "/unpin Important safety code: GLFP-TEST",
                        "/exit",
                    ],
                    line_delay_seconds=0.01,
                )

                theme = ThemeEngine().get("phantom")
                console = ConsoleManager(theme, record=True, width=100, force_terminal=False)
                app = ChatApp(
                    console,
                    session,
                    history=history,
                    input_source=input_source,
                )

                code = await app.run()
                assert code == 0

                output = console.export_text()
                assert "Reacted 🔥" in output
                assert "Pinned: 'Important safety code: GLFP-TEST'" in output
                assert "Found 1 match" in output
                assert "Testing secure terminal features" in output

                # Check JSON export
                assert json_export_file.is_file()
                exported_data = json.loads(json_export_file.read_text(encoding="utf-8"))
                assert len(exported_data) >= 2
                assert exported_data[0]["text"] == "Welcome to the secret channel"

                # Check TXT export
                assert txt_export_file.is_file()
                exported_txt = txt_export_file.read_text(encoding="utf-8")
                assert "Welcome to the secret channel" in exported_txt

                await session.close()

        asyncio.run(scenario())
