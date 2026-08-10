"""Dedicated Storage Manager screen.

Calculates real filesystem usage across state, history, file transfers,
temp chunks, and logs, providing safe maintenance actions with confirmations.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
from pathlib import Path

from rich.align import Align
from rich.console import Group
from rich.table import Table
from rich.text import Text

from ghostlink.config.serializer import save_config_file
from ghostlink.i18n import t
from ghostlink.ui.components.badges import BadgeTone, badge
from ghostlink.ui.components.dialogs import confirm, notice_dialog, prompt_text
from ghostlink.ui.components.notifications import NotificationLevel
from ghostlink.ui.components.panels import section_panel
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
    """Dedicated Storage Manager interface."""

    def __init__(self, context: ScreenContext) -> None:
        super().__init__(context)
        self._menu = InteractiveMenu(context.console)

    async def show(self) -> None:
        while True:
            lang = self.context.settings.ui.language
            self._render_overview()

            entries = (
                MenuEntry(
                    key="clean_temp",
                    label="Clean Temporary In-Flight Chunks",
                    description="Safely delete orphan transfer chunk cache",
                    icon="🧹",
                ),
                MenuEntry(
                    key="clean_logs",
                    label="Trim Diagnostic Log Files",
                    description="Rotate and clear historical log entries",
                    icon="🗑",
                ),
                MenuEntry(
                    key="config_dir",
                    label="Configure Data Directory Path",
                    description="Set custom root path for all state and cache",
                    icon="📁",
                ),
                MenuEntry(
                    key="reset_dir",
                    label="Reset to Platform Default Directory",
                    description="Revert to standard OS data location",
                    icon="↺",
                ),
                MenuEntry(
                    key="back",
                    label=t("action.back", lang),
                    description="Return to the Main Menu",
                    icon="↩",
                ),
            )

            choice = self._menu.prompt(entries, default_key="back")
            if choice == "back":
                return
            if choice == "clean_temp":
                await self._clean_temp()
            elif choice == "clean_logs":
                await self._clean_logs()
            elif choice == "config_dir":
                await self._config_dir()
            elif choice == "reset_dir":
                await self._reset_dir()

    def _render_overview(self) -> None:
        context = self.context
        console = context.console
        theme = context.theme
        data_dir = context.data_dir

        console.clear()
        console.newline()

        state_size = _dir_size(data_dir / "state")
        logs_size = _dir_size(data_dir / "logs")
        history_size = _dir_size(data_dir / "history") + _dir_size(data_dir / "state" / "history")
        transfers_size = _dir_size(data_dir / "transfers")
        total_size = state_size + logs_size + history_size + transfers_size

        storage_table = Table(
            box=None,
            show_header=True,
            header_style="gl.title",
            pad_edge=False,
            expand=True,
        )
        storage_table.add_column("Storage Domain", style="gl.accent", width=24)
        storage_table.add_column("Filesystem Path", style="gl.text", width=38)
        storage_table.add_column("Size on Disk", style="gl.highlight")

        storage_table.add_row(
            "State & Key Storage",
            Text(f"{data_dir}/state", style="gl.muted"),
            Text(format_bytes(state_size)),
        )
        storage_table.add_row(
            "Encrypted History",
            Text(f"{data_dir}/history", style="gl.muted"),
            Text(format_bytes(history_size)),
        )
        storage_table.add_row(
            "In-Flight Transfers",
            Text(f"{data_dir}/transfers", style="gl.muted"),
            Text(format_bytes(transfers_size)),
        )
        storage_table.add_row(
            "Diagnostic Logs",
            Text(f"{data_dir}/logs", style="gl.muted"),
            Text(format_bytes(logs_size)),
        )
        storage_table.add_section()
        storage_table.add_row(
            "Total GhostLink Usage",
            Text(str(data_dir), style="bold gl.text"),
            Text(format_bytes(total_size), style="bold gl.accent"),
        )

        badge_header = Align.center(
            badge("Local Filesystem & Storage Manager", BadgeTone.ACCENT, theme=theme)
        )

        body = Group(
            badge_header,
            Text(""),
            storage_table,
            Text(""),
            Text(
                "GhostLink uses isolated local directories for encryption state and downloads.\n"
                "No temporary data is synced with external servers.",
                style="gl.muted",
            ),
        )

        console.print(section_panel("Storage Manager", body, subtitle="Disk Usage & Health"))
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
            f"✓ Cleaned {format_bytes(cleaned_bytes)} of temporary transfer cache",
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
            f"✓ Trimmed {format_bytes(cleaned)} of diagnostic logs",
            NotificationLevel.SUCCESS,
        )

    async def _config_dir(self) -> None:
        console = self.context.console
        lang = self.context.settings.ui.language

        raw = await asyncio.to_thread(
            prompt_text,
            console.console,
            t("dialog.datadir_prompt", lang),
            allow_empty=True,
            max_length=512,
        )
        if raw is None:
            return

        path_str = raw.strip()
        if path_str:
            target = Path(path_str).expanduser()
            if target.is_file():
                console.newline()
                console.print(
                    notice_dialog(
                        "Invalid Path",
                        f"'{target}' is an existing file.",
                        tone=BadgeTone.ERROR,
                    )
                )
                await self.pause()
                return

        updated = replace(
            self.context.settings,
            storage=replace(self.context.settings.storage, data_dir=path_str),
        )
        try:
            save_config_file(self.context.config_path, updated)
            self.context.settings = updated
            self.context.notifications.notify(
                f"✓ Data directory updated: {path_str or 'Default'}",
                NotificationLevel.SUCCESS,
            )
        except Exception as exc:
            console.newline()
            console.print(notice_dialog("Save Failed", str(exc), tone=BadgeTone.ERROR))
            await self.pause()

    async def _reset_dir(self) -> None:
        if not self.context.settings.storage.data_dir.strip():
            self.context.notifications.notify(
                "Already using platform default.", NotificationLevel.INFO
            )
            return

        updated = replace(
            self.context.settings,
            storage=replace(self.context.settings.storage, data_dir=""),
        )
        try:
            save_config_file(self.context.config_path, updated)
            self.context.settings = updated
            self.context.notifications.notify(
                "✓ Data directory reset to default", NotificationLevel.SUCCESS
            )
        except Exception as exc:
            self.context.console.newline()
            self.context.console.print(
                notice_dialog("Reset Failed", str(exc), tone=BadgeTone.ERROR)
            )
            await self.pause()
