"""Chart primitives: sparklines and a latency panel for network dashboards."""

from __future__ import annotations

from collections.abc import Sequence
from statistics import fmean, pstdev

from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from ghostlink.models.theme import ThemeSpec
from ghostlink.ui.components.tables import kv_grid
from ghostlink.ui.gradients import mix_colors

SPARK_CHARS = "▁▂▃▄▅▆▇█"


def sparkline(
    values: Sequence[float],
    *,
    theme: ThemeSpec | None = None,
    width: int | None = None,
) -> Text:
    """Render values as a one-line block sparkline, gradient-tinted by height."""

    series = list(values)
    if width is not None and width >= 4:
        series = series[-width:]
    if not series:
        return Text("no samples yet", style="gl.muted")

    low = min(series)
    high = max(series)
    spread = high - low
    text = Text()
    for value in series:
        ratio = (value - low) / spread if spread > 0 else 0.5
        level = round(ratio * (len(SPARK_CHARS) - 1))
        char = SPARK_CHARS[level]
        if theme is not None:
            color = mix_colors(theme.gradient[0], theme.gradient[1], ratio)
            text.append(char, style=f"bold {color}")
        else:
            text.append(char, style="gl.accent")
    return text


def latency_stats(samples_ms: Sequence[float]) -> dict[str, float]:
    """Summary statistics for RTT samples; empty dict when no samples."""

    if not samples_ms:
        return {}
    values = list(samples_ms)
    stats = {
        "min": min(values),
        "avg": fmean(values),
        "max": max(values),
        "last": values[-1],
        "count": float(len(values)),
    }
    stats["jitter"] = pstdev(values) if len(values) > 1 else 0.0
    return stats


def latency_panel(
    samples_ms: Sequence[float],
    *,
    theme: ThemeSpec,
    title: str = "Latency",
) -> Panel:
    """Sparkline + min/avg/max/last/jitter grid for RTT measurements."""

    stats = latency_stats(samples_ms)
    if not stats:
        body: Group | Text = Text("No round-trip samples were collected.", style="gl.muted")
    else:
        grid = kv_grid(
            [
                ("Samples", Text(str(int(stats["count"])))),
                ("Min", Text(f"{stats['min']:.1f} ms")),
                ("Average", Text(f"{stats['avg']:.1f} ms")),
                ("Max", Text(f"{stats['max']:.1f} ms")),
                ("Last", Text(f"{stats['last']:.1f} ms")),
                ("Jitter", Text(f"±{stats['jitter']:.1f} ms")),
            ]
        )
        body = Group(sparkline(samples_ms, theme=theme), Text(""), grid)
    return Panel(
        body,
        title=f"[gl.accent]{title}[/]",
        border_style="gl.border",
        padding=(1, 2),
        expand=False,
    )
