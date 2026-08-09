# Database: production (Phase 13)

## Status

- **IMPLEMENTED** — a clean database abstraction (`portal_server.db`) with two
  backends selected by `DATABASE_URL`:
  - **SQLite** (`db/sqlite.py`) — default for local development, tests, Termux.
  - **PostgreSQL** (`db/postgres.py`) — first-class production backend using
    `psycopg3` + `psycopg_pool`.
- **NOT VERIFIED** — PostgreSQL runtime integration was **not available in
  this build environment** (no reachable PostgreSQL server). The adapter is
  covered by static/unit tests and the CI workflow runs the full portal suite
  against a real PostgreSQL service. We did **not** fake a green run.

## Abstraction

Route handlers depend only on `DatabaseBackend`
(`query / query_one / execute / execute_many / transaction / connection /
schema_version / backend_name`). SQL is written once with `?` placeholders;
the PostgreSQL adapter translates `? → %s` outside string literals and appends
`RETURNING id` for inserts that need the new row id.

```
portal/backend/portal_server/db/
    __init__.py       # Database() factory (URL → backend)
    base.py           # DatabaseBackend ABC + placeholder translation
    sqlite.py         # SQLite backend
    postgres.py       # psycopg3/pool backend
    migrations.py     # numbered + checksummed Postgres migrations
    transactions.py   # transaction() / with_transaction helpers
    pool.py           # PoolConfig + build_database()
```

## PostgreSQL connection management (13B)

Environment-driven (`DATABASE_POOL_MIN/MAX`, `DATABASE_CONNECT_TIMEOUT`,
`DATABASE_STATEMENT_TIMEOUT`):

- connection pooling (`psycopg_pool`, min/max, idle timeout)
- per-checkout `statement_timeout`
- graceful shutdown via `pool.close()`
- broken-connection recovery via the pool
- DSN redaction: `DATABASE_URL` is never logged or printed.

Production **fails closed** if mandatory configuration is missing.

## Transaction safety (13C)

Security-sensitive operations run inside `db.transaction()`:

- credential rotation / revocation
- refresh-token rotation
- device revocation
- pairing approval
- session revocation
- MFA changes
- password changes
- migration state
- security-event creation where consistency requires it

Multi-process correctness relies on the backend (unique constraints, row
locking, PostgreSQL advisory locks for migrations), not on Python locks alone.

## Constraints & indexes (13E)

The PostgreSQL schema enforces `NOT NULL`, `UNIQUE`, foreign keys, `CHECK`
constraints (e.g. `role IN ('owner','developer')`, status/kinds), and indexes
for authorization lookups, token-hash lookups, expiration cleanup and security
events. Authorization is always derived from server-side identity — never from
client-supplied ownership fields.
