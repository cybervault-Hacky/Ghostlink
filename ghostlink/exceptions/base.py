"""Base exception type and process exit codes for GhostLink."""

from __future__ import annotations

from enum import IntEnum
from typing import ClassVar


class ExitCode(IntEnum):
    """Deterministic process exit codes returned by the CLI entrypoint."""

    OK = 0
    GENERAL = 1
    CONFIGURATION = 2
    ENVIRONMENT = 3
    STORAGE = 4
    UI = 5
    NETWORK = 6
    INVITE = 7
    GROUP = 8
    INTERRUPTED = 130


class GhostLinkError(Exception):
    """Base class for every expected, user-facing GhostLink failure.

    Parameters
    ----------
    message:
        A concise, single-sentence description of what went wrong.
    hint:
        Optional actionable guidance that tells the user how to recover
        (for example, which file to inspect or which flag to pass).
    """

    exit_code: ClassVar[ExitCode] = ExitCode.GENERAL
    error_title: ClassVar[str] = "GhostLink error"

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:
        return self.message
