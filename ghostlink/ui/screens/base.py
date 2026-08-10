"""Screen infrastructure.

Every screen receives a :class:`ScreenContext` — the typed bundle of runtime
services it may use — and implements the async :meth:`Screen.show` contract.

Screens share one visual language through :meth:`Screen.header`, the Back
helpers on :class:`~ghostlink.ui.menu.InteractiveMenu` and the primitives in
:mod:`ghostlink.ui.components.layout`, so no screen invents its own chrome.
"""

from __future__ import annotations

import asyncio
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path

from ghostlink.i18n import t
from ghostlink.models.environment import EnvironmentInfo
from ghostlink.models.settings import AppSettings
from ghostlink.models.theme import ThemeSpec
from ghostlink.services.rooms import RoomService
from ghostlink.ui.components.dialogs import wait_for_enter
from ghostlink.ui.components.layout import back_label, page_header
from ghostlink.ui.components.notifications import NotificationCenter, NotificationLevel
from ghostlink.ui.console import ConsoleManager
from ghostlink.ui.menu import MenuEntry

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

    # ------------------------------------------------------------- chrome

    def header(self, title: str, subtitle: str | None = None) -> None:
        """Clear and print the standard screen header (title/subtitle/rule)."""

        console = self.context.console
        console.clear()
        console.newline()
        console.print(page_header(title, subtitle))
        console.newline()

    def back_menu_entry(self, description: str = "") -> MenuEntry:
        """The one Back affordance — same label on every screen."""

        lang = self.context.settings.ui.language
        return MenuEntry(
            key="back", label=back_label(t("action.back", lang)), description=description
        )

    async def pause(self, prompt: str | None = None) -> None:
        """Wait for acknowledgement without blocking the event loop."""

        if prompt is None:
            lang = self.context.settings.ui.language
            prompt = t("action.return", lang)
        await asyncio.to_thread(wait_for_enter, self.context.console.console, prompt=prompt)
