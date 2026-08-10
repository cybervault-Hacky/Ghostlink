"""Console management.

:class:`ConsoleManager` owns the application's single Rich console: it wires
the active theme in, centralises colour policy (``--no-color`` / unsupported
terminals), and exposes the small set of operations screens rely on. Width
and record mode are injectable so tests and asset generation share the exact
rendering pipeline used interactively.
"""

from __future__ import annotations

from typing import IO, Any

from rich.console import Console, RenderableType

from ghostlink.models.theme import ThemeSpec
from ghostlink.ui.themes import ThemeEngine


class ConsoleManager:
    """The one console every screen renders through."""

    def __init__(
        self,
        theme: ThemeSpec,
        *,
        no_color: bool = False,
        record: bool = False,
        width: int | None = None,
        height: int | None = None,
        force_terminal: bool | None = None,
        file: IO[str] | None = None,
        stderr: bool = False,
    ) -> None:
        self._theme = theme
        self.console = Console(
            theme=ThemeEngine.rich_theme(theme),
            no_color=no_color,
            record=record,
            width=width,
            height=height,
            force_terminal=force_terminal,
            file=file,
            stderr=stderr,
            highlight=False,
            soft_wrap=False,
        )

    @property
    def theme(self) -> ThemeSpec:
        return self._theme

    @property
    def width(self) -> int:
        return self.console.width

    @property
    def height(self) -> int:
        return self.console.height

    @property
    def is_terminal(self) -> bool:
        return self.console.is_terminal

    @property
    def no_color(self) -> bool:
        return self.console.no_color

    def set_theme(self, theme: ThemeSpec) -> None:
        """Switch the active color theme at runtime."""

        self._theme = theme
        self.console.push_theme(ThemeEngine.rich_theme(theme))

    # ------------------------------------------------------------- operations

    def print(self, *renderables: RenderableType, **kwargs: Any) -> None:
        self.console.print(*renderables, **kwargs)

    def newline(self, count: int = 1) -> None:
        self.console.line(count)

    def clear(self) -> None:
        if self.console.is_terminal:
            self.console.clear()

    def rule(self, title: str = "", *, style: str = "gl.border") -> None:
        self.console.rule(title, style=style)

    def export_text(self, *, styles: bool = False) -> str:
        return self.console.export_text(styles=styles)
