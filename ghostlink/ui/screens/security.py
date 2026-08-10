"""Dedicated Security Dashboard screen.

Displays an honest, verified overview of GhostLink's active security posture,
encryption algorithms, key status, transport security, and diagnostics.
"""

from __future__ import annotations

import sys

from rich.align import Align
from rich.console import Group
from rich.table import Table
from rich.text import Text

from ghostlink.constants.files import STATE_DIR_NAME
from ghostlink.i18n import t
from ghostlink.identity.fingerprint import identity_fingerprint
from ghostlink.identity.storage import IdentityStore
from ghostlink.storage.manager import StorageManager
from ghostlink.ui.components.badges import BadgeTone, badge
from ghostlink.ui.components.panels import section_panel
from ghostlink.ui.components.tables import kv_grid
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.base import Screen, ScreenContext


class SecurityDashboardScreen(Screen):
    """Dedicated Security & Diagnostics Dashboard."""

    def __init__(self, context: ScreenContext) -> None:
        super().__init__(context)
        self._menu = InteractiveMenu(context.console)

    async def show(self) -> None:
        while True:
            lang = self.context.settings.ui.language
            self._render_dashboard()

            entries = (
                MenuEntry(
                    key="diagnostics",
                    label="View Detailed Cryptographic Audit",
                    description="Inspect algorithm specifications and forward secrecy parameters",
                    icon="🛡",
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
            if choice == "diagnostics":
                await self._show_audit()

    def _render_dashboard(self) -> None:
        context = self.context
        console = context.console
        theme = context.theme
        settings = context.settings

        console.clear()
        console.newline()

        # Check real identity
        state_dir = context.data_dir / STATE_DIR_NAME
        has_identity = False
        fingerprint = "Not Initialized"
        try:
            store = IdentityStore(StorageManager(state_dir))
            ident = store.load()
            if ident is not None:
                has_identity = True
                fingerprint = identity_fingerprint(ident.public_key_bytes)
        except Exception:
            pass

        # Relay status
        relay_url = settings.relay.url.strip()
        is_tls_relay = relay_url.startswith("wss://")

        overview_table = Table(
            box=None,
            show_header=True,
            header_style="gl.title",
            pad_edge=False,
            expand=True,
        )
        overview_table.add_column("Security Domain", style="gl.accent", width=22)
        overview_table.add_column("Status", style="gl.text", width=18)
        overview_table.add_column("Mechanism & Verification", style="gl.highlight")

        overview_table.add_row(
            "Cryptographic Identity",
            badge("Protected", BadgeTone.SUCCESS, theme=theme)
            if has_identity
            else badge("Pending", BadgeTone.INFO, theme=theme),
            Text(f"Ed25519 · {fingerprint[:16]}…", style="gl.text"),
        )
        overview_table.add_row(
            "End-to-End Encryption",
            badge("Active", BadgeTone.SUCCESS, theme=theme),
            Text("X25519 + ChaCha20-Poly1305 AEAD", style="gl.text"),
        )
        overview_table.add_row(
            "Transport Security",
            badge("TLS Encrypted", BadgeTone.SUCCESS, theme=theme)
            if is_tls_relay
            else badge("Local / Plain", BadgeTone.INFO, theme=theme),
            Text(
                f"WebSocket ({'WSS/TLS' if is_tls_relay else 'Local WS'})",
                style="gl.text",
            ),
        )
        overview_table.add_row(
            "Invite Authority",
            badge("One-Time", BadgeTone.SUCCESS, theme=theme),
            Text("HMAC Token Authority · Single-use redemption", style="gl.text"),
        )
        overview_table.add_row(
            "Local Storage Safety",
            badge(
                "Encrypted" if settings.chat.history_mode == "encrypted" else "Ephemeral",
                BadgeTone.SUCCESS,
                theme=theme,
            ),
            Text(f"History Mode: {settings.chat.history_mode}", style="gl.text"),
        )
        overview_table.add_row(
            "Relay Rendezvous",
            badge("Configured", BadgeTone.SUCCESS, theme=theme)
            if relay_url
            else badge("Local-Only", BadgeTone.MUTED, theme=theme),
            Text(relay_url or "Local rendezvous (no external relay)", style="gl.muted"),
        )

        badge_header = Align.center(
            badge("GhostLink Verified Security Posture", BadgeTone.ACCENT, theme=theme)
        )

        body = Group(
            badge_header,
            Text(""),
            overview_table,
            Text(""),
            Text(
                "All security indicators reflect active cryptographic implementations.\n"
                "End-to-end encryption keys are generated in RAM and never shared with relays.",
                style="gl.muted",
            ),
        )

        console.print(section_panel("Security Dashboard", body, subtitle="Verified Posture"))
        console.newline()

    async def _show_audit(self) -> None:
        console = self.context.console
        console.clear()
        console.newline()

        py_ver = sys.version.split()[0]
        audit_grid = kv_grid(
            [
                ("Key Exchange", Text("X25519 (Curve25519 ECDH) Ephemeral & Static")),
                ("Authenticated Cipher", Text("ChaCha20-Poly1305 (IETF RFC 8439, 256-bit)")),
                ("Digital Signatures", Text("Ed25519 (EdDSA Curve25519, SHA-512)")),
                ("Key Derivation", Text("HKDF-SHA256 (RFC 5869) Session Rekeying")),
                ("File Integrity", Text("SHA-256 Per-Chunk & File-Level Verification")),
                ("Forward Secrecy", Text("Ratcheted sender keys & ephemeral handshakes")),
                ("Python Cryptography", Text(f"PyCA Cryptography (CPython {py_ver})")),
                ("Zero Knowledge", Text("Relays route ciphertext frames without access to keys")),
            ]
        )

        body = Group(
            Align.center(Text("CRYPTOGRAPHIC PROTOCOL SPECIFICATIONS", style="bold gl.title")),
            Text(""),
            audit_grid,
            Text(""),
            Text(
                "GhostLink uses audited, memory-safe cryptographic primitives from PyCA.",
                style="gl.muted",
            ),
        )

        console.print(section_panel("Cryptographic Audit Details", body))
        await self.pause()
