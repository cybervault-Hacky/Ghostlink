"""Theme engine failures."""

from __future__ import annotations

from ghostlink.exceptions.base import ExitCode, GhostLinkError


class ThemeNotFoundError(GhostLinkError):
    """A theme name was requested that the theme engine does not register."""

    exit_code = ExitCode.UI
    error_title = "Unknown theme"
