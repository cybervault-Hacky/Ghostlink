"""Network sessions: lifecycle, expiration, and the sweeping registry."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from ghostlink.transport.session import (
    Session,
    SessionRegistry,
    SessionStatus,
    generate_session_id,
)
from tests.conftest import run

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)


class TestSessionIds:
    def test_format(self) -> None:
        session_id = generate_session_id()
        assert session_id.startswith("sess_")
        assert len(session_id) == 21
        int(session_id.removeprefix("sess_"), 16)  # hex payload parses

    def test_unique(self) -> None:
        assert generate_session_id() != generate_session_id()


class TestSessionLifecycle:
    def test_create_defaults(self) -> None:
        session = Session.create(metadata={"remote": "ws://x"}, ttl_seconds=60, now=NOW)
        assert session.status is SessionStatus.ACTIVE
        assert session.created_at == NOW == session.last_activity_at
        assert session.expires_at == NOW + timedelta(seconds=60)

    def test_no_ttl_means_no_expiry(self) -> None:
        session = Session.create(now=NOW)
        assert session.expires_at is None
        assert not session.is_expired(NOW + timedelta(days=365))

    def test_touch_refreshes_activity_and_expiry(self) -> None:
        session = Session.create(ttl_seconds=60, now=NOW)
        session.last_activity_at = NOW  # roll the clock deterministically
        session.mark_idle()
        assert session.status is SessionStatus.IDLE
        session.touch(activity="PING")
        assert session.status is SessionStatus.ACTIVE
        assert session.metadata["last_activity"] == "PING"
        assert session.last_activity_at > NOW

    def test_mark_idle_only_from_active(self) -> None:
        session = Session.create(now=NOW)
        session.close()
        session.mark_idle()
        assert session.status is SessionStatus.CLOSED

    def test_is_expired_uses_idle_clock(self) -> None:
        session = Session.create(ttl_seconds=30, now=NOW)
        assert not session.is_expired(NOW + timedelta(seconds=29))
        assert session.is_expired(NOW + timedelta(seconds=30))

    def test_close_marks_closed(self) -> None:
        session = Session.create(now=NOW)
        session.close()
        assert session.status is SessionStatus.CLOSED

    def test_age_and_idle_are_monotonic_floors(self) -> None:
        session = Session.create(now=NOW)
        earlier = NOW - timedelta(seconds=5)
        assert session.age_seconds(earlier) == 0.0
        assert session.idle_seconds(earlier) == 0.0
        later = NOW + timedelta(seconds=10)
        assert session.age_seconds(later) == 10.0
        assert session.idle_seconds(later) == 10.0


class TestRegistry:
    def test_register_get_remove(self) -> None:
        registry = SessionRegistry()
        session = registry.register(Session.create())
        assert registry.get(session.session_id) is session
        assert registry.count() == 1
        removed = registry.remove(session.session_id)
        assert removed is session
        assert removed.status is SessionStatus.CLOSED
        assert registry.get(session.session_id) is None
        assert registry.remove(session.session_id) is None

    def test_sessions_snapshot(self) -> None:
        registry = SessionRegistry()
        sessions = [registry.register(Session.create()) for _ in range(3)]
        assert {entry.session_id for entry in registry.sessions()} == {
            entry.session_id for entry in sessions
        }

    def test_sweep_expired_removes_only_overdue(self) -> None:
        registry = SessionRegistry()
        overdue = registry.register(Session.create(ttl_seconds=5, now=NOW))
        fresh = registry.register(Session.create(ttl_seconds=5000, now=NOW))
        swept = registry.sweep_expired(NOW + timedelta(seconds=10))
        assert [entry.session_id for entry in swept] == [overdue.session_id]
        assert overdue.status is SessionStatus.EXPIRED
        assert registry.count() == 1
        assert registry.get(fresh.session_id) is fresh
        assert registry.total_expired == 1

    def test_sweeper_task_expires_sessions_in_background(self) -> None:
        async def scenario() -> tuple[SessionRegistry, Session]:
            registry = SessionRegistry(sweep_interval_seconds=0.02)
            session = registry.register(Session.create(ttl_seconds=0.05))
            registry.start_sweeper()
            registry.start_sweeper()  # idempotent
            deadline = asyncio.get_running_loop().time() + 2.0
            while registry.count() and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            await registry.stop_sweeper()
            return registry, session

        registry, session = run(scenario())
        assert registry.count() == 0
        assert session.status is SessionStatus.EXPIRED
        assert registry.total_expired == 1

    def test_stop_sweeper_without_start_is_safe(self) -> None:
        run(SessionRegistry().stop_sweeper())
