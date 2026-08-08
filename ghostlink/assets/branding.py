"""ASCII branding art for GhostLink.

The wordmark is rendered in an "ANSI Shadow" style from per-letter glyph
blocks. Letters are composed programmatically so rows always align, and three
size variants let the banner adapt gracefully to narrow Termux displays.
"""

from __future__ import annotations

from importlib import resources

from ghostlink.constants.files import DEFAULT_CONFIG_RESOURCE

_LETTER_G = (
    " ██████╗ ",
    "██╔════╝ ",
    "██║  ███╗",
    "██║   ██║",
    "╚██████╔╝",
    " ╚═════╝ ",
)

_LETTER_H = (
    "██╗  ██╗",
    "██║  ██║",
    "███████║",
    "██╔══██║",
    "██║  ██║",
    "╚═╝  ╚═╝",
)

_LETTER_O = (
    " ██████╗ ",
    "██╔═══██╗",
    "██║   ██║",
    "██║   ██║",
    "╚██████╔╝",
    " ╚═════╝ ",
)

_LETTER_S = (
    "███████╗",
    "██╔════╝",
    "███████╗",
    "╚════██║",
    "███████║",
    "╚══════╝",
)

_LETTER_T = (
    "████████╗",
    "╚══██╔══╝",
    "   ██║   ",
    "   ██║   ",
    "   ██║   ",
    "   ╚═╝   ",
)

_LETTER_L = (
    "██╗     ",
    "██║     ",
    "██║     ",
    "██║     ",
    "███████╗",
    "╚══════╝",
)

_LETTER_I = (
    "██╗",
    "██║",
    "██║",
    "██║",
    "██║",
    "╚═╝",
)

_LETTER_N = (
    "███╗   ██╗",
    "████╗  ██║",
    "██╔██╗ ██║",
    "██║╚██╗██║",
    "██║ ╚████║",
    "╚═╝  ╚═══╝",
)

_LETTER_K = (
    "██╗  ██╗",
    "██║ ██╔╝",
    "█████╔╝ ",
    "██╔═██╗ ",
    "██║  ██╗",
    "╚═╝  ╚═╝",
)

_GHOST_EMBLEM_LINES = (
    " ▄█████▄ ",
    " ███████ ",
    " ██▀█▀██ ",
    " ███████ ",
    " █▀█▀█▀█ ",
)


def _compose(*letters: tuple[str, ...]) -> tuple[str, ...]:
    """Join letter glyph blocks row-wise, padding each letter to equal width."""

    composed: list[str] = []
    for row_index in range(6):
        segments = [letter[row_index].ljust(max(map(len, letter))) for letter in letters]
        composed.append(" ".join(segments).rstrip())
    return tuple(composed)


LOGO_LARGE: tuple[str, ...] = _compose(
    _LETTER_G,
    _LETTER_H,
    _LETTER_O,
    _LETTER_S,
    _LETTER_T,
    _LETTER_L,
    _LETTER_I,
    _LETTER_N,
    _LETTER_K,
)
"""Full 'GHOSTLINK' wordmark, six rows tall, for wide terminals."""

LOGO_MIN_WIDTH: int = max(len(line) for line in LOGO_LARGE)

LOGO_STACKED: tuple[str, ...] = (
    *_compose(_LETTER_G, _LETTER_H, _LETTER_O, _LETTER_S, _LETTER_T),
    "",
    *_compose(_LETTER_L, _LETTER_I, _LETTER_N, _LETTER_K),
)
"""'GHOST' stacked above 'LINK' for medium-width terminals."""

LOGO_STACKED_MIN_WIDTH: int = max(len(line) for line in LOGO_STACKED)

WORDMARK: str = "G H O S T L I N K"
"""Plain spaced wordmark for very narrow terminals."""

GHOST_EMBLEM: tuple[str, ...] = _GHOST_EMBLEM_LINES
"""Small ghost mascot used on the About screen."""


def load_default_config_text() -> str:
    """Read the packaged default configuration template."""

    return (
        resources.files("ghostlink.assets")
        .joinpath(DEFAULT_CONFIG_RESOURCE)
        .read_text(encoding="utf-8")
    )
