"""Service container and session service behavior."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ghostlink.exceptions.services import ServiceNotRegisteredError
from ghostlink.models.environment import EnvironmentInfo
from ghostlink.models.session import SessionInfo
from ghostlink.services.container import ServiceContainer
from ghostlink.services.session import SessionService
from ghostlink.storage.json_store import JsonFileStorage
from ghostlink.storage.manager import StorageManager


class TestServiceContainer:
    def test_register_and_resolve_by_type(self) -> None:
        container = ServiceContainer()
        marker = {"name": "crypto-future"}
        container.register(dict, marker)
        assert container.get(dict) is marker

    def test_factory_resolves_lazily_once(self) -> None:
        calls: list[int] = []
        container = ServiceContainer()
        container.register_factory("counter", lambda: calls.append(1) or object())
        first = container.get("counter")
        assert container.get("counter") is first
        assert len(calls) == 1

    def test_missing_service_raises_with_registry_summary(self) -> None:
        container = ServiceContainer()
        container.register(str, "present")
        with pytest.raises(ServiceNotRegisteredError) as captured:
            container.get(int)
        assert "str" in (captured.value.hint or "")

    def test_has_and_keys(self) -> None:
        container = ServiceContainer()
        container.register("a", 1)
        container.register_factory("b", lambda: 2)
        assert container.has("a") and container.has("b")
        assert set(container.keys()) == {"a", "b"}
        container.clear()
        assert not container.has("a")


class TestSessionService:
    def _service(self, root: Path, environment_info: EnvironmentInfo) -> SessionService:
        return SessionService(StorageManager(root), environment_info)

    def test_start_records_launch(self, tmp_path: Path, environment_info: EnvironmentInfo) -> None:
        service = self._service(tmp_path / "state", environment_info)
        session = asyncio.run(service.start())
        assert session.launch_count == 1
        store = JsonFileStorage(tmp_path / "state" / "session.json")
        assert store["launch_count"] == 1
        assert store["last_platform"] == "Linux"

    def test_second_launch_increments(
        self, tmp_path: Path, environment_info: EnvironmentInfo
    ) -> None:
        root = tmp_path / "state"
        asyncio.run(self._service(root, environment_info).start())
        session = asyncio.run(self._service(root, environment_info).start())
        assert session.launch_count == 2
        assert session.first_launch_at <= session.started_at

    def test_stop_records_uptime(self, tmp_path: Path, environment_info: EnvironmentInfo) -> None:
        service = self._service(tmp_path / "state", environment_info)
        session = asyncio.run(service.start())
        asyncio.run(service.stop(session))
        store = JsonFileStorage(tmp_path / "state" / "session.json")
        assert "last_seen_at" in store
        assert store["last_session_seconds"] >= 0.0

    def test_session_uptime_is_monotonic(self, environment_info: EnvironmentInfo) -> None:
        from datetime import UTC, datetime, timedelta

        now = datetime.now(UTC)
        session = SessionInfo(
            started_at=now,
            first_launch_at=now,
            launch_count=1,
            pid=1234,
        )
        assert session.uptime(at=now + timedelta(seconds=5)) == 5.0
        assert session.uptime(at=now - timedelta(seconds=5)) == 0.0
