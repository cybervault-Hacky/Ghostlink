"""Network session objects with expiration and automatic cleanup.

A session describes one authenticated-by-handshake relationship with a peer:
who, since when, how active, and when it expires. Sessions are used on both
sides — the relay client holds one for its own connection; the reference
relay tracks one per attached client in a sweeping registry.
"""

from __future__ import annotations

import asyncio
import secrets
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum

from ghostlink.core.logging import get_logger


class SessionStatus(str, Enum):
    """Lifecycle states of a network session."""

    ACTIVE = "active"
    IDLE = "idle"
    EXPIRED = "expired"
    CLOSED = "closed"


def generate_session_id() -> str:
    """Create a random session identifier: ``sess_`` + 16 hex chars."""

    return f"sess_{secrets.token_hex(8)}"


@dataclass(slots=True)
class Session:
    """A single network session with expiration support."""

    session_id: str
    created_at: datetime
    last_activity_at: datetime
    status: SessionStatus
    metadata: dict[str, str] = field(default_factory=dict)
    ttl_seconds: float | None = None

    @classmethod
    def create(
        cls,
        *,
        metadata: dict[str, str] | None = None,
        ttl_seconds: float | None = None,
        session_id: str | None = None,
        now: datetime | None = None,
    ) -> Session:
        moment = now if now is not None else datetime.now(UTC)
        return cls(
            session_id=session_id or generate_session_id(),
            created_at=moment,
            last_activity_at=moment,
            status=SessionStatus.ACTIVE,
            metadata=dict(metadata) if metadata else {},
            ttl_seconds=ttl_seconds,
        )

    # ------------------------------------------------------------ properties

    @property
    def expires_at(self) -> datetime | None:
        if self.ttl_seconds is None:
            return None
        return self.last_activity_at + timedelta(seconds=self.ttl_seconds)

    def age_seconds(self, now: datetime | None = None) -> float:
        moment = now if now is not None else datetime.now(UTC)
        return max(0.0, (moment - self.created_at).total_seconds())

    def idle_seconds(self, now: datetime | None = None) -> float:
        moment = now if now is not None else datetime.now(UTC)
        return max(0.0, (moment - self.last_activity_at).total_seconds())

    # ------------------------------------------------------------- transitions

    def touch(self, *, activity: str | None = None) -> None:
        """Record activity: refresh the activity clock and reactivate."""

        self.last_activity_at = datetime.now(UTC)
        if activity:
            self.metadata["last_activity"] = activity
        if self.status is SessionStatus.IDLE:
            self.status = SessionStatus.ACTIVE

    def mark_idle(self) -> None:
        if self.status is SessionStatus.ACTIVE:
            self.status = SessionStatus.IDLE

    def is_expired(self, now: datetime | None = None) -> bool:
        if self.status is SessionStatus.EXPIRED:
            return True
        if self.ttl_seconds is None:
            return False
        return self.idle_seconds(now) >= self.ttl_seconds

    def close(self) -> None:
        self.status = SessionStatus.CLOSED


class SessionRegistry:
    """Tracks live sessions and sweeps expired ones automatically."""

    def __init__(
        self,
        *,
        sweep_interval_seconds: float = 30.0,
        logger_name: str = "transport.sessions",
    ) -> None:
        self._sessions: dict[str, Session] = {}
        self._sweep_interval = sweep_interval_seconds
        self._sweeper_task: asyncio.Task[None] | None = None
        self._logger = get_logger(logger_name)
        self.total_expired = 0

    # --------------------------------------------------------------- registry

    def register(self, session: Session) -> Session:
        self._sessions[session.session_id] = session
        self._logger.debug(
            "Session registered — %s (%s)",
            session.session_id,
            session.metadata.get("remote", "unknown peer"),
        )
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def remove(self, session_id: str) -> Session | None:
        session = self._sessions.pop(session_id, None)
        if session is not None:
            session.close()
            self._logger.debug("Session removed — %s", session_id)
        return session

    def count(self) -> int:
        return len(self._sessions)

    def sessions(self) -> tuple[Session, ...]:
        return tuple(self._sessions.values())

    # ---------------------------------------------------------------- sweeper

    def sweep_expired(self, now: datetime | None = None) -> tuple[Session, ...]:
        """Expire every overdue session; returns what was swept."""

        expired = tuple(session for session in self._sessions.values() if session.is_expired(now))
        for session in expired:
            session.status = SessionStatus.EXPIRED
            del self._sessions[session.session_id]
        if expired:
            self.total_expired += len(expired)
            self._logger.info("Swept %d expired session(s).", len(expired))
        return expired

    def start_sweeper(self) -> None:
        """Launch the periodic sweep task. Idempotent."""

        if self._sweeper_task is not None and not self._sweeper_task.done():
            return
        self._sweeper_task = asyncio.create_task(self._sweep_loop())

    async def stop_sweeper(self) -> None:
        """Cancel the sweeper task and wait for it to finish."""

        task = self._sweeper_task
        self._sweeper_task = None
        if task is None:
            return
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    async def _sweep_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._sweep_interval)
                self.sweep_expired()
        except asyncio.CancelledError:
            raise
