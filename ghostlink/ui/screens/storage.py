"""Storage screen — disk usage overview and safe cleanup actions.

Calculates real filesystem usage across state, history, file transfers, and
logs, and provides maintenance actions gated behind calm confirmations.
"""

from __future__ import annotations

import asyncio
import contextlib
from pathlib import Path

from rich.text import Text

from ghostlink.i18n import t
from ghostlink.ui.components.dialogs import confirm
from ghostlink.ui.components.notifications import NotificationLevel
from ghostlink.ui.components.tables import kv_grid
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.base import Screen, ScreenContext
from ghostlink.utils.text import format_bytes


def _dir_size(path: Path) -> int:
    """Calculate total size of all files under path in bytes."""
    total = 0
    if not path.exists():
        return 0
    if path.is_file():
        with contextlib.suppress(OSError):
            return path.stat().st_size
        return 0
    with contextlib.suppress(OSError):
        for item in path.rglob("*"):
            if item.is_file():
                with contextlib.suppress(OSError):
                    total += item.stat().st_size
    return total


class StorageManagerScreen(Screen):
    """Storage usage and maintenance interface."""

    def __init__(self, context: ScreenContext) -> None:
        super().__init__(context)
        self._menu = InteractiveMenu(context.console)

    async def show(self) -> None:
        while True:
            self._render_overview()

            entries = (
                MenuEntry(
                    key="clean_temp",
                    label="Clean Temporary Chunks",
                    description="Delete orphaned in-flight transfer cache",
                ),
                MenuEntry(
                    key="clean_logs",
                    label="Trim Diagnostic Logs",
                    description="Clear historical log file contents",
                ),
                MenuEntry.spacer(),
                self.back_menu_entry(),
            )

            choice = self._menu.prompt(entries, default_key="back")
            if choice == "back":
                return
            if choice == "clean_temp":
                await self._clean_temp()
            elif choice == "clean_logs":
                await self._clean_logs()

    def _render_overview(self) -> None:
        context = self.context
        console = context.console
        lang = context.settings.ui.language
        data_dir = context.data_dir

        self.header(t("menu.storage.label", lang), "Local disk usage")

        state_size = _dir_size(data_dir / "state")
        logs_size = _dir_size(data_dir / "logs")
        history_size = _dir_size(data_dir / "history") + _dir_size(data_dir / "state" / "history")
        transfers_size = _dir_size(data_dir / "transfers")
        total_size = state_size + logs_size + history_size + transfers_size

        console.print(
            kv_grid(
                [
                    ("State & Key Storage", Text(format_bytes(state_size))),
                    ("Encrypted History", Text(format_bytes(history_size))),
                    ("In-Flight Transfers", Text(format_bytes(transfers_size))),
                    ("Diagnostic Logs", Text(format_bytes(logs_size))),
                    ("Total", Text(format_bytes(total_size), style="gl.highlight")),
                ]
            )
        )
        console.newline()
        console.print(Text(f"Root — {data_dir}", style="gl.muted"))
        console.print(
            Text(
                "Directories are local and isolated; nothing is synced to external servers.",
                style="gl.muted",
            )
        )
        console.newline()

    async def _clean_temp(self) -> None:
        console = self.context.console
        transfers_dir = self.context.data_dir / "transfers"
        confirmed = await asyncio.to_thread(
            confirm,
            console.console,
            "Delete incomplete transfer chunk files and temp cache?",
            default=True,
        )
        if not confirmed:
            return

        cleaned_bytes = 0
        if transfers_dir.exists():
            for item in transfers_dir.glob("*"):
                with contextlib.suppress(OSError):
                    if item.is_file():
                        cleaned_bytes += item.stat().st_size
                        item.unlink()

        self.context.notifications.notify(
            f"Cleaned {format_bytes(cleaned_bytes)} of temporary transfer cache",
            NotificationLevel.SUCCESS,
        )

    async def _clean_logs(self) -> None:
        console = self.context.console
        logs_dir = self.context.data_dir / "logs"
        confirmed = await asyncio.to_thread(
            confirm,
            console.console,
            "Clear diagnostic log files?",
            default=True,
        )
        if not confirmed:
            return

        cleaned = 0
        if logs_dir.exists():
            for log_file in logs_dir.glob("*.log*"):
                with contextlib.suppress(OSError):
                    cleaned += log_file.stat().st_size
                    log_file.write_text("", encoding="utf-8")

        self.context.notifications.notify(
            f"Trimmed {format_bytes(cleaned)} of diagnostic logs",
            NotificationLevel.SUCCESS,
        )
