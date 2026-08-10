"""First-run onboarding — a professional setup wizard.

One step per screen, in a fixed order: Identity → Theme → Language → Privacy.
Fully skippable; every step writes through the same persistence path as the
Settings screen.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from rich.text import Text

from ghostlink.config.serializer import save_config_file
from ghostlink.constants.app import BUILTIN_THEMES, LANGUAGE_NAMES, SUPPORTED_LANGUAGES
from ghostlink.constants.files import STATE_DIR_NAME
from ghostlink.i18n import set_current_language, t
from ghostlink.identity.fingerprint import identity_fingerprint
from ghostlink.identity.lifecycle import IdentityManager
from ghostlink.identity.storage import IdentityStore
from ghostlink.storage.manager import StorageManager
from ghostlink.ui.components.dialogs import confirm, prompt_text
from ghostlink.ui.components.notifications import NotificationLevel
from ghostlink.ui.components.tables import kv_grid
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.base import Screen, ScreenContext
from ghostlink.ui.themes import ThemeEngine

_TOTAL_STEPS = 4


class OnboardingWizard(Screen):
    """First-run setup wizard for new GhostLink installations."""

    def __init__(self, context: ScreenContext) -> None:
        super().__init__(context)
        self._menu = InteractiveMenu(context.console)

    async def show(self) -> None:
        console = self.context.console

        self.header("Welcome to GhostLink", "Secure communication for your terminal")
        console.print(
            Text(
                "This short setup configures your identity, appearance, and privacy "
                "defaults. You can change everything later in Settings.",
                style="gl.text",
            )
        )
        console.newline()

        start_choices = (
            MenuEntry(
                key="start",
                label="Continue",
                description="Walk through the four setup steps",
            ),
            MenuEntry(
                key="skip",
                label="Skip Setup",
                description="Use recommended defaults",
            ),
        )
        choice = self._menu.prompt(start_choices, default_key="start")
        if choice == "skip":
            await self._finish()
            return

        await self._step_identity()
        await self._step_theme()
        await self._step_language()
        await self._step_privacy()
        await self._finish()

    def _step_header(self, step: int, title: str, description: str) -> None:
        self.header(title, f"Step {step} of {_TOTAL_STEPS}")
        self.context.console.print(Text(description, style="gl.text"))
        self.context.console.newline()

    async def _step_identity(self) -> None:
        console = self.context.console
        state_dir = self.context.data_dir / STATE_DIR_NAME
        storage = StorageManager(state_dir)
        mgr = IdentityManager(IdentityStore(storage))
        ident = mgr.ensure()
        fp = identity_fingerprint(ident.public_key_bytes)

        self._step_header(1, "Identity", "Your local GhostLink identity is ready.")
        console.print(
            kv_grid(
                [
                    ("Public Fingerprint", Text(fp, style="gl.accent")),
                    ("Display Name", Text("optional — shown to room contacts", style="gl.muted")),
                ]
            )
        )
        console.newline()

        raw = await asyncio.to_thread(
            prompt_text,
            console.console,
            "Display name (Enter to skip)",
            allow_empty=True,
            max_length=24,
        )
        if raw and raw.strip():
            name = raw.strip()
            mgr.set_nickname(name)
            self.context.settings = replace(
                self.context.settings,
                chat=replace(self.context.settings.chat, display_name=name),
            )

    async def _step_theme(self) -> None:
        engine = ThemeEngine()
        current = self.context.settings.ui.theme

        self._step_header(2, "Theme", "Choose a color theme for the terminal.")

        entries = []
        for name in BUILTIN_THEMES:
            entries.append(
                MenuEntry(
                    key=name,
                    label=name.title(),
                    description=engine.get(name).description,
                    value="current" if name == current else "",
                )
            )
        entries.append(MenuEntry.spacer())
        entries.append(
            MenuEntry(key="keep", label="Keep Default (Phantom)", description="Continue unchanged")
        )

        chosen = self._menu.prompt(entries, default_key="keep")
        if chosen != "keep" and chosen in BUILTIN_THEMES:
            spec = engine.get(chosen)
            self.context.console.set_theme(spec)
            self.context.settings = replace(
                self.context.settings,
                ui=replace(self.context.settings.ui, theme=chosen),
            )

    async def _step_language(self) -> None:
        current = self.context.settings.ui.language
        self._step_header(3, "Language", "Select the interface language.")

        entries = []
        for code in sorted(SUPPORTED_LANGUAGES.keys()):
            entries.append(
                MenuEntry(
                    key=code,
                    label=LANGUAGE_NAMES.get(code, SUPPORTED_LANGUAGES[code]),
                    description=f"Language code: {code}",
                    value="current" if code == current else "",
                )
            )

        chosen = self._menu.prompt(entries, default_key=current)
        if chosen in SUPPORTED_LANGUAGES:
            set_current_language(chosen)
            self.context.settings = replace(
                self.context.settings,
                ui=replace(self.context.settings.ui, language=chosen),
            )

    async def _step_privacy(self) -> None:
        console = self.context.console
        self._step_header(4, "Privacy", "Set the recommended communication defaults.")

        enable_receipts = await asyncio.to_thread(
            confirm, console.console, "Send read receipts (peers see when you read)?", default=True
        )
        enable_typing = await asyncio.to_thread(
            confirm, console.console, "Send typing indicators to room peers?", default=True
        )

        self.context.settings = replace(
            self.context.settings,
            chat=replace(
                self.context.settings.chat,
                read_receipts=enable_receipts,
                typing_indicators=enable_typing,
            ),
        )

    async def _finish(self) -> None:
        updated = replace(
            self.context.settings,
            meta=replace(self.context.settings.meta, onboarding_completed=True),
        )
        try:
            save_config_file(self.context.config_path, updated)
            self.context.settings = updated
        except Exception:
            pass

        done = t("dialog.onboarding_done", self.context.settings.ui.language)
        self.context.notifications.notify(done, NotificationLevel.SUCCESS)
