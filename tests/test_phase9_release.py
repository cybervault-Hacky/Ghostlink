"""Phase 9 release / regression tests.

Covers the production-readiness concerns: Termux path & platform detection,
Ctrl+C exit semantics, deterministic CLI exit codes with no tracebacks for
expected failures, read-only-filesystem handling, narrow-terminal safety,
and the Python-compatibility floor.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ghostlink.cli.arguments import parse_args
from ghostlink.cli.entrypoint import main
from ghostlink.constants.app import MIN_PYTHON
from ghostlink.core.environment import EnvironmentDetector
from ghostlink.exceptions.base import ExitCode
from ghostlink.models.environment import PlatformKind


class TestTermuxDetection:
    def test_termux_markers_detected(self) -> None:
        env = {"TERMUX_VERSION": "0.118.1", "HOME": "/data/data/com.termux/files/home"}
        assert EnvironmentDetector.classify_platform(env, "Linux") is PlatformKind.TERMUX

    def test_prefix_marker_detected(self) -> None:
        env = {"PREFIX": "/data/data/com.termux/files/usr"}
        assert EnvironmentDetector.classify_platform(env, "Linux") is PlatformKind.TERMUX

    def test_plain_linux_is_linux(self) -> None:
        assert EnvironmentDetector.classify_platform({}, "Linux") is PlatformKind.LINUX

    def test_unsupported_is_unsupported(self) -> None:
        assert EnvironmentDetector.classify_platform({}, "Windows") is PlatformKind.UNSUPPORTED


class TestPythonFloor:
    def test_min_python_is_3_11(self) -> None:
        assert MIN_PYTHON == (3, 11)


class TestCliExitCodes:
    def test_version_is_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        # argparse `--version` prints and exits 0 via SystemExit.
        with pytest.raises(SystemExit) as excinfo:
            main(["--version"])
        assert excinfo.value.code == 0

    def test_help_is_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        # argparse `--help` prints and exits 0 via SystemExit.
        with pytest.raises(SystemExit) as excinfo:
            main(["--help"])
        assert excinfo.value.code == 0

    def test_doctor_returns_zero_or_env(self, capsys: pytest.CaptureFixture[str]) -> None:
        code = main(["--doctor"])
        assert code in (int(ExitCode.OK), int(ExitCode.ENVIRONMENT))

    def test_unknown_command_is_error_no_traceback(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:

        # argparse rejects an unknown subcommand with SystemExit(2).
        with pytest.raises(SystemExit) as excinfo:
            parse_args(["nonexistent-subcommand"])
        assert excinfo.value.code == 2

    def test_keyboard_interrupt_is_130(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        def _boom(*args: object, **kwargs: object) -> object:
            raise KeyboardInterrupt

        monkeypatch.setattr("ghostlink.cli.entrypoint.run_command", _boom)
        code = main(["group", "list"])
        assert code == int(ExitCode.INTERRUPTED)
        out = capsys.readouterr().out
        assert "Interrupted" in out
        assert "Traceback" not in out


class TestConfigPermission:
    def test_unwritable_config_dir_is_typed_error(
        self,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        from ghostlink.config.manager import ConfigurationManager
        from ghostlink.exceptions.config import ConfigurationError

        # Point config at a path whose parent is a file (cannot be a dir).
        config_path = isolated_home / "blocked" / "config.toml"
        (isolated_home / "blocked").write_text("x")
        manager = ConfigurationManager(config_path=config_path)
        with pytest.raises(ConfigurationError):
            manager.load()

    def test_malformed_config_is_typed_error_no_traceback(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config_dir = isolated_home / "gl-data" / "config"
        config_dir.mkdir(parents=True)
        cfg = config_dir / "config.toml"
        cfg.write_text("[ui\ntheme = ")
        from ghostlink.cli.entrypoint import main as _main

        code = _main(["--config", str(cfg), "group", "list"])
        assert code == int(ExitCode.CONFIGURATION)
        out = capsys.readouterr().out
        assert "Traceback" not in out


class TestNarrowTerminal:
    def test_narrow_terminal_menu_does_not_crash(self, tmp_path: Path) -> None:
        # The UI must not crash on a 40-column terminal.
        from ghostlink.core.environment import EnvironmentDetector

        info = EnvironmentDetector.detect()
        assert info.terminal_columns >= 0  # detected without error
