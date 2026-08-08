"""Identity persistence (Phase 5).

Built on the existing storage abstraction: one document inside the
``identity`` namespace managed by :class:`~ghostlink.storage.manager.StorageManager`.
The underlying JSON store writes atomically with owner-only (0600)
permissions, so identity material at rest is readable by the owner only.
"""

from __future__ import annotations

from pathlib import Path

from ghostlink.core.logging import get_logger
from ghostlink.exceptions.storage import StorageCorruptionError
from ghostlink.identity.identity import LocalIdentity
from ghostlink.storage.manager import StorageManager

_logger = get_logger("identity.storage")

_IDENTITY_KEY: str = "local"
_IDENTITY_NAMESPACE: str = "identity"


class IdentityStore:
    """Loads and persists the single local identity."""

    def __init__(self, storage: StorageManager) -> None:
        self._storage = storage

    @property
    def store_path(self) -> Path:
        return self._storage.root / f"{_IDENTITY_NAMESPACE}.json"

    def load(self) -> LocalIdentity | None:
        """The stored identity, or ``None`` when none exists yet."""

        backend = self._storage.store(_IDENTITY_NAMESPACE)
        raw = backend.get(_IDENTITY_KEY)
        if raw is None:
            return None
        if not isinstance(raw, dict):
            raise StorageCorruptionError(
                "The identity store holds an unexpected document shape.",
                hint="Remove the identity store to start over with a fresh identity.",
            )
        return LocalIdentity.from_storage_dict(raw)

    def save(self, identity: LocalIdentity) -> None:
        """Persist the identity (owner-only file permissions)."""

        self._storage.store(_IDENTITY_NAMESPACE)[_IDENTITY_KEY] = identity.to_storage_dict()
        _logger.debug("identity persisted — %s", identity.identity_id)

    def delete(self) -> bool:
        """Remove the stored identity; returns True when one existed."""

        backend = self._storage.store(_IDENTITY_NAMESPACE)
        if _IDENTITY_KEY not in backend:
            return False
        del backend[_IDENTITY_KEY]
        _logger.info("local identity deleted")
        return True
