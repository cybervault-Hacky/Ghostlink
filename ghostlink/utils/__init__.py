"""Shared utilities: platform paths, text helpers, and duration formatting."""

from __future__ import annotations

from ghostlink.utils.paths import (
    default_config_dir,
    default_data_dir,
    ensure_directory,
    xdg_config_home,
    xdg_data_home,
)
from ghostlink.utils.text import format_duration, pluralize, truncate

__all__ = [
    "default_config_dir",
    "default_data_dir",
    "ensure_directory",
    "format_duration",
    "pluralize",
    "truncate",
    "xdg_config_home",
    "xdg_data_home",
]
