"""About screen — version, platform, and creator credits."""

from __future__ import annotations

from rich.text import Text

from ghostlink.constants.app import (
    APP_TAGLINE,
    APP_VERSION,
    BUILD_DATE,
    LICENSE_NAME,
    REPOSITORY_URL,
)
from ghostlink.i18n import t
from ghostlink.ui.components.credits import (
    CREATOR_INSTAGRAM,
    CREATOR_NAME,
    CREATOR_YOUTUBE,
)
from ghostlink.ui.components.tables import kv_grid
from ghostlink.ui.screens.base import Screen


class AboutScreen(Screen):
    """Displays application identity and build information."""

    async def show(self) -> None:
        context = self.context
        console = context.console
        environment = context.environment
        lang = context.settings.ui.language

        self.header("GhostLink", "Encrypted terminal communication")

        facts = kv_grid(
            [
                (t("about.version", lang), Text(f"v{APP_VERSION}")),
                (
                    t("about.platform", lang),
                    Text(f"{environment.platform_label} · Python {environment.python_version}"),
                ),
                ("Created by", Text(CREATOR_NAME, style="gl.accent")),
                ("YouTube", Text(CREATOR_YOUTUBE)),
                ("Instagram", Text(CREATOR_INSTAGRAM)),
                (t("about.license", lang), Text(LICENSE_NAME)),
                (t("about.build_date", lang), Text(BUILD_DATE)),
            ]
        )
        console.print(facts)
        console.newline()
        console.print(Text(REPOSITORY_URL, style="gl.muted"))
        console.print(Text(APP_TAGLINE, style="gl.muted"))
        await self.pause()
