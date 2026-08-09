# GhostLink Developer Portal — Operations (Phase 11)

## Observability (without tracking)

The portal emits **metadata-only** operational logs — no behavioral
analytics, no advertising trackers, no invasive telemetry.

Logged (INFO via the `ghostlink.portal` loggers):

* startup / shutdown
* request method, path (never the query string), HTTP status, duration
* security events (written to the `security_events` table)

Never logged: passwords, permanent credentials, TOTP secrets, recovery
codes, WebAuthn private material, session tokens, CSRF tokens, email
verification tokens, password reset tokens, or request bodies.

## Health

`GET /health` returns `{"ok": true}` unauthenticated (no data).

## Migration health

On startup the schema is migrated and validated; the current version is
readable via `schema_meta`. A future schema fails closed.

## Rate limiting

In-memory, per-process, sliding-window limits for sign-up, sign-in,
password reset, email verify, MFA, credential create/rotate/revoke, and
session actions. Overrides via `RATE_LIMIT_*`. Limits are per-process, not
distributed/global — documented honestly.

## Backup

See [docs/DISASTER_RECOVERY.md](DISASTER_RECOVERY.md).

---

## Phase 13 — administration commands & retention

**IMPLEMENTED & TESTED.** Production administration commands (deterministic
exit codes; never expose secrets):

```
ghostlink db status          ghostlink db migrate      ghostlink db verify
ghostlink backup create      ghostlink backup verify  ghostlink backup list
ghostlink backup restore --dry-run
ghostlink system health      ghostlink system readiness
ghostlink security-audit
```

These delegate to the portal backend ops layer (`python -m portal_server.manage`).

**Data retention (13O)** — bounded, idempotent, transactional cleanup via
`RETENTION_*_DAYS` for sessions, expired tokens, security events, API
activity, pairing records and expired verification tokens. Cleanup never
deletes active security state. **NOT VERIFIED** against live PostgreSQL in
this environment.

---

## Phase 14 — alerting / monitoring model

**DOCUMENTED ONLY** (provider-neutral; no SaaS integrated). Alerts should be
raised for:

- application unavailable (`/health/live` 5xx)
- readiness failure (`/health/ready` 503) — DB unavailable, migration mismatch,
  missing production config
- database unavailable / connection-pool exhaustion
- migration failure or checksum mismatch
- backup failure and backup-verification failure
- high authentication-failure rate (from `security_events` / `failed_signin`)
- rate-limit backend failure (`503 rate_limit_store_unavailable`)
- excessive 5xx and high latency (from structured request logs)
- disk exhaustion (backup/storage volume)
- TLS certificate expiry (reverse proxy)

The structured JSON logs (`event`, `request_id`, `route`, `status`,
`duration_ms`, `error_class`, `environment`) and `/health/*` endpoints provide
the raw signals; any alerting system can consume them. No telemetry or
third-party analytics is added.

---

## Phase 15 — severity model & escalation

`portal_server.securityseverity` classifies operational/security events:
INFO / NOTICE / WARNING / HIGH / CRITICAL. Events at WARNING and above trigger
escalation. Classified events (never containing secret payloads):

- authentication failure (spikes → HIGH)
- rate-limit exhaustion
- refresh-token replay (WARNING)
- credential / device revocation (NOTICE)
- suspicious scope request (WARNING)
- malformed authentication (NOTICE)
- migration integrity failure (CRITICAL)
- backup integrity failure (HIGH)
- configuration failure (CRITICAL)
- repeated 5xx (WARNING)

Operational event categories: startup, shutdown, request_error,
authentication_failure, rate_limit, migration, backup, restore,
credential_revocation, device_revocation, security_alert, owner_boundary,
refresh_replay, configuration_failure.

**ENVIRONMENT-GATED**: live PostgreSQL and container execution are not
available here; `ghostlink production check`, the Docker static check, the
secret scanner, and the gated PostgreSQL/DR test suites provide the
deterministic verification path.
