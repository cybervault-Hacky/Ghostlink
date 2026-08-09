# GhostLink — Backup & Recovery Guidance

This document tells you exactly what is safe to back up and what is
sensitive, so you can back up GhostLink without accidentally leaking
secrets. GhostLink provides **no cloud backup** and **never transmits your
private keys anywhere**; the guidance here is for your own local backups.

## Where GhostLink stores things

By default (override with `storage.data_dir`):

| Path | Contents |
| --- | --- |
| `<data_dir>/config.toml` | Configuration (the file GhostLink generated) |
| `<data_dir>/state/identity.json` | **Your identity keypair — SENSITIVE** |
| `<data_dir>/state/*.json` | Groups, invites, rooms, session metadata |
| `<data_dir>/logs/ghostlink.log*` | Logs (metadata only — no secrets by design) |
| `<data_dir>/history/` | Encrypted history file (if you enabled `chat.history_mode = "encrypted"`) |
| `~/Download/GhostLink/` | Received files (plaintext, decrypted) |

## Backupable vs sensitive

**BACKUPABLE** — safe to include in a normal backup:

- `config.toml` (non-secret settings)
- group/invite/room metadata (fingerprints, names, epochs — no keys)
- logs (already secret-free)
- encrypted history file, **if and only if** you also back up the
  passphrase separately and treat that file + passphrase together as
  sensitive
- any documentation or notes you keep

**SENSITIVE — handle like a password:**

- `state/identity.json` — the Ed25519 private key. Anyone who gets this
  file can impersonate you. If you back it up, protect it (encrypted
  backup, `0600` permissions, never shared).
- The encrypted-history passphrase.
- Any decrypted message history you exported as plaintext.

**NEVER:**

- print, log, or upload your private key or passphrase
- share `identity.json` or your passphrase with anyone
- enable a "cloud sync" of the data directory

## Recovery

Because identity is ephemeral and local, the practical recovery story is:

- **If you lose the device but backed up `identity.json`:** restore the
  data directory; your identity and (metadata-only) group records come
  back. Groups whose relay instance restarted will be `defunct` and must
  be re-created by the owner — that is a documented design behavior, not a
  bug.
- **If you did not back up `identity.json`:** you get a fresh identity.
  This is the intended model (no accounts); peers re-verify your new
  fingerprint out-of-band.

## Detecting corrupted state

`ghostlink --doctor` reports data-directory health and storage writability.
On a corrupt JSON document, GhostLink raises a typed error and refuses to
load it rather than guessing — see `docs/ROADMAP.md` Phase 9 and the crash-
consistency tests. A corrupted document should be inspected; if it is not
recoverable, removing it lets GhostLink recreate fresh state (never
silently reinterpret a corrupt or newer-version document).

## Versioning guarantee

Every state/config document carries a schema version and GhostLink **fails
closed** on a document written by a *newer* version (it refuses to
reinterpret it). If you restore an old backup over a newer install (or vice
versa), GhostLink reports the version mismatch instead of misreading your
data — restore the matching version or migrate deliberately.
