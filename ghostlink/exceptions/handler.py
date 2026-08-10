"""Central exception rendering.

``render_exception`` is the single place where escaped exceptions become
user-facing output. It deliberately uses only Rich's built-in palette (no
``gl.*`` theme tokens) so it stays safe even when the failure is the theme
engine itself.
"""

from __future__ import annotations

from rich.console import Console, Group
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text

from ghostlink.exceptions.base import ExitCode, GhostLinkError


def exit_code_for(error: BaseException) -> ExitCode:
    """Map an exception to the process exit code it should produce."""

    if isinstance(error, GhostLinkError):
        return error.exit_code
    if isinstance(error, KeyboardInterrupt):
        return ExitCode.INTERRUPTED
    return ExitCode.GENERAL


def _build_body(error: BaseException, *, unexpected: bool) -> Group:
    parts: list[Text] = []

    if unexpected:
        parts.append(Text(f"{type(error).__name__}: {error}", style="bright_white"))
        parts.append(
            Text(
                "This is an unexpected defect. Please report it with the log file attached.",
                style="yellow",
            )
        )
    elif isinstance(error, GhostLinkError):
        parts.append(Text(error.message, style="bright_white"))
        if error.hint:
            parts.append(Text(f"Hint — {error.hint}", style="yellow"))
    else:
        parts.append(Text(str(error) or repr(error), style="bright_white"))

    if error.__cause__ is not None:
        parts.append(Text(f"Caused by — {error.__cause__}", style="grey62"))

    return Group(*parts)


def render_exception(
    console: Console,
    error: BaseException,
    *,
    debug: bool = False,
) -> ExitCode:
    """Render ``error`` to ``console`` and return its exit code.

    When ``debug`` is enabled, a full Rich traceback follows the panel so
    developers keep maximum context without harming the default experience.
    """

    code = exit_code_for(error)
    unexpected = not isinstance(error, GhostLinkError)

    if isinstance(error, GhostLinkError):
        title = error.error_title
    elif isinstance(error, KeyboardInterrupt):
        title = "Interrupted"
    else:
        title = "Unexpected error"

    console.print()
    console.print(
        Panel(
            _build_body(error, unexpected=unexpected),
            title=f"[bold bright_red]✕ {escape(title)}[/]",
            subtitle=f"[grey62]exit code {int(code)}[/]",
            border_style="bright_red",
            padding=(1, 2),
        )
    )

    if debug and unexpected and not isinstance(error, KeyboardInterrupt):
        console.print_exception(show_locals=False)

    return code
