# GhostLink Developer Portal — Backup & Disaster Recovery (Phase 11)

## Backupable vs sensitive

**BACKUPABLE**

* The database (users, credentials metadata, projects, sessions, security
  events, MFA/WebAuthn verifier data).
* Migration state (`schema_meta`).
* Non-secret configuration metadata.

**SENSITIVE — protect like a password**

* `SESSION_SECRET` (session signing).
* Credential verification material (salted hashes).
* WebAuthn public-key registrations.
* TOTP secrets and recovery-code hashes.

**NEVER back up / transmit**

* Plaintext developer credentials (they are never stored).
* Plaintext passwords (never stored).
* Recovery-code plaintext after initial reveal.
* Permanent browser secrets.

## Backup procedure

1. Back up the database file(s) with the portal offline or with a consistent
   snapshot (SQLite: use `sqlite3 <db> ".backup '<path>'"` or a hot-backup
   method; Postgres: `pg_dump`).
2. Store the `SESSION_SECRET` separately and securely (encrypted secret
   store) — never in the same archive as the database in plaintext.
3. Encrypt backups at rest if the backup medium is not access-controlled.

Do **not** claim backups are secure unless encryption/access controls are
actually configured.

## Restore procedure

1. Stop the portal.
2. Restore the database file / run the migration forward to the current
   schema version.
3. Restore `SESSION_SECRET` to match the pre-backup value (otherwise all
   sessions are invalidated — safe, but a full re-login).
4. Start the portal; verify `/health` and the current schema version.

## Migration / rollback

* Migrations are forward-only and transactional; a failed migration rolls
  back and leaves the previous state intact.
* To roll back a deployment, restore the pre-upgrade database backup and the
  matching application version. Do **not** silently downgrade a newer schema
  — the app fails closed on a future version.

## Compromised-session response

1. Sign out / revoke the session via the Security Center (or revoke-all).
2. Change the password (revokes all sessions automatically).
3. Review the Security Activity log.
4. If the session cookie or `SESSION_SECRET` leaked, rotate `SESSION_SECRET`.

## Compromised-account response

1. Reset the password (revokes all sessions).
2. Disable and re-enable MFA; regenerate recovery codes.
3. Revoke and recreate all developer credentials.
4. Remove any unrecognized passkeys.
5. Review Security Activity for suspicious events.

## Credential revocation procedure

A leaked developer credential is revoked immediately via
`POST /api/v1/developer-keys/{key_id}/revoke` (or the Security Center).
Revocation is persistent and irreversible; create a new credential to
restore access.

---

## Phase 13 — automated recovery procedure & tests

**IMPLEMENTED & TESTED** (SQLite runtime). The Phase 13 recovery procedure is
exercised by automated tests:

1. Create a production-like database.
2. Create users/devices/projects/credentials and security events.
3. Backup.
4. Destroy the test database.
5. Restore into an isolated database.
6. Verify integrity (checksum).
7. Verify authentication and revocation state.
8. **Recovery never reactivates revoked credentials** — revoked statuses are
   preserved byte-for-byte as backed up.

Tooling: `ghostlink backup create|verify|list|restore --dry-run`,
`ghostlink db verify`. A real restore targets a fresh, isolated database and
is never auto-destructive. See `BACKUPS.md`.

---

## Phase 14 — disaster scenarios (procedures)

See `docs/INCIDENT_RESPONSE.md` for the full incident procedures. Documented
scenarios: database corruption, accidental deletion, credential compromise,
stolen session, migration failure, application deployment failure, and backup
corruption. The common recovery path is: verify backup → restore into an
isolated DB → migrate → verify schema, security invariants, revocation state,
and the Owner invariant → only then replace production (never automatically,
never without explicit confirmation).
