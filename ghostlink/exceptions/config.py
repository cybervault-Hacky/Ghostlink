"""Configuration-related failures."""

from __future__ import annotations

from ghostlink.exceptions.base import ExitCode, GhostLinkError


class ConfigurationError(GhostLinkError):
    """The configuration could not be located, read, or applied."""

    exit_code = ExitCode.CONFIGURATION
    error_title = "Configuration error"


class ConfigParseError(ConfigurationError):
    """The configuration file exists but is not valid TOML."""

    error_title = "Configuration parse error"


class ConfigValidationError(ConfigurationError):
    """The configuration file parsed, but contains invalid keys or values."""

    error_title = "Configuration validation error"
