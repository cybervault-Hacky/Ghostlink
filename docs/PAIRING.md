# GhostLink ↔ Portal — Secure Pairing (Phase 12)

## Flow

1. **Termux** runs `ghostlink developer device register` → the portal
   `POST /api/v1/developer/auth/pair-begin` returns a short-lived,
   single-use, random **pairing code** (never a permanent credential).
2. **User approves** on the portal (session) via
   `POST /api/v1/developer/auth/pair-approve` with the code → a scoped
   credential is issued and its **secret is revealed exactly once**.
3. **Termux** runs `ghostlink developer login` with that credential →
   `POST /api/v1/developer/auth/token` mints the access + refresh token
   pair.

## Properties

* Pairing codes are random, short-lived (10 min), single-use, rate-limited,
  and invalidated on success or expiry.
* Permanent credentials are never used as pairing codes.
* The credential secret is shown once and never stored by the CLI.

## Status

IMPLEMENTED and TESTED (real-socket E2E). NOT VERIFIED: a live internet
deployment (local-first).
