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
