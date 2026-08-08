"""Screen infrastructure.

Every screen receives a :class:`ScreenContext` — the typed bundle of runtime
services it may use — and implements the async :meth:`Screen.show` contract.
Async screens keep Phase 2 free to await transports directly inside a screen
without restructuring the UI.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ghostlink.models.environment import EnvironmentInfo
from ghostlink.models.settings import AppSettings
from ghostlink.models.theme import ThemeSpec
from ghostlink.services.rooms import RoomService
from ghostlink.ui.components.dialogs import wait_for_enter
from ghostlink.ui.components.notifications import NotificationCenter, NotificationLevel
from ghostlink.ui.console import ConsoleManager

DeferredNotice = tuple[NotificationLevel, str]


@dataclass(slots=True)
class ScreenContext:
    """Everything a screen needs, injected once during bootstrap."""

    console: ConsoleManager
    settings: AppSettings
    environment: EnvironmentInfo
    notifications: NotificationCenter
    config_path: Path
    data_dir: Path
    rooms: RoomService
    deferred: list[DeferredNotice] = field(default_factory=list)

    @property
    def theme(self) -> ThemeSpec:
        return self.console.theme

    def drain_deferred(self) -> tuple[DeferredNotice, ...]:
        """Return deferred bootstrap notices exactly once."""

        notices = tuple(self.deferred)
        self.deferred.clear()
        return notices


class Screen(ABC):
    """Base class for full-screen views."""

    def __init__(self, context: ScreenContext) -> None:
        self.context = context

    @abstractmethod
    async def show(self) -> None:
        """Render the screen and run its interaction until it is dismissed."""

    async def pause(self, prompt: str = "Press Enter to return…") -> None:
        """Wait for acknowledgement without blocking the event loop."""

        await asyncio.to_thread(wait_for_enter, self.context.console.console)
