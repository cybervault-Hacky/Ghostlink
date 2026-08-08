"""ConfigurationManager: first-run generation, overrides, directory resolution."""

from __future__ import annotations

from pathlib import Path

import pytest

from ghostlink.config.manager import ConfigOverrides, ConfigurationManager
from ghostlink.exceptions.config import ConfigValidationError


class TestFirstRun:
    def test_generates_default_file(self, isolated_home: Path) -> None:
        manager = ConfigurationManager()
        settings = manager.load()
        assert manager.created_default is True
        assert manager.config_path.is_file()
        content = manager.config_path.read_text(encoding="utf-8")
        assert "[ui]" in content and 'theme = "phantom"' in content
        assert settings.ui.theme == "phantom"

    def test_second_load_does_not_regenerate(self, isolated_home: Path) -> None:
        ConfigurationManager().load()
        second = ConfigurationManager()
        second.load()
        assert second.created_default is False

    def test_probe_mode_is_read_only(self, isolated_home: Path) -> None:
        manager = ConfigurationManager()
        settings = manager.load(create_missing=False)
        assert manager.created_default is False
        assert not manager.config_path.exists()
        assert settings.ui.theme == "phantom"  # defaults still supplied


class TestValidation:
    def test_invalid_language(self, isolated_home: Path) -> None:
        manager = ConfigurationManager()
        manager.load()
        manager.config_path.write_text('[ui]\nlanguage = "xx"\n', encoding="utf-8")
        with pytest.raises(ConfigValidationError, match=r"ui\.language"):
            ConfigurationManager().load()

    def test_empty_theme_rejected(self, isolated_home: Path) -> None:
        manager = ConfigurationManager()
        manager.load()
        manager.config_path.write_text('[ui]\ntheme = "  "\n', encoding="utf-8")
        with pytest.raises(ConfigValidationError, match=r"ui\.theme"):
            ConfigurationManager().load()


class TestOverrides:
    def test_theme_override_wins_over_file(self, isolated_home: Path) -> None:
        ConfigurationManager().load()
        manager = ConfigurationManager(overrides=ConfigOverrides(theme="ember"))
        assert manager.load().ui.theme == "ember"

    def test_data_dir_override_wins_over_file(self, isolated_home: Path, tmp_path: Path) -> None:
        custom = tmp_path / "custom-data"
        manager = ConfigurationManager(overrides=ConfigOverrides(data_dir=custom))
        manager.load()
        assert manager.data_dir == custom
        assert manager.state_dir == custom / "state"
        assert manager.logs_dir == custom / "logs"

    def test_debug_override(self, isolated_home: Path) -> None:
        manager = ConfigurationManager(overrides=ConfigOverrides(debug=True))
        assert manager.load().diagnostics.debug is True


class TestDirectories:
    def test_defaults_under_xdg(self, isolated_home: Path) -> None:
        manager = ConfigurationManager()
        manager.load()
        assert manager.config_path == isolated_home / ".config" / "ghostlink" / "config.toml"
        assert manager.data_dir == isolated_home / ".local" / "share" / "ghostlink"

    def test_configured_data_dir_in_file(self, isolated_home: Path, tmp_path: Path) -> None:
        manager = ConfigurationManager()
        manager.load()
        target = tmp_path / "elsewhere"
        manager.config_path.write_text(f'[storage]\ndata_dir = "{target}"\n', encoding="utf-8")
        fresh = ConfigurationManager()
        fresh.load()
        assert fresh.data_dir == target

    def test_ensure_directories_creates_tree(self, isolated_home: Path) -> None:
        manager = ConfigurationManager()
        manager.load()
        manager.ensure_directories()
        for path in (manager.config_dir, manager.data_dir, manager.state_dir, manager.logs_dir):
            assert path.is_dir()
