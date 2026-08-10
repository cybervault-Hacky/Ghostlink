"""Settings — one calm, predictable settings center.

Exactly one Settings screen exists, reached from Home. It owns six category
submenus and nothing else; each submenu owns its rows and returns one level
with Back. Selecting a category opens only that category's rows — the same
screen is never rendered twice and no submenu reopens itself after a return.

Every row shows its current value right-aligned (``Theme → Phantom``,
``Read Receipts → ON``). Changes are validated, atomically persisted,
immediately applied to runtime state, and confirmed with a single status
toast.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from rich.align import Align
from rich.console import Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ghostlink.config.serializer import save_config_file
from ghostlink.constants.app import (
    APP_NAME,
    APP_VERSION,
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
from ghostlink.ui.components.badges import BadgeTone
from ghostlink.ui.components.dialogs import confirm, confirm_action, notice_dialog, prompt_text
from ghostlink.ui.components.layout import (
    GLYPH_ACCENT,
    GLYPH_CURSOR,
    GLYPH_ERROR,
    GLYPH_MUTED,
    GLYPH_SUCCESS,
    GLYPH_WARNING,
)
from ghostlink.ui.components.notifications import NotificationLevel
from ghostlink.ui.components.tables import kv_grid
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.base import Screen, ScreenContext
from ghostlink.ui.themes import ThemeEngine

EntriesBuilder = Callable[[], list[MenuEntry]]
ActionHandler = Callable[[], Awaitable[None]]


def _render_theme_preview_box(spec: ThemeSpec) -> Panel:
    """Compact live preview demonstrating a theme's semantic palette roles."""

    preview_table = Table(box=None, show_header=False, pad_edge=False, expand=True)
    preview_table.add_column("Token", style=spec.muted, width=12)
    preview_table.add_column("Sample")

    preview_table.add_row(
        "Primary", Text(f"{GLYPH_ACCENT} GhostLink", style=f"bold {spec.primary}")
    )
    preview_table.add_row(
        "Selected",
        Text(
            f" {GLYPH_CURSOR} {spec.name.title()} ", style=f"bold {spec.on_accent} on {spec.accent}"
        ),
    )
    preview_table.add_row(
        "Success", Text(f"{GLYPH_SUCCESS} Encryption verified", style=f"bold {spec.success}")
    )
    preview_table.add_row(
        "Warning", Text(f"{GLYPH_WARNING} Relay reconnecting", style=f"bold {spec.warning}")
    )
    preview_table.add_row(
        "Error", Text(f"{GLYPH_ERROR} Invalid frame checksum", style=f"bold {spec.error}")
    )
    preview_table.add_row("Muted", Text(f"{GLYPH_MUTED} Session ephemeral", style=spec.muted))

    return Panel(
        Group(
            Text(spec.description, style=f"italic {spec.text}"),
            Text(""),
            preview_table,
        ),
        title=f"[bold {spec.primary}]Preview — {spec.name.title()}[/]",
        border_style=spec.border,
        padding=(1, 2),
        expand=False,
    )


class SettingsScreen(Screen):
    """The single interactive Settings screen."""

    def __init__(self, context: ScreenContext) -> None:
        super().__init__(context)
        self._menu = InteractiveMenu(context.console)

    async def show(self) -> None:
        while True:
            lang = self.context.settings.ui.language
            set_current_language(lang)
            self.header(t("settings.title", lang), t("settings.subtitle", lang))

            entries = (
                MenuEntry(
                    key="appearance",
                    label=t("settings.cat.appearance.title", lang),
                    description=t("settings.cat.appearance.desc", lang),
                ),
                MenuEntry(
                    key="privacy",
                    label=t("settings.cat.privacy.title", lang),
                    description=t("settings.cat.privacy.desc", lang),
                ),
                MenuEntry(
                    key="notifications",
                    label=t("settings.cat.notifications.title", lang),
                    description=t("settings.cat.notifications.desc", lang),
                ),
                MenuEntry(
                    key="network",
                    label=t("settings.cat.network.title", lang),
                    description=t("settings.cat.network.desc", lang),
                ),
                MenuEntry(
                    key="storage",
                    label=t("settings.cat.storage.title", lang),
                    description=t("settings.cat.storage.desc", lang),
                ),
                MenuEntry(
                    key="developer",
                    label=t("settings.cat.developer.title", lang),
                    description=t("settings.cat.developer.desc", lang),
                ),
                MenuEntry.spacer(),
                self.back_menu_entry(t("settings.back.desc", lang)),
            )

            choice = self._menu.prompt(entries, default_key="back")
            if choice == "back":
                return
            await self._dispatch_category(choice)

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

    # ---------------------------------------------------------------- shared plumbing

    async def _run_submenu(
        self,
        title: str,
        subtitle: str,
        build_entries: EntriesBuilder,
        actions: dict[str, ActionHandler],
    ) -> None:
        """One loop, one dispatch table: the owner of a category submenu."""

        while True:
            self.header(title, subtitle)
            entries = (*build_entries(), MenuEntry.spacer(), self.back_menu_entry())
            choice = self._menu.prompt(entries, default_key="back")
            if choice == "back":
                return
            handler = actions.get(choice)
            if handler is not None:
                await handler()

    def _toggle_chat(self, field: str, label: str) -> None:
        settings = self.context.settings
        new_val = not getattr(settings.chat, field)
        changes: dict[str, Any] = {field: new_val}
        updated = replace(settings, chat=replace(settings.chat, **changes))
        if self._persist_settings(updated):
            status = self._on_off(new_val, settings.ui.language)
            self.context.notifications.notify(
                f"{label}: {status}",
                NotificationLevel.SUCCESS if new_val else NotificationLevel.INFO,
            )

    def _toggle_notifications(self, field: str, label: str) -> None:
        settings = self.context.settings
        new_val = not getattr(settings.notifications, field)
        changes: dict[str, Any] = {field: new_val}
        updated = replace(settings, notifications=replace(settings.notifications, **changes))
        if self._persist_settings(updated):
            status = self._on_off(new_val, settings.ui.language)
            self.context.notifications.notify(
                f"{label}: {status}",
                NotificationLevel.SUCCESS if new_val else NotificationLevel.INFO,
            )

    def _on_off(self, value: bool, lang: str) -> str:
        return (t("status.on", lang) if value else t("status.off", lang)).upper()

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
                    "Could not save settings",
                    str(exc),
                    tone=BadgeTone.ERROR,
                    hint="Check write permissions for the configuration file and directory.",
                )
            )
            return False

    # ---------------------------------------------------------------- Appearance

    async def _appearance_menu(self) -> None:
        lang = self.context.settings.ui.language

        def entries() -> list[MenuEntry]:
            current_lang = self.context.settings.ui.language
            return [
                MenuEntry(
                    key="theme",
                    label=t("settings.theme.label", lang),
                    description=t("settings.theme.desc", lang),
                    value=self.context.theme.name.title(),
                ),
                MenuEntry(
                    key="language",
                    label=t("settings.language.label", lang),
                    description=t("settings.language.desc", lang),
                    value=LANGUAGE_NAMES.get(current_lang, current_lang),
                ),
            ]

        async def edit_theme() -> None:
            await self._select_theme()

        async def edit_language() -> None:
            await self._select_language()

        await self._run_submenu(
            t("settings.cat.appearance.title", lang),
            t("settings.cat.appearance.desc", lang),
            entries,
            {"theme": edit_theme, "language": edit_language},
        )

    async def _select_theme(self) -> None:
        engine = ThemeEngine()
        lang = self.context.settings.ui.language
        current_theme_name = self.context.settings.ui.theme.lower()

        self.header(
            t("dialog.theme_select.title", lang),
            t("settings.theme.desc", lang),
        )
        entries: list[MenuEntry] = []
        for name in BUILTIN_THEMES:
            is_active = name == current_theme_name
            entries.append(
                MenuEntry(
                    key=name,
                    label=name.title(),
                    description=engine.get(name).description,
                    value=t("status.current", lang) if is_active else "",
                )
            )
        entries.append(MenuEntry.spacer())
        entries.append(
            MenuEntry(
                key="cancel", label=t("action.cancel", lang), description="Keep current theme"
            )
        )

        chosen = self._menu.prompt(entries, default_key="cancel")
        if chosen == "cancel" or chosen == current_theme_name:
            return

        # Live preview, then one clean confirmation.
        spec = engine.get(chosen)
        self.context.console.newline()
        self.context.console.print(Align.center(_render_theme_preview_box(spec)))
        self.context.console.newline()

        apply_confirmed = await asyncio.to_thread(
            confirm,
            self.context.console.console,
            f"Apply {spec.name.title()} theme?",
            default=True,
        )
        if not apply_confirmed:
            return
        updated = replace(
            self.context.settings,
            ui=replace(self.context.settings.ui, theme=spec.name),
        )
        if self._persist_settings(updated):
            self.context.console.set_theme(spec)
            self.context.notifications.notify(
                t("dialog.theme_changed", lang, theme=spec.name.title()),
                NotificationLevel.SUCCESS,
            )

    async def _select_language(self) -> None:
        lang = self.context.settings.ui.language
        current_lang = self.context.settings.ui.language

        self.header(
            t("dialog.language_select.title", lang),
            t("settings.language.desc", lang),
        )
        entries: list[MenuEntry] = []
        for code in sorted(SUPPORTED_LANGUAGES.keys()):
            is_active = code == current_lang
            entries.append(
                MenuEntry(
                    key=code,
                    label=LANGUAGE_NAMES.get(code, SUPPORTED_LANGUAGES[code]),
                    description=f"Language code: {code}",
                    value=t("status.current", lang) if is_active else "",
                )
            )
        entries.append(MenuEntry.spacer())
        entries.append(
            MenuEntry(
                key="cancel", label=t("action.cancel", lang), description="Keep current language"
            )
        )

        chosen = self._menu.prompt(entries, default_key="cancel")
        if chosen == "cancel" or chosen == current_lang:
            return

        updated = replace(
            self.context.settings,
            ui=replace(self.context.settings.ui, language=chosen),
        )
        if self._persist_settings(updated):
            set_current_language(chosen)
            target_name = SUPPORTED_LANGUAGES.get(chosen, chosen)
            self.context.notifications.notify(
                t("dialog.language_changed", chosen, language=target_name),
                NotificationLevel.SUCCESS,
            )

    # ---------------------------------------------------------------- Privacy & Chat

    async def _privacy_menu(self) -> None:
        lang = self.context.settings.ui.language

        def entries() -> list[MenuEntry]:
            settings = self.context.settings
            lang_now = settings.ui.language
            chat = settings.chat
            return [
                MenuEntry(
                    key="toggle_receipts",
                    label=t("settings.read_receipts.label", lang_now),
                    description=t("settings.read_receipts.desc", lang_now),
                    value=self._on_off(chat.read_receipts, lang_now),
                ),
                MenuEntry(
                    key="toggle_typing",
                    label=t("settings.typing_indicators.label", lang_now),
                    description=t("settings.typing_indicators.desc", lang_now),
                    value=self._on_off(chat.typing_indicators, lang_now),
                ),
                MenuEntry(
                    key="toggle_presence",
                    label=t("settings.presence.label", lang_now),
                    description=t("settings.presence.desc", lang_now),
                    value=self._on_off(chat.presence, lang_now),
                ),
                MenuEntry(
                    key="history_mode",
                    label=t("settings.history_mode.label", lang_now),
                    description=t("settings.history_mode.desc", lang_now),
                    value=chat.history_mode.title(),
                ),
                MenuEntry(
                    key="toggle_cleanup",
                    label=t("settings.auto_cleanup.label", lang_now),
                    description=t("settings.auto_cleanup.desc", lang_now),
                    value=self._on_off(chat.cleanup_on_exit, lang_now),
                ),
                MenuEntry(
                    key="display_name",
                    label=t("settings.display_name.label", lang_now),
                    description=t("settings.display_name.desc", lang_now),
                    value=chat.display_name or t("status.per_run_default", lang_now),
                ),
                MenuEntry(
                    key="identity_keys",
                    label=t("settings.identity.label", lang_now),
                    description=t("settings.identity.desc", lang_now),
                ),
            ]

        async def receipts() -> None:
            self._toggle_chat("read_receipts", t("settings.read_receipts.label", lang))

        async def typing() -> None:
            self._toggle_chat("typing_indicators", t("settings.typing_indicators.label", lang))

        async def presence() -> None:
            self._toggle_chat("presence", t("settings.presence.label", lang))

        async def history() -> None:
            await self._select_history_mode()

        async def cleanup() -> None:
            self._toggle_chat("cleanup_on_exit", t("settings.auto_cleanup.label", lang))

        async def name() -> None:
            await self._edit_display_name()

        async def identity() -> None:
            await self._identity_menu()

        await self._run_submenu(
            t("settings.cat.privacy.title", lang),
            t("settings.cat.privacy.desc", lang),
            entries,
            {
                "toggle_receipts": receipts,
                "toggle_typing": typing,
                "toggle_presence": presence,
                "history_mode": history,
                "toggle_cleanup": cleanup,
                "display_name": name,
                "identity_keys": identity,
            },
        )

    async def _identity_menu(self) -> None:
        lang = self.context.settings.ui.language
        while True:
            self.header(t("settings.identity.label", lang), t("settings.identity.desc", lang))
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
                    label=t("settings.fingerprint.label", lang),
                    description="The GLFP fingerprint peers verify out-of-band",
                    value=fingerprint_display[:16] + "…",
                ),
                MenuEntry(
                    key="rotate_keys",
                    label=t("settings.rotate.label", lang),
                    description="Generate a fresh identity keypair",
                ),
                MenuEntry.spacer(),
                self.back_menu_entry(),
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
        self.header(t("settings.fingerprint.label", self.context.settings.ui.language))
        console.print(
            kv_grid(
                [
                    ("Fingerprint", Text(fingerprint, style="gl.highlight")),
                    (
                        "Verification",
                        Text(
                            "Compare out-of-band with contacts to verify end-to-end encryption.",
                            style="gl.muted",
                        ),
                    ),
                ]
            )
        )
        await self.pause()

    async def _rotate_identity(self) -> None:
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
            console.print(notice_dialog("Identity rotation failed", str(exc), tone=BadgeTone.ERROR))
            await self.pause()

    async def _select_history_mode(self) -> None:
        settings = self.context.settings
        lang = settings.ui.language
        current_mode = settings.chat.history_mode

        descriptions = {
            "disabled": "Ephemeral — conversations live in memory only",
            "session": "Retained in memory for the active session",
            "encrypted": "Encrypted in passphrase-protected local storage",
        }

        self.header(
            t("dialog.history_select.title", lang),
            t("settings.history_mode.desc", lang),
        )
        entries: list[MenuEntry] = []
        for mode in sorted(CHAT_HISTORY_MODES):
            entries.append(
                MenuEntry(
                    key=mode,
                    label=mode.title(),
                    description=descriptions.get(mode, mode),
                    value=t("status.current", lang) if mode == current_mode else "",
                )
            )
        entries.append(MenuEntry.spacer())
        entries.append(
            MenuEntry(key="cancel", label=t("action.cancel", lang), description="Keep current mode")
        )

        chosen = self._menu.prompt(entries, default_key="cancel")
        if chosen == "cancel" or chosen == current_mode:
            return
        updated = replace(settings, chat=replace(settings.chat, history_mode=chosen))
        if self._persist_settings(updated):
            self.context.notifications.notify(
                t(
                    "dialog.setting_saved",
                    lang,
                    setting=f"{t('settings.history_mode.label', lang)}: {chosen.title()}",
                ),
                NotificationLevel.SUCCESS,
            )

    async def _edit_display_name(self) -> None:
        console = self.context.console
        lang = self.context.settings.ui.language
        current_name = self.context.settings.chat.display_name

        self.header(
            t("settings.display_name.label", lang),
            f"Current: {current_name or t('status.per_run_default', lang)}",
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
                    t("dialog.setting_saved", lang, setting=f"Display name: {name}"),
                    NotificationLevel.SUCCESS,
                )
            else:
                self.context.notifications.notify(
                    t("dialog.name_cleared", lang),
                    NotificationLevel.INFO,
                )

    # ---------------------------------------------------------------- Notifications

    async def _notifications_menu(self) -> None:
        lang = self.context.settings.ui.language

        def entries() -> list[MenuEntry]:
            settings = self.context.settings
            lang_now = settings.ui.language
            notif = settings.notifications
            state = (
                t("status.enabled", lang_now) if notif.enabled else t("status.disabled", lang_now)
            )
            return [
                MenuEntry(
                    key="toggle",
                    label=t("settings.notifications_enabled.label", lang_now),
                    description=t("settings.notifications_enabled.desc", lang_now),
                    value=state.upper(),
                ),
                MenuEntry(
                    key="toggle_messages",
                    label=t("settings.notify_messages.label", lang_now),
                    description=t("settings.notify_messages.desc", lang_now),
                    value=self._on_off(notif.messages, lang_now),
                ),
                MenuEntry(
                    key="toggle_room",
                    label=t("settings.notify_room.label", lang_now),
                    description=t("settings.notify_room.desc", lang_now),
                    value=self._on_off(notif.room_activity, lang_now),
                ),
                MenuEntry(
                    key="toggle_invites",
                    label=t("settings.notify_invites.label", lang_now),
                    description=t("settings.notify_invites.desc", lang_now),
                    value=self._on_off(notif.invites, lang_now),
                ),
                MenuEntry(
                    key="toggle_sound",
                    label=t("settings.notify_sound.label", lang_now),
                    description=t("settings.notify_sound.desc", lang_now),
                    value=self._on_off(notif.sound, lang_now),
                ),
                MenuEntry(
                    key="toggle_vibration",
                    label=t("settings.notify_vibration.label", lang_now),
                    description=t("settings.notify_vibration.desc", lang_now),
                    value=self._on_off(notif.vibration, lang_now),
                ),
                MenuEntry(
                    key="style",
                    label=t("settings.notification_style.label", lang_now),
                    description=t("settings.notification_style.desc", lang_now),
                    value=settings.chat.notification_style.title(),
                ),
            ]

        async def master() -> None:
            self._toggle_notifications("enabled", t("settings.notifications_enabled.label", lang))

        async def messages() -> None:
            self._toggle_notifications("messages", t("settings.notify_messages.label", lang))

        async def room() -> None:
            self._toggle_notifications("room_activity", t("settings.notify_room.label", lang))

        async def invites() -> None:
            self._toggle_notifications("invites", t("settings.notify_invites.label", lang))

        async def sound() -> None:
            self._toggle_notifications("sound", t("settings.notify_sound.label", lang))

        async def vibration() -> None:
            self._toggle_notifications("vibration", t("settings.notify_vibration.label", lang))

        async def style() -> None:
            await self._select_notification_style()

        await self._run_submenu(
            t("settings.cat.notifications.title", lang),
            t("settings.cat.notifications.desc", lang),
            entries,
            {
                "toggle": master,
                "toggle_messages": messages,
                "toggle_room": room,
                "toggle_invites": invites,
                "toggle_sound": sound,
                "toggle_vibration": vibration,
                "style": style,
            },
        )

    async def _select_notification_style(self) -> None:
        settings = self.context.settings
        lang = settings.ui.language
        current_style = settings.chat.notification_style

        descriptions = {
            "banner": "Prominent bordered alert banners",
            "compact": "Single-line status notifications",
            "muted": "Silent — only errors are displayed",
        }

        self.header(
            t("dialog.style_select.title", lang),
            t("settings.notification_style.desc", lang),
        )
        entries: list[MenuEntry] = []
        for style_name in sorted(CHAT_NOTIFICATION_STYLES):
            entries.append(
                MenuEntry(
                    key=style_name,
                    label=style_name.title(),
                    description=descriptions.get(style_name, style_name),
                    value=t("status.current", lang) if style_name == current_style else "",
                )
            )
        entries.append(MenuEntry.spacer())
        entries.append(
            MenuEntry(
                key="cancel", label=t("action.cancel", lang), description="Keep current style"
            )
        )

        chosen = self._menu.prompt(entries, default_key="cancel")
        if chosen == "cancel" or chosen == current_style:
            return
        updated = replace(settings, chat=replace(settings.chat, notification_style=chosen))
        if self._persist_settings(updated):
            self.context.notifications.notify(
                t(
                    "dialog.setting_saved",
                    lang,
                    setting=f"{t('settings.notification_style.label', lang)}: {chosen.title()}",
                ),
                NotificationLevel.SUCCESS,
            )

    # ---------------------------------------------------------------- Network & Relay

    async def _network_menu(self) -> None:
        lang = self.context.settings.ui.language

        def entries() -> list[MenuEntry]:
            settings = self.context.settings
            lang_now = settings.ui.language
            relay_display = settings.relay.url.strip() or t("settings.relay.local", lang_now)
            return [
                MenuEntry(
                    key="configure_relay",
                    label=t("settings.relay_url.label", lang_now),
                    description=t("settings.relay_url.desc", lang_now),
                    value=relay_display,
                ),
                MenuEntry(
                    key="test_connectivity",
                    label=t("settings.relay.test.label", lang_now),
                    description=t("settings.relay.test.desc", lang_now),
                ),
                MenuEntry(
                    key="clear_relay",
                    label=t("settings.relay.clear.label", lang_now),
                    description=t("settings.relay.clear.desc", lang_now),
                ),
            ]

        async def configure() -> None:
            await self._configure_relay()

        async def probe() -> None:
            await self._test_connectivity()

        async def clear() -> None:
            await self._clear_relay()

        await self._run_submenu(
            t("settings.cat.network.title", lang),
            t("settings.cat.network.desc", lang),
            entries,
            {"configure_relay": configure, "test_connectivity": probe, "clear_relay": clear},
        )

    async def _configure_relay(self) -> None:
        console = self.context.console
        lang = self.context.settings.ui.language
        current_url = self.context.settings.relay.url

        self.header(
            t("settings.relay_url.label", lang),
            f"Current: {current_url or t('settings.relay.local', lang)}",
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
                    "Invalid relay URL",
                    f"The endpoint '{url}' must start with ws:// or wss://.",
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
                    t("dialog.setting_saved", lang, setting=f"Relay: {url}"),
                    NotificationLevel.SUCCESS,
                )
            else:
                self.context.notifications.notify(
                    t("dialog.relay_cleared", lang),
                    NotificationLevel.INFO,
                )

    async def _test_connectivity(self) -> None:
        console = self.context.console
        lang = self.context.settings.ui.language
        relay = self.context.settings.relay
        url = relay.url.strip()

        self.header(t("settings.relay.test.label", lang), url or t("settings.relay.local", lang))
        if not url:
            relay_label = t("settings.relay_url.label", lang)
            network_label = t("settings.cat.network.title", lang)
            self.context.console.print(
                notice_dialog(
                    "No relay configured",
                    "GhostLink is running in local room mode — there is nothing to probe.",
                    tone=BadgeTone.INFO,
                    hint=f"Set a relay under {network_label} → {relay_label}.",
                )
            )
            await self.pause()
            return

        from ghostlink.transport.relay.client import RelayClientConfig, probe_relay
        from ghostlink.ui.dashboards import render_relay_dashboard

        console.print(Text(f"Connecting to {url}…", style="gl.muted"))
        try:
            report = await probe_relay(
                url,
                client_name=f"{APP_NAME}/{APP_VERSION}",
                config=RelayClientConfig(
                    connect_timeout_seconds=relay.connect_timeout_seconds,
                    handshake_timeout_seconds=relay.handshake_timeout_seconds,
                    heartbeat_interval_seconds=relay.heartbeat_interval_seconds,
                    heartbeat_timeout_seconds=relay.heartbeat_timeout_seconds,
                    reconnect_attempts=relay.reconnect_attempts,
                    reconnect_base_delay_seconds=relay.reconnect_base_delay_seconds,
                ),
                pings=2,
            )
        except Exception as exc:
            console.newline()
            console.print(
                notice_dialog(
                    "Connection failed",
                    f"Unable to connect to the configured relay.\nReason: {exc}",
                    tone=BadgeTone.ERROR,
                    hint="Check the relay URL and network access, then try again.",
                )
            )
            await self.pause()
            return
        render_relay_dashboard(console, report)
        await self.pause()

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
            confirm_action,
            console.console,
            t("dialog.confirm_clear_relay_title", lang),
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
        lang = self.context.settings.ui.language

        def entries() -> list[MenuEntry]:
            settings = self.context.settings
            lang_now = settings.ui.language
            configured = settings.storage.data_dir.strip()
            display = configured if configured else f"{t('status.default', lang_now)}"
            return [
                MenuEntry(
                    key="set_datadir",
                    label=t("settings.data_dir.label", lang_now),
                    description=t("settings.data_dir.desc", lang_now),
                    value=display,
                ),
                MenuEntry(
                    key="open_storage",
                    label=t("settings.storage_overview.label", lang_now),
                    description=t("settings.storage_overview.desc", lang_now),
                ),
                MenuEntry(
                    key="reset_datadir",
                    label=t("settings.storage_reset.label", lang_now),
                    description=t("settings.storage_reset.desc", lang_now),
                ),
            ]

        async def set_dir() -> None:
            await self._configure_data_dir()

        async def open_overview() -> None:
            from ghostlink.ui.screens.storage import StorageManagerScreen

            await StorageManagerScreen(self.context).show()

        async def reset_dir() -> None:
            await self._reset_data_dir()

        await self._run_submenu(
            t("settings.cat.storage.title", lang),
            t("settings.cat.storage.desc", lang),
            entries,
            {"set_datadir": set_dir, "open_storage": open_overview, "reset_datadir": reset_dir},
        )

    async def _configure_data_dir(self) -> None:
        console = self.context.console
        lang = self.context.settings.ui.language

        self.header(
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
                        "Invalid directory path",
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
            data_label = t("settings.data_dir.label", lang)
            shown = path_str or t("status.default", lang)
            self.context.notifications.notify(
                t("dialog.setting_saved", lang, setting=f"{data_label}: {shown}"),
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
                t(
                    "dialog.setting_saved",
                    lang,
                    setting=f"{t('settings.data_dir.label', lang)}: {t('status.default', lang)}",
                ),
                NotificationLevel.SUCCESS,
            )

    # ---------------------------------------------------------------- Developer & System

    async def _developer_menu(self) -> None:
        lang = self.context.settings.ui.language

        def entries() -> list[MenuEntry]:
            settings = self.context.settings
            lang_now = settings.ui.language
            return [
                MenuEntry(
                    key="toggle_debug",
                    label=t("settings.debug_mode.label", lang_now),
                    description=t("settings.debug_mode.desc", lang_now),
                    value=self._on_off(settings.diagnostics.debug, lang_now),
                ),
                MenuEntry(
                    key="view_system_info",
                    label=t("settings.system_info.label", lang_now),
                    description=t("settings.system_info.desc", lang_now),
                ),
            ]

        async def toggle_debug() -> None:
            settings = self.context.settings
            new_debug = not settings.diagnostics.debug
            updated = replace(settings, diagnostics=replace(settings.diagnostics, debug=new_debug))
            if self._persist_settings(updated):
                status = self._on_off(new_debug, lang)
                self.context.notifications.notify(
                    f"{t('settings.debug_mode.label', lang)}: {status}",
                    NotificationLevel.WARNING if new_debug else NotificationLevel.INFO,
                )

        async def system_info() -> None:
            await self._view_system_info()

        await self._run_submenu(
            t("settings.cat.developer.title", lang),
            t("settings.cat.developer.desc", lang),
            entries,
            {"toggle_debug": toggle_debug, "view_system_info": system_info},
        )

    async def _view_system_info(self) -> None:
        context = self.context
        console = context.console
        env = context.environment
        lang = context.settings.ui.language

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

        self.header(t("settings.system_info.label", lang), "read-only")
        console.print(facts)
        console.newline()
        console.print(
            Text(
                "Values reflect the active host environment and runtime configuration.",
                style="gl.muted",
            )
        )
        await self.pause()
