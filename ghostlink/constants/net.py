"""Networking protocol constants (Phase 2: Secure Networking)."""

from __future__ import annotations

PROTOCOL_VERSION: int = 2
SUPPORTED_PROTOCOL_VERSIONS: frozenset[int] = frozenset({1, 2})

WEBSOCKET_GUID: str = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
WEBSOCKET_VERSION: str = "13"
MAX_MESSAGE_BYTES: int = 1_048_576
MAX_CONTROL_PAYLOAD: int = 125
MAX_HANDSHAKE_BYTES: int = 16_384

DEFAULT_RELAY_PORT: int = 8787
SERVER_NAME: str = "ghostlink-relay"

CONNECT_TIMEOUT_SECONDS: float = 5.0
HANDSHAKE_TIMEOUT_SECONDS: float = 5.0
HEARTBEAT_INTERVAL_SECONDS: float = 10.0
HEARTBEAT_TIMEOUT_SECONDS: float = 5.0
RECONNECT_ATTEMPTS: int = 3
RECONNECT_BASE_DELAY_SECONDS: float = 1.0
RECONNECT_MAX_DELAY_SECONDS: float = 30.0
RECONNECT_JITTER: float = 0.25

RELAY_SESSION_TTL_SECONDS: float = 300.0
SESSION_SWEEP_INTERVAL_SECONDS: float = 30.0

ROOM_ID_PREFIX: str = "gl-room"
ROOM_ID_ALPHABET: str = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
INVITE_TOKEN_PREFIX: str = "gli_"

DEFAULT_ROOM_LIFETIME_MINUTES: int = 60
DEFAULT_INVITE_LIFETIME_MINUTES: int = 15

# ----------------------------------------------------------------- messaging
# A FORWARD body carries one base64-encoded secure-channel frame.
MAX_FORWARD_PAYLOAD_BYTES: int = 8_192
MAX_FORWARD_BODY_LENGTH: int = 10_924  # ceil(8192 / 3) * 4 — exact b64 ceiling
CHANNEL_ROLES: frozenset[str] = frozenset({"host", "guest"})
CHANNEL_CAPACITY: int = 2
PEER_EVENTS: frozenset[str] = frozenset({"joined", "left"})
