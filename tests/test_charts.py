"""Chart primitives and the shared color helpers behind them."""

from __future__ import annotations

from ghostlink.models.theme import ThemeSpec
from ghostlink.ui.components.charts import SPARK_CHARS, latency_panel, latency_stats, sparkline
from ghostlink.ui.console import ConsoleManager
from ghostlink.ui.gradients import hex_to_rgb, mix_colors


class TestGradients:
    def test_hex_to_rgb(self) -> None:
        assert hex_to_rgb("#ff0080") == (255, 0, 128)
        assert hex_to_rgb("00ff00") == (0, 255, 0)

    def test_mix_at_boundaries(self) -> None:
        assert mix_colors("#000000", "#ffffff", 0.0) == "#000000"
        assert mix_colors("#000000", "#ffffff", 1.0) == "#ffffff"

    def test_mix_midpoint(self) -> None:
        assert mix_colors("#000000", "#ffffff", 0.5) == "#808080"

    def test_mix_clamps_out_of_range_ratios(self) -> None:
        assert mix_colors("#102030", "#a0b0c0", -1.0) == "#102030"
        assert mix_colors("#102030", "#a0b0c0", 5.0) == "#a0b0c0"


class TestSparkline:
    def test_empty_series_shows_placeholder(self) -> None:
        assert sparkline([]).plain == "no samples yet"

    def test_monotonic_series_rises(self) -> None:
        rendered = sparkline([1.0, 2.0, 3.0, 4.0])
        assert rendered.plain[0] == SPARK_CHARS[0]
        assert rendered.plain[-1] == SPARK_CHARS[-1]

    def test_flat_series_uses_a_single_level(self) -> None:
        rendered = sparkline([7.0, 7.0, 7.0])
        assert len(set(rendered.plain)) == 1

    def test_width_truncates_to_recent_samples(self) -> None:
        rendered = sparkline([float(i) for i in range(10)], width=4)
        assert len(rendered.plain) == 4
        assert rendered.plain == sparkline([6.0, 7.0, 8.0, 9.0]).plain

    def test_theme_gradient_colors_are_applied(self, theme: ThemeSpec) -> None:
        rendered = sparkline([0.0, 1.0], theme=theme)
        styles = {str(span.style) for span in rendered._spans}
        assert any("bold #" in style for style in styles)

    def test_output_is_single_line(self) -> None:
        assert "\n" not in sparkline([1.0, 5.0, 3.0]).plain


class TestLatencyStats:
    def test_empty_input(self) -> None:
        assert latency_stats([]) == {}

    def test_single_sample(self) -> None:
        stats = latency_stats([12.5])
        assert stats == {
            "min": 12.5,
            "avg": 12.5,
            "max": 12.5,
            "last": 12.5,
            "count": 1.0,
            "jitter": 0.0,
        }

    def test_aggregation(self) -> None:
        stats = latency_stats([10.0, 20.0, 30.0])
        assert stats["min"] == 10.0
        assert stats["avg"] == 20.0
        assert stats["max"] == 30.0
        assert stats["last"] == 30.0
        assert stats["count"] == 3.0
        assert stats["jitter"] > 0.0

    def test_panel_with_samples(self, console_manager: ConsoleManager) -> None:
        console_manager.print(latency_panel([1.0, 2.0, 3.0], theme=console_manager.theme))
        output = console_manager.export_text()
        assert "Latency" in output
        assert "Jitter" in output
        assert "3.0 ms" in output

    def test_panel_without_samples(self, console_manager: ConsoleManager) -> None:
        console_manager.print(latency_panel([], theme=console_manager.theme))
        assert "No round-trip samples" in console_manager.export_text()
