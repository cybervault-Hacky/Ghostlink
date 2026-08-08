"""About screen — version, developer, platform, license, and build date."""

from __future__ import annotations

from rich.align import Align
from rich.console import Group
from rich.text import Text

from ghostlink.assets.branding import GHOST_EMBLEM
from ghostlink.constants.app import (
    APP_NAME,
    APP_TAGLINE,
    APP_VERSION,
    BUILD_DATE,
    DEVELOPER,
    LICENSE_NAME,
    RELEASE_LABEL,
    REPOSITORY_URL,
)
from ghostlink.ui.components.panels import section_panel
from ghostlink.ui.components.tables import kv_grid
from ghostlink.ui.screens.base import Screen


class AboutScreen(Screen):
    """Displays application identity and build information."""

    async def show(self) -> None:
        context = self.context
        console = context.console
        environment = context.environment
        theme = context.theme

        emblem = Text("\n".join(GHOST_EMBLEM), style=f"bold {theme.accent}")

        facts = kv_grid(
            [
                ("Version", Text(f"v{APP_VERSION} — {RELEASE_LABEL}")),
                ("Developer", Text(DEVELOPER)),
                (
                    "Platform",
                    Text(f"{environment.platform_label} · Python {environment.python_version}"),
                ),
                ("License", Text(LICENSE_NAME)),
                ("Build Date", Text(BUILD_DATE)),
            ]
        )

        footer = Text.assemble(
            (APP_TAGLINE, "gl.muted"),
            ("\n", ""),
            (REPOSITORY_URL, "gl.accent"),
        )

        body = Group(
            Align.center(emblem),
            Text(""),
            Align.center(Text(f"{APP_NAME}", style="gl.title")),
            Text(""),
            facts,
            Text(""),
            Align.center(footer),
        )

        console.clear()
        console.newline()
        console.print(section_panel("About", body))
        await self.pause()
