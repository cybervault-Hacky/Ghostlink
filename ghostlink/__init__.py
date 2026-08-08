"""GhostLink — a terminal-only encrypted messenger for Termux and Linux.

Phase 1 (Foundation) delivers the application shell: startup branding,
environment detection, configuration management, logging, storage
abstraction, theming, and the interactive menu system.

Phase 2 (Secure Networking) adds the real relay protocol, WebSocket
transport, connection management with supervised reconnection, heartbeat
and latency measurement, rooms, invites, and live status dashboards.

Phase 3 (Secure Messaging) adds authenticated end-to-end encryption
(fresh session key per conversation), validated message frames, delivery
acknowledgements and read receipts, typing indicators, optional message
history (off / session-only / encrypted-at-rest), and the interactive
terminal chat surface — all in the terminal, nothing leaves it.

Phase 4 (Secure File Transfer) adds chunked, end-to-end encrypted file
transfer over the same secure channel: sealed manifests, per-transfer
HKDF sub-keys, AEAD-sealed chunks with per-chunk acknowledgements and
a sliding send window, receiver-side bitmap resumes across reconnects,
SHA-256 verification before a byte becomes visible, and traversal-proof
downloads into a dedicated GhostLink directory.

Phase 5 (Ephemeral Identity & One-Time Invites) adds local, Ed25519
ephemeral identities (GL-… handles with GLFP-… verification
fingerprints, no accounts, no email or phone) and secure one-time join
invites: gl://join/<token> links minted locally, enforced by the relay
authority — monotonic-clock expiration, atomic single redemption,
creator-only revocation, and binding of each invite to the session it
creates. Peer fingerprints can be compared inside the chat for stronger
authentication.
"""

from __future__ import annotations

__version__ = "0.5.0"
__all__ = ["__version__", "version_info"]

_major, _minor, _patch = (int(part) for part in __version__.split("."))
version_info: tuple[int, int, int] = (_major, _minor, _patch)
