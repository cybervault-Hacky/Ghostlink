"""First-Run Onboarding Wizard.

Guides users through initial identity creation, theme selection, language
preferences, and privacy settings on their first launch. Fully skippable.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

from rich.align import Align
from rich.console import Group
from rich.text import Text

from ghostlink.config.serializer import save_config_file
from ghostlink.constants.app import BUILTIN_THEMES, LANGUAGE_NAMES, SUPPORTED_LANGUAGES
from ghostlink.constants.files import STATE_DIR_NAME
from ghostlink.i18n import set_current_language
from ghostlink.identity.fingerprint import identity_fingerprint
from ghostlink.identity.lifecycle import IdentityManager
from ghostlink.identity.storage import IdentityStore
from ghostlink.storage.manager import StorageManager
from ghostlink.ui.components.badges import BadgeTone, badge
from ghostlink.ui.components.dialogs import confirm, prompt_text
from ghostlink.ui.components.notifications import NotificationLevel
from ghostlink.ui.components.panels import section_panel
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.base import Screen, ScreenContext
from ghostlink.ui.themes import ThemeEngine


class OnboardingWizard(Screen):
    """First-run setup wizard for new GhostLink installations."""

    def __init__(self, context: ScreenContext) -> None:
        super().__init__(context)
        self._menu = InteractiveMenu(context.console)

    async def show(self) -> None:
        console = self.context.console
        theme = self.context.theme

        console.clear()
        console.newline()

        welcome_body = Group(
            Align.center(Text("Welcome to GhostLink", style="bold gl.title")),
            Text(""),
            Align.center(
                Text(
                    "Private, terminal-only encrypted messaging designed for Termux and Linux.\n"
                    "Let's configure your initial preferences in a few quick steps.",
                    style="gl.text",
                    justify="center",
                )
            ),
            Text(""),
            Align.center(badge("Zero Knowledge · Zero Compromise", BadgeTone.ACCENT, theme=theme)),
        )

        console.print(section_panel("Setup Wizard", welcome_body))
        console.newline()

        start_choices = (
            MenuEntry(
                key="start",
                label="Begin Quick Setup",
                description="Configure nickname, theme, and privacy",
                icon="▶",
            ),
            MenuEntry(
                key="skip",
                label="Skip Setup",
                description="Use recommended defaults immediately",
                icon="⏭",
            ),
        )
        choice = self._menu.prompt(start_choices, default_key="start")
        if choice == "skip":
            await self._finish()
            return

        # 1. Identity & Nickname
        await self._step_nickname()

        # 2. Theme Selection
        await self._step_theme()

        # 3. Language Selection
        await self._step_language()

        # 4. Privacy Configuration
        await self._step_privacy()

        # 5. Finish
        await self._finish()

    async def _step_nickname(self) -> None:
        console = self.context.console
        state_dir = self.context.data_dir / STATE_DIR_NAME
        storage = StorageManager(state_dir)
        mgr = IdentityManager(IdentityStore(storage))
        ident = mgr.ensure()
        fp = identity_fingerprint(ident.public_key_bytes)

        console.clear()
        console.newline()
        panel_body = Group(
            Text(f"Your public safety fingerprint: {fp}", style="gl.accent"),
            Text(""),
            Text("Enter a display pseudonym for room chats (optional):", style="gl.text"),
        )
        console.print(section_panel("Step 1 of 4: Identity & Nickname", panel_body))
        console.newline()

        raw = await asyncio.to_thread(
            prompt_text,
            console.console,
            "Chat pseudonym (Enter to skip)",
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

        self.context.console.clear()
        self.context.console.newline()
        self.context.console.print(
            section_panel(
                "Step 2 of 4: Color Theme",
                Text("Choose your terminal visual palette", style="gl.text"),
            )
        )
        self.context.console.newline()

        entries = []
        for name in BUILTIN_THEMES:
            entries.append(
                MenuEntry(
                    key=name,
                    label=name.title(),
                    description=engine.get(name).description,
                    icon="●" if name == current else "○",
                )
            )
        entries.append(
            MenuEntry(
                key="keep",
                label="Keep Default (Phantom)",
                description="Continue with current theme",
                icon="✔",
            )
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
        self.context.console.clear()
        self.context.console.newline()
        self.context.console.print(
            section_panel(
                "Step 3 of 4: Language",
                Text("Select interface language", style="gl.text"),
            )
        )
        self.context.console.newline()

        entries = []
        for code in sorted(SUPPORTED_LANGUAGES.keys()):
            entries.append(
                MenuEntry(
                    key=code,
                    label=LANGUAGE_NAMES.get(code, SUPPORTED_LANGUAGES[code]),
                    description=f"Language code: {code}",
                    icon="🌐",
                )
            )

        chosen = self._menu.prompt(entries, default_key="en")
        if chosen in SUPPORTED_LANGUAGES:
            set_current_language(chosen)
            self.context.settings = replace(
                self.context.settings,
                ui=replace(self.context.settings.ui, language=chosen),
            )

    async def _step_privacy(self) -> None:
        console = self.context.console
        console.clear()
        console.newline()
        self.context.console.print(
            section_panel(
                "Step 4 of 4: Privacy Defaults",
                Text(
                    "Configure double-check read receipts and typing indicators",
                    style="gl.text",
                ),
            )
        )
        console.newline()

        enable_receipts = await asyncio.to_thread(
            confirm, console.console, "Send read receipts (✓✓ turns blue on read)?", default=True
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
        # Mark onboarding as completed and persist to disk
        updated = replace(
            self.context.settings,
            meta=replace(self.context.settings.meta, onboarding_completed=True),
        )
        try:
            save_config_file(self.context.config_path, updated)
            self.context.settings = updated
        except Exception:
            pass

        self.context.notifications.notify(
            "✓ Setup complete — welcome to GhostLink!",
            NotificationLevel.SUCCESS,
        )
