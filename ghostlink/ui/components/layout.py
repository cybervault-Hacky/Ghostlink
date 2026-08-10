"""GhostLink terminal design system — one visual language for every screen.

Every interactive screen is composed from the primitives defined here:

* :func:`page_header` — screen title, optional subtitle, divider rule.
* :func:`section_label` — muted uppercase label grouping related rows.
* :func:`status_text` / :func:`status_line` — consistent ✓ / ! / ✕ / • states.
* :func:`back_label` / :func:`back_entry` — the single Back affordance.

The visual language is deliberately restrained: clean text, uppercase major
headings, thin divider rules, and a small set of terminal-safe glyphs
(› • ◆ ─ │ ✓ ✕ → ! ←). No emoji is part of system UI; every screen stays
readable in monochrome and on narrow terminals.
"""

from __future__ import annotations

from rich.console import Console, Group, RenderableType
from rich.markup import escape
from rich.panel import Panel
from rich.rule import Rule
from rich.text import Text

# --------------------------------------------------------------------- glyphs

GLYPH_SUCCESS = "✓"
GLYPH_WARNING = "!"
GLYPH_ERROR = "✕"
GLYPH_INFO = "•"
GLYPH_ACCENT = "◆"
GLYPH_MUTED = "·"
GLYPH_CURSOR = "›"
GLYPH_BACK = "←"
GLYPH_NEXT = "→"

#: Prompt shown before line input everywhere in the app.
PROMPT_GLYPH = "›"


def back_label(text: str = "Back") -> str:
    """The one Back label used by every menu in the application."""

    return f"{GLYPH_BACK} {text}"


# -------------------------------------------------------------------- header


def page_header(
    title: str,
    subtitle: str | None = None,
    *,
    upper: bool = True,
) -> Group:
    """The standard screen header.

    TITLE
    subtitle
    ───────────────────────────
    """

    heading = Text(title.upper() if upper else title, style="gl.title")
    parts: list[RenderableType] = [heading]
    if subtitle:
        parts.append(Text(subtitle, style="gl.muted"))
    parts.append(Rule(style="gl.border"))
    return Group(*parts)


def section_label(text: str) -> Text:
    """A muted uppercase section marker used to group related rows."""

    return Text(text.upper(), style="gl.accent")


# -------------------------------------------------------------------- status


def status_text(message: str, tone: str) -> Text:
    """A one-line status with the standard glyph for ``tone``.

    ``tone`` is one of ``success`` / ``warning`` / ``error`` / ``info``.
    """

    glyph = {
        "success": GLYPH_SUCCESS,
        "warning": GLYPH_WARNING,
        "error": GLYPH_ERROR,
    }.get(tone, GLYPH_INFO)
    return Text.assemble((f"{glyph} ", f"bold gl.{tone}"), (message, "gl.text"))


def status_line(console: Console, message: str, *, tone: str = "info") -> None:
    """Print a consistent status line (✓ / ! / ✕ / • + message)."""

    console.print(status_text(message, tone))


# -------------------------------------------------------------------- dialogs


def confirm_panel(
    title: str,
    message: str,
    *,
    consequence: str | None = None,
) -> Panel:
    """The body of a confirmation dialog: calm title, explanation, no drama.

    Callers prompt with a plain ``Confirm?`` line right after printing this
    panel (see :func:`ghostlink.ui.components.dialogs.confirm_action`).
    """

    parts: list[Text] = [Text(message, style="gl.text")]
    if consequence:
        parts.append(Text(""))
        parts.append(Text(consequence, style="gl.muted"))
    body: Group | Text = Group(*parts) if consequence else parts[0]
    return Panel(
        body,
        title=f"[gl.warning]{escape(title.upper())}[/]",
        border_style="gl.border",
        padding=(1, 2),
        expand=False,
    )
