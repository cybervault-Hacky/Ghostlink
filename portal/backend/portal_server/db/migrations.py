"""Numbered, checksummed, deterministic database migrations (Phase 13D).

PostgreSQL keeps its own dialect migration set here (SQLite keeps its own in
``db/sqlite.py`` because the two dialects differ). The shared helpers provide:

* ``migration_checksum`` — a deterministic SHA-256 over the canonical SQL.
* ``verify_checksums`` / ``migration_status`` — used by ``ghostlink db
  status`` / ``ghostlink db verify`` and by the Postgres backend at startup.
* ``MIGRATION_LOCK_KEY`` — a Postgres advisory-lock key used to serialise
  concurrent migration runs across processes.

Migrations are forward-only. A database that is *ahead* of this build is
refused (fail closed); a checksum mismatch on an already-applied migration is
also refused so a tampered/corrupt migration is never silently replayed.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

# Shared advisory-lock key for Postgres migration serialisation.
MIGRATION_LOCK_KEY = 0x47684F53_74744C6E  # "GhostL n" marker

PG_SCHEMA_VERSION = 3

# --------------------------------------------------------------------------
# PostgreSQL DDL (mirrors the SQLite schema shape 1:1 so migrations, backups
# and queries stay backend-agnostic). Types differ (BIGSERIAL vs AUTOINCREMENT)
# and security invariants are expressed as CHECK/UNIQUE constraints (13E).
# --------------------------------------------------------------------------

_PG_V1 = """
CREATE TABLE IF NOT EXISTS schema_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY,
    email TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    email_verified SMALLINT NOT NULL DEFAULT 0,
    developer_id TEXT NOT NULL UNIQUE,
    role TEXT NOT NULL DEFAULT 'developer',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    failed_logins INTEGER NOT NULL DEFAULT 0,
    locked_until TEXT,
    CONSTRAINT users_role_check CHECK (role IN ('owner', 'developer')),
    CONSTRAINT users_status_check CHECK (status IN ('pending', 'active', 'disabled'))
);

CREATE TABLE IF NOT EXISTS tokens (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    kind TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used_at TEXT,
    created_at TEXT NOT NULL,
    CONSTRAINT tokens_kind_check CHECK (kind IN ('email_verify', 'password_reset'))
);

CREATE TABLE IF NOT EXISTS sessions (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    token_hash TEXT NOT NULL,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    last_active_at TEXT NOT NULL,
    revoked_at TEXT,
    device TEXT,
    browser TEXT,
    os TEXT,
    ip TEXT,
    csrf_token TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS credentials (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    key_id TEXT NOT NULL UNIQUE,
    secret_hash TEXT NOT NULL,
    secret_salt TEXT NOT NULL,
    verifier TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    rotated_at TEXT,
    revoked_at TEXT,
    CONSTRAINT credentials_status_check CHECK (status IN ('active', 'revoked'))
);

CREATE TABLE IF NOT EXISTS projects (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE (user_id, slug),
    CONSTRAINT projects_status_check CHECK (status IN ('active', 'archived'))
);

CREATE TABLE IF NOT EXISTS security_events (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    action TEXT NOT NULL,
    actor TEXT NOT NULL,
    metadata TEXT NOT NULL DEFAULT '{}',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mfa_totp (
    user_id BIGINT PRIMARY KEY REFERENCES users(id),
    secret TEXT NOT NULL,
    enabled SMALLINT NOT NULL DEFAULT 0,
    verified SMALLINT NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mfa_recovery_codes (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    code_hash TEXT NOT NULL,
    used SMALLINT NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS webauthn_credentials (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    credential_id TEXT NOT NULL,
    public_key TEXT NOT NULL,
    algorithm TEXT NOT NULL,
    sign_count BIGINT NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    UNIQUE (user_id, credential_id)
);
"""

_PG_V2 = """
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS idx_sessions_token_hash ON sessions(token_hash);
CREATE INDEX IF NOT EXISTS idx_tokens_hash ON tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_tokens_user ON tokens(user_id);
CREATE INDEX IF NOT EXISTS idx_credentials_user ON credentials(user_id);
CREATE INDEX IF NOT EXISTS idx_credentials_key_id ON credentials(key_id);
CREATE INDEX IF NOT EXISTS idx_projects_user ON projects(user_id);
CREATE INDEX IF NOT EXISTS idx_security_events_user_time ON security_events(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_recovery_user ON mfa_recovery_codes(user_id);
CREATE INDEX IF NOT EXISTS idx_webauthn_user ON webauthn_credentials(user_id);
"""

_PG_V3 = """
CREATE TABLE IF NOT EXISTS developer_devices (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    device_id TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    platform TEXT NOT NULL DEFAULT 'termux',
    client_version TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_seen_at TEXT,
    CONSTRAINT developer_devices_status_check CHECK (status IN ('active', 'revoked'))
);

CREATE TABLE IF NOT EXISTS pairing_codes (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT REFERENCES users(id),
    code_hash TEXT NOT NULL,
    nonce TEXT NOT NULL UNIQUE,
    device_id TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',
    expires_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    CONSTRAINT pairing_codes_status_check CHECK (status IN ('pending', 'used', 'expired'))
);

CREATE TABLE IF NOT EXISTS api_credentials (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    project_id BIGINT REFERENCES projects(id),
    device_id BIGINT REFERENCES developer_devices(id),
    name TEXT NOT NULL,
    credential_id TEXT NOT NULL UNIQUE,
    secret_hash TEXT NOT NULL,
    secret_salt TEXT NOT NULL,
    verifier TEXT NOT NULL,
    scopes TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'active',
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    rotated_at TEXT,
    revoked_at TEXT,
    CONSTRAINT api_credentials_status_check CHECK (status IN ('active', 'revoked'))
);

CREATE TABLE IF NOT EXISTS api_tokens (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    device_id BIGINT REFERENCES developer_devices(id),
    project_id BIGINT REFERENCES projects(id),
    credential_id BIGINT REFERENCES api_credentials(id),
    kind TEXT NOT NULL,
    token_hash TEXT NOT NULL,
    scopes TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    last_used_at TEXT,
    CONSTRAINT api_tokens_kind_check CHECK (kind IN ('access', 'refresh'))
);

CREATE TABLE IF NOT EXISTS api_activity (
    id BIGSERIAL PRIMARY KEY,
    user_id BIGINT NOT NULL REFERENCES users(id),
    device_id BIGINT REFERENCES developer_devices(id),
    project_id BIGINT REFERENCES projects(id),
    endpoint TEXT NOT NULL,
    category TEXT NOT NULL,
    result TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_devices_user ON developer_devices(user_id);
CREATE INDEX IF NOT EXISTS idx_api_tokens_hash ON api_tokens(token_hash);
CREATE INDEX IF NOT EXISTS idx_api_activity_user_time ON api_activity(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_pairing_nonce ON pairing_codes(nonce);
"""


@dataclass(frozen=True)
class Migration:
    """A numbered migration with a deterministic checksum."""

    version: int
    checksum: str
    sql: str


def migration_checksum(sql: str) -> str:
    """Deterministic SHA-256 over normalized canonical SQL."""
    normalized = " ".join(sql.split())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# PostgreSQL numbered migration set (ordered by version).
PG_MIGRATIONS: tuple[Migration, ...] = (
    Migration(1, migration_checksum(_PG_V1), _PG_V1),
    Migration(2, migration_checksum(_PG_V2), _PG_V2),
    Migration(3, migration_checksum(_PG_V3), _PG_V3),
)

PG_SCHEMA_CHECKSUM = migration_checksum("\n".join(m.sql for m in PG_MIGRATIONS))


def verify_checksums(applied: dict[int, str]) -> list[str]:
    """Compare stored per-version checksums against the canonical set.

    Returns a list of mismatch descriptions (empty == all verified). Unknown
    future versions already fail closed in the backend before this is called.
    """
    problems: list[str] = []
    by_version = {m.version: m for m in PG_MIGRATIONS}
    for version, stored in applied.items():
        canonical = by_version.get(version)
        if canonical is None:
            problems.append(f"version {version} is unknown to this build")
            continue
        if stored != canonical.checksum:
            problems.append(
                f"version {version} checksum mismatch (stored {stored[:12]}… "
                f"expected {canonical.checksum[:12]}…)"
            )
    return problems


def migration_status(applied_version: int, applied_checksums: dict[int, str]) -> dict[str, object]:
    """Summary used by ``ghostlink db status`` (server-side ops)."""
    latest = PG_MIGRATIONS[-1].version
    problems = verify_checksums(applied_checksums)
    missing = [m.version for m in PG_MIGRATIONS if m.version > applied_version]
    return {
        "schema_version": applied_version,
        "latest_version": latest,
        "migrations": [{"version": m.version, "checksum": m.checksum} for m in PG_MIGRATIONS],
        "applied_checksums": {str(k): v for k, v in applied_checksums.items()},
        "missing": missing,
        "up_to_date": not missing,
        "checksum_problems": problems,
        "healthy": (not missing) and not problems,
    }


__all__ = [
    "MIGRATION_LOCK_KEY",
    "PG_MIGRATIONS",
    "PG_SCHEMA_CHECKSUM",
    "PG_SCHEMA_VERSION",
    "Migration",
    "migration_checksum",
    "migration_status",
    "verify_checksums",
]
