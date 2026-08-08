"""Durable JSON document storage with atomic writes.

Writes go to a sibling temporary file, are flushed to disk, and are then
atomically renamed over the target — a crash mid-write can never leave a
truncated document behind. Files are created with owner-only permissions
(0600), appropriate for a privacy-focused messenger.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from ghostlink.exceptions.storage import (
    StorageCorruptionError,
    StorageReadError,
    StorageWriteError,
)
from ghostlink.storage.backend import StorageBackend

FILE_PERMISSIONS = 0o600


class JsonFileStorage(StorageBackend):
    """Mapping-style storage persisted as a single JSON document."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = threading.RLock()

    @property
    def name(self) -> str:
        return f"json:{self._path.name}"

    @property
    def path(self) -> Path:
        return self._path

    def _read_all(self) -> dict[str, Any]:
        with self._lock:
            if not self._path.exists():
                return {}
            try:
                with self._path.open("r", encoding="utf-8") as handle:
                    data = json.load(handle)
            except json.JSONDecodeError as exc:
                raise StorageCorruptionError(
                    f"Storage document '{self._path}' is not valid JSON.",
                    hint="Inspect or remove the file; GhostLink will recreate it "
                    "with fresh state on next launch.",
                ) from exc
            except OSError as exc:
                raise StorageReadError(
                    f"Storage document '{self._path}' could not be read.",
                    hint="Check file permissions for the current user.",
                ) from exc
            if not isinstance(data, dict):
                raise StorageCorruptionError(
                    f"Storage document '{self._path}' must contain a JSON object.",
                    hint="Inspect or remove the file; GhostLink will recreate it "
                    "with fresh state on next launch.",
                )
            return data

    def _write_all(self, data: dict[str, Any]) -> None:
        with self._lock:
            temp_path = self._path.with_name(f"{self._path.name}.tmp")
            try:
                self._path.parent.mkdir(parents=True, exist_ok=True)
                with temp_path.open("w", encoding="utf-8") as handle:
                    json.dump(data, handle, indent=2, sort_keys=True)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temp_path, FILE_PERMISSIONS)
                os.replace(temp_path, self._path)
            except TypeError as exc:
                temp_path.unlink(missing_ok=True)
                raise StorageWriteError(
                    f"Value for '{self._path}' is not JSON-serializable: {exc}.",
                    hint="Store only JSON-compatible types: str, int, float, "
                    "bool, None, list, and dict.",
                ) from exc
            except OSError as exc:
                temp_path.unlink(missing_ok=True)
                raise StorageWriteError(
                    f"Storage document '{self._path}' could not be written.",
                    hint="Check that the data directory is writable and has free space.",
                ) from exc
