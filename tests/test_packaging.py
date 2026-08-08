"""Packaging integrity: version sync, packaged assets, and metadata."""

from __future__ import annotations

import tomllib
from pathlib import Path

import ghostlink
from ghostlink.assets.branding import load_default_config_text
from ghostlink.constants.app import APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestVersionSync:
    def test_pyproject_matches_package_version(self) -> None:
        pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert pyproject["project"]["version"] == ghostlink.__version__
        assert ghostlink.__version__ == APP_VERSION

    def test_version_info_matches(self) -> None:
        assert ghostlink.version_info == tuple(
            int(part) for part in ghostlink.__version__.split(".")
        )


class TestPackagedAssets:
    def test_default_config_resource_is_available(self) -> None:
        text = load_default_config_text()
        assert "[ui]" in text
        assert "[diagnostics]" in text

    def test_default_config_parses_as_toml(self) -> None:
        parsed = tomllib.loads(load_default_config_text())
        assert parsed["ui"]["theme"] == "phantom"
        assert parsed["diagnostics"]["debug"] is False
