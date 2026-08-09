"""Phase 8 crash-consistency tests.

Persistent state must survive interruption at any point: a crash mid-temp-
write must never corrupt the active document, a leftover temporary file must
be ignored, and identity / groups / config stores must recover cleanly after
a restart. Uses the real JSON storage backend with no mocks.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ghostlink.exceptions.storage import StorageCorruptionError
from ghostlink.storage.json_store import JsonFileStorage


class TestInterruptionRecovery:
    def test_leftover_temp_file_ignored(self, tmp_path: Path) -> None:
        target = tmp_path / "store.json"
        target.write_text(json.dumps({"v": 1, "group": "gl-group-AAAA-BBBB-CCCC"}))
        # A crash left a stale temp file behind (partial write, never renamed).
        (tmp_path / "store.json.tmp").write_text('{"v": 1, "grou')
        store = JsonFileStorage(target)
        data = dict(store)
        assert data["group"] == "gl-group-AAAA-BBBB-CCCC"
        # The temp file is not consulted.
        assert data.get("grou") is None

    def test_write_then_restart_is_atomic(self, tmp_path: Path) -> None:
        target = tmp_path / "groups.json"
        store = JsonFileStorage(target)
        store["epoch"] = 3
        store["state"] = "active"
        # Fresh instance over the same file sees the committed state.
        store2 = JsonFileStorage(target)
        assert dict(store2) == {"epoch": 3, "state": "active"}

    def test_corrupt_active_file_surfaces_typed_error(self, tmp_path: Path) -> None:
        target = tmp_path / "groups.json"
        target.write_text('{"epoch": 3, "members": [')  # truncated JSON
        store = JsonFileStorage(target)
        with pytest.raises(StorageCorruptionError):
            dict(store)

    def test_non_object_document_rejected(self, tmp_path: Path) -> None:
        target = tmp_path / "config.json"
        target.write_text("[1, 2, 3]")
        store = JsonFileStorage(target)
        with pytest.raises(StorageCorruptionError):
            dict(store)

    def test_identity_store_survives_restart(self, tmp_path: Path) -> None:
        from ghostlink.storage.manager import StorageManager

        manager = StorageManager(tmp_path)
        from ghostlink.identity.identity import LocalIdentity
        from ghostlink.identity.storage import IdentityStore

        store = IdentityStore(manager)
        identity = LocalIdentity.generate()
        store.save(identity)
        # Simulate a process restart: a fresh store over the same manager.
        store2 = IdentityStore(manager)
        loaded = store2.load()
        assert loaded is not None
        assert loaded.public_key_hex == identity.public_key_hex
        # No private key material persists in plaintext fields we expose here —
        # the identity model stores the keypair, which is the design; the store
        # is 0600-atomic. Assert the file permissions are owner-only.
        from ghostlink.storage.json_store import FILE_PERMISSIONS

        assert (tmp_path / "identity.json").stat().st_mode & 0o777 == FILE_PERMISSIONS
