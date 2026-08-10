"""Reusable UI components: badges, tables, panels, notifications, menus."""

from __future__ import annotations

import pytest

from ghostlink.models.settings import AppSettings
from ghostlink.models.theme import ThemeSpec
from ghostlink.ui.components.badges import BadgeTone, badge, badge_row
from ghostlink.ui.components.dialogs import notice_dialog, roadmap_dialog
from ghostlink.ui.components.notifications import (
    NotificationCenter,
    NotificationLevel,
)
from ghostlink.ui.components.tables import info_table, kv_grid
from ghostlink.ui.console import ConsoleManager
from ghostlink.ui.menu import InteractiveMenu, MenuEntry

ENTRIES = (
    MenuEntry(key="one", label="First", description="first option", icon="1"),
    MenuEntry(key="two", label="Second", description="second option", icon="2"),
    MenuEntry(key="exit", label="Exit", description="leave", icon="x"),
)


class TestBadges:
    def test_filled_badge_uses_theme_background(self, theme: ThemeSpec) -> None:
        chip = badge("Enabled", BadgeTone.SUCCESS, theme=theme)
        assert "✓ Enabled" in chip.plain
        assert f"on {theme.success}" in chip.style

    def test_themeless_badge_uses_semantic_style(self) -> None:
        chip = badge("Ready", BadgeTone.INFO)
        assert chip.plain == "[• Ready]"
        assert chip.style == "bold gl.info"

    def test_badge_row_joins_with_separator(self, theme: ThemeSpec) -> None:
        row = badge_row(
            badge("A", theme=theme),
            badge("B", theme=theme),
        )
        assert row.plain == " ◆ A    ◆ B "


class TestTables:
    def test_info_table_renders_rows(self, console_manager: ConsoleManager) -> None:
        console_manager.print(info_table([("Theme", "phantom"), ("Debug Mode", "Off")]))
        output = console_manager.export_text()
        assert "Theme" in output and "phantom" in output and "Debug Mode" in output

    def test_kv_grid_renders_pairs(self, console_manager: ConsoleManager) -> None:
        console_manager.print(kv_grid([("Version", "v0.1.0")]))
        assert "v0.1.0" in console_manager.export_text()


class TestDialogs:
    def test_notice_dialog_content(self, console_manager: ConsoleManager) -> None:
        console_manager.print(
            notice_dialog("Heads up", "Something happened.", hint="No action needed.")
        )
        output = console_manager.export_text()
        assert "Heads up" in output and "Something happened." in output
        assert "No action needed." in output

    def test_roadmap_dialog_lists_planned_items(self, console_manager: ConsoleManager) -> None:
        console_manager.print(
            roadmap_dialog(
                "Create Room",
                summary="Not part of Phase 1.",
                planned=("Room key generation", "Invite links"),
                phase_label="Phase 2",
            )
        )
        output = console_manager.export_text()
        assert "Create Room" in output
        assert "Room key generation" in output
        assert "Phase 2" in output


class TestNotifications:
    def test_enabled_center_renders_and_records(self, console_manager: ConsoleManager) -> None:
        center = NotificationCenter(console_manager, enabled=True)
        center.success("Configuration initialized")
        assert "Configuration initialized" in console_manager.export_text()
        history = center.history()
        assert len(history) == 1
        assert history[0].level is NotificationLevel.SUCCESS

    def test_disabled_center_records_silently(self, console_manager: ConsoleManager) -> None:
        center = NotificationCenter(console_manager, enabled=False)
        center.warning("muted")
        assert "muted" not in console_manager.export_text()
        assert len(center.history()) == 1

    def test_history_is_bounded(self, console_manager: ConsoleManager) -> None:
        center = NotificationCenter(console_manager, enabled=False, history_limit=5)
        for index in range(10):
            center.info(f"n{index}")
        assert len(center.history()) == 5


class TestNumberedMenu:
    def test_valid_selection(
        self, console_manager: ConsoleManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inputs = iter(["2"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        menu = InteractiveMenu(console_manager)
        assert menu.prompt(ENTRIES, default_key="exit") == "two"

    def test_retries_after_invalid_input(
        self, console_manager: ConsoleManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inputs = iter(["99", "bogus", "1"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        menu = InteractiveMenu(console_manager)
        assert menu.prompt(ENTRIES, default_key="exit") == "one"
        assert "between 1 and 3" in console_manager.export_text()

    def test_eof_returns_default(
        self, console_manager: ConsoleManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _eof(prompt: str = "") -> str:
            raise EOFError

        monkeypatch.setattr("builtins.input", _eof)
        menu = InteractiveMenu(console_manager)
        assert menu.prompt(ENTRIES, default_key="exit") == "exit"

    def test_q_returns_default(
        self, console_manager: ConsoleManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        inputs = iter(["q"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(inputs))
        menu = InteractiveMenu(console_manager)
        assert menu.prompt(ENTRIES, default_key="exit") == "exit"

    def test_empty_entries_rejected(self, console_manager: ConsoleManager) -> None:
        with pytest.raises(ValueError, match="at least one"):
            InteractiveMenu(console_manager).prompt((), default_key="exit")


class TestSettingsSurface:
    def test_app_settings_defaults(self) -> None:
        settings = AppSettings.defaults()
        assert settings.ui.theme == "phantom"
        assert settings.ui.language == "en"
        assert settings.notifications.enabled is True
        assert settings.storage.data_dir == ""
        assert settings.diagnostics.debug is False
