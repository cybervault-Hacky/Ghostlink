"""In-memory storage backend.

Used by tests and for deliberately ephemeral state. Reads return deep copies
so callers can never mutate the stored document by accident.
"""

from __future__ import annotations

import copy
import threading
from typing import Any

from ghostlink.storage.backend import StorageBackend


class MemoryStorage(StorageBackend):
    """Non-persistent, thread-safe dictionary storage."""

    def __init__(self, initial: dict[str, Any] | None = None) -> None:
        self._data: dict[str, Any] = copy.deepcopy(initial) if initial else {}
        self._lock = threading.RLock()

    @property
    def name(self) -> str:
        return "memory"

    def _read_all(self) -> dict[str, Any]:
        with self._lock:
            return copy.deepcopy(self._data)

    def _write_all(self, data: dict[str, Any]) -> None:
        with self._lock:
            self._data = copy.deepcopy(data)
