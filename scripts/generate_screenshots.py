#!/usr/bin/env python3
"""Regenerate the documentation screenshots.

Renders the *real* GhostLink interface through Rich's SVG exporter, so the
images shipped in the README always match what users see in their terminal.
Run from the project root:

    python scripts/generate_screenshots.py
"""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Callable
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rich.align import Align  # noqa: E402
from rich.text import Text  # noqa: E402

from ghostlink.config.manager import ConfigurationManager  # noqa: E402
from ghostlink.models.environment import (  # noqa: E402
    ColorSupport,
    EnvironmentInfo,
    PlatformKind,
)
from ghostlink.models.room import generate_room_id  # noqa: E402
from ghostlink.services.rooms import RoomService  # noqa: E402
from ghostlink.storage.manager import StorageManager  # noqa: E402
from ghostlink.transport.relay.client import RelayClientConfig, probe_relay  # noqa: E402
from ghostlink.transport.relay.server import RelayServer  # noqa: E402
from ghostlink.ui.banner import BannerRenderer  # noqa: E402
from ghostlink.ui.components.badges import BadgeTone, badge, badge_row  # noqa: E402
from ghostlink.ui.components.notifications import NotificationCenter  # noqa: E402
from ghostlink.ui.console import ConsoleManager  # noqa: E402
from ghostlink.ui.chat import ScriptedInputSource, run_chat_session  # noqa: E402
from ghostlink.ui.dashboards import render_relay_dashboard, render_room_dashboard  # noqa: E402
from ghostlink.ui.menu import InteractiveMenu  # noqa: E402
from ghostlink.ui.screens.about import AboutScreen  # noqa: E402
from ghostlink.ui.screens.base import Screen, ScreenContext  # noqa: E402
from ghostlink.ui.screens.home import MENU_ENTRIES  # noqa: E402
from ghostlink.ui.screens.settings import SettingsScreen  # noqa: E402
from ghostlink.ui.themes import ThemeEngine  # noqa: E402

OUTPUT_DIR = PROJECT_ROOT / "docs" / "assets"
RENDER_WIDTH = 100
TERMUX_HOME = Path("/data/data/com.termux/files/home")


def _termux_environment() -> EnvironmentInfo:
    """The environment shown in the screenshots: Termux on Android."""

    return EnvironmentInfo(
        platform=PlatformKind.TERMUX,
        system="Linux",
        release="5.15-android",
        machine="aarch64",
        python_version="3.12.4",
        python_version_info=(3, 12, 4),
        python_implementation="CPython",
        terminal_columns=RENDER_WIDTH,
        terminal_rows=34,
        stdin_is_tty=True,
        stdout_is_tty=True,
        color_support=ColorSupport.TRUECOLOR,
    )


def _context() -> ScreenContext:
    """A recording console context with the default configuration."""

    theme = ThemeEngine().get(ThemeEngine().default_name)
    console = ConsoleManager(theme, record=True, width=RENDER_WIDTH)
    settings = ConfigurationManager().load(create_missing=False)
    scratch = Path("/tmp/ghostlink-docs-state")
    rooms = RoomService(StorageManager(scratch), settings)
    return ScreenContext(
        console=console,
        settings=settings,
        environment=_termux_environment(),
        notifications=NotificationCenter(console),
        config_path=TERMUX_HOME / ".config" / "ghostlink" / "config.toml",
        data_dir=TERMUX_HOME / ".local" / "share" / "ghostlink",
        rooms=rooms,
    )


async def _instant_pause(self: Screen, prompt: str = "…") -> None:
    """Screens acknowledge instantly during documentation rendering."""


def _render_home(context: ScreenContext) -> None:
    from ghostlink.constants.app import APP_VERSION, RELEASE_LABEL

    console = context.console
    banner = BannerRenderer(console)
    menu = InteractiveMenu(console)
    status = badge_row(
        badge(f"v{APP_VERSION}", BadgeTone.ACCENT, theme=context.theme),
        badge(context.environment.platform_label, BadgeTone.INFO, theme=context.theme),
        badge(RELEASE_LABEL, BadgeTone.MUTED, theme=context.theme),
    )
    console.newline()
    console.print(banner.hero(status))
    console.newline()
    console.print(Align.center(Text("↑/↓ or j/k to move · Enter to select", style="gl.muted")))
    console.newline()
    menu.render_legend(MENU_ENTRIES)


def _render_host(context: ScreenContext) -> None:
    room, invite = context.rooms.host_room(name="Midnight Lounge")
    render_room_dashboard(
        context.console,
        room,
        invite,
        relay_note="Relay configured: ws://127.0.0.1:8787",
    )


def _render_relay(context: ScreenContext) -> None:
    """Probe a real relay started in-process and render the live dashboard."""

    async def _probe():
        async with RelayServer(port=0, session_ttl_seconds=60) as server:
            return await probe_relay(
                server.url,
                client_name="GhostLink/0.3.0",
                config=RelayClientConfig(connect_timeout_seconds=3.0),
                pings=8,
                ping_interval_seconds=0.05,
            )

    report = asyncio.run(_probe())
    render_relay_dashboard(context.console, report)


def _render_chat(context: ScreenContext) -> None:
    """Run a real scripted conversation between two chat sessions.

    The host's screen (the recording console) is what the SVG shows — the
    guest runs headless on a second console. Nothing is faked: handshake,
    frames, acks and read receipts all cross a real in-process relay."""

    async def _chat() -> None:
        settings = context.settings
        theme = context.theme
        guest_console = ConsoleManager(theme, record=True, width=RENDER_WIDTH)
        config = RelayClientConfig(
            connect_timeout_seconds=3.0,
            handshake_timeout_seconds=3.0,
            heartbeat_interval_seconds=3600.0,
        )
        scratch = Path("/tmp/ghostlink-docs-state/chat")
        channel = generate_room_id()
        async with RelayServer(port=0, session_ttl_seconds=60) as server:
            host = asyncio.create_task(
                run_chat_session(
                    context.console,
                    settings=settings,
                    state_dir=scratch / "host",
                    role="host",
                    channel=channel,
                    relay_url=server.url,
                    client_config=config,
                    display_name="Nova",
                    input_source=ScriptedInputSource(
                        [
                            "Hey Ravi — finally on Termux 🎉",
                            "Compare the safety code before we talk.",
                            "Identical on my side too. Relay only ever sees ciphertext.",
                            None,
                        ],
                        line_delay_seconds=0.45,
                    ),
                )
            )
            guest = asyncio.create_task(
                run_chat_session(
                    guest_console,
                    settings=settings,
                    state_dir=scratch / "guest",
                    role="guest",
                    channel=channel,
                    relay_url=server.url,
                    client_config=config,
                    display_name="Ravi",
                    input_source=ScriptedInputSource(
                        [
                            "Nova! Yes — reading the code out loud now…",
                            "✅ matches. And the ✓✓ turned read in seconds.",
                            "Privacy without leaving the terminal. Love it.",
                            None,
                        ],
                        line_delay_seconds=0.6,
                    ),
                )
            )
            await asyncio.gather(host, guest)

    asyncio.run(_chat())


SHOTS: dict[str, Callable[[ScreenContext], None]] = {
    "home": _render_home,
    "host": _render_host,
    "relay": _render_relay,
    "chat": _render_chat,
    "settings": lambda ctx: asyncio.run(SettingsScreen(ctx).show()),
    "about": lambda ctx: asyncio.run(AboutScreen(ctx).show()),
}


def main() -> int:
    Screen.pause = _instant_pause  # type: ignore[method-assign]

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for name, render in SHOTS.items():
        context = _context()
        render(context)
        target = OUTPUT_DIR / f"{name}.svg"
        svg = context.console.console.export_svg(title=f"GhostLink — {name.title()}")
        target.write_text(svg, encoding="utf-8")
        print(f"wrote {target.relative_to(PROJECT_ROOT)} ({len(svg):,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
