"""Session lifecycle service.

Tracks launch statistics in the durable ``session`` store and owns the
async start/stop hooks. Phase 1 bodies are synchronous work wrapped in
coroutines; the async signature is the contract Phase 2 needs to await
transport and cryptographic teardown on shutdown.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime

from ghostlink.core.logging import get_logger
from ghostlink.models.environment import EnvironmentInfo
from ghostlink.models.session import SessionInfo
from ghostlink.storage.manager import StorageManager
from ghostlink.utils.text import format_duration


class SessionService:
    """Records session state and owns application lifecycle hooks."""

    NAMESPACE = "session"

    def __init__(self, storage: StorageManager, environment: EnvironmentInfo) -> None:
        self._storage = storage
        self._environment = environment
        self._logger = get_logger("services.session")

    async def start(self) -> SessionInfo:
        """Open a session, persist launch statistics, and return its info."""

        store = self._storage.store(self.NAMESPACE)
        now = datetime.now(UTC)
        session_pid = os.getpid()

        launch_count = int(store.get("launch_count", 0)) + 1
        first_launch_raw = store.get("first_launch_at")
        first_launch_at = (
            datetime.fromisoformat(first_launch_raw) if isinstance(first_launch_raw, str) else now
        )

        store["launch_count"] = launch_count
        store["first_launch_at"] = first_launch_at.isoformat()
        store["last_launch_at"] = now.isoformat()
        store["last_platform"] = self._environment.platform_label
        store["last_pid"] = session_pid

        session = SessionInfo(
            started_at=now,
            first_launch_at=first_launch_at,
            launch_count=launch_count,
            pid=os.getpid(),
        )
        self._logger.debug("Session opened — pid=%d launch=%d", session.pid, session.launch_count)
        return session

    async def stop(self, session: SessionInfo) -> None:
        """Close a session, recording the wall-clock time it lasted."""

        uptime = session.uptime()
        store = self._storage.store(self.NAMESPACE)
        store["last_seen_at"] = datetime.now(UTC).isoformat()
        store["last_session_seconds"] = round(uptime, 3)
        self._logger.debug("Session closed — uptime=%s", format_duration(uptime))
