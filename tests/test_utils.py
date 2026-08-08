"""Utilities: XDG paths, directory provisioning, and text helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from ghostlink.exceptions.storage import StorageError
from ghostlink.utils.paths import (
    default_config_dir,
    default_data_dir,
    ensure_directory,
    xdg_config_home,
    xdg_data_home,
)
from ghostlink.utils.text import format_duration, pluralize, truncate


class TestXdgPaths:
    def test_defaults_under_home(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        assert xdg_config_home(env={}, home=home) == home / ".config"
        assert xdg_data_home(env={}, home=home) == home / ".local" / "share"

    def test_env_overrides(self, tmp_path: Path) -> None:
        env = {
            "XDG_CONFIG_HOME": str(tmp_path / "cfg"),
            "XDG_DATA_HOME": str(tmp_path / "data"),
        }
        assert xdg_config_home(env=env, home=tmp_path) == tmp_path / "cfg"
        assert xdg_data_home(env=env, home=tmp_path) == tmp_path / "data"

    def test_blank_override_falls_back(self, tmp_path: Path) -> None:
        env = {"XDG_CONFIG_HOME": "   "}
        assert xdg_config_home(env=env, home=tmp_path) == tmp_path / ".config"

    def test_application_dirs_use_slug(self, tmp_path: Path) -> None:
        assert default_config_dir(env={}, home=tmp_path).name == "ghostlink"
        assert default_data_dir(env={}, home=tmp_path).name == "ghostlink"


class TestEnsureDirectory:
    def test_creates_nested(self, tmp_path: Path) -> None:
        target = tmp_path / "a" / "b" / "c"
        assert ensure_directory(target) == target
        assert target.is_dir()

    def test_existing_directory_ok(self, tmp_path: Path) -> None:
        assert ensure_directory(tmp_path) == tmp_path

    def test_failure_raises_ghostlink_error(self, tmp_path: Path) -> None:
        blocked = tmp_path / "file"
        blocked.write_text("occupied", encoding="utf-8")
        with pytest.raises(StorageError) as captured:
            ensure_directory(blocked / "child")
        assert captured.value.hint is not None


class TestTextHelpers:
    def test_truncate_shortens_with_ellipsis(self) -> None:
        assert truncate("hello world", 8) == "hello w…"
        assert truncate("short", 8) == "short"

    def test_truncate_edge_widths(self) -> None:
        assert truncate("abcdef", 1) == "…"
        with pytest.raises(ValueError):
            truncate("text", 0)

    def test_pluralize(self) -> None:
        assert pluralize(1, "room") == "1 room"
        assert pluralize(3, "room") == "3 rooms"
        assert pluralize(2, "key", "keys") == "2 keys"

    def test_format_duration(self) -> None:
        assert format_duration(0.42) == "0.42s"
        assert format_duration(9) == "9s"
        assert format_duration(64) == "1m 04s"
        assert format_duration(3725) == "1h 02m"
