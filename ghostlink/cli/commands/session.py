"""``ghostlink session`` — session dashboard: app history, rooms, invites."""

from __future__ import annotations

from datetime import UTC, datetime

from ghostlink.cli.arguments import CLIOptions
from ghostlink.cli.commands.base import CommandRuntime, build_runtime
from ghostlink.exceptions.base import ExitCode
from ghostlink.models.session import SessionInfo
from ghostlink.services.session import SessionService
from ghostlink.storage.json_store import JsonFileStorage
from ghostlink.ui.dashboards import render_session_overview


def run_session(options: CLIOptions) -> int:
    """Render the session overview from durable state. Read-only."""

    runtime = build_runtime(options)

    session_info = _load_last_session(runtime)
    rooms = runtime.rooms.list_rooms()
    invites = runtime.rooms.list_invites()

    render_session_overview(runtime.console, session=session_info, rooms=rooms, invites=invites)
    runtime.console.newline()
    return int(ExitCode.OK)


def _load_last_session(runtime: CommandRuntime) -> SessionInfo | None:
    """Rebuild the last recorded app session, if one exists."""

    store = JsonFileStorage(runtime.config.state_dir / f"{SessionService.NAMESPACE}.json")
    launch_count = store.get("launch_count", 0)
    if not isinstance(launch_count, int) or launch_count < 1:
        return None
    return SessionInfo(
        started_at=_parse_iso(store.get("last_launch_at")),
        first_launch_at=_parse_iso(store.get("first_launch_at")),
        launch_count=launch_count,
        pid=_parse_pid(store.get("last_pid")),
    )


def _parse_iso(raw: object) -> datetime:
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.fromtimestamp(0, UTC)


def _parse_pid(raw: object) -> int:
    return raw if isinstance(raw, int) and raw > 0 else 0
