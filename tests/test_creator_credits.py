"""Creator-credit rendering and placement tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from ghostlink.cli.arguments import parse_args
from ghostlink.core.bootstrap import build_application
from ghostlink.ui.components.credits import (
    CREATOR_INSTAGRAM,
    CREATOR_NAME,
    CREATOR_YOUTUBE,
    creator_credits,
)
from ghostlink.ui.console import ConsoleManager
from ghostlink.ui.menu import InteractiveMenu
from ghostlink.ui.screens.base import Screen
from ghostlink.ui.screens.home import HomeScreen
from ghostlink.ui.themes import ThemeEngine

_RECORD_WIDTH = 100
EXPECTED_STRINGS = (CREATOR_NAME, CREATOR_YOUTUBE, CREATOR_INSTAGRAM)


def _render(renderable, *, width: int, no_color: bool = False) -> str:
    console = ConsoleManager(
        ThemeEngine().get("phantom"),
        record=True,
        width=width,
        no_color=no_color,
        force_terminal=False,
    )
    console.print(renderable)
    return console.export_text()


def _recording_application(monkeypatch: pytest.MonkeyPatch, *argv: str):
    """Build an application whose console records all output for assertions."""

    application = build_application(parse_args(list(argv)))
    recording = ConsoleManager(
        application._context.console.theme,
        record=True,
        width=_RECORD_WIDTH,
        no_color=application._context.console.no_color,
        force_terminal=False,
    )
    application._context.console = recording
    monkeypatch.setattr(
        InteractiveMenu,
        "prompt",
        lambda self, entries, *, default_key: "exit",
    )
    return application


class TestCreatorCreditsContent:
    def test_contains_all_credit_strings(self) -> None:
        text = _render(creator_credits(), width=100)
        for expected in EXPECTED_STRINGS:
            assert expected in text

    def test_credit_strings_exact(self) -> None:
        assert CREATOR_NAME == "Sarthak Bharambe"
        assert CREATOR_YOUTUBE == "Cyber Vault"
        assert CREATOR_INSTAGRAM == "@cyber_vault123"

    def test_renders_without_color(self) -> None:
        text = _render(creator_credits(), width=100, no_color=True)
        for expected in EXPECTED_STRINGS:
            assert expected in text

    @pytest.mark.parametrize("width", [32, 40, 60, 80, 120])
    def test_renders_in_narrow_terminals_without_truncation(self, width: int) -> None:
        text = _render(creator_credits(), width=width)
        # At widths >= 32 each credit line ("Instagram — @cyber_vault123" is
        # the longest at 30 chars) fits on a single line without truncation.
        for expected in EXPECTED_STRINGS:
            assert expected in text
        lines = [line for line in text.splitlines() if line.strip()]
        assert any(CREATOR_NAME in line for line in lines)
        assert any(CREATOR_YOUTUBE in line for line in lines)
        assert any(CREATOR_INSTAGRAM in line for line in lines)

    def test_renders_in_very_narrow_terminal_without_losing_content(self) -> None:
        # 20 cols forces wrapping — verify no characters are dropped. Normalise
        # whitespace and the em-dash separator used in the labels.
        raw = "".join(_render(creator_credits(), width=20).split())
        compact = raw.replace(" ", "").replace("—", "").replace("-", "")
        assert "CreatedbySarthakBharambe" in compact
        assert "YouTubeCyberVault" in compact
        assert "Instagram@cyber_vault123" in compact


class TestCreditsOnHomeScreen:
    def test_credits_appear_on_starting_screen(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = _recording_application(monkeypatch)
        asyncio.run(HomeScreen(application._context).show())

        output = application._context.console.export_text()
        for expected in EXPECTED_STRINGS:
            assert expected in output

    def test_credits_appear_with_no_color(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = _recording_application(monkeypatch, "--no-color")
        assert application._context.console.no_color
        asyncio.run(HomeScreen(application._context).show())

        output = application._context.console.export_text()
        for expected in EXPECTED_STRINGS:
            assert expected in output

    def test_menu_navigation_unchanged(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = build_application(parse_args([]))
        application._context.console = ConsoleManager(
            application._context.console.theme,
            record=True,
            width=_RECORD_WIDTH,
            force_terminal=False,
        )
        choices = iter(["settings", "exit"])
        monkeypatch.setattr(
            InteractiveMenu,
            "prompt",
            lambda self, entries, *, default_key: next(choices, default_key),
        )

        async def _no_pause(self: Screen, prompt: str = "") -> None:
            return None

        monkeypatch.setattr(Screen, "pause", _no_pause)

        asyncio.run(HomeScreen(application._context).show())

        output = application._context.console.export_text()
        assert CREATOR_NAME in output
        assert "Settings" in output


class TestCreditsOnCompletion:
    def test_credits_in_farewell_output(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = _recording_application(monkeypatch)
        code = asyncio.run(application.run())

        output = application._context.console.export_text()
        assert code == 0
        for expected in EXPECTED_STRINGS:
            assert expected in output
