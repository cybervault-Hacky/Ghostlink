"""TOML configuration serializer and atomic persistence."""

from __future__ import annotations

import tomllib
from contextlib import suppress
from pathlib import Path

from ghostlink.config.loader import validate_sections
from ghostlink.exceptions.config import ConfigurationError
from ghostlink.models.settings import AppSettings
from ghostlink.utils.paths import ensure_directory


def _escape_toml_string(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{escaped}"'


def serialize_settings(settings: AppSettings, *, config_version: int = 1) -> str:
    """Serialize an :class:`AppSettings` instance into a valid TOML document."""

    meta_version = settings.meta.config_version if hasattr(settings, "meta") else config_version
    onboarding = settings.meta.onboarding_completed if hasattr(settings, "meta") else False

    lines: list[str] = [
        "# ─────────────────────────────────────────────────────────────────────────────",
        "#  GhostLink — configuration",
        "#",
        "#  Values are validated at startup; command-line flags override file settings.",
        "# ─────────────────────────────────────────────────────────────────────────────",
        "",
        "[meta]",
        f"config_version = {meta_version}",
        f"onboarding_completed = {str(onboarding).lower()}",
        "",
        "[ui]",
        f"theme = {_escape_toml_string(settings.ui.theme)}",
        f"language = {_escape_toml_string(settings.ui.language)}",
        "",
        "[notifications]",
        f"enabled = {str(settings.notifications.enabled).lower()}",
        f"messages = {str(settings.notifications.messages).lower()}",
        f"room_activity = {str(settings.notifications.room_activity).lower()}",
        f"invites = {str(settings.notifications.invites).lower()}",
        f"sound = {str(settings.notifications.sound).lower()}",
        f"vibration = {str(settings.notifications.vibration).lower()}",
        "",
        "[storage]",
        f"data_dir = {_escape_toml_string(settings.storage.data_dir)}",
        f"auto_clean_temp = {str(settings.storage.auto_clean_temp).lower()}",
        f"max_cache_mb = {int(settings.storage.max_cache_mb)}",
        "",
        "[diagnostics]",
        f"debug = {str(settings.diagnostics.debug).lower()}",
        "",
        "[relay]",
        f"url = {_escape_toml_string(settings.relay.url)}",
        f"connect_timeout_seconds = {float(settings.relay.connect_timeout_seconds)}",
        f"handshake_timeout_seconds = {float(settings.relay.handshake_timeout_seconds)}",
        f"heartbeat_interval_seconds = {float(settings.relay.heartbeat_interval_seconds)}",
        f"heartbeat_timeout_seconds = {float(settings.relay.heartbeat_timeout_seconds)}",
        f"reconnect_attempts = {int(settings.relay.reconnect_attempts)}",
        f"reconnect_base_delay_seconds = {float(settings.relay.reconnect_base_delay_seconds)}",
        "",
        "[rooms]",
        f"default_lifetime_minutes = {int(settings.rooms.default_lifetime_minutes)}",
        "",
        "[invites]",
        f"default_lifetime_minutes = {int(settings.invites.default_lifetime_minutes)}",
        f"one_time = {str(settings.invites.one_time).lower()}",
        f"default_expiry_seconds = {int(settings.invites.default_expiry_seconds)}",
        f"max_expiry_seconds = {int(settings.invites.max_expiry_seconds)}",
        f"retention_hours = {int(settings.invites.retention_hours)}",
        "",
        "[chat]",
        f"display_name = {_escape_toml_string(settings.chat.display_name)}",
        f"read_receipts = {str(settings.chat.read_receipts).lower()}",
        f"typing_indicators = {str(settings.chat.typing_indicators).lower()}",
        f"presence = {str(settings.chat.presence).lower()}",
        f"history_mode = {_escape_toml_string(settings.chat.history_mode)}",
        f"timestamp_format = {_escape_toml_string(settings.chat.timestamp_format)}",
        f"notification_style = {_escape_toml_string(settings.chat.notification_style)}",
        f"message_wrapping = {str(settings.chat.message_wrapping).lower()}",
        f"cleanup_on_exit = {str(settings.chat.cleanup_on_exit).lower()}",
        "",
        "[transfer]",
        f"download_dir = {_escape_toml_string(settings.transfer.download_dir)}",
        f"max_file_size_mb = {int(settings.transfer.max_file_size_mb)}",
        f"max_concurrent_transfers = {int(settings.transfer.max_concurrent_transfers)}",
        f"chunk_size_kb = {int(settings.transfer.chunk_size_kb)}",
        f"ack_timeout_seconds = {float(settings.transfer.ack_timeout_seconds)}",
        f"retry_limit = {int(settings.transfer.retry_limit)}",
        f"transfer_expiry_minutes = {int(settings.transfer.transfer_expiry_minutes)}",
        f"temp_storage_limit_mb = {int(settings.transfer.temp_storage_limit_mb)}",
        "",
    ]
    return "\n".join(lines)


def save_config_file(path: Path, settings: AppSettings) -> None:
    """Validate and atomically write an :class:`AppSettings` document to disk."""

    target = path.expanduser()
    ensure_directory(target.parent, error_type=ConfigurationError)
    raw = serialize_settings(settings)

    # Pre-validate TOML syntax & schema before touching the file
    try:
        parsed = tomllib.loads(raw)
        validate_sections(parsed, source=str(target))
    except Exception as exc:
        raise ConfigurationError(
            f"Generated configuration is invalid: {exc}",
            hint="Please review the setting values before saving.",
        ) from exc

    temp_path = target.with_suffix(f".tmp.{target.name}")
    try:
        temp_path.write_text(raw, encoding="utf-8")
        temp_path.replace(target)
    except OSError as exc:
        if temp_path.exists():
            with suppress(OSError):
                temp_path.unlink()
        raise ConfigurationError(
            f"Could not save configuration to '{target}': {exc.strerror or exc}",
            hint="Check file write permissions for the current user.",
        ) from exc
