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
"""

from __future__ import annotations

__version__ = "0.3.0"
__all__ = ["__version__", "version_info"]

_major, _minor, _patch = (int(part) for part in __version__.split("."))
version_info: tuple[int, int, int] = (_major, _minor, _patch)
