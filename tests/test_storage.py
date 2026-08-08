"""Storage backends: mapping contract, durability, atomicity, namespaces."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ghostlink.exceptions.storage import (
    StorageCorruptionError,
    StorageError,
    StorageWriteError,
)
from ghostlink.storage.json_store import JsonFileStorage
from ghostlink.storage.manager import StorageManager
from ghostlink.storage.memory import MemoryStorage


class TestJsonFileStorage:
    def test_roundtrip(self, tmp_path: Path) -> None:
        store = JsonFileStorage(tmp_path / "data.json")
        store["greeting"] = "hello"
        store["count"] = 3
        store["flags"] = [True, False]
        assert store["greeting"] == "hello"
        assert store["count"] == 3
        assert store["flags"] == [True, False]
        assert len(store) == 3
        assert set(store) == {"greeting", "count", "flags"}

    def test_persists_across_instances(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        JsonFileStorage(path)["key"] = "value"
        assert JsonFileStorage(path)["key"] == "value"

    def test_missing_key_raises_keyerror(self, tmp_path: Path) -> None:
        with pytest.raises(KeyError):
            _ = JsonFileStorage(tmp_path / "data.json")["absent"]

    def test_delete_and_contains(self, tmp_path: Path) -> None:
        store = JsonFileStorage(tmp_path / "data.json")
        store["key"] = 1
        assert "key" in store
        del store["key"]
        assert "key" not in store
        with pytest.raises(KeyError):
            del store["key"]

    def test_atomic_write_leaves_no_temp_file(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        JsonFileStorage(path)["key"] = "value"
        assert not (tmp_path / "data.json.tmp").exists()

    def test_file_permissions_are_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        JsonFileStorage(path)["key"] = "value"
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600

    def test_corrupt_json_raises_with_hint(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        path.write_text("{not json", encoding="utf-8")
        with pytest.raises(StorageCorruptionError) as captured:
            _ = JsonFileStorage(path)["key"]
        assert captured.value.hint is not None

    def test_non_object_document_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "data.json"
        path.write_text('["a", "list"]', encoding="utf-8")
        with pytest.raises(StorageCorruptionError, match="JSON object"):
            _ = JsonFileStorage(path)["a"]

    def test_unserializable_value_rejected(self, tmp_path: Path) -> None:
        store = JsonFileStorage(tmp_path / "data.json")
        with pytest.raises(StorageWriteError) as captured:
            store["bad"] = object()
        assert "JSON-serializable" in str(captured.value)
        assert not (tmp_path / "data.json.tmp").exists()


class TestMemoryStorage:
    def test_roundtrip(self) -> None:
        store = MemoryStorage()
        store["a"] = 1
        assert store["a"] == 1
        assert len(store) == 1

    def test_reads_are_isolated_copies(self) -> None:
        store = MemoryStorage()
        store["nested"] = {"items": [1]}
        snapshot = store["nested"]
        snapshot["items"].append(999)
        assert store["nested"] == {"items": [1]}

    def test_initial_data(self) -> None:
        store = MemoryStorage({"seed": True})
        assert store["seed"] is True


class TestStorageManager:
    def test_namespaced_documents(self, tmp_path: Path) -> None:
        manager = StorageManager(tmp_path / "state")
        session = manager.store("session")
        identity = manager.store("identity")
        session["a"] = 1
        identity["b"] = 2
        assert (tmp_path / "state" / "session.json").is_file()
        assert (tmp_path / "state" / "identity.json").is_file()
        assert session["a"] == 1 and identity["b"] == 2
        assert manager.namespaces() == ("identity", "session")

    def test_store_is_cached(self, tmp_path: Path) -> None:
        manager = StorageManager(tmp_path / "state")
        assert manager.store("session") is manager.store("session")

    @pytest.mark.parametrize("bad", ["Bad Name", "UPPER", "with space", "", "-lead"])
    def test_invalid_namespace_rejected(self, tmp_path: Path, bad: str) -> None:
        manager = StorageManager(tmp_path / "state")
        with pytest.raises(StorageError, match="Invalid storage namespace"):
            manager.store(bad)

    def test_root_is_created(self, tmp_path: Path) -> None:
        root = tmp_path / "deep" / "state"
        StorageManager(root)
        assert root.is_dir()
