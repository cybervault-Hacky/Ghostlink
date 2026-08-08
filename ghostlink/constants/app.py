"""Application metadata. This module is the single source of truth for names,
versioning, and release labels displayed across the interface and docs."""

from __future__ import annotations

from ghostlink import __version__

APP_NAME: str = "GhostLink"
APP_SLUG: str = "ghostlink"
APP_VERSION: str = __version__
APP_TAGLINE: str = "Private conversations. Zero compromise."
APP_DESCRIPTION: str = (
    "GhostLink is a terminal-only encrypted messenger designed for Termux on "
    "Android, with first-class support for desktop Linux."
)

DEVELOPER: str = "cybervault-Hacky"
LICENSE_NAME: str = "MIT"
BUILD_DATE: str = "2026-08-08"
REPOSITORY_URL: str = "https://github.com/cybervault-Hacky/Ghostlink"

RELEASE_PHASE: str = "Phase 3"
RELEASE_CODENAME: str = "Secure Messaging"
RELEASE_LABEL: str = f"{RELEASE_PHASE} · {RELEASE_CODENAME}"

MIN_PYTHON: tuple[int, int] = (3, 12)

DEFAULT_THEME: str = "phantom"
DEFAULT_LANGUAGE: str = "en"
SUPPORTED_LANGUAGES: dict[str, str] = {"en": "English"}

# ------------------------------------------------------------------- chat
CHAT_HISTORY_MODES: frozenset[str] = frozenset({"disabled", "session", "encrypted"})
CHAT_TIMESTAMP_FORMATS: frozenset[str] = frozenset({"24h", "12h"})
CHAT_NOTIFICATION_STYLES: frozenset[str] = frozenset({"banner", "compact", "muted"})
DEFAULT_HISTORY_MODE: str = "disabled"
DEFAULT_TIMESTAMP_FORMAT: str = "24h"
DEFAULT_NOTIFICATION_STYLE: str = "banner"
