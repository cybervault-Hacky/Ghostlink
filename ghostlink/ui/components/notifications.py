"""Notification toasts and history.

The :class:`NotificationCenter` renders compact one-line toasts and keeps a
bounded in-memory history regardless of the enabled switch — diagnostics can
always inspect what was raised, even when display is muted.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from rich.text import Text

from ghostlink.ui.console import ConsoleManager


class NotificationLevel(Enum):
    INFO = ("●", "gl.info")
    SUCCESS = ("✔", "gl.success")
    WARNING = ("▲", "gl.warning")
    ERROR = ("✖", "gl.error")

    @property
    def icon(self) -> str:
        return self.value[0]

    @property
    def style(self) -> str:
        return self.value[1]


@dataclass(frozen=True, slots=True)
class Notification:
    message: str
    level: NotificationLevel
    raised_at: datetime


class NotificationCenter:
    """Renders toasts through the console and records notification history."""

    def __init__(
        self,
        console: ConsoleManager,
        *,
        enabled: bool = True,
        history_limit: int = 100,
    ) -> None:
        self._console = console
        self._enabled = enabled
        self._history: deque[Notification] = deque(maxlen=history_limit)

    @property
    def enabled(self) -> bool:
        return self._enabled

    def history(self) -> tuple[Notification, ...]:
        return tuple(self._history)

    def notify(self, message: str, level: NotificationLevel = NotificationLevel.INFO) -> None:
        entry = Notification(message=message, level=level, raised_at=datetime.now())
        self._history.append(entry)
        if not self._enabled:
            return
        toast = Text()
        toast.append(f" {entry.level.icon} ", style=f"bold {entry.level.style}")
        toast.append(message, style="gl.text")
        toast.append(f"  ·  {entry.raised_at:%H:%M:%S}", style="gl.muted")
        self._console.print(toast)

    # ------------------------------------------------------------ convenience

    def info(self, message: str) -> None:
        self.notify(message, NotificationLevel.INFO)

    def success(self, message: str) -> None:
        self.notify(message, NotificationLevel.SUCCESS)

    def warning(self, message: str) -> None:
        self.notify(message, NotificationLevel.WARNING)

    def error(self, message: str) -> None:
        self.notify(message, NotificationLevel.ERROR)

    def __iter__(self) -> Iterator[Notification]:
        return iter(self._history)
