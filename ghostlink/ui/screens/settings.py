"""Settings Center — interactive configuration and customization.

Allows genuine in-app customization of GhostLink preferences including color
themes with live previews, interface language, privacy controls (read receipts,
typing indicators, history retention), in-app notifications, relay rendezvous
networking, storage directories, and diagnostics.

Every change is validated, atomically persisted to the configuration file,
immediately applied to runtime state, and confirmed with clear visual feedback.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from pathlib import Path

from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ghostlink.config.serializer import save_config_file
from ghostlink.constants.app import (
    BUILTIN_THEMES,
    CHAT_HISTORY_MODES,
    CHAT_NOTIFICATION_STYLES,
    LANGUAGE_NAMES,
    SUPPORTED_LANGUAGES,
)
from ghostlink.constants.files import STATE_DIR_NAME
from ghostlink.exceptions.config import ConfigurationError, ConfigValidationError
from ghostlink.i18n import set_current_language, t
from ghostlink.identity.fingerprint import identity_fingerprint
from ghostlink.identity.lifecycle import IdentityManager
from ghostlink.identity.storage import IdentityStore
from ghostlink.models.settings import AppSettings
from ghostlink.models.theme import ThemeSpec
from ghostlink.storage.manager import StorageManager
from ghostlink.ui.components.badges import BadgeTone, badge
from ghostlink.ui.components.dialogs import confirm, notice_dialog, prompt_text
from ghostlink.ui.components.notifications import NotificationLevel
from ghostlink.ui.components.panels import section_panel
from ghostlink.ui.components.tables import kv_grid
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.base import Screen, ScreenContext
from ghostlink.ui.themes import ThemeEngine


def _format_relay_value(settings: AppSettings, theme: ThemeSpec, lang: str) -> Text:
    """Formatted relay indicator."""

    url = settings.relay.url.strip()
    if not url:
        return Text.assemble(
            badge(t("status.not_configured", lang), BadgeTone.MUTED, theme=theme),
            ("  (local rooms)", "gl.muted"),
        )
    return Text.assemble(
        badge(t("status.configured", lang), BadgeTone.SUCCESS, theme=theme),
        f"  {url}",
    )


def _render_theme_preview_box(spec: ThemeSpec) -> Panel:
    """Render a compact live preview demonstrating a theme palette."""

    preview_table = Table(box=None, show_header=False, pad_edge=False, expand=True)
    preview_table.add_column("Element", style="gl.muted", width=18)
    preview_table.add_column("Sample Display")

    preview_table.add_row(
        "Primary Accent",
        Text("■ GhostLink Secure Terminal", style=f"bold {spec.primary}"),
    )
    preview_table.add_row(
        "Selected Badge",
        Text(f" ◆ {spec.name.title()} ", style=f"bold {spec.on_accent} on {spec.accent}"),
    )
    preview_table.add_row(
        "Success Notice",
        Text("✔ Encryption verified · 0 compromise", style=f"bold {spec.success}"),
    )
    preview_table.add_row(
        "Warning Alert",
        Text("▲ Relay reconnecting in 1.0s", style=f"bold {spec.warning}"),
    )
    preview_table.add_row(
        "Error State",
        Text("✖ Invalid frame checksum", style=f"bold {spec.error}"),
    )
    preview_table.add_row(
        "Muted Text",
        Text("○ Session ephemeral · No disk traces", style=spec.muted),
    )

    return Panel(
        Group(
            Text(spec.description, style=f"italic {spec.text}"),
            Text(""),
            preview_table,
        ),
        title=f"[bold {spec.primary}]Theme Preview: {spec.name.title()}[/]",
        border_style=spec.border,
        padding=(1, 2),
        expand=False,
    )


class SettingsScreen(Screen):
    """Interactive Settings Center."""

    def __init__(self, context: ScreenContext) -> None:
        super().__init__(context)
        self._menu = InteractiveMenu(context.console)

    async def show(self) -> None:
        while True:
            lang = self.context.settings.ui.language
            set_current_language(lang)
            self._render_overview()

            entries = (
                MenuEntry(
                    key="appearance",
                    label=t("settings.cat.appearance.title", lang),
                    description=t("settings.cat.appearance.desc", lang),
                    icon="🎨",
                ),
                MenuEntry(
                    key="privacy",
                    label=t("settings.cat.privacy.title", lang),
                    description=t("settings.cat.privacy.desc", lang),
                    icon="🔒",
                ),
                MenuEntry(
                    key="notifications",
                    label=t("settings.cat.notifications.title", lang),
                    description=t("settings.cat.notifications.desc", lang),
                    icon="🔔",
                ),
                MenuEntry(
                    key="network",
                    label=t("settings.cat.network.title", lang),
                    description=t("settings.cat.network.desc", lang),
                    icon="🌐",
                ),
                MenuEntry(
                    key="storage",
                    label=t("settings.cat.storage.title", lang),
                    description=t("settings.cat.storage.desc", lang),
                    icon="💾",
                ),
                MenuEntry(
                    key="developer",
                    label=t("settings.cat.developer.title", lang),
                    description=t("settings.cat.developer.desc", lang),
                    icon="🛠",
                ),
                MenuEntry(
                    key="back",
                    label=t("settings.back", lang),
                    description=t("settings.back.desc", lang),
                    icon="↩",
                ),
            )

            choice = self._menu.prompt(entries, default_key="back")
            if choice == "back":
                return
            await self._dispatch_category(choice)

    # ---------------------------------------------------------------- render overview

    def _render_overview(self) -> None:
        context = self.context
        console = context.console
        settings = context.settings
        theme = context.theme
        lang = settings.ui.language

        console.clear()
        console.newline()

        overview_table = Table(
            box=None,
            show_header=True,
            header_style="gl.title",
            pad_edge=False,
            expand=True,
        )
        overview_table.add_column("Category", style="gl.accent", width=22)
        overview_table.add_column("Setting", style="gl.text", width=24)
        overview_table.add_column("Current Value", style="gl.highlight")

        # Appearance
        overview_table.add_row(
            t("settings.cat.appearance.title", lang),
            t("settings.theme.label", lang),
            badge(theme.name.title(), BadgeTone.ACCENT, theme=theme),
        )
        overview_table.add_row(
            "",
            t("settings.language.label", lang),
            Text(f"{SUPPORTED_LANGUAGES.get(lang, lang)} ({lang})"),
        )
        overview_table.add_section()

        # Privacy
        overview_table.add_row(
            t("settings.cat.privacy.title", lang),
            t("settings.read_receipts.label", lang),
            badge(
                t("status.on", lang) if settings.chat.read_receipts else t("status.off", lang),
                BadgeTone.SUCCESS if settings.chat.read_receipts else BadgeTone.MUTED,
                theme=theme,
            ),
        )
        overview_table.add_row(
            "",
            t("settings.typing_indicators.label", lang),
            badge(
                t("status.on", lang) if settings.chat.typing_indicators else t("status.off", lang),
                BadgeTone.SUCCESS if settings.chat.typing_indicators else BadgeTone.MUTED,
                theme=theme,
            ),
        )
        overview_table.add_row(
            "",
            t("settings.history_mode.label", lang),
            Text(settings.chat.history_mode),
        )
        overview_table.add_row(
            "",
            t("settings.display_name.label", lang),
            Text(
                settings.chat.display_name or t("status.per_run_default", lang),
                style="gl.muted",
            ),
        )
        overview_table.add_section()

        # Notifications
        notif_label = (
            t("status.enabled", lang)
            if settings.notifications.enabled
            else t("status.disabled", lang)
        )
        overview_table.add_row(
            t("settings.cat.notifications.title", lang),
            t("settings.notifications_enabled.label", lang),
            badge(
                notif_label,
                BadgeTone.SUCCESS if settings.notifications.enabled else BadgeTone.MUTED,
                theme=theme,
            ),
        )
        overview_table.add_row(
            "",
            t("settings.notification_style.label", lang),
            Text(settings.chat.notification_style),
        )
        overview_table.add_section()

        # Network
        overview_table.add_row(
            t("settings.cat.network.title", lang),
            t("settings.relay_url.label", lang),
            _format_relay_value(settings, theme, lang),
        )
        overview_table.add_section()

        # Storage
        data_dir_display = (
            settings.storage.data_dir
            if settings.storage.data_dir.strip()
            else f"{t('status.default', lang)} ({context.data_dir})"
        )
        overview_table.add_row(
            t("settings.cat.storage.title", lang),
            t("settings.data_dir.label", lang),
            Text(data_dir_display, style="gl.muted"),
        )
        overview_table.add_section()

        # Developer & System
        overview_table.add_row(
            t("settings.cat.developer.title", lang),
            t("settings.debug_mode.label", lang),
            badge(
                t("status.on", lang) if settings.diagnostics.debug else t("status.off", lang),
                BadgeTone.WARNING if settings.diagnostics.debug else BadgeTone.MUTED,
                theme=theme,
            ),
        )
        overview_table.add_row(
            "",
            t("settings.system_info.label", lang),
            badge(t("status.readonly", lang), BadgeTone.INFO, theme=theme),
        )

        config_footer = Text.assemble(
            ("Configuration Path: ", "gl.muted"),
            (str(context.config_path), "gl.text"),
        )

        content = Group(
            overview_table,
            Text(""),
            config_footer,
        )

        console.print(
            section_panel(
                t("settings.title", lang),
                content,
                subtitle=t("settings.subtitle", lang),
            )
        )
        console.newline()

    # ---------------------------------------------------------------- category dispatch

    async def _dispatch_category(self, category: str) -> None:
        if category == "appearance":
            await self._appearance_menu()
        elif category == "privacy":
            await self._privacy_menu()
        elif category == "notifications":
            await self._notifications_menu()
        elif category == "network":
            await self._network_menu()
        elif category == "storage":
            await self._storage_menu()
        elif category == "developer":
            await self._developer_menu()

    # ---------------------------------------------------------------- Appearance

    async def _appearance_menu(self) -> None:
        while True:
            lang = self.context.settings.ui.language
            self._render_category_header(
                t("settings.cat.appearance.title", lang),
                t("settings.cat.appearance.desc", lang),
            )
            entries = (
                MenuEntry(
                    key="theme",
                    label=f"{t('settings.theme.label', lang)}: {self.context.theme.name.title()}",
                    description="Change color theme with live preview",
                    icon="🎨",
                ),
                MenuEntry(
                    key="language",
                    label=f"{t('settings.language.label', lang)}: {LANGUAGE_NAMES.get(lang, lang)}",
                    description="Change interface language",
                    icon="🌐",
                ),
                MenuEntry(
                    key="back",
                    label=t("action.back", lang),
                    description="Return to Settings Center",
                    icon="↩",
                ),
            )
            choice = self._menu.prompt(entries, default_key="back")
            if choice == "back":
                return
            if choice == "theme":
                await self._select_theme()
            elif choice == "language":
                await self._select_language()

    async def _select_theme(self) -> None:
        engine = ThemeEngine()
        current_theme_name = self.context.settings.ui.theme.lower()
        while True:
            lang = self.context.settings.ui.language
            self._render_category_header(
                t("dialog.theme_select.title", lang),
                "Select a color theme to preview and apply",
            )
            entries = []
            for name in BUILTIN_THEMES:
                is_active = name == current_theme_name
                tag = f" [{t('status.active', lang)}]" if is_active else ""
                entries.append(
                    MenuEntry(
                        key=name,
                        label=f"{name.title()}{tag}",
                        description=engine.get(name).description,
                        icon="●" if is_active else "○",
                    )
                )
            entries.append(
                MenuEntry(
                    key="cancel",
                    label=t("action.cancel", lang),
                    description="Keep current theme and return",
                    icon="✖",
                )
            )

            chosen = self._menu.prompt(entries, default_key="cancel")
            if chosen == "cancel":
                return

            # Show theme live preview and confirm
            spec = engine.get(chosen)
            self.context.console.clear()
            self.context.console.newline()
            self.context.console.print(Align.center(_render_theme_preview_box(spec)))
            self.context.console.newline()

            apply_confirmed = await asyncio.to_thread(
                confirm,
                self.context.console.console,
                f"Apply {spec.name.title()} theme?",
                default=True,
            )
            if apply_confirmed:
                updated = replace(
                    self.context.settings,
                    ui=replace(self.context.settings.ui, theme=spec.name),
                )
                if self._persist_settings(updated):
                    self.context.console.set_theme(spec)
                    current_theme_name = spec.name
                    self.context.notifications.notify(
                        t("dialog.theme_changed", lang, theme=spec.name.title()),
                        NotificationLevel.SUCCESS,
                    )
                    return

    async def _select_language(self) -> None:
        current_lang = self.context.settings.ui.language
        while True:
            lang = self.context.settings.ui.language
            self._render_category_header(
                t("dialog.language_select.title", lang),
                "Select the display language for menus, dialogs, and settings",
            )
            entries = []
            for code in sorted(SUPPORTED_LANGUAGES.keys()):
                is_active = code == current_lang
                name_display = LANGUAGE_NAMES.get(code, SUPPORTED_LANGUAGES[code])
                tag = f" [{t('status.active', lang)}]" if is_active else ""
                entries.append(
                    MenuEntry(
                        key=code,
                        label=f"{name_display}{tag}",
                        description=f"Language code: {code}",
                        icon="●" if is_active else "○",
                    )
                )
            entries.append(
                MenuEntry(
                    key="cancel",
                    label=t("action.cancel", lang),
                    description="Keep current language and return",
                    icon="✖",
                )
            )

            chosen = self._menu.prompt(entries, default_key="cancel")
            if chosen == "cancel":
                return

            updated = replace(
                self.context.settings,
                ui=replace(self.context.settings.ui, language=chosen),
            )
            if self._persist_settings(updated):
                set_current_language(chosen)
                current_lang = chosen
                target_name = SUPPORTED_LANGUAGES.get(chosen, chosen)
                self.context.notifications.notify(
                    t("dialog.language_changed", chosen, language=target_name),
                    NotificationLevel.SUCCESS,
                )
                return

    # ---------------------------------------------------------------- Privacy & Chat

    async def _privacy_menu(self) -> None:
        while True:
            settings = self.context.settings
            lang = settings.ui.language
            self._render_category_header(
                t("settings.cat.privacy.title", lang),
                t("settings.cat.privacy.desc", lang),
            )

            rr_on = settings.chat.read_receipts
            ti_on = settings.chat.typing_indicators
            pres_on = settings.chat.presence
            clean_on = settings.chat.cleanup_on_exit
            rr_state = t("status.on", lang) if rr_on else t("status.off", lang)
            ti_state = t("status.on", lang) if ti_on else t("status.off", lang)
            pres_state = t("status.on", lang) if pres_on else t("status.off", lang)
            clean_state = t("status.on", lang) if clean_on else t("status.off", lang)
            name_state = settings.chat.display_name or t("status.per_run_default", lang)

            mode_label = f"{t('settings.history_mode.label', lang)} [{settings.chat.history_mode}]"
            entries = (
                MenuEntry(
                    key="toggle_receipts",
                    label=f"{t('settings.read_receipts.label', lang)} [{rr_state}]",
                    description="Send and display message read receipts",
                    icon="✓" if settings.chat.read_receipts else "○",
                ),
                MenuEntry(
                    key="toggle_typing",
                    label=f"{t('settings.typing_indicators.label', lang)} [{ti_state}]",
                    description="Broadcast typing status to peers",
                    icon="✎" if settings.chat.typing_indicators else "○",
                ),
                MenuEntry(
                    key="toggle_presence",
                    label=f"Presence & Peer Visibility [{pres_state}]",
                    description="Broadcast peer online presence status",
                    icon="🟢" if settings.chat.presence else "○",
                ),
                MenuEntry(
                    key="history_mode",
                    label=mode_label,
                    description="Message retention (disabled, session, encrypted)",
                    icon="◷",
                ),
                MenuEntry(
                    key="toggle_cleanup",
                    label=f"Auto Data Cleanup on Exit [{clean_state}]",
                    description="Wipe session caches and history on exit",
                    icon="🧹" if settings.chat.cleanup_on_exit else "○",
                ),
                MenuEntry(
                    key="display_name",
                    label=f"{t('settings.display_name.label', lang)} [{name_state}]",
                    description=t("settings.display_name.desc", lang),
                    icon="👤",
                ),
                MenuEntry(
                    key="identity_keys",
                    label="Identity & Cryptographic Keys",
                    description="View fingerprint or rotate identity keypair",
                    icon="🔑",
                ),
                MenuEntry(
                    key="back",
                    label=t("action.back", lang),
                    description="Return to Settings Center",
                    icon="↩",
                ),
            )

            choice = self._menu.prompt(entries, default_key="back")
            if choice == "back":
                return
            if choice == "toggle_receipts":
                new_val = not settings.chat.read_receipts
                updated = replace(
                    settings,
                    chat=replace(settings.chat, read_receipts=new_val),
                )
                if self._persist_settings(updated):
                    status_str = t("status.on", lang) if new_val else t("status.off", lang)
                    self.context.notifications.notify(
                        f"✓ {t('settings.read_receipts.label', lang)}: {status_str}",
                        NotificationLevel.SUCCESS if new_val else NotificationLevel.INFO,
                    )
            elif choice == "toggle_typing":
                new_val = not settings.chat.typing_indicators
                updated = replace(
                    settings,
                    chat=replace(settings.chat, typing_indicators=new_val),
                )
                if self._persist_settings(updated):
                    status_str = t("status.on", lang) if new_val else t("status.off", lang)
                    self.context.notifications.notify(
                        f"✓ {t('settings.typing_indicators.label', lang)}: {status_str}",
                        NotificationLevel.SUCCESS if new_val else NotificationLevel.INFO,
                    )
            elif choice == "toggle_presence":
                new_val = not settings.chat.presence
                updated = replace(
                    settings,
                    chat=replace(settings.chat, presence=new_val),
                )
                if self._persist_settings(updated):
                    status_str = t("status.on", lang) if new_val else t("status.off", lang)
                    self.context.notifications.notify(
                        f"✓ Presence Visibility: {status_str}",
                        NotificationLevel.SUCCESS if new_val else NotificationLevel.INFO,
                    )
            elif choice == "toggle_cleanup":
                new_val = not settings.chat.cleanup_on_exit
                updated = replace(
                    settings,
                    chat=replace(settings.chat, cleanup_on_exit=new_val),
                )
                if self._persist_settings(updated):
                    status_str = t("status.on", lang) if new_val else t("status.off", lang)
                    self.context.notifications.notify(
                        f"✓ Auto Cleanup on Exit: {status_str}",
                        NotificationLevel.SUCCESS if new_val else NotificationLevel.INFO,
                    )
            elif choice == "history_mode":
                await self._select_history_mode()
            elif choice == "display_name":
                await self._edit_display_name()
            elif choice == "identity_keys":
                await self._identity_menu()

    async def _identity_menu(self) -> None:
        while True:
            lang = self.context.settings.ui.language
            self._render_category_header(
                "Identity & Cryptographic Keys",
                "Manage local installation identity, public fingerprint, and key rotation",
            )
            state_dir = self.context.data_dir / STATE_DIR_NAME
            fingerprint_display = "No local identity initialized"
            try:
                store = IdentityStore(StorageManager(state_dir))
                mgr = IdentityManager(store)
                identity = mgr.load()
                if identity is not None:
                    fingerprint_display = identity_fingerprint(identity.public_key_bytes)
            except Exception:
                fingerprint_display = "Unavailable"

            entries = (
                MenuEntry(
                    key="view_fingerprint",
                    label=f"View Public Fingerprint [{fingerprint_display[:16]}…]",
                    description="View the GLFP cryptographic fingerprint peers verify",
                    icon="🔑",
                ),
                MenuEntry(
                    key="rotate_keys",
                    label="Rotate Identity Keypair",
                    description="Generate a fresh cryptographic identity keypair",
                    icon="↺",
                ),
                MenuEntry(
                    key="back",
                    label=t("action.back", lang),
                    description="Return to Privacy & Chat menu",
                    icon="↩",
                ),
            )
            choice = self._menu.prompt(entries, default_key="back")
            if choice == "back":
                return
            if choice == "view_fingerprint":
                await self._view_fingerprint(fingerprint_display)
            elif choice == "rotate_keys":
                await self._rotate_identity()

    async def _view_fingerprint(self, fingerprint: str) -> None:
        console = self.context.console
        console.clear()
        console.newline()
        body = Group(
            Text("Your cryptographic public identity fingerprint:", style="gl.text"),
            Text(""),
            Align.center(Text(fingerprint, style="bold gl.accent")),
            Text(""),
            Text(
                "Share this fingerprint out-of-band to verify end-to-end encryption with contacts.",
                style="gl.muted",
            ),
        )
        console.print(section_panel("Identity Fingerprint", body))
        await self.pause()

    async def _rotate_identity(self) -> None:
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
        state_dir = self.context.data_dir / STATE_DIR_NAME
        try:
            store = IdentityStore(StorageManager(state_dir))
            mgr = IdentityManager(store)
            mgr.reset()
            self.context.notifications.notify(
                t("dialog.identity_rotated", lang),
                NotificationLevel.WARNING,
            )
        except Exception as exc:
            console.newline()
            console.print(notice_dialog("Identity Rotation Failed", str(exc), tone=BadgeTone.ERROR))
            await self.pause()

    async def _select_history_mode(self) -> None:
        settings = self.context.settings
        lang = settings.ui.language
        current_mode = settings.chat.history_mode

        descriptions = {
            "disabled": "Ephemeral — conversations exist in RAM only and vanish on exit",
            "session": "Retained in memory during the active session",
            "encrypted": "Encrypted to a passphrase-protected local storage file",
        }

        self._render_category_header(
            t("dialog.history_select.title", lang),
            "Choose message history retention policy",
        )
        entries = []
        for mode in sorted(CHAT_HISTORY_MODES):
            is_active = mode == current_mode
            tag = f" [{t('status.active', lang)}]" if is_active else ""
            entries.append(
                MenuEntry(
                    key=mode,
                    label=f"{mode.title()}{tag}",
                    description=descriptions.get(mode, mode),
                    icon="●" if is_active else "○",
                )
            )
        entries.append(
            MenuEntry(
                key="cancel",
                label=t("action.cancel", lang),
                description="Keep current mode and return",
                icon="✖",
            )
        )

        chosen = self._menu.prompt(entries, default_key="cancel")
        if chosen != "cancel" and chosen != current_mode:
            updated = replace(
                settings,
                chat=replace(settings.chat, history_mode=chosen),
            )
            if self._persist_settings(updated):
                self.context.notifications.notify(
                    f"✓ {t('settings.history_mode.label', lang)}: {chosen}",
                    NotificationLevel.SUCCESS,
                )

    async def _edit_display_name(self) -> None:
        console = self.context.console
        lang = self.context.settings.ui.language
        current_name = self.context.settings.chat.display_name

        self._render_category_header(
            t("settings.display_name.label", lang),
            f"Current pseudonym: {current_name or t('status.per_run_default', lang)}",
        )

        raw = await asyncio.to_thread(
            prompt_text,
            console.console,
            t("dialog.name_prompt", lang),
            allow_empty=True,
            max_length=24,
        )
        if raw is None:
            return

        name = raw.strip()
        updated = replace(
            self.context.settings,
            chat=replace(self.context.settings.chat, display_name=name),
        )
        if self._persist_settings(updated):
            if name:
                self.context.notifications.notify(
                    f"✓ Pseudonym set to '{name}'",
                    NotificationLevel.SUCCESS,
                )
            else:
                self.context.notifications.notify(
                    t("dialog.name_cleared", lang),
                    NotificationLevel.INFO,
                )

    # ---------------------------------------------------------------- Notifications

    async def _notifications_menu(self) -> None:
        while True:
            settings = self.context.settings
            lang = settings.ui.language
            self._render_category_header(
                t("settings.cat.notifications.title", lang),
                t("settings.cat.notifications.desc", lang),
            )

            notif = settings.notifications
            is_en = notif.enabled
            state_label = t("status.enabled", lang) if is_en else t("status.disabled", lang)
            msg_state = t("status.on", lang) if notif.messages else t("status.off", lang)
            room_state = t("status.on", lang) if notif.room_activity else t("status.off", lang)
            inv_state = t("status.on", lang) if notif.invites else t("status.off", lang)
            snd_state = t("status.on", lang) if notif.sound else t("status.off", lang)
            vib_state = t("status.on", lang) if notif.vibration else t("status.off", lang)
            style_label = (
                f"{t('settings.notification_style.label', lang)} "
                f"[{settings.chat.notification_style}]"
            )

            entries = (
                MenuEntry(
                    key="toggle",
                    label=f"{t('settings.notifications_enabled.label', lang)} [{state_label}]",
                    description="Master switch for in-app alert toasts",
                    icon="🔔" if settings.notifications.enabled else "🔕",
                ),
                MenuEntry(
                    key="toggle_messages",
                    label=f"Message Alerts [{msg_state}]",
                    description="Show alert toasts when new chat messages arrive",
                    icon="💬" if settings.notifications.messages else "○",
                ),
                MenuEntry(
                    key="toggle_room",
                    label=f"Room Activity Alerts [{room_state}]",
                    description="Show alert toasts on room peer join/leave events",
                    icon="👥" if settings.notifications.room_activity else "○",
                ),
                MenuEntry(
                    key="toggle_invites",
                    label=f"Invite Alerts [{inv_state}]",
                    description="Show alert toasts on invite creation and redemptions",
                    icon="✉" if settings.notifications.invites else "○",
                ),
                MenuEntry(
                    key="toggle_sound",
                    label=f"Sound Alerts [{snd_state}]",
                    description="Audible bell notification on messages where terminal permits",
                    icon="🔊" if settings.notifications.sound else "🔇",
                ),
                MenuEntry(
                    key="toggle_vibration",
                    label=f"Vibration Alerts [{vib_state}]",
                    description="Haptic vibration feedback on Termux/Android",
                    icon="📳" if settings.notifications.vibration else "📴",
                ),
                MenuEntry(
                    key="style",
                    label=style_label,
                    description="Choose banner, compact, or muted presentation",
                    icon="📑",
                ),
                MenuEntry(
                    key="back",
                    label=t("action.back", lang),
                    description="Return to Settings Center",
                    icon="↩",
                ),
            )

            choice = self._menu.prompt(entries, default_key="back")
            if choice == "back":
                return
            if choice == "toggle":
                new_enabled = not settings.notifications.enabled
                updated = replace(
                    settings,
                    notifications=replace(settings.notifications, enabled=new_enabled),
                )
                if self._persist_settings(updated):
                    status_str = (
                        t("status.enabled", lang) if new_enabled else t("status.disabled", lang)
                    )
                    self.context.notifications.notify(
                        f"✓ Notifications {status_str}",
                        NotificationLevel.SUCCESS if new_enabled else NotificationLevel.INFO,
                    )
            elif choice == "toggle_messages":
                new_val = not settings.notifications.messages
                updated = replace(
                    settings,
                    notifications=replace(settings.notifications, messages=new_val),
                )
                if self._persist_settings(updated):
                    status_str = t("status.on", lang) if new_val else t("status.off", lang)
                    self.context.notifications.notify(
                        f"✓ Message Alerts: {status_str}",
                        NotificationLevel.SUCCESS if new_val else NotificationLevel.INFO,
                    )
            elif choice == "toggle_room":
                new_val = not settings.notifications.room_activity
                updated = replace(
                    settings,
                    notifications=replace(settings.notifications, room_activity=new_val),
                )
                if self._persist_settings(updated):
                    status_str = t("status.on", lang) if new_val else t("status.off", lang)
                    self.context.notifications.notify(
                        f"✓ Room Activity Alerts: {status_str}",
                        NotificationLevel.SUCCESS if new_val else NotificationLevel.INFO,
                    )
            elif choice == "toggle_invites":
                new_val = not settings.notifications.invites
                updated = replace(
                    settings,
                    notifications=replace(settings.notifications, invites=new_val),
                )
                if self._persist_settings(updated):
                    status_str = t("status.on", lang) if new_val else t("status.off", lang)
                    self.context.notifications.notify(
                        f"✓ Invite Alerts: {status_str}",
                        NotificationLevel.SUCCESS if new_val else NotificationLevel.INFO,
                    )
            elif choice == "toggle_sound":
                new_val = not settings.notifications.sound
                updated = replace(
                    settings,
                    notifications=replace(settings.notifications, sound=new_val),
                )
                if self._persist_settings(updated):
                    status_str = t("status.on", lang) if new_val else t("status.off", lang)
                    self.context.notifications.notify(
                        f"✓ Sound Alerts: {status_str}",
                        NotificationLevel.SUCCESS if new_val else NotificationLevel.INFO,
                    )
            elif choice == "toggle_vibration":
                new_val = not settings.notifications.vibration
                updated = replace(
                    settings,
                    notifications=replace(settings.notifications, vibration=new_val),
                )
                if self._persist_settings(updated):
                    status_str = t("status.on", lang) if new_val else t("status.off", lang)
                    self.context.notifications.notify(
                        f"✓ Vibration Alerts: {status_str}",
                        NotificationLevel.SUCCESS if new_val else NotificationLevel.INFO,
                    )
            elif choice == "style":
                await self._select_notification_style()

    async def _select_notification_style(self) -> None:
        settings = self.context.settings
        lang = settings.ui.language
        current_style = settings.chat.notification_style

        descriptions = {
            "banner": "Full prominent bordered alert banners",
            "compact": "Concise single-line status notifications",
            "muted": "Silent operation — only errors are displayed",
        }

        self._render_category_header(
            t("dialog.style_select.title", lang),
            "Select visual presentation of in-app notifications",
        )
        entries = []
        for style_name in sorted(CHAT_NOTIFICATION_STYLES):
            is_active = style_name == current_style
            tag = f" [{t('status.active', lang)}]" if is_active else ""
            entries.append(
                MenuEntry(
                    key=style_name,
                    label=f"{style_name.title()}{tag}",
                    description=descriptions.get(style_name, style_name),
                    icon="●" if is_active else "○",
                )
            )
        entries.append(
            MenuEntry(
                key="cancel",
                label=t("action.cancel", lang),
                description="Keep current style and return",
                icon="✖",
            )
        )

        chosen = self._menu.prompt(entries, default_key="cancel")
        if chosen != "cancel" and chosen != current_style:
            updated = replace(
                settings,
                chat=replace(settings.chat, notification_style=chosen),
            )
            if self._persist_settings(updated):
                self.context.notifications.notify(
                    f"✓ Notification style: {chosen}",
                    NotificationLevel.SUCCESS,
                )

    # ---------------------------------------------------------------- Network & Relay

    async def _network_menu(self) -> None:
        while True:
            settings = self.context.settings
            lang = settings.ui.language
            self._render_category_header(
                t("settings.cat.network.title", lang),
                t("settings.cat.network.desc", lang),
            )

            relay_display = settings.relay.url.strip() or t("status.not_configured", lang)

            entries = (
                MenuEntry(
                    key="configure_relay",
                    label=f"Configure Relay Endpoint [{relay_display}]",
                    description="Enter a WebSocket endpoint (ws:// or wss://)",
                    icon="🔗",
                ),
                MenuEntry(
                    key="clear_relay",
                    label="Clear Relay Endpoint",
                    description="Reset relay to run in local-only room mode",
                    icon="✖",
                ),
                MenuEntry(
                    key="back",
                    label=t("action.back", lang),
                    description="Return to Settings Center",
                    icon="↩",
                ),
            )

            choice = self._menu.prompt(entries, default_key="back")
            if choice == "back":
                return
            if choice == "configure_relay":
                await self._configure_relay()
            elif choice == "clear_relay":
                await self._clear_relay()

    async def _configure_relay(self) -> None:
        console = self.context.console
        lang = self.context.settings.ui.language
        current_url = self.context.settings.relay.url

        self._render_category_header(
            t("settings.relay_url.label", lang),
            f"Current: {current_url or t('status.not_configured', lang)}",
        )

        raw = await asyncio.to_thread(
            prompt_text,
            console.console,
            t("dialog.relay_prompt", lang),
            allow_empty=True,
            max_length=256,
        )
        if raw is None:
            return

        url = raw.strip()
        if url and not url.startswith(("ws://", "wss://")):
            console.newline()
            console.print(
                notice_dialog(
                    "Invalid Relay URL",
                    f"The endpoint '{url}' must start with ws:// or wss://",
                    tone=BadgeTone.ERROR,
                    hint="Example: wss://relay.example.org or ws://127.0.0.1:8787",
                )
            )
            await self.pause()
            return

        updated = replace(
            self.context.settings,
            relay=replace(self.context.settings.relay, url=url),
        )
        if self._persist_settings(updated):
            if url:
                self.context.notifications.notify(
                    f"✓ Relay configured: {url}",
                    NotificationLevel.SUCCESS,
                )
            else:
                self.context.notifications.notify(
                    t("dialog.relay_cleared", lang),
                    NotificationLevel.INFO,
                )

    async def _clear_relay(self) -> None:
        console = self.context.console
        lang = self.context.settings.ui.language
        if not self.context.settings.relay.url.strip():
            self.context.notifications.notify(
                "Relay is already not configured.",
                NotificationLevel.INFO,
            )
            return

        confirmed = await asyncio.to_thread(
            confirm,
            console.console,
            t("dialog.confirm_clear_relay", lang),
            default=True,
        )
        if confirmed:
            updated = replace(
                self.context.settings,
                relay=replace(self.context.settings.relay, url=""),
            )
            if self._persist_settings(updated):
                self.context.notifications.notify(
                    t("dialog.relay_cleared", lang),
                    NotificationLevel.INFO,
                )

    # ---------------------------------------------------------------- Storage

    async def _storage_menu(self) -> None:
        while True:
            settings = self.context.settings
            lang = settings.ui.language
            self._render_category_header(
                t("settings.cat.storage.title", lang),
                t("settings.cat.storage.desc", lang),
            )

            current_configured = settings.storage.data_dir.strip()
            def_str = t("status.default", lang)
            display_path = (
                current_configured if current_configured else f"{def_str} ({self.context.data_dir})"
            )

            entries = (
                MenuEntry(
                    key="set_datadir",
                    label=f"Data Directory: {display_path}",
                    description="Custom filesystem path for state and logs",
                    icon="📁",
                ),
                MenuEntry(
                    key="reset_datadir",
                    label="Reset to Platform Default",
                    description="Use standard OS data directory location",
                    icon="↺",
                ),
                MenuEntry(
                    key="back",
                    label=t("action.back", lang),
                    description="Return to Settings Center",
                    icon="↩",
                ),
            )

            choice = self._menu.prompt(entries, default_key="back")
            if choice == "back":
                return
            if choice == "set_datadir":
                await self._configure_data_dir()
            elif choice == "reset_datadir":
                await self._reset_data_dir()

    async def _configure_data_dir(self) -> None:
        console = self.context.console
        lang = self.context.settings.ui.language

        self._render_category_header(
            t("settings.data_dir.label", lang),
            f"Effective path: {self.context.data_dir}",
        )

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
            candidate = Path(path_str).expanduser()
            if candidate.is_file():
                console.newline()
                console.print(
                    notice_dialog(
                        "Invalid Directory Path",
                        f"'{candidate}' already exists as a regular file.",
                        tone=BadgeTone.ERROR,
                        hint="Specify a valid directory path.",
                    )
                )
                await self.pause()
                return

        updated = replace(
            self.context.settings,
            storage=replace(self.context.settings.storage, data_dir=path_str),
        )
        if self._persist_settings(updated):
            self.context.notifications.notify(
                f"✓ Data directory updated: {path_str or t('status.default', lang)}",
                NotificationLevel.SUCCESS,
            )

    async def _reset_data_dir(self) -> None:
        lang = self.context.settings.ui.language
        if not self.context.settings.storage.data_dir.strip():
            self.context.notifications.notify(
                "Data directory is already using default.",
                NotificationLevel.INFO,
            )
            return

        updated = replace(
            self.context.settings,
            storage=replace(self.context.settings.storage, data_dir=""),
        )
        if self._persist_settings(updated):
            self.context.notifications.notify(
                f"✓ Data directory reset to {t('status.default', lang)}",
                NotificationLevel.SUCCESS,
            )

    # ---------------------------------------------------------------- Developer & System

    async def _developer_menu(self) -> None:
        while True:
            settings = self.context.settings
            lang = settings.ui.language
            self._render_category_header(
                t("settings.cat.developer.title", lang),
                t("settings.cat.developer.desc", lang),
            )

            is_dbg = settings.diagnostics.debug
            debug_state = t("status.on", lang) if is_dbg else t("status.off", lang)

            entries = (
                MenuEntry(
                    key="toggle_debug",
                    label=f"{t('settings.debug_mode.label', lang)} [{debug_state}]",
                    description=t("settings.debug_mode.desc", lang),
                    icon="🐞" if settings.diagnostics.debug else "○",
                ),
                MenuEntry(
                    key="view_system_info",
                    label=t("settings.system_info.label", lang),
                    description="View read-only environment, terminal, and identity information",
                    icon="◆",
                ),
                MenuEntry(
                    key="back",
                    label=t("action.back", lang),
                    description="Return to Settings Center",
                    icon="↩",
                ),
            )

            choice = self._menu.prompt(entries, default_key="back")
            if choice == "back":
                return
            if choice == "toggle_debug":
                new_debug = not settings.diagnostics.debug
                updated = replace(
                    settings,
                    diagnostics=replace(settings.diagnostics, debug=new_debug),
                )
                if self._persist_settings(updated):
                    status_str = t("status.on", lang) if new_debug else t("status.off", lang)
                    self.context.notifications.notify(
                        f"✓ {t('settings.debug_mode.label', lang)}: {status_str}",
                        NotificationLevel.WARNING if new_debug else NotificationLevel.INFO,
                    )
            elif choice == "view_system_info":
                await self._view_system_info()

    async def _view_system_info(self) -> None:
        context = self.context
        console = context.console
        env = context.environment
        theme = context.theme

        # Safely compute identity fingerprint
        state_dir = context.data_dir / STATE_DIR_NAME
        fingerprint_display = "No local identity initialized"
        try:
            store = IdentityStore(StorageManager(state_dir))
            identity = store.load()
            if identity is not None:
                fingerprint_display = identity_fingerprint(identity.public_key_bytes)
        except Exception:
            fingerprint_display = "Unavailable"

        geom = f"{env.terminal_columns} cols × {env.terminal_rows} rows"
        facts = kv_grid(
            [
                ("Platform", Text(env.platform_label)),
                ("OS / Kernel", Text(f"{env.system} {env.release} ({env.machine})")),
                ("Python Runtime", Text(f"{env.python_implementation} {env.python_version}")),
                ("Terminal Geometry", Text(geom)),
                ("Color Support", Text(env.color_support.value)),
                ("Configuration File", Text(str(context.config_path))),
                ("Effective Data Dir", Text(str(context.data_dir))),
                ("State Directory", Text(str(state_dir))),
                ("Logs Directory", Text(str(context.data_dir / "logs"))),
                ("Identity Fingerprint", Text(fingerprint_display, style="gl.accent")),
            ]
        )

        badge_header = Align.center(
            badge("Read-Only Environment & Diagnostic Information", BadgeTone.INFO, theme=theme)
        )

        body = Group(
            badge_header,
            Text(""),
            facts,
            Text(""),
            Text(
                "System values reflect the active host environment and runtime configuration.",
                style="gl.muted",
            ),
        )

        console.clear()
        console.newline()
        console.print(section_panel("System Diagnostics", body, subtitle="read-only"))
        await self.pause()

    # ---------------------------------------------------------------- helpers

    def _render_category_header(self, title: str, subtitle: str) -> None:
        console = self.context.console
        console.clear()
        console.newline()
        console.print(section_panel(title, Text(subtitle, style="gl.text")))
        console.newline()

    def _persist_settings(self, new_settings: AppSettings) -> bool:
        """Atomically persist settings to disk and update context."""

        try:
            save_config_file(self.context.config_path, new_settings)
            self.context.settings = new_settings
            return True
        except (ConfigurationError, ConfigValidationError, OSError) as exc:
            self.context.console.newline()
            self.context.console.print(
                notice_dialog(
                    "Could Not Save Settings",
                    str(exc),
                    tone=BadgeTone.ERROR,
                    hint="Check write permissions for the configuration file and directory.",
                )
            )
            return False
