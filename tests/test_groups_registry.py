"""Local group registry: atomic, metadata-only persistence (Phase 6B)."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from ghostlink.exceptions.config import ConfigValidationError
from ghostlink.exceptions.groups import GroupStateError, GroupUnknownError
from ghostlink.exceptions.storage import StorageCorruptionError, StorageWriteError
from ghostlink.groups.models import (
    GroupMember,
    GroupRole,
    LocalGroupRecord,
    LocalGroupState,
)
from ghostlink.groups.registry import NAMESPACE, LocalGroupRegistry
from ghostlink.identity.identity import LocalIdentity
from ghostlink.storage.manager import StorageManager
from tests.group_helpers import fingerprint_of

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _record_for(
    identity: LocalIdentity,
    *,
    state: LocalGroupState = LocalGroupState.ACTIVE,
    group_id: str = "gl-group-AAAA-BBBB-CCCC",
) -> LocalGroupRecord:
    fp = fingerprint_of(identity)
    owner = GroupMember(
        fingerprint=fp,
        handle=identity.identity_id,
        display_name=identity.identity_id,
        public_key_hex=identity.public_key_hex,
        role=GroupRole.OWNER,
        joined_epoch=1,
    )
    return LocalGroupRecord(
        group_id=group_id,
        name="Persisted",
        owner_fingerprint=fp,
        owner_public_key_hex=identity.public_key_hex,
        my_fingerprint=fp,
        epoch=1,
        members={fp: owner},
        state=state,
        relay_url="ws://127.0.0.1:1/relay",
        created_at=T0,
        updated_at=T0,
    )


@pytest.fixture()
def registry(tmp_path: Path) -> LocalGroupRegistry:
    return LocalGroupRegistry(StorageManager(tmp_path))


class TestRoundtrip:
    def test_save_get_reload(self, tmp_path: Path) -> None:
        identity = LocalIdentity.generate()
        record = _record_for(identity)
        LocalGroupRegistry(StorageManager(tmp_path)).save(record)
        # A fresh registry over the same directory sees the record (restart).
        reloaded = LocalGroupRegistry(StorageManager(tmp_path))
        restored = reloaded.require(record.group_id)
        assert restored.group_id == record.group_id
        assert restored.epoch == 1
        assert restored.owner_public_key_hex == identity.public_key_hex

    def test_require_unknown_raises(self, registry: LocalGroupRegistry) -> None:
        with pytest.raises(GroupUnknownError):
            registry.require("gl-group-ZZZZ-YYYY-XXXX")

    def test_get_malformed_id_returns_none(self, registry: LocalGroupRegistry) -> None:
        assert registry.get("not-a-group") is None

    def test_list_and_delete(self, registry: LocalGroupRegistry) -> None:
        first = _record_for(LocalIdentity.generate(), group_id="gl-group-AAAA-BBBB-CCCC")
        second = _record_for(LocalIdentity.generate(), group_id="gl-group-DDDD-EEEE-FFFF")
        registry.save(first)
        registry.save(second)
        assert [r.group_id for r in registry.list_all()] == sorted(
            [first.group_id, second.group_id]
        )
        assert registry.delete(first.group_id) is True
        assert registry.delete(first.group_id) is False
        assert [r.group_id for r in registry.list_all()] == [second.group_id]

    def test_active_group_cap(self, registry: LocalGroupRegistry) -> None:
        from ghostlink.constants.net import GROUP_MAX_GROUPS
        from ghostlink.groups.ids import generate_group_id

        for _ in range(GROUP_MAX_GROUPS):
            registry.save(_record_for(LocalIdentity.generate(), group_id=generate_group_id()))
        with pytest.raises(GroupStateError):
            registry.save(_record_for(LocalIdentity.generate(), group_id=generate_group_id()))
        # Terminal records do not count against the active cap.
        terminal = _record_for(
            LocalIdentity.generate(),
            group_id=generate_group_id(),
            state=LocalGroupState.DISSOLVED,
        )
        registry.save(terminal)

    def test_atomic_write_permissions(self, tmp_path: Path) -> None:
        registry = LocalGroupRegistry(StorageManager(tmp_path))
        record = _record_for(LocalIdentity.generate())
        registry.save(record)
        target = tmp_path / f"{NAMESPACE}.json"
        assert target.exists()
        mode = os.stat(target).st_mode & 0o777
        assert mode == 0o600, oct(mode)


class TestCorruption:
    def test_garbage_document_surfaces_typed_error(self, tmp_path: Path) -> None:
        registry = LocalGroupRegistry(StorageManager(tmp_path))
        target = tmp_path / f"{NAMESPACE}.json"
        target.write_text("{ not json", encoding="utf-8")
        with pytest.raises(StorageCorruptionError):
            registry.list_all()

    def test_tampered_entry_type_surfaces_typed_error(self, tmp_path: Path) -> None:
        registry = LocalGroupRegistry(StorageManager(tmp_path))
        target = tmp_path / f"{NAMESPACE}.json"
        target.write_text(
            json.dumps({"gl-group-AAAA-BBBB-CCCC": "junk"}),
            encoding="utf-8",
        )
        with pytest.raises(StorageCorruptionError):
            registry.get("gl-group-AAAA-BBBB-CCCC")

    def test_missing_fields_surface_typed_error(self, tmp_path: Path) -> None:
        registry = LocalGroupRegistry(StorageManager(tmp_path))
        target = tmp_path / f"{NAMESPACE}.json"
        target.write_text(
            json.dumps({"gl-group-AAAA-BBBB-CCCC": {"name": "x"}}),
            encoding="utf-8",
        )
        with pytest.raises(ConfigValidationError):
            registry.get("gl-group-AAAA-BBBB-CCCC")

    def test_atomic_failure_leaves_prior_state(self, tmp_path: Path, monkeypatch) -> None:
        registry = LocalGroupRegistry(StorageManager(tmp_path))
        record = _record_for(LocalIdentity.generate())
        registry.save(record)
        before = (tmp_path / f"{NAMESPACE}.json").read_text(encoding="utf-8")

        def boom(*args: object, **kwargs: object) -> None:
            raise OSError("disk full")

        monkeypatch.setattr("ghostlink.storage.json_store.os.replace", boom)
        with pytest.raises(StorageWriteError):
            registry.save(_record_for(LocalIdentity.generate(), group_id="gl-group-QQQQ-WWWW-EEEE"))
        monkeypatch.undo()
        after = (tmp_path / f"{NAMESPACE}.json").read_text(encoding="utf-8")
        assert after == before  # no partial state was written


class TestRetention:
    def test_purge_archived_only_terminal_records(self, tmp_path: Path) -> None:
        registry = LocalGroupRegistry(StorageManager(tmp_path))
        old_terminal = _record_for(LocalIdentity.generate(), state=LocalGroupState.DISSOLVED)
        active = _record_for(LocalIdentity.generate(), group_id="gl-group-NEWA-AAAA-BBBB")
        registry.save(old_terminal)
        registry.save(active)
        purged = registry.purge_archived(timedelta(hours=1), now=T0 + timedelta(days=2))
        assert purged == 1
        assert registry.get(old_terminal.group_id) is None
        assert registry.get(active.group_id) is not None

    def test_purge_keeps_recent_terminal(self, tmp_path: Path) -> None:
        registry = LocalGroupRegistry(StorageManager(tmp_path))
        record = _record_for(LocalIdentity.generate(), state=LocalGroupState.LEFT)
        registry.save(record)
        assert registry.purge_archived(timedelta(days=30), now=T0 + timedelta(hours=1)) == 0
        assert registry.get(record.group_id) is not None


class TestMetadataOnlyGuard:
    def test_records_are_metadata_only_by_construction(self) -> None:
        identity = LocalIdentity.generate()
        record = _record_for(identity)
        LocalGroupRegistry.assert_metadata_only(record)  # no raise
        document = json.dumps(record.to_dict())
        for marker in ("token", "private_key", "secret", "session_key"):
            assert marker not in document

    def test_guard_catches_secret_looking_fields(self, monkeypatch) -> None:
        identity = LocalIdentity.generate()
        record = _record_for(identity)
        original = LocalGroupRecord.to_dict

        def tampered(self: LocalGroupRecord) -> dict[str, object]:
            document = original(self)
            document["invite_token"] = "should-never-persist"
            return document

        monkeypatch.setattr(LocalGroupRecord, "to_dict", tampered)
        with pytest.raises(GroupStateError):
            LocalGroupRegistry.assert_metadata_only(record)
