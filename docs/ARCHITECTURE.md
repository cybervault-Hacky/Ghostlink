# GhostLink Architecture

> Phases 1–3 — Foundation, Secure Networking, and Secure Messaging. This
> document describes the structural contract every later phase builds upon.

## 1. Design goals

| Goal | How the codebase enforces it |
| --- | --- |
| **Modularity** | Each layer owns one directory; dependencies point strictly inward. |
| **Async readiness** | `Application.run` and every screen/service lifecycle hook is a coroutine driven by `asyncio.run`. |
| **Testability** | Environment, paths, and console geometry are injected, never read globally; tests use real loopback sockets, not mocks. |
| **Fail loud, recover clearly** | All expected errors derive from `GhostLinkError` with messages, hints, and exit codes. |
| **Minimal network footprint** | One outbound WebSocket to the configured relay only. Nothing listens, beacons, or phones home. |
| **Zero compiled hot paths** | The WebSocket layer is implemented over `asyncio` streams; the only native code is the audited `cryptography` AEAD/KDF underneath the messaging layer. |
| **Keys in memory only** | Session keys are derived per conversation, never persisted, and overwritten before release. |

## 2. Layer map

```
┌────────────────────────────────────────────────────────────────┐
│ ghostlink.py  →  python -m ghostlink  →  pip entrypoint         │
└──────────────────────────────┬─────────────────────────────────┘
                               ▼
┌────────────────────────────────────────────────────────────────┐
│ cli/         arguments.py · commands/ · doctor · entrypoint     │
│              Parses flags, routes subcommands, owns exit codes  │
└──────────────────────────────┬─────────────────────────────────┘
                               ▼
┌────────────────────────────────────────────────────────────────┐
│ core/        bootstrap.py → builds the object graph             │
│              application.py → async run loop                    │
│              environment.py → platform/Termux/terminal probing  │
│              logging.py     → rotating file + debug console     │
└──────┬──────────────┬───────────────┬───────────────┬──────────┘
       ▼              ▼               ▼               ▼
┌────────────┐ ┌────────────┐ ┌─────────────┐ ┌──────────────────┐
│ config/    │ │ storage/   │ │ services/   │ │ ui/               │
│ validation │ │ JSON/Memory│ │ container + │ │ console · themes  │
│ overrides  │ │ atomic IO  │ │ session +   │ │ banner · menu     │
│            │ │            │ │ rooms       │ │ components · scr. │
└─────┬──────┘ └─────┬──────┘ └──────┬──────┘ │ dashboards/charts│
      └──────────────┴───────┬───────┴────────┴───────┬──────────┘
                             ▼                        ▼
┌────────────────────────────────────────────────────────────────┐
│ messaging/  protocol/ (kex + AEAD) · packets/ (frame codec) ·   │
│             models/ (message lifecycle) · queue/ (outbox +      │
│             inbox ordering) · session/ (ChatSession) ·          │
│             receipts · typing · history                         │
├──────────────────────────────┬─────────────────────────────────┤
│ transport/   Transport ABC · connection state machine ·         │
│              heartbeat monitor · sessions with sweeper          │
│              ├─ websocket/  RFC 6455 framing + transport        │
│              └─ relay/      packets v3 · channels · invite      │
│                             authority · endpoint · client ·     │
│                             reference server                    │
└──────────────────────────────┬─────────────────────────────────┘
                               ▼
┌────────────────────────────────────────────────────────────────┐
│ models/ · constants/ · exceptions/ · utils/ · assets/           │
│ Shared vocabulary: typed models, metadata, errors, helpers      │
└────────────────────────────────────────────────────────────────┘
```

**Dependency rule:** layers may only depend on layers below them.
`ui` never imports `core`; `transport/` depends only on the shared
vocabulary below it — it never renders or touches configuration. The CLI and
menu own transport lifecycles; dashboards render measurements they are
handed (`RelayProbeReport`, room and invite models). `messaging/` sits on
top of `transport/`: it speaks to a `RelayClient` through channels and never
opens a socket itself; `ui/chat.py` renders `ChatEvent`s and owns nothing
but presentation. `identity/` depends only on storage and the shared
vocabulary; `invites/` adds the relay client as a deferred (cycle-free)
dependency, and the relay server hosts the invite authority in-process.

## 3. Bootstrap sequence

`core/bootstrap.build_application(CLIOptions)` constructs the runtime in a
strict order, registering every singleton in the `ServiceContainer`:

1. `EnvironmentDetector.detect()` — platform, Python, terminal, color, TTY.
2. `ConfigurationManager.load()` — generate-on-first-run, strict TOML
   validation, CLI overrides merged last.
3. `ensure_directories()` — config, data, state, and log directories exist
   before anything writes.
4. `setup_logging()` — rotating file handler; Rich stderr handler only in
   Debug Mode.
5. `ThemeEngine.get(settings.ui.theme)` — unknown themes fail with a list of
   valid names.
6. `ConsoleManager(theme, no_color=…)` — single console for the process.
7. `StorageManager(state_dir)` + `SessionService` — durable JSON stores.
8. `ScreenContext` assembly; bootstrap notices (first run, unsupported
   platform, etc.) are **deferred** and rendered once, after the first banner,
   so startup output is never interleaved mid-screen.

Phase 2 extended step 7 with the `RoomService` (rooms + invites stores) and
registered it in the `ScreenContext`; transports are deliberately **not**
singletons — the CLI commands and menu flows construct and own them per
operation. Phase 3 keeps that shape: the chat runner
(`ui/chat.py::run_chat_session`) owns the relay client, the `ChatSession`,
and the history backend per conversation, and is shared verbatim by the CLI
commands and the menu flows.

## 4. Async model

```
entrypoint.main()
    └── asyncio.run(Application.run())          # single event loop
            ├── SessionService.start()          # async lifecycle hook
            ├── HomeScreen.show()               # async screen loop
            │       ├── asyncio.to_thread(menu.prompt)     # blocking input off-loop
            │       ├── SettingsScreen.show() / AboutScreen.show()
            │       └── pause() → to_thread(wait_for_enter)
            └── SessionService.stop()           # always runs (finally)
```

Blocking terminal input is isolated behind `asyncio.to_thread`, so the loop
stays responsive and Phase 2 transports can run beside the UI without
restructuring it.

## 5. Configuration pipeline

```
packaged default (assets/default_config.toml)
        │  written to disk on first launch
        ▼
user file (config.toml)  ──strict validate──▶  unknown keys/sections rejected
        ▼
settings models (frozen dataclasses, __post_init__ validation)
        ▼
CLI overrides (--theme, --data-dir, --debug)   ← highest precedence
        ▼
AppSettings (immutable for the whole run)
```

## 6. Error taxonomy

| Exception | Exit code | Raised when |
| --- | --- | --- |
| `ConfigurationError` family | 2 | TOML invalid, unknown keys, bad values, unwritable config dir |
| `UnsupportedPlatformError` family | 3 | Non-Linux platform, Python below 3.11 (hard gate) |
| `StorageError` family | 4 | Corrupt JSON, write failures, invalid namespaces |
| `HistoryError` family | 4 | Locked/unreadable/wrong-passphrase encrypted history |
| `ThemeNotFoundError` | 5 | Theme name not registered |
| `TransportError` family | 6 | Relay unreachable, timeout, handshake or packet violation, relay error |
| `MessagingError` family | 6 | Frame validation, secure-channel handshake, integrity (AEAD) failures, peer unavailable |
| `InviteError` family | 7 | Unknown/expired/revoked/used invite, invite permission or state failures |
| `GroupError` family | 8 | Unknown group, not-owner/not-member, group full, stale epoch, suspect state, join timeout |
| `ServiceNotRegisteredError` | 1 | Container misuse (programming error) |
| any other exception | 1 | Rendered as “Unexpected error”, traceback in log/Debug Mode |
| `KeyboardInterrupt` outside menu | 130 | Ctrl+C |

`exceptions/handler.render_exception` is the only code path that turns
exceptions into terminal output, which guarantees identical error panels in
every failure scenario.

## 7. Storage layer

`StorageBackend` is a `MutableMapping[str, Any]`; backends implement only
`_read_all` / `_write_all`:

- `JsonFileStorage` — one document per file, atomic `tempfile → fsync →
  os.replace` writes, `0600` permissions, corruption detected with operator
  hints.
- `MemoryStorage` — deep-copying, thread-safe, for tests and ephemeral state.

`StorageManager` hands out cached, validated namespaces (`session`, `rooms`,
and `invites` today; `identity`, `keys` in Phase 3) rooted at
`<data_dir>/state`.

## 8. UI system

- **Theme engine** — every color lives in an immutable `ThemeSpec`; components
  use semantic `gl.*` styles projected onto a Rich `Theme`. Four themes ship:
  `phantom` (default), `emerald`, `ember`, `mono`.
- **Banner** — three ASCII variants chosen by measured terminal width,
  colorised with a per-character gradient interpolated from the theme.
- **Menu** — arrow-key navigation on real terminals (`simple-term-menu`);
  automatic numbered fallback when input is piped or the package is absent.
- **Components** — panels, tables, badges, dialogs, notifications, progress.
  Screens compose these primitives and never hand-roll chrome.

## 9. Logging

- Root logger `ghostlink`, child loggers via `get_logger("ui.menu")` etc.
- Rotating file: `<data_dir>/logs/ghostlink.log`, 512 KiB × 3 backups,
  INFO by default, DEBUG when Debug Mode is enabled.
- Rich stderr handler attaches only in Debug Mode — the interactive UI is
  never polluted by log noise.
- File-logging failure degrades to a warning notice; it never crashes the app.
- Networking logs live under `transport.*` (`transport.relay.client`,
  `transport.connection.relay`, `transport.heartbeat`, …) — DEBUG captures
  every state transition, packet, heartbeat, and RTT sample.
- Messaging logs live under `messaging.*` and are **content-free by design**:
  ids, sequence numbers, sizes, and states only. Plaintext and ciphertext
  are never logged; the message model's log view is redacted by contract.

## 10. Transport layer (Phase 2)

The networking stack is built in four tiers, each independently testable:

```
┌─────────────────────────────────────────────────────────┐
│ relay/        RelayClient: HELLO→WELCOME handshake,             │
│               ping/RTT futures, ERROR cache, probe              │
│               RelayServer: reference relay (dev/tests)          │
├─────────────────────────────────────────────────────────┤
│ connection.py ConnectionManager: state machine enforcing        │
│               legal transitions, reader loop, backoff           │
│               reconnection, lifecycle stats                     │
├─────────────────────────────────────────────────────────┤
│ heartbeat.py  HeartbeatMonitor: keepalive cadence,              │
│               nonce pings, RTT samples, missed-limit            │
├─────────────────────────────────────────────────────────┤
│ transport.py  Transport ABC · websocket/ own RFC 6455           │
│               framing (masking, fragmentation, control          │
│               frames, close semantics) over raw streams         │
└─────────────────────────────────────────────────────────┘
```

- **WebSocket framing** (`websocket/protocol.py`) implements the RFC 6455
  client and server handshakes and the frame codec itself — masking
  enforcement, reserved-bit checks, control-frame rules, payload lengths up
  to 64-bit, and a close handshake that never leaks the underlying socket.
- **`WebSocketTransport`** adapts that codec to the `Transport` ABC:
  `open/send/receive/close` plus byte/message counters. Future backends
  (Tor, QUIC, raw TLS) slot in behind the same four methods.
- **`ConnectionManager`** owns one *logical* connection across successive
  physical transports: CONNECTING → CONNECTED → (RECONNECTING) →
  DISCONNECTED → CLOSED, with an explicit transition table — illegal moves
  raise instead of corrupting state. Reconnection uses exponential backoff
  with ±25 % jitter, bounded attempts, and deterministic reader teardown.
- **`HeartbeatMonitor`** drives protocol keepalives and nonce-stamped pings,
  keeps bounded RTT samples (latest/avg/min/max), and escalates three
  consecutive missed pings into one supervised recovery event.
- **Sessions** (`transport/session.py`) hold creation/activity clocks, TTL
  expiry, and metadata; the `SessionRegistry` sweeps overdue sessions on a
  timer. The relay server enforces TTL; the client holds its own session.
- **Relay protocol** (`relay/protocol.py`) is a versioned JSON envelope
  (`{"v", "id", "type", "ts", "payload"}`) with per-type schema validation
  applied symmetrically to inbound and outbound packets — malformed traffic
  can never reach application code.

Everything above is exercised over real loopback sockets in the test suite;
no part of the stack is a stub or a fake.

## 11. Messaging layer (Phase 3)

The chat stack sits *above* the relay channel and treats the relay as an
untrusted courier that only ever forwards ciphertext.

```
┌─────────────────────────────────────────────────────────┐
│ session/chat.py   ChatSession: orchestrates everything          │
│    pump · dispatch · handshake · reconnect · cleanup            │
├─────────────────────────────────────────────────────────┤
│ queue/            Outbox: FIFO + lifecycle + ack                │
│                   waiters + requeue-on-reconnect                │
│                   Inbox:  strict ordering, duplicate            │
│                   detection, self-healing reordering            │
├─────────────────────────────────────────────────────────┤
│ packets/frames.py Secure-channel frames (v1 envelope):          │
│    KEX_HELLO/KEX_REPLY · MESSAGE · MESSAGE_ACK ·                │
│    READ_RECEIPT · TYPING_START/STOP · ERROR — all               │
│    schema-validated in both directions                          │
├─────────────────────────────────────────────────────────┤
│ protocol/         handshake.py: 2-message authenticated         │
│    key exchange (X25519 ephemeral + HKDF bound to the           │
│    transcript + AEAD key confirmation)                          │
│                   crypto.py: ChaCha20-Poly1305 AEAD,            │
│    per-message nonces, transcript safety code                   │
├─────────────────────────────────────────────────────────┤
│ relay channels (transport/ protocol v2)                         │
│    ATTACH/DETACH join a capacity-2 room; FORWARD ships          │
│    opaque bodies to the counterparty; PEER events               │
│    report join/leave; channel-scoped ERRORs                     │
└─────────────────────────────────────────────────────────┘
```

**Trust model.** The relay learns *that* two clients talk on a channel,
never *what* they say. KEX frames carry only public keys/nonces; MESSAGE
frames carry only AEAD ciphertext. Peers compare the transcript safety code
out-of-band to rule out a relay-level MITM.

**Key lifecycle.** Every conversation starts with a fresh X25519 ephemeral
pair per side; the shared secret feeds HKDF-SHA256 whose salt is the hash of
the full handshake transcript (channel + both public keys + both nonces).
The responder proves the same derivation by sealing a transcript commitment
under it. Reconnects trigger a brand-new handshake — pending messages are
re-queued, then sealed under the *new* key before resend. On close, key
bytes are overwritten before references drop.

**Message lifecycle.** `QUEUED → SENDING → SENT → DELIVERED → READ`, with
`FAILED` as the terminal failure state and re-queue transitions reserved for
connection loss. Every transition is validated; illegal moves raise.
Delivery is end-to-end: the sender side waits for a `MESSAGE_ACK` (bounded
resends), and `READ_RECEIPT`s are cumulative cursors — one receipt covers a
whole backlog and can be disabled entirely in settings.

**History.** Off by default. `session` mode keeps an in-memory transcript
wiped on close. `encrypted` mode persists it passphrase-locked
(scrypt → ChaCha20-Poly1305, atomic `0600` writes, tamper-evident). A wrong
passphrase fails with a clear, dedicated error — never a silent reset.

**UI contract.** `ui/chat.py` renders a stream of `ChatEvent`s (session,
message, delivery, typing, peer, connection, notice); it never sees frames,
keys, or transport details, and every rendering path escapes user content.

## 12. Transfer layer (Phase 4)

File transfer is a **sub-protocol of the secure channel**, not a second
network stack. `ChatSession` routes validated `FILE_*` frames to a
registered delegate and lends its live session key; everything file-shaped
lives in `transfer/`.

```
┌─────────────────────────────────────────────────────────┐
│ manager.py     TransferManager: offers · accept/reject          │
│    · sliding-window send pump (ACKs, bounded retries,           │
│    timeouts, backpressure) · pause/resume · auto-pause          │
│    on link loss + bitmap resume after rekey · expiry            │
│    sweeper · concurrency cap + queue · history record           │
├─────────────────────────────────────────────────────────┤
│ models.py        Transfer lifecycle state machine               │
│    (offered/…/terminal), immutable snapshots for UI             │
│ manifest.py      Sealed file metadata (no local paths)          │
│    with cross-checked validation on receipt                     │
│ chunking.py      ChunkReader (seek-on-demand streaming)         │
│    + ChunkBitmap (received/acked truth, b64 resume)             │
│ integrity.py     HKDF per-transfer keys from the                │
│    session key · seal/open chunk (AAD id|n) · stream            │
│    SHA-256 of source and finished temp                          │
├─────────────────────────────────────────────────────────┤
│ packets.py       FILE_* frame builders (validated by            │
│    the Phase 3 codec — size-bounded, schema-checked)            │
├─────────────────────────────────────────────────────────┤
│ storage.py       Temp .part hygiene (0600, state dir) ·         │
│    filename sanitization · traversal-proof, collision-          │
│    resistant destinations · temp quota · atomic rename          │
│ cleanup.py       Orphan .part deletion on startup               │
│ progress.py      format_bytes · SpeedMeter · width-             │
│    aware bars/lines/tables (pure presentation)                  │
└─────────────────────────────────────────────────────────┘
```

**Key hygiene.** Per-transfer keys are HKDF-SHA256 sub-keys of the live
conversation key, domain-separated by `ghostlink/transfer/v1|<id>`, so
chunks never share a key context with chat messages or another transfer.
They are re-derived after every session rekey (the manager wipes its cache
on `SESSION Active`) and zeroized on terminal states and close.

**Resume protocol.** The receiver marks verified chunks in a bitmap *before*
acking; on any re-offer or `FILE_RESUME` it replies `FILE_ACCEPT{bm}` (or
`FILE_COMPLETE` if finished). The sender adopts that bitmap — never its own
ACK log — so verified chunks are never resent, and stale-session ciphertext
fails AEAD instead of corrupting state.

**Failure taxonomy.** Local validation errors (`TransferValidationError`),
lifecycle errors (`TransferStateError`), and limit breaches
(`TransferLimitError`) surface as clean UI lines; remote faults arrive as
`FILE_ERROR`. Integrity failures are always loud: FAILED + temp purge +
peer notice. Receiving never publishes bytes that were not hash-verified.

## 13. Identity & Invites (Phase 5)

Phase 5 adds two terminal-local subsystems that slot into the existing
relay and chat stack without a second network or storage system.

```
┌───────────────────────────────────────────────────────────────┐
│ identity/                                                       │
│   identity.py     LocalIdentity: Ed25519 keypair · GL-…         │
│                   handle · optional nickname (public            │
│                   material only in storage/output)              │
│   fingerprint.py  identity_fingerprint → GLFP-XXXX-XXXX-XXXX    │
│                   (SHA-256 over the public key)                 │
│   storage.py      IdentityStore: identity.json, 0600, atomic    │
│   lifecycle.py    IdentityManager: ensure/load/nickname/reset   │
├───────────────────────────────────────────────────────────────┤
│ invites/                                                        │
│   tokens.py       20-char CSPRNG tokens (~103 bits) ·           │
│                   gl://join/<token> parsing · gi_… public ids   │
│   models.py       InviteRecord + state machine (CREATED →       │
│                   ACTIVE → REDEEMING → REDEEMED; EXPIRED /      │
│                   REVOKED terminal; fail-safe transitions)      │
│   expiration.py   duration parsing · monotonic deadlines ·      │
│                   countdown formatting                          │
│   authority.py    InviteAuthority — lives in the relay          │
│                   server: hash-keyed records, monotonic+wall    │
│                   expiry, atomic await-free consume, creator-   │
│                   only revoke, bound_session linkage            │
│   registry.py     LocalInviteRegistry: metadata-only records,   │
│                   expiry folding, retention purges              │
│   lifecycle.py    SecureInviteManager: mint/register/revoke/    │
│                   redeem bookkeeping (tokens in memory only)    │
│   redemption.py   redeem_invite: local validation → authority   │
│                   verdict → typed exceptions (exit code 7)      │
│   formatter.py    invite card · INVITE EXPIRED panel ·          │
│                   tables · verification panel (pure render)     │
└───────────────────────────────────────────────────────────────┘
```

**Identity model.** A `LocalIdentity` is an Ed25519 keypair minted locally.
The private key is written once, `0600`, under the state directory; it is
never displayed, never placed in an invite, never transmitted. What peers
see is the handle (`GL-…`, derived from the public key) and the
verification fingerprint (`GLFP-…`). During the Phase 3 handshake each
side optionally adds its identity public key to `KEX_HELLO`/`KEX_REPLY`
(`idpub`); the key is folded into the HKDF transcript, so a relay that
substitutes an identity key breaks the AEAD proof and the session fails
loudly. Comparing `GLFP-…` values out-of-band authenticates *who* you are
talking to; it does not make anyone anonymous.

**Invite model.** An invite is a tuple of `(token, room, expiry, max
redemptions, state)` — the token carries no IP addresses, keys, passwords,
paths, or identity material. Links are a *terminal representation* only:
`gl://join/<token>` is typed into GhostLink; a browser cannot redeem it,
and GhostLink never pretends it can.

**Authority and clocks.** The relay's `InviteAuthority` is the single
source of truth for every invite verdict. Expiry compares a monotonic
deadline (and an aware-UTC wall deadline) captured at creation, so local
clock drift, timezone confusion, and client tampering cannot extend an
invite. Redemption is an atomic check-and-consume with **no `await`
between the state check and the state write**, so N concurrent attempts
produce exactly one winner and N−1 `invite/already-used` verdicts.
Revocation requires the creator's token. Unknown, expired, revoked, and
already-used tokens all fail closed. Sessions created by redemption are
recorded as `bound_session` on the invite, and both sides persist the
invite→conversation binding locally; redeemed invites cannot start
unrelated sessions. Terminals keep only metadata (hashed ids, state,
timestamps) and purge terminal records after `invites.retention_hours`.

**Protocol v3.** The relay envelope remains versioned; v3 adds
`INVITE_CREATE → INVITE_GRANTED`, `INVITE_QUERY → INVITE_STATE`,
`INVITE_REVOKE`, and `INVITE_REDEEM → INVITE_REDEEMED`. Servers and
clients negotiate versions; a v≤2 peer asking for invite operations gets
`protocol/unsupported`. Older v1/v2 envelopes keep working unchanged.

**Threat model (Phase 5), briefly.** Protected: content (E2E), invite
integrity and single-use (authority), identity-key substitution by the
relay (transcript binding), offline guessing of tokens (entropy).
Not protected, by design and stated openly: connection metadata visible
to the relay operator and network provider (IP addresses, timing,
volume) — GhostLink minimizes application-level identity but makes no
anonymity or untraceability claims; a stolen terminal state directory can
leak stored metadata (mitigated by `0600`, retention purges, and
history-off defaults); denial of service by the relay or network.

## 14. Groups (Phase 6B)

Group membership adds a second in-memory authority next to the Phase 5
`InviteAuthority`, plus a small metadata-only client store:

- `ghostlink/groups/ids.py` — `gl-group-XXXX-XXXX-XXXX` identifiers
  (same alphabet discipline as rooms, distinct prefix).
- `ghostlink/groups/events.py` — canonical signed forms; Ed25519 only
  (the Phase 5 identity primitive). `ghostlink/group-event/v1|group|epoch|
  kind|subject|wall_ts` for roster events; separate create/attest forms
  bind proof-of-possession to the relay-issued per-session `attest_nonce`
  delivered in the existing `WELCOME` packet.
- `ghostlink/groups/authority.py` — relay-side `GroupAuthority`:
  race-free roster/epoch registry. Every check-and-commit is one
  synchronous call with no `await` between check and write, so the single
  event loop serializes all mutations deterministically. Mutations are
  two-phase: enqueue → promote (epoch = current+1, canonical bytes and
  wall timestamp pinned) → authorized signature verified → commit; one
  outstanding signature per group. Owner signs join/removed/dissolved,
  the leaver signs left. Capacity (`roster + candidates + pending joins
  ≤ 8`) is checked atomically at invite redemption *before* the token is
  consumed, so a full-group verdict never burns a redemption.
- Transport protocol v4 (`GROUP_CREATE/GRANTED/ATTEST/ATTESTED/STATE/
  ROSTER/LEAVE/REMOVE/DISSOLVE/SIGN_REQUEST/SIGN/EVENT`), symmetric
  validation, additive and version-gated: clients below v4 get
  `protocol/unsupported`; chat (v1–v3) traffic is untouched. `WELCOME`
  carries the optional `attest_nonce`; `INVITE_*` gains a `kind` field
  (`chat` default keeps byte-compatible payloads).
- `ghostlink/groups/registry.py` + `models.py` — local `groups.json`
  store via the same atomic-write `StorageManager`; metadata only
  (fingerprints, public keys, epochs, event descriptors — asserted
  metadata-only at save). `LocalGroupRecord` applies only
  signature-verified events with strict +1 epochs; stale/duplicate
  events are dropped, gapped events mark the record *suspect*, terminal
  records (`left/removed/dissolved/defunct`) archive and purge after 24 h
  of retention.
- `ghostlink/groups/lifecycle.py` — `LocalGroupManager`, the client-side
  engine the CLI calls: create (PoP), invite (owner-gated, headroom
  checked locally *and* at the relay), join (redemption-pinned snapshot
  per GROUPS.md §12.2, candidate attestation, owner-countersign await,
  then re-attest + authoritative roster adoption — success-only
  persistence), leave/remove/dissolve via committed events, re-sync after
  reconnect (epoch rollback → suspect, never a silent regression), and
  the signer callback that declines anything the local record does not
  authorize at exactly the next epoch.
- CLI: `ghostlink group create|list|info|invite|join|leave|remove|
  dissolve|sync|host` — thin rendering over the manager; exit code 8 for
  all group refusals. `group host` is the owner listener that
  countersigns admissions (the design's §13.2 requirement that the owner
  be online).

State the relay keeps is volatile: a relay restart makes groups defunct
locally (§28.4 of docs/GROUPS.md) — never silently resurrected. Group
messaging/encryption is explicitly the next stage; nothing in 6B seals
or routes group content.

## 14A. Group messaging (Phase 6C)

End-to-end group conversations per docs/GROUPS.md §17–§33, on the
pairwise mesh (the Phase 6C data plane; Phase 7 adds sender keys as an
opt-in suite on top — §14B):

- `ghostlink/groups/mesh.py` — `GroupMeshManager`: one identity-bound
  pairwise link per (group, peer) at the current epoch. Handshakes
  reuse the Phase 3 authenticated exchange with context
  `ghostlink/group/v1|{group}|{epoch}`; both sides must present the
  identity key the *roster* pins for them (`idpub`), and session proof
  is HKDF over the handshake transcript — cross-group/epoch or key
  substitution cannot complete a pairing. The smaller identity key is
  the deterministic initiator (§17.2.3); a demand *knock* lets the
  responder role pull a pairing (knocks force a re-pair, zeroizing the
  superseded key, because a knocking peer lost its session-scoped key,
  §27.2). Epoch leaps move superseded links into a real-time 30 s drain
  window (§16.5) then zeroize them; close/attach zeroize everything.
- `ghostlink/groups/frames.py` — sealed inner frames (`GMSG/GACK/
  GREAD`), each re-carrying `{group, epoch, sender, recipient}` for the
  post-decrypt cross-check (§18.2 step 3); `gmsg_` + 16 hex CSPRNG
  message ids; per-sender `gseq` tracker (memory-scoped, gap ≤ 64
  flagged, beyond dropped + suspect); `(sender_fp, message_id)` LRU
  dedupe (512/sender); per-recipient delivery ledgers
  (QUEUED/SENDING/SENT/DELIVERED/READ/FAILED) with `delivered k/m`
  always explicit.
- `ghostlink/groups/service.py` — `GroupMessagingService`: the domain
  engine. Sending computes AAD
  `ghostlink/group-msg/v1|{group}|{epoch}|{from}|{to}` and seals
  independently per roster recipient (fanout ≤ 7, sender excluded,
  never a non-roster member); the sender must be ACTIVE in the current
  epoch. Receiving runs the §18.2 pipeline — envelope gates, AEAD open
  (fail-closed), sealed-context equality, inner schema, dedupe, gseq
  monotonicity, attribution to the authenticated fingerprint, GACK —
  and never crashes on malformed input. Offline members get
  `group/offline` from the relay (fast failure, stale-link teardown)
  plus a bounded sender-side retry queue (8 FIFO drop-oldest with
  explicit FAILED markings); reconnected peers are re-paired with fresh
  keys and queued jobs re-seal to the current epoch (§27.2).
- Relay (`ghostlink/transport/relay/server.py`): `GROUP_FORWARD` is
  validated (framing, fingerprints, kind, per-kind body caps),
  ACL-checked against the authoritative roster (sender attested ACTIVE,
  recipient ACTIVE, epoch ∈ {e, e−1}), rate-limited (token bucket 20/s
  + burst 20 per group+sender over all forwarding incl. kex), then
  forwarded **opaque**. `group/offline` and `group/rate` errors carry a
  member hint so the sender correlates per-recipient state. There is no
  relay-side message queue.
- `ghostlink/ui/group_chat.py` + `ghostlink group chat` — the terminal
  conversation surface: banner (name/id, member count, epoch, mesh
  links, encryption status, latency), attributed message blocks,
  delivery lines, security/membership notices, gap warnings, and
  `/help /info /members /fingerprint /delivery /invite /leave /history
  /export /quit`. No key material is ever rendered. Group history
  reuses the existing history backends (off / session / encrypted) with
  `record_group` entries keyed by group id.

## 14B. Sender-key group messaging (Phase 7)

Opt-in O(1) group encryption per docs/GROUPS.md §36, chosen at creation
by the group's `crypto_suite` (`mesh-v1` default, or `senderkey-v1`):

- `ghostlink/groups/senderkeys.py` — the cryptographic core: a one-way
  HKDF-SHA256 hash ratchet (`ghostlink/group/senderkey/v1` domain
  separation) with per-message keys bound to group/epoch/sender/
  generation/index; `OutgoingChain` (sender) and `ReceiverChain`
  (recipient, with a bounded skipped-key cache ≤ 64 for out-of-order
  delivery); `SenderKeyStore` for per-(group,epoch) outgoing and
  per-(group,sender) incoming chains with epoch pruning, sender-drop and
  full zeroization. Secrets live in `bytearray` slots; nothing is logged,
  stored, or put in exceptions.
- `ghostlink/groups/frames.py` — new inner frame types `GSK`
  (distribution: chain root + generation + index) and `GSKREQ` (pull);
  broadcast `GMSG` uses the recipient sentinel `"*"`.
- `ghostlink/groups/mesh.py` — AAD helpers `group-sk-key/v1` (GSK
  distribution) and `group-sk-msg/v1` (broadcast message, recipient-free).
- `ghostlink/groups/service.py` — the `senderkey-v1` path: on send, seal
  the message **once** (`KIND_SKMSG` with a public `{gen, seq}` header)
  and fan the identical ciphertext to every roster member, distributing
  the sender's chain (`KIND_SK`) over the live pairwise link first and
  answering `GSKREQ` pulls. On receive, resolve the message key (replay/
  gap rejected; missing key → buffer ≤ 16/sender and pull), open with the
  recipient-free AAD, then run the same dedupe/gseq/attribution pipeline.
  Epoch mutations prune all chains and force re-distribution; offline
  members get a bounded re-transmission queue for the already-sealed
  envelopes. `GROUP_FORWARD` kinds `sk`/`skmsg` are relay-opaque.
- CLI/UI — `ghostlink group create --crypto-suite senderkey-v1`, and an
  in-chat `/security` command showing suite/epoch/key-state without ever
  rendering key material; the banner advertises the active suite.

## 15. Reliability & adversarial hardening (Phase 8)

- `ghostlink/core/recovery.py` — the single authoritative recovery
  authority. `RecoveryState` enumerates `CONNECTED/DEGRADED/RECONNECTING/
  RESYNC_REQUIRED/RECOVERING/READY/FAILED/CLOSED` with a closed transition
  table; `RecoveryCoordinator` hands out exclusive `RecoveryLease`s so
  exactly-one resync/install/reconnect runs per key (no competing loops).
  The group service wires it into its resync path.
- `ghostlink/core/logging.py` — a `SecretRedactor`/`RedactingFilter`
  backstop scrubs registered secrets and invite-token shapes from every
  log record; `register_secret()` is called at invite mint time.
- `ghostlink/groups/service.py` — sender-key recovery hardening: a
  `GSKREQ` abuse brake per (group, requester) in both directions, bounded
  pending-buffer constants, and coordinator-backed resync dedup.
- `ghostlink/transport/relay/server.py` — connection-cap + per-source-IP
  connection token bucket (in-memory, fail-closed, swept), on top of the
  existing forward/event brakes.
- `ghostlink/cli/doctor.py` — extended `--doctor` now reports dependency
  availability, the OpenSSL/crypto backend, data-directory health, and the
  available crypto suites (read-only, no state writes).
- `ghostlink/cli/commands/security_status.py` — `ghostlink security-status`
  renders a read-only security & recovery summary (suite, identity
  fingerprint, relay, group recovery) with no secrets, keys, or tokens.

## 16. Production readiness & compatibility (Phase 9)

- **Python floor = 3.11** — the declared minimum (`requires-python`,
  `MIN_PYTHON`) is what the code actually uses and is verified green on
  3.11.2; the matrix lives in `docs/COMPATIBILITY.md`. The parser never
  uses PEP 695 type-alias syntax, so it parses cleanly on 3.11.
- **Schema versioning** — `ghostlink/core/migration.py` defines
  `CONFIG_SCHEMA_VERSION` and `STATE_SCHEMA_VERSION`. Config carries
  `[meta].config_version`; each stored group record carries `"v"`. A
  *newer* version is rejected (fail closed) rather than reinterpreted.
- **Release engineering** — `scripts/release_check.sh` (the release-candidate
  gate) and `scripts/scan_secrets.py` (offline secret-leak tripwire) are
  invoked by the CI/release process and are also covered by tests.
- **Backup guidance** — `docs/BACKUP.md` distinguishes backupable metadata
  from sensitive key material and documents recovery behavior.

