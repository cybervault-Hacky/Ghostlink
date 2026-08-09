# GhostLink — Developer Accounts & Credential Infrastructure (Phase 10A)

Phase 10A introduces a **local, production-grade Developer Account system**
for GhostLink developers. It establishes a local developer identity and
manages cryptographically random API credentials that a future developer
portal (**Phase 10B**) can authenticate against.

> **Status: Phase 10A is local-only.** There is **no website, no remote
> service, no API endpoint, and no secret upload** in Phase 10A. The
> credential is never transmitted anywhere unless a future authenticated
> registration flow (Phase 10B) explicitly sends it. Nothing here makes a
> network request.

---

## 1. What a developer credential is — and is not

A **developer credential** is an API authentication secret for a developer
account. It is entirely distinct from GhostLink's other keys:

| Key | Purpose | Namespace |
| --- | --- | --- |
| Messaging identity key (Ed25519) | E2E identity, sessions, group membership | `GL-…` / `GLFP-…` |
| Session / group / sender keys | E2E message encryption | internal |
| One-time invite token | Join a room/group | `gli_…` |
| **Developer credential** | **Local developer API authentication** | `gl_dev_…` |

The developer credential is **never derived from** any of the above, nor
from the username, email, device ID, timestamp, PID, MAC/Android ID,
filesystem path, identity fingerprint, group/room ID, invite token,
password, or any predictable counter. It is generated with the Python
standard-library CSPRNG (`secrets.token_bytes`). `random.random()` and
`uuid4` are never used as the entropy source.

**Principle: public identifiers ≠ authentication secrets.** A developer
account has public metadata (`developer_id`, `key_id`, `created_at`,
`status`, `last_used_at`) and a separate secret that is never stored in
plaintext.

---

## 2. Credential format

```
gl_dev_<key_id>_<secret>
```

* `key_id` = `dk_` + **8** Crockford-base32 chars from **5 random bytes**
  = **40 bits** of identifier entropy. Public, non-secret; used to look up,
  list, revoke, and audit a credential.
* `secret` = unpadded base64url of **32 random bytes = 256 bits** of CSPRNG
  entropy. This is the authentication secret.
* `developer_id` = `dev_` + 13 Crockford-base32 chars from **8 random
  bytes** = **64 bits** of public account-identifier entropy.

Example (fake — never use a real-looking value in docs):

```
gl_dev_dk_7K4MQ2W3_<43-character-base64url-secret>
```

> The full credential is shown **exactly once** at creation/rotation, with
> an explicit *"Save this key now. It will not be shown again."* warning.
> After that, every command (status, list, doctor, security-status,
> export-info) shows only redacted metadata.

---

## 3. Entropy policy

| Field | Bytes | Entropy | Source |
| --- | --- | --- | --- |
| `secret` | 32 | 256 bits | `secrets.token_bytes(32)` |
| `key_id` body | 5 | 40 bits | `secrets.token_bytes(5)` |
| `developer_id` body | 8 | 64 bits | `secrets.token_bytes(8)` |
| verification salt | 16 | 128 bits | `secrets.token_bytes(16)` |

256 bits of secret entropy means offline guessing is computationally
infeasible. Key IDs carry enough entropy to make collisions negligible for
the bounded credential set (≤ 4 active), and uniqueness is enforced.

---

## 4. Storage model

Developer account + credential metadata live under the existing data
directory:

```
<data-dir>/
    developer/
        account.json       (developer_id, created_at, status, schema version)
        credentials.json    (key_id → status, timestamps, verification material)
```

* Written with the existing **atomic** `JsonFileStorage` (0600, temp +
  fsync + `os.replace`) — a crash never leaves a partial document. The
  credential document itself is written in a single atomic `replace`; the
  account-metadata document is written first and credentials last, so an
  interruption between the two leaves the previous (still-consistent)
  credential set rather than a torn one.
* Files are `0600`; the directory is `0700`. Storage is **hardened on
  access** (chmod) and refuses a symlinked path.
* Every document carries an explicit **schema version** (`v`). A document
  from a **newer** GhostLink is **rejected (fail closed)**, never silently
  reinterpreted (Phase 9 migration infrastructure).
* The plaintext secret is **never written to disk** — only salted
  verification material and public metadata.

---

## 5. Verification model

We do **not** store the plaintext secret. For each credential we store:

```
verifier = "v1"
salt     = 16 random bytes (base64url)
hash     = HKDF-SHA256(salt=salt, info="ghostlink/developer-key/v1", ikm=secret)
```

* The random salt binds each stored hash to that credential and prevents
  precomputation/reuse across credentials.
* Comparison uses **constant-time** `hmac.compare_digest`.
* Because the secret has 256 bits of entropy, a leaked hash does not enable
  offline guessing; the salted-HKDF construction is still the standard,
  versioned choice (not a bare `SHA-256(key)`).
* A `verifier` version field means a future, incompatible scheme fails
  closed instead of being misinterpreted.

This clearly distinguishes **verification material** (stored) from
**credential plaintext** (shown once, never stored).

---

## 6. Credential lifecycle

Commands:

| Command | Behavior |
| --- | --- |
| `ghostlink developer init` | Create the account + first credential (shown once) |
| `ghostlink developer status` | Account + credential metadata (no secret) |
| `ghostlink developer key create` | Add a new active credential (shown once) |
| `ghostlink developer key list` | List credential metadata |
| `ghostlink developer key rotate` | Revoke all active, issue one new (shown once) |
| `ghostlink developer key revoke <key-id>` | Revoke a specific credential |
| `ghostlink developer export-info` | Export public metadata as JSON (no secret) |

### Rotation
1. Generate a completely new credential (distinct `key_id` and secret).
2. Revoke every previously active credential.
3. Persist atomically.
4. Display the new credential exactly once; the old secret is never shown.

### Revocation
* Explicit and irreversible (unless a new credential is generated).
* Survives restart (persisted) — a revoked key never becomes valid again
  because the process restarted.
* A revoked key's verification always fails.

### Bounded model
An account holds at most **`MAX_ACTIVE_CREDENTIALS = 4`** active
credentials. Rotation keeps the ceiling; adding beyond it is refused until
one is revoked.

---

## 7. Rate limiting (local)

Local verification is protected from brute-force abuse with an in-memory,
per-process limiter (deterministic and testable):

* `DEFAULT_MAX_FAILED_ATTEMPTS = 5` failures within
* `DEFAULT_WINDOW_SECONDS = 300` triggers
* `DEFAULT_COOLDOWN_SECONDS = 60` of lockout.

A successful verification resets the counter. Because the secret has 256
bits of entropy, this is defense-in-depth, not the primary protection.

---

## 8. Audit logging

Developer events are logged as metadata only:

```
developer_account_created
developer_key_created
developer_key_rotated
developer_key_revoked
developer_key_verification_failed
```

The raw key, secret, password, private key, and verification hash are
**never** logged. The secret is additionally registered with the Phase 8
log-redaction backstop so even a code-path slip is scrubbed. Regression
tests prove no secret reaches logs or exceptions.

---

## 9. Doctor & security-status

* **`ghostlink --doctor`** reports: developer account configured/absent,
  credential storage health, permissions security, and corruption. It never
  shows the secret.
* **`ghostlink security-status`** shows developer account status,
  `developer_id`, credential count, per-credential key IDs, status, and
  created/last-used timestamps. Never the secret.

---

## 10. Local security assumptions & limitations

* Storage is hardened (0600/0700), symlink-refusing, and fail-closed.
* **Best-effort, not absolute:** if the Android/Termux/Linux environment
  itself is compromised, local secrets cannot be guaranteed safe. GhostLink
  does **not** claim device security, anonymity, or protection against a
  compromised OS.
* Developer-account identity is **logically separate** from the messaging
  Ed25519 identity. It is never derived from, nor a replacement for, the
  E2E identity system.

## 11. Optional local protection

Phase 10A does **not** introduce a password-based unlock for the developer
credential. A password-encryption layer would require a standard KDF +
authenticated encryption + versioned storage (non-trivial scope). This is a
documented **future hardening item** rather than unsafe custom crypto.

---

## 12. Phase 10B integration contract

Phase 10B (a future developer portal) can authenticate against the local
developer account **without the user exposing the secret to GhostLink
itself beyond the registration step**:

* **What the server needs:** the user supplies the `gl_dev_…` credential
  during an explicit, authenticated registration. The server stores the
  same salted-HKDF verification material (or an equivalent versioned
  construction) and verifies future API calls by hashing the presented
  secret.
* **What never leaves the device:** the plaintext secret is only presented
  by the user (out-of-band) at registration/auth time — GhostLink itself
  never uploads it, and `export-info` emits **only** public metadata.
* **Identifiers:** `developer_id` and `key_id` are the stable public
  identifiers the portal records.
* **Lifecycle:** the portal mirrors local revocation/rotation semantics —
  a revoked `key_id` must be rejected server-side too.
* **Server-side expectations:** verify with constant-time comparison against
  salted verification material; reject unknown/revoked/stale `key_id`s; do
  not accept a plaintext secret that was logged or transmitted insecurely.

Phase 10A defines this interface but does **not** implement any cloud
service or fake endpoint.

---

## 13. Security review summary

* Secrets are CSPRNG-generated, high-entropy, shown once, never persisted
  in plaintext, never logged, never uploaded.
* Storage is atomic, versioned, permission-hardened, symlink-refusing, and
  fail-closed on corruption/future schema.
* Verification is constant-time against salted HKDF material; revocation is
  persistent and irreversible; rotation is atomic.
* Local rate limiting bounds brute force.
* The module makes **zero network requests** (tested) and contains no
  telemetry, analytics, or remote logging.
