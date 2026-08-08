"""Color interpolation helpers shared by branding and charts."""

from __future__ import annotations


def hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = color.lstrip("#")
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def mix_colors(start: str, end: str, ratio: float) -> str:
    """Linearly interpolate two hex colors at ``ratio`` in ``[0, 1]``."""

    clamped = min(1.0, max(0.0, ratio))
    sr, sg, sb = hex_to_rgb(start)
    er, eg, eb = hex_to_rgb(end)
    return (
        f"#{round(sr + (er - sr) * clamped):02x}"
        f"{round(sg + (eg - sg) * clamped):02x}"
        f"{round(sb + (eb - sb) * clamped):02x}"
    )
