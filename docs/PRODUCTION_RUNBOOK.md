# Production Runbook (Phase 14)

## Status

**DOCUMENTED ONLY.** No public deployment has been performed. This runbook
describes the operational procedures for a GhostLink portal deployment; each
step is verified by tests where possible (SQLite runtime) and marked
**ENVIRONMENT-GATED** where it needs a live PostgreSQL server or Docker.

## Deployment sequence (safe, low-downtime)

1. **Backup** — `ghostlink backup create` (encrypted).
2. **Verify backup** — `ghostlink backup verify <file> --passphrase …`.
3. **Verify migrations** — `ghostlink db verify` (checksums + schema).
4. **Apply migration** — `ghostlink db migrate` (advisory-lock serialised).
5. **Verify schema** — `ghostlink db status` shows the expected version.
6. **Health** — `/health/live`.
7. **Readiness** — `/health/ready` must return 200 before routing traffic.
8. **Graceful shutdown** — send `SIGTERM`; gunicorn drains in-flight requests
   (systemd `TimeoutStopSec=30`).
9. **Rollback** — retain the previous immutable version; recovery restores a
   backup into an isolated DB and only replaces production after verification.

Traffic is never routed to an unhealthy application: the load balancer /
reverse proxy checks `/health/ready` before sending requests.

## Readiness semantics

`/health/ready` fails (503) when:

- the database is unreachable
- migrations are not applied / schema version is unexpected
- required production configuration is missing or insecure
- a critical dependency is unavailable

`/health/live` is database-independent (process liveness only).

## Environment model

- `development` — safe defaults (SQLite, memory limiter, dev email).
- `staging` — PostgreSQL, real SMTP to a staging sink, secure cookies.
- `production` — fails closed on any missing/insecure value; PostgreSQL only.

Templates: `deployment/env/.env{,development,staging,production}.example`.

## Operational commands

```
ghostlink db status | migrate | verify
ghostlink backup create | verify | list | restore --dry-run
ghostlink system health | readiness
ghostlink security-audit
```

## Alerting (see docs/OPERATIONS.md)

Alerts for application unavailability, readiness failure, DB/migration/backup
failure, high auth-failure rate, rate-limit-backend failure, excessive 5xx,
high latency, disk exhaustion, certificate expiry, connection-pool exhaustion.

## Disaster recovery

See `docs/DISASTER_RECOVERY.md` and `docs/INCIDENT_RESPONSE.md`.
