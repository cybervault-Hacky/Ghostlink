"""Filesystem locations and environment variable names used by GhostLink."""

from __future__ import annotations

CONFIG_FILE_NAME: str = "config.toml"
DEFAULT_CONFIG_RESOURCE: str = "default_config.toml"

STATE_DIR_NAME: str = "state"
LOGS_DIR_NAME: str = "logs"
LOG_FILE_NAME: str = "ghostlink.log"

ENV_NO_COLOR: str = "NO_COLOR"
ENV_XDG_CONFIG_HOME: str = "XDG_CONFIG_HOME"
ENV_XDG_DATA_HOME: str = "XDG_DATA_HOME"
ENV_TERM: str = "TERM"
ENV_COLORTERM: str = "COLORTERM"
ENV_TERMUX_VERSION: str = "TERMUX_VERSION"
ENV_PREFIX: str = "PREFIX"
ENV_HOME: str = "HOME"

TERMUX_PREFIX_MARKER: str = "com.termux"
