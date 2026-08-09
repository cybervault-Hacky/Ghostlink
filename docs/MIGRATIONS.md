# Migrations (Phase 13D)

## Status

**IMPLEMENTED & TESTED** for SQLite and as static logic for PostgreSQL. The
PostgreSQL migration runner is exercised by unit tests; the CI
`postgres-integration` job runs it against a real server (not available in the
build environment).

## Properties

- **Numbered, deterministic ordering** — migrations are an ordered list by
  version (SQLite in `db/sqlite.py`, PostgreSQL in `db/migrations.py`).
- **Transactional** — each migration runs in one transaction (SQLite `BEGIN`;
  PostgreSQL `BEGIN … COMMIT`).
- **Migration locking** — PostgreSQL migrations are serialised across
  processes with `pg_advisory_lock(MIGRATION_LOCK_KEY)`; SQLite uses its file
  lock.
- **Schema-version tracking** — `schema_meta.version`.
- **Checksum validation** — PostgreSQL stores a SHA-256 per migration
  (`migration_<v>_checksum`); on startup and `db verify`, already-applied
  checksums must match or the portal refuses to continue.
- **Future-version rejection** — a schema newer than this build is refused
  (fail closed); a downgrade is never silently attempted.
- **Duplicate / interrupted migration detection** — version bookkeeping plus
  checksums detect duplicates and interrupted applies; the transaction rolls
  back a failed migration.

## Commands

```
ghostlink db status        # schema version, migration list, checksum health
ghostlink db migrate       # apply pending migrations (also runs on start)
ghostlink db verify        # verify checksums + schema (fail-closed exit code)
ghostlink db rollback --dry-run   # (not implemented) — see below
```

**No unsafe automatic destructive rollback.** A destructive migration requires
explicit, human confirmation; the tooling does not silently drop data.

## `db rollback --dry-run`

**NOT IMPLEMENTED.** Rollback is intentionally not provided as an automatic
destructive operation. Forward-only migrations plus backups/restore
(`BACKUPS.md`, `DISASTER_RECOVERY.md`) are the supported recovery path. We
document this rather than shipping an unsafe auto-rollback.
