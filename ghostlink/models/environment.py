"""Models describing the runtime environment GhostLink executes in."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from ghostlink.constants.app import MIN_PYTHON


class PlatformKind(str, Enum):
    """The operating environments GhostLink officially supports."""

    TERMUX = "termux"
    LINUX = "linux"
    UNSUPPORTED = "unsupported"


class ColorSupport(Enum):
    """Color capability of the attached terminal, best to worst."""

    TRUECOLOR = "truecolor"
    EXTENDED = "256-color"
    BASIC = "16-color"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class EnvironmentInfo:
    """An immutable snapshot of the detected runtime environment."""

    platform: PlatformKind
    system: str
    release: str
    machine: str
    python_version: str
    python_version_info: tuple[int, int, int]
    python_implementation: str
    terminal_columns: int
    terminal_rows: int
    stdin_is_tty: bool
    stdout_is_tty: bool
    color_support: ColorSupport

    @property
    def is_termux(self) -> bool:
        return self.platform is PlatformKind.TERMUX

    @property
    def is_linux(self) -> bool:
        return self.platform is PlatformKind.LINUX

    @property
    def is_supported(self) -> bool:
        return self.platform is not PlatformKind.UNSUPPORTED

    @property
    def supported_python(self) -> bool:
        return self.python_version_info[:2] >= MIN_PYTHON

    @property
    def is_interactive(self) -> bool:
        """True when keyboard-driven UI (raw-mode input) can be used."""

        return self.stdin_is_tty and self.stdout_is_tty

    @property
    def platform_label(self) -> str:
        if self.platform is PlatformKind.TERMUX:
            return "Termux (Android)"
        if self.platform is PlatformKind.LINUX:
            return "Linux"
        return self.system or "Unknown"

    @property
    def terminal_size_label(self) -> str:
        return f"{self.terminal_columns}×{self.terminal_rows}"

    @property
    def is_compact_terminal(self) -> bool:
        """True when the terminal is smaller than the comfortable baseline."""

        return self.terminal_columns < 80 or self.terminal_rows < 24
