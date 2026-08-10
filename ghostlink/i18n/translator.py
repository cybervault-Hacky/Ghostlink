"""Localization and translation service for GhostLink."""

from __future__ import annotations

from typing import Any

from ghostlink.constants.app import DEFAULT_LANGUAGE, SUPPORTED_LANGUAGES
from ghostlink.i18n.catalog import CATALOG

_current_language: str = DEFAULT_LANGUAGE


def set_current_language(lang: str) -> None:
    """Set the globally active interface language."""

    global _current_language
    if lang in SUPPORTED_LANGUAGES:
        _current_language = lang


def get_current_language() -> str:
    """Get the currently active interface language code."""

    return _current_language


def t(key: str, lang: str | None = None, **kwargs: Any) -> str:
    """Translate a message key into the requested (or active) language.

    Falls back to English if the key is missing in the target language catalog,
    or returns the key itself formatted with kwargs if missing everywhere.
    """

    target_lang = lang if lang is not None else _current_language
    catalog = CATALOG.get(target_lang, CATALOG.get(DEFAULT_LANGUAGE, {}))
    template = catalog.get(key)
    if template is None:
        template = CATALOG.get(DEFAULT_LANGUAGE, {}).get(key, key)
    if kwargs:
        try:
            return template.format(**kwargs)
        except (KeyError, ValueError, IndexError):
            return template
    return template
