# GhostLink Developer API — v1 (Phase 12)

Base path: `/api/v1/developer/*`. This is a narrowly scoped, authenticated,
auditable, revocable developer API for an authenticated Termux/Linux
installation to talk to the GhostLink Developer Portal.

## Authentication

Developer endpoints authenticate with a **short-lived bearer access token**
plus a **rotating refresh token**:

* `Authorization: Bearer <access_token>`
* Access tokens live ~15 minutes; refresh tokens ~30 days and rotate on use.
* Tokens are opaque random values stored only as hashes at rest. They are
  never placed in URLs, query strings, or logs.
* Permanent developer credentials are used **only** at
  `/auth/token` to mint the first token pair.

## Error envelope

```json
{ "error": { "code": "…", "message": "…", "request_id": "…" } }
```

No stack traces, database errors, internal paths, or secret material.

## Scopes

Least-privilege scopes are checked server-side and never trusted from the
client. There is **no owner/root/admin scope** — developer credentials can
never receive Owner privileges.

| Scope | Grants |
| --- | --- |
| `project:read` | list projects |
| `project:write` | (reserved) |
| `device:read` | list devices |
| `device:write` | revoke devices |
| `credential:read` | list/revoke credentials |
| `credential:rotate` | rotate credentials |
| `security:read` | API security activity |

## Endpoints

| Method | Path | Auth | Scopes |
| --- | --- | --- | --- |
| POST | `/auth/pair-begin` | none | — |
| POST | `/auth/pair-approve` | session | — |
| POST | `/auth/token` | credential | — |
| POST | `/auth/refresh` | refresh token | — |
| GET | `/devices` | bearer | `device:read` |
| POST | `/devices/{device_id}/revoke` | bearer | `device:write` |
| GET | `/projects` | bearer | `project:read` |
| GET | `/credentials` | bearer | `credential:read` |
| POST | `/credentials/{credential_id}/rotate` | bearer | `credential:rotate` |
| POST | `/credentials/{credential_id}/revoke` | bearer | `credential:read` |
| GET | `/security/activity` | bearer | `security:read` |
| GET | `/health` | none | — |

## Pairing

1. Termux: `POST /auth/pair-begin` → returns a short-lived, single-use
   pairing code.
2. User approves on the portal (`POST /auth/pair-approve`, session) → a
   scoped credential is issued and its secret is revealed **once**.
3. Termux: `POST /auth/token` with the credential → access + refresh tokens.

## Versioning

The API is `v1`. Future incompatible changes use `v2`; v1 semantics are not
silently changed.
