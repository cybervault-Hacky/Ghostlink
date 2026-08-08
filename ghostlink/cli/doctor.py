"""``ghostlink --doctor`` — read-only environment diagnostics.

Renders a report covering platform, Python runtime, terminal capabilities,
and configuration resolution, then exits with a status code: 0 when the
environment is fully supported, 3 when something needs attention.
"""

from __future__ import annotations

from rich.console import Group
from rich.text import Text

from ghostlink.cli.arguments import CLIOptions
from ghostlink.config.manager import ConfigOverrides, ConfigurationManager
from ghostlink.constants.app import MIN_PYTHON
from ghostlink.core.environment import EnvironmentDetector
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.base import ExitCode, GhostLinkError
from ghostlink.models.environment import (
    ColorSupport,
    EnvironmentInfo,
    PlatformKind,
)
from ghostlink.models.theme import ThemeSpec
from ghostlink.ui.components.badges import BadgeTone, badge
from ghostlink.ui.components.panels import section_panel
from ghostlink.ui.components.progress import StepProgress
from ghostlink.ui.components.tables import info_table
from ghostlink.ui.console import ConsoleManager
from ghostlink.ui.themes import ThemeEngine


def _python_row(environment: EnvironmentInfo, theme: ThemeSpec) -> tuple[str, Text]:
    label = f"{environment.python_version} ({environment.python_implementation})"
    if environment.supported_python:
        state = badge("Supported", BadgeTone.SUCCESS, theme=theme)
    else:
        state = badge(
            f"Requires ≥ {MIN_PYTHON[0]}.{MIN_PYTHON[1]}",
            BadgeTone.WARNING,
            theme=theme,
        )
    return "Python Runtime", Text.assemble(label, "  ", state)


def run_doctor(options: CLIOptions) -> int:
    """Execute diagnostics and return the process exit code."""

    engine = ThemeEngine()
    environment = EnvironmentDetector.detect()

    config = ConfigurationManager(
        config_path=options.config_path,
        overrides=ConfigOverrides(
            theme=options.theme, data_dir=options.data_dir, debug=options.debug
        ),
    )

    config_error: GhostLinkError | None = None
    try:
        settings = config.load(create_missing=False)
        theme = engine.get(settings.ui.theme)
    except GhostLinkError as exc:
        # Configuration problems are reported as findings; the diagnostic
        # itself keeps rendering through the default theme.
        config_error = exc
        theme = engine.get(engine.default_name)

    console = ConsoleManager(
        theme,
        no_color=options.no_color or environment.color_support is ColorSupport.NONE,
    )
    get_logger("cli.doctor").debug("doctor diagnostics started")

    with StepProgress(console, title="Diagnosing environment…") as progress:
        with progress.step("Probing platform"):
            platform_ok = environment.platform is not PlatformKind.UNSUPPORTED
        with progress.step("Inspecting terminal"):
            terminal_label = environment.terminal_size_label
        with progress.step("Reading configuration"):
            config_exists = config.config_path.is_file()

    config_state = (
        badge("Error", BadgeTone.ERROR, theme=theme)
        if config_error is not None
        else badge("Found", BadgeTone.SUCCESS, theme=theme)
        if config_exists
        else badge("Pending", BadgeTone.INFO, theme=theme)
    )

    rows: list[tuple[str, Text]] = [
        (
            "Platform",
            Text.assemble(
                environment.platform_label,
                f"  ({environment.system} {environment.release}, {environment.machine})",
                "  ",
                badge(
                    "OK" if platform_ok else "Unsupported",
                    BadgeTone.SUCCESS if platform_ok else BadgeTone.WARNING,
                    theme=theme,
                ),
            ),
        ),
        _python_row(environment, theme),
        (
            "Terminal Size",
            Text.assemble(
                terminal_label,
                "  ",
                badge(
                    "Comfortable" if not environment.is_compact_terminal else "Compact",
                    BadgeTone.SUCCESS if not environment.is_compact_terminal else BadgeTone.INFO,
                    theme=theme,
                ),
            ),
        ),
        (
            "Input Mode",
            Text(
                "Keyboard navigation (TTY)"
                if environment.is_interactive
                else "Numbered selection (piped input)"
            ),
        ),
        ("Color Support", Text(environment.color_support.value)),
        (
            "Configuration",
            Text.assemble(config_state, f"  {config.config_path}"),
        ),
        ("Data Directory", Text(str(config.data_dir) if config_error is None else "(unresolved)")),
    ]

    if config_error is not None:
        rows.append(("Finding", Text(str(config_error))))
        if config_error.hint:
            rows.append(("Suggested Fix", Text(config_error.hint)))

    healthy = environment.supported_python and platform_ok and config_error is None
    summary = (
        "Environment is ready — launch with `ghostlink`."
        if healthy
        else "Environment needs attention — review the report above."
    )

    console.newline()
    console.print(
        section_panel(
            "GhostLink Doctor",
            Group(info_table(rows), Text(""), Text(summary, style="gl.muted")),
            subtitle="read-only diagnostics",
        )
    )
    console.newline()

    return int(ExitCode.OK if healthy else ExitCode.ENVIRONMENT)
