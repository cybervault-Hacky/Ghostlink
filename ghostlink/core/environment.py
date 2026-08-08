"""Automatic environment detection.

Detection is split into small pure classifiers (fully unit-testable) and a
single :meth:`EnvironmentDetector.detect` entrypoint that probes the real
system: platform (Linux / Termux), the Python runtime, terminal geometry,
TTY availability, and color capability.
"""

from __future__ import annotations

import os
import platform
import shutil
import sys
from collections.abc import Mapping

from ghostlink.constants.files import (
    ENV_COLORTERM,
    ENV_HOME,
    ENV_NO_COLOR,
    ENV_PREFIX,
    ENV_TERM,
    ENV_TERMUX_VERSION,
    TERMUX_PREFIX_MARKER,
)
from ghostlink.models.environment import ColorSupport, EnvironmentInfo, PlatformKind

FALLBACK_COLUMNS = 80
FALLBACK_ROWS = 24


class EnvironmentDetector:
    """Produces an :class:`EnvironmentInfo` snapshot of the current machine."""

    @staticmethod
    def classify_platform(env: Mapping[str, str], system: str) -> PlatformKind:
        """Classify the platform from its environment markers and system name."""

        termux_markers = (
            env.get(ENV_TERMUX_VERSION, ""),
            env.get(ENV_PREFIX, ""),
            env.get(ENV_HOME, ""),
        )
        if any(TERMUX_PREFIX_MARKER in marker for marker in termux_markers) or env.get(
            ENV_TERMUX_VERSION
        ):
            return PlatformKind.TERMUX
        if system == "Linux":
            return PlatformKind.LINUX
        return PlatformKind.UNSUPPORTED

    @staticmethod
    def classify_color_support(env: Mapping[str, str]) -> ColorSupport:
        """Determine terminal color capability from standard environment hints."""

        if ENV_NO_COLOR in env or env.get(ENV_TERM, "") == "dumb":
            return ColorSupport.NONE
        if env.get(ENV_COLORTERM, "").lower() in {"truecolor", "24bit"}:
            return ColorSupport.TRUECOLOR
        if "256color" in env.get(ENV_TERM, ""):
            return ColorSupport.EXTENDED
        return ColorSupport.BASIC

    @staticmethod
    def detect(*, env: Mapping[str, str] | None = None) -> EnvironmentInfo:
        """Probe the running system and return its environment snapshot."""

        environ = os.environ if env is None else env
        size = shutil.get_terminal_size(fallback=(FALLBACK_COLUMNS, FALLBACK_ROWS))
        return EnvironmentInfo(
            platform=EnvironmentDetector.classify_platform(environ, platform.system()),
            system=platform.system(),
            release=platform.release(),
            machine=platform.machine() or "unknown",
            python_version=platform.python_version(),
            python_version_info=sys.version_info[:3],
            python_implementation=platform.python_implementation(),
            terminal_columns=size.columns,
            terminal_rows=size.lines,
            stdin_is_tty=sys.stdin.isatty(),
            stdout_is_tty=sys.stdout.isatty(),
            color_support=EnvironmentDetector.classify_color_support(environ),
        )
