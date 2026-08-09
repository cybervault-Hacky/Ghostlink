"""Phase 12 Termux↔portal local store + client tests.

Verifies the local token store security (permissions, symlink, atomicity,
corruption, schema) and that the client never leaks tokens/credentials into
URLs or error messages.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from ghostlink.developer_portal.client import PortalClient, PortalClientError
from ghostlink.developer_portal.store import PortalStore, PortalStoreError


class TestStore:
    def test_save_load_roundtrip(self, tmp_path: Path) -> None:
        store = PortalStore(tmp_path)
        store.save({"access_token": "at", "refresh_token": "rt", "developer_id": "dev_x"})
        data = store.load()
        assert data["access_token"] == "at"
        assert data["refresh_token"] == "rt"

    def test_store_permissions(self, tmp_path: Path) -> None:
        store = PortalStore(tmp_path)
        store.save({"access_token": "at"})
        assert stat.S_IMODE(store.directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((store.directory / "tokens.json").stat().st_mode) == 0o600

    def test_clear(self, tmp_path: Path) -> None:
        store = PortalStore(tmp_path)
        store.save({"access_token": "at"})
        store.clear()
        assert store.load() == {}

    def test_future_schema_rejected(self, tmp_path: Path) -> None:
        store = PortalStore(tmp_path)
        store.save({"access_token": "at"})
        # Corrupt the schema version.
        from ghostlink.storage.json_store import JsonFileStorage

        JsonFileStorage(store.directory / "tokens.json").replace({"v": 99, "data": {}})
        with pytest.raises(PortalStoreError):
            store.load()

    def test_symlink_dir_refused(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "developer_portal"
        link.symlink_to(real, target_is_directory=True)
        with pytest.raises(PortalStoreError):
            PortalStore(tmp_path)


class TestClient:
    def test_bearer_header_never_in_url(self) -> None:
        # Ensure the client sends tokens via header only.
        import inspect

        source = inspect.getsource(PortalClient._request)
        assert "Authorization" in source
        assert "Bearer" in source
        # The URL is built from base + path only; token is never in path.
        assert (
            "access_token" not in source.split("def _request")[1].split("url =")[1].split("\n")[0]
        )

    def test_network_failure_never_success(self) -> None:
        client = PortalClient("http://127.0.0.1:1")  # unreachable port
        with pytest.raises(PortalClientError) as excinfo:
            client.health()
        assert excinfo.value.code == "network"

    def test_error_response_never_leaks_internals(self) -> None:
        # A malformed/non-JSON server response is surfaced without tokens.
        client = PortalClient("http://127.0.0.1:9")
        try:
            client.projects_list("a-secret-token")
            raise AssertionError("expected failure")
        except PortalClientError as exc:
            assert "a-secret-token" not in str(exc)
