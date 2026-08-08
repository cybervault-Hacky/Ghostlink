"""Storage abstraction layer.

Backends implement the :class:`StorageBackend` mapping contract, so the rest
of the application never touches files directly. Phase 1 ships a durable,
atomic JSON document store and an in-memory backend for tests and ephemeral
state; Phase 2 can add encrypted backends behind the same interface without
touching calling code.
"""

from __future__ import annotations

from ghostlink.storage.backend import StorageBackend
from ghostlink.storage.json_store import JsonFileStorage
from ghostlink.storage.manager import StorageManager
from ghostlink.storage.memory import MemoryStorage

__all__ = [
    "JsonFileStorage",
    "MemoryStorage",
    "StorageBackend",
    "StorageManager",
]
