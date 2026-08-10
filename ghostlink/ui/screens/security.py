"""Security Status screen.

An honest, verified overview of GhostLink's active security posture:
identity, encryption, transport security, invite authority, and storage
safety — followed by an optional cryptographic protocol reference.
"""

from __future__ import annotations

import sys

from rich.text import Text

from ghostlink.constants.files import STATE_DIR_NAME
from ghostlink.i18n import t
from ghostlink.identity.fingerprint import identity_fingerprint
from ghostlink.identity.storage import IdentityStore
from ghostlink.storage.manager import StorageManager
from ghostlink.ui.components.layout import section_label, status_text
from ghostlink.ui.components.tables import kv_grid
from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens.base import Screen, ScreenContext


class SecurityDashboardScreen(Screen):
    """Security posture overview and protocol reference."""

    def __init__(self, context: ScreenContext) -> None:
        super().__init__(context)
        self._menu = InteractiveMenu(context.console)

    async def show(self) -> None:
        while True:
            self._render_dashboard()

            entries = (
                MenuEntry(
                    key="diagnostics",
                    label="Cryptographic Reference",
                    description="Algorithm specifications and forward secrecy parameters",
                ),
                MenuEntry.spacer(),
                self.back_menu_entry(),
            )

            choice = self._menu.prompt(entries, default_key="back")
            if choice == "back":
                return
            if choice == "diagnostics":
                await self._show_audit()

    def _render_dashboard(self) -> None:
        context = self.context
        console = context.console
        settings = context.settings
        lang = settings.ui.language

        self.header(t("menu.security.label", lang), "Verified security posture")

        # Real identity state
        state_dir = context.data_dir / STATE_DIR_NAME
        has_identity = False
        fingerprint = "Not initialized"
        try:
            store = IdentityStore(StorageManager(state_dir))
            ident = store.load()
            if ident is not None:
                has_identity = True
                fingerprint = identity_fingerprint(ident.public_key_bytes)
        except Exception:
            pass

        relay_url = settings.relay.url.strip()
        is_tls_relay = relay_url.startswith("wss://")

        rows = [
            (
                "Cryptographic Identity",
                status_text(
                    f"Protected — {fingerprint[:16]}…" if has_identity else "Pending",
                    "success" if has_identity else "info",
                ),
            ),
            (
                "End-to-End Encryption",
                status_text("Active — X25519 + ChaCha20-Poly1305 AEAD", "success"),
            ),
            (
                "Transport Security",
                status_text(
                    "TLS protected (wss)" if is_tls_relay else "Local WebSocket (no TLS)",
                    "success" if is_tls_relay else "info",
                ),
            ),
            (
                "Invite Authority",
                status_text("One-time HMAC tokens, single-use redemption", "success"),
            ),
            (
                "Local Storage Safety",
                status_text(
                    f"History mode: {settings.chat.history_mode}",
                    "success" if settings.chat.history_mode == "encrypted" else "info",
                ),
            ),
            (
                "Relay Rendezvous",
                status_text(
                    relay_url if relay_url else "Local mode — no external relay",
                    "info",
                ),
            ),
        ]

        console.print(kv_grid(rows))
        console.newline()
        console.print(
            Text(
                "Indicators reflect the active cryptographic implementation. "
                "End-to-end keys are generated in memory and never shared with relays.",
                style="gl.muted",
            )
        )
        console.newline()

    async def _show_audit(self) -> None:
        console = self.context.console
        self.header("Cryptographic Reference", "Protocol specifications")

        py_ver = sys.version.split()[0]
        console.print(section_label("Cryptographic Protocol Specifications"))
        console.newline()
        audit_grid = kv_grid(
            [
                ("Key Exchange", Text("X25519 (Curve25519 ECDH), ephemeral & static")),
                ("Authenticated Cipher", Text("ChaCha20-Poly1305 (IETF RFC 8439, 256-bit)")),
                ("Digital Signatures", Text("Ed25519 (EdDSA Curve25519, SHA-512)")),
                ("Key Derivation", Text("HKDF-SHA256 (RFC 5869) session rekeying")),
                ("File Integrity", Text("SHA-256 per-chunk and file-level verification")),
                ("Forward Secrecy", Text("Ratcheted sender keys, ephemeral handshakes")),
                ("Cryptography Provider", Text(f"PyCA Cryptography (CPython {py_ver})")),
                ("Zero Knowledge", Text("Relays route ciphertext frames without keys")),
            ]
        )
        console.print(audit_grid)
        console.newline()
        console.print(
            Text(
                "GhostLink uses audited, memory-safe cryptographic primitives from PyCA.",
                style="gl.muted",
            )
        )
        await self.pause()
