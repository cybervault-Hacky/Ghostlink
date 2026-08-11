"""Rooms screen — room management and QR invite generation."""

from __future__ import annotations

import asyncio

from rich.align import Align
from rich.text import Text

from ghostlink.constants.files import STATE_DIR_NAME
from ghostlink.i18n import t
from ghostlink.invites.lifecycle import SecureInviteManager
from ghostlink.invites.registry import LocalInviteRegistry
from ghostlink.storage.manager import StorageManager
from ghostlink.ui.components.badges import BadgeTone
from ghostlink.ui.components.dialogs import notice_dialog, prompt_text
from ghostlink.ui.components.tables import kv_grid
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.base import Screen, ScreenContext
from ghostlink.utils.qr import copy_to_clipboard, render_invite_qr_panel


class RoomManagementScreen(Screen):
    """Room management and QR invite interface."""

    def __init__(self, context: ScreenContext) -> None:
        super().__init__(context)
        self._menu = InteractiveMenu(context.console)

    def _invites(self) -> SecureInviteManager:
        state_dir = self.context.data_dir / STATE_DIR_NAME
        storage = StorageManager(state_dir)
        registry = LocalInviteRegistry(storage)
        return SecureInviteManager(registry)

    async def show(self) -> None:
        while True:
            self._render_overview()

            entries = (
                MenuEntry(
                    key="create",
                    label="Create Room & QR Invite",
                    description="Host a room and produce a link and QR code",
                ),
                MenuEntry(
                    key="qr_view",
                    label="Show QR for Invite Link",
                    description="Render a gl://join/ link as a terminal QR code",
                ),
                MenuEntry.spacer(),
                self.back_menu_entry(),
            )

            choice = self._menu.prompt(entries, default_key="back")
            if choice == "back" or choice is None:
                return
            if choice == "create":
                await self._create_room_with_qr()
            elif choice == "qr_view":
                await self._view_qr_prompt()

    def _render_overview(self) -> None:
        context = self.context
        console = context.console
        lang = context.settings.ui.language

        self.header(t("menu.rooms.label", lang), "Encrypted channels and invites")

        rooms = context.rooms.list_rooms()
        lifetime_min = context.settings.rooms.default_lifetime_minutes
        console.print(
            kv_grid(
                [
                    ("Hosted Rooms", Text(f"{len(rooms)} active")),
                    ("Default Lifetime", Text(f"{lifetime_min} minutes")),
                    ("Invite Mode", Text("Single-use cryptographic tokens (HMAC)")),
                    (
                        "Relay Routing",
                        Text(context.settings.relay.url or "Local rendezvous", style="gl.muted"),
                    ),
                ]
            )
        )
        console.newline()
        console.print(
            Text(
                "Rooms are ephemeral channels protected by X25519 session handshakes. "
                "Share the invite QR code directly with a contact.",
                style="gl.muted",
            )
        )
        console.newline()

    async def _display_invite_screen(
        self,
        invite_url: str,
        *,
        title: str = "ROOM INVITE",
        expires_minutes: int | None = None,
    ) -> None:
        """Display the QR invite screen with copy fallback and clean navigation."""
        console = self.context.console
        while True:
            console.clear()
            console.newline()
            qr_panel = render_invite_qr_panel(
                invite_url,
                title=title,
                subtitle="Scan this code with another device",
                expires_minutes=expires_minutes,
            )
            console.print(Align.center(qr_panel))
            console.newline()

            entries = (
                MenuEntry(
                    key="copy",
                    label="Copy Invite",
                    description="Copy the gl://join/… URI to clipboard",
                ),
                self.back_menu_entry("Return to room management"),
            )

            choice = self._menu.prompt(entries, default_key="back")
            if choice == "back" or choice is None:
                return
            if choice == "copy":
                copied = copy_to_clipboard(invite_url)
                console.newline()
                msg = f"Invite URI: {invite_url}"
                detail = f"Copied to clipboard.\n\n{msg}" if copied else msg
                console.print(
                    notice_dialog(
                        "Invite URI",
                        detail,
                        tone=BadgeTone.ACCENT,
                    )
                )
                await self.pause()

    async def _create_room_with_qr(self) -> None:
        console = self.context.console
        raw_name = await asyncio.to_thread(
            prompt_text,
            console.console,
            "Room name (Enter to skip)",
            allow_empty=True,
        )
        if raw_name is None:
            return

        try:
            room, invite = self.context.rooms.host_room(name=raw_name or None)
            invite_url = f"gl://join/{invite.token}"
            lifetime_min = self.context.settings.rooms.default_lifetime_minutes
            await self._display_invite_screen(
                invite_url,
                title=f"ROOM: {room.display_name.upper()}",
                expires_minutes=lifetime_min,
            )
        except Exception as exc:
            console.newline()
            console.print(notice_dialog("Could not create room", str(exc), tone=BadgeTone.ERROR))
            await self.pause()

    async def _view_qr_prompt(self) -> None:
        console = self.context.console
        raw_link = await asyncio.to_thread(
            prompt_text,
            console.console,
            "Paste invite link (gl://join/...)",
            allow_empty=False,
            max_length=256,
        )
        if raw_link is None:
            return

        link = raw_link.strip()
        await self._display_invite_screen(
            link,
            title="JOIN ROOM",
            expires_minutes=None,
        )
