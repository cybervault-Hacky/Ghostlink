# PostgreSQL provisioning (Phase 13)

This directory documents the PostgreSQL assumptions for a GhostLink portal
deployment. No database server is bundled with the repository.

## Requirements

- PostgreSQL 14+ (16 is the tested recommendation).
- A dedicated database and a dedicated role (never reuse `postgres` superuser
  for the application).
- Backups performed with `pg_dump` and verified with `pg_restore --list` (see
  `docs/BACKUPS.md` for the portal's own encrypted logical backups).

## Suggested provisioning

```sql
-- As the postgres superuser:
CREATE ROLE ghostlink_app LOGIN PASSWORD 'CHANGE_ME' NOSUPERUSER NOCREATEDB NOCREATEROLE;
CREATE DATABASE ghostlink OWNER ghostlink_app;
REVOKE CONNECT ON DATABASE ghostlink FROM PUBLIC;
GRANT CONNECT ON DATABASE ghostlink TO ghostlink_app;
```

The application connects with the `DATABASE_URL`
`postgresql://ghostlink_app:CHANGE_ME@127.0.0.1:5432/ghostlink`. The migration
step creates the schema (tables, indexes, constraints) automatically on first
start, guarded by an advisory lock.

## Operational notes

- `statement_timeout` is enforced per-connection by the portal backend.
- Connection pooling is handled by the portal's `psycopg_pool` layer
  (`DATABASE_POOL_MIN` / `DATABASE_POOL_MAX`); do not run an external pgbouncer
  unless you understand its transaction-pooling semantics.
- Retention cleanup is bounded and idempotent (`ghostlink` / portal ops).
- Backup/restore must never run while `ghostlink backup restore --apply` is
  executing against the live database.

## SECURITY ASSUMPTIONS

- Credentials are provided via environment/secret manager only — never stored
  in the repository, compose files, or the image.
- The database port should not be exposed to the public internet; only the
  portal host (and operators) should reach it.
- `pg_hba.conf` should use `scram-sha-256` for password authentication.
