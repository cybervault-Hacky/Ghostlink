# GhostLink Developer Portal — API Reference (Phase 10B)

Base path: `/api/v1`. All responses are JSON. Errors use a stable envelope:

```json
{ "error": { "code": "…", "message": "…" } }
```

Authenticated endpoints require the session cookie and (for unsafe methods)
the `X-CSRF-Token` header. Responses never contain secrets after creation.

## Auth

| Method | Path | Description |
| --- | --- | --- |
| POST | `/auth/signup` | Create a pending account; mints a verification token |
| POST | `/auth/verify` | Verify email with the single-use token |
| POST | `/auth/signin` | Sign in; sets session cookie + returns `csrfToken` (or `mfaRequired`) |
| POST | `/auth/mfa` | Complete sign-in with a TOTP code |
| POST | `/auth/signout` | Revoke the current session |
| POST | `/auth/password-reset/request` | Request a reset (generic response) |
| POST | `/auth/password-reset` | Reset password with the single-use token |

## Account

| Method | Path | Description |
| --- | --- | --- |
| GET | `/dashboard` | User + counts (credentials, projects, sessions, mfa) |

## Developer keys

| Method | Path | Description |
| --- | --- | --- |
| GET | `/developer-keys` | List credential metadata (no secret) |
| POST | `/developer-keys` | Create a key; returns the secret **once** |
| POST | `/developer-keys/verify` | Public: verify a presented credential |
| POST | `/developer-keys/{key_id}/rotate` | Rotate: revoke old, return new secret once |
| POST | `/developer-keys/{key_id}/revoke` | Revoke a credential (irreversible) |

## Projects

| Method | Path | Description |
| --- | --- | --- |
| GET | `/projects` | List projects |
| POST | `/projects` | Create a project |
| PATCH | `/projects/{project_id}` | Rename a project |
| POST | `/projects/{project_id}` | `{"action":"archive"}` archives a project |

## Sessions

| Method | Path | Description |
| --- | --- | --- |
| GET | `/sessions` | List sessions (with `current` flag) |
| POST | `/sessions/{session_id}/revoke` | Revoke one session |
| POST | `/sessions/revoke-others` | Revoke all other sessions |

## Security

| Method | Path | Description |
| --- | --- | --- |
| GET | `/activity` | Metadata-only security event log |
| POST | `/security/password` | Change password (requires current password) |
| POST | `/security/mfa/setup` | Begin TOTP setup; with `{"confirm":true,"code"}` enable + return recovery codes once |
| POST | `/security/mfa/disable` | Disable MFA |
| POST | `/security/webauthn/begin` | Issue a WebAuthn challenge |
| POST | `/security/webauthn/register` | Register an ES256 passkey credential |

## Security properties

* All mutations are CSRF-protected and rate-limited.
* Credential secrets are returned only from create/rotate, never from list.
* The verify endpoint returns a generic 401 for unknown/revoked/wrong keys.
* No endpoint returns a plaintext password, recovery code, session token, or
  verification material.

---

## Phase 14 — API reliability

- Consistent error envelope `{"error":{"code","message"}}`; no stack traces.
- `429` rate-limited, `503` rate-limit store unavailable, `413` payload too
  large — all surfaced by the Termux client with clear, secret-free errors.
- Activity endpoints are bounded (`LIMIT`) to avoid unbounded queries.
- IDOR/scope-escalation/replay/revoked-credential behavior covered by the
  security regression suite (`test_phase13_security.py`,
  `test_owner_invariant.py`).
