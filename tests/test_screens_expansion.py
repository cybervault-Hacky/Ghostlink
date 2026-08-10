"""Tests for the expanded GhostLink screens and dashboards."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from pathlib import Path

import pytest

from ghostlink.cli.arguments import parse_args
from ghostlink.core.bootstrap import build_application
from ghostlink.ui.console import ConsoleManager
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.base import Screen
from ghostlink.ui.screens.help import HelpScreen
from ghostlink.ui.screens.identity import IdentityScreen
from ghostlink.ui.screens.onboarding import OnboardingWizard
from ghostlink.ui.screens.rooms import RoomManagementScreen
from ghostlink.ui.screens.security import SecurityDashboardScreen
from ghostlink.ui.screens.storage import StorageManagerScreen
from ghostlink.ui.screens.transfers import TransferDashboardScreen


def _script_menu(monkeypatch: pytest.MonkeyPatch, choices: list[str]) -> None:
    script: Iterator[str] = iter(choices)

    def _prompt(
        self: InteractiveMenu,
        entries: tuple[MenuEntry, ...] | list[MenuEntry],
        *,
        default_key: str,
    ) -> str:
        try:
            return next(script)
        except StopIteration:
            return default_key

    monkeypatch.setattr(InteractiveMenu, "prompt", _prompt)


async def _instant_pause(self: Screen, prompt: str = "…") -> None:
    return None


class TestIdentityScreen:
    def test_identity_screen_rendering_and_actions(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Screen, "pause", _instant_pause)
        application = build_application(parse_args([]))
        recording = ConsoleManager(
            application._context.console.theme, record=True, width=100, force_terminal=False
        )
        application._context.console = recording

        _script_menu(monkeypatch, ["nickname", "view_fingerprint", "clear_nickname", "back"])
        monkeypatch.setattr(
            "ghostlink.ui.screens.identity.prompt_text",
            lambda *args, **kwargs: "GhostCoder",
        )

        asyncio.run(IdentityScreen(application._context).show())
        output = recording.export_text()

        assert "IDENTITY" in output
        assert "Protected" in output
        assert "GhostCoder" in output


class TestSecurityDashboardScreen:
    def test_security_dashboard_renders_verified_status(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Screen, "pause", _instant_pause)
        application = build_application(parse_args([]))
        recording = ConsoleManager(
            application._context.console.theme, record=True, width=100, force_terminal=False
        )
        application._context.console = recording

        _script_menu(monkeypatch, ["back"])

        screen = SecurityDashboardScreen(application._context)
        asyncio.run(screen.show())
        output = recording.export_text()

        assert "SECURITY STATUS" in output
        assert "End-to-End Encryption" in output
        assert "ChaCha20-Poly1305" in output

    def test_security_dashboard_audit_details(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Screen, "pause", _instant_pause)
        application = build_application(parse_args([]))
        recording = ConsoleManager(
            application._context.console.theme, record=True, width=100, force_terminal=False
        )
        application._context.console = recording

        screen = SecurityDashboardScreen(application._context)
        asyncio.run(screen._show_audit())
        output = recording.export_text()

        assert "CRYPTOGRAPHIC PROTOCOL SPECIFICATIONS" in output.upper()
        assert "Key Exchange" in output
        assert "ChaCha20-Poly1305" in output


class TestStorageManagerScreen:
    def test_storage_manager_overview_and_cleaning(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Screen, "pause", _instant_pause)
        application = build_application(parse_args([]))
        recording = ConsoleManager(
            application._context.console.theme, record=True, width=100, force_terminal=False
        )
        application._context.console = recording

        # Create dummy temp transfer and log file
        transfers_dir = application._context.data_dir / "transfers"
        transfers_dir.mkdir(parents=True, exist_ok=True)
        (transfers_dir / "chunk_0.tmp").write_bytes(b"x" * 1024)

        _script_menu(monkeypatch, ["clean_temp", "clean_logs", "back"])
        monkeypatch.setattr("ghostlink.ui.screens.storage.confirm", lambda *args, **kwargs: True)

        asyncio.run(StorageManagerScreen(application._context).show())
        output = recording.export_text()

        assert "STORAGE" in output
        assert "State & Key Storage" in output
        assert not (transfers_dir / "chunk_0.tmp").exists()


class TestTransferDashboardScreen:
    def test_transfers_dashboard_rendering(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Screen, "pause", _instant_pause)
        application = build_application(parse_args([]))
        recording = ConsoleManager(
            application._context.console.theme, record=True, width=100, force_terminal=False
        )
        application._context.console = recording

        _script_menu(monkeypatch, ["back"])

        asyncio.run(TransferDashboardScreen(application._context).show())
        output = recording.export_text()

        assert "TRANSFERS" in output
        assert "Chunk Size" in output
        assert "SHA-256" in output


class TestHelpScreen:
    def test_help_screen_renders_shortcuts(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Screen, "pause", _instant_pause)
        application = build_application(parse_args([]))
        recording = ConsoleManager(
            application._context.console.theme, record=True, width=100, force_terminal=False
        )
        application._context.console = recording

        asyncio.run(HelpScreen(application._context).show())
        output = recording.export_text()

        assert "HELP" in output
        assert "Ctrl+C" in output
        assert "/help" in output
        assert "/info" in output


class TestOnboardingWizard:
    def test_onboarding_wizard_full_flow(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        application = build_application(parse_args([]))
        recording = ConsoleManager(
            application._context.console.theme, record=True, width=100, force_terminal=False
        )
        application._context.console = recording

        # Flow: start -> nickname prompt -> theme -> language -> privacy -> finish
        _script_menu(monkeypatch, ["start", "obsidian", "es"])
        monkeypatch.setattr(
            "ghostlink.ui.screens.onboarding.prompt_text",
            lambda *args, **kwargs: "Alice",
        )
        monkeypatch.setattr("ghostlink.ui.screens.onboarding.confirm", lambda *args, **kwargs: True)

        wizard = OnboardingWizard(application._context)
        asyncio.run(wizard.show())

        assert application._context.settings.meta.onboarding_completed is True
        assert application._context.settings.chat.display_name == "Alice"
        assert application._context.settings.ui.theme == "obsidian"
        assert application._context.settings.ui.language == "es"


class TestRoomManagementScreen:
    def test_room_management_overview(
        self, isolated_home: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(Screen, "pause", _instant_pause)
        application = build_application(parse_args([]))
        recording = ConsoleManager(
            application._context.console.theme, record=True, width=100, force_terminal=False
        )
        application._context.console = recording

        _script_menu(monkeypatch, ["back"])

        asyncio.run(RoomManagementScreen(application._context).show())
        output = recording.export_text()

        assert "ROOMS" in output
        assert "Default Lifetime" in output
