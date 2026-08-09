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

## Phase 5 — Ephemeral Identity & One-Time Invites ✅

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

## Phase 6A — Group Security Design ✅

**STATUS: DESIGN COMPLETE** — security model documented and reviewed in
[docs/GROUPS.md](GROUPS.md); implementation proceeded in Phase 6B.

Direction: secure multi-peer communication (groups beyond the one-to-one
channel), prioritized ahead of the other Phase 6 items. The full
specification — threat model, trust model, membership authorization,
epochs, pairwise-mesh encryption, envelope/AAD binding, replay and
sequence rules, forward/backward secrecy analysis, relay visibility,
resource limits, failure handling, future sender-key path, migration,
testing strategy, implementation checklist, and open questions — lives in
[docs/GROUPS.md](GROUPS.md).

Selected design:

- Private, invite-only groups of **up to 8 members** on the existing relay
  (quadratic mesh arithmetic, Termux screens, and auditability drive the
  cap — rationale documented in GROUPS.md §32)
- Full membership lifecycle: create → invite → redeem → join → leave →
  remove → dissolve, with unambiguous per-operation authorization, a
  relay-authoritative roster, and owner-signed membership events
- **Encryption model: pairwise mesh over the existing Phase 3 stack**
  (X25519 + HKDF-SHA256 + ChaCha20-Poly1305, mandatory identity binding,
  group/epoch-bound keys, sender-side fanout). A shared/copied group key
  is explicitly rejected; sender keys are the documented Phase 7
  candidate, not part of 6A
- Group epochs: every roster mutation increments the epoch; keys are
  epoch-bound; removed members lose future epochs, joiners gain no past
- Relay protocol v4 (`GROUP_*`), additive and version-negotiated; the
  relay routes opaque ciphertext and non-secret routing metadata only and
  never receives plaintext, keys, or message ids
- Termux-grade, explicit resource limits; a §5 threat model covering ten
  attacker classes with no overstated claims; a 15-category loopback
  test strategy mirroring the Phase 5 bar

## Phase 6B — Secure Group Lifecycle ✅

**STATUS: IMPLEMENTED** — the membership lifecycle foundation of
docs/GROUPS.md. Group messaging/encryption landed in Phase 6C (below),
built entirely on this foundation.

- Groups (`gl-group-XXXX-XXXX-XXXX`, up to 8 members): owner creates with
  an Ed25519 proof-of-possession over the relay-issued attestation
  challenge; the relay mints the collision-free id; owner is member #1
  at epoch 1
- Relay-authoritative roster (`GroupAuthority`): the single source of
  truth for membership and epochs; every authorization check happens in
  one await-free critical section, so concurrent joins/leaves/removals
  serialize deterministically with no lost updates, no duplicate members,
  and no duplicate epochs
- Epochs start at 1 and increment by exactly one per committed roster
  mutation; clients can never pick, skip, roll back, or reuse an epoch;
  local records reject stale/gapped events and mark suspect state for
  re-sync
- Signed membership events: join/removed/dissolved are signed by the
  pinned owner key, `left` by the leaving member; admissions are
  countersigned by the owner while online (`ghostlink group host`), so a
  candidate can never self-admit; sign requests carry a 60-second
  timeout, pending admissions 300 seconds
- Group invites on the Phase 5 authority (`kind=group`): owner-only
  minting, relay-side capacity checks before token consumption (a
  full-group verdict never burns an invite), single atomic redemption
  under concurrency, one-time semantics and expiry unchanged
- Roster-pinning at redemption (§12.2): every member's public key ↔
  fingerprint binding is verified before any state is trusted
- Local records are metadata-only (fingerprints, handles, public keys,
  epochs, signed event descriptors) in `groups.json` — atomic writes,
  `0600`, corruption-safe typed errors, success-only join persistence,
  retention purging of terminal records, and a defunct state for
  post-restart relays (authority state is volatile by design, §28.4)
- Relay protocol v4 (`GROUP_*` family) with version gating: v1–v3
  clients are untouched and refused group access as
  `protocol/unsupported`; no existing packet family changed meaning
- CLI: `ghostlink group create|list|info|invite|join|leave|remove|
  dissolve|sync|host` — a thin layer over the domain manager; all
  security decisions live in the service layer and exit via typed errors
  (exit code 8)
- 168 new tests including seven concurrent joins to the 8-member cap,
  9th-member refusal, concurrent invite redemption, forced signature
  failures, restart/defunct, storage corruption, log/store secret-hygiene
  audits, and full CLI end-to-end runs against a live relay

## Phase 6C — Group Messaging + Pairwise-Mesh Encryption ✅

**STATUS: IMPLEMENTED** — end-to-end group messaging exactly as
docs/GROUPS.md specifies: pairwise mesh, no sender keys (Phase 7).
Every group message is encrypted separately for each authorized
recipient; the relay routes opaque ciphertext envelopes only.

- Pairwise links reuse the Phase 3 stack verbatim — X25519,
  HKDF-SHA256 (transcript salt, `ghostlink/session/v1` info),
  ChaCha20-Poly1305 with random 96-bit nonces — no new primitives, no
  shared group key, no new ratchet
- Handshake context `ghostlink/group/v1|{group}|{epoch}` with
  identity-key binding in both directions (substituted `idpub` refuses
  the link); deterministic initiator by smaller identity key, with a
  demand *knock* so responder-role members can pull a pairing too
- AEAD AAD binds `group-msg/v1 | group | epoch | sender | recipient`;
  the sealed inner frame re-carries the same context, cross-checked
  after decrypt — cross-group/epoch/sender/recipient ciphertext always
  fails authentication, and 1:1-chat ciphertext can never open as group
  traffic
- Sender gates in the domain layer: only ACTIVE members in the current
  epoch may send; removed/departed/unknown/stale senders are refused
  before any ciphertext exists; fanout ≤ 7, never to a non-roster
  member
- Replay defense: `(sender_fp, message_id)` LRU (512/sender),
  memory-scoped per-sender `gseq` cursors with bounded-gap flagging
  (≤ 64) and suspect-drop beyond; duplicates re-ACK but never re-render
- Epoch drain exactly per §16.5: current epoch, or previous epoch
  inside a real 30 s window with the old link still alive — then
  reject-and-log; removed members hold no new-epoch links; joiners
  gain no history
- Relay extension is minimal: `GROUP_FORWARD` is validated for framing,
  routing, and size, ACL-checked (both endpoints ACTIVE on the
  authoritative roster, epochs {e, e−1}), rate-limited (20/s per
  group+sender), and forwarded **opaque** — no relay-side queue, no
  plaintext, keys, message ids, or sequence numbers; offline members
  yield `group/offline`, overload yields `group/rate`, both correlated
  to the refused recipient
- Offline members: bounded sender-side queue (8/job FIFO, drop-oldest
  with explicit failed markings), fast failure on relay `group/offline`,
  lazy re-pairing with force-refreshed keys on reconnect, and re-seal
  to the *current* epoch before flush; relay restart ⇒ groups defunct
- Delivery is per-recipient fanout: QUEUED/SENDING/SENT/DELIVERED/READ/
  FAILED with `delivered k/m` always explicit — the UI never reports
  full delivery for partial fanout; GACK per message id, GREAD
  cumulative per gseq (both sealed on the same links)
- Terminal group chat UI + `ghostlink group chat`:
  banner with name/id, member count, epoch, mesh-link and encryption
  status, latency; message blocks with attribution to the authenticated
  fingerprint; delivery lines; security notices; gap warnings;
  `/help /info /members /fingerprint /delivery /invite /leave /history
  /export /quit`; history reuses the existing store (off / session /
  encrypted modes, existing caps, no keys)
- 118 new tests: cross-context AEAD matrices, replay/rollback/gap
  tables, member removal and new-member isolation, offline bounds and
  flush, reconnect re-keying with zeroization proofs, relay
  flood/rate/abuse limits, malformed-envelope matrices, log/secret
  hygiene, full eight-member loopback fanout, and the CLI/TUI surfaces
  against a live relay

## Phase 7 — Sender-Key Hardening ✅ (latest implemented)

**STATUS: IMPLEMENTED** — O(1) group-message encryption on opt-in
`senderkey-v1` groups, exactly as docs/GROUPS.md §36 specifies: a
per-sender forward-evolving hash-ratchet chain per (group, epoch),
distributed over the existing pairwise mesh, one ChaCha20-Poly1305 seal
per sender message broadcast to the whole roster.

- **Sender-key chains** reuse the Phase 3 stack only (X25519 for the mesh
  distribution channel, HKDF-SHA256 for the one-way ratchet, ChaCha20-
  Poly1305 for sealing) with the `ghostlink/group/senderkey/v1` domain
  separation; message-key derivation binds group/epoch/sender/gen/index
- **Distribution** is a `GSK` inner frame sealed over an identity-bound
  pairwise link (AAD binds group/epoch/sender/recipient/gen), generation-
  scoped, with a `GSKREQ` pull path; the relay never sees a chain key,
  message key, or plaintext
- **Epoch scoping** is authoritative: any roster mutation prunes every
  old-epoch chain and mints + redistributes fresh ones; removed members
  get no new-epoch keys, joiners get no history (one-way ratchet)
- **Replay / out-of-order**: per-(sender, epoch, generation) index
  monotonicity rejects replays; a bounded skipped-key cache (≤ 64/sender)
  heals 10→8→9 delivery; jumps beyond the window are rejected, not
  allocated; id-LRU + gseq gates still run on the decrypted frame
- **Relay behavior** unchanged: routes opaque `sk`/`skmsg` envelopes, enforces
  ACL/rate, never inspects content
- **Suite negotiation**: relay-authoritative `crypto_suite` (`mesh-v1`
  default for backward compatibility, `senderkey-v1` opt-in) minted at
  creation and advertised on redeem/attest/roster; a mismatch on an
  established group is refused (no silent downgrade)
- **CLI/UI**: `ghostlink group create --crypto-suite senderkey-v1`, an
  in-chat `/security` command, and suite-aware banners — no secrets shown
- **Hygiene**: sender keys live in zeroizable `bytearray` slots in memory
  only; never logged, stored, or in exceptions; honest §36.7 forward/
  backward-secrecy statement (no overclaiming)
- **New tests**: 17 sender-key unit tests (chain, window, distribution,
  store) + 18 sender-key loopback e2e tests (fanout/broadcast, removal,
  joiner isolation, reconnect, replay/tamper, offline retry, suite wiring,
  relay-opaqueness) — full suite green at 1318 passing

## Phase 6D — Rich Communication

- Friend system: adding, verifying safety numbers, blocking
- Message replies, edits, and reactions
- Contact cards and room metadata panels
- Settings screen becomes editable; notification center gains unread state

## Phase 8 — Hardening & Polish

- Full security review and threat-model document
- Offline message queue and multi-device sync design
- Localization framework activation (beyond `en`)
- Plugin hooks for room automations

## Explicitly out of scope

- GUI or web clients — GhostLink is terminal-only, by design.
- Federated public rooms — GhostLink targets private, invite-only spaces.
- Voice/video/screen sharing — the terminal is the medium.
