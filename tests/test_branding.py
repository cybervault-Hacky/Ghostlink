"""Branding assets and the adaptive banner renderer."""

from __future__ import annotations

import pytest

from ghostlink.assets.branding import (
    GHOST_EMBLEM,
    LOGO_LARGE,
    LOGO_MIN_WIDTH,
    LOGO_STACKED,
    LOGO_STACKED_MIN_WIDTH,
    WORDMARK,
)
from ghostlink.models.theme import ThemeSpec
from ghostlink.ui.banner import BannerRenderer
from ghostlink.ui.console import ConsoleManager
from ghostlink.ui.gradients import mix_colors


class TestBrandAssets:
    def test_large_logo_block_shape(self) -> None:
        assert len(LOGO_LARGE) == 6
        assert max(len(line) for line in LOGO_LARGE) == LOGO_MIN_WIDTH
        # Every row carries glyphs (no empty row in the large wordmark)
        assert all(line.strip() for line in LOGO_LARGE)

    def test_stacked_logo_contains_split(self) -> None:
        assert len(LOGO_STACKED) == 13  # 6 + blank separator + 6
        assert LOGO_STACKED_MIN_WIDTH < LOGO_MIN_WIDTH

    def test_emblem_is_rectangular(self) -> None:
        assert len({len(line) for line in GHOST_EMBLEM}) == 1


class TestGradientMath:
    def test_mix_endpoints(self) -> None:
        assert mix_colors("#000000", "#ffffff", 0.0) == "#000000"
        assert mix_colors("#000000", "#ffffff", 1.0) == "#ffffff"

    def test_mix_midpoint(self) -> None:
        assert mix_colors("#000000", "#ffffff", 0.5) == "#808080"


class TestAdaptiveSelection:
    def _renderer(self, width: int, theme: ThemeSpec) -> BannerRenderer:
        return BannerRenderer(ConsoleManager(theme, record=True, width=width))

    def test_wide_terminal_uses_large_logo(self, theme: ThemeSpec) -> None:
        renderer = self._renderer(LOGO_MIN_WIDTH + 20, theme)
        assert renderer.select_art() == LOGO_LARGE

    def test_medium_terminal_uses_stacked_logo(self, theme: ThemeSpec) -> None:
        renderer = self._renderer(LOGO_STACKED_MIN_WIDTH + 4, theme)
        assert renderer.select_art() == LOGO_STACKED

    def test_narrow_terminal_uses_wordmark(self, theme: ThemeSpec) -> None:
        renderer = self._renderer(40, theme)
        assert renderer.select_art() == WORDMARK

    def test_wordmark_renders_glyph_rows(self, theme: ThemeSpec) -> None:
        renderer = self._renderer(120, theme)
        text = renderer.wordmark()
        assert "██████" in text.plain

    def test_no_color_wordmark_is_plain(self, theme: ThemeSpec) -> None:
        console = ConsoleManager(theme, record=True, width=120, no_color=True)
        text = BannerRenderer(console).wordmark()
        assert text.plain.strip()
        # No styling spans are produced when colour is disabled
        assert len(text.spans) == 0

    def test_hero_block_contains_tagline(self, theme: ThemeSpec) -> None:
        console = ConsoleManager(theme, record=True, width=120)
        console.print(BannerRenderer(console).hero())
        assert "Private conversations" in console.export_text()


class TestStatusBadgesBranding:
    def test_hero_badge_row_content(self, theme: ThemeSpec) -> None:
        from ghostlink.constants.app import APP_VERSION
        from ghostlink.ui.components.badges import BadgeTone, badge, badge_row

        console = ConsoleManager(theme, record=True, width=120)
        status = badge_row(
            badge(f"v{APP_VERSION}", BadgeTone.ACCENT, theme=theme),
            badge("Termux (Android)", BadgeTone.INFO, theme=theme),
        )
        console.print(status)
        output = console.export_text()
        assert f"v{APP_VERSION}" in output
        assert "Termux (Android)" in output
        assert "Phase 15" not in output
        assert "Production Operations & Platform Maturity" not in output

    def test_hero_badge_row_renders_on_standard_terminal_width(self, theme: ThemeSpec) -> None:
        from ghostlink.constants.app import APP_VERSION
        from ghostlink.ui.components.badges import BadgeTone, badge, badge_row

        console = ConsoleManager(theme, record=True, width=80)
        status = badge_row(
            badge(f"v{APP_VERSION}", BadgeTone.ACCENT, theme=theme),
            badge("Termux (Android)", BadgeTone.INFO, theme=theme),
        )
        console.print(status)
        lines = [line.strip() for line in console.export_text().splitlines() if line.strip()]
        assert len(lines) == 1
        assert "◆ v" in lines[0]
        assert "● Termux (Android)" in lines[0]
        assert "Phase 15" not in lines[0]

    def test_home_screen_contains_no_phase_15_text(
        self, isolated_home: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from ghostlink.cli.arguments import parse_args
        from ghostlink.constants.app import APP_VERSION
        from ghostlink.core.bootstrap import build_application
        from ghostlink.ui.menu import InteractiveMenu
        from ghostlink.ui.screens.home import HomeScreen

        application = build_application(parse_args([]))
        recording = ConsoleManager(
            application._context.console.theme,
            record=True,
            width=100,
            force_terminal=False,
        )
        application._context.console = recording
        monkeypatch.setattr(
            InteractiveMenu,
            "prompt",
            lambda self, entries, *, default_key: "exit",
        )

        asyncio.run(HomeScreen(application._context).show())
        output = recording.export_text()
        assert f"v{APP_VERSION}" in output
        assert "Phase 15" not in output
        assert "Production Operations & Platform Maturity" not in output

    def test_compact_header_contains_no_phase_15_text(self, theme: ThemeSpec) -> None:
        console = ConsoleManager(theme, record=True, width=100)
        BannerRenderer(console).compact_header()
        console.print(BannerRenderer(console).compact_header())
        output = console.export_text()
        assert "GHOSTLINK" in output
        assert "Phase 15" not in output
        assert "Production Operations & Platform Maturity" not in output

    def test_about_screen_contains_no_phase_15_text(
        self, isolated_home: object, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import asyncio

        from ghostlink.cli.arguments import parse_args
        from ghostlink.constants.app import APP_VERSION
        from ghostlink.core.bootstrap import build_application
        from ghostlink.ui.screens.about import AboutScreen
        from ghostlink.ui.screens.base import Screen

        application = build_application(parse_args([]))
        recording = ConsoleManager(
            application._context.console.theme,
            record=True,
            width=100,
            force_terminal=False,
        )
        application._context.console = recording

        async def _instant_pause(self: Screen, prompt: str = "…") -> None:
            return None

        monkeypatch.setattr(Screen, "pause", _instant_pause)

        asyncio.run(AboutScreen(application._context).show())
        output = recording.export_text()
        assert f"v{APP_VERSION}" in output
        assert "Phase 15" not in output
        assert "Production Operations & Platform Maturity" not in output
