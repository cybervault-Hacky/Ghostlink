"""CLI argument parsing, --version, and entrypoint exit codes."""

from __future__ import annotations

from pathlib import Path

import pytest

from ghostlink.cli.arguments import parse_args
from ghostlink.cli.entrypoint import main
from ghostlink.exceptions.base import ExitCode


class TestArgumentParsing:
    def test_defaults(self) -> None:
        options = parse_args([])
        assert options.config_path is None
        assert options.theme is None
        assert options.debug is None
        assert options.no_color is False
        assert options.doctor is False

    def test_full_flags(self, tmp_path: Path) -> None:
        config = tmp_path / "config.toml"
        config.write_text('[ui]\ntheme = "ember"\n', encoding="utf-8")
        options = parse_args(
            [
                "--config",
                str(config),
                "--data-dir",
                str(tmp_path / "data"),
                "--theme",
                "ember",
                "--debug",
                "--no-color",
            ]
        )
        assert options.config_path == config
        assert options.data_dir == tmp_path / "data"
        assert options.theme == "ember"
        assert options.debug is True
        assert options.no_color is True

    def test_invalid_theme_rejected(self) -> None:
        with pytest.raises(SystemExit) as captured:
            parse_args(["--theme", "nocturne"])
        assert captured.value.code == 2

    def test_missing_config_file_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit) as captured:
            parse_args(["--config", str(tmp_path / "absent.toml")])
        assert captured.value.code == 2

    def test_version_flag_prints_and_exits(self, capsys: pytest.CaptureFixture[str]) -> None:
        import ghostlink

        with pytest.raises(SystemExit) as captured:
            parse_args(["--version"])
        assert captured.value.code == 0
        assert f"GhostLink {ghostlink.__version__}" in capsys.readouterr().out


class TestEntrypoint:
    def test_doctor_runs_and_returns_status(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main(["--doctor"])
        output = capsys.readouterr().out
        assert "GhostLink Doctor" in output
        assert code in (int(ExitCode.OK), int(ExitCode.ENVIRONMENT))

    def test_config_error_maps_to_exit_2(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        config = isolated_home / ".config" / "ghostlink" / "config.toml"
        config.parent.mkdir(parents=True)
        config.write_text('[ui]\nlanguage = "xx"\n', encoding="utf-8")
        code = main([])
        output = capsys.readouterr().out
        assert code == int(ExitCode.CONFIGURATION)
        assert "Hint" in output
