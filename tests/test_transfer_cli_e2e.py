"""Full-stack CLI file transfer: /send → relay → accept → saved (Phase 4).

Both peers run the real :func:`run_chat_session` entry point — the same
wiring the ``host``/``join`` commands use — over a real relay on loopback.
The keyboard is scripted; everything else is production code.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from ghostlink.models.room import generate_room_id
from ghostlink.models.settings import AppSettings
from ghostlink.transport.relay.server import RelayServer
from ghostlink.ui.chat import ScriptedInputSource, run_chat_session
from ghostlink.ui.console import ConsoleManager
from ghostlink.ui.themes import ThemeEngine
from tests.conftest import run
from tests.test_chat_session import FAST


def _console() -> ConsoleManager:
    return ConsoleManager(ThemeEngine().get("phantom"), record=True, width=100)


def _settings(download_dir: Path) -> AppSettings:
    settings = AppSettings.defaults()
    return replace(
        settings,
        transfer=replace(settings.transfer, download_dir=str(download_dir)),
    )


def _text(console: ConsoleManager) -> str:
    """Full recorded output so far — non-destructive (fresh polls compose asserts)."""

    return console.console.export_text(clear=False)


async def _wait_for(predicate: object, timeout: float = 10.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():  # type: ignore[operator]
            return
        await asyncio.sleep(0.02)
    raise AssertionError("condition not met within the deadline")


class TestCliTransfer:
    def test_send_accept_complete_via_cli(self, tmp_path: Path) -> None:
        async def scenario() -> None:
            payload = tmp_path / "photo.jpg"
            payload.write_bytes(bytes(index % 253 for index in range(96_000)))
            channel = generate_room_id()

            host_console = _console()
            guest_console = _console()
            host_source = ScriptedInputSource([])
            guest_source = ScriptedInputSource([])
            guest_downloads = tmp_path / "guest-downloads"

            server = RelayServer(host="127.0.0.1", port=0)
            await server.start()
            try:
                host_task = asyncio.create_task(
                    run_chat_session(
                        host_console,
                        settings=_settings(tmp_path / "host-downloads"),
                        state_dir=tmp_path / "host-state",
                        role="host",
                        channel=channel,
                        relay_url=server.url,
                        client_config=FAST,
                        display_name="Nova",
                        input_source=host_source,
                    )
                )
                guest_task = asyncio.create_task(
                    run_chat_session(
                        guest_console,
                        settings=_settings(guest_downloads),
                        state_dir=tmp_path / "guest-state",
                        role="guest",
                        channel=channel,
                        relay_url=server.url,
                        client_config=FAST,
                        display_name="Ravi",
                        input_source=guest_source,
                    )
                )
                # Wait for both encrypted sessions to come up.
                await _wait_for(
                    lambda: (
                        "Safety code" in _text(host_console)
                        and "Safety code" in _text(guest_console)
                    ),
                    timeout=15.0,
                )
                host_source.push(f"/send {payload}")
                await _wait_for(lambda: "Accept transfer?" in _text(guest_console))
                guest_source.push("Y")
                await _wait_for(
                    lambda: "Transfer completed" in _text(host_console),
                    timeout=30.0,
                )
                await _wait_for(lambda: "Saved to:" in _text(guest_console))
                saved = guest_downloads / "photo.jpg"
                assert saved.read_bytes() == payload.read_bytes()
                assert (saved.stat().st_mode & 0o777) == 0o600
                # The sender's console narrates the whole lifecycle.
                host_log = _text(host_console)
                assert "Waiting for peer approval" in host_log
                assert "Peer accepted." in host_log
                guest_log = _text(guest_console)
                assert "Incoming File" in guest_log
                assert "photo.jpg" in guest_log
                assert "Verified" in guest_log
                host_source.push("/exit")
                guest_source.push("/exit")
                assert await host_task == 0
                assert await guest_task == 0
            finally:
                await server.aclose()

        run(scenario())

    def test_cli_pause_resume_cancel_commands(self, tmp_path: Path) -> None:
        """The /transfers and /cancel command surface works mid-flight."""

        async def scenario() -> None:
            payload = tmp_path / "movie.mkv"
            payload.write_bytes(b"\x07" * 400_000)
            channel = generate_room_id()
            host_console = _console()
            guest_console = _console()
            host_source = ScriptedInputSource([])
            guest_source = ScriptedInputSource([])

            server = RelayServer(host="127.0.0.1", port=0)
            await server.start()
            try:
                host_task = asyncio.create_task(
                    run_chat_session(
                        host_console,
                        settings=_settings(tmp_path / "host-downloads"),
                        state_dir=tmp_path / "host-state2",
                        role="host",
                        channel=channel,
                        relay_url=server.url,
                        client_config=FAST,
                        display_name="Nova",
                        input_source=host_source,
                    )
                )
                guest_task = asyncio.create_task(
                    run_chat_session(
                        guest_console,
                        settings=_settings(tmp_path / "guest-downloads2"),
                        state_dir=tmp_path / "guest-state2",
                        role="guest",
                        channel=channel,
                        relay_url=server.url,
                        client_config=FAST,
                        display_name="Ravi",
                        input_source=guest_source,
                    )
                )
                await _wait_for(
                    lambda: (
                        "Safety code" in _text(host_console)
                        and "Safety code" in _text(guest_console)
                    ),
                    timeout=15.0,
                )
                host_source.push(f"/send {payload}")
                await _wait_for(lambda: "Accept transfer?" in _text(guest_console))
                guest_source.push("/accept")
                await _wait_for(
                    lambda: (
                        "transferring" in _text(host_console)
                        or "Peer accepted." in _text(host_console)
                    )
                )
                host_source.push("/transfers")
                await _wait_for(lambda: "movie.mkv" in _text(host_console))
                # Cancel from the sender side; the peer is told + cleaned up.
                host_source.push("/cancel")
                await _wait_for(lambda: "A transfer id is required" in _text(host_console))
                host_source.push("/cancel tf_")  # unique prefix of the only transfer
                await _wait_for(
                    lambda: "Cancelled" in _text(guest_console),
                    timeout=15.0,
                )
                assert "Cancelled." in _text(host_console)
                host_source.push("/exit")
                guest_source.push("/exit")
                assert await host_task == 0
                assert await guest_task == 0
            finally:
                await server.aclose()

        run(scenario())
