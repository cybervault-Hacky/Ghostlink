"""Local invite registry (Phase 5).

Persists invite *metadata* — never the secret token — via the existing
``StorageManager`` in its own ``secure_invites`` namespace (owner-only JSON
store). Retention: records whose lifecycle ended longer than the configured
window are purged, keeping only what the invite lifecycle still needs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from ghostlink.core.logging import get_logger
from ghostlink.invites.models import TERMINAL_STATES, InviteRecord, InviteState
from ghostlink.storage.backend import StorageBackend
from ghostlink.storage.manager import StorageManager

_logger = get_logger("invites.registry")

_INVITES_NAMESPACE: str = "secure_invites"


class LocalInviteRegistry:
    """Durable store of locally-created (or redeemed) invite records."""

    def __init__(self, storage: StorageManager) -> None:
        self._storage = storage

    def _backend(self) -> StorageBackend:
        return self._storage.store(_INVITES_NAMESPACE)

    # ------------------------------------------------------------------- CRUD

    def save(self, record: InviteRecord) -> None:
        self._backend()[record.invite_id] = record.to_dict()
        _logger.debug("invite record saved — %s (%s)", record.invite_id, record.state.value)

    def get(self, invite_id: str) -> InviteRecord | None:
        raw = self._backend().get(invite_id.strip().lower())
        if raw is None:
            return None
        if not isinstance(raw, dict):
            return None
        return InviteRecord.from_dict(raw)

    def list_all(self) -> list[InviteRecord]:
        """Every stored record, newest first."""

        records: list[InviteRecord] = []
        backend = self._backend()
        for key in backend:
            raw = backend[key]
            if isinstance(raw, dict):
                records.append(InviteRecord.from_dict(raw))
        return sorted(records, key=lambda item: item.created_at, reverse=True)

    def refresh_expired(self, *, now: datetime | None = None) -> list[InviteRecord]:
        """Persist ACTIVE → EXPIRED for every record past its deadline."""

        moment = now if now is not None else datetime.now(UTC)
        moved: list[InviteRecord] = []
        for record in self.list_all():
            if record.state is InviteState.ACTIVE and record.is_due(moment):
                record.transition(InviteState.EXPIRED)
                self.save(record)
                moved.append(record)
        return moved

    def delete(self, invite_id: str) -> bool:
        backend = self._backend()
        key = invite_id.strip().lower()
        if key not in backend:
            return False
        del backend[key]
        return True

    # -------------------------------------------------------------- retention

    def purge_terminal(self, retention: timedelta, *, now: datetime | None = None) -> int:
        """Drop terminal records older than ``retention``; returns the count."""

        moment = now if now is not None else datetime.now(UTC)
        cutoff = moment - retention
        removed = 0
        for record in self.list_all():
            if record.state not in TERMINAL_STATES:
                continue
            if record.expires_at < cutoff and self.delete(record.invite_id):
                removed += 1
        if removed:
            _logger.info("purged %d expired/revoked invite record(s)", removed)
        return removed
