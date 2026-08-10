"""Command Palette — keyboard-driven quick-action navigator."""

from __future__ import annotations

from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from ghostlink.ui.components.badges import BadgeTone, badge
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.base import ScreenContext


class CommandPalette:
    """Quick jump command palette."""

    def __init__(self, context: ScreenContext) -> None:
        self.context = context
        self._menu = InteractiveMenu(context.console)

    def show(self) -> str:
        console = self.context.console
        theme = self.context.theme

        console.clear()
        console.newline()

        header_text = Text.assemble(
            ("⚡ COMMAND PALETTE  ", "bold gl.title"),
            ("·  Quick Jump Navigator", "gl.muted"),
        )
        badge_row_el = Align.center(
            badge("Press Enter to execute action", BadgeTone.INFO, theme=theme)
        )

        console.print(
            Panel(
                Group(Align.center(header_text), Text(""), badge_row_el),
                border_style="gl.accent",
                padding=(0, 2),
            )
        )
        console.newline()

        entries = (
            MenuEntry(
                key="create-room",
                label="Create Encrypted Room",
                description="Host a new encrypted one-to-one conversation",
                icon="✚",
            ),
            MenuEntry(
                key="join-room",
                label="Join Friend's Room",
                description="Enter an existing encrypted chat via room ID",
                icon="➤",
            ),
            MenuEntry(
                key="transfers",
                label="File Transfer Center",
                description="View transfer pipeline, download path, and limits",
                icon="📎",
            ),
            MenuEntry(
                key="security",
                label="Security Dashboard",
                description="Inspect verified cryptographic posture & audit details",
                icon="🛡",
            ),
            MenuEntry(
                key="identity",
                label="Identity Manager",
                description="Manage local cryptographic keys, nickname & fingerprint",
                icon="🔑",
            ),
            MenuEntry(
                key="storage",
                label="Storage Manager",
                description="Calculate disk footprint, clear temp chunks & logs",
                icon="💾",
            ),
            MenuEntry(
                key="settings",
                label="Settings Center",
                description="Customize themes, interface languages, and privacy",
                icon="⚙",
            ),
            MenuEntry(
                key="help",
                label="Help & Keyboard Shortcuts",
                description="View keyboard shortcuts and interactive commands",
                icon="❓",
            ),
            MenuEntry(
                key="about",
                label="About GhostLink",
                description="Platform, version, license, and creator credits",
                icon="◆",
            ),
            MenuEntry(
                key="cancel",
                label="Close Palette",
                description="Return to the active view",
                icon="✖",
            ),
        )

        return self._menu.prompt(entries, default_key="cancel")
