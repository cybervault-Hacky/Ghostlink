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

# ------------------------------------------------- group messaging (Phase 6C)
# Pairwise-mesh E2E messaging per docs/GROUPS.md §17-§22: every limit below is
# enforced in the domain/authority layer, never only in the UI (§32).
GROUP_MSG_MAX_BYTES: int = 4096  # UTF-8 plaintext ceiling (§32)
GROUP_FANOUT_MAX: int = MAX_GROUP_MEMBERS - 1  # envelopes per send (§32)
GROUP_SEEN_IDS_PER_SENDER: int = 512  # (sender, message_id) dedupe LRU
GROUP_GSEQ_MAX_GAP: int = 64  # bounded reorder tolerance (§22)
GROUP_OFFLINE_QUEUE_PER_MEMBER: int = 8  # FIFO drop-oldest (§29)
GROUP_FORWARD_BODY_MAX: int = 6144  # sealed frame b64 ceiling on the wire
GROUP_KEX_BODY_MAX: int = 2048  # handshake payload ceiling
GROUP_MESSAGE_ID_PREFIX: str = "gmsg_"
GROUP_MESSAGE_ID_HEX: int = 16
GROUP_AEAD_FAILURES_NOTICE: int = 3  # suspect-link UI threshold (§31)
GROUP_FORWARD_RATE_PER_SECOND: float = 20.0  # relay flood brake (§32)
GROUP_FORWARD_RATE_BURST: int = 20
GROUP_EVENT_RATE_OPS: int = 4  # membership ops per window (§32)
GROUP_EVENT_RATE_WINDOW_SECONDS: float = 10.0
GROUP_KEX_TIMEOUT_SECONDS: float = 10.0
GROUP_LEDGER_KEPT: int = 64  # delivery ledgers retained per group
GROUP_SEQ_STATE_MAX: int = 8 * 4  # gseq cursors (defensive cap)

# ------------------------------------------------- group sender keys (Phase 7)
# Sender-key hardening (docs/GROUPS.md §36-§37). A group's `crypto_suite`
# selects its message-encryption path: `mesh-v1` (Phase 6C pairwise fanout,
# backward compatible) or `senderkey-v1` (O(1) per-message seal + sender-key
# distribution over the pairwise mesh). The suite is relay-authoritative and
# never downgrades silently (§37).
GROUP_CRYPTO_SUITES: frozenset[str] = frozenset({"mesh-v1", "senderkey-v1"})
DEFAULT_CRYPTO_SUITE: str = "mesh-v1"  # Phase 6C default; Phase 7 is additive
# Max out-of-order message keys cached per sender (bounded skipped-key cache).
GROUP_SK_SKIPPED_MAX: int = 64
# Distribution carries the chain root + current index; the root is never sent
# as plaintext — it rides a pairwise-link AEAD envelope.
GROUP_SK_ROOT_BYTES: int = 32
GROUP_SK_DISTRIBUTION_BODY_MAX: int = 1024  # GSK inner-frame body ceiling
# Defensive cap on incoming receiver-chain state (groups x members upper bound).
GROUP_SK_MAX_RECEIVER_STATE: int = 8 * 8
# Phase 8 — sender-key request (GSKREQ) abuse brake: at most this many
# key-pull requests per (group, requester) per window before drops.
GROUP_SKREQ_RATE_OPS: int = 4
GROUP_SKREQ_RATE_WINDOW_SECONDS: float = 10.0
# Phase 8 — bounded pending-buffer ceilings for sender-key recovery.
GROUP_SK_PENDING_BUCKETS: int = 64
GROUP_SK_PENDING_PER_SENDER: int = 16

# ---------------------------------------------------- relay abuse brakes (8)
# In-memory, never persisted. The relay remains a lightweight routing /
# authority component; these bounds prevent a single abusive source from
# exhausting the process (docs/GROUPS.md §32, Phase 8 §10).
RELAY_MAX_CONNECTIONS: int = 1024  # hard cap on concurrent live clients
RELAY_CONNECT_RATE_PER_SECOND: float = 40.0  # per-source-IP token bucket
RELAY_CONNECT_BURST: int = 80  # burst tolerance for legitimate bursts
