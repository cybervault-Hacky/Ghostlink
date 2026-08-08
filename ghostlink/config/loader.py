"""TOML configuration loading, merging, and structural validation.

The loader is deliberately strict: unknown sections or keys are rejected with
a list of what *is* allowed, which catches typos early instead of silently
ignoring them. Semantic value validation happens in the settings models.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from ghostlink.exceptions.config import ConfigParseError, ConfigValidationError

ALLOWED_SCHEMA: dict[str, frozenset[str]] = {
    "ui": frozenset({"theme", "language"}),
    "notifications": frozenset({"enabled"}),
    "storage": frozenset({"data_dir"}),
    "diagnostics": frozenset({"debug"}),
    "relay": frozenset(
        {
            "url",
            "connect_timeout_seconds",
            "handshake_timeout_seconds",
            "heartbeat_interval_seconds",
            "heartbeat_timeout_seconds",
            "reconnect_attempts",
            "reconnect_base_delay_seconds",
        }
    ),
    "rooms": frozenset({"default_lifetime_minutes"}),
    "invites": frozenset(
        {
            "default_lifetime_minutes",
            "one_time",
            "default_expiry_seconds",
            "max_expiry_seconds",
            "retention_hours",
        }
    ),
    "chat": frozenset(
        {
            "display_name",
            "read_receipts",
            "typing_indicators",
            "history_mode",
            "timestamp_format",
            "notification_style",
            "message_wrapping",
        }
    ),
    "transfer": frozenset(
        {
            "download_dir",
            "max_file_size_mb",
            "max_concurrent_transfers",
            "chunk_size_kb",
            "ack_timeout_seconds",
            "retry_limit",
            "transfer_expiry_minutes",
            "temp_storage_limit_mb",
        }
    ),
}
"""The complete set of configuration sections and the keys each accepts."""


def load_config_file(path: Path) -> dict[str, Any]:
    """Parse a TOML file into a dictionary, wrapping failures precisely."""

    if not path.is_file():
        raise ConfigParseError(
            f"Configuration file '{path}' does not exist.",
            hint="Launch GhostLink once to generate it, or pass a valid path with --config.",
        )
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigParseError(
            f"Configuration file '{path}' is not valid TOML: {exc}.",
            hint="Fix the reported line, or delete the file to have GhostLink "
            "regenerate it on next launch.",
        ) from exc
    except OSError as exc:
        raise ConfigParseError(
            f"Configuration file '{path}' could not be read: {exc.strerror or exc}.",
            hint="Check file permissions for the current user.",
        ) from exc
    if not isinstance(data, dict):
        raise ConfigParseError(
            f"Configuration file '{path}' must contain a TOML table at its root.",
            hint="Regenerate a clean file by deleting it and relaunching.",
        )
    return data


def validate_sections(
    data: dict[str, Any],
    *,
    source: str,
    schema: dict[str, frozenset[str]] | None = None,
) -> dict[str, dict[str, Any]]:
    """Reject unknown sections/keys and return the recognised section tables."""

    allowed = ALLOWED_SCHEMA if schema is None else schema
    cleaned: dict[str, dict[str, Any]] = {}

    for section, values in data.items():
        if section not in allowed:
            known = ", ".join(sorted(allowed))
            raise ConfigValidationError(
                f"{source}: unknown configuration section '[{section}]'.",
                hint=f"Valid sections are: {known}.",
            )
        if not isinstance(values, dict):
            raise ConfigValidationError(
                f"{source}: section '[{section}]' must be a TOML table.",
                hint=f"Write it as '[{section}]' followed by key = value lines.",
            )
        unknown_keys = sorted(set(values) - allowed[section])
        if unknown_keys:
            offenders = ", ".join(unknown_keys)
            known = ", ".join(sorted(allowed[section]))
            raise ConfigValidationError(
                f"{source}: unknown key(s) {offenders} in section '[{section}]'.",
                hint=f"This section accepts: {known}.",
            )
        cleaned[section] = dict(values)
    return cleaned


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge ``override`` onto ``base`` without mutating either."""

    merged = dict(base)
    for key, value in override.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = deep_merge(existing, value)
        else:
            merged[key] = value
    return merged
