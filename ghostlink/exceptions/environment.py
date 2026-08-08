"""Environment-related failures (platform and Python runtime)."""

from __future__ import annotations

from ghostlink.exceptions.base import ExitCode, GhostLinkError


class UnsupportedPlatformError(GhostLinkError):
    """GhostLink was launched on a platform it does not support."""

    exit_code = ExitCode.ENVIRONMENT
    error_title = "Unsupported platform"


class UnsupportedPythonVersionError(UnsupportedPlatformError):
    """The running Python interpreter is older than the supported minimum."""

    error_title = "Unsupported Python version"
