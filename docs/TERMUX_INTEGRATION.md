# GhostLink ↔ Portal — Termux Integration (Phase 12)

A GhostLink installation on Termux (or Linux) can register as a developer
device and communicate with the Developer Portal via the scoped Developer
API.

## Status

* **IMPLEMENTED** — Developer API (`/api/v1/developer/*`), scoped bearer
  tokens, device registration, pairing, project listing, credential
  lifecycle.
* **IMPLEMENTED** — Termux CLI (`ghostlink developer login/device/project/
  credential/security-status/doctor`).
* **TESTED** — real-socket E2E pairing → token → scoped call → refresh →
  revocation.
* **NOT IMPLEMENTED** — a production frontend portal page for Devices/
  Projects (the API is fully functional; a portal UI page is deferred).

## Termux CLI

```
ghostlink developer device register        # shows a short-lived pairing code
ghostlink developer login                  # exchange the credential (shown once) for tokens
ghostlink developer whoami                 # show developer id + scopes
ghostlink developer device list|revoke <id>
ghostlink developer project list
ghostlink developer credential status
ghostlink developer security-status
ghostlink developer doctor                 # local store + API health (no secrets)
ghostlink developer logout
```

The portal URL is taken from `--relay` or the configured relay URL; the
local token store lives under `<data-dir>/developer_portal/` at 0600 with
atomic writes and symlink refusal.

## Secrets policy

* Permanent developer credentials are entered at login time and are never
  stored locally.
* Only short-lived access tokens and rotating refresh tokens are persisted
  (0600). Logout clears them.
* Tokens are never printed, logged, or placed in URLs.
* A network failure is never treated as successful authentication.
