"""Phase 9 config & state migration tests.

GhostLink fails closed on documents written by a newer version: it refuses
to reinterpret them rather than guessing. It also rejects malformed version
markers. Absent version = version 1 (backward compatible).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ghostlink.config.loader import load_config_file, validate_sections
from ghostlink.core.migration import (
    CONFIG_SCHEMA_VERSION,
    STATE_SCHEMA_VERSION,
    validate_config_version,
    validate_state_version,
)
from ghostlink.exceptions.config import ConfigValidationError


class TestConfigVersion:
    def test_absent_version_is_accepted(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[ui]\ntheme = 'phantom'\n")
        raw = load_config_file(path)
        sections = validate_sections(raw, source=str(path))
        assert sections["ui"]["theme"] == "phantom"

    def test_current_version_is_accepted(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text(
            f"[meta]\nconfig_version = {CONFIG_SCHEMA_VERSION}\n[ui]\ntheme = 'phantom'\n"
        )
        raw = load_config_file(path)
        sections = validate_sections(raw, source=str(path))
        assert sections["meta"]["config_version"] == CONFIG_SCHEMA_VERSION

    def test_future_version_fails_closed(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[meta]\nconfig_version = 9999\n[ui]\ntheme = 'phantom'\n")
        raw = load_config_file(path)
        with pytest.raises(ConfigValidationError):
            validate_sections(raw, source=str(path))

    def test_non_integer_version_rejected(self) -> None:
        with pytest.raises(ConfigValidationError):
            validate_config_version("1.0")
        with pytest.raises(ConfigValidationError):
            validate_config_version(True)

    def test_unknown_section_still_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text("[bogus]\nx = 1\n")
        raw = load_config_file(path)
        with pytest.raises(ConfigValidationError):
            validate_sections(raw, source=str(path))


class TestStateVersion:
    def test_group_record_future_version_fails_closed(self) -> None:
        from ghostlink.groups.models import LocalGroupRecord

        record = {
            "v": 99,
            "group_id": "gl-group-AAAA-BBBB-CCCC",
            "name": "Ops",
            "owner_fingerprint": "GLFP-ABCD-1234-5678",
            "my_fingerprint": "GLFP-ABCD-1234-5678",
            "epoch": 1,
            "state": "active",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "members": {},
        }
        with pytest.raises(ConfigValidationError):
            LocalGroupRecord.from_dict(record)

    def test_group_record_current_version_accepted(self) -> None:
        from ghostlink.groups.models import LocalGroupRecord

        record = {
            "v": 1,
            "group_id": "gl-group-AAAA-BBBB-CCCC",
            "name": "Ops",
            "owner_fingerprint": "GLFP-ABCD-1234-5678",
            "my_fingerprint": "GLFP-ABCD-1234-5678",
            "epoch": 1,
            "state": "active",
            "created_at": "2026-01-01T00:00:00+00:00",
            "updated_at": "2026-01-01T00:00:00+00:00",
            "members": {},
        }
        parsed = LocalGroupRecord.from_dict(record)
        assert parsed.epoch == 1

    def test_state_version_helpers(self) -> None:
        assert STATE_SCHEMA_VERSION == 1
        validate_state_version(None)
        validate_state_version(1)
        with pytest.raises(ConfigValidationError):
            validate_state_version(2)
        with pytest.raises(ConfigValidationError):
            validate_state_version("1")
