"""Settings screen — a read-only view of the effective configuration.

Values shown here are fully resolved: platform defaults, the configuration
file, and command-line overrides already merged. In-app editing is scheduled
for a later phase; until then the configuration file is the editing surface,
and the screen points directly at it.
"""

from __future__ import annotations

from rich.console import Group
from rich.text import Text

from ghostlink.constants.app import SUPPORTED_LANGUAGES
from ghostlink.models.theme import ThemeSpec
from ghostlink.ui.components.badges import BadgeTone, badge
from ghostlink.ui.components.panels import section_panel
from ghostlink.ui.components.tables import info_table
from ghostlink.ui.screens.base import Screen, ScreenContext


def _relay_value(context: ScreenContext, theme: ThemeSpec) -> Text:
    """The relay row: configured endpoint, or an honest 'not configured'."""

    url = context.settings.relay.url.strip()
    if not url:
        return Text.assemble(
            badge("Not configured", BadgeTone.MUTED, theme=theme),
            ("  rooms work locally today", "gl.muted"),
        )
    return Text.assemble(badge("Configured", BadgeTone.SUCCESS, theme=theme), f"  {url}")


class SettingsScreen(Screen):
    """Displays the active settings."""

    async def show(self) -> None:
        context = self.context
        console = context.console
        settings = context.settings
        theme = context.theme

        rows = [
            (
                "Theme",
                badge(theme.name.title(), BadgeTone.ACCENT, theme=theme),
            ),
            (
                "Language",
                Text(f"{SUPPORTED_LANGUAGES[settings.ui.language]} ({settings.ui.language})"),
            ),
            (
                "Notifications",
                badge(
                    "Enabled" if settings.notifications.enabled else "Disabled",
                    BadgeTone.SUCCESS if settings.notifications.enabled else BadgeTone.MUTED,
                    theme=theme,
                ),
            ),
            ("Data Directory", Text(str(context.data_dir))),
            (
                "Relay",
                _relay_value(context, theme),
            ),
            (
                "Chat Identity",
                Text(settings.chat.display_name or "per-run default"),
            ),
            (
                "Read Receipts",
                badge(
                    "On" if settings.chat.read_receipts else "Off",
                    BadgeTone.SUCCESS if settings.chat.read_receipts else BadgeTone.MUTED,
                    theme=theme,
                ),
            ),
            (
                "Typing Indicators",
                badge(
                    "On" if settings.chat.typing_indicators else "Off",
                    BadgeTone.SUCCESS if settings.chat.typing_indicators else BadgeTone.MUTED,
                    theme=theme,
                ),
            ),
            (
                "History Mode",
                Text(settings.chat.history_mode),
            ),
            (
                "Debug Mode",
                badge(
                    "On" if settings.diagnostics.debug else "Off",
                    BadgeTone.WARNING if settings.diagnostics.debug else BadgeTone.MUTED,
                    theme=theme,
                ),
            ),
        ]

        note = Text.assemble(
            ("Configuration file  ", "gl.muted"),
            (str(context.config_path), "gl.text"),
            ("\n", ""),
            (
                "In-app editing is scheduled for a later phase — adjust values "
                "in the file directly; validation reports any mistake on launch.",
                "gl.muted",
            ),
        )

        console.clear()
        console.newline()
        console.print(
            section_panel(
                "Settings",
                Group(info_table(rows), Text(""), note),
                subtitle="read-only",
            )
        )
        await self.pause()
