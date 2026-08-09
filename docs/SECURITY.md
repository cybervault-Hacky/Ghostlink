# GhostLink — Security

This document summarizes GhostLink's security posture. For the full group /
sender-key / developer-account security models, see
[docs/GROUPS.md](GROUPS.md), [docs/DEVELOPER_ACCOUNTS.md](DEVELOPER_ACCOUNTS.md),
and [docs/ARCHITECTURE.md](ARCHITECTURE.md).

## Cryptographic primitives

GhostLink uses only established, audited primitives via the `cryptography`
library — no custom crypto:

* **X25519** — ephemeral key agreement (handshake / session / group links)
* **Ed25519** — ephemeral identity keys, group membership signing
* **HKDF-SHA256** — key derivation (sessions, sender-key chains, developer
  credential verification)
* **ChaCha20-Poly1305** — AEAD message/file encryption

## Key inventory

| Key | Lifetime | At rest |
| --- | --- | --- |
| Identity Ed25519 | Until user reset | Local store `0600`, never transmitted |
| Session / group / sender keys | In-memory | Memory only, zeroized best-effort |
| **Developer credential** | Until revoked/rotated | **Never plaintext** — only salted HKDF verification material `0600` |
| History passphrase key | While unlocked | Memory only |

## Secrets are never

* logged (a redaction backstop scrubs registered secrets and token shapes),
* placed in exceptions,
* stored in plaintext,
* uploaded or transmitted (the developer-account module makes zero network
  calls),
* derived from predictable device/user identifiers.

## What GhostLink does not claim

GhostLink does **not** claim:

* anonymity, invisibility, or untraceability — the relay observes IPs,
  timing, and traffic volume;
* protection against a fully compromised endpoint or OS;
* perfect forward secrecy beyond what is implemented (documented honestly
  in [docs/GROUPS.md](GROUPS.md) §24/§36);
* device security for local developer credentials once the OS is
  compromised.

## Security model documents

* **Groups / messaging** — [docs/GROUPS.md](GROUPS.md) (§5 threat model,
  §24 forward secrecy, §25 backward secrecy, §34 metadata, §41 recovery)
* **Developer accounts** — [docs/DEVELOPER_ACCOUNTS.md](DEVELOPER_ACCOUNTS.md)
* **Architecture** — [docs/ARCHITECTURE.md](ARCHITECTURE.md)
