"""Application runtime.

:class:`Application` owns the session lifecycle: it opens a session through
the injected :class:`SessionService`, runs the home screen loop, and shuts
down gracefully — including on Ctrl+C — recording session statistics on the
way out. ``run`` is a coroutine so Phase 2's transport lifecycle integrates
without restructuring the entrypoint.
"""

from __future__ import annotations

from rich.align import Align
from rich.text import Text

from ghostlink.core.logging import get_logger
from ghostlink.exceptions.base import ExitCode
from ghostlink.models.session import SessionInfo
from ghostlink.services.session import SessionService
from ghostlink.ui.components.badges import BadgeTone
from ghostlink.ui.components.panels import app_panel
from ghostlink.ui.screens.base import ScreenContext
from ghostlink.ui.screens.home import HomeScreen
from ghostlink.utils.text import format_duration


class Application:
    """The top-level runtime driving screens and the session lifecycle."""

    def __init__(self, context: ScreenContext, session_service: SessionService) -> None:
        self._context = context
        self._session_service = session_service
        self._logger = get_logger("core.application")

    async def run(self) -> int:
        """Run the application and return the process exit code."""

        session = await self._session_service.start()
        self._logger.info(
            "%s started — %s, Python %s",
            "GhostLink",
            self._context.environment.platform_label,
            self._context.environment.python_version,
        )

        interrupted = False
        try:
            await HomeScreen(self._context).show()
        except KeyboardInterrupt:
            interrupted = True
            self._logger.info("Interrupted by user (Ctrl+C).")
        finally:
            await self._session_service.stop(session)

        self._render_farewell(session, interrupted=interrupted)
        self._logger.info("Application shutdown complete.")
        return int(ExitCode.INTERRUPTED if interrupted else ExitCode.OK)

    def _render_farewell(self, session: SessionInfo, *, interrupted: bool) -> None:
        console = self._context.console
        uptime = format_duration(session.uptime())
        console.clear()
        console.newline()
        title = "Session interrupted" if interrupted else "Session closed"
        body = Text.assemble(
            ("GhostLink ran for ", "gl.text"),
            (uptime, "gl.highlight"),
            (".\n", "gl.text"),
            ("Stay invisible.", "gl.muted"),
        )
        console.print(
            Align.center(
                app_panel(
                    Align.center(body),
                    title=title,
                    tone=BadgeTone.MUTED,
                    padding=(1, 3),
                    expand=False,
                )
            )
        )
        console.newline()
