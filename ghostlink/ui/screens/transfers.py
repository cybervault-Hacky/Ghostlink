"""Dedicated File Transfer Dashboard screen.

Provides configuration for end-to-end encrypted file transfers, download paths,
storage boundaries, concurrency limits, and integrity verification metrics.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from rich.align import Align
from rich.console import Group
from rich.text import Text

from ghostlink.config.serializer import save_config_file
from ghostlink.i18n import t
from ghostlink.ui.components.badges import BadgeTone, badge
from ghostlink.ui.components.dialogs import notice_dialog, prompt_text
from ghostlink.ui.components.notifications import NotificationLevel
from ghostlink.ui.components.panels import section_panel
from ghostlink.ui.components.tables import kv_grid
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.base import Screen, ScreenContext
from ghostlink.utils.paths import default_download_dir


class TransferDashboardScreen(Screen):
    """Dedicated File Transfer Management interface."""

    def __init__(self, context: ScreenContext) -> None:
        super().__init__(context)
        self._menu = InteractiveMenu(context.console)

    async def show(self) -> None:
        while True:
            lang = self.context.settings.ui.language
            self._render_overview()

            entries = (
                MenuEntry(
                    key="download_dir",
                    label="Change Download Directory",
                    description="Configure where received encrypted files are saved",
                    icon="📁",
                ),
                MenuEntry(
                    key="max_size",
                    label="Change Max File Size Limit",
                    description="Set highest acceptable payload size in megabytes",
                    icon="📏",
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
            if choice == "download_dir":
                await self._change_download_dir()
            elif choice == "max_size":
                await self._change_max_size()

    def _render_overview(self) -> None:
        context = self.context
        console = context.console
        theme = context.theme
        transfer = context.settings.transfer

        console.clear()
        console.newline()

        download_display = (
            transfer.download_dir.strip()
            if transfer.download_dir.strip()
            else f"Default ({default_download_dir()})"
        )

        ack_str = f"{transfer.ack_timeout_seconds}s (limit: {transfer.retry_limit})"
        facts = kv_grid(
            [
                ("Status", badge("Active", BadgeTone.SUCCESS, theme=theme)),
                ("Download Folder", Text(download_display, style="gl.highlight")),
                ("Max File Size", Text(f"{transfer.max_file_size_mb} MB")),
                ("Max Concurrent", Text(f"{transfer.max_concurrent_transfers} parallel")),
                ("Chunk Protocol", Text(f"GF1 · {transfer.chunk_size_kb} KiB per frame")),
                ("Integrity Cipher", Text("SHA-256 Per-Chunk & File Verification")),
                ("ACK Timeout", Text(ack_str)),
                ("Temp Buffer Limit", Text(f"{transfer.temp_storage_limit_mb} MB max")),
            ]
        )

        badge_header = Align.center(
            badge("Encrypted File Transfer Subsystem", BadgeTone.ACCENT, theme=theme)
        )

        body = Group(
            badge_header,
            Text(""),
            facts,
            Text(""),
            Text(
                "Files transfer chunk-by-chunk inside sealed ChaCha20-Poly1305 frames.\n"
                "Recipients must explicitly approve before any bytes are transmitted.",
                style="gl.muted",
            ),
        )

        console.print(section_panel("File Transfers", body, subtitle="Encrypted Pipeline"))
        console.newline()

    async def _change_download_dir(self) -> None:
        console = self.context.console

        raw = await asyncio.to_thread(
            prompt_text,
            console.console,
            "Enter download directory (Enter for default)",
            allow_empty=True,
            max_length=512,
        )
        if raw is None:
            return

        path_str = raw.strip()
        updated = replace(
            self.context.settings,
            transfer=replace(self.context.settings.transfer, download_dir=path_str),
        )
        try:
            save_config_file(self.context.config_path, updated)
            self.context.settings = updated
            self.context.notifications.notify(
                f"✓ Download directory set to '{path_str or 'Default'}'",
                NotificationLevel.SUCCESS,
            )
        except Exception as exc:
            console.newline()
            console.print(notice_dialog("Save Failed", str(exc), tone=BadgeTone.ERROR))
            await self.pause()

    async def _change_max_size(self) -> None:
        console = self.context.console

        raw = await asyncio.to_thread(
            prompt_text,
            console.console,
            "Enter maximum file size in MB (1 - 4096)",
            allow_empty=False,
            max_length=8,
        )
        if raw is None:
            return

        try:
            val = int(raw.strip())
            if not 1 <= val <= 4096:
                raise ValueError("Value must be between 1 and 4096 MB")
            updated = replace(
                self.context.settings,
                transfer=replace(self.context.settings.transfer, max_file_size_mb=val),
            )
            save_config_file(self.context.config_path, updated)
            self.context.settings = updated
            self.context.notifications.notify(
                f"✓ Max file size limit set to {val} MB",
                NotificationLevel.SUCCESS,
            )
        except Exception as exc:
            console.newline()
            console.print(notice_dialog("Invalid Limit", str(exc), tone=BadgeTone.ERROR))
            await self.pause()
