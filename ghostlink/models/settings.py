"""Validated, immutable application settings.

Each settings section is a frozen dataclass with ``__post_init__`` validation
so an invalid value can never exist in memory — construction either succeeds
with a sound object or raises :class:`ConfigValidationError` with precise
field-level feedback.
"""

from __future__ import annotations

from dataclasses import dataclass

from ghostlink.constants.app import (
    CHAT_HISTORY_MODES,
    CHAT_NOTIFICATION_STYLES,
    CHAT_TIMESTAMP_FORMATS,
    DEFAULT_HISTORY_MODE,
    DEFAULT_LANGUAGE,
    DEFAULT_NOTIFICATION_STYLE,
    DEFAULT_THEME,
    DEFAULT_TIMESTAMP_FORMAT,
    SUPPORTED_LANGUAGES,
    TRANSFER_DEFAULT_CHUNK_KB,
    TRANSFER_DEFAULT_CONCURRENT,
    TRANSFER_DEFAULT_EXPIRY_MINUTES,
    TRANSFER_DEFAULT_MAX_FILE_MB,
    TRANSFER_DEFAULT_TEMP_LIMIT_MB,
    TRANSFER_MAX_CHUNK_KB,
    TRANSFER_MAX_CHUNKS_PER_FILE,
    TRANSFER_MAX_CONCURRENT,
    TRANSFER_MAX_FILE_MB,
    TRANSFER_MAX_TEMP_LIMIT_MB,
    TRANSFER_MIN_CHUNK_KB,
    TRANSFER_MIN_TEMP_LIMIT_MB,
)
from ghostlink.constants.net import (
    CONNECT_TIMEOUT_SECONDS,
    DEFAULT_INVITE_LIFETIME_MINUTES,
    DEFAULT_ROOM_LIFETIME_MINUTES,
    HANDSHAKE_TIMEOUT_SECONDS,
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_TIMEOUT_SECONDS,
    INVITE_DEFAULT_TTL_SECONDS,
    INVITE_MAX_TTL_SECONDS,
    INVITE_MIN_TTL_SECONDS,
    INVITE_RETENTION_HOURS,
    RECONNECT_ATTEMPTS,
    RECONNECT_BASE_DELAY_SECONDS,
)
from ghostlink.exceptions.config import ConfigValidationError


def _require_non_empty(value: str, *, field: str) -> str:
    cleaned = value.strip()
    if not cleaned:
        raise ConfigValidationError(
            f"Setting '{field}' must not be empty.",
            hint="Provide a non-empty string value in your configuration file.",
        )
    return cleaned


@dataclass(frozen=True, slots=True)
class UISettings:
    """User-interface preferences."""

    theme: str = DEFAULT_THEME
    language: str = DEFAULT_LANGUAGE

    def __post_init__(self) -> None:
        _require_non_empty(self.theme, field="ui.theme")
        if self.language not in SUPPORTED_LANGUAGES:
            supported = ", ".join(sorted(SUPPORTED_LANGUAGES))
            raise ConfigValidationError(
                f"Setting 'ui.language' has unsupported value '{self.language}'.",
                hint=f"Supported languages today: {supported}.",
            )


@dataclass(frozen=True, slots=True)
class NotificationSettings:
    """In-app notification behaviour."""

    enabled: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.enabled, bool):
            raise ConfigValidationError(
                f"Setting 'notifications.enabled' must be true or false, got {self.enabled!r}.",
                hint="Use a TOML boolean: enabled = true",
            )


@dataclass(frozen=True, slots=True)
class StorageSettings:
    """Storage locations. An empty ``data_dir`` selects the platform default."""

    data_dir: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.data_dir, str):
            raise ConfigValidationError(
                "Setting 'storage.data_dir' must be a string path.",
                hint='Use a TOML string: data_dir = "~/ghostlink-data"',
            )


@dataclass(frozen=True, slots=True)
class DiagnosticsSettings:
    """Diagnostics behaviour, surfaced in the UI as 'Debug Mode'."""

    debug: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.debug, bool):
            raise ConfigValidationError(
                f"Setting 'diagnostics.debug' must be true or false, got {self.debug!r}.",
                hint="Use a TOML boolean: debug = false",
            )


@dataclass(frozen=True, slots=True)
class RelaySettings:
    """Relay transport behaviour.

    An empty ``url`` means "no relay configured": room hosting stays local
    and relay-backed commands show guidance instead of failing.
    """

    url: str = ""
    connect_timeout_seconds: float = CONNECT_TIMEOUT_SECONDS
    handshake_timeout_seconds: float = HANDSHAKE_TIMEOUT_SECONDS
    heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS
    heartbeat_timeout_seconds: float = HEARTBEAT_TIMEOUT_SECONDS
    reconnect_attempts: int = RECONNECT_ATTEMPTS
    reconnect_base_delay_seconds: float = RECONNECT_BASE_DELAY_SECONDS

    def __post_init__(self) -> None:
        url = self.url.strip()
        if url and not url.startswith(("ws://", "wss://")):
            raise ConfigValidationError(
                f"Setting 'relay.url' must start with ws:// or wss://, got '{url}'.",
                hint="Example: wss://relay.example.org or ws://127.0.0.1:8787",
            )
        for field_name in (
            "connect_timeout_seconds",
            "handshake_timeout_seconds",
            "heartbeat_interval_seconds",
            "heartbeat_timeout_seconds",
            "reconnect_base_delay_seconds",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int | float) or value <= 0:
                raise ConfigValidationError(
                    f"Setting 'relay.{field_name}' must be a positive number, got {value!r}.",
                    hint="Use seconds, e.g. heartbeat_interval_seconds = 10.0",
                )
        if not isinstance(self.reconnect_attempts, int) or self.reconnect_attempts < 0:
            raise ConfigValidationError(
                f"Setting 'relay.reconnect_attempts' must be an integer ≥ 0, "
                f"got {self.reconnect_attempts!r}.",
                hint="Use 0 to disable automatic reconnection.",
            )


@dataclass(frozen=True, slots=True)
class RoomsSettings:
    """Room defaults. A lifetime of 0 minutes means rooms never expire."""

    default_lifetime_minutes: int = DEFAULT_ROOM_LIFETIME_MINUTES

    def __post_init__(self) -> None:
        if not isinstance(self.default_lifetime_minutes, int) or self.default_lifetime_minutes < 0:
            raise ConfigValidationError(
                "Setting 'rooms.default_lifetime_minutes' must be an integer ≥ 0, "
                f"got {self.default_lifetime_minutes!r}.",
                hint="Use 0 for rooms that never expire.",
            )


@dataclass(frozen=True, slots=True)
class InvitesSettings:
    """Invite defaults (Phase 2 rooms + Phase 5 one-time join invites).

    ``default_expiry_seconds`` bounds how long a join invite stays valid;
    the relay authority enforces it. ``retention_hours`` controls how long
    terminal (expired/revoked/redeemed) invite records are kept locally.
    """

    default_lifetime_minutes: int = DEFAULT_INVITE_LIFETIME_MINUTES
    one_time: bool = True
    default_expiry_seconds: int = INVITE_DEFAULT_TTL_SECONDS
    max_expiry_seconds: int = int(INVITE_MAX_TTL_SECONDS)
    retention_hours: int = INVITE_RETENTION_HOURS

    def __post_init__(self) -> None:
        if not isinstance(self.default_lifetime_minutes, int) or self.default_lifetime_minutes < 1:
            raise ConfigValidationError(
                "Setting 'invites.default_lifetime_minutes' must be an integer ≥ 1, "
                f"got {self.default_lifetime_minutes!r}.",
                hint="Invites always expire; choose at least one minute.",
            )
        if not isinstance(self.one_time, bool):
            raise ConfigValidationError(
                f"Setting 'invites.one_time' must be true or false, got {self.one_time!r}.",
                hint="Use a TOML boolean: one_time = true",
            )
        for field_name, minimum, maximum in (
            ("default_expiry_seconds", int(INVITE_MIN_TTL_SECONDS), int(INVITE_MAX_TTL_SECONDS)),
            ("max_expiry_seconds", 60, 7 * 24 * 3600),
            ("retention_hours", 1, 30 * 24),
        ):
            value = getattr(self, field_name)
            in_range = (
                isinstance(value, int)
                and not isinstance(value, bool)
                and minimum <= value <= maximum
            )
            if not in_range:
                raise ConfigValidationError(
                    f"Setting 'invites.{field_name}' must be an integer between {minimum} "
                    f"and {maximum}, got {value!r}.",
                    hint=f"Choose a value in [{minimum}, {maximum}].",
                )
        if self.default_expiry_seconds > self.max_expiry_seconds:
            raise ConfigValidationError(
                "Setting 'invites.default_expiry_seconds' must not exceed "
                f"'invites.max_expiry_seconds' ({self.default_expiry_seconds} > "
                f"{self.max_expiry_seconds}).",
                hint="Raise max_expiry_seconds or lower the default.",
            )


@dataclass(frozen=True, slots=True)
class ChatSettings:
    """One-to-one chat behaviour (Phase 3).

    ``display_name`` empty means "not configured": the CLI falls back to a
    per-run pseudonym. History is off by default — retention is a decision
    the user makes explicitly.
    """

    display_name: str = ""
    read_receipts: bool = True
    typing_indicators: bool = True
    history_mode: str = DEFAULT_HISTORY_MODE
    timestamp_format: str = DEFAULT_TIMESTAMP_FORMAT
    notification_style: str = DEFAULT_NOTIFICATION_STYLE
    message_wrapping: bool = True

    def __post_init__(self) -> None:
        if not isinstance(self.display_name, str):
            raise ConfigValidationError(
                "Setting 'chat.display_name' must be a string.",
                hint='Use a TOML string: display_name = "Nova"',
            )
        if len(self.display_name) > 24:
            raise ConfigValidationError(
                "Setting 'chat.display_name' must be at most 24 characters.",
                hint="Pick a short printable pseudonym.",
            )
        if any(0 < ord(char) < 32 or ord(char) == 127 for char in self.display_name):
            raise ConfigValidationError(
                "Setting 'chat.display_name' must be printable text.",
                hint="Remove control characters from the name.",
            )
        for field_name in ("read_receipts", "typing_indicators", "message_wrapping"):
            value = getattr(self, field_name)
            if not isinstance(value, bool):
                raise ConfigValidationError(
                    f"Setting 'chat.{field_name}' must be true or false, got {value!r}.",
                    hint="Use a TOML boolean.",
                )
        for field_name, value, choices in (
            ("history_mode", self.history_mode, CHAT_HISTORY_MODES),
            ("timestamp_format", self.timestamp_format, CHAT_TIMESTAMP_FORMATS),
            ("notification_style", self.notification_style, CHAT_NOTIFICATION_STYLES),
        ):
            if value not in choices:
                raise ConfigValidationError(
                    f"Setting 'chat.{field_name}' has unsupported value '{value}'.",
                    hint=f"Choose one of: {', '.join(sorted(choices))}.",
                )


@dataclass(frozen=True, slots=True)
class TransferSettings:
    """Secure file-transfer limits and behaviour (Phase 4).

    Empty ``download_dir`` selects ``~/Download/GhostLink``. Chunk size is
    capped at 4 KiB so every chunk fits one secure-channel frame; the
    chunk/file-size cross-check guarantees resume bitmaps stay bounded."""

    download_dir: str = ""
    max_file_size_mb: int = TRANSFER_DEFAULT_MAX_FILE_MB
    max_concurrent_transfers: int = TRANSFER_DEFAULT_CONCURRENT
    chunk_size_kb: int = TRANSFER_DEFAULT_CHUNK_KB
    ack_timeout_seconds: float = 5.0
    retry_limit: int = 5
    transfer_expiry_minutes: int = TRANSFER_DEFAULT_EXPIRY_MINUTES
    temp_storage_limit_mb: int = TRANSFER_DEFAULT_TEMP_LIMIT_MB

    def __post_init__(self) -> None:
        if not isinstance(self.download_dir, str):
            raise ConfigValidationError(
                "Setting 'transfer.download_dir' must be a string path.",
                hint='Use a TOML string: download_dir = "~/Download/GhostLink"',
            )
        _require_int_range(
            self.max_file_size_mb,
            field="transfer.max_file_size_mb",
            minimum=1,
            maximum=TRANSFER_MAX_FILE_MB,
        )
        _require_int_range(
            self.max_concurrent_transfers,
            field="transfer.max_concurrent_transfers",
            minimum=1,
            maximum=TRANSFER_MAX_CONCURRENT,
        )
        _require_int_range(
            self.chunk_size_kb,
            field="transfer.chunk_size_kb",
            minimum=TRANSFER_MIN_CHUNK_KB,
            maximum=TRANSFER_MAX_CHUNK_KB,
        )
        if (
            not isinstance(self.ack_timeout_seconds, int | float)
            or isinstance(self.ack_timeout_seconds, bool)
            or not 0.5 <= self.ack_timeout_seconds <= 60.0
        ):
            raise ConfigValidationError(
                "Setting 'transfer.ack_timeout_seconds' must be between 0.5 and 60."
                f" Got {self.ack_timeout_seconds!r}.",
                hint="Use seconds, e.g. ack_timeout_seconds = 5.0",
            )
        _require_int_range(self.retry_limit, field="transfer.retry_limit", minimum=1, maximum=20)
        _require_int_range(
            self.transfer_expiry_minutes,
            field="transfer.transfer_expiry_minutes",
            minimum=1,
            maximum=1440,
        )
        _require_int_range(
            self.temp_storage_limit_mb,
            field="transfer.temp_storage_limit_mb",
            minimum=TRANSFER_MIN_TEMP_LIMIT_MB,
            maximum=TRANSFER_MAX_TEMP_LIMIT_MB,
        )
        total_chunks = -(-(self.max_file_size_mb * 1024) // self.chunk_size_kb)
        if total_chunks > TRANSFER_MAX_CHUNKS_PER_FILE:
            raise ConfigValidationError(
                "That combination needs more than "
                f"{TRANSFER_MAX_CHUNKS_PER_FILE} chunks per file "
                f"(max_file_size_mb={self.max_file_size_mb}, "
                f"chunk_size_kb={self.chunk_size_kb}).",
                hint="Increase chunk_size_kb or lower max_file_size_mb.",
            )


def _require_int_range(value: object, *, field: str, minimum: int, maximum: int) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise ConfigValidationError(
            f"Setting '{field}' must be an integer between {minimum} and {maximum}, got {value!r}.",
            hint=f"Choose a value in [{minimum}, {maximum}].",
        )


@dataclass(frozen=True, slots=True)
class AppSettings:
    """The fully-resolved settings tree for one application run."""

    ui: UISettings
    notifications: NotificationSettings
    storage: StorageSettings
    diagnostics: DiagnosticsSettings
    relay: RelaySettings
    rooms: RoomsSettings
    invites: InvitesSettings
    chat: ChatSettings
    transfer: TransferSettings

    @classmethod
    def defaults(cls) -> AppSettings:
        return cls(
            ui=UISettings(),
            notifications=NotificationSettings(),
            storage=StorageSettings(),
            diagnostics=DiagnosticsSettings(),
            relay=RelaySettings(),
            rooms=RoomsSettings(),
            invites=InvitesSettings(),
            chat=ChatSettings(),
            transfer=TransferSettings(),
        )
