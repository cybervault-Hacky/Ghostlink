# GhostLink Developer Portal — Database (Phase 11)

## Abstraction

The portal uses a small, dependency-light database layer
(`portal_server/db.py`) built on the standard-library `sqlite3` for
development, with a schema that is **PostgreSQL-ready** (explicit primary
keys, foreign keys, ISO-8601 timestamps, uniqueness constraints).

* Development: SQLite file (`portal.db`).
* Production: a PostgreSQL connection via `DATABASE_URL` (the same schema;
  a Postgres driver/backend is wired at deployment — documented, not
  shipped here).

## Migrations

Migrations are **versioned, ordered, and transactional**:

* `MIGRATIONS` is an ordered list of `(version, [sql statements])`.
* On startup the runner creates `schema_meta`, reads the stored version,
  applies each pending migration inside a single transaction, and records
  the new version.
* **Fail closed:** a database whose stored version is *newer* than this
  build (a downgrade) raises `DatabaseMigrationError` and is never modified.
* A failed migration rolls back and raises rather than leaving partial
  state.

Schema version is currently `2`:

* v1 — the full Phase 10B schema (users, tokens, sessions, credentials,
  projects, security_events, mfa_totp, mfa_recovery_codes,
  webauthn_credentials).
* v2 — production indexes on the hot query paths.

## Constraints & integrity

* Foreign keys are enforced (`PRAGMA foreign_keys = ON`).
* `email`, `developer_id`, `key_id`, and `(user_id, credential_id)` are
  unique.
* Writes are transactional (single-statement or `execute_many` in one
  transaction).

See `tests/test_migrations.py` for coverage (fresh schema, index presence,
future-schema fail-closed, FK enforcement, uniqueness).
