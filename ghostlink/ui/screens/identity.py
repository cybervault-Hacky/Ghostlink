"""Dedicated Identity Manager screen.

Displays the local cryptographic installation identity, public key fingerprint
(GLFP-...), display nickname, and allows safe management actions including
changing nickname, rotating the keypair, and viewing public verification cards.
Never exposes private cryptographic keys.
"""

from __future__ import annotations

import asyncio

from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.text import Text

from ghostlink.constants.files import STATE_DIR_NAME
from ghostlink.i18n import t
from ghostlink.identity.lifecycle import IdentityManager
from ghostlink.identity.storage import IdentityStore
from ghostlink.storage.manager import StorageManager
from ghostlink.ui.components.badges import BadgeTone, badge
from ghostlink.ui.components.dialogs import confirm, notice_dialog, prompt_text
from ghostlink.ui.components.notifications import NotificationLevel
from ghostlink.ui.components.panels import section_panel
from ghostlink.ui.components.tables import kv_grid
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.base import Screen, ScreenContext


class IdentityScreen(Screen):
    """Dedicated Identity Management interface."""

    def __init__(self, context: ScreenContext) -> None:
        super().__init__(context)
        self._menu = InteractiveMenu(context.console)

    def _manager(self) -> IdentityManager:
        state_dir = self.context.data_dir / STATE_DIR_NAME
        storage = StorageManager(state_dir)
        store = IdentityStore(storage)
        return IdentityManager(store)

    async def show(self) -> None:
        while True:
            lang = self.context.settings.ui.language
            manager = self._manager()
            identity = manager.ensure()
            fingerprint = manager.fingerprint(identity)
            nickname = identity.nickname or t("status.per_run_default", lang)

            self._render_header(identity.identity_id, nickname, fingerprint)

            entries = (
                MenuEntry(
                    key="nickname",
                    label=f"Change Display Nickname [{nickname}]",
                    description="Set custom nickname visible to room contacts",
                    icon="👤",
                ),
                MenuEntry(
                    key="view_fingerprint",
                    label="View Full Fingerprint Card",
                    description="Display the complete cryptographic verification card",
                    icon="🔑",
                ),
                MenuEntry(
                    key="rotate",
                    label="Rotate Cryptographic Identity",
                    description="Generate a fresh keypair (invalidates prior verifications)",
                    icon="↺",
                ),
                MenuEntry(
                    key="clear_nickname",
                    label="Clear Display Nickname",
                    description="Revert display name to per-run pseudonym",
                    icon="✖",
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
            if choice == "nickname":
                await self._change_nickname(manager)
            elif choice == "view_fingerprint":
                await self._view_card(identity.identity_id, nickname, fingerprint)
            elif choice == "rotate":
                await self._rotate_identity(manager)
            elif choice == "clear_nickname":
                await self._clear_nickname(manager)

    def _render_header(self, identity_id: str, nickname: str, fingerprint: str) -> None:
        context = self.context
        console = context.console
        theme = context.theme

        console.clear()
        console.newline()

        facts = kv_grid(
            [
                ("Status", badge("Protected", BadgeTone.SUCCESS, theme=theme)),
                ("Identity ID", Text(identity_id, style="gl.accent")),
                ("Display Nickname", Text(nickname, style="gl.highlight")),
                ("Public Fingerprint", Text(fingerprint, style="bold gl.accent")),
                ("Key Algorithm", Text("Ed25519 (Signing) + X25519 (Key Exchange)")),
                ("Key Storage", Text(f"{context.data_dir}/state (encrypted/isolated)")),
            ]
        )

        badge_bar = Align.center(
            badge("Local Cryptographic Identity", BadgeTone.ACCENT, theme=theme)
        )

        body = Group(
            badge_bar,
            Text(""),
            facts,
            Text(""),
            Text(
                "GhostLink identities are local-only and require no accounts or phone numbers.\n"
                "Private keys never leave this device.",
                style="gl.muted",
            ),
        )

        console.print(section_panel("Identity Manager", body, subtitle="Cryptographic Security"))
        console.newline()

    async def _change_nickname(self, manager: IdentityManager) -> None:
        console = self.context.console
        lang = self.context.settings.ui.language

        raw = await asyncio.to_thread(
            prompt_text,
            console.console,
            t("dialog.name_prompt", lang),
            allow_empty=False,
            max_length=24,
        )
        if raw is None:
            return

        name = raw.strip()
        try:
            manager.set_nickname(name)
            self.context.notifications.notify(
                f"✓ Display nickname updated to '{name}'",
                NotificationLevel.SUCCESS,
            )
        except Exception as exc:
            console.newline()
            console.print(notice_dialog("Invalid Nickname", str(exc), tone=BadgeTone.ERROR))
            await self.pause()

    async def _clear_nickname(self, manager: IdentityManager) -> None:
        lang = self.context.settings.ui.language
        manager.clear_nickname()
        self.context.notifications.notify(
            t("dialog.name_cleared", lang),
            NotificationLevel.INFO,
        )

    async def _view_card(self, identity_id: str, nickname: str, fingerprint: str) -> None:
        console = self.context.console
        theme = self.context.theme

        console.clear()
        console.newline()

        card_body = Group(
            Align.center(Text("GHOSTLINK VERIFIED IDENTITY", style="bold gl.title")),
            Text(""),
            Align.center(Text(f"Handle: {nickname} ({identity_id})", style="gl.text")),
            Text(""),
            Align.center(
                Panel(
                    Text(fingerprint, style=f"bold {theme.accent}", justify="center"),
                    title="[gl.title]Public Safety Fingerprint[/]",
                    border_style="gl.accent",
                    padding=(1, 3),
                    expand=False,
                )
            ),
            Text(""),
            Align.center(
                Text(
                    "Compare this fingerprint with your contacts out-of-band to verify integrity.",
                    style="gl.muted",
                )
            ),
        )

        console.print(section_panel("Identity Verification Card", card_body))
        await self.pause()

    async def _rotate_identity(self, manager: IdentityManager) -> None:
        console = self.context.console
        lang = self.context.settings.ui.language

        confirmed = await asyncio.to_thread(
            confirm,
            console.console,
            t("dialog.confirm_rotate_identity", lang),
            default=False,
        )
        if not confirmed:
            return

        try:
            manager.reset()
            self.context.notifications.notify(
                t("dialog.identity_rotated", lang),
                NotificationLevel.WARNING,
            )
        except Exception as exc:
            console.newline()
            console.print(notice_dialog("Rotation Failed", str(exc), tone=BadgeTone.ERROR))
            await self.pause()
