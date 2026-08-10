"""Tests for the GhostLink localization and translation (i18n) subsystem."""

from __future__ import annotations

import pytest

from ghostlink.constants.app import LANGUAGE_NAMES, SUPPORTED_LANGUAGES
from ghostlink.i18n import (
    CATALOG,
    get_current_language,
    set_current_language,
    t,
)
from ghostlink.ui.screens.home import get_menu_entries

REQUIRED_LANGUAGES = {"en", "hi", "hinglish", "mr", "es", "fr", "de", "pt", "ja"}
CORE_KEYS = [
    "menu.create_room.label",
    "menu.join_room.label",
    "menu.settings.label",
    "menu.about.label",
    "menu.exit.label",
    "settings.title",
    "settings.cat.appearance.title",
    "settings.cat.privacy.title",
    "settings.cat.notifications.title",
    "settings.cat.network.title",
    "settings.cat.storage.title",
    "settings.cat.developer.title",
    "settings.theme.label",
    "settings.language.label",
    "status.enabled",
    "status.disabled",
    "action.apply",
    "action.cancel",
    "action.back",
]


class TestI18nCatalog:
    def test_all_required_languages_supported(self) -> None:
        assert set(SUPPORTED_LANGUAGES.keys()) >= REQUIRED_LANGUAGES
        assert set(LANGUAGE_NAMES.keys()) >= REQUIRED_LANGUAGES
        assert set(CATALOG.keys()) >= REQUIRED_LANGUAGES

    @pytest.mark.parametrize("lang", sorted(REQUIRED_LANGUAGES))
    def test_catalog_contains_all_core_keys(self, lang: str) -> None:
        catalog = CATALOG[lang]
        for key in CORE_KEYS:
            assert key in catalog, f"Language '{lang}' is missing key '{key}'"
            assert catalog[key].strip(), f"Language '{lang}' has empty value for key '{key}'"

    def test_translation_formatting_placeholders(self) -> None:
        result = t("dialog.theme_changed", "en", theme="Arctic")
        assert "Arctic" in result

        hi_result = t("dialog.theme_changed", "hi", theme="Arctic")
        assert "Arctic" in hi_result

    def test_fallback_to_english_on_missing_key(self) -> None:
        assert t("nonexistent.key.test", "es") == "nonexistent.key.test"

    def test_set_and_get_current_language(self) -> None:
        original = get_current_language()
        try:
            set_current_language("es")
            assert get_current_language() == "es"
            assert t("action.save") == "Guardar"

            set_current_language("hi")
            assert get_current_language() == "hi"
            assert t("action.save") == "सहेजें"

            set_current_language("ja")
            assert get_current_language() == "ja"
            assert t("action.save") == "保存"

            # Unsupported language does not override valid state
            set_current_language("unsupported_xyz")
            assert get_current_language() == "ja"
        finally:
            set_current_language(original)

    @pytest.mark.parametrize("lang", sorted(REQUIRED_LANGUAGES))
    def test_get_menu_entries_localized(self, lang: str) -> None:
        entries = get_menu_entries(lang)
        assert len(entries) == 9
        keys = [entry.key for entry in entries]
        assert keys == [
            "create-room",
            "join-room",
            "transfers",
            "security",
            "identity",
            "settings",
            "help",
            "about",
            "exit",
        ]
        assert all(entry.label.strip() for entry in entries)
        assert all(entry.description.strip() for entry in entries)
