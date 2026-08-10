"""Creator credit block.

A small, theme-aware footer identifying GhostLink's creator. It is rendered
subtly on the home screen and in final success/completion output. The block is
pure text (no ANSI art, no animations, no extra dependencies) so it renders
correctly in Termux, on desktop Linux, and in narrow terminals while honouring
``--no-color`` through the existing Rich theme.
"""

from __future__ import annotations

from rich.align import Align
from rich.console import Group, RenderableType
from rich.text import Text

CREATOR_NAME = "Sarthak Bharambe"
CREATOR_YOUTUBE = "Cyber Vault"
CREATOR_INSTAGRAM = "@cyber_vault123"

_CREATOR_LINE = f"Created by {CREATOR_NAME}"
_YOUTUBE_LINE = f"YouTube — {CREATOR_YOUTUBE}"
_INSTAGRAM_LINE = f"Instagram — {CREATOR_INSTAGRAM}"


def creator_credits() -> RenderableType:
    """Return a centered, muted creator-credit block.

    Each line is its own :class:`~rich.text.Text` so Rich wraps and aligns
    them correctly even on narrow Termux widths. The palette stays within the
    existing theme (``gl.muted`` with a soft accent on the creator's name) so
    the block reads as intentional chrome rather than debug output.
    """

    name = Text(_CREATOR_LINE, style="gl.muted", justify="center")
    name.highlight_regex(CREATOR_NAME, style="gl.accent")

    youtube = Text(_YOUTUBE_LINE, style="gl.muted", justify="center")
    instagram = Text(_INSTAGRAM_LINE, style="gl.muted", justify="center")

    block = Group(
        Text(" ", end=""),
        name,
        youtube,
        instagram,
    )
    return Align.center(block)
