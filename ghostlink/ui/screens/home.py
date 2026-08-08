"""Home screen — the primary navigation menu.

Presents the five destinations: Create Room, Join Room, Settings, About, and
Exit. Room actions are live: Create hosts a room, Join validates a room
identifier — and when a relay is configured, both drop straight into the
end-to-end encrypted chat (Phase 3).
"""

from __future__ import annotations

import asyncio
from functools import partial

from rich.align import Align
from rich.text import Text

from ghostlink.constants.app import APP_VERSION, RELEASE_LABEL
from ghostlink.constants.files import STATE_DIR_NAME
from ghostlink.exceptions.base import GhostLinkError
from ghostlink.exceptions.config import ConfigValidationError
from ghostlink.models.room import is_valid_room_id, normalize_room_id
from ghostlink.ui.banner import BannerRenderer
from ghostlink.ui.components.badges import BadgeTone, badge, badge_row
from ghostlink.ui.components.dialogs import confirm, notice_dialog, prompt_text
from ghostlink.ui.dashboards import render_join_result, render_room_dashboard
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.about import AboutScreen
from ghostlink.ui.screens.base import Screen, ScreenContext
from ghostlink.ui.screens.settings import SettingsScreen

MENU_ENTRIES: tuple[MenuEntry, ...] = (
    MenuEntry(
        key="create-room",
        label="Create Room",
        description="Host an encrypted one-to-one chat",
        icon="✚",
    ),
    MenuEntry(
        key="join-room",
        label="Join Room",
        description="Enter a friend's encrypted chat",
        icon="➤",
    ),
    MenuEntry(
        key="settings",
        label="Settings",
        description="View configuration",
        icon="⚙",
    ),
    MenuEntry(
        key="about",
        label="About",
        description="Version and build information",
        icon="◆",
    ),
    MenuEntry(
        key="exit",
        label="Exit",
        description="Close GhostLink",
        icon="✖",
    ),
)


class HomeScreen(Screen):
    """The main menu loop."""

    def __init__(self, context: ScreenContext) -> None:
        super().__init__(context)
        self._banner = BannerRenderer(context.console)
        self._menu = InteractiveMenu(context.console)

    async def show(self) -> None:
        first_render = True
        while True:
            self._render(hero=first_render)
            if first_render:
                self._flush_bootstrap_notices()
                first_render = False
            prompt = partial(self._menu.prompt, MENU_ENTRIES, default_key="exit")
            choice = await asyncio.to_thread(prompt)
            if choice == "exit":
                return
            await self._dispatch(choice)

    # ------------------------------------------------------------------ render

    def _render(self, *, hero: bool) -> None:
        context = self.context
        console = context.console
        console.clear()
        console.newline()
        if hero:
            status = badge_row(
                badge(f"v{APP_VERSION}", BadgeTone.ACCENT, theme=context.theme),
                badge(context.environment.platform_label, BadgeTone.INFO, theme=context.theme),
                badge(RELEASE_LABEL, BadgeTone.MUTED, theme=context.theme),
            )
            console.print(self._banner.hero(status))
        else:
            console.print(self._banner.compact_header(subtitle=RELEASE_LABEL))
            console.rule(style="gl.border")
        console.newline()
        console.print(Align.center(Text(self._menu.interaction_hint, style="gl.muted")))
        console.newline()

    def _flush_bootstrap_notices(self) -> None:
        context = self.context
        notices = context.drain_deferred()
        if not notices:
            return
        context.console.newline()
        for level, message in notices:
            context.notifications.notify(message, level)

    # ---------------------------------------------------------------- dispatch

    async def _dispatch(self, choice: str) -> None:
        if choice == "settings":
            await SettingsScreen(self.context).show()
        elif choice == "about":
            await AboutScreen(self.context).show()
        elif choice == "create-room":
            await self._create_room_flow()
        elif choice == "join-room":
            await self._join_room_flow()

    async def _create_room_flow(self) -> None:
        context = self.context
        console = context.console
        name = await asyncio.to_thread(
            prompt_text,
            console.console,
            "Room name (Enter to skip)",
            allow_empty=True,
        )
        if name is None:
            return
        try:
            room, invite = context.rooms.host_room(name=name or None)
        except ConfigValidationError as exc:
            console.newline()
            console.print(notice_dialog("Could not host room", exc.message, hint=exc.hint))
            await self.pause()
            return
        relay_url = self._relay_url()
        render_room_dashboard(console, room, invite, relay_note=self._relay_note())
        if relay_url is None:
            await self.pause()
            return
        if not await self._confirm_chat(
            "Share the room ID, then press Enter — the chat opens in this screen."
        ):
            return
        await self._enter_chat(role="host", channel=room.room_id, relay_url=relay_url)

    def _relay_note(self) -> str:
        configured = self._relay_url()
        if configured:
            return f"Relay configured: {configured} — you will enter the chat next"
        return "No relay configured — room is recorded locally."

    def _relay_url(self) -> str | None:
        configured = self.context.settings.relay.url.strip()
        return configured or None

    async def _confirm_chat(self, message: str) -> bool:
        console = self.context.console
        console.newline()
        console.print(Text(message, style="gl.muted"))
        return await asyncio.to_thread(
            confirm, console.console, "Enter the chat now?", default=True
        )

    async def _enter_chat(self, *, role: str, channel: str, relay_url: str) -> None:
        """Drop the whole menu screen into the live secure chat."""

        context = self.context
        from ghostlink.transport.relay.client import RelayClientConfig
        from ghostlink.ui.chat import run_chat_session

        relay = context.settings.relay
        try:
            await run_chat_session(
                context.console,
                settings=context.settings,
                state_dir=context.data_dir / STATE_DIR_NAME,
                role=role,
                channel=channel,
                relay_url=relay_url,
                client_config=RelayClientConfig(
                    connect_timeout_seconds=relay.connect_timeout_seconds,
                    handshake_timeout_seconds=relay.handshake_timeout_seconds,
                    heartbeat_interval_seconds=relay.heartbeat_interval_seconds,
                    heartbeat_timeout_seconds=relay.heartbeat_timeout_seconds,
                    reconnect_attempts=relay.reconnect_attempts,
                    reconnect_base_delay_seconds=relay.reconnect_base_delay_seconds,
                ),
            )
        except GhostLinkError as exc:
            context.console.newline()
            context.console.print(
                notice_dialog("Could not open the chat", exc.message, hint=exc.hint)
            )
        await self.pause()

    async def _join_room_flow(self) -> None:
        context = self.context
        console = context.console
        candidate = await asyncio.to_thread(
            prompt_text, console.console, "Room identifier", allow_empty=False
        )
        if candidate is None:
            return
        canonical = normalize_room_id(candidate)
        valid = is_valid_room_id(candidate) or is_valid_room_id(canonical)
        render_join_result(
            console,
            candidate=candidate,
            valid=valid,
            canonical=canonical if valid else None,
        )
        if not valid:
            await self.pause()
            return
        relay_url = self._relay_url()
        if relay_url is None:
            console.newline()
            console.print(
                notice_dialog(
                    "Relay required for chat",
                    "The room identifier is valid, but the live chat needs a relay endpoint.",
                    hint="Set url under [relay] in the configuration file.",
                )
            )
            await self.pause()
            return
        room_id = candidate if is_valid_room_id(candidate) else canonical
        if not await self._confirm_chat("The chat opens in this screen."):
            return
        await self._enter_chat(role="guest", channel=room_id, relay_url=relay_url)
