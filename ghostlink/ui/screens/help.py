"""Help & Keyboard Shortcuts screen."""

from __future__ import annotations

from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ghostlink.ui.components.badges import BadgeTone, badge
from ghostlink.ui.components.panels import section_panel
from ghostlink.ui.screens.base import Screen


class HelpScreen(Screen):
    """Keyboard shortcuts and interactive command reference."""

    async def show(self) -> None:
        console = self.context.console
        theme = self.context.theme

        console.clear()
        console.newline()

        # Keyboard shortcuts table
        nav_table = Table(box=None, show_header=True, header_style="gl.title", pad_edge=False)
        nav_table.add_column("Shortcut / Key", style="gl.accent", width=22)
        nav_table.add_column("Action & Description", style="gl.text")

        nav_table.add_row("↑ / ↓  or  j / k", "Navigate menu items and scroll selection")
        nav_table.add_row("Enter", "Confirm selection / execute action")
        nav_table.add_row("q  or  Esc", "Go back / cancel current prompt / exit")
        nav_table.add_row("1 - 9", "Direct number select in fallback terminal mode")
        nav_table.add_row("Ctrl+K", "Open Command Palette quick jump from anywhere")

        # Chat commands table
        chat_table = Table(box=None, show_header=True, header_style="gl.title", pad_edge=False)
        chat_table.add_column("Chat Command", style="gl.highlight", width=22)
        chat_table.add_column("Description", style="gl.text")

        chat_table.add_row("/help", "List all in-session chat commands")
        chat_table.add_row("/info", "View active session encryption, latency & peer metrics")
        chat_table.add_row("/identity", "Display your local identity and public fingerprint")
        chat_table.add_row("/fingerprint", "Verify peer safety fingerprint out-of-band")
        chat_table.add_row("/send <file>", "Send an end-to-end encrypted file")
        chat_table.add_row("/transfers", "List active, completed, and pending transfers")
        chat_table.add_row("/react <emoji>", "React to the latest message")
        chat_table.add_row("/reply <text>", "Quote and reply to a message")
        chat_table.add_row("/export [file]", "Export retained history to TXT or JSON")
        chat_table.add_row("/clear", "Clear screen and redraw session header")
        chat_table.add_row("/exit  or  /quit", "Wipe ephemeral keys and close session")
        chat_table.add_row("\\ (backslash)", "End line with backslash for multi-line input")

        body = Group(
            Align.center(badge("Navigation & Interactive Control", BadgeTone.ACCENT, theme=theme)),
            Text(""),
            Panel(
                nav_table,
                title="[gl.title]Terminal Navigation Shortcuts[/]",
                border_style="gl.border",
            ),
            Text(""),
            Panel(
                chat_table,
                title="[gl.title]In-Session Chat Commands[/]",
                border_style="gl.border",
            ),
            Text(""),
            Text(
                "GhostLink is built natively for Termux and Linux with full arrow-key support.",
                style="gl.muted",
                justify="center",
            ),
        )

        console.print(section_panel("Help & Shortcuts", body, subtitle="Keyboard Reference"))
        await self.pause()
