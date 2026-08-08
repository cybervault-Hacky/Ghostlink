"""Small, dependency-free text helpers used across the interface."""

from __future__ import annotations


def truncate(text: str, width: int, *, ellipsis: str = "…") -> str:
    """Truncate ``text`` to at most ``width`` characters, preserving the head.

    Widths smaller than one character are rejected; when truncation occurs the
    returned string includes the ellipsis and still fits within ``width``.
    """

    if width < 1:
        raise ValueError("width must be a positive integer")
    if len(text) <= width:
        return text
    if width <= len(ellipsis):
        return ellipsis[:width]
    return text[: width - len(ellipsis)] + ellipsis


def pluralize(count: int, singular: str, plural: str | None = None) -> str:
    """Return ``count`` + the correctly inflected noun: ``3 items``."""

    form = singular if count == 1 else (plural or f"{singular}s")
    return f"{count} {form}"


def format_bytes(size: float) -> str:
    """Human-readable byte count, 1024-based: ``512 B``, ``18.4 MB``."""

    if size < 0:
        size = 0
    if size < 1024:
        return f"{int(size)} B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024.0
        if size < 1024:
            return f"{size:.1f} {unit}"
    return f"{size:.1f} PB"


def format_duration(seconds: float) -> str:
    """Render a duration compactly: ``0.42s``, ``3m 04s``, ``1h 02m``."""

    if seconds < 1:
        return f"{seconds:.2f}s"
    whole = int(seconds)
    if whole < 60:
        return f"{whole}s"
    minutes, secs = divmod(whole, 60)
    if minutes < 60:
        return f"{minutes}m {secs:02d}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins:02d}m"
