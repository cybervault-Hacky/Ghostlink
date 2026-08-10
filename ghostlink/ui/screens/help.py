"""Help screen — keyboard and chat command reference."""

from __future__ import annotations

from rich.text import Text

from ghostlink.i18n import t
from ghostlink.ui.components.layout import section_label
from ghostlink.ui.components.tables import kv_grid
from ghostlink.ui.screens.base import Screen


class HelpScreen(Screen):
    """Keyboard shortcuts and interactive command reference."""

    async def show(self) -> None:
        console = self.context.console
        lang = self.context.settings.ui.language

        self.header(t("menu.help.label", lang), "Keyboard shortcuts and chat commands")

        console.print(section_label("Keyboard"))
        console.newline()
        console.print(
            kv_grid(
                [
                    ("↑ / ↓  ·  j / k", Text("Move selection")),
                    ("Enter", Text("Select / confirm")),
                    ("q  ·  Esc", Text("Back / cancel")),
                    ("1 - 9", Text("Direct select when arrow keys are unavailable")),
                    ("Ctrl+C", Text("Exit GhostLink")),
                ]
            )
        )
        console.newline()
        console.print(section_label("Commands"))
        console.newline()
        console.print(
            kv_grid(
                [
                    ("/help", Text("List in-session chat commands")),
                    ("/info", Text("Session encryption, latency, and peer metrics")),
                    ("/identity", Text("Your local identity and public fingerprint")),
                    ("/fingerprint", Text("Verify the peer safety fingerprint out-of-band")),
                    ("/send <file>", Text("Send an end-to-end encrypted file")),
                    ("/transfers", Text("List active, completed, and pending transfers")),
                    ("/react <emoji>", Text("React to the latest message")),
                    ("/reply <text>", Text("Quote and reply to a message")),
                    ("/export [file]", Text("Export retained history to TXT or JSON")),
                    ("/clear", Text("Clear the screen and redraw the session header")),
                    ("/exit", Text("Wipe ephemeral keys and close the session")),
                    ("\\", Text("End a line with backslash for multi-line input")),
                ]
            )
        )
        console.newline()
        console.print(
            Text(
                "Built for Termux and Linux with full arrow-key support.",
                style="gl.muted",
            )
        )
        await self.pause()
