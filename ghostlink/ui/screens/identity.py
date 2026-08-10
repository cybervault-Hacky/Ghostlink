"""Identity screen — the local cryptographic installation identity.

Displays the local identity, public key fingerprint (GLFP-...), and display
nickname, and allows safe management actions (change/clear nickname, rotate
the keypair, view the verification card). Never exposes private keys.
"""

from __future__ import annotations

import asyncio

from rich.align import Align
from rich.console import Group
from rich.text import Text

from ghostlink.constants.files import STATE_DIR_NAME
from ghostlink.i18n import t
from ghostlink.identity.lifecycle import IdentityManager
from ghostlink.identity.storage import IdentityStore
from ghostlink.storage.manager import StorageManager
from ghostlink.ui.components.badges import BadgeTone
from ghostlink.ui.components.dialogs import confirm_action, notice_dialog, prompt_text
from ghostlink.ui.components.layout import status_text
from ghostlink.ui.components.notifications import NotificationLevel
from ghostlink.ui.components.tables import kv_grid
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.base import Screen, ScreenContext


class IdentityScreen(Screen):
    """Identity management interface."""

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

            self._render_overview(identity.identity_id, nickname, fingerprint)

            entries = (
                MenuEntry(
                    key="nickname",
                    label="Display Name",
                    description="Nickname visible to room contacts",
                    value=nickname,
                ),
                MenuEntry(
                    key="view_fingerprint",
                    label="Verification Card",
                    description="Full fingerprint card for out-of-band verification",
                ),
                MenuEntry(
                    key="rotate",
                    label="Rotate Identity Keys",
                    description="Generate a fresh keypair (peers must re-verify)",
                ),
                MenuEntry(
                    key="clear_nickname",
                    label="Clear Display Name",
                    description="Revert to the per-run pseudonym",
                ),
                MenuEntry.spacer(),
                self.back_menu_entry(),
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

    def _render_overview(self, identity_id: str, nickname: str, fingerprint: str) -> None:
        context = self.context
        lang = context.settings.ui.language
        self.header(t("menu.identity.label", lang), "Local cryptographic identity")

        facts = kv_grid(
            [
                ("Status", status_text("Protected — keys stored locally", "success")),
                ("Identity ID", Text(identity_id, style="gl.accent")),
                ("Display Name", Text(nickname, style="gl.highlight")),
                ("Public Fingerprint", Text(fingerprint, style="bold gl.accent")),
                ("Key Algorithm", Text("Ed25519 (signing) + X25519 (key exchange)")),
                ("Key Storage", Text(f"{context.data_dir}/state (isolated)", style="gl.muted")),
            ]
        )
        context.console.print(facts)
        context.console.newline()
        context.console.print(
            Text(
                "Identities are local-only — no accounts, no phone numbers. "
                "Private keys never leave this device.",
                style="gl.muted",
            )
        )
        context.console.newline()

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
                f"Display name updated to '{name}'",
                NotificationLevel.SUCCESS,
            )
        except Exception as exc:
            console.newline()
            console.print(notice_dialog("Invalid nickname", str(exc), tone=BadgeTone.ERROR))
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

        self.header("Verification Card", "Compare out-of-band with your contact")

        card = Group(
            Align.center(Text("GHOSTLINK IDENTITY", style="bold gl.title")),
            Text(""),
            Align.center(Text(f"{nickname}  ·  {identity_id}", style="gl.text")),
            Text(""),
            Align.center(Text(fingerprint, style="bold gl.accent")),
            Text(""),
            Align.center(
                Text(
                    "If this fingerprint matches on both devices, "
                    "the end-to-end channel is verified.",
                    style="gl.muted",
                )
            ),
        )
        console.print(card)
        await self.pause()

    async def _rotate_identity(self, manager: IdentityManager) -> None:
        console = self.context.console
        lang = self.context.settings.ui.language

        confirmed = await asyncio.to_thread(
            confirm_action,
            console.console,
            t("dialog.confirm_rotate_title", lang),
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
            console.print(notice_dialog("Rotation failed", str(exc), tone=BadgeTone.ERROR))
            await self.pause()
