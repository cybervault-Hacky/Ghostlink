# GhostLink ↔ Portal — Secure Pairing

## Flow

### One-Click Connect Device Flow (Recommended)
1. **User clicks "Connect this device"** on the Developer Portal →
   `POST /api/v1/devapi/pairing/create` creates a short-lived pairing session
   and displays the pairing code (e.g. `GL-XXXX-YYYY`) and QR code.
2. **Termux runs** `ghostlink developer pair GL-XXXX-YYYY` (or `ghostlink developer start`) →
   `POST /api/v1/developer/auth/pair-complete` securely exchanges the pairing code
   for short-lived access and rotating refresh tokens stored at 0600 in the local token store.
3. **Portal updates in real time** showing the connected Termux device. Permanent credentials
   are never printed in the terminal.

### Manual Pairing Flow
1. **Termux** runs `ghostlink developer device register` → the portal
   `POST /api/v1/developer/auth/pair-begin` returns a short-lived,
   single-use, random **pairing code** (never a permanent credential).
2. **User approves** on the portal (session) via
   `POST /api/v1/developer/auth/pair-approve` with the code → a scoped
   credential is issued.
3. **Termux** runs `ghostlink developer login` with that credential →
   `POST /api/v1/developer/auth/token` mints the access + refresh token
   pair.

## Properties

* Pairing codes are random, short-lived (10 min), single-use, rate-limited,
  and invalidated on success or expiry.
* Permanent credentials are never used as pairing codes.
* The credential secret is never printed in terminal output during one-click pairing.
* Local tokens are stored under 0600 permissions in `tokens.json`.

## Status

IMPLEMENTED and TESTED (real-socket E2E).
