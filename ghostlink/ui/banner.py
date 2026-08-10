"""Startup banner renderer.

The banner adapts to the terminal: the full ANSI-shadow wordmark on wide
displays, a stacked variant on medium ones, and a clean spaced wordmark on
narrow Termux screens. Lines are colorised with a per-character horizontal
gradient derived from the active theme.
"""

from __future__ import annotations

from rich.align import Align
from rich.console import Group
from rich.rule import Rule
from rich.text import Text

from ghostlink.assets.branding import (
    LOGO_LARGE,
    LOGO_MIN_WIDTH,
    LOGO_STACKED,
    LOGO_STACKED_MIN_WIDTH,
    WORDMARK,
)
from ghostlink.constants.app import APP_TAGLINE
from ghostlink.ui.console import ConsoleManager
from ghostlink.ui.gradients import mix_colors


class BannerRenderer:
    """Renders the GhostLink wordmark through the active theme."""

    def __init__(self, console: ConsoleManager) -> None:
        self._console = console

    # -------------------------------------------------------------- art selection

    def select_art(self) -> tuple[str, ...] | str:
        """Choose the logo variant that fits the current terminal width."""

        width = self._console.width
        if width >= LOGO_MIN_WIDTH + 4:
            return LOGO_LARGE
        if width >= LOGO_STACKED_MIN_WIDTH + 4:
            return LOGO_STACKED
        return WORDMARK

    # --------------------------------------------------------------- rendering

    def _gradient_text(self, lines: tuple[str, ...]) -> Text:
        theme = self._console.theme
        gradient_start, gradient_end = theme.gradient
        text = Text()
        total_lines = len(lines)
        for line_index, line in enumerate(lines):
            width = max(len(line), 1)
            for char_index, char in enumerate(line):
                if char == " ":
                    text.append(char)
                    continue
                across = char_index / width
                down = line_index / max(total_lines - 1, 1) * 0.25
                ratio = min(1.0, across * 0.85 + down)
                text.append(char, style=f"bold {mix_colors(gradient_start, gradient_end, ratio)}")
            if line_index < total_lines - 1:
                text.append("\n")
        return text

    def wordmark(self) -> Text:
        """Render the size-appropriate wordmark, gradient-coloured when possible."""

        art = self.select_art()
        if self._console.no_color:
            plain = art if isinstance(art, str) else "\n".join(art)
            return Text(plain)
        if isinstance(art, str):
            return self._gradient_text((art,))
        return self._gradient_text(art)

    def tagline(self, text: str | None = None) -> Text:
        return Text(APP_TAGLINE if text is None else text, style="gl.muted", justify="center")

    def hero(self, *extras: Text, tagline: str | None = None) -> Group:
        """The full centered hero block: wordmark, tagline, and optional rows."""

        centered_logo = Align.center(self.wordmark())
        parts: list[object] = [centered_logo, "", Align.center(self.tagline(tagline))]
        for extra in extras:
            parts.append("")
            parts.append(Align.center(extra))
        return Group(*parts)  # type: ignore[arg-type]

    def compact_header(self, *, subtitle: str | None = None) -> Group:
        """Two-line header used when returning to the menu from a sub-screen."""

        theme = self._console.theme
        wordmark = Text(WORDMARK.replace(" ", ""), style=f"bold {theme.primary}")
        header = Text()
        header.append_text(wordmark)
        if subtitle:
            header.append(f"  ·  {subtitle}", style="gl.muted")
        return Group(header, Rule(style="gl.border"))
