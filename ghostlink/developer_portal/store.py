"""Local developer-portal token store (Phase 12).

Persists short-lived access tokens and rotating refresh tokens at 0600,
with atomic writes, symlink refusal, schema versioning, and corruption
detection. Reuses the standard-library JSON storage patterns. Permanent
developer secrets are never stored here.
"""

from __future__ import annotations

import contextlib
import os
import stat
from pathlib import Path

from ghostlink.storage.json_store import JsonFileStorage

SCHEMA_VERSION = 1
STORE_DIR_NAME = "developer_portal"


class PortalStoreError(Exception):
    """Local portal-store failure (never carries secret material)."""


def _secure_dir(path: Path) -> None:
    if path.is_symlink():
        raise PortalStoreError("Developer-portal store directory is a symlink.")
    path.mkdir(parents=True, exist_ok=True)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o700)


class PortalStore:
    def __init__(self, data_dir: Path) -> None:
        self._dir = data_dir / STORE_DIR_NAME
        _secure_dir(self._dir)
        self._store = JsonFileStorage(self._dir / "tokens.json")

    @property
    def directory(self) -> Path:
        return self._dir

    def _check_perms(self) -> None:
        path = self._store.path
        if path.is_symlink():
            raise PortalStoreError("Portal token file is a symlink.")
        if path.exists():
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode & 0o022:
                raise PortalStoreError("Portal token file is group/world writable.")

    def save(self, data: dict[str, object]) -> None:
        self._check_perms()
        payload = {"v": SCHEMA_VERSION, "data": data}
        self._store.replace(payload)

    def load(self) -> dict[str, object]:
        self._check_perms()
        raw = dict(self._store)
        if not raw:
            return {}
        version = raw.get("v")
        if not isinstance(version, int) or version > SCHEMA_VERSION:
            raise PortalStoreError("Portal token store uses an unsupported schema version.")
        data = raw.get("data")
        if not isinstance(data, dict):
            raise PortalStoreError("Portal token store is corrupt.")
        return data

    def clear(self) -> None:
        self._store.replace({"v": SCHEMA_VERSION, "data": {}})


__all__ = ["SCHEMA_VERSION", "STORE_DIR_NAME", "PortalStore", "PortalStoreError"]
