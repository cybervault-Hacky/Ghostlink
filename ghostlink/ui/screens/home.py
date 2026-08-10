"""Home screen — the primary navigation menu.

Presents the five destinations: Create Room, Join Room, Settings, About, and
Exit. Room actions are live: Create hosts a room, Join validates a room
identifier — and when a relay is configured, both drop straight into the
end-to-end encrypted chat (Phase 3).
"""

from __future__ import annotations

import asyncio

from rich.align import Align
from rich.text import Text

from ghostlink.constants.app import APP_VERSION
from ghostlink.constants.files import STATE_DIR_NAME
from ghostlink.exceptions.base import GhostLinkError
from ghostlink.exceptions.config import ConfigValidationError
from ghostlink.i18n import t
from ghostlink.models.room import is_valid_room_id, normalize_room_id
from ghostlink.ui.banner import BannerRenderer
from ghostlink.ui.components.badges import BadgeTone, badge, badge_row
from ghostlink.ui.components.credits import creator_credits
from ghostlink.ui.components.dialogs import confirm, notice_dialog, prompt_text
from ghostlink.ui.dashboards import render_join_result, render_room_dashboard
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.about import AboutScreen
from ghostlink.ui.screens.base import Screen, ScreenContext
from ghostlink.ui.screens.help import HelpScreen
from ghostlink.ui.screens.identity import IdentityScreen
from ghostlink.ui.screens.security import SecurityDashboardScreen
from ghostlink.ui.screens.settings import SettingsScreen
from ghostlink.ui.screens.transfers import TransferDashboardScreen


def get_menu_entries(lang: str = "en") -> tuple[MenuEntry, ...]:
    """Build localized main menu entries."""

    return (
        MenuEntry(
            key="create-room",
            label=t("menu.create_room.label", lang),
            description=t("menu.create_room.desc", lang),
            icon="✚",
        ),
        MenuEntry(
            key="join-room",
            label=t("menu.join_room.label", lang),
            description=t("menu.join_room.desc", lang),
            icon="➤",
        ),
        MenuEntry(
            key="transfers",
            label="File Transfers",
            description="Inspect transfer pipeline, speeds & downloads",
            icon="📎",
        ),
        MenuEntry(
            key="security",
            label="Security",
            description="Verified cryptographic posture & audit details",
            icon="🛡",
        ),
        MenuEntry(
            key="identity",
            label="Identity",
            description="Cryptographic fingerprint & display nickname",
            icon="🔑",
        ),
        MenuEntry(
            key="settings",
            label=t("menu.settings.label", lang),
            description=t("menu.settings.desc", lang),
            icon="⚙",
        ),
        MenuEntry(
            key="help",
            label="Help & Shortcuts",
            description="Interactive commands and keyboard reference",
            icon="❓",
        ),
        MenuEntry(
            key="about",
            label=t("menu.about.label", lang),
            description=t("menu.about.desc", lang),
            icon="◆",
        ),
        MenuEntry(
            key="exit",
            label=t("menu.exit.label", lang),
            description=t("menu.exit.desc", lang),
            icon="✖",
        ),
    )


MENU_ENTRIES: tuple[MenuEntry, ...] = get_menu_entries("en")


class HomeScreen(Screen):
    """The main menu loop."""

    def __init__(self, context: ScreenContext) -> None:
        super().__init__(context)
        self._banner = BannerRenderer(context.console)
        self._menu = InteractiveMenu(context.console)

    async def show(self) -> None:
        first_render = True
        while True:
            lang = self.context.settings.ui.language
            self._render(hero=first_render)
            if first_render:
                self._flush_bootstrap_notices()
                first_render = False
            # simple-term-menu registers a SIGWINCH handler during ``show()``,
            # and CPython only permits signal handlers to be installed from
            # the main interpreter thread (the crash seen on Termux/Python 3.11
            # was "signal only works in main thread of the main interpreter").
            # ``show`` is a coroutine driven by ``asyncio.run`` on the main
            # thread, so a direct synchronous call keeps the menu on that
            # thread while it blocks for input. Do NOT move this prompt into a
            # worker/executor thread — see ghostlink.ui.menu for the guard.
            entries = get_menu_entries(lang)
            choice = self._menu.prompt(entries, default_key="exit")
            if choice == "exit":
                return
            await self._dispatch(choice)

    # ------------------------------------------------------------------ render

    def _render(self, *, hero: bool) -> None:
        context = self.context
        console = context.console
        lang = context.settings.ui.language
        console.clear()
        console.newline()
        if hero:
            status = badge_row(
                badge(f"v{APP_VERSION}", BadgeTone.ACCENT, theme=context.theme),
                badge(context.environment.platform_label, BadgeTone.INFO, theme=context.theme),
            )
            console.print(self._banner.hero(status))
        else:
            console.print(self._banner.compact_header())
            console.rule(style="gl.border")
        if hero:
            # Creator credits sit subtly below the version/status badges and
            # above the navigation hint, only on the full startup screen.
            console.print(creator_credits())
        console.newline()
        hint = (
            t("menu.hint.keyboard", lang)
            if self._menu.keyboard_driven
            else t("menu.hint.numbered", lang)
        )
        console.print(Align.center(Text(hint, style="gl.muted")))
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
        elif choice == "identity":
            await IdentityScreen(self.context).show()
        elif choice == "security":
            await SecurityDashboardScreen(self.context).show()
        elif choice == "transfers":
            await TransferDashboardScreen(self.context).show()
        elif choice == "help":
            await HelpScreen(self.context).show()
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
