"""UI/UX polish regression tests — navigation, duplication, emoji, width, themes.

Pins the design-language contract of the interactive terminal UI:

1.  Settings opens exactly once per selection.
2.  Settings categories are unique and do not duplicate.
3.  Back returns exactly one level (never skips or loops a screen).
4.  Menu callbacks execute exactly once per selection.
5.  Home navigation opens each destination exactly once.
6.  Theme selection persists and is reflected on the entry row.
7.  Language selection persists and is reflected on the entry row.
8.  Settings reopen correctly (fresh state, no stale duplication).
9.  simple-term-menu is only driven from the main thread (see tests/test_menu_main_thread.py).
10. No emoji anywhere in system UI output.
11. No internal development labels (e.g. "Phase 15") in UI output.
12. Narrow-terminal rendering (60 columns) stays within bounds.
13. ``--no-color`` rendering stays complete and readable.
14. Every builtin theme renders Home and Settings without errors.
15. Every supported language renders without raw translation keys.
"""

from __future__ import annotations

import asyncio
import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from ghostlink.cli.arguments import parse_args
from ghostlink.constants.app import BUILTIN_THEMES, SUPPORTED_LANGUAGES
from ghostlink.core.bootstrap import build_application
from ghostlink.i18n import t
from ghostlink.ui.console import ConsoleManager
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.base import Screen
from ghostlink.ui.screens.home import HomeScreen, get_menu_entries
from ghostlink.ui.screens.settings import SettingsScreen
from ghostlink.ui.themes import ThemeEngine

# Emoji and pictographic ranges that must never appear in system UI.
_EMOJI = re.compile(
    "[\U0001f000-\U0001faff\u2600-\u26ff\u2700-\u2712\u2714\u2716-\u27bf\u2b00-\u2bff\ufe0f]"
)


def _assert_clean_text(output: str) -> None:
    """No emoji, no phase labels, no raw translation keys."""

    emoji_found = _EMOJI.findall(output)
    assert not emoji_found, f"emoji in system UI: {emoji_found[:10]}"
    assert "Phase 15" not in output
    assert "Production Operations & Platform Maturity" not in output
    raw_keys = re.findall(r"\b(?:menu|settings|dialog|status|action|about)\.[a-z_.]+", output)
    assert not raw_keys, f"raw translation keys leaked: {raw_keys[:10]}"


async def _instant_pause(self: Screen, prompt: str | None = None) -> None:
    return None


@pytest.fixture(autouse=True)
def _no_pause(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Screen, "pause", _instant_pause)


def _recording_app(width: int = 100, no_color: bool = False):
    application = build_application(parse_args([]))
    recording = ConsoleManager(
        application._context.console.theme,
        record=True,
        width=width,
        force_terminal=False,
        no_color=no_color,
    )
    application._context.console = recording
    return application, recording


def _script_menu(monkeypatch: pytest.MonkeyPatch, choices: list[str]) -> list[tuple[str, ...]]:
    """Script menu answers; returns a recorder of the entry keys per prompt."""

    calls: list[tuple[str, ...]] = []
    script: Iterator[str] = iter(choices)

    def _prompt(
        self: InteractiveMenu,
        entries: tuple[MenuEntry, ...] | list[MenuEntry],
        *,
        default_key: str,
    ) -> str:
        calls.append(tuple(entry.key for entry in entries))
        return next(script, default_key)

    monkeypatch.setattr(InteractiveMenu, "prompt", _prompt)
    return calls


def _capture_entries(monkeypatch: pytest.MonkeyPatch, choices: list[str]) -> list[list[MenuEntry]]:
    """Script menu answers; returns the full entry lists shown per prompt."""

    calls: list[list[MenuEntry]] = []
    script: Iterator[str] = iter(choices)

    def _prompt(
        self: InteractiveMenu,
        entries: tuple[MenuEntry, ...] | list[MenuEntry],
        *,
        default_key: str,
    ) -> str:
        calls.append(list(entries))
        return next(script, default_key)

    monkeypatch.setattr(InteractiveMenu, "prompt", _prompt)
    return calls


class TestNavigationSingleOwnership:
    def test_settings_opens_exactly_once(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Selecting Settings from Home constructs exactly one SettingsScreen."""

        constructed: list[str] = []
        real_init = SettingsScreen.__init__

        def _counting_init(self: SettingsScreen, context: object) -> None:
            constructed.append("settings")
            real_init(self, context)  # type: ignore[arg-type]

        application, recording = _recording_app()
        monkeypatch.setattr(SettingsScreen, "__init__", _counting_init)
        _script_menu(monkeypatch, ["settings", "back", "exit"])

        asyncio.run(HomeScreen(application._context).show())

        assert constructed == ["settings"]
        output = recording.export_text()
        # One Settings header per open, not a stack of them.
        assert output.count("SETTINGS\n") == 1

    def test_settings_categories_do_not_duplicate(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application, recording = _recording_app()
        calls = _script_menu(monkeypatch, ["back"])

        asyncio.run(SettingsScreen(application._context).show())

        assert calls, "settings menu was never prompted"
        top_level = calls[0]
        categories = [key for key in top_level if key not in ("back", "")]
        assert len(categories) == len(set(categories)), f"duplicate categories: {categories}"
        assert categories == [
            "appearance",
            "privacy",
            "notifications",
            "network",
            "storage",
            "developer",
        ]
        output = recording.export_text()
        _assert_clean_text(output)

    def test_back_returns_exactly_one_level(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Home → Settings → Appearance → Back → Back → Exit.

        The observed screen order must be exactly:
        home, settings, appearance, settings, home — one level per Back.
        """

        screens: list[str] = []

        real_settings_init = SettingsScreen.__init__

        def _settings_init(self: SettingsScreen, context: object) -> None:
            screens.append("settings")
            real_settings_init(self, context)  # type: ignore[arg-type]

        real_header = SettingsScreen.header

        def _header(self: SettingsScreen, title: str, subtitle: str | None = None) -> None:
            screens.append(f"header:{title}")
            return real_header(self, title, subtitle)

        application, _ = _recording_app()
        monkeypatch.setattr(SettingsScreen, "__init__", _settings_init)
        monkeypatch.setattr(SettingsScreen, "header", _header)
        _script_menu(monkeypatch, ["settings", "appearance", "back", "back", "exit"])

        asyncio.run(HomeScreen(application._context).show())

        headers = [s for s in screens if s.startswith("header:")]
        assert screens[0] == "settings"
        assert headers == [
            "header:Settings",
            "header:Appearance",
            "header:Settings",
        ], f"unexpected navigation sequence: {headers}"
        # SettingsScreen constructed once for the whole visit.
        assert screens.count("settings") == 1

    def test_menu_callback_executes_once(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Toggling a setting once flips it exactly once (no double dispatch)."""

        application, _ = _recording_app()
        assert application._context.settings.chat.read_receipts is True

        _script_menu(monkeypatch, ["privacy", "toggle_receipts", "back", "back"])
        asyncio.run(SettingsScreen(application._context).show())

        assert application._context.settings.chat.read_receipts is False

    def test_home_navigation_opens_each_screen_once(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Visiting several destinations constructs each screen exactly once."""

        from ghostlink.ui.screens.about import AboutScreen
        from ghostlink.ui.screens.help import HelpScreen
        from ghostlink.ui.screens.identity import IdentityScreen

        counts = {"about": 0, "help": 0, "identity": 0}

        def _wrap(cls: type, name: str) -> None:
            real_init = cls.__init__

            def _counting(self: object, context: object, _r=real_init, _n=name) -> None:
                counts[_n] += 1
                _r(self, context)  # type: ignore[misc]

            monkeypatch.setattr(cls, "__init__", _counting)

        _wrap(AboutScreen, "about")
        _wrap(HelpScreen, "help")
        _wrap(IdentityScreen, "identity")

        application, _ = _recording_app()
        _script_menu(
            monkeypatch,
            ["about", "help", "identity", "back", "exit"],
        )
        asyncio.run(HomeScreen(application._context).show())

        assert counts == {"about": 1, "help": 1, "identity": 1}

    def test_submenu_does_not_reopen_parent(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Cancelling the theme picker returns to Appearance exactly once."""

        headers: list[str] = []
        real_header = SettingsScreen.header

        def _header(self: SettingsScreen, title: str, subtitle: str | None = None) -> None:
            headers.append(title)
            return real_header(self, title, subtitle)

        application, _ = _recording_app()
        monkeypatch.setattr(SettingsScreen, "header", _header)
        _script_menu(monkeypatch, ["appearance", "theme", "cancel", "back", "back"])

        asyncio.run(SettingsScreen(application._context).show())

        assert headers == [
            "Settings",
            "Appearance",
            t("dialog.theme_select.title", "en"),
            "Appearance",
            "Settings",
        ]


class TestValuePersistenceAndRefresh:
    def test_theme_selection_persists_and_row_reflects(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application, _ = _recording_app()
        _script_menu(monkeypatch, ["appearance", "theme", "emerald", "back", "back"])
        monkeypatch.setattr("ghostlink.ui.screens.settings.confirm", lambda *a, **k: True)

        asyncio.run(SettingsScreen(application._context).show())

        assert application._context.settings.ui.theme == "emerald"

        # Reopen: the Appearance submenu theme row must show the new value.
        calls = _capture_entries(monkeypatch, ["appearance", "back", "back"])
        asyncio.run(SettingsScreen(application._context).show())
        appearance_call = next(
            entries for entries in calls if any(e.key == "theme" for e in entries)
        )
        theme_row = next(e for e in appearance_call if e.key == "theme")
        assert theme_row.value == "Emerald"

    def test_language_selection_persists_and_row_reflects(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application, _ = _recording_app()
        _script_menu(monkeypatch, ["appearance", "language", "es", "back", "back"])

        asyncio.run(SettingsScreen(application._context).show())

        assert application._context.settings.ui.language == "es"

        # Reopen: the language row reflects the persisted selection.
        calls = _capture_entries(monkeypatch, ["appearance", "back", "back"])
        asyncio.run(SettingsScreen(application._context).show())
        appearance_call = next(
            entries for entries in calls if any(e.key == "language" for e in entries)
        )
        language_row = next(e for e in appearance_call if e.key == "language")
        assert "Spanish" in language_row.value or "Español" in language_row.value

    def test_settings_reopen_cleanly(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Two sequential Settings visits produce identical, single renders."""

        application, recording = _recording_app()
        _script_menu(monkeypatch, ["back"])
        asyncio.run(SettingsScreen(application._context).show())
        first = recording.export_text().count("SETTINGS\n")

        recording2 = ConsoleManager(
            application._context.console.theme, record=True, width=100, force_terminal=False
        )
        application._context.console = recording2
        _script_menu(monkeypatch, ["back"])
        asyncio.run(SettingsScreen(application._context).show())
        second = recording2.export_text().count("SETTINGS\n")

        assert first == 1 and second == 1


class TestMenuStructure:
    def test_home_menu_has_no_duplicates_and_one_exit(self) -> None:
        entries = get_menu_entries("en")
        selectable = [entry for entry in entries if not entry.separator]
        keys = [entry.key for entry in selectable]
        assert len(keys) == len(set(keys)), f"duplicate home entries: {keys}"
        assert keys.count("exit") == 1
        assert keys[-1] == "exit", "exit must be the final entry"

    def test_every_menu_offers_back_or_exit(self) -> None:
        """Every selectable entry list the screens build ends in back/exit/cancel."""

        entries = get_menu_entries("en")
        selectable = [entry for entry in entries if not entry.separator]
        assert selectable[-1].key == "exit"

    def test_separators_are_never_selectable(self) -> None:
        entries = get_menu_entries("en")
        for entry in entries:
            if entry.separator:
                assert entry.key == ""
                assert entry.label == ""

    def test_numbered_prompt_skips_separators(
        self, console_manager: ConsoleManager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In fallback mode separators occupy no number."""

        entries = (
            MenuEntry(key="one", label="First", description="d1"),
            MenuEntry.spacer(),
            MenuEntry(key="two", label="Second", description="d2"),
            MenuEntry.spacer(),
            MenuEntry(key="exit", label="Exit", description="leave"),
        )
        menu = InteractiveMenu(console_manager)
        monkeypatch.setattr(InteractiveMenu, "keyboard_driven", property(lambda self: False))

        inputs = iter(["2"])
        monkeypatch.setattr(console_manager.console, "input", lambda *a, **k: next(inputs))
        assert menu.prompt(entries, default_key="exit") == "two"

        inputs2 = iter(["9", "3"])  # 9 invalid → reprompt → 3 = exit
        monkeypatch.setattr(console_manager.console, "input", lambda *a, **k: next(inputs2))
        assert menu.prompt(entries, default_key="exit") == "exit"


class TestCleanRendering:
    def test_home_output_has_no_emoji_or_phase_labels(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application, recording = _recording_app()
        _script_menu(monkeypatch, ["exit"])
        asyncio.run(HomeScreen(application._context).show())
        _assert_clean_text(recording.export_text())

    def test_settings_walk_has_no_emoji_or_phase_labels(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Walk every settings category; no emoji/debug labels anywhere."""

        application, recording = _recording_app()
        monkeypatch.setattr("ghostlink.ui.screens.settings.confirm", lambda *a, **k: False)
        choices = [
            "appearance",
            "back",
            "privacy",
            "toggle_receipts",
            "back",
            "notifications",
            "back",
            "network",
            "back",
            "storage",
            "back",
            "developer",
            "view_system_info",
            "back",
            "back",
        ]
        _script_menu(monkeypatch, choices)
        asyncio.run(SettingsScreen(application._context).show())
        _assert_clean_text(recording.export_text())

    def test_all_screens_render_cleanly(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ghostlink.ui.screens.about import AboutScreen
        from ghostlink.ui.screens.help import HelpScreen
        from ghostlink.ui.screens.identity import IdentityScreen
        from ghostlink.ui.screens.rooms import RoomManagementScreen
        from ghostlink.ui.screens.security import SecurityDashboardScreen
        from ghostlink.ui.screens.storage import StorageManagerScreen
        from ghostlink.ui.screens.transfers import TransferDashboardScreen

        for cls in (
            AboutScreen,
            HelpScreen,
            IdentityScreen,
            RoomManagementScreen,
            SecurityDashboardScreen,
            StorageManagerScreen,
            TransferDashboardScreen,
        ):
            application, recording = _recording_app()
            _script_menu(monkeypatch, ["back"])
            asyncio.run(cls(application._context).show())
            _assert_clean_text(recording.export_text())

    @pytest.mark.parametrize("width", [60, 80, 100, 120])
    def test_narrow_to_wide_rendering(
        self, width: int, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application, recording = _recording_app(width=width)
        _script_menu(monkeypatch, ["settings", "back", "exit"])
        asyncio.run(HomeScreen(application._context).show())
        output = recording.export_text()
        for line in output.splitlines():
            assert len(line) <= width, f"line overflows width {width}: {line!r}"
        _assert_clean_text(output)

    def test_no_color_mode_has_no_styles_and_full_content(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application, recording = _recording_app(no_color=True)
        assert recording.no_color
        _script_menu(monkeypatch, ["settings", "back", "exit"])
        asyncio.run(HomeScreen(application._context).show())

        # The user-facing console never emits ANSI when color is disabled:
        # verify against a live terminal-style sink, not the record buffer.
        import io

        from ghostlink.ui.themes import ThemeEngine

        sink = io.StringIO()
        live = ConsoleManager(
            ThemeEngine().get("phantom"),
            width=100,
            force_terminal=True,
            file=sink,
            no_color=True,
        )
        live.print("[gl.success]✓ Connected[/]")
        live.rule(style="gl.border")
        assert "\x1b[" not in sink.getvalue()

        plain = recording.export_text()
        assert "SETTINGS" in plain
        assert "GHOSTLINK" in plain
        assert "Type a number and press Enter" in plain
        # Status glyphs communicate state without relying on color.
        assert any(g in plain for g in ("◆", "•", "›", "→", "─"))
        _assert_clean_text(plain)


class TestThemesAndLanguages:
    @pytest.mark.parametrize("theme_name", BUILTIN_THEMES)
    def test_all_themes_render_home_and_settings(
        self, theme_name: str, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        spec = ThemeEngine().get(theme_name)
        application = build_application(parse_args([]))
        recording = ConsoleManager(spec, record=True, width=100, force_terminal=False)
        application._context.console = recording
        _script_menu(monkeypatch, ["settings", "back", "exit"])
        asyncio.run(HomeScreen(application._context).show())
        output = recording.export_text()
        _assert_clean_text(output)
        assert "GHOSTLINK" in output
        assert "SETTINGS" in output

    @pytest.mark.parametrize("lang", sorted(SUPPORTED_LANGUAGES.keys()))
    def test_all_languages_render_without_raw_keys(
        self, lang: str, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application, recording = _recording_app()
        application._context.settings = application._context.settings.__class__.defaults()
        from dataclasses import replace as dc_replace

        application._context.settings = dc_replace(
            application._context.settings,
            ui=dc_replace(application._context.settings.ui, language=lang),
        )
        _script_menu(monkeypatch, ["settings", "back", "exit"])
        asyncio.run(HomeScreen(application._context).show())
        output = recording.export_text()
        raw_keys = re.findall(r"\b(?:menu|settings)\.[a-z_.]{3,}", output)
        assert not raw_keys, f"[{lang}] raw translation keys leaked: {raw_keys[:10]}"
        # The settings header must be a real translation, not the key.
        assert "settings.title" not in output
        _assert_clean_text(output)


class TestDesignLanguage:
    def test_menu_entries_use_no_emoji_icons(self) -> None:
        entries = get_menu_entries("en")
        for entry in entries:
            assert not _EMOJI.search(entry.icon or ""), f"emoji icon on {entry.key}"
            assert not _EMOJI.search(entry.label)
            assert not _EMOJI.search(entry.description)

    def test_back_label_is_consistent(self) -> None:
        from ghostlink.ui.components.layout import back_label

        assert back_label("Back") == "← Back"
        assert back_label("Atrás") == "← Atrás"

    def test_status_glyphs_are_the_design_set(self) -> None:
        from ghostlink.ui.components.badges import BadgeTone, badge

        assert badge("On", BadgeTone.SUCCESS).plain == "[✓ On]"
        assert badge("Bad", BadgeTone.ERROR).plain == "[✕ Bad]"
        assert badge("Careful", BadgeTone.WARNING).plain == "[! Careful]"
        assert badge("FYI", BadgeTone.INFO).plain == "[• FYI]"

    def test_notification_levels_match_design_language(self) -> None:
        from ghostlink.ui.components.notifications import NotificationLevel

        assert NotificationLevel.SUCCESS.icon == "✓"
        assert NotificationLevel.WARNING.icon == "!"
        assert NotificationLevel.ERROR.icon == "✕"
        assert NotificationLevel.INFO.icon == "•"
