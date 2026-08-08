# GhostLink Roadmap

GhostLink ships in deliberate phases. Each phase is self-contained, tested,
and production-quality before the next begins.

## Phase 1 — Foundation ✅

- Adaptive ASCII branding with theme gradients
- Automatic environment detection (Termux / Linux / Python / terminal)
- Strictly validated TOML configuration with first-run generation
- Professional logging (rotating file, debug console mode)
- Interactive menu: Create Room · Join Room · Settings · About · Exit
- Read-only Settings and About screens
- Reusable component library (panels, tables, badges, dialogs, toasts, progress)
- Exception framework with deterministic exit codes
- Storage abstraction (atomic JSON + in-memory backends)
- Service container and async application lifecycle
- `ghostlink --doctor` read-only diagnostics

## Phase 2 — Secure Networking ✅

- `transport/` layer: `Transport` abstraction, connection state machine with
  legal-transition enforcement, heartbeat monitor, expiring sessions with an
  automatic sweeper
- Own RFC 6455 WebSocket implementation (handshakes, masking, fragmentation,
  control frames, close semantics) — zero compiled dependencies
- Relay wire protocol v1: `HELLO` / `WELCOME` / `PING` / `PONG` /
  `DISCONNECT` / `ERROR` / `HEARTBEAT`, validated in both directions
- Relay client: handshake, reconnection with exponential backoff + jitter,
  RTT measurement, graceful disconnect
- Reference relay server: `python -m ghostlink.transport.relay.server`
- Rooms: canonical IDs (`gl-room-XXXX-XXXX-XXXX`), lifetimes, states —
  durable models persisted atomically
- Invites: one-time tokens (`gli_…`), custom lifetimes, redemption and
  revocation semantics
- CLI commands: `host`, `join`, `relay-status`, `session`, `doctor`
- Live network dashboards: latency sparkline, jitter, heartbeat panel,
  connection timeline — rendered from real measurements
- Menu flows wired for real: Create Room hosts; Join Room validates

## Phase 3 — Secure Messaging ✅ (current)

- Relay protocol v2: rendezvous channels with `ATTACH` / `DETACH` /
  `FORWARD` / `PEER` packets, capacity-two rooms, scoped errors, precise
  payload limits; the relay only ever routes ciphertext
- Authenticated key exchange inside the channel: two-message handshake,
  X25519 (ephemeral per conversation) + HKDF-SHA256 bound to the full
  transcript, AEAD key-confirmation proof, out-of-band safety code
- End-to-end encryption: ChaCha20-Poly1305 per message, fresh session key
  per conversation (and per reconnect), keys held in memory only and
  zeroized on close; tampering fails loudly
- Message model with a strict lifecycle: `queued → sending → sent →
  delivered → read` (plus `failed`), unique ids, validation, redacted logging
- Secure-channel frames: `MESSAGE`, `MESSAGE_ACK`, `READ_RECEIPT`,
  `TYPING_START`, `TYPING_STOP`, `ERROR` (+ `KEX_HELLO` / `KEX_REPLY`) —
  every frame schema-validated in both directions
- Chat session orchestration: outgoing pump queue, incoming ordering with a
  self-healing reorder buffer, duplicate detection (re-acked, never
  re-delivered), bounded resends, requeue-and-reseal after reconnection
- Delivery acknowledgements, optional read receipts (setting-gated,
  coalesced cursors), throttled typing indicators with expiry
- History modes: **disabled** (default) · **session** (memory-only) ·
  **encrypted** (passphrase → scrypt → ChaCha20-Poly1305, atomic writes,
  `0600` permissions)
- Terminal chat surface: status/encryption/latency banner, wrapped message
  blocks with delivery glyphs, typing and connection notices, unread
  tracking, multi-line composer, arrow-key input history, graceful Ctrl+C,
  and local commands `/help /info /clear /history /export /exit`
- CLI: `host` and `join` now open the live chat (with `--relay`, `--as`);
  menu Create/Join flows drop straight into the conversation when a relay
  is configured
- 600+ tests, including full two-party conversations over a real relay

## Phase 4 — Rich Communication

- File transfer (chunked, resumable, integrity-checked)
- Friend system: adding, verifying safety numbers, blocking
- Group rooms beyond the one-to-one channel
- Message replies, edits, and reactions
- Contact cards and room metadata panels
- Settings screen becomes editable; notification center gains unread state

## Phase 5 — Hardening & Polish

- Full security review and threat-model document
- Offline message queue and multi-device sync design
- Localization framework activation (beyond `en`)
- Plugin hooks for room automations

## Explicitly out of scope

- GUI or web clients — GhostLink is terminal-only, by design.
- Federated public rooms — GhostLink targets private, invite-only spaces.
- Voice/video/screen sharing — the terminal is the medium.
