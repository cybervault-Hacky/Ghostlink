# GhostLink — Device Security (Phase 12)

## Device identity

A registered developer device gets a cryptographically random `device_id`
(~100 bits). It is **not** derived from a phone number, Android ID, IMEI,
MAC address, IP address, username, or filesystem path.

## What is never collected

No IMEI, SIM information, contacts, personal files, precise location, or
biometric data. WebAuthn/passkeys are handled by the device authenticator;
the server receives only cryptographic assertions.

## Revocation

Devices can be listed (`device:read`) and revoked (`device:write`). A
revoked device's tokens are revoked too; revoked devices fail closed and the
revocation persists across restarts.

## Local store

The Termux CLI stores only short-lived access + rotating refresh tokens at
0600 with atomic writes, symlink refusal, schema versioning, and corruption
detection. Permanent secrets are never stored locally.
