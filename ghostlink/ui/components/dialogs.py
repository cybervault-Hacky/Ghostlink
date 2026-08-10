"""Dialog components: notices, confirmations, and pauses."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import suppress

from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.prompt import Confirm
from rich.text import Text

from ghostlink.ui.components.badges import BadgeTone
from ghostlink.ui.components.layout import PROMPT_GLYPH, confirm_panel


def notice_dialog(
    title: str,
    message: str,
    *,
    tone: BadgeTone = BadgeTone.INFO,
    hint: str | None = None,
) -> Panel:
    """A bordered notice dialog — informative, warning, or error."""

    parts: list[Text] = [Text(message, style="gl.text")]
    if hint:
        parts.append(Text(f"Hint — {hint}", style="gl.muted"))
    body: Group | Text = Group(*parts) if hint else parts[0]
    return Panel(
        body,
        title=f"[gl.{tone.value}]{escape(title)}[/]",
        border_style=f"gl.{tone.value}",
        padding=(1, 2),
        expand=False,
    )


def roadmap_dialog(
    feature: str,
    *,
    summary: str,
    planned: Sequence[str],
    phase_label: str,
) -> Panel:
    """Explain, honestly, that a feature arrives in a later phase.

    This is communication, not a stub: the panel tells the user exactly what
    the feature will deliver and which milestone ships it.
    """

    lines: list[Text] = [
        Text(summary, style="gl.text"),
        Text(""),
        Text(f"Arriving with {phase_label}:", style="gl.accent"),
    ]
    lines.extend(Text(f"  ·  {item}", style="gl.muted") for item in planned)
    return Panel(
        Group(*lines),
        title=f"[gl.title]{escape(feature)}[/]",
        subtitle=f"[gl.muted]{escape(phase_label)}[/]",
        border_style="gl.border",
        padding=(1, 2),
        expand=False,
    )


def wait_for_enter(
    console: Console,
    *,
    prompt: str = "Press Enter to return…",
) -> None:
    """Block until the user acknowledges. Tolerates closed stdin (EOF)."""

    with suppress(EOFError, OSError):
        console.input(f"\n[gl.muted]{escape(prompt)}[/]")


def prompt_text(
    console: Console,
    question: str,
    *,
    allow_empty: bool = False,
    max_length: int = 128,
) -> str | None:
    """Ask for one line of text.

    Returns the validated input, an empty string when allowed, or ``None``
    when the user cancels (``q``) or stdin is unavailable (EOF/OSError).
    """

    while True:
        try:
            raw = console.input(f"[gl.highlight]{PROMPT_GLYPH} {escape(question)}:[/] ").strip()
        except (EOFError, OSError):
            return None
        if raw.lower() in {"q", "quit", "cancel"}:
            return None
        if not raw and allow_empty:
            return ""
        if not raw:
            console.print("[gl.warning]  Please enter a value, or q to cancel.[/]")
            continue
        if len(raw) > max_length:
            console.print(f"[gl.warning]  Keep it under {max_length} characters.[/]")
            continue
        return raw


def confirm(
    console: Console,
    question: str,
    *,
    default: bool = False,
) -> bool:
    """Ask a yes/no question; returns ``default`` when stdin is unavailable."""

    try:
        return Confirm.ask(f"[gl.text]{escape(question)}[/]", console=console, default=default)
    except (EOFError, OSError):
        return default


def confirm_action(
    console: Console,
    title: str,
    message: str,
    *,
    consequence: str | None = None,
    default: bool = False,
) -> bool:
    """A professional confirmation dialog for consequential actions.

    Prints a calm, bordered explanation panel (``CONFIRM …`` title, plain
    language, optional consequence note) followed by a single Continue?
    prompt. No alarmist styling, no emoji.
    """

    console.print(confirm_panel(title, message, consequence=consequence))
    console.print()
    return confirm(console, "Continue?", default=default)
