# GhostLink Developer API — Security (Phase 12)

## Authentication

Short-lived bearer access tokens + rotating refresh tokens. Both are stored
as SHA-256 hashes at rest. Permanent credentials are used only once at
token issuance.

## Authorization

* Scopes are validated server-side (`normalize_scopes` rejects unknown
  scopes) and enforced on every protected endpoint.
* **No owner scope exists.** A developer credential can never receive Owner
  privileges; there is no endpoint that grants the owner role.
* Project binding is resolved server-side from the token/credential — a
  client cannot change its project identity via a request parameter.

## Device identity

Device IDs are cryptographically random (no IMEI, MAC, Android ID, IP, or
hardware identifiers). Devices are revocable and revoked devices fail
closed.

## Pairing

Short-lived, single-use, random pairing codes (never the permanent
credential). Rate-limited and invalidated on success/expiry.

## Replay / expiry

* Access tokens expire; expired tokens are rejected.
* Refresh tokens rotate on use and are single-use.
* Revoked credentials/devices/tokens are persistent across restarts.

## Activity

API activity is metadata-only (timestamp, developer/device/project,
endpoint category, result). The Authorization header, tokens, credentials,
and request bodies are never recorded.

## Honest limitations

* Rate limits are per-process/in-memory, not distributed/global.
* The local token store holds refresh tokens at rest (0600) because the CLI
  must be able to refresh; this is documented, not hidden.

## Rate limiting (Phase 12P)

Developer-API endpoints are rate-limited (per-process, in-memory):

* pairing (begin / approve) — 10 per 5 min
* token issuance — 20 per 5 min
* token refresh — 20 per 5 min
* credential rotation — 10 per 5 min

The portal's existing web endpoints (sign-in, sign-up, password reset,
MFA, etc.) retain their limits. All limits are per-process, not
distributed/global — documented honestly.

## Portal UI (Phase 12N)

The portal web UI exposes developer-API surfaces via session-authenticated
endpoints under `/api/v1/devapi/*`:

* `GET /api/v1/devapi/devices` — list developer devices
* `POST /api/v1/devapi/devices/{device_id}/revoke` — revoke a device
* `GET /api/v1/devapi/credentials` — list scoped API credentials (metadata only)
* `POST /api/v1/devapi/credentials/{credential_id}/revoke` — revoke a credential
* `GET /api/v1/devapi/activity` — metadata-only API activity
* `GET /api/v1/devapi/pairing` — active pending pairing codes

Frontend pages: `Developer API`, `Devices`, `API activity`. These display
metadata only — never secrets, tokens, or Authorization headers.
