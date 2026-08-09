# Observability (Phase 13J/K)

## Status

**IMPLEMENTED & TESTED** (with tests that inject secret-shaped values and
assert they never reach logs).

## Structured JSON logging

`portal_server.observability.StructuredLogger` emits one JSON line per event
with a safe allow-list of metadata keys:

`ts`, `level`, `message`, `request_id`, `method`, `route`, `status`,
`duration_ms`, `authenticated_user_id`, `developer_id`, `device_id`,
`security_event_type`, `deployment_version`, `actor`, `action`, `outcome`,
`category`.

**Never logged:** passwords, API keys, developer secrets, access/refresh
tokens, session cookies, CSRF tokens, MFA codes, recovery codes, pairing
codes, authorization headers, database credentials. Free-form message text is
run through `scrub_secrets` defensively (redacts shaped tokens such as
`Bearer …`, `dk_…`, `GL-…`, 64-hex hashes, JWTs).

## Request correlation (13K)

Every request gets a request id:

- A client-supplied id is **validated** (alphanumeric `_ -`, length ≤ 64) and
  capped; malformed values are ignored.
- Otherwise a server-side id (`req_…`) is generated.
- Returned as `X-Request-ID` (configurable via `REQUEST_ID_HEADER`).
- Used in structured logs. **Request ids are never used as authentication.**

## Audit logging (13L)

Security events (login success/failure, logout, MFA, WebAuthn, password
changes, credential lifecycle, device lifecycle, pairing, token rotation,
suspicious failures) are recorded as append-oriented, timestamped,
attributable, queryable, non-secret rows in `security_events`. Sensitive
payloads are never stored. Retention is configurable (`RETENTION_*_DAYS`).
