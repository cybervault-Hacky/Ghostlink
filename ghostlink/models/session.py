"""Session lifecycle model."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime


@dataclass(frozen=True, slots=True)
class SessionInfo:
    """Describes a single application session, from launch to shutdown."""

    started_at: datetime
    first_launch_at: datetime
    launch_count: int
    pid: int

    def uptime(self, at: datetime | None = None) -> float:
        """Return the session uptime in seconds at ``at`` (default: now)."""

        moment = at if at is not None else datetime.now(UTC)
        return max(0.0, (moment - self.started_at).total_seconds())
