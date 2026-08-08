"""CLI entrypoint: parse, bootstrap, run, and handle every failure mode.

Exit semantics:

* 0   — clean run
* 2   — argument/config validation errors (argparse and config loader)
* 3   — environment problems reported by ``--doctor``
* 130 — interrupted with Ctrl+C outside the menu loop
* 1   — unexpected failure (rendered panel, log contains the traceback)
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence

from ghostlink.cli.arguments import parse_args
from ghostlink.cli.commands import run_command
from ghostlink.cli.doctor import run_doctor
from ghostlink.constants.files import ENV_NO_COLOR
from ghostlink.core.bootstrap import build_application
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.base import ExitCode, GhostLinkError
from ghostlink.exceptions.handler import render_exception
from ghostlink.ui.console import ConsoleManager
from ghostlink.ui.themes import ThemeEngine


def _fallback_console(*, no_color_flag: bool = False) -> ConsoleManager:
    """A theme-carrying console safe to build even when bootstrap failed."""

    engine = ThemeEngine()
    return ConsoleManager(
        engine.get(engine.default_name),
        no_color=no_color_flag or ENV_NO_COLOR in os.environ,
    )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the GhostLink CLI and return the process exit code."""

    options = parse_args(argv)
    debug_hint = bool(options.debug)
    try:
        if options.command is not None:
            return run_command(options)
        if options.doctor:
            return run_doctor(options)

        application = build_application(options)
        return asyncio.run(application.run())

    except GhostLinkError as exc:
        get_logger("cli.entrypoint").error("%s", exc)
        console = _fallback_console(no_color_flag=options.no_color)
        return int(render_exception(console.console, exc, debug=debug_hint))
    except KeyboardInterrupt:
        console = _fallback_console(no_color_flag=options.no_color)
        console.newline()
        console.console.print("[grey62]Interrupted — goodbye.[/]")
        return int(ExitCode.INTERRUPTED)
    except Exception as exc:
        get_logger("cli.entrypoint").exception("Unexpected failure: %s", exc)
        console = _fallback_console(no_color_flag=options.no_color)
        return int(render_exception(console.console, exc, debug=debug_hint))
