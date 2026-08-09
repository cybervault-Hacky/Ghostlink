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
BUILD_DATE: str = "2026-08-09"
REPOSITORY_URL: str = "https://github.com/cybervault-Hacky/Ghostlink"

RELEASE_PHASE: str = "Phase 13"
RELEASE_CODENAME: str = "Production Infrastructure, Database & Deployment Hardening"
RELEASE_LABEL: str = f"{RELEASE_PHASE} · {RELEASE_CODENAME}"

MIN_PYTHON: tuple[int, int] = (3, 11)

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

# ---------------------------------------------------------------- transfers
TRANSFER_DEFAULT_MAX_FILE_MB: int = 100
TRANSFER_MAX_FILE_MB: int = 4096
TRANSFER_DEFAULT_CONCURRENT: int = 3
TRANSFER_MAX_CONCURRENT: int = 10
TRANSFER_DEFAULT_CHUNK_KB: int = 4
TRANSFER_MIN_CHUNK_KB: int = 1
TRANSFER_MAX_CHUNK_KB: int = 4  # bounded by the secure-frame payload budget
TRANSFER_DEFAULT_TEMP_LIMIT_MB: int = 1024
TRANSFER_MIN_TEMP_LIMIT_MB: int = 16
TRANSFER_MAX_TEMP_LIMIT_MB: int = 65536
# Resume state travels as a 4096-byte bitmap → at most 32768 chunks per file.
TRANSFER_MAX_CHUNKS_PER_FILE: int = 32768
TRANSFER_WINDOW_CHUNKS: int = 8  # sender backpressure window (chunks in flight)
TRANSFER_DEFAULT_EXPIRY_MINUTES: int = 60
