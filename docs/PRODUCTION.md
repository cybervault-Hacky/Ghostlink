# Production (Phase 13)

This document is the operator-facing overview of running the GhostLink
Developer Portal in production. It is **honest about status**:

- **IMPLEMENTED** — the code, configuration, deployment artifacts, tests and
  documentation described below exist and pass the repository quality gates.
- **TESTED** — everything not marked otherwise passes automated tests (SQLite
  runtime, static PostgreSQL adapter tests).
- **NOT VERIFIED** — PostgreSQL runtime integration was not available in the
  build environment; see `DATABASE_PRODUCTION.md`.
- **NOT IMPLEMENTED / NOT DEPLOYED** — no public deployment exists. GhostLink
  is **not deployed to any public server**.

## Top-level guarantees

- **Exactly one Owner.** Developer accounts can never become Owner, grant
  Owner, transfer ownership, or request `owner:*` scopes. This is an immutable
  invariant enforced by tests.
- **Secrets are never logged, printed, or committed.** Database URLs,
  credentials, tokens, cookies, CSRF tokens, MFA/recovery codes and pairing
  codes are redacted/omitted by the observability layer and the CLI.
- **Fail closed.** Production rejects missing/insecure configuration, refuses
  unknown schema versions, refuses migration-checksum mismatches, and refuses
  to silently use per-process rate limiting for a PostgreSQL deployment.

## Production requirements (fail closed)

`APP_ENV=production` requires, and the portal refuses to start without:

| Variable | Notes |
|----------|-------|
| `DATABASE_URL` | PostgreSQL for multi-process production |
| `SESSION_SECRET` | must not be the development default |
| `PORTAL_SECURE_COOKIES=true` | required |
| `EMAIL_PROVIDER` + SMTP | `dev` is rejected |
| `WEBAUTHN_RP_ID`, `WEBAUTHN_ORIGIN` | required |
| `ALLOWED_HOSTS` | required (Host validation) |
| `RATE_LIMIT_BACKEND=postgresql` | required with PostgreSQL |

Optional: `TRUSTED_PROXIES`, `LOG_LEVEL`, `LOG_FORMAT`, `REQUEST_ID_HEADER`,
`BACKUP_DIR`, `BACKUP_ENCRYPT`, `BACKUP_PASSPHRASE`, retention variables,
`DEPLOYMENT_VERSION`.

## Non-goals / out of scope

No payments, billing, marketplace, analytics, telemetry, biometrics, IMEI or
precise-location collection. GhostLink remains terminal-only.

## Operational workflow

1. Provision PostgreSQL (see `deployment/postgres/README.md`).
2. Configure the environment (see `DEPLOYMENT.md`).
3. Start the portal behind an HTTPS reverse proxy.
4. Verify with `/health/live`, `/health/ready`, `ghostlink system readiness`.
5. Take and verify encrypted backups (`ghostlink backup create|verify`).
6. Run retention cleanup periodically (`ghostlink` / portal ops).
7. Exercise the disaster-recovery procedure (`DISASTER_RECOVERY.md`).
