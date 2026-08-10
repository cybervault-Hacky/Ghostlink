"""Status badges — compact labelled chips like ``[ ✓ ENABLED ]``."""

from __future__ import annotations

from enum import Enum

from rich.text import Text

from ghostlink.models.theme import ThemeSpec
from ghostlink.ui.components.layout import (
    GLYPH_ACCENT,
    GLYPH_ERROR,
    GLYPH_INFO,
    GLYPH_MUTED,
    GLYPH_SUCCESS,
    GLYPH_WARNING,
)


class BadgeTone(Enum):
    """Semantic badge intents mapped onto theme palette tokens."""

    ACCENT = "accent"
    SUCCESS = "success"
    WARNING = "warning"
    ERROR = "error"
    INFO = "info"
    MUTED = "muted"


_TONE_ICONS: dict[BadgeTone, str] = {
    BadgeTone.ACCENT: GLYPH_ACCENT,
    BadgeTone.SUCCESS: GLYPH_SUCCESS,
    BadgeTone.WARNING: GLYPH_WARNING,
    BadgeTone.ERROR: GLYPH_ERROR,
    BadgeTone.INFO: GLYPH_INFO,
    BadgeTone.MUTED: GLYPH_MUTED,
}


def badge(
    text: str,
    tone: BadgeTone = BadgeTone.ACCENT,
    *,
    theme: ThemeSpec | None = None,
    icon: str | None = None,
) -> Text:
    """Build a badge.

    With a ``theme`` the badge renders as a filled chip using the tone color
    as background. Without one it renders as bracketed text using the
    semantic ``gl.*`` style — useful where only a Rich console is available.
    """

    glyph = _TONE_ICONS[tone] if icon is None else icon
    label = f"{glyph} {text}" if glyph else text
    if theme is not None:
        color = getattr(theme, tone.value)
        return Text(f" {label} ", style=f"bold {theme.on_accent} on {color}")
    return Text(f"[{label}]", style=f"bold gl.{tone.value}")


def badge_row(*badges: Text, separator: str = "  ") -> Text:
    """Join several badges into one line."""

    row = Text()
    for index, item in enumerate(badges):
        if index:
            row.append(separator)
        row.append_text(item)
    return row
