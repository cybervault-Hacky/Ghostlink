"""Comprehensive tests for the GhostLink Settings Center."""

from __future__ import annotations

import asyncio
import tomllib
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest

from ghostlink.cli.arguments import parse_args
from ghostlink.config.loader import load_config_file, validate_sections
from ghostlink.config.manager import ConfigurationManager
from ghostlink.config.serializer import save_config_file, serialize_settings
from ghostlink.constants.app import BUILTIN_THEMES, SUPPORTED_LANGUAGES
from ghostlink.core.bootstrap import build_application
from ghostlink.exceptions.config import ConfigurationError
from ghostlink.models.settings import AppSettings
from ghostlink.ui.console import ConsoleManager
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.base import Screen
from ghostlink.ui.screens.settings import SettingsScreen, _render_theme_preview_box
from ghostlink.ui.themes import ThemeEngine


def _script_menu(monkeypatch: pytest.MonkeyPatch, choices: list[str]) -> None:
    """Script InteractiveMenu selections for deterministic headless tests."""

    script: Iterator[str] = iter(choices)

    def _prompt(
        self: InteractiveMenu,
        entries: tuple[MenuEntry, ...] | list[MenuEntry],
        *,
        default_key: str,
    ) -> str:
        try:
            return next(script)
        except StopIteration:
            return default_key

    monkeypatch.setattr(InteractiveMenu, "prompt", _prompt)


class TestConfigSerialization:
    def test_serialize_and_parse_round_trip(self) -> None:
        defaults = AppSettings.defaults()
        raw = serialize_settings(defaults)
        parsed = tomllib.loads(raw)
        validated = validate_sections(parsed, source="<test>")

        assert validated["meta"]["config_version"] == 1
        assert validated["ui"]["theme"] == "phantom"
        assert validated["ui"]["language"] == "en"
        assert validated["notifications"]["enabled"] is True
        assert validated["diagnostics"]["debug"] is False
        assert validated["chat"]["read_receipts"] is True
        assert validated["chat"]["typing_indicators"] is True
        assert validated["chat"]["history_mode"] == "disabled"

    def test_atomic_save_creates_valid_file(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "config.toml"
        settings = replace(
            AppSettings.defaults(),
            ui=replace(AppSettings.defaults().ui, theme="arctic", language="es"),
            diagnostics=replace(AppSettings.defaults().diagnostics, debug=True),
        )
        save_config_file(cfg_path, settings)
        assert cfg_path.is_file()

        reloaded = load_config_file(cfg_path)
        assert reloaded["ui"]["theme"] == "arctic"
        assert reloaded["ui"]["language"] == "es"
        assert reloaded["diagnostics"]["debug"] is True

    def test_atomic_save_handles_nested_dirs(self, tmp_path: Path) -> None:
        cfg_path = tmp_path / "deep" / "nested" / "config.toml"
        save_config_file(cfg_path, AppSettings.defaults())
        assert cfg_path.is_file()

    def test_atomic_save_fails_gracefully_on_unwritable_path(self, tmp_path: Path) -> None:
        blocked = tmp_path / "file_block"
        blocked.write_text("not a directory")
        cfg_path = blocked / "config.toml"
        with pytest.raises(ConfigurationError):
            save_config_file(cfg_path, AppSettings.defaults())


class TestThemeSettings:
    @pytest.mark.parametrize("theme_name", BUILTIN_THEMES)
    def test_all_builtin_themes_render_preview(self, theme_name: str) -> None:
        spec = ThemeEngine().get(theme_name)
        panel = _render_theme_preview_box(spec)
        assert panel.title is not None
        assert theme_name.title() in str(panel.title)

    def test_theme_switch_and_persistence(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = build_application(parse_args([]))
        recording = ConsoleManager(
            application._context.console.theme,
            record=True,
            width=100,
            force_terminal=False,
        )
        application._context.console = recording

        # Sequence: enter appearance -> enter theme -> select 'emerald' -> confirm -> back -> back
        _script_menu(monkeypatch, ["appearance", "theme", "emerald", "back", "back"])
        monkeypatch.setattr("ghostlink.ui.screens.settings.confirm", lambda *args, **kwargs: True)

        screen = SettingsScreen(application._context)
        asyncio.run(screen.show())

        # Runtime updated
        assert application._context.theme.name == "emerald"
        assert application._context.settings.ui.theme == "emerald"

        # Persisted to config file
        mgr = ConfigurationManager(config_path=application._context.config_path)
        loaded = mgr.load(create_missing=False)
        assert loaded.ui.theme == "emerald"

    def test_theme_change_reflected_in_console_theme(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = build_application(parse_args([]))
        recording = ConsoleManager(
            application._context.console.theme,
            record=True,
            width=100,
            force_terminal=False,
        )
        application._context.console = recording

        _script_menu(monkeypatch, ["appearance", "theme", "arctic", "back", "back"])
        monkeypatch.setattr("ghostlink.ui.screens.settings.confirm", lambda *args, **kwargs: True)

        asyncio.run(SettingsScreen(application._context).show())
        assert application._context.console.theme.name == "arctic"
        assert application._context.theme.name == "arctic"


class TestLanguageSettings:
    @pytest.mark.parametrize("lang_code", sorted(SUPPORTED_LANGUAGES.keys()))
    def test_language_switch_and_persistence(
        self, lang_code: str, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = build_application(parse_args([]))
        recording = ConsoleManager(
            application._context.console.theme,
            record=True,
            width=100,
            force_terminal=False,
        )
        application._context.console = recording

        _script_menu(monkeypatch, ["appearance", "language", lang_code, "back", "back"])

        asyncio.run(SettingsScreen(application._context).show())

        assert application._context.settings.ui.language == lang_code

        mgr = ConfigurationManager(config_path=application._context.config_path)
        loaded = mgr.load(create_missing=False)
        assert loaded.ui.language == lang_code


class TestPrivacyAndChatSettings:
    def test_toggle_read_receipts(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = build_application(parse_args([]))
        assert application._context.settings.chat.read_receipts is True

        _script_menu(monkeypatch, ["privacy", "toggle_receipts", "back", "back"])
        asyncio.run(SettingsScreen(application._context).show())

        assert application._context.settings.chat.read_receipts is False

        mgr = ConfigurationManager(config_path=application._context.config_path)
        assert mgr.load(create_missing=False).chat.read_receipts is False

    def test_toggle_typing_indicators(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = build_application(parse_args([]))
        assert application._context.settings.chat.typing_indicators is True

        _script_menu(monkeypatch, ["privacy", "toggle_typing", "back", "back"])
        asyncio.run(SettingsScreen(application._context).show())

        assert application._context.settings.chat.typing_indicators is False

        mgr = ConfigurationManager(config_path=application._context.config_path)
        assert mgr.load(create_missing=False).chat.typing_indicators is False

    def test_select_history_mode(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = build_application(parse_args([]))
        assert application._context.settings.chat.history_mode == "disabled"

        _script_menu(monkeypatch, ["privacy", "history_mode", "session", "back", "back"])
        asyncio.run(SettingsScreen(application._context).show())

        assert application._context.settings.chat.history_mode == "session"

        mgr = ConfigurationManager(config_path=application._context.config_path)
        assert mgr.load(create_missing=False).chat.history_mode == "session"

    def test_edit_chat_pseudonym(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = build_application(parse_args([]))
        assert application._context.settings.chat.display_name == ""

        _script_menu(monkeypatch, ["privacy", "display_name", "back", "back"])
        monkeypatch.setattr(
            "ghostlink.ui.screens.settings.prompt_text",
            lambda *args, **kwargs: "ShadowFox",
        )

        asyncio.run(SettingsScreen(application._context).show())
        assert application._context.settings.chat.display_name == "ShadowFox"

        mgr = ConfigurationManager(config_path=application._context.config_path)
        assert mgr.load(create_missing=False).chat.display_name == "ShadowFox"

    def test_identity_view_fingerprint_and_rotate(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = build_application(parse_args([]))
        recording = ConsoleManager(
            application._context.console.theme,
            record=True,
            width=100,
            force_terminal=False,
        )
        application._context.console = recording

        _script_menu(
            monkeypatch,
            ["privacy", "identity_keys", "view_fingerprint", "rotate_keys", "back", "back", "back"],
        )
        monkeypatch.setattr("ghostlink.ui.screens.settings.confirm", lambda *args, **kwargs: True)

        async def _instant_pause(self: Screen, prompt: str = "…") -> None:
            return None

        monkeypatch.setattr(Screen, "pause", _instant_pause)

        asyncio.run(SettingsScreen(application._context).show())
        output = recording.export_text()
        assert "PUBLIC FINGERPRINT" in output
        assert "IDENTITY" in output


class TestNotificationSettings:
    def test_toggle_notifications(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = build_application(parse_args([]))
        assert application._context.settings.notifications.enabled is True

        _script_menu(monkeypatch, ["notifications", "toggle", "back", "back"])
        asyncio.run(SettingsScreen(application._context).show())

        assert application._context.settings.notifications.enabled is False

        mgr = ConfigurationManager(config_path=application._context.config_path)
        assert mgr.load(create_missing=False).notifications.enabled is False

    def test_select_notification_style(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = build_application(parse_args([]))
        assert application._context.settings.chat.notification_style == "banner"

        _script_menu(monkeypatch, ["notifications", "style", "compact", "back", "back"])
        asyncio.run(SettingsScreen(application._context).show())

        assert application._context.settings.chat.notification_style == "compact"

        mgr = ConfigurationManager(config_path=application._context.config_path)
        assert mgr.load(create_missing=False).chat.notification_style == "compact"


class TestNetworkRelaySettings:
    def test_configure_valid_relay_url(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = build_application(parse_args([]))
        assert application._context.settings.relay.url == ""

        _script_menu(monkeypatch, ["network", "configure_relay", "back", "back"])
        monkeypatch.setattr(
            "ghostlink.ui.screens.settings.prompt_text",
            lambda *args, **kwargs: "wss://relay.ghostlink.net",
        )

        asyncio.run(SettingsScreen(application._context).show())
        assert application._context.settings.relay.url == "wss://relay.ghostlink.net"

        mgr = ConfigurationManager(config_path=application._context.config_path)
        assert mgr.load(create_missing=False).relay.url == "wss://relay.ghostlink.net"

    def test_invalid_relay_url_rejected_without_modifying_config(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = build_application(parse_args([]))

        _script_menu(monkeypatch, ["network", "configure_relay", "back", "back"])
        monkeypatch.setattr(
            "ghostlink.ui.screens.settings.prompt_text",
            lambda *args, **kwargs: "http://invalid-prefix.com",
        )

        async def _instant_pause(self: Screen, prompt: str = "…") -> None:
            return None

        monkeypatch.setattr(Screen, "pause", _instant_pause)

        asyncio.run(SettingsScreen(application._context).show())
        assert application._context.settings.relay.url == ""

    def test_clear_relay_endpoint(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = build_application(parse_args([]))
        application._context.settings = replace(
            application._context.settings,
            relay=replace(application._context.settings.relay, url="wss://custom-relay.org"),
        )
        save_config_file(application._context.config_path, application._context.settings)

        _script_menu(monkeypatch, ["network", "clear_relay", "back", "back"])
        monkeypatch.setattr("ghostlink.ui.screens.settings.confirm", lambda *args, **kwargs: True)

        asyncio.run(SettingsScreen(application._context).show())
        assert application._context.settings.relay.url == ""

        mgr = ConfigurationManager(config_path=application._context.config_path)
        assert mgr.load(create_missing=False).relay.url == ""


class TestStorageSettings:
    def test_set_custom_data_dir(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = build_application(parse_args([]))
        custom_path = str(isolated_home / "custom_data")

        _script_menu(monkeypatch, ["storage", "set_datadir", "back", "back"])
        monkeypatch.setattr(
            "ghostlink.ui.screens.settings.prompt_text",
            lambda *args, **kwargs: custom_path,
        )

        asyncio.run(SettingsScreen(application._context).show())
        assert application._context.settings.storage.data_dir == custom_path

        mgr = ConfigurationManager(config_path=application._context.config_path)
        assert mgr.load(create_missing=False).storage.data_dir == custom_path

    def test_reset_data_dir_to_default(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = build_application(parse_args([]))
        application._context.settings = replace(
            application._context.settings,
            storage=replace(application._context.settings.storage, data_dir="/tmp/old_dir"),
        )
        save_config_file(application._context.config_path, application._context.settings)

        _script_menu(monkeypatch, ["storage", "reset_datadir", "back", "back"])
        asyncio.run(SettingsScreen(application._context).show())

        assert application._context.settings.storage.data_dir == ""


class TestDeveloperAndSystemSettings:
    def test_toggle_debug_mode(self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        application = build_application(parse_args([]))
        assert application._context.settings.diagnostics.debug is False

        _script_menu(monkeypatch, ["developer", "toggle_debug", "back", "back"])
        asyncio.run(SettingsScreen(application._context).show())

        assert application._context.settings.diagnostics.debug is True

        mgr = ConfigurationManager(config_path=application._context.config_path)
        assert mgr.load(create_missing=False).diagnostics.debug is True

    def test_view_system_diagnostics_screen(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = build_application(parse_args([]))
        recording = ConsoleManager(
            application._context.console.theme,
            record=True,
            width=100,
            force_terminal=False,
        )
        application._context.console = recording

        _script_menu(monkeypatch, ["developer", "view_system_info", "back", "back"])

        async def _instant_pause(self: Screen, prompt: str = "…") -> None:
            return None

        monkeypatch.setattr(Screen, "pause", _instant_pause)

        asyncio.run(SettingsScreen(application._context).show())
        output = recording.export_text()

        assert "SYSTEM DIAGNOSTICS" in output
        assert "Platform" in output
        assert "Python Runtime" in output
        assert "Configuration File" in output
