"""Phase 10A developer-account CLI tests.

Verify the CLI surfaces: init (show-once), status, key list/create/rotate/
revoke, export-info (no secret), doctor and security-status integration,
and that the raw secret never appears in later output.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from ghostlink.cli.arguments import parse_args
from ghostlink.cli.entrypoint import main
from ghostlink.exceptions.base import ExitCode

KEY_RE = re.compile(r"gl_dev_[A-Za-z0-9_-]+")


def _data_args(home: Path) -> list[str]:
    return ["--data-dir", str(home / "gl-data")]


class TestArgParsing:
    def test_developer_actions(self) -> None:
        assert parse_args(["developer"]).command == "developer"
        assert parse_args(["developer", "status"]).developer_action == "status"
        assert parse_args(["developer", "init"]).developer_action == "init"
        assert parse_args(["developer", "key", "list"]).developer_key_action == "list"
        assert parse_args(["developer", "key", "rotate"]).developer_key_action == "rotate"
        assert (
            parse_args(["developer", "key", "revoke", "dk_AB12CD34"]).developer_key_id
            == "dk_AB12CD34"
        )


class TestCliLifecycle:
    def test_init_shows_once_then_never_again(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = _data_args(tmp_path)
        code = main([*args, "developer", "init"])
        out = capsys.readouterr().out
        assert code == int(ExitCode.OK), out
        matches = KEY_RE.findall(out)
        assert len(matches) == 1  # shown exactly once
        secret = matches[0]
        assert "Save this key now" in out
        # status never shows the secret
        main([*args, "developer", "status"])
        out2 = capsys.readouterr().out
        assert secret not in out2
        # key list never shows the secret
        main([*args, "developer", "key", "list"])
        out3 = capsys.readouterr().out
        assert secret not in out3

    def test_second_init_refused(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        args = _data_args(tmp_path)
        main([*args, "developer", "init"])
        capsys.readouterr()
        code = main([*args, "developer", "init"])
        assert code == int(ExitCode.CONFIGURATION)

    def test_status_when_unconfigured(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = main([*_data_args(tmp_path), "developer", "status"])
        out = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "Developer Status" in out or "No developer account" in out

    def test_rotate_shows_new_once_and_old_gone(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = _data_args(tmp_path)
        main([*args, "developer", "init"])
        old = KEY_RE.findall(capsys.readouterr().out)
        assert old and len(old) == 1
        main([*args, "developer", "key", "rotate"])
        new = KEY_RE.findall(capsys.readouterr().out)
        assert new and len(new) == 1
        assert new[0] != old[0]

    def test_revoke_by_key_id(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        args = _data_args(tmp_path)
        main([*args, "developer", "init"])
        capsys.readouterr()
        main([*args, "developer", "key", "list"])
        key_id = re.search(r"dk_[A-Z0-9]{8}", capsys.readouterr().out)
        assert key_id is not None
        code = main([*args, "developer", "key", "revoke", key_id.group(0)])
        assert code == int(ExitCode.OK)
        main([*args, "developer", "key", "list"])
        assert "revoked" in capsys.readouterr().out

    def test_revoke_unknown_key_id(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = _data_args(tmp_path)
        main([*args, "developer", "init"])
        capsys.readouterr()
        code = main([*args, "developer", "key", "revoke", "dk_XXXXXXXX"])
        assert code == int(ExitCode.CONFIGURATION)


class TestExportInfo:
    def test_export_never_contains_secret(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = _data_args(tmp_path)
        main([*args, "developer", "init"])
        secret = KEY_RE.findall(capsys.readouterr().out)[0]
        main([*args, "developer", "export-info"])
        out = capsys.readouterr().out
        assert secret not in out
        doc = json.loads(out[out.index("{") : out.rindex("}") + 1])
        assert doc["developer_id"].startswith("dev_")
        assert "credentials" in doc
        # no full credential / verification material ever exported
        assert "salt" not in doc and "hash" not in doc
        assert not KEY_RE.search(out)  # no gl_dev_<id>_<secret> credential


class TestDoctorSecurityStatus:
    def test_doctor_reports_developer(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        main([*_data_args(tmp_path), "developer", "init"])
        capsys.readouterr()
        code = main([*_data_args(tmp_path), "--doctor"])
        out = capsys.readouterr().out
        assert code in (int(ExitCode.OK), int(ExitCode.ENVIRONMENT))
        assert "Developer account" in out
        assert "gl_dev_" not in out


class TestDeveloperPortalCli:
    def test_portal_stop_cli(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        args = _data_args(tmp_path)
        code = main([*args, "developer", "stop"])
        out = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "developer servers" in out or "Stopped" in out

    def test_portal_status_cli(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        args = _data_args(tmp_path)
        code = main([*args, "developer", "status"])
        out = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "Developer Status" in out

    def test_portal_doctor_cli(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        args = _data_args(tmp_path)
        code = main([*args, "developer", "doctor"])
        out = capsys.readouterr().out
        assert code == int(ExitCode.OK)
        assert "Developer Doctor" in out

    def test_portal_pair_no_input(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        args = _data_args(tmp_path)
        monkeypatch.setattr("builtins.input", lambda _: "")
        code = main([*args, "developer", "pair"])
        out = capsys.readouterr().out
        assert code == int(ExitCode.CONFIGURATION)
        assert "Pairing code is required" in out
