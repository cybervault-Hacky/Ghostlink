"""Exception framework: exit codes, hints, and rendered output."""

from __future__ import annotations

import pytest

from ghostlink.exceptions import (
    ConfigParseError,
    ConfigValidationError,
    ExitCode,
    GhostLinkError,
    StorageCorruptionError,
    StorageError,
    ThemeNotFoundError,
    UnsupportedPythonVersionError,
    render_exception,
)
from ghostlink.exceptions.handler import exit_code_for
from ghostlink.ui.console import ConsoleManager


class TestExitCodes:
    @pytest.mark.parametrize(
        ("error", "expected"),
        [
            (GhostLinkError("base"), ExitCode.GENERAL),
            (ConfigParseError("parse"), ExitCode.CONFIGURATION),
            (ConfigValidationError("bad"), ExitCode.CONFIGURATION),
            (StorageError("io"), ExitCode.STORAGE),
            (StorageCorruptionError("corrupt"), ExitCode.STORAGE),
            (ThemeNotFoundError("theme"), ExitCode.UI),
            (UnsupportedPythonVersionError("old"), ExitCode.ENVIRONMENT),
            (KeyboardInterrupt(), ExitCode.INTERRUPTED),
            (RuntimeError("boom"), ExitCode.GENERAL),
        ],
    )
    def test_mapping(self, error: BaseException, expected: ExitCode) -> None:
        assert exit_code_for(error) is expected


class TestRendering:
    def test_panel_contains_message_hint_and_code(self, console_manager: ConsoleManager) -> None:
        error = ConfigValidationError("bad language value", hint="Use 'en'.")
        code = render_exception(console_manager.console, error)
        output = console_manager.export_text()
        assert code is ExitCode.CONFIGURATION
        assert "bad language value" in output
        assert "Use 'en'." in output
        assert "exit code 2" in output
        assert "Configuration validation error" in output

    def test_unexpected_error_shows_type(self, console_manager: ConsoleManager) -> None:
        code = render_exception(console_manager.console, ValueError("inner"))
        output = console_manager.export_text()
        assert code is ExitCode.GENERAL
        assert "ValueError: inner" in output
        assert "Unexpected error" in output

    def test_cause_chain_is_shown(self, console_manager: ConsoleManager) -> None:
        try:
            try:
                raise OSError("disk gone")
            except OSError as exc:
                raise StorageError("write failed") from exc
        except StorageError as error:
            render_exception(console_manager.console, error)
        assert "Caused by" in console_manager.export_text()


class TestStrAndHint:
    def test_str_is_message(self) -> None:
        assert str(GhostLinkError("plain message")) == "plain message"

    def test_hint_defaults_to_none(self) -> None:
        assert GhostLinkError("x").hint is None
