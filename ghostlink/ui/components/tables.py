"""Table primitives for key/value and grid presentation."""

from __future__ import annotations

from collections.abc import Iterable

from rich.table import Table
from rich.text import Text

KeyValueRow = tuple[str, str | Text]


def info_table(
    rows: Iterable[KeyValueRow],
    *,
    label_header: str = "Setting",
    value_header: str = "Value",
    title: str | None = None,
) -> Table:
    """A two-column labelled table (settings, diagnostics, reports)."""

    table = Table(
        title=title,
        title_style="gl.title",
        title_justify="left",
        box=None,
        show_header=True,
        header_style="gl.accent",
        show_edge=False,
        pad_edge=False,
        expand=False,
    )
    table.add_column(label_header, style="gl.muted", no_wrap=True, ratio=1)
    table.add_column(value_header, style="gl.text", ratio=3, overflow="fold")
    for label, value in rows:
        table.add_row(label, value)
    return table


def kv_grid(
    rows: Iterable[KeyValueRow],
    *,
    padding: tuple[int, int, int, int] = (0, 2, 0, 0),
) -> Table:
    """A borderless key/value grid for compact in-panel layouts."""

    grid = Table.grid(padding=padding)
    grid.add_column(style="gl.muted", no_wrap=True)
    grid.add_column(style="gl.text", overflow="fold")
    for label, value in rows:
        grid.add_row(label, value)
    return grid
