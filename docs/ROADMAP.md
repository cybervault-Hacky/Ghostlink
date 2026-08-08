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

## Phase 3 — Secure Messaging ✅

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

## Phase 4 — Secure File Transfer ✅

- Transfer domain model with a strict lifecycle: `offered → accepted →
  transferring → completed`, with `queued / paused` and terminal
  `rejected / cancelled / failed / expired` — every transition validated on
  both sides
- Ten new secure-channel frames (`FILE_OFFER / FILE_ACCEPT / FILE_REJECT /
  FILE_CHUNK / FILE_ACK / FILE_PAUSE / FILE_RESUME / FILE_CANCEL /
  FILE_COMPLETE / FILE_ERROR`), schema-validated in both directions inside
  the same v1 envelope the chat uses
- Sealed manifests: filename, size, chunk geometry, SHA-256 and protocol
  version travel encrypted — the relay never learns a name or a byte count,
  and the manifest never contains the sender's local path
- Per-transfer keys: HKDF-SHA256 sub-keys derived from the live session key
  (domain-separated by transfer id), refreshed automatically every rekey;
  every chunk is ChaCha20-Poly1305-sealed with `transfer_id|chunk_number`
  as associated data — tampering, truncation, replay and mis-numbering are
  detected, never silently accepted
- Streaming chunk I/O: files are read and written chunk-by-chunk at bounded
  memory (no whole-file buffering), receiver writes land at their offsets
  directly, out-of-order and duplicate chunks handled (duplicates re-acked,
  never rewritten)
- Sliding-window pump with per-chunk ACKs, bounded retries, timeouts and
  backpressure; the receiver's chunk bitmap is the resume source of truth
  on every reconnect — verified chunks are never retransmitted
- Final SHA-256 verification before publish: mismatch → FAILED, partial
  temp deleted, peer informed; success → atomic rename into the download
  directory (visible only after verification), `0600` permissions
- Pause / resume mid-flight (`/pause`, `/resume`), link-loss auto-pause with
  automatic resume after reconnection, offer/transfer expiry, and orphan
  temp cleanup on startup
- Hostile-name defense: filenames sanitized (all separator styles + unicode
  lookalikes, control chars, reserved device names, length caps), no path
  traversal, no absolute writes, collision-proof `name (2).ext` destinations,
  downloads only inside the dedicated GhostLink directory — the peer can
  never choose a destination
- Configurable limits with validation: max file size, concurrent transfers,
  chunk size, ack timeout, retry limit, expiry, temp quota — bad values fail
  launch with hinted errors
- Terminal UX: incoming-offer panel with Y/N keyboard answer, throttled
  `bar · % · bytes · speed · ETA` progress lines (narrow-Termux safe),
  saved-to confirmations, and commands `/send /transfers /transfer /
  /accept /reject /pause /resume /cancel` (unique id prefixes)
- Transfer history as metadata only (filename, size, direction, status,
  timestamp, id) — folded into all three history modes, encrypted mode
  included; contents and keys never retained
- 200+ new tests including live sender→relay→receiver transfers, relay
  restarts with resume, corruption/tamper injection, and CLI-end-to-end runs

## Phase 5 — Ephemeral Identity & One-Time Invites ✅ (current)

- Ephemeral local identities: an Ed25519 keypair generated on your device,
  stored `0600`, never uploaded; a short handle (`GL-…`) and an optional
  nickname — no email, no phone, no accounts, no profiles
- Identity fingerprints (`GLFP-XXXX-XXXX-XXXX`) derived from the public
  key only; identity keys are bound into the handshake transcript so a
  relay swapping keys fails the session; `/fingerprint` and
  `ghostlink identity fingerprint` support out-of-band comparison for
  stronger authentication (not an anonymity feature)
- One-time join invites: `gl://join/<token>` minted in the terminal —
  20-character, ~103-bit CSPRNG tokens (never timestamp-derived), shown
  exactly once; `invite list` / `invite info` expose metadata only
- Relay-authoritative invite lifecycle v3 (`INVITE_CREATE / INVITE_QUERY /
  INVITE_REVOKE / INVITE_REDEEM`): expiration on the relay's monotonic
  clock, atomic check-and-consume single redemption (concurrent attempts
  yield exactly one winner), creator-only revocation, fail-closed verdicts
  for unknown / expired / revoked / already-used tokens, and session
  binding from invite to the conversation it created
- Invite lifecycle states `CREATED → ACTIVE → REDEEMING → REDEEMED` with
  terminal `EXPIRED / REVOKED`; invalid transitions fail safely, terminal
  records are purged on a retention policy
- CLI + chat commands: `identity show|fingerprint|nickname`,
  `invite create|list|info|revoke`, `join gl://join/<token>`, and in-chat
  `/identity`, `/fingerprint`, `/invite [list|revoke]`
- 160+ new tests including live two-session invite flows, 1s/2s expirations
  against a real relay, concurrent-redemption races, revocation, peer
  fingerprint display, and log secret-leak audits

## Phase 6 — Rich Communication

- Friend system: adding, verifying safety numbers, blocking
- Group rooms beyond the one-to-one channel
- Message replies, edits, and reactions
- Contact cards and room metadata panels
- Settings screen becomes editable; notification center gains unread state

## Phase 7 — Hardening & Polish

- Full security review and threat-model document
- Offline message queue and multi-device sync design
- Localization framework activation (beyond `en`)
- Plugin hooks for room automations

## Explicitly out of scope

- GUI or web clients — GhostLink is terminal-only, by design.
- Federated public rooms — GhostLink targets private, invite-only spaces.
- Voice/video/screen sharing — the terminal is the medium.
