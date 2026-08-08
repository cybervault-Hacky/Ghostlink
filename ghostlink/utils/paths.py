"""Platform-aware directory resolution.

GhostLink follows the XDG Base Directory specification on Linux and keeps the
same conventions inside Termux (where ``$HOME`` is the app's private home).
All functions accept an injectable environment mapping so behaviour is fully
testable without mutating ``os.environ``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path

from ghostlink.constants.app import APP_SLUG
from ghostlink.constants.files import (
    ENV_XDG_CONFIG_HOME,
    ENV_XDG_DATA_HOME,
)
from ghostlink.exceptions.base import GhostLinkError
from ghostlink.exceptions.storage import StorageError


def xdg_config_home(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the XDG configuration home (``~/.config`` by default)."""

    environ = os.environ if env is None else env
    base = home if home is not None else Path.home()
    override = environ.get(ENV_XDG_CONFIG_HOME, "").strip()
    return Path(override).expanduser() if override else base / ".config"


def xdg_data_home(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return the XDG data home (``~/.local/share`` by default)."""

    environ = os.environ if env is None else env
    base = home if home is not None else Path.home()
    override = environ.get(ENV_XDG_DATA_HOME, "").strip()
    return Path(override).expanduser() if override else base / ".local" / "share"


def default_config_dir(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return GhostLink's default configuration directory."""

    return xdg_config_home(env=env, home=home) / APP_SLUG


def default_data_dir(
    *,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> Path:
    """Return GhostLink's default data directory."""

    return xdg_data_home(env=env, home=home) / APP_SLUG


def ensure_directory(
    path: Path,
    *,
    error_type: type[GhostLinkError] = StorageError,
) -> Path:
    """Create ``path`` (including parents) and return it.

    Raises the requested :class:`GhostLinkError` subclass with an actionable
    hint when the directory cannot be created — for example due to file
    permissions — instead of leaking a bare ``OSError`` to the interface.
    """

    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise error_type(
            f"Unable to create directory '{path}'.",
            hint="Check that the parent path is writable, or choose another "
            "location with the --data-dir flag.",
        ) from exc
    return path
