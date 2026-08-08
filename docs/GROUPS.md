# GhostLink Groups — Phase 6A Security & Design Specification

> **STATUS: DESIGN / REVIEW — NO IMPLEMENTATION HAS STARTED.**
>
> This document is the complete security model for Phase 6A secure groups.
> It exists so that implementation never invents security rules during
> coding. Every security-critical decision is stated here explicitly, with
> its attacker analysis, deterministic failure behavior, and test
> obligations. Group implementation begins **only** after this design is
> reviewed and approved; any implementation finding that contradicts this
> document must amend the document first — code must not silently diverge.
>
> Scope anchors: GhostLink remains a **Python ≥ 3.12, terminal-only
> (TUI) project for Termux and Linux**. No APK, no Android app, no GUI, no
> browser client, no web frontend is introduced or implied by this design.
> No new cryptographic primitive and no new cryptographic library is
> introduced: only the existing Phase 3 stack (X25519, HKDF-SHA256,
> ChaCha20-Poly1305, the authenticated handshake, and the existing
> replay/sequence machinery) is reused. See [ARCHITECTURE.md](ARCHITECTURE.md)
> for the verified Phase 1–5 substrate this design builds upon.

---

## 1. Overview

Phase 6A adds **private, invite-only group conversations** to GhostLink:
up to **8 members** exchanging end-to-end encrypted text in a shared
terminal chat surface, on top of the existing relay infrastructure.

The selected architecture is **"mesh now, sender keys later"**:

* **Phase 6A (this design) — pairwise mesh encryption.** Every pair of
  group members holds an authenticated Phase 3 end-to-end session bound to
  the group and its current epoch. A group message is encrypted separately
  for each authorized recipient (sender-side fanout). For a group
  A, B, C, D the mesh is A↔B, A↔C, A↔D, B↔C, B↔D, C↔D.
* **Phase 7+ (§36, future) — sender keys.** A per-sender ratcheted key
  distributed over the mesh links would make encryption O(1) per message.
  This is a documented migration path **only**; nothing in Phase 6A
  implements sender keys.

A **shared or copied group key is explicitly rejected** (§17.4). The
relay routes opaque ciphertext and required non-secret routing metadata
only; it never receives plaintext, private keys, group keys, or pairwise
session keys (§28).

## 2. Goals

G1. **Correct, auditable group security for small groups.** 8 members
maximum (§32), so every security property can be reasoned about
exhaustively and tested with real loopback relays.

G2. **Full membership lifecycle.** Create → invite → redeem → join →
leave → remove → dissolve, with unambiguous authorization (§9) and
deterministic concurrency behavior (§9.4).

G3. **End-to-end confidentiality and integrity** of group messages
between members, reusing Phase 3 primitives only (§17–§20).

G4. **Epoch-bounded access.** Every membership change advances the group
epoch; removed members lose access to future epochs, joiners gain no
access to past epochs (§16, §24, §25).

G5. **Relay-authoritative membership** — one atomic, fail-closed registry
per relay, modeled on the verified Phase 5 invite authority (§8.2), with
owner-signed membership events so a malicious relay can censor but not
forge rosters (§9.5).

G6. **Honest security claims.** Every limitation is documented (§34,
§35). Nothing here claims anonymity, untraceability, or traffic-analysis
resistance.

G7. **Termux-grade resource discipline.** Explicit bounds for messages,
queues, rosters, sessions, events, and history (§32).

G8. **Terminal-only surfaces.** CLI and TUI designs only — the terminal
remains the medium (§39, checklist item I8).

## 3. Non-goals

N1. **No implementation in Phase 6A-design.** This document ships no
group models, packets, encryption code, CLI, UI, storage, services, or
managers, and no sender keys.

N2. **No new cryptography.** No new primitives, no second key-exchange
system, no additional crypto library. Anything the Phase 3 stack cannot
express is deferred, not invented.

N3. **Groups larger than 8.** Larger groups require sender keys (§36)
and are not claimed.

N4. **Group file transfer** (mesh fanout of Phase 4 `FILE_*` frames) —
deferred to a later sub-phase; Phase 4 one-to-one behavior is untouched
by this design.

N5. **Replies, edits, reactions, contact cards, rich room metadata.**
Phase 6B roadmap items.

N6. **Friend system / safety numbers / blocking.** Phase 6B.

N7. **Anonymity, pseudonymity guarantees beyond Phase 5, traffic-analysis
resistance, cover traffic, padding.** Out of scope; explicitly not
claimed (§34).

N8. **Owner transfer, member-delegated invites, joiner approval flows
beyond §13.** Recorded as open questions (§40).

N9. **Public or federated rooms.** Permanently out of scope per
[ROADMAP.md](ROADMAP.md).

N10. **Relay-side message queuing / store-and-forward for groups.** The
relay routes live traffic only (§29).

## 4. Group terminology

| Term | Definition |
| --- | --- |
| **Group** | A relay-registered multi-party room with id `gl-group-XXXX-XXXX-XXXX`, an owner, a roster, and an epoch. |
| **Owner** | The member who created the group. Exactly one. Holds invite, admission, removal, and dissolution authority. Not transferable in 6A. |
| **Member** | An identity (Phase 5 Ed25519 keypair, `GL-…` handle, `GLFP-…` fingerprint) occupying a roster slot. The owner is also a member. |
| **Roster** | The relay-authoritative member list: `{fingerprint, handle, public key, role, joined_epoch}` per member. |
| **Epoch** | Per-group monotone integer starting at 1; **every roster mutation increments it by exactly 1**. Session keys are bound to (group id, epoch). |
| **Pairwise link** | One Phase 3 secure session between two members, established in the context of a specific group and epoch. |
| **Mesh** | The set of pairwise links among current members: n(n−1)/2 links for n members; ≤ 28 at cap. |
| **Group message** | One logical message from one sender, fanned out per-recipient as separately sealed ciphertexts sharing one `message_id`. |
| **message_id** | Globally unique id per group message (`gmsg_` + 16 hex, CSPRNG) — survives rekeys; drives dedupe. |
| **gseq** | Per-sender, per-group monotone sequence number for transcript ordering (distinct from per-link frame `seq`). |
| **Group invite** | A Phase 5 invite of kind `group` bound to a group id (§11). |
| **Drain window** | Bounded interval after an epoch leap during which in-flight ciphertext of the *previous* epoch remains acceptable on the *old* link (§16.5). |
| **Defunct group** | A group whose relay-side record is gone (relay restart or dissolution). Local metadata is retained; the group is dead (§28.4). |

## 5. Threat model

Ten attacker classes. "Can" entries state what the design *concedes*;
"cannot" entries are enforced by named mechanisms and tested per §38.

### 5.1 Malicious relay (active, not merely honest-but-curious)

| CAN | CANNOT |
| --- | --- |
| Read all routing metadata: group ids, roster fingerprints, epochs, join/leave times, frame sizes/timing, per-fanout arity (§34) | Read plaintext (AEAD; §20) |
| Censor, delay, reorder, drop, partition traffic (availability attack — *detectable*, not preventable; §35 L1) | Read pairwise session keys, identity private keys, or message ids (sealed in the inner frame; §19) |
| Withhold/stall membership events (clients detect roster stall; flag *suspect*; refuse to send — §27.3) | Forge roster changes: mutation events require the signer's Ed25519 signature verified against keys pinned at join (§9.5) |
| Replay whole unchanged envelopes (defeated by §21) | Decrypt or selectively replay by message id (ids are sealed) |
| Refuse service entirely | Substitute identity or handshake keys: `idpub` is folded into the HKDF transcript + AEAD proof fails (§17.2) — same guarantee as Phase 5 1:1 |

### 5.2 Network attacker (on-path, off-relay)

| CAN | CANNOT |
| --- | --- |
| Observe the same metadata the relay sees when transport is `ws://` (loopback/LAN typical) | Read plaintext or keys (E2E holds hop-by-hop regardless of transport TLS) |
| Inject/modify/drop relay-protocol packets (schema validation + AEAD + context cross-checks fail closed; §31, §33) | Forge valid E2E ciphertext or group EventRoom signatures |
| Cause connection loss (Phase 2 supervised reconnect handles; §27) | — |

Transport endpoints support `ws://` (typical loopback) and `wss://`
(TLS). **Content protection never depends on transport TLS**; `wss://`
only adds hop metadata protection and relay authentication. Phase 6A
does not make `wss://` mandatory (recorded in §40).

### 5.3 Malicious group member (legitimate roster member gone rogue)

| CAN | CANNOT |
| --- | --- |
| Read, copy, screenshot, republish all group plaintext (inherent to every group system; §35 L2) | Read *other pairs'* link keys — each link is an independent X25519 session (§17) |
| Send fake/arbitrary content *as themself* (attributable per-link; §20.3) | Forge another member: sender authenticity is per-link proof of possession of that member's identity key (§20.3) |
| Replay their own old messages — defeated in-epoch by dedupe/gseq (§21), cross-epoch by epoch AAD (§16.6) | Invite, remove, or dissolve (owner-only authority; §9) |
| Flood until limits trip (rate/size caps fail loud; §32, §33) | Keep reading after removal (exclusion is ACL-first; §15) |
| Refuse to fan out to specific members (partial delivery is *displayed* per recipient, never masked; §18.3) | Modify roster metadata at the authority (authorization checks; §9) |

### 5.4 Removed group member

| CAN | CANNOT |
| --- | --- |
| Retain what they legitimately received while a member (local history is theirs; unavoidable) | Receive/decrypt post-removal ciphertext: relay ACL drops their frames immediately; remaining clients tear down the link and leap epoch; old keys never fit the new epoch context (§15, §16, §25) |
| Transmit stale packets (rejected by ACL → roster check → AAD epoch, in that order; §15.3, §33) | Be re-added silently (only via a fresh owner invite + fresh epoch; §15.4) |

### 5.5 Unauthorized invite recipient (someone who obtained or guessed a link)

| CAN | CANNOT |
| --- | --- |
| Attempt redemption | Guess tokens: 20 chars from a 32-symbol alphabet ≈ 103 bits, CSPRNG, never timestamp-derived (Phase 5 property, unchanged) |
| Race a legitimate joiner | Win twice: atomic check-and-consume gives exactly one winner per redemption slot (Phase 5 property, retained; §12) |
| — | Redeem expired/revoked/exhausted invites (authority fail-closed verdicts; §12) — or learn private keys from an invite (invites carry none; §11.5) |

### 5.6 Replay attacker (relay, network, or member replays recorded ciphertext)

| CAN | CANNOT |
| --- | --- |
| Replay whole envelopes unchanged | Pass dedupe in-epoch: (sender_fp, message_id) LRU + gseq monotonicity (§21, §22) |
| Replay across epoch boundaries | Survive: old-epoch frames are rejected after the drain window; epoch is AAD-bound so re-tagging fails (§16.5–§16.6) |
| Replay across groups or recipients | Survive: group id, sender, recipient, and protocol string are AAD-bound (§20.2) |
| Replay across restarts | Survive: reconnects and restarts mint fresh session keys; old ciphertext never fits (§27) |

### 5.7 Malformed-packet sender

| CAN | CANNOT |
| --- | --- |
| Send garbage at any layer | Crash any client (validators at relay-envelope, frame, AEAD, and model layers; §31) |
| Send oversized packets | Exceed symmetric byte ceilings (§32); oversize is refused before allocation |
| Send unknown types | Achieve anything: unknown types drop + log + bounded peer ERROR (§31) |

### 5.8 Compromised endpoint (live device under attacker control)

| CAN | CANNOT |
| --- | --- |
| Read that device's live session keys, plaintext on screen, and decrypted history | Retroactively open *closed* sessions/epochs of any pair (ephemeral keys are zeroized on teardown; §26) |
| Impersonate that member until detected | Impersonate *other* members (their identity keys are elsewhere) |
| — | Remain undetected indefinitely if peers compare `GLFP-…` fingerprints out of band (§20.3 detection aid, not prevention) |

Remediation: owner removes the member → epoch leap → fresh links (§15);
the compromised identity fingerprint must never be re-admitted (§26.5).

### 5.9 Stolen local storage (offline attacker with the state directory)

| CAN | CANNOT |
| --- | --- |
| Read local metadata at `0600` discipline: group ids, roster fingerprints/handles, invite metadata (never tokens), settings | Recover session keys (never persisted; §26) — or invite tokens (only hashes persist; Phase 5) — or identity private key if the device's OS user boundary held (`0600`) |
| Read encrypted history **only** if the attacker also holds the history passphrase (scrypt → ChaCha20-Poly1305, Phase 3 property, unchanged) | Plant executable state: corrupt/foreign records fail closed with typed storage errors (Phase 1 storage discipline) |

Mitigations are Phase 1/3/5 ones — `0600`, atomic writes, retention
purges, history-off default. 6A adds no new at-rest secrets.

### 5.10 Concurrent-membership-change adversary (racing legitimate ops)

| CAN | CANNOT |
| --- | --- |
| Race joins/removes/leaves/redemptions from multiple clients | Produce divergent authority state: per-group serialization, one atomic section per op, epochs assigned in a single total order (§9.4) |
| Race two redemptions of one invite slot | Both win: exactly one succeeds (Phase 5 atomicity) |
| Race removal against in-flight fanout | Cause removed member to keep receiving: exclusion ACL applies at commit time; the residual pre-commit in-flight window is bounded and documented (§15.3, §35 L4) |

## 6. Trust model

| Domain | Trusted with | Not trusted with | Verification path |
| --- | --- | --- | --- |
| **Own endpoint** | Own identity keypair, live session keys, own plaintext, local stores | Other members' keys | OS user boundary, `0600`, passphrased history |
| **Other members** | Group plaintext while members (inherent) | Roster authority, other pairs' link keys | Per-link authenticated sessions, fingerprint comparison |
| **Owner** | Membership authority (invite/admit/remove/dissolve) | Content keys of pairs they don't belong to — none exist (owner is a mesh member, key custodian of nothing extra) | Owner ops require owner-session binding + Ed25519 event signatures pinned at join (§9.5) |
| **Relay** | Routing, membership registry serialization, invite verdicts | Plaintext, any key material, forging rosters, message ids | E2E AEAD + AAD context + event signatures + fail-closed verdicts (§28) |
| **Network** | Nothing | Everything encrypted end-to-end | Phase 3 AEAD |

Explicit statement, repeated from §5 and §34 because it must not be
lost: **this design protects content confidentiality between endpoints.
It does not provide network anonymity and does not resist traffic
analysis** (§34).

## 7. Group identity

* **Format:** `gl-group-XXXX-XXXX-XXXX` — three groups of four symbols
  from the Phase 2 room alphabet (`ABCDEFGHJKLMNPQRSTUVWXYZ23456789`,
  ambiguous glyphs excluded), distinct prefix so groups are never
  confused with one-to-one `gl-room-…` channels. ≈ 60 bits of id space.
* **Generation:** minted by the **relay authority** at `GROUP_CREATE`
  (never by a client), from a CSPRNG, registry-checked for collision and
  regenerated on collision. Collision probability is negligible at 60
  bits with ≤ 32 active groups per client and relay-side registries of
  realistic size; the registry check makes correctness independent of
  probability.
* **No secret content:** the id contains no keys, tokens, timestamps, or
  identity material. It is public routing metadata (§19, §34).
* **Persistence:** relay-side in the authority's memory (volatile — see
  §28.4 restart semantics); client-side in the local `groups` store
  (metadata-only, atomic `0600` writes, Phase 1 storage discipline).
* **Validation:** strict pattern match `gl-group-(ALPHABET{4}-){2}ALPHABET{4}`
  at every trust boundary (CLI input, frames, authority ops), same rigor
  as `is_valid_room_id`. Malformed ids fail closed (§33).
* **Relationship to owner identity:** the group record pins the owner
  fingerprint + Ed25519 public key **at creation**, proven by a
  proof-of-possession signature over the create challenge (§10). The id
  itself is independent of the owner key; ownership is a registry fact,
  not a derivable property of the id.

## 8. Group membership

### 8.1 Roster entry

```
fingerprint     GLFP-…            (durable member key; roster primary key)
public_key      32-byte Ed25519   (for verifying that member's signatures)
handle          GL-XXXX           (display; NOT an authorization input)
display_name    1–24 printable    (unauthenticated claim inside the
role            owner | member     secure channel; §31.3)
joined_epoch    int ≥ 1
session_binding current relay session id (volatile, refreshed by §27.1 attestation)
```

Authorization decisions use **fingerprint + role only**. Handles and
display names are presentation data and never gate security decisions
(§31.3).

### 8.2 Authority

One **`GroupAuthority` per relay process**, modeled directly on the
verified Phase 5 `InviteAuthority`: hash/id-keyed records, **atomic
transitions with no `await` between check and write**, typed fail-closed
verdicts, monotone clocks for anything time-sensitive, public ids only in
logs. State is in-memory (matches the relay's existing posture; restart
semantics in §28.4). Phase 9 of the implementation checklist (§39)
mirrors the Phase 5 authority test battery against it 1:1.

### 8.3 Capacity

Roster size ≤ `MAX_GROUP_MEMBERS = 8` including the owner. Enforced
atomically at redemption/commit time (§12.3), never by client politeness.

## 9. Membership authorization

### 9.1 Operation table

| Operation | Who may invoke | Authority check (all atomic) |
| --- | --- | --- |
| **Create** | Any connected client | Valid name/pubkey/PoP signature; mints id; creator = owner; epoch 1 |
| **Invite** | **Owner only** | Owner session-binding matches; group ACTIVE; roster + unredeemed invite slots < capacity |
| **Accept a member (admission countersign)** | **Owner only** | Redemption verdict was REDEEMED and pending; roster < capacity; assigns epoch; two-phase sign (§13.2) |
| **Join (redeem)** | Holder of a valid group invite | Phase 5 verdict REDEEMED **and** group capacity available — decided in one atomic section (§12.3) |
| **Leave** | The member themself | Membership exists; self-signed event |
| **Remove** | **Owner only** | Target is a member; target ≠ owner |
| **Dissolve** | **Owner only** | Group ACTIVE → DISSOLVED (terminal); ACL refuses everything thereafter |
| **Change any other group state** | No one (6A has no other mutable group state) | — |

Owner self-removal is impossible: owner departure = dissolution (§14.3).

### 9.2 Two-phased signed commits

Roster mutations commit in two phases:

1. **Authority commit:** the op is validated and serialized; the epoch is
   assigned; for *exclusions* (remove/dissolve) the **routing ACL is
   applied immediately at commit** (fail-safe: excluding someone never
   waits on signatures).
2. **Signature attach:** the authority requests the authorized signer's
   Ed25519 signature over the canonical event form (§9.5) and broadcasts
   `GROUP_EVENT` only with the signature attached. Clients apply
   inclusions (join) **only** when the signature verifies — including a
   member must never be possible on the relay's word alone.

Principle: **exclusion is ACL-first; inclusion is signature-first.**
Excluding fast is safe; including fast is dangerous.

Liveness consequence: owner ops (admission, removal, dissolution) require
the owner client online to countersign. Removal/dissolution still take
effect at the ACL immediately (safety), and members stop accepting the
subject's frames once they receive the event; the roster *event* remains
pending-signature until the owner returns (flagged in UI; §33). Member
self-leave is one-phase (the leaver signs at commit time by definition
of being online to leave).

### 9.3 Concurrent membership changes

* The authority serializes ops **per group** on its single event loop;
  each op's check-and-commit is one synchronous section — the Phase 5
  technique that produced exactly-one-winner redemption, applied to
  roster mutation.
* Epochs therefore form a **single total order** per group; two racing
  ops get epochs e and e+1 in commit order, not both e.
* Conflicts resolve first-committed-first-served: remove racing join →
  whichever commits first decides (join-then-remove ⇒ subject joined and
  is removed at e+1; remove-then-join ⇒ the join's pending redemption
  fails `group/not-member` … the join is impossible because its target
  roster check sees the committed state).
* Clients always converge by applying the signed event stream in epoch
  order and re-syncing on any gap (§27.3). There is no voting, no
  consensus protocol, and none is claimed.

### 9.4 Deterministic outcomes for the racing pairs this design must handle

| Race | Deterministic rule |
| --- | --- |
| join vs join (one slot left) | First committed join wins; second gets `group/full` |
| redeem vs redeem (one invite slot) | Exactly one wins (Phase 5 atomicity) |
| remove vs remove (same target) | First wins; second `group/not-member` |
| remove vs leave | First committed wins; the other verdicts off final state |
| dissolve vs anything | After DISSOLVED, everything fails `group/dissolved` |
| invite vs roster reaching capacity | Redemption-time capacity check refuses (§12.3) |

### 9.5 Event authenticity

Canonical signed form (Ed25519, same primitive family as Phase 5
identity — no new crypto):

`ghostlink/group-event/v1 | group_id | epoch | kind | subject_fingerprint | wall_ts_utc`

Signers: join/remove/dissolve → **owner key**; leave → **leaving
member's key**. Verifiers: all members, against roster-pinned keys.
Missing/invalid signature → event not applied, group flagged *suspect*,
client refuses new sends until a consistent signed re-sync (§33). The
relay's residual power is censorship (stall events) — visible as a
suspect/stall state — never forgery.

## 10. Group creation

1. Client: `ghostlink group create --name <name>` (TUI flow mirrors).
2. Client sends `GROUP_CREATE {name, owner_pubkey, pop_signature}` where
   `pop_signature = Ed25519_sign(owner_key, "ghostlink/group-create/v1|" + challenge)` over a relay-issued handshake challenge (binds the
   request to this relay session; prevents create-request replay).
3. Authority validates: name (1–48 printable, no control chars), pubkey
   shape, PoP signature, protocol ≥ 4. On success: mints collision-free
   id, records owner fingerprint + key + session binding, roster=[owner],
   epoch=1, state=ACTIVE (single atomic section).
4. Reply `GROUP_GRANTED {group_id, epoch:1}`. Client persists local group
   metadata (§32.5). Owner's mesh is empty; the group becomes useful at
   the first admission.
5. Failure paths: malformed → typed refusal; replay of a consumed
   challenge → refusal. No partial records exist — creation is atomic.

## 11. Group invitation

### 11.1 Format — Phase 5 reuse

Group invites **reuse the Phase 5 invite system wholesale**: same 20-char
(~103-bit) CSPRNG token, same `gl://join/<token>` link form, same TTL
discipline (min 1 s, default 15 min, max 24 h; relay monotonic clock
enforcement), same atomic redemption, same creator-only revocation, same
metadata-only local persistence. The invite authority gains exactly two
fields: `kind ∈ {chat, group}` and, for kind=`group`, `group_id`. The
link itself does not reveal the group or kind (token is opaque; the
verdict response discloses, §12.2).

### 11.2 Minting rules

* Owner only; each invite carries `max_redemptions` ∈ [1, 8−rostersize]
  (default 1). `INVITE_MAX_REDEMPTIONS` (16) remains the absolute ceiling;
  group capacity (§32) is the effective one.
* TTL rules unchanged; the 1–2 s expirations exercised live in the Phase
  5 test suite must behave identically for group invites (§38).
* Multiple invites per group may be active concurrently, bounded by
  `GROUP_MAX_ACTIVE_INVITES = 8` (§32).

### 11.3 Lifecycle and security properties

Expiration, one-time-per-slot redemption, concurrent-redemption
single-winner, revocation (creator-only), session binding — all inherited
from Phase 5 and **unchanged in mechanism**. Group binding: a group-kind
invite redeems *only* into its bound group; it can never start a 1:1
session and a 1:1 invite can never join a group (kind is
authority-enforced, not client-convention).

### 11.4 Replay protection

Token hashing (SHA-256 digests only at the authority), single consumption
per slot, and verdict binding to the redeemer's relay session, all as in
Phase 5. Replayed redemption requests hit already-used/unknown verdicts.

### 11.5 Secrecy

An invite contains **no private keys, no session keys, no group keys, no
roster data** — the token carries only the capability to request one
admission. Sanity-checked in tests by log-leakage audits (§38.8).

## 12. Invitation redemption

### 12.1 Flow

1. `ghostlink group join gl://join/<token>` (also `ghostlink join …` —
   the verdict response carries `kind`, so the client branches after
   redemption; the user need not know the kind in advance).
2. Client validates the link locally (format only), submits
   `INVITE_REDEEM` to the relay.
3. Authority verdict, unchanged from Phase 5: REDEEMED / unknown /
   expired / revoked / already-used — fail-closed, atomic
   check-and-consume, monotonic+wall expiry, exactly-one-winner.

### 12.2 On REDEEMED (group kind)

The response discloses `{kind: group, group_id, owner_fingerprint,
owner_public_key, roster_snapshot {fp, handle, pubkey, joined_epoch…},
epoch, name}`. The redeemer **pins** the owner key and every roster
fingerprint before proceeding (these pins are admission-state anchors
for all later signature checks, §9.5).

### 12.3 Capacity is decided atomically

The redemption verdict and the capacity check happen in **one** atomic
section: if the roster is full, the verdict is `group/full` and the
redemption slot is **not** consumed (joining may be retried while the
invite remains valid). Consumption happens only on a successful
redemption — preserving Phase 5's atomicity while avoiding burnt invites
for a full group.

### 12.4 Session binding

The authority records the redeemer's relay session as the invite's
`bound_session` (Phase 5 property) and marks the roster-pending slot for
the joiner. The join is not complete until §13 finishes; the pending
slot expires after `GROUP_JOIN_PENDING_SECONDS = 300` and frees its
capacity.

## 13. Group join

### 13.1 Steps after a REDEEMED verdict

1. Redeemer pins owner key + roster (§12.2) and persists the pending
   membership locally.
2. Redeemer sends `GROUP_ATTEST` — Ed25519 signature over the relay
   challenge with its own identity key — binding its fingerprint to its
   current relay session (this is the §8.1 re-presentation mechanism).
3. Authority requests the **owner's admission countersign** over the join
   event (§9.2 phase 2). Owner online ⇒ signs ⇒ `GROUP_EVENT(join,
   epoch e+1)` broadcasts. Owner offline ⇒ the joiner waits in
   `PENDING_JOIN`; on `GROUP_JOIN_PENDING_SECONDS` expiry the join fails
   closed with a clear error (invite already consumed per §12.3 — the
   owner re-invites; the tradeoff is deliberate, see §40 Q1).
4. On the signed join event, the joiner establishes pairwise links per
   §17.2 and enters the chat.

### 13.2 Why countersigned admission

Without it, the *relay alone* could declare a member joined (its verdict,
its roster bit). With it, adding a member requires the owner's key —
the same standard as removal. Liveness cost: owner must be online to
admit. Accepted for 6A (small private groups; the owner who minted the
invite is typically present); revisit in §40 Q1.

### 13.3 What the joiner does and does not get

* Gets: current roster + epoch, live traffic from join forward.
* Does not get: **any history** (protocol serves none; §25.3), earlier
  epochs' keys (gone; §26), or identity secrets (none exist beyond their
  own).

## 14. Group leave

1. Member issues `GROUP_LEAVE` over an attested session; authority
   commits (epoch+1) and attaches the leaver's own signature
   (§9.5); ACL stops routing to that member at commit; event broadcasts.
2. Leaver's client: tears down all pairwise links of the group (zeroized
   per §26.4), archives the group read-only locally (history per settings;
   §32.6), renders a clear notice.
3. Remaining members: on the verified event, tear down their link to the
   leaver, leap epoch (§16.2).
4. **Owner leave ≡ dissolution** (§15.5 rationale): attempting owner
   self-leave requires confirming `group dissolve` instead; the CLI/TUI
   must say so explicitly.

## 15. Member removal

Scenario: {A, B, C} → owner removes C → {A, B}.

### 15.1 Authoritative sequence

1. Owner issues `GROUP_REMOVE(subject=C fp)`; authority validates
   (owner session-binding; C is member; C ≠ owner), commits epoch+1,
   **applies the routing ACL immediately** (C can no longer send to or
   receive from the group at the relay), requests owner countersign, then
   broadcasts `GROUP_EVENT(removed, C, epoch e+1)`.
2. Removed member's client is notified once (best effort) and thereafter
   refused `group/not-member` on any group op.

### 15.2 How A and B stop honoring C

Three independent layers, in evaluation order (defense in depth; each is
sufficient alone after it engages):

1. **Relay ACL** (immediate at commit): no routing to/from C.
2. **Roster gate** (as each client processes the verified event): C's
   frames dropped before decryption — C is off-roster.
3. **Epoch AAD** (after each client's leap to e+1): C's old-epoch
   ciphertext fails AEAD context even if hand-delivered.

### 15.3 The residual window — stated honestly

Between authority commit and a given client's event processing, frames
from C *already accepted into that client's per-link pipeline* may still
be delivered (they were legitimately sealed pre-removal). This window is
bounded by event delivery (~heartbeat scale on a live relay) and only
affects content C could have sent while still a member anyway. No
post-commit C content can arrive at an updated client. §35 L4 records
this as an accepted limitation with its bound.

### 15.4 Re-admission

Only via a **fresh owner invite** and a fresh join (new epoch, fresh
links). Nothing about C's prior membership carries over; if C's identity
key was the compromise reason, the fingerprint must not be re-admitted
— the design's remediation note (§26.5) is a UI warning at re-invite
time, not a hard-coded blocklist (owner's judgment call, logged).

### 15.5 Dissolution

Owner-only; commits epoch+1, state DISSOLVED (terminal), all forwarding
refused permanently; every client tears down and archives read-only.
Owner leave routes here (§14.4) because 6A has no owner transfer.

## 16. Group epochs

### 16.1 Definition and the running example

Epoch = per-group monotone integer, starts at 1, +1 per committed roster
mutation, assigned only by the authority (§9.3). Example:

```
epoch 1: {A}            A created the group
epoch 2: {A, B}         B admitted
epoch 3: {A, B, C}      C admitted
epoch 4: {A, B}         C left (or was removed)
epoch 5: {A, B, D}      D admitted
```

### 16.2 Distribution of membership changes

Members learn of changes **only** via verified `GROUP_EVENT`s (§9.5) —
or via roster re-sync after reconnect (§27.3). Events carry the new
epoch; clients apply strictly in epoch order; a gap ⇒ stop sending +
re-sync, never guess.

### 16.3 Key binding

Pairwise sessions bind `ghostlink/group/v1|<group_id>|<epoch>` into the
HKDF context (§17.2). Keys for (g, e) open nothing from (g, e′≠e) or
(g′≠g). This is the cryptographic enforcement of §16.1's semantics.

### 16.4 Old messages (receiver side)

A message sealed under epoch e is acceptable **only** on the pairwise
link keyed for epoch e, which lives until link teardown (§16.5). Torn
down ⇒ AEAD fails ⇒ reject (§33). Deterministic; no heuristics.

### 16.5 Drain window (exact rule)

On an epoch leap e→e+1:

* New sends use only epoch e+1 links (lazily handshaken, §16.7).
* The old epoch-e link remains acceptable **for receive-only draining**
  for `GROUP_EPOCH_DRAIN_SECONDS = 30`, covering in-flight pre-leap
  messages; then it is torn down (zeroized).
* After drain expiry, any epoch-e frame is rejected and logged. This is
  the *only* window in which two epochs are concurrently acceptable, and
  it is closed-form: accept iff (frame.epoch == current) OR
  (frame.epoch == current−1 AND now < leap_time + 30 s AND old link
  alive).

### 16.6 Removed members and future epochs

Removed at e→e+1: ACL excludes them at commit (§15.1); they hold no
(n, e+1) links (nobody handshakes with off-roster members — the roster
gate precedes handshake); even injected old ciphertext fails e+1 AAD
checks at leap'd clients. §24/§25 quantify the secrecy meaning.

### 16.7 Joiners and past epochs

Joiner admitted at e+1 handshakes only e+1 links. There is no
history-serving path anywhere in the protocol (§13.3), and old-epoch
keys no longer exist on any updated client (§26).

### 16.8 Client-side epoch memory

Clients retain metadata for **current and previous epoch only** (roster
fingerprint sets + epoch numbers — for drain acceptance and diagnostics).
Older epoch data is purged. No unbounded epoch logs (§32).

## 17. Pairwise mesh encryption

### 17.1 Reuse, verbatim, of the Phase 3 stack

Mesh links are Phase 3 sessions: the same 2-message authenticated
handshake (X25519 ephemeral per side, 16-byte nonces, AEAD key
confirmation), the same HKDF-SHA256 derivation (salt =
SHA-256(transcript), info `ghostlink/session/v1`), the same
ChaCha20-Poly1305 `seal/open_sealed` with random 96-bit nonces, the same
frame validators, the same inbox ordering/duplicate machinery and outbox
ack/requeue machinery. **No second key-exchange system exists in this
design.**

### 17.2 Group-context binding (the only deltas from 1:1)

1. `idpub` **mandatory** in both handshake directions (optional in 1:1):
   each member's Ed25519 identity key is folded into the HKDF transcript,
   so the established session key is bound to *who both parties are* —
   relay key-substitution breaks the AEAD proof, as in Phase 5, now
   guaranteed for every group link.
2. The HKDF context additionally mixes
   `ghostlink/group/v1|<group_id>|<epoch>` (both sides compute it from
   pinned roster state; the KEX wire format is unchanged).
3. **Initiator selection is deterministic:** the member with the
   lexicographically smaller identity-public-key hex initiates. No
   negotiation traffic, no both-wait/both-fire races, stable across
   reconnects.
4. Links ride the group's channel with **addressed forwarding**
   (§28.2): relay `GROUP_FORWARD {group_id, to, from, body}` with body
   opaque; relay ACL enforces both endpoints on-roster.

### 17.3 Mesh arithmetic (why 8 stays cheap)

n members ⇒ n(n−1)/2 links. At cap: 28 total, ≤ 7 live sessions per
client, ≤ 7 seals per outgoing message (each ≤ 4 KiB plaintext;
ChaCha20-Poly1305 at mobile speeds is microseconds-scale), ≤ 7
in-flight handshakes during a join burst. Worst-case work is bounded,
measured in §38 tests on loopback, and well inside Termux capability.

### 17.4 Explicitly rejected: shared/copied group key

Distributing one static key to all members (or copying the 1:1 session
key onward) is rejected because: (1) removal can never re-secure the
group — the ex-member's copy still decrypts; (2) one member compromise
exposes every member's traffic, past and present, including recorded
ciphertext; (3) no per-sender attribution — any key holder can
impersonate any other. The mesh has none of these failures at 6A's
scale. Sender keys (§36) address mesh's *efficiency* limits without
reintroducing these *security* failures.

## 18. Message encryption flow

### 18.1 Sender (deterministic order)

1. Compose plaintext; validate ≤ §32 size; require local epoch e and
   roster R (from verified state; suspect/stale state ⇒ refuse, §33).
2. Assign `message_id` (`gmsg_`+16 hex, CSPRNG) and `gseq` = sender's
   next per-group counter (persisted for the session; §22).
3. Build inner frame `GMSG {message_id, gseq, sender display_name,
   text, client_ts}`.
4. For each authorized recipient r ∈ R∖{self} (current epoch, verified
   roster): ensure-live link (g, e, self, r) — lazily handshaken per
   §16.7/§27.2; compute AAD per §20.2; `seal(link_key, inner_frame,
   aad)`; emit one `GROUP_FORWARD` envelope to r.
5. Track delivery per recipient via the link's ack machinery
   (GACK inner frames, §18.4); render `delivered k/m` with pending
   handles — **partial delivery is always explicit**.

### 18.2 Recipient (deterministic order)

1. Envelope checks: valid packet; `to == my fingerprint`; `from`
   on-roster; group known and ACTIVE; epoch acceptable per §16.5.
2. `open_sealed(link_key, body, aad)` — failure ⇒ drop + count (§31).
3. Cross-check sealed context equality with the envelope/AAD context
   (group, epoch, sender, recipient) — mismatch ⇒ reject (§20.2).
4. Validate inner frame schema; dedupe `(sender_fp, message_id)` (§21);
   gseq monotonic-accept with gap flagging (§22).
5. Deliver plaintext to the UI **attributed to the link's authenticated
   fingerprint** (display name is decoration only; §31.3); send GACK.
6. Any failure: fail closed, log metadata-only, UI notice where useful.
   The process never crashes (§33).

### 18.3 Fanout honesty

A member refusing to fan out to some recipients (§5.3) is *visible*:
those recipients' delivery state stays pending from the sender's
perspective — and other members simply never see the message. 6A offers
no broadcast consistency guarantee (§35 L1); the mitigation is small
groups + displayed delivery + Phase 7 transcript work (§36).

### 18.4 Acknowledgements

GACK/GREAD are inner-frame types on the same links (delivery ack per
message_id; cumulative read cursor per sender's gseq, optional per the
existing read-receipts setting). They inherit Phase 3 ack semantics
(bounded resends, cumulative receipts) unchanged, scoped per link and
aggregated per message at the sender.

## 19. Message envelope

Three data classes — kept disjoint by construction:

| Class | Fields | Visible to relay | Integrity |
| --- | --- | --- | --- |
| **PUBLIC ROUTING METADATA** | relay envelope: protocol v4, type `GROUP_FORWARD`, `group_id`, `epoch`, `from_fp`, `to_fp`, body length, relay timestamps | Yes | Not integrity-protected at this layer (relay needs them; tampering is caught one layer down) |
| **AUTHENTICATED DATA (AAD)** | `"ghostlink/group-msg/v1\|{group_id}\|{epoch}\|{from_fp}\|{to_fp}"` | Yes (same values as envelope, by design) | AEAD-bound: any mutation fails decryption |
| **SECRET (sealed inner frame)** | frame type, `message_id`, `gseq`, display name, message text, client timestamp | No — sealed with the pairwise link key | AEAD confidentiality + integrity |

Notes:

* AAD deliberately duplicates the envelope's context fields so the
  recipient can cross-check equality (§18.2 step 3): a relay that
  re-labels an envelope cannot produce matching AEAD-authenticated
  context.
* `message_id` and `gseq` are **sealed**, unlike Phase 3 1:1 where the
  frame's id/seq fields ride in the (base64-only, relay-decodable)
  frame. This is a deliberate group-hardening decision: it denies the
  relay selective per-message replay/drop knowledge and per-sender
  sequence visibility. It adds no crypto — only placement.
* The nonce is per-message random 96-bit, prepended to ciphertext per
  Phase 3 `seal()`.
* No padding/cover in 6A: sizes leak message length classes (§34).

## 20. Authentication and integrity

### 20.1 Message level

ChaCha20-Poly1305 per recipient: any bit-flip/truncation/substitution
fails the tag (Phase 3 `open_sealed` behavior, `DecryptionError`),
counted and dropped (§33).

### 20.2 Context binding (anti cross-context replay)

AAD binds: group id, epoch, sender fingerprint, recipient fingerprint,
protocol string. Consequences, all tested per §38:

* ciphertext from group X never opens as group Y;
* from epoch e never opens at e′;
* addressed to B never opens at C;
* a 1:1-channel ciphertext never opens as a group message and vice
  versa (protocol string separation).

### 20.3 Sender authenticity

A delivered message is provably from the holder of the sender's identity
key: the link exists only after a mutually `idpub`-bound handshake
(§17.2), so fanout frames arrive on a channel authenticated to exactly
one roster fingerprint. Forgery of another member requires their private
key. Display names are *not* authenticity (§31.3). Out-of-band
`GLFP-…` comparison (existing Phase 5 commands) remains the upgrade
path from "message from key X" to "key X is person P".

## 21. Replay protection

Layered, deterministic:

1. **Transport-of-record per link:** AEAD tag failure on any tampering;
   per-link frame sequencing and duplicate suppression from the Phase 3
   inbox (unchanged code path).
2. **Group-level dedupe:** `(sender_fp, message_id)` — bounded LRU of
   `GROUP_SEEN_IDS_PER_SENDER = 512`; older than the LRU window +
   unknown ⇒ dropped (an ancient replay is indistinguishable from a
   duplicate of a long-forgotten id; both are dropped: safe direction).
3. **gseq monotonicity** with bounded-gap acceptance (§22).
4. **Epoch scoping:** §16.5 rule; post-drain old frames are rejects,
   not duplicates-buffers.
5. **Cross-restart:** reconnect/relay-restart mints fresh link keys;
   stale ciphertext can never fit a fresh key (§27). Replay protection
   has **no persistence requirements** beyond the session: everything it
   must outlive is already invalidated by re-keying (this is why the
   seen-id LRU is intentionally memory-only).

## 22. Sequence numbers

Two sequences, different jobs — do not conflate:

| Sequence | Scope | Guarantees | On violation |
| --- | --- | --- | --- |
| **Frame `seq`** (Phase 3, per link) | One pairwise link | Strict per-link ordering/reorder-healing/duplicate drop (existing inbox) | Existing behavior: buffer within reorder window, then force-progress; duplicates re-ACKed and dropped |
| **`gseq`** (per sender per group) | Group transcript view | Per-sender monotone; survive link re-keys (they are sealed inside the inner frame, not reset by handshakes) | Accept with gap-flag up to `GROUP_GSEQ_MAX_GAP = 64` (missed or reordered — renders with a gap notice); `gseq ≤ last_seen` dedupes as replay; gap > bound ⇒ drop + suspect notice (§33) |

Cross-sender ordering is arrival order; no total order is claimed (§35).

## 23. Membership-change handling

Consolidated behavior table (mechanisms in §9, §15, §16):

| Change | Authority | ACL | Clients | Epoch |
| --- | --- | --- | --- | --- |
| Join (admitted) | commit e+1 after owner sign | admits new fp | established links to newcomer; bind e+1 | +1 |
| Leave | commit e+1, self-signed | exclude leaver | teardown link to leaver; leap | +1 |
| Remove | commit e+1, ACL-first (§15.1) | exclude subject immediately | verified event ⇒ teardown + leap; before verification the subject is still roster-valid under epoch e (§15.3's bounded window) — the ACL nonetheless blocks new subject traffic from reaching anyone | +1 |
| Dissolve | commit e+1, terminal | refuse all group traffic | teardown all links; archive read-only | +1 (terminal) |

For removals, clients that have not yet verified the event simply
operate under the pre-change epoch — bounded by §15.3 — while the ACL
already protects them from *receiving new C-origin traffic*.

## 24. Forward secrecy properties

**Definition used here:** after a compromise at time T, ciphertext
recorded *before* T remains unreadable.

* **At session/epoch granularity: YES, per link.** Link keys are fresh
  ephemeral X25519 per session; closed sessions (reconnect re-key, epoch
  leap, teardown) are zeroized (§26.4). Compromising a live session key
  does not open ciphertext sealed under *earlier* sessions of that pair
  — the DH secrets are gone. Other pairs' traffic is untouched (mesh
  isolation).
* **Within one live session: NO additional margin (partial, stated
  plainly).** The mesh has no per-message ratchet; one live session key
  opens every undelivered/recorded message of *that same* session for
  that pair. Phase 3's conversation-granularity FS is inherited
  unchanged. Per-message FS is a sender-key/ratchet property (§36).
* **Removed member / forward direction:** see §25.2.
* **Compromised endpoint (§5.8):** loses exactly {its open session keys,
  its screen, its decrypted history}; not closed sessions/epochs.

No blanket "forward secrecy: yes" claim is made; the granularity above
is the claim, and §38 tests it (keys from closed epochs cannot open
their ciphertext after zeroization — AEAD failure proof).

## 25. Backward secrecy properties

**Definition used here:** after a state change at time T, ciphertext
produced *after* T is protected from parties excluded as of T, and
parties admitted after T gain nothing from before T.

### 25.1 Member leaves or is removed at T (epoch e→e+1)

The ex-member cannot read post-T traffic: (1) no shared group key ever
existed; (2) no valid (e+1) link includes them (roster-gated
handshakes); (3) relay ACL refuses routing at commit; (4) AAD epoch
binding rejects their old material at updated clients. **Exception,
stated:** traffic of the still-current epoch e *in flight before* their
exclusion engaged (§15.3, bounded) and anything they received
legitimately pre-T (definitionally unfixable).

### 25.2 Removed member attacking future epoch reception

Even with their old identity key and old session keys: cannot complete
new handshakes (off-roster ⇒ no peer finishes a KEX with them; ACL
blocks the frames anyway); cannot decrypt what they cannot receive; a
captured full recording of post-removal ciphertext is useless without
the per-pair ephemeral DH.

### 25.3 New member joining at T (epoch e+1)

Cannot read pre-T traffic: the protocol serves no history (§13.3);
e-epoch link keys are zeroized on updated clients; their presence is
cryptographically irrelevant to already-closed epochs. **Caveat:** any
member can *voluntarily* show/paste old plaintext to a newcomer — a
social property of all group systems, documented not "fixed".

### 25.4 Post-compromise healing

Per link: reconnect/epoch re-key heals the pair (new ephemeral DH) once
the endpoint is clean. Group-wide after a member's identity compromise:
removal + epoch leap + (optionally) fresh identities for re-admission.
A *persistent* compromise of a current member yields no protocol-level
healing while they remain a member — inherent, stated.

## 26. Key lifecycle

### 26.1 Inventory

| Key | Kind | Lifetime | At rest |
| --- | --- | --- | --- |
| Identity Ed25519 (Phase 5) | Long-term asymmetric | Until user reset | Local store `0600`, never transmitted |
| Pairwise link session key | Ephemeral symmetric (32 B, HKDF output) | One session of (pair, group, epoch) | **Memory only** |
| Handshake ephemeral X25519 | Ephemeral asymmetric | One handshake | Memory only, zeroized post-use |
| History passphrase key (Phase 3) | Optional local KDF output | While unlocked | Memory only |

**There is no group key.** "Group encryption key" is a non-goal (§17.4).

### 26.2 Generation → use → rotate → teardown

* **Generation:** per §17.1 handshake; group/epoch/identity binding per
  §17.2.
* **Use:** seal/open with per-message random nonces; keys never logged
  (Phase 3 redaction contract extended to groups; §38.8).
* **Rotate:** on reconnect (Phase 3 behavior), on epoch leap (§16.5),
  on link-failure recovery. Rotation is cheap and frequent by design.
* **Teardown/zeroize:** on leave/removal/dissolve/timeout/drain-expiry/
  session close — key bytes overwritten before references drop (Phase 3
  hygiene, extended to the mesh manager's registry of links).

### 26.3 Storage prohibition

Link keys and handshake secrets are **never persisted**, never written
to swap-committed files by GhostLink itself, never placed in invites,
frames, logs, or local stores. The local `groups` store holds §8.1-style
metadata only. Verified by log/storage audits (§38.8).

### 26.4 Zeroization obligations (implementation checklist I3 references)

Teardown paths enumerated: normal close, connection error, epoch drain
expiry, roster event (leave/removed/dissolved), restart. Every path
overwrites key material; the mesh manager owns a bounded link registry
(≤7 entries + ≤7 draining) so teardown is enumerable and leak-free.

### 26.5 Compromise remediation

Identity-key compromise ⇒ owner removal + never re-admit that
fingerprint (§15.4); session-key compromise ⇒ re-key heals forward;
both are user-visible flows (removal UI + suspect notices), not silent
automation.

## 27. Reconnection behavior

### 27.1 Transport reconnect (Phase 2 machinery, reused)

`ConnectionManager` state machine + backoff/jitter + heartbeat — used
unchanged. After HELLO/WELCOME (protocol v4), a member **re-presents**
each held membership: `GROUP_ATTEST` challenge-response with the
identity key (§13.1 step 2), refreshing `session_binding`. Unknown
group ⇒ `group/unknown` ⇒ handle per §28.4/§33; never crash.

### 27.2 Link re-establishment

All live links were tied to the dead session ⇒ re-handshake per pair on
next use (lazy), deterministic initiator (§17.2). Outbox requeue +
re-seal under the **new** keys before resend (Phase 3 behavior — now
also epoch-aware): queued messages always seal against the *current*
epoch at actual send time; §16.5 forbids transmitting stale-epoch
seals. `message_id` is stable across re-seals ⇒ receivers dedupe (§21).

### 27.3 Group state synchronization (missed events / epoch gaps)

After re-presenting, the client pulls `GROUP_STATE` (authority roster +
epoch), compares with local, applies **verified** events forward in
order. Local epoch behind ⇒ catch up before sending (refuse-while-stale,
§33). Signed-event verification still gates *application*; the relay's
censorship power is bounded at stalling, which the client surfaces as a
suspect/roster-stall notice.

### 27.4 Relay restart

Authority state is volatile (§28.4): after restart, attestations answer
`group/unknown`. Clients mark the group **defunct**, preserve local
metadata/history, and render a clear notice; the owner may re-create the
group (new id) and re-invite. No silent resurrection, no client-side
roster bootstrapping against an empty authority (that would let a
restarted/ malicious relay re-constitute rosters by client report —
rejected).

## 28. Relay behavior

### 28.1 What the relay sees

Protocol envelopes; group ids; roster fingerprints + handles (metadata,
not identity secrets); epochs; join/leave/remove/dissolve events and
their signatures (public keys are roster data); frame sizes, timing,
fanout arity (§34); IP addresses and transport-level metadata.

### 28.2 What the relay must never see — and the mechanism guaranteeing it

| Never sees | Guarantee |
| --- | --- |
| Plaintext | AEAD on every content frame (§18–§20) |
| Pairwise session keys / handshake secrets | Derived endpoints-side; KEX carries only public keys/nonces (Phase 3 property) |
| Identity private keys | Never leave local storage (Phase 5 property) |
| Group encryption keys | Do not exist (§26.1) |
| message_id / gseq / inner frame fields | Sealed (§19) |
| Invite tokens | SHA-256 digests only, at the authority (Phase 5 property) |

### 28.3 Protocol surface (v4, additive, negotiated)

`GROUP_CREATE/GROUP_GRANTED`, `GROUP_ATTEST`(+challenge),
`GROUP_STATE/GROUP_ROSTER`, `GROUP_INVITE` → group-kind invite grant,
`GROUP_LEAVE`, `GROUP_REMOVE`, `GROUP_DISSOLVE`, `GROUP_EVENT` (push),
`GROUP_FORWARD` (addressed opaque routing, ACL both-endpoints-on-roster).
v ≤ 3 peers receive `protocol/unsupported`; v1–v3 traffic is untouched;
1:1 paths do not change. Symmetric schema validation, same ceilings
philosophy as FORWARD (§32).

### 28.4 Restart/volatility

The authority (like the Phase 5 invite authority) is in-memory: relay
downtime = hosted groups defunct (§27.4). Clients treat `group/unknown`
after reconnection as terminal for that group id. A future relay *may*
persist signed roster snapshots — open question §40 Q6, not assumed.

### 28.5 Honest relay sentence for all user-facing copy

"The relay routes opaque ciphertext and sees connection/group metadata.
It cannot read messages or keys. It can observe IP addresses, timing,
and traffic volume, and can refuse or delay service."

## 29. Offline members

* **No relay-side queue** (N10): frames to an offline member are not
  stored by the relay. The sender's per-recipient delivery ledger shows
  that member "offline — undelivered"; the sender's client may hold the
  message for retry within §32 caps (`GROUP_OFFLINE_QUEUE_PER_MEMBER =
  8` messages, FIFO drop-oldest with UI indication — *bounded*, never
  unlimited).
* **On reconnect:** attestation → roster/epoch re-sync (§27.3) → fresh
  links → queued items re-seal to the *current* epoch before send
  (§27.2). Duplicates at receivers are absorbed by §21.
* **Missed epochs handled, not guessed:** an offline-through-3-leaps
  member resyncs to current; their stale-epoch material is dead (§16.6);
  anything they missed is simply absent (no catch-up service; §13.3
  applies symmetrically).
* **Offline at removal:** misses the notice; on reconnect, attestation
  answers `group/not-member`; client archives read-only. Their stale
  sends (if any) are ACL-refused.

## 30. Duplicate messages

| Cause | Absorbed by |
| --- | --- |
| Re-fanout after reconnect (same message_id re-sealed) | `(sender_fp, message_id)` dedupe (§21.2) — re-ACK, don't re-render (Phase 3 duplicate etiquette extended) |
| Same message racing across retry within a link | Phase 3 inbox id/seq duplicate handling |
| Malicious duplication by relay/member | Same dedupe; AEAD tag excludes modifications |
| Duplicates of long-forgotten ids (LRU-evicted) | Dropped as unknown-old (fail-safe direction; §21.2) |

Duplicates must never double-render, must never corrupt gseq cursors,
and must re-ACK so honest senders converge on "delivered".

## 31. Malformed messages

Three enforcement gates, each independently sufficient to prevent harm:

1. **Relay envelope gate:** schema validation, type whitelist, byte
   ceilings — malformed packets never reach clients (Phase 2 property,
   extended to v4 types).
2. **Client frame/context gate:** frame schema, epoch rule (§16.5),
   roster/authorization checks, AAD cross-checks (§18.2).
3. **AEAD gate:** tag verification before any plaintext exists.

Behavioral rules: unknown message types ⇒ drop + debug log + at most one
rate-limited ERROR toward the sender; oversized ⇒ refuse before
allocation; AEAD failure counters per link ⇒ threshold
(`GROUP_AEAD_FAILURES_NOTICE = 3`) ⇒ *suspect link* UI notice (never
auto-punish beyond silence; §33). Display names: sanitized, length-capped,
control-character-stripped, rendered escaped as all user content —
**never an identity/authority input** (§31.3 cross-references §8.1).
Nothing in this section may panic, assert-crash, or exit the process.

## 32. Resource limits

Canonical constants (final names/values fixed in `constants/net.py` at
implementation; documented in [CONFIGURATION.md](CONFIGURATION.md)):

| Limit | Value | Rationale |
| --- | --- | --- |
| `MAX_GROUP_MEMBERS` | **8** (incl. owner) | See below |
| `GROUP_MAX_GROUPS` | 32 active per client | ≤ 7 links each ⇒ bounded session heap |
| `GROUP_NAME_MAX_LEN` | 48 printable | Fits panels; validated like other fields |
| `GROUP_MSG_MAX_BYTES` | 4096 (UTF-8 plaintext) | Matches existing ciphertext ceilings |
| `GROUP_FANOUT_MAX` | 7 envelopes per send | = members − 1 at cap |
| `GROUP_SEEN_IDS_PER_SENDER` | 512 (LRU) | Dedupe without unbounded memory |
| `GROUP_GSEQ_MAX_GAP` | 64 | Bounded reorder tolerance |
| `GROUP_OFFLINE_QUEUE_PER_MEMBER` | 8 (FIFO drop-oldest) | §29; never unlimited |
| `GROUP_MAX_ACTIVE_INVITES` | 8 per group | Invite surface stays auditable |
| `GROUP_JOIN_PENDING_SECONDS` | 300 | §12.4/§13.1 pending-slot hygiene |
| `GROUP_EPOCH_DRAIN_SECONDS` | 30 | §16.5 closed-form rule |
| `GROUP_EVENT_RATE` | 4 ops / 10 s / session | Abuse brake, fail-loud |
| `GROUP_FORWARD_RATE` | 20 / s / (group, sender) | Protects Termux peers from floods |
| `GROUP_ROSTER_SNAPSHOT_BYTES` | ≤ 8 KiB | Single-frame sync |
| Epoch metadata retained | current + previous | §16.8; purge older |
| History (group) | existing modes & caps | Off / session / encrypted; per-group records purged per retention |
| Local metadata retention after defunct/dissolved | 24 h purge (mirror invite retention) | No stale-secret accumulation |

**Why exactly 8 members:** (1) mesh arithmetic is quadratic —
n(n−1)/2 links — and 8 keeps the worst case at 28 links / 7 sessions /
7 AEAD seals per message, all trivially cheap today while keeping join
bursts ≤ 7 concurrent handshakes bounded by existing timeouts; (2)
roster + delivery ledgers fit small Termux screens without pagination;
(3) every security argument in this document (atomic capacity checks,
countersigned admissions, drain windows) is simpler to audit at fixed
small n; (4) correctness and auditability outrank scale — larger groups
are a sender-key problem (§36), not a mesh problem.

## 33. Failure handling

Fail-closed for security-sensitive verdicts; never crash; log
metadata-only; typed errors through the existing exception taxonomy.

| Condition | Expected behavior |
| --- | --- |
| Invalid group id | Validation error before any network I/O (CLI: exit code per taxonomy) |
| Unknown/defunct group at authority | `group/unknown` ⇒ mark defunct locally + notice (§27.4) |
| Invalid/unknown `to` member | `group/not-member`; frame refused at ACL |
| Unknown sender at client | Drop pre-decryption + log |
| Removed sender | §15.2 three layers; drop/refuse |
| Wrong epoch (older) | §16.5 closed-form: drain-accept or reject |
| Wrong epoch (newer) | Roster-stale ⇒ re-sync (§27.3), refuse to send meanwhile |
| Replayed message | §21 layers ⇒ drop (+re-ACK if in-link duplicate etiquette applies) |
| Duplicate message | §30: dedupe, no double-render, re-ACK |
| Invalid ciphertext / auth failure | Drop + counter; suspect-link notice at threshold (§31) |
| Unknown message type | Drop + rate-limited ERROR (§31) |
| Malformed packet | Gate 1/2 reject; connection stays up |
| Oversized packet | Refuse pre-allocation |
| Missing local membership state | Treat as not-a-member ⇒ refuse sends; require join/re-sync |
| Stale local membership state | Refuse sends; re-sync (§27.3); *suspect* flag until consistent |
| Owner offline for admission/removal sign | Pending per §9.2/§13.1; timeout ⇒ fail closed with clear notice |
| Invite: expired/revoked/used/full | Phase 5 typed verdicts; `group/full` does not consume slot (§12.3) |
| All link handshakes failing for a pair | Pair marked unavailable; delivery ledger shows that member undelivered; rest of mesh unaffected |
| Relay DoS/censorship | Detect (heartbeat/roster stall) + surface; never block the process |

## 34. Metadata considerations

Three distinct properties — only the first is provided:

* **CONTENT CONFIDENTIALITY: provided.** Plaintext, keys, message ids,
  and inner sequencing are protected end-to-end (§28.2).
* **NETWORK ANONYMITY: NOT provided.** The relay sees member IP
  addresses; the ISP/network sees traffic to the relay. Nothing here (or
  elsewhere in GhostLink) hides that. Tor/transport-plumbing is a
  separate roadmap concern and is not claimed.
* **TRAFFIC-ANALYSIS RESISTANCE: NOT provided.** Timing, volume,
  fanout arity (one client emitting k similar frames ⇒ likely a group
  message to k members), roster churn, and message lengths are visible
  to the relay/network. 6A has no padding, no cover traffic, no
  fixed-rate shaping.

Also visible to the relay (§28.1): fingerprints as stable pseudonymous
roster identifiers, group ids, epochs. Not visible (§28.2): everything
sealed. Any UI/docs wording for groups must use §28.5's sentence and
must never use "anonymous", "untraceable", "IP-invisible", or
"unhackable" — a greppable rule enforced by §38.8 wording audits.

## 35. Security limitations (accepted)

L1. **No broadcast consistency / transcript agreement.** A relay may
show different members different cross-sender orderings or selectively
withhold one member's fanout; members may fan out dishonestly (§18.3).
Content can never be forged, but *views* can diverge. Detection aids:
delivery ledgers, gap/suspect notices; real fix: Phase 7 transcript
chaining (§36).

L2. **Malicious members own the plaintext they receive** (§5.3). No
protocol change removes this; mitigations are social (small groups,
fingerprint verification) plus removal.

L3. **Relay/network see rich metadata** (§34). Not mitigated in 6A.

L4. **Bounded pre-verification window on removals** (§15.3): a removed
member's already-in-flight, pre-removal traffic may deliver during a
heartbeat-scale window. Post-removal-originated traffic cannot reach
updated clients.

L5. **Owner liveness gates membership ops** (§13.1): owner offline = no
admissions, unsigned pending events for removal/dissolution (ACL still
applies instantly for safety).

L6. **Relay restart destroys hosted groups** (§27.4/§28.4). Clients
fail closed to defunct; no automatic resurrection.

L7. **Mesh isolation compromises at membership granularity:** one
compromised member exposes traffic *to/from them* (and everything the
group showed them), but not other pairs' links; healing requires their
removal (§25.4).

L8. **No forward-secrecy ratchet within a live session** (§24).

## 36. Future sender-key architecture (DESIGN ONLY — not implemented)

Motivation: mesh costs O(n) seals per message and O(n²) links; beyond
~8 members this stops being "cheap and obvious". Sketch of the Phase 7
direction (its own full design document required before any code):

* **Sender keys:** each member derives a per-sender symmetric chain key
  (hash ratchet). Each message is sealed **once** under the sender's
  current ratchet step — O(1) per message per sender.
* **Distribution:** sender keys/epochs are delivered to members over the
  *existing mesh links* (this is why 6A's mesh is the control channel
  for 7's data channel — no alternate key-exchange system appears).
* **Epochs:** the §16 model carries over unchanged; roster change ⇒
  epoch leap ⇒ **immediate re-distribution of fresh sender keys** to the
  surviving roster over fresh links. Removed members hold only dead-epoch
  sender keys: useless by construction.
* **Join:** new member receives *current-epoch* sender keys only;
  ratchet gives no backward derivation (hash-chain one-wayness).
* **Replay protection:** per-(sender, epoch, ratchet-step) windows
  replace the §21.2 id-LRU for data frames; control frames stay mesh.
* **Forward secrecy:** per-message-step ratcheting heals continuously —
  strictly stronger than §24's session granularity.
* **Migration:** §37.
* **Hard requirements to accept this design later:** distribution acks
  and bounded re-distribution storms; transcript hash-chaining for L1;
  a threat-model *diff* against this document; the §38-class test
  battery scaled to key-rotation races.

## 37. Migration considerations

* **Negotiation:** protocol v4 advertises group capability; all roster
  members must be ≥ v4 (attestation carries the version; mixed-version
  admissions are refused, never downgraded). A v4 group never silently
  falls back to 1:1 channels for group content.
* **1:1 paths untouched:** Phases 2–5 wire behavior for rooms, 1:1 chat,
  transfers, and invites is unchanged; v4 is additive.
* **Local storage:** new `groups` namespace, schema-versioned (`"v": 1`)
  like other stores; readers must accept only known versions (fail
  closed, operator hint) — enabling forward migrations without corruption
  ambiguity.
* **Group-id kind separation:** `gl-group-…` vs `gl-room-…` prevents
  cross-context attachment; invite `kind` prevents cross-context
  redemption (§11.3).
* **Toward sender keys later:** frame family/version field in inner
  frames allows `GSK_*` types to coexist with `GMSG` transitionally;
  §16 epochs and §9.5 event signatures are designed to be sufficient for
  §36 without rework. A group advertises its crypto-suite in owner-signed
  state; suite upgrade = owner action + all-member re-distribution,
  epoch-leaping like any membership change.
* **Rollback stance:** no wire-level rollback from v4 to ≤ v3 for groups
  (there is nothing to roll back *to* — old relays answer
  `protocol/unsupported`, clients fail with an upgrade hint).

## 38. Testing strategy

All networking tests run against the in-process/loopback relay (the
Phase 3–5 harness pattern: `running_relay`, real sockets, no fakes).
Implementation must deliver, at minimum:

1. **Creation:** valid create; malformed name/pubkey/PoP refused; id
   format + collision-regeneration; owner pinning.
2. **Invitation:** owner-only mint (non-owner refused); TTL incl. live
   1 s/2 s expirations on a real relay; revocation (creator-only);
   `max_redemptions` boundaries; kind separation (group invite cannot
   open 1:1; chat invite cannot join group); token/log secrecy audits.
3. **Redemption:** verdict matrix (unknown/expired/revoked/used);
   exactly-one-winner races (Phase 5 style, on a real relay);
   `group/full` non-consumption + retry-after-space; pending-slot expiry.
4. **Join:** full 3–8 member admission e2e; owner-offline admission
   timeout; wrong-signature / unsigned join event rejected; roster pins
   established.
5. **Leave/Remove/Dissolve:** ACL-first exclusion (removed member's
   frames refused by relay and ignored by clients); re-admission only via
   fresh invite; owner-leave ⇒ dissolve flow; terminal-state refusals.
6. **Authorization:** every §9.1 row has a positive and negative test;
   §9.4 race matrix on the real authority; unsigned-event suspect flow.
7. **Encryption flows:** happy-path fanout k/n delivery; deterministic
   initiator selection; wrong-recipient open fails; wrong-group open
   fails; wrong-epoch open fails; 1:1-ciphertext-as-group fails
   (§20.2 matrix in full).
8. **Replay/duplicates/gseq:** in-epoch replay drop; post-drain
   old-epoch reject; re-seal-after-reconnect dedupe; LRU eviction drop;
   gap > bound suspect; duplicate re-ACK etiquette.
9. **Handshakes/identity:** idpub substitution by relay breaks the proof
   (Phase 5 test pattern, group-flavored); tampered KEX fields fail;
   handshake storms at 8 members bounded.
10. **Epochs:** leap on every mutation; drain-window accept-then-reject
    boundary tests (t = 29 s/31 s equivalents via injected clocks);
    epoch/gseq separation under forced rekeys.
11. **Offline/Reconnect:** offline queue caps + drop-oldest; reconnect
    re-attest + re-sync + re-seal-to-current-epoch; relay restart ⇒
    defunct handling; stale-state send refusal.
12. **Malformed/abuse:** unknown types, oversize, garbage at each gate;
    rate-limit trips fail loud; AEAD-failure counters + suspect notice;
    no-crash fuzz pass over frame parsers.
13. **Limits:** 9th member refused; 33rd group refused; every §32 row
    boundary-tested.
14. **Log/storage hygiene:** group flows audited for tokens/keys/
    plaintext (extend Phase 5's leak audits); local stores contain
    metadata only; retention purges run.
15. **Regression gates:** `pytest`, `ruff check`, `ruff format --check`,
    `mypy --strict`, `compileall`, `scripts/dev_check.sh` — all green,
    never weakened, no test deleted or skipped without a written reason.

## 39. Implementation checklist

Ordered so each step lands reviewable and test-gated. **Status of every
item: NOT STARTED** (design-review gate).

- [ ] I1. `GroupAuthority` + group records + protocol v4 envelope types
      (`GROUP_*`, kind-extension of invite authority) — authority unit
      tests mirroring Phase 5's battery (§8.2).
- [ ] I2. Wire/commands: attestation, create, invite, redeem+join,
      leave, remove, dissolve, state-sync — loopback integration tests
      (§38.1–§38.6).
- [ ] I3. Mesh manager: link registry, deterministic initiator,
      group/epoch-bound HKDF context, lazy handshakes, drain windows,
      zeroization paths (§17, §26) — crypto-context test matrix (§38.7,
      §38.9, §38.10).
- [ ] I4. Group messaging: inner frames (GMSG/GACK/GREAD), envelope,
      AAD, fanout ledger, inbox integration (§18–§22) — replay/duplicate/
      gseq tests (§38.8, §38.12).
- [ ] I5. Storage: `groups` namespace (metadata-only, versioned),
      retention purges (§32, §37).
- [ ] I6. CLI: `ghostlink group …` family (create/list/info/invite/join/
      leave/remove/dissolve/chat) — terminal-only; CLI e2e per the
      Phase 4 CLI-e2e pattern.
- [ ] I7. TUI: group chat surface (event stream + sender attribution),
      roster panel with fingerprints/verified marks, suspect/defunct/
      pending-state notices, `/members`, `/fingerprint`, owner commands.
- [ ] I8. Docs sync: ARCHITECTURE.md new section, CONFIGURATION.md new
      keys, ROADMAP.md status flip on completion, README — only when the
      above are true, never before.
- [ ] I9. Full gate sweep (§38.15) + wording audit (§34 greppable rule).

## 40. Open questions

Q1. **Owner-offline admission.** 6A consumes the invite at redemption and
fails the join after 300 s pending (§13.1). Alternatives: un-consumed
pending redemptions, or pre-signed delegation tokens for members. Decide
with UX data before 6B.
Q2. **Owner transfer.** Requires its own authorization design (two-party
signed handover); excluded from 6A — dissolve-and-recreate is the
documented path.
Q3. **Member-delegated invites** (invite trees): attractive for private
groups, weakens §9's single-writer clarity; needs revocation-cascade
design.
Q4. **Drain window value.** 30 s proposed (§16.5); validate against
observed reconnect latencies in field testing; make it a config key with
documented bounds (5–120 s).
Q5. **`wss://` default policy** for non-loopback relays (§5.2): recommend
in docs vs enforce in code — decision deferred to hardening phase.
Q6. **Relay-persisted roster snapshots** (signed, replay-bounded) to
survive restarts (§28.4): convenience vs enlarged relay-side metadata
footprint — needs a consent/UX story.
Q7. **Display-name collisions/uniqueness within a group:** 6A treats
names as decoration (§31.3); if UX shows confusion attacks, add
fingerprint-suffixed rendering before considering any registry role for
names.
Q8. **Read receipts in groups:** default on (consistent) or off
(metadata-frugal)? Current proposal: inherit the global setting,
documented; may flip after field feedback.

---

## Security review checklist (pre-implementation gate)

A reviewer must be able to answer **YES** to every item before §39 opens:

**Scope & substrate**
- [ ] R1. This document introduces no code, no new primitive, no new
      crypto library — verifiable by `git diff --stat`.
- [ ] R2. Every cryptographic mechanism cited exists in the verified
      Phase 3/5 stack (X25519, HKDF-SHA256, ChaCha20-Poly1305, Ed25519
      identity, transcript binding, AEAD proof, zeroization).
- [ ] R3. Phase 4 behavior is untouched and uncontradicted (file
      transfer remains one-to-one; group transfer deferred, N4).

**Threats & trust**
- [ ] R4. Every attacker class in §5 has CAN/CANNOT entries backed by a
      named mechanism — no unattributed claims.
- [ ] R5. Metadata honesty: §34/§28.5 distinguish content confidentiality
      from anonymity/traffic-analysis resistance; no anonymity claims
      anywhere (greppable).
- [ ] R6. Relay trust is minimized *and* bounded: forgery impossible
      (signatures + AEAD), censorship detectable, both stated.

**Authorization & membership**
- [ ] R7. §9.1 names exactly one authorizer per operation; no operation
      is ambiguous.
- [ ] R8. Concurrency has a single-writer story (authority serialization)
      plus client convergence (ordered verified events + re-sync);
      §9.4's race matrix is complete and deterministic.
- [ ] R9. Inclusion is signature-first; exclusion is ACL-first (§9.2) —
      the asymmetry is deliberate and understood.

**Epochs & secrecy**
- [ ] R10. Epoch semantics (§16) are closed-form: assignment, ordering,
      drain window, teardown, retention caps.
- [ ] R11. Forward-secrecy claims are granular (§24) and don't overreach;
      backward-secrecy claims enumerate join/leave/remove/compromise
      separately (§25).
- [ ] R12. The residual windows that exist (§15.3, L4, L8) are written
      down with bounds.

**Messages & replay**
- [ ] R13. The envelope's three data classes (§19) are disjoint; nothing
      secret rides in public or AAD fields; AAD cross-checks are
      deterministic (§18.2, §20.2).
- [ ] R14. Replay/duplicate/sequence handling (§21–§22, §30) needs no
      unbounded state and no new persistence.

**Limits & failure**
- [ ] R15. Every resource that grows has a row in §32 (members, groups,
      queues, caches, events, rates, retention).
- [ ] R16. Every §33 condition fails closed and cannot crash the
      process.
- [ ] R17. Limits values are justified where security-relevant (8
      members, §32 rationale).

**Tests & rollout**
- [ ] R18. §38 covers every attacker table row in §5 with at least one
      test; loopback/real-relay usage matches the Phase 3–5 pattern.
- [ ] R19. Hygiene audits (logs, storage, wording) are test obligations,
      not aspirations (§38.14, §34).
- [ ] R20. Migration (§37) cannot downgrade or confuse 1:1 paths;
      rollback fails loudly, never silently.

---

*End of Phase 6A security & design specification. Implementation of any
group functionality before this document is approved is a process
violation, not a shortcut.*
