"""Local group registry (Phase 6B) — metadata-only persistence.

One document, ``groups.json``, under the state directory via the standard
``StorageManager`` (atomic tempfile→fsync→replace writes, ``0600``
permissions, corruption surfacing as typed storage errors). Records are
metadata only: rosters of fingerprints, public keys, handles, epochs, and
signed event descriptors. **No tokens, no private keys, no session keys,
no group encryption keys** (there are none — docs/GROUPS.md §26).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ghostlink.constants.net import GROUP_MAX_GROUPS, GROUP_RETENTION_HOURS
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.groups import GroupStateError
from ghostlink.groups.ids import is_valid_group_id
from ghostlink.groups.models import (
    TERMINAL_GROUP_STATES,
    LocalGroupRecord,
)
from ghostlink.storage.backend import StorageBackend
from ghostlink.storage.manager import StorageManager

_logger = get_logger("groups.registry")

NAMESPACE: str = "groups"


class LocalGroupRegistry:
    """Persistent, validated store for the user's group records."""

    def __init__(self, storage: StorageManager) -> None:
        self._storage = storage

    def _backend(self) -> StorageBackend:  # cached namespace document
        return self._storage.store(NAMESPACE)

    # ------------------------------------------------------------------ CRUD

    def save(self, record: LocalGroupRecord) -> None:
        """Persist a record atomically; new active groups obey the cap."""

        backend = self._backend()
        existing = backend.get(record.group_id)
        if existing is None:
            active = sum(1 for other in self.list_all() if other.state not in TERMINAL_GROUP_STATES)
            if record.state not in TERMINAL_GROUP_STATES and active >= GROUP_MAX_GROUPS:
                raise GroupStateError(
                    f"You already track {GROUP_MAX_GROUPS} active groups.",
                    hint="Leave or archive old groups before creating or joining more.",
                )
        backend[record.group_id] = record.to_dict()
        _logger.info(
            "group %s saved — state=%s epoch=%d members=%d",
            record.group_id,
            record.state.value,
            record.epoch,
            record.member_count(),
        )

    def get(self, group_id: str) -> LocalGroupRecord | None:
        if not is_valid_group_id(group_id):
            return None
        raw = self._backend().get(group_id)
        if raw is None:
            return None
        if not isinstance(raw, dict):
            from ghostlink.exceptions.storage import StorageCorruptionError

            raise StorageCorruptionError(
                f"The stored record for group {group_id} is malformed.",
                hint="Remove the groups store to reset locally tracked groups.",
            )
        return LocalGroupRecord.from_dict(raw)

    def require(self, group_id: str) -> LocalGroupRecord:
        from ghostlink.exceptions.groups import GroupUnknownError

        record = self.get(group_id)
        if record is None:
            raise GroupUnknownError(
                f"No local record of group {group_id}.",
                hint="Create or join the group first (ghostlink group list shows yours).",
            )
        return record

    def list_all(self) -> list[LocalGroupRecord]:
        backend = self._backend()
        records = []
        for key in sorted(backend.keys()):
            raw = backend[key]
            if isinstance(raw, dict):
                records.append(LocalGroupRecord.from_dict(raw))
        return records

    def delete(self, group_id: str) -> bool:
        backend = self._backend()
        if group_id in backend:
            del backend[group_id]
            _logger.info("group %s record deleted", group_id)
            return True
        return False

    # -------------------------------------------------------------- retention

    def purge_archived(
        self,
        retention: timedelta | None = None,
        *,
        now: datetime | None = None,
    ) -> int:
        """Delete terminal (left/removed/dissolved/defunct) records past retention."""

        window = retention if retention is not None else timedelta(hours=GROUP_RETENTION_HOURS)
        moment = now if now is not None else datetime.now(UTC)
        purged = 0
        backend = self._backend()
        for record in self.list_all():
            if (
                record.state in TERMINAL_GROUP_STATES
                and moment - record.updated_at > window
                and backend.pop(record.group_id, None) is not None
            ):
                purged += 1
                _logger.info("group %s archived record purged", record.group_id)
        return purged

    # -------------------------------------------------------------- hygiene

    @staticmethod
    def assert_metadata_only(record: LocalGroupRecord) -> None:
        """Guard rail for tests and saves: the record must contain no secrets.

        The model only carries metadata by construction, so this is a
        defense-in-depth assertion that persisted documents never grow
        secret-looking fields (tokens, private keys, session keys).
        """

        forbidden = {"token", "token_hash", "private_key", "secret", "session_key"}
        document = record.to_dict()

        def _scan(mapping: dict[str, object], path: str) -> None:
            for key, value in mapping.items():
                lowered = key.lower()
                for marker in forbidden:
                    if marker in lowered:
                        raise GroupStateError(
                            f"Group record would persist a forbidden field '{path}{key}'.",
                            hint="Group stores are metadata-only by design (docs/GROUPS.md §26).",
                        )
                if isinstance(value, dict):
                    _scan(value, f"{path}{key}.")
                elif isinstance(value, list):
                    for item in value:
                        if isinstance(item, dict):
                            _scan(item, f"{path}{key}[].")

        _scan(document, "")
