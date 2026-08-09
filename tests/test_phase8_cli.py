"""Phase 8 CLI surface tests: `security-status` and extended `--doctor`.

Both commands must render security-relevant metadata without ever exposing
keys, tokens, plaintext, or any secret material, and without modifying user
state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ghostlink.cli.arguments import parse_args
from ghostlink.cli.entrypoint import main
from ghostlink.exceptions.base import ExitCode


def _data_args(home: Path) -> list[str]:
    return ["--data-dir", str(home / "gl-data")]


class TestSecurityStatusCommand:
    def test_parse(self) -> None:
        options = parse_args(["security-status"])
        assert options.command == "security-status"

    def test_renders_no_secrets(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main([*_data_args(isolated_home), "security-status"])
        output = capsys.readouterr().out
        assert code == int(ExitCode.OK), output
        assert "Security Status" in output
        assert "GLFP-" in output  # identity fingerprint (public metadata)
        assert "mesh-v1" in output and "senderkey-v1" in output  # suites
        # never render key material / tokens / plaintext
        assert "private_key" not in output.lower()
        assert "sender_key" not in output.lower()
        assert "gli_" not in output


class TestDoctorExtension:
    def test_doctor_shows_new_rows(
        self, isolated_home: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main([*_data_args(isolated_home), "--doctor"])
        output = capsys.readouterr().out
        assert code in (int(ExitCode.OK), int(ExitCode.ENVIRONMENT))
        assert "Dependencies" in output
        assert "Crypto" in output
        assert "Data Directory" in output

    def test_doctor_is_read_only(self, isolated_home: Path) -> None:
        state = isolated_home / "gl-data"
        main([*_data_args(isolated_home), "--doctor"])
        # Doctor must not create identity/session state files.
        from ghostlink.constants.files import STATE_DIR_NAME

        state_dir = state / STATE_DIR_NAME
        if state_dir.exists():
            assert not (state_dir / "identity.json").exists()
            assert not (state_dir / "groups.json").exists()
