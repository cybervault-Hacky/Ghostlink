"""End-to-end application runs, driven headlessly through the fallback menu."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from ghostlink.cli.arguments import parse_args
from ghostlink.core.bootstrap import build_application
from ghostlink.core.environment import EnvironmentDetector
from ghostlink.exceptions.base import ExitCode
from ghostlink.storage.json_store import JsonFileStorage
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.base import Screen


async def _instant_pause(self: Screen, prompt: str = "…") -> None:
    """Test double: sub-screens acknowledge immediately."""


def _script_choices(monkeypatch: pytest.MonkeyPatch, choices: list[str] | BaseException) -> None:
    if isinstance(choices, list):
        script: Iterator[str] = iter(choices)

        def _prompt(
            self: InteractiveMenu,
            entries: tuple[MenuEntry, ...],
            *,
            default_key: str,
        ) -> str:
            return next(script, default_key)

        monkeypatch.setattr(InteractiveMenu, "prompt", _prompt)
    else:

        def _interrupt(
            self: InteractiveMenu,
            entries: tuple[MenuEntry, ...],
            *,
            default_key: str,
        ) -> str:
            raise choices

        monkeypatch.setattr(InteractiveMenu, "prompt", _interrupt)


class TestApplicationRun:
    def test_full_navigation_headless(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Screen, "pause", _instant_pause)
        _script_choices(monkeypatch, ["create-room", "settings", "about", "join-room", "exit"])
        answers = iter(["Lounge", "gl-room-ABCD-EFGH-JKLM"])
        monkeypatch.setattr("builtins.input", lambda prompt="": next(answers, "q"))

        options = parse_args([])
        application = build_application(options)
        code = asyncio.run(application.run())

        assert code == int(ExitCode.OK)

        session_file = application._context.data_dir / "state" / "session.json"
        store = JsonFileStorage(session_file)
        assert store["launch_count"] == 1
        assert "last_session_seconds" in store

        rooms_file = application._context.data_dir / "state" / "rooms.json"
        room_store = JsonFileStorage(rooms_file)
        assert len(room_store) == 1
        hosted = next(iter(room_store))
        assert room_store[hosted]["name"] == "Lounge"

        invites_file = application._context.data_dir / "state" / "invites.json"
        assert len(JsonFileStorage(invites_file)) == 1

        log_file = application._context.data_dir / "logs" / "ghostlink.log"
        contents = log_file.read_text(encoding="utf-8")
        assert "GhostLink" in contents

    def test_keyboard_interrupt_exits_130(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Screen, "pause", _instant_pause)
        _script_choices(monkeypatch, KeyboardInterrupt())

        application = build_application(parse_args([]))
        assert asyncio.run(application.run()) == int(ExitCode.INTERRUPTED)

    def test_first_launch_creates_config_and_state(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Screen, "pause", _instant_pause)
        _script_choices(monkeypatch, ["exit"])

        options = parse_args([])
        assert options.config_path is None
        application = build_application(options)
        asyncio.run(application.run())

        assert application._context.config_path.is_file()
        assert application._context.data_dir.is_dir()

    def test_environment_detection_real_system(self) -> None:
        info = EnvironmentDetector.detect()
        assert info.python_version_info[0] == 3
