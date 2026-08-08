"""Progress components for multi-step operations.

``StepProgress`` shows a themed spinner whose label follows the current step;
on non-interactive terminals it degrades to silent execution (the steps still
run — only the display changes), which keeps piped and CI output clean.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from types import TracebackType
from typing import Literal

from rich.progress import BarColumn, Progress, SpinnerColumn, TaskID, TextColumn

from ghostlink.core.logging import get_logger
from ghostlink.ui.console import ConsoleManager


class StepProgress:
    """Drive a labelled spinner through a sequence of named steps."""

    def __init__(self, console: ConsoleManager, *, title: str | None = None) -> None:
        self._console = console
        self._title = title
        self._logger = get_logger("ui.progress")
        self._active = console.is_terminal
        self._progress = Progress(
            SpinnerColumn(style="gl.accent"),
            TextColumn("[gl.muted]{task.description}"),
            BarColumn(bar_width=None, complete_style="gl.accent", finished_style="gl.success"),
            console=console.console,
            transient=True,
            disable=not self._active,
        )
        self._task_id: TaskID | None = None

    def __enter__(self) -> StepProgress:
        if self._active:
            self._progress.__enter__()
            self._task_id = self._progress.add_task(self._title or "Working…", total=None)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> Literal[False]:
        if self._active:
            self._progress.__exit__(exc_type, exc, tb)
        return False

    @contextmanager
    def step(self, description: str) -> Iterator[None]:
        """Mark ``description`` as the current step while the block runs."""

        self._logger.debug("step: %s", description)
        if self._active and self._task_id is not None:
            self._progress.update(self._task_id, description=description)
        yield


def track_bar(console: ConsoleManager) -> Progress:
    """A standalone determinate progress bar for future transfer workloads."""

    return Progress(
        SpinnerColumn(style="gl.accent"),
        TextColumn("[gl.text]{task.description}"),
        BarColumn(bar_width=None, complete_style="gl.accent", finished_style="gl.success"),
        TextColumn("[gl.muted]{task.percentage:>3.0f}%"),
        console=console.console,
        transient=True,
    )
