"""Configuration management.

Responsibilities of :class:`ConfigurationManager`:

* resolve the configuration file path (explicit flag or platform default)
* generate the packaged default configuration on first launch
* load, structurally validate, and build the typed settings tree
* apply command-line overrides on top of file values (highest precedence)
* resolve and provision the data, state, and log directories
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ghostlink.assets.branding import load_default_config_text
from ghostlink.config.loader import load_config_file, validate_sections
from ghostlink.config.serializer import save_config_file
from ghostlink.constants.files import (
    CONFIG_FILE_NAME,
    LOGS_DIR_NAME,
    STATE_DIR_NAME,
)
from ghostlink.exceptions.config import ConfigurationError
from ghostlink.models.settings import (
    AppSettings,
    ChatSettings,
    DiagnosticsSettings,
    InvitesSettings,
    MetaSettings,
    NotificationSettings,
    RelaySettings,
    RoomsSettings,
    StorageSettings,
    TransferSettings,
    UISettings,
)
from ghostlink.utils.paths import (
    default_config_dir,
    default_data_dir,
    ensure_directory,
)


@dataclass(frozen=True, slots=True)
class ConfigOverrides:
    """Command-line overrides applied over file-based configuration."""

    theme: str | None = None
    data_dir: Path | None = None
    debug: bool | None = None

    @property
    def is_empty(self) -> bool:
        return self.theme is None and self.data_dir is None and self.debug is None


class ConfigurationManager:
    """Owns configuration resolution for one application run."""

    def __init__(
        self,
        *,
        config_path: Path | None = None,
        overrides: ConfigOverrides | None = None,
    ) -> None:
        self._config_path = (
            config_path.expanduser()
            if config_path is not None
            else default_config_dir() / CONFIG_FILE_NAME
        )
        self._overrides = overrides or ConfigOverrides()
        self._settings: AppSettings | None = None
        self._created_default = False

    # ------------------------------------------------------------------ paths

    @property
    def config_path(self) -> Path:
        return self._config_path

    @property
    def config_dir(self) -> Path:
        return self._config_path.parent

    @property
    def created_default(self) -> bool:
        """True when this run generated the default configuration file."""

        return self._created_default

    @property
    def settings(self) -> AppSettings:
        if self._settings is None:
            raise ConfigurationError(
                "Configuration was requested before it was loaded.",
                hint="Call ConfigurationManager.load() during bootstrap.",
            )
        return self._settings

    @property
    def data_dir(self) -> Path:
        """Effective data directory: CLI flag > config value > platform default."""

        if self._overrides.data_dir is not None:
            return self._overrides.data_dir.expanduser()
        configured = self.settings.storage.data_dir.strip()
        if configured:
            return Path(configured).expanduser()
        return default_data_dir()

    @property
    def state_dir(self) -> Path:
        return self.data_dir / STATE_DIR_NAME

    @property
    def logs_dir(self) -> Path:
        return self.data_dir / LOGS_DIR_NAME

    # ----------------------------------------------------------------- loading

    def load(self, *, create_missing: bool = True) -> AppSettings:
        """Load configuration and return the typed settings tree.

        With ``create_missing`` (the default), a missing configuration file is
        generated from the packaged template. Diagnostics such as ``--doctor``
        pass ``False`` to stay fully read-only.
        """

        if not self._config_path.is_file():
            if not create_missing:
                self._settings = self._build_sections({})
                return self._settings
            self._write_default_file()

        raw = load_config_file(self._config_path)
        sections = validate_sections(raw, source=str(self._config_path))
        self._settings = self._build_sections(sections)
        return self._settings

    def save(self, settings: AppSettings) -> None:
        """Atomically persist a validated settings instance to disk."""

        save_config_file(self._config_path, settings)
        self._settings = self._apply_overrides(settings)

    def ensure_directories(self) -> None:
        """Provision every directory GhostLink needs, up front."""

        ensure_directory(self.config_dir, error_type=ConfigurationError)
        ensure_directory(self.data_dir, error_type=ConfigurationError)
        ensure_directory(self.state_dir, error_type=ConfigurationError)
        ensure_directory(self.logs_dir, error_type=ConfigurationError)

    # ------------------------------------------------------------------ private

    def _write_default_file(self) -> None:
        ensure_directory(self.config_dir, error_type=ConfigurationError)
        try:
            self._config_path.write_text(load_default_config_text(), encoding="utf-8")
        except OSError as exc:
            raise ConfigurationError(
                f"Could not generate the default configuration at '{self._config_path}'.",
                hint="Check that the configuration directory is writable.",
            ) from exc
        self._created_default = True

    def _build_sections(self, sections: dict[str, dict[str, Any]]) -> AppSettings:
        settings = AppSettings(
            ui=UISettings(**sections.get("ui", {})),
            notifications=NotificationSettings(**sections.get("notifications", {})),
            storage=StorageSettings(**sections.get("storage", {})),
            diagnostics=DiagnosticsSettings(**sections.get("diagnostics", {})),
            relay=RelaySettings(**sections.get("relay", {})),
            rooms=RoomsSettings(**sections.get("rooms", {})),
            invites=InvitesSettings(**sections.get("invites", {})),
            chat=ChatSettings(**sections.get("chat", {})),
            transfer=TransferSettings(**sections.get("transfer", {})),
            meta=MetaSettings(**sections.get("meta", {})),
        )
        return self._apply_overrides(settings)

    def _apply_overrides(self, settings: AppSettings) -> AppSettings:
        overrides = self._overrides
        if overrides.theme is not None:
            settings = replace(settings, ui=replace(settings.ui, theme=overrides.theme))
        if overrides.debug is not None:
            settings = replace(
                settings, diagnostics=replace(settings.diagnostics, debug=overrides.debug)
            )
        if overrides.data_dir is not None:
            settings = replace(
                settings,
                storage=replace(settings.storage, data_dir=str(overrides.data_dir)),
            )
        return settings
