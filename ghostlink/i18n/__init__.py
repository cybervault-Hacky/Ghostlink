"""GhostLink Localization and Internationalization (i18n) package."""

from __future__ import annotations

from ghostlink.i18n.catalog import CATALOG
from ghostlink.i18n.translator import (
    get_current_language,
    set_current_language,
    t,
)

__all__ = [
    "CATALOG",
    "get_current_language",
    "set_current_language",
    "t",
]
