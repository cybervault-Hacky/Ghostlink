"""TOML loading, structural validation, and deep merging."""

from __future__ import annotations

from pathlib import Path

import pytest

from ghostlink.config.loader import deep_merge, load_config_file, validate_sections
from ghostlink.exceptions.config import ConfigParseError, ConfigValidationError


class TestLoadConfigFile:
    def test_parses_valid_toml(self, tmp_path: Path) -> None:
        path = tmp_path / "config.toml"
        path.write_text('[ui]\ntheme = "ember"\n', encoding="utf-8")
        assert load_config_file(path) == {"ui": {"theme": "ember"}}

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigParseError, match="does not exist"):
            load_config_file(tmp_path / "absent.toml")

    def test_invalid_toml_raises_with_hint(self, tmp_path: Path) -> None:
        path = tmp_path / "broken.toml"
        path.write_text("[ui\ntheme = !!", encoding="utf-8")
        with pytest.raises(ConfigParseError) as captured:
            load_config_file(path)
        assert captured.value.hint is not None
        assert "regenerate" in captured.value.hint


class TestValidateSections:
    def test_accepts_known_schema(self) -> None:
        data = {"ui": {"theme": "mono"}, "diagnostics": {"debug": True}}
        cleaned = validate_sections(data, source="test")
        assert cleaned["ui"]["theme"] == "mono"
        assert cleaned["diagnostics"]["debug"] is True

    def test_rejects_unknown_section(self) -> None:
        with pytest.raises(ConfigValidationError, match="unknown configuration section"):
            validate_sections({"crypto": {"cipher": "aes"}}, source="test")

    def test_rejects_unknown_key_with_allowed_list(self) -> None:
        with pytest.raises(ConfigValidationError) as captured:
            validate_sections({"ui": {"font": "big"}}, source="test")
        assert "font" in str(captured.value)
        assert "theme" in (captured.value.hint or "")

    def test_rejects_non_table_section(self) -> None:
        with pytest.raises(ConfigValidationError, match="must be a TOML table"):
            validate_sections({"ui": "phantom"}, source="test")


class TestDeepMerge:
    def test_merges_nested_without_mutation(self) -> None:
        base = {"ui": {"theme": "phantom", "language": "en"}, "top": 1}
        override = {"ui": {"theme": "ember"}}
        merged = deep_merge(base, override)
        assert merged == {"ui": {"theme": "ember", "language": "en"}, "top": 1}
        assert base["ui"]["theme"] == "phantom"  # original untouched

    def test_override_scalars_and_new_keys(self) -> None:
        assert deep_merge({"a": 1}, {"a": 2, "b": 3}) == {"a": 2, "b": 3}
