"""Transfers screen — encrypted file transfer pipeline configuration."""

from __future__ import annotations

import asyncio
from dataclasses import replace

from rich.text import Text

from ghostlink.config.serializer import save_config_file
from ghostlink.i18n import t
from ghostlink.ui.components.badges import BadgeTone
from ghostlink.ui.components.dialogs import notice_dialog, prompt_text
from ghostlink.ui.components.notifications import NotificationLevel
from ghostlink.ui.components.tables import kv_grid
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.base import Screen, ScreenContext
from ghostlink.utils.paths import default_download_dir


class TransferDashboardScreen(Screen):
    """Encrypted file transfer settings and pipeline overview."""

    def __init__(self, context: ScreenContext) -> None:
        super().__init__(context)
        self._menu = InteractiveMenu(context.console)

    async def show(self) -> None:
        while True:
            self._render_overview()

            transfer = self.context.settings.transfer
            entries = (
                MenuEntry(
                    key="download_dir",
                    label="Download Directory",
                    description="Where received encrypted files are saved",
                    value=transfer.download_dir.strip() or "Default",
                ),
                MenuEntry(
                    key="max_size",
                    label="Max File Size",
                    description="Largest accepted payload, in megabytes",
                    value=f"{transfer.max_file_size_mb} MB",
                ),
                MenuEntry.spacer(),
                self.back_menu_entry(),
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
        lang = context.settings.ui.language
        transfer = context.settings.transfer

        self.header(t("menu.transfers.label", lang), "Encrypted file pipeline")

        download_display = (
            transfer.download_dir.strip()
            if transfer.download_dir.strip()
            else f"Default ({default_download_dir()})"
        )

        facts = kv_grid(
            [
                ("Download Folder", Text(download_display, style="gl.highlight")),
                ("Max File Size", Text(f"{transfer.max_file_size_mb} MB")),
                ("Max Concurrent", Text(f"{transfer.max_concurrent_transfers} parallel")),
                ("Chunk Size", Text(f"{transfer.chunk_size_kb} KiB")),
                ("Integrity", Text("SHA-256 per-chunk and file verification")),
                ("ACK Timeout", Text(f"{transfer.ack_timeout_seconds}s")),
                ("Temp Buffer Limit", Text(f"{transfer.temp_storage_limit_mb} MB")),
            ]
        )
        console.print(facts)
        console.newline()
        console.print(
            Text(
                "Files transfer chunk-by-chunk inside sealed ChaCha20-Poly1305 frames. "
                "Recipients approve before any bytes are transmitted.",
                style="gl.muted",
            )
        )
        console.newline()

    async def _change_download_dir(self) -> None:
        console = self.context.console

        raw = await asyncio.to_thread(
            prompt_text,
            console.console,
            "Download directory path (Enter for default)",
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
                f"Download directory: {path_str or 'Default'}",
                NotificationLevel.SUCCESS,
            )
        except Exception as exc:
            console.newline()
            console.print(notice_dialog("Save failed", str(exc), tone=BadgeTone.ERROR))
            await self.pause()

    async def _change_max_size(self) -> None:
        console = self.context.console

        raw = await asyncio.to_thread(
            prompt_text,
            console.console,
            "Maximum file size in MB (1 - 4096)",
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
                f"Max file size: {val} MB",
                NotificationLevel.SUCCESS,
            )
        except Exception as exc:
            console.newline()
            console.print(notice_dialog("Invalid limit", str(exc), tone=BadgeTone.ERROR))
            await self.pause()
