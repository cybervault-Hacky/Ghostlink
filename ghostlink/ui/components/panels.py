"""Panel primitives — bordered containers with consistent chrome."""

from __future__ import annotations

from rich.console import RenderableType
from rich.markup import escape
from rich.panel import Panel

from ghostlink.ui.components.badges import BadgeTone


def app_panel(
    renderable: RenderableType,
    *,
    title: str | None = None,
    subtitle: str | None = None,
    tone: BadgeTone = BadgeTone.ACCENT,
    padding: tuple[int, int] = (1, 2),
    expand: bool = True,
) -> Panel:
    """A panel carrying GhostLink chrome: themed border, styled title."""

    return Panel(
        renderable,
        title=f"[gl.{tone.value}]{escape(title)}[/]" if title else None,
        subtitle=f"[gl.muted]{escape(subtitle)}[/]" if subtitle else None,
        border_style=f"gl.{tone.value}" if tone is not BadgeTone.MUTED else "gl.border",
        padding=padding,
        expand=expand,
    )


def section_panel(
    title: str,
    renderable: RenderableType,
    *,
    subtitle: str | None = None,
    padding: tuple[int, int] = (1, 2),
) -> Panel:
    """A neutral full-screen section panel used by every sub-screen."""

    return Panel(
        renderable,
        title=f"[gl.title]{escape(title)}[/]",
        subtitle=f"[gl.muted]{escape(subtitle)}[/]" if subtitle else None,
        border_style="gl.border",
        padding=padding,
        expand=True,
    )
