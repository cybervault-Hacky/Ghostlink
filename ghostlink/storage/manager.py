"""Storage manager: namespaced stores rooted in the data directory."""

from __future__ import annotations

import re
from pathlib import Path

from ghostlink.exceptions.storage import StorageError
from ghostlink.storage.backend import StorageBackend
from ghostlink.storage.json_store import JsonFileStorage
from ghostlink.utils.paths import ensure_directory

_NAMESPACE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class StorageManager:
    """Opens and caches per-namespace storage backends.

    Each namespace maps to one document (``<namespace>.json``) beneath the
    root directory, keeping domains like ``session`` — and Phase 2's
    ``identity`` or ``rooms`` — cleanly separated.
    """

    def __init__(self, root: Path) -> None:
        self._root = ensure_directory(root)
        self._stores: dict[str, StorageBackend] = {}

    @property
    def root(self) -> Path:
        return self._root

    def store(self, namespace: str) -> StorageBackend:
        """Return the (cached) backend for ``namespace``, creating it lazily."""

        if not _NAMESPACE_PATTERN.fullmatch(namespace):
            raise StorageError(
                f"Invalid storage namespace '{namespace}'.",
                hint="Namespaces must be lowercase: letters, digits, '-' and "
                "'_', starting with a letter or digit.",
            )
        backend = self._stores.get(namespace)
        if backend is None:
            backend = JsonFileStorage(self._root / f"{namespace}.json")
            self._stores[namespace] = backend
        return backend

    def namespaces(self) -> tuple[str, ...]:
        """Return the namespaces opened during this run."""

        return tuple(sorted(self._stores))
