# GhostLink Groups — Phase 6a Design (Secure Multi-Peer Communication)

> **Status: design specification — no implementation yet.**
> This document defines the security model, protocols, limits, and test
> plan for secure groups *before* any code is written. Implementation must
> not begin until this design is reviewed and approved. Where this document
> and future code disagree, the code must be changed or this document
> amended — never silently diverge.
>
> Everything here builds on the verified Phase 1–5 architecture
> (see [ARCHITECTURE.md](ARCHITECTURE.md)) and keeps the project
> **terminal-only** (Termux/Linux), **Python ≥ 3.12**, and honest about
> what it protects.

## 1. Goals and non-goals

### Goals for Phase 6a

- Private, invite-only group conversations for **up to 8 members** on top of
  the existing relay infrastructure.
- Full membership lifecycle: create → invite → join → leave → remove →
  dissolve, with one clearly authorized owner.
- End-to-end encrypted group text chat between members, reusing the proven
  Phase 3 primitives (X25519 + HKDF-SHA256 + ChaCha20-Poly1305).
- A relay-authoritative membership registry with the same rigor as the
  Phase 5 invite authority: atomic transitions, fail-closed verdicts,
  monotonic-clock reasoning, no secrets at rest.
- Terminal UI + CLI for groups, consistent with existing surfaces.
- A test suite as demanding as Phase 5's: live in-process relay, races,
  tamper injection, log-hygiene audits.

### Explicitly deferred (Phase 6b / Phase 7)

- Group file transfer (mesh fanout of `FILE_*` frames).
- Replies, edits, reactions; contact cards; notification unread state.
- Friend system (add/verify/block) and safety numbers.
- Sender-key group encryption (design sketched in §9 — **upgrade path only,
  not Phase 6a scope**).
- Member-delegated invitations, owner transfer, groups larger than 8,
  encrypted-at-rest rosters beyond the standard `0600` metadata stores.
- Public/federated rooms — permanently out of scope per the roadmap.

## 2. Trust model and honest security claims

GhostLink groups extend, and do not widen, the Phase 5 threat model.

**Protected (and how):**

| Property | Mechanism |
| --- | --- |
| Message confidentiality & integrity between members | Per-pair AEAD over Phase 3 handshakes (§6) |
| Sender authenticity inside a group | Pairwise links bind identity keys into the HKDF transcript; `idpub` is **mandatory** for group links (§6.2) |
| Relay key-substitution (MITM) | Identity keys folded into the handshake transcript; AEAD proof fails loudly on substitution |
| Membership authorization | Relay-authoritative registry; invite redemption per Phase 5; owner-only removal, verified by Ed25519 event signatures (§5) |
| Removed members reading new traffic | Relay ACL stops routing to them **and** remaining clients tear down the pairwise link and re-key to a new epoch (§6.4) |
| Replay / reorder of group messages | Per-link AEAD nonces and sequence rules as in Phase 3, plus message-id dedupe across rekeys (§6.5) |
| Secret hygiene | Keys in memory only, zeroized on teardown; local stores metadata-only; tokens hashed; log views redacted |

**Not protected — stated openly, never implied otherwise:**

- **Network-level identity is not hidden.** The relay operator and the
  network provider see IP addresses, connection timing, traffic volume, and
  group metadata (group ids, member counts, join/leave times). Groups make
  **no anonymity, "untraceability", or metadata-invisibility claims**.
- **A malicious member sees plaintext.** Any member can read, copy, and
  republish group content. End-to-end encryption protects transport between
  members, not content from members. Choose members accordingly; use
  fingerprint verification before admitting sensitive traffic.
- **A compromised endpoint is a compromised group share.** A stolen
  unlocked terminal yields local metadata and (if history is enabled)
  stored transcripts, exactly as in Phases 3–5.
- **Denial of service is out of scope.** A relay can censor, delay, or
  partition traffic. Availability attacks are detectable (missing events,
  stalled links) but not preventable by this design.
- **No cryptographic transcript consistency in 6a.** A malicious relay can
  show different message *orderings* to different members (it cannot forge
  content). §12 lists this limitation and the Phase 7 mitigation path.

Nothing in this document may be described as "untraceable", "IP-invisible",
"100% anonymous", or "unhackable" — in code, UI, or docs.

## 3. Vocabulary and identities

- **Group** — a persisted, relay-registered room with id
  `gl-group-XXXX-XXXX-XXXX` (same alphabet/shape discipline as room ids;
  new prefix keeps groups visibly distinct from one-to-one rooms).
- **Owner** — the creating member. Exactly one. Owns invitations, removal,
  and dissolution. Not transferable in 6a.
- **Member** — an identity (`GL-…` handle + Ed25519 identity key from
  Phase 5) holding a slot in the roster. The owner is also a member.
- **Epoch** — a monotonically increasing integer scoped to the group.
  Starts at 1. **Every membership change increments it.** Session keys are
  bound to (group id, epoch), so keys never straddle a roster change.
- **Pairwise link** — one Phase 3 secure session between two members,
  established *inside the group context*. A group of *n* members is a mesh
  of *n·(n−1)/2* links. With cap 8: at most 7 links per client, 28 total.
- **Roster** — the authoritative member list, owned by the relay registry:
  `{identity_fingerprint, handle, role, joined_epoch}` per member.

## 4. Why not a shared copied key (explicitly rejected)

The handoff warning is restated as a design rule: **the one-to-one session
key must never be forwarded to additional members.** A single static group
key copied to every member would mean:

1. **Removal is unfixable** — a removed member keeps a key that still
   decrypts future traffic; "rotating" it means replacing the entire scheme.
2. **One compromise collapses the whole group** — one stolen member key
   decrypts every member's traffic, past and present, including recorded
   ciphertext.
3. **No per-sender authentication** — any holder can impersonate any holder;
   a leakage cannot even be attributed.

The Phase 6a **mesh-of-pairwise** model has none of these failures, reuses
only audited Phase 3 code paths, and costs at most 7 ChaCha20-Poly1305
seals per outgoing message at the cap — trivial on Termux-class hardware.
Its known costs (O(n) encryption, O(n²) link count, no single group
transcript) are why §9 documents a sender-key upgrade for Phase 7.

## 5. Membership authority (relay) and authorization model

The relay hosts a **`GroupAuthority`** modeled directly on the Phase 5
`InviteAuthority`: one in-process registry, the single source of truth,
atomic transitions with no `await` between check and write, fail-closed
verdicts, digests and public ids only in logs.

### 5.1 Group record (relay-held)

```
group_id        gl-group-…                      (public)
state           ACTIVE | DISSOLVED              (DISSOLVED is terminal)
owner           owner identity fingerprint +    (set once at creation)
                  owner relay-session binding
members         roster keyed by identity fingerprint
                  {handle, role, joined_epoch, relay-session binding}
epoch           int, incremented on every roster mutation
created_at      aware-UTC wall clock
capacity        ≤ GROUP_MAX_MEMBERS (8)
```

Relay-session bindings expire with relay sessions (as invites do); the
*identity fingerprint* is the durable key of a roster entry. A member
reconnecting with a fresh relay session must **re-present** membership
(proof: an Ed25519 signature over a relay-issued attestation challenge,
made with the member identity key — the same key already pinned in the
roster, so no new cryptography is introduced).

### 5.2 Authorization rules

| Operation | Who may invoke | Registry check (atomic) |
| --- | --- | --- |
| Create | any client | mints record; creator becomes owner, epoch 1 |
| Invite | **owner only** | owner session binding matches; group ACTIVE; roster + pending redemptions < capacity |
| Join (redeem) | invite holder | Phase 5 invite verdict MUST be `REDEEMED` for a group-kind invite; roster < capacity |
| Leave | the member themself | membership exists |
| Remove | **owner only**; owner cannot remove themself | target is a member; not the owner |
| Dissolve | **owner only** | group becomes terminal; all routing refuse-closed |
| Forward | any **current member**, to any other current member | both endpoints on roster under same group (§7) |

All other orderings/actors answer with typed refusals
(`group/not-owner`, `group/not-member`, `group/full`, `group/unknown`,
`group/dissolved`). Unknown and malformed inputs fail closed, never silent.

### 5.3 Owner-signed membership events

Relay session binding answers "is this owner *connected right now*"; it does
not let a member *audibly verify* a roster change after the fact. So every
roster-mutating event (join, leave, remove, dissolve) additionally carries
the **owner's Ed25519 signature** over a canonical form
`ghostlink/group-event/v1|group_id|epoch|kind|subject_fingerprint|wall_ts`,
produced by the owner's client, relayed verbatim, and verified by every
member against the owner key pinned at join time (§5.4).

- Valid signature → client applies the roster change and bumps epoch.
- Missing/invalid signature → the client **does not apply** the change,
  flags the group as *suspect* in the UI, and refuses to send new content
  until a re-sync returns consistent, signed state.
- Join events are owner-side generated (the join arrived through an
  owner-minted invite), so owner signature on joins is mandatory in 6a.
  Leave events are signed by the *leaving* member instead (self-service
  exit cannot require an absent owner); removal/dissolve are owner-signed.

This bounds a *malicious relay* to censorship (withholding events — an
availability failure, visible as stalled rosters) rather than forgery
(fabricating members — an integrity failure, cryptographically refused).

### 5.4 Join flow

1. Owner mints a **group-kind invite** (`gl://join/<token>`; same 20-char,
   ~103-bit CSPRNG token format, same TTL/one-time/concurrency semantics as
   Phase 5 — the invite authority gains a `kind` (`chat`|`group`) and a
   bound `group_id` instead of a room id).
2. Joiner redeems. On success the authority response carries
   `{group_id, owner_fingerprint, owner_identity_key, roster snapshot,
   epoch}`. The redeeming client **pins** the owner key and every roster
   fingerprint locally before chatting.
3. Joiner connects its pairwise links to existing members (§6.2) and learns
   handles; fingerprints are comparable out-of-band via
   `/fingerprint <member>` / `ghostlink identity fingerprint`.
4. Join completes only when the owner client's signature over the join
   event verifies at the joiner — else fail closed with a clear error.

### 5.5 Leave, removal, dissolution semantics

- **Leave:** self-signed event; roster drops the member; epoch +1; the
  leaver's client tears down all links, zeroizes keys, and archives the
  group read-only.
- **Remove:** owner-signed event; relay ACL immediately stops routing the
  target; members likewise tear down; epoch +1. The removed client is
  notified once (best effort) and then refused.
- **Dissolve:** owner-signed; registry moves to terminal `DISSOLVED`;
  all forwarding for the group fails closed thereafter; every client tears
  down and archives.
- **Owner disconnect (not leave):** chat continues (mesh transport does not
  depend on the owner). Owner-only ops queue *locally* at the owner's
  client until it reconnects; nothing else pauses.
- **Owner leave == dissolve** in 6a (no ownership transfer), chosen
  explicitly to keep authorization a single-writer model.

## 6. Group cryptography — mesh of pairwise sessions (Phase 6a model)

### 6.1 Primitives — unchanged from Phase 3

X25519 ephemeral key exchange, HKDF-SHA256 session derivation,
ChaCha20-Poly1305 AEAD, per-message nonces, transcript-bound salt, AEAD key
confirmation, memory-only keys zeroized on teardown. **No new primitives
are invented for groups; no Phase 3 code path is weakened.**

### 6.2 Per-pair link establishment inside the group context

- **Both members' identity keys are mandatory** on group links (`idpub` is
  *required*, unlike 1:1 where it is optional) — this is what makes group
  senders attributable and gives the relay-substitution proof its teeth.
- **Group/epoch binding:** the HKDF context gains
  `ghostlink/group/v1|<group_id>|<epoch>` alongside the existing transcript.
  Both sides compute it from pinned local state (group id from the roster,
  epoch from the registry), so the wire format of `KEX_HELLO`/`KEX_REPLY`
  is untouched; a key derived for (group g, epoch e) cannot open ciphertext
  from (g, e′) or from any other group.
- **Deterministic roles:** the member whose identity-public-key hex is
  lexicographically smaller acts as handshake **initiator** for that pair.
  This removes both-initiate/both-wait deadlocks without any negotiation
  traffic and is stable across reconnects.
- Every pairwise link runs on the group's channel with **addressed
  forwarding** (§7): bodies remain opaque to the relay; `to`/`from` are
  roster slots the relay can already see.

### 6.3 Sending a group message (mesh fanout)

1. Sender composes one plaintext; it is assigned one globally unique
   message id (`glm_…`, CSPRNG) and a per-sender group sequence (local
   monotone counter, per group).
2. For each *current* roster peer with a live link: seal the message under
   that pair's session key (per-pair nonce as in Phase 3) and ship it
   addressed. — At cap: ≤7 seals, ≤7 frames.
3. Delivery/ack machinery is the existing per-link one (outbox,
   `MESSAGE_ACK` waiters, requeue-on-reconnect). The UI aggregates:
   `delivered 5/7`, listing undelivered handles, never masking partial
   delivery as success.

There is deliberately **no relay fanout of one ciphertext to many** — the
relay routes addressed opaque frames; fanout happens at the sender, end to
end.

### 6.4 Membership change = epoch leap

- On every roster mutation the registry increments epoch; members learn it
  from the signed event (§5.3) or from a re-sync after reconnect.
- **Removed/dissolved side:** remaining clients tear down their link to
  the removed member (zeroize its keys) *and* the relay ACL refuses further
  routing to it — defense in depth, server-enforced, not client politeness.
- **Joiner side:** new member starts with links bound to the *new* epoch.
  Joiners receive **no history**: group history is local-only per device
  (history modes unchanged: off / session-only / encrypted-at-rest) and is
  never synchronized between members.
- **Lazy re-handshake:** after an epoch leap, existing pairs re-handshake
  bound to the new epoch on their next exchange, exactly as reconnects
  re-handshake today; pending sends re-queue and seal under the fresh key.
- Property delivered: a member removed at epoch *e* cannot read ciphertext
  of epochs > *e* (relay ACL + link teardown + epoch-bound keys), and a
  member joining at epoch *e* cannot be handed ciphertext of epochs < *e*
  (no history distribution, and old-epoch keys are already gone).

### 6.5 Replay, dedupe, ordering

- Per link: unchanged Phase 3 guarantees (per-message AEAD nonces, strict
  inbox ordering, duplicate detection, self-healing reordering).
- Across rekeys: group messages dedupe on **message id** (survives the
  per-link sequence reset), so a re-fanned-out copy after reconnect never
  double-renders. A bounded per-peer seen-id cache (default 512 ids, FIFO)
  backs this; older repeats are dropped harmlessly.
- Ordering promise, stated honestly: **per-sender** ordering is preserved
  in the group transcript; **cross-sender** ordering is arrival order.
  No total-order claim is made in 6a (see §12).

### 6.6 Forward secrecy and post-compromise posture

- Per link, unchanged from Phase 3: fresh ephemeral X25519 per session;
  reconnects and epoch leaps re-key; recorded traffic cannot be opened by
  later-compromised session keys.
- Identity-key compromise is a **group-wide trust event**: the only
  response is owner removal of the compromised fingerprint, epoch leap,
  and fresh invites for any re-admission (new identity). Documented in the
  UI; not automated in 6a.
- Post-compromise healing per pair is inherited (new handshake ⇒ new
  keys); group-wide healing after roster compromise is the Phase 7
  sender-key work.

## 7. Relay protocol v4 (additive, version-negotiated)

v4 adds a `GROUP_*` family to the versioned JSON envelope. Validation
remains schema-first in both directions; all bodies that carry user content
remain base64 AEAD ciphertext, size-bounded exactly as `FORWARD` today.

```
GROUP_CREATE     → GROUP_GRANTED        {group_id, epoch=1}
GROUP_STATE      → GROUP_ROSTER         {epoch, roster[], capacity}
GROUP_INVITE     → INVITE_GRANTED       (group-kind invite; §5.4)
GROUP_LEAVE      → GROUP_EVENT(left)    (signed by leaver)
GROUP_REMOVE     → GROUP_EVENT(removed) (signed by owner)
GROUP_DISSOLVE   → GROUP_EVENT(dissolved)(signed by owner)
GROUP_FORWARD    → routed               {group_id, to: fp, body: b64 ct}
GROUP_EVENT      (push)                 join/left/removed/dissolved + epoch + signature
GROUP_ATTEST     (challenge for §5.1 re-presentation)
```

- **Version gates:** servers and clients negotiate as today; a v≤3 peer
  driving `GROUP_*` gets `protocol/unsupported`. v1–v3 traffic is
  untouched; one-to-one chat paths do not change.
- **Server-side ACL:** `GROUP_FORWARD` is accepted only when *sender and
  recipient* are both roster-current under the same group; everything else
  fails closed. The relay never inspects bodies.
- **Addressing:** `to` is a roster identity fingerprint, publicly visible
  to the relay anyway (§2). Broadcast addressing is not provided; the
  sender fans out (§6.3).
- **Rate/size guards:** per-group membership-op rate limit per relay
  session; `GROUP_FORWARD` bodies bounded by `MAX_FORWARD_PAYLOAD_BYTES`;
  frames-per-second soft cap per group to keep a misbehaving client from
  exhausting a Termux peer (exact values fixed at implementation time in
  `constants/net.py` and documented in CONFIGURATION.md).

## 8. Storage, identifiers, and resource limits (Termux constraints)

- New local namespace `groups` (metadata-only): group id, pinned owner key,
  own role, cached roster `{fingerprint, handle, role, joined_epoch}`,
  epoch, state, optional per-identity *verified* marks for out-of-band
  fingerprint confirmation. **No keys, no tokens, no invite secrets** —
  same discipline as the invites store; atomic `0600` writes.
- History stays opt-in per group under the existing history settings; on
  removal/leaving/dissolution the local group is archived read-only and
  obeys the same retention/purge hygiene as terminal invites.
- Bounded everything (defaults fixed in `constants/net.py`):

| Limit | Value | Rationale |
| --- | --- | --- |
| `GROUP_MAX_MEMBERS` | 8 | ≤28 links total, ≤7 seals/message; roster panels fit a phone terminal |
| `GROUP_MAX_GROUPS` | 32 active per client | mesh connections stay bounded |
| `GROUP_NAME_MAX_LEN` | 48 (validated printable) | consistent with field-length rules |
| roster snapshot payload | ≤ 8 KiB | single frame, no pagination in 6a |
| seen-id cache per peer | 512 ids | dedupe without unbounded memory |
| pending owner ops queue | 64 | owner-offline bursts bounded |
| membership-op rate | 4 / 10 s / session | abuse brake, fail-loud not silent |

## 9. Upgrade path — sender keys (Phase 7 candidate, **not** 6a scope)

The mesh model's costs (O(n) seal-per-message, O(n²) links, no single
group transcript) motivate a later **sender-key** design, sketched here so
6a never boxes it out:

- Each member derives a per-sender chain key (hash-ratcheted), distributes
  it to peers over the *existing pairwise links* (6a's links thus become
  the control channel for 7's data channel), and seals each message once
  under its own sender key: O(1) encryption per message.
- Epoch leaps trigger **immediate re-distribution** of fresh sender keys to
  the survived roster; removed members hold only dead keys for dead epochs.
- Per-(sender, epoch) replay windows and ratchet steps bound reuse;
  transcripts gain key-id/epoch headers, making cross-sender total
  ordering and transcript-consistency work (§12) feasible.
- Forward secrecy becomes per-message-step ratcheting; post-compromise
  healing is a single re-distribution away.

Acceptance gate for that work: a dedicated design doc, threat-model diff
against this one, and the same test rigor. **Until then, 6a's mesh is the
only implemented group encryption.**

## 10. CLI and terminal UI surface (terminal-only)

CLI (top level, consistent with `identity`/`invite` families):

```
ghostlink group create  [--name …] [--relay …]
ghostlink group list                  # local groups + roles + epoch + state
ghostlink group info    <gl-group-…>  # roster table, fingerprints, verified marks
ghostlink group invite  <gl-group-…> [--expires 15m]   # owner only
ghostlink group join    gl://join/<token>
ghostlink group leave   <gl-group-…>
ghostlink group remove  <gl-group-…> <GLFP-…>          # owner only
ghostlink group dissolve <gl-group-…>                  # owner only
ghostlink group chat    <gl-group-…>   # enters the group chat surface
```

In-chat commands extend the existing slash vocabulary: `/members`
(roster + fingerprints + verified marks), `/fingerprint <member>`,
`/invite` (owner only, reuses invite card), `/remove`, `/leave`,
`/dissolve`, `/epoch` (diagnostics). The chat surface is the existing
event stream with sender handles; roster/notice events (join, leave,
remove, epoch leap, suspect-state warnings) render as notices, escaped as
all user content is. No HTML, no widgets frameworks, no GUI — the terminal
remains the medium.

## 11. Test plan (gates for implementation)

Mirror Phase 5's bar; all networking on the in-process/loopback relay.

1. **Lifecycle e2e:** create → invite → 3–8 members join → chat → leave →
   remove → dissolve, against the live test relay. Real 1 s/2 s invite
   expirations, as Phase 5 does.
2. **Concurrency:** simultaneous redemptions of one group invite (exactly
   one winner); join/remove racing a fanout; double-remove; re-sync during
   epoch leap.
3. **Confidentiality on removal:** removed member's frames are refused by
   relay ACL *and* rejected by clients; ciphertext of epoch e+1 fails AEAD
   under epoch-e keys (tamper check).
4. **Authorization:** non-owner remove/invite/dissolve refused; forged or
   unsigned owner events rejected and flagged suspect; replay of old-epoch
   GROUP_EVENT refused.
5. **Sender authenticity:** identity-key substitution by relay breaks the
   AEAD proof (per §6.2); tampered `idpub` fails loudly (as Phase 5 tests).
6. **Mesh mechanics:** deterministic initiator selection; partial delivery
   rendering; message-id dedupe across a forced rekey; offline member
   catches up roster on re-present (no ghost content).
7. **Limits:** 9th member refused `group/full`; rate-limit trip fails loud;
   payload ceilings enforced symmetrically.
8. **Hygiene:** group logs carry fingerprints/ids/states only — no tokens,
   no keys, no plaintext (extend the Phase 5 leak-audit pattern).
9. **Quality gates:** `pytest`, `ruff check`, `ruff format --check`,
   `mypy --strict`, `compileall`, `scripts/dev_check.sh` — all green,
   none weakened.

## 12. Known limitations (accepted, documented, with mitigations)

- **No transcript consistency (6a).** A malicious relay can present
  different cross-sender orderings to different members. It cannot forge or
  alter content. Members sensitive to this compare transcripts out of
  band; the sender-key phase (§9) enables a hash-chained group transcript.
- **Relay-visible metadata.** Group ids, roster sizes, timings, and volumes
  are visible to the relay and the network. Documented in §2; never
  minimized in UI copy.
- **Single-owner liveness.** Owner-only operations stall while the owner is
  offline; owner departure dissolves the group in 6a. Owner transfer is a
  possible 6b item with its own authorization design.
- **Malicious-member leakage.** Inherent to any group system (see §2);
  mitigated culturally (small groups, fingerprint verification) and by
  removal + epoch leap, not by any technical silver bullet.
- **Scale.** 8-member cap; larger groups need sender keys and are not
  claimed.

---

*This specification is a design contract for Phase 6a. Code that cannot
satisfy it must not land; findings that invalidate it must amend it first.*
