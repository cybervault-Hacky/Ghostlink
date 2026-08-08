"""Networking protocol constants (Phase 2: Secure Networking)."""

from __future__ import annotations

PROTOCOL_VERSION: int = 4
SUPPORTED_PROTOCOL_VERSIONS: frozenset[int] = frozenset({1, 2, 3, 4})
# Clients must speak at least relay protocol 3 to drive invite operations.
INVITE_PROTOCOL_VERSION: int = 3
# Clients must speak at least relay protocol 4 to drive group operations.
GROUP_PROTOCOL_VERSION: int = 4

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

# ------------------------------------------------- one-time invites (Phase 5)
# join-link tokens use the room alphabet: 20 chars ≈ 103 bits of entropy.
INVITE_LINK_SCHEME: str = "gl"
INVITE_LINK_HOST: str = "join"
INVITE_LINK_TOKEN_LENGTH: int = 20
INVITE_ID_PREFIX: str = "gi_"
INVITE_MIN_TTL_SECONDS: float = 1.0
INVITE_MAX_TTL_SECONDS: float = 86_400.0
INVITE_DEFAULT_TTL_SECONDS: int = 900
INVITE_DEFAULT_MAX_REDEMPTIONS: int = 1
INVITE_MAX_REDEMPTIONS: int = 16
INVITE_RETENTION_HOURS: int = 24
MAX_INVITE_FIELD_LENGTH: int = 96

# ----------------------------------------------------------------- messaging
# A FORWARD body carries one base64-encoded secure-channel frame.
MAX_FORWARD_PAYLOAD_BYTES: int = 8_192
MAX_FORWARD_BODY_LENGTH: int = 10_924  # ceil(8192 / 3) * 4 — exact b64 ceiling
CHANNEL_ROLES: frozenset[str] = frozenset({"host", "guest"})
CHANNEL_CAPACITY: int = 2
PEER_EVENTS: frozenset[str] = frozenset({"joined", "left"})

# ------------------------------------------------- secure groups (Phase 6B)
# Lifecycle foundation per docs/GROUPS.md. Group messaging/encryption is a
# later stage; these limits govern membership, epochs and roster state only.
GROUP_ID_PREFIX: str = "gl-group"
MAX_GROUP_MEMBERS: int = 8
GROUP_MAX_GROUPS: int = 32
GROUP_NAME_MAX_LEN: int = 48
GROUP_DISPLAY_NAME_MAX_LEN: int = 24
GROUP_MAX_ACTIVE_INVITES: int = 8
GROUP_JOIN_PENDING_SECONDS: float = 300.0
GROUP_SIGN_TIMEOUT_SECONDS: float = 60.0
GROUP_EPOCH_DRAIN_SECONDS: float = 30.0
GROUP_EVENTS_KEPT: int = 16
GROUP_MAX_PENDING_OPS: int = 8
GROUP_PUBKEY_HEX_LENGTH: int = 64
GROUP_SIGNATURE_B64_MAX: int = 128
GROUP_OP_ID_LENGTH: int = 16
GROUP_RETENTION_HOURS: int = 24
