"""Theme engine: registry, resolution, and style projection."""

from __future__ import annotations

import pytest

from ghostlink.exceptions.themes import ThemeNotFoundError
from ghostlink.ui.themes import ThemeEngine


class TestThemeRegistry:
    def test_builtin_themes_present(self) -> None:
        names = ThemeEngine().names
        expected = {"phantom", "obsidian", "ember", "emerald", "arctic", "aurora", "mono"}
        assert expected <= set(names)
        assert len(names) >= 7

    def test_default_is_phantom(self) -> None:
        engine = ThemeEngine()
        assert engine.default_name == "phantom"
        assert engine.default().name == "phantom"

    def test_case_insensitive_resolution(self) -> None:
        assert ThemeEngine().get("  PHANTOM ").name == "phantom"


class TestThemeResolution:
    def test_unknown_theme_lists_available(self) -> None:
        with pytest.raises(ThemeNotFoundError) as captured:
            ThemeEngine().get("solarized")
        hint = captured.value.hint or ""
        assert "phantom" in hint and "mono" in hint

    def test_every_theme_defines_all_style_tokens(self) -> None:
        engine = ThemeEngine()
        expected_styles = {
            "gl.primary",
            "gl.accent",
            "gl.success",
            "gl.warning",
            "gl.error",
            "gl.info",
            "gl.muted",
            "gl.text",
            "gl.border",
            "gl.highlight",
            "gl.title",
        }
        for name in engine.names:
            styles = engine.get(name).style_map()
            assert expected_styles <= set(styles), f"theme '{name}' misses tokens"

    def test_gradient_is_two_hex_colors(self) -> None:
        for name in ThemeEngine().names:
            gradient = ThemeEngine().get(name).gradient
            assert len(gradient) == 2
            assert all(color.startswith("#") and len(color) == 7 for color in gradient)

    def test_rich_theme_registration(self) -> None:
        rich_theme = ThemeEngine.rich_theme(ThemeEngine().get("phantom"))
        assert "gl.primary" in rich_theme.styles
