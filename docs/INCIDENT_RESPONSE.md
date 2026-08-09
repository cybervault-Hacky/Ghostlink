# Security Incident Response (Phase 15)

## Status

**DOCUMENTED ONLY.** These are the procedures to follow for a suspected
compromise. They are written to **preserve evidence and safety** — never to
hide or downplay an incident. Every procedure ends with verification before
service is restored.

## Shared principles

- Do **not** weaken authentication or hide the incident.
- Preserve safe audit metadata (the `security_events` table is append-only and
  metadata-only; do not delete it).
- Revoke affected credentials/sessions/tokens before rotating secrets.
- Restore service only after verification.

## 1. Leaked developer credential

1. Revoke the credential (`ghostlink developer credential revoke <id>`).
2. Revoke related sessions and tokens.
3. Rotate secrets if the same material is reused.
4. Inspect security/API activity for misuse.
5. Issue a new credential only after verification.

## 2. Stolen access token

Access tokens are short-lived (15 min). Revoke the owning credential and all
bound tokens; block the device if identified. Refresh tokens are single-use —
a replayed refresh token fails closed.

## 3. Stolen refresh token

1. Revoke the owning credential (`api_credentials.status='revoked'`).
2. Revoke the bound device — this revokes all its tokens.
3. Rotate the credential secret if the pairing material is exposed.
4. Inspect activity; verify the refresh token can no longer be redeemed.

## 4. Compromised account

1. Revoke all sessions (`ghostlink` / portal "revoke all").
2. Force a password change (invalidates all sessions).
3. If MFA is enabled, require re-enrollment after reset.
4. Revoke developer credentials/devices.
5. Inspect security events.

## 5. Compromised server

1. Isolate the host (stop public traffic).
2. Rotate the session secret, SMTP credentials, backup key.
3. Revoke all sessions and developer credentials.
4. Audit logs and security events.
5. Restore from a verified, clean backup into a fresh host **before**
   replacing production.

## 6. Database leak

1. Rotate the database credentials.
2. Revoke all sessions and developer credentials (leaked verifiers/hashes
   enable offline brute-force).
3. Rotate the backup encryption key.
4. Notify affected users (password/session reset).

## 7. SMTP credential leak

1. Rotate SMTP credentials.
2. Inspect outbound mail for abuse.
3. If the SMTP sink allowed phishing, notify users.

## 8. Backup leak

Encrypted backups require the `BACKUP_ENCRYPTION_KEY`; a leak of only the
backup file is low-risk. If the key is also leaked, rotate the key and re-encrypt
existing backups. Verify integrity (`ghostlink backup verify`).

## 9. Suspected Owner compromise

The Owner is a permanent, single account that cannot be created, transferred,
or escalated through any API. If the Owner account is suspected compromised:

1. Revoke the Owner's sessions and developer credentials immediately.
2. Rotate secrets (session secret, DB credentials, SMTP, backup key).
3. Audit all security events for anomalous activity.
4. There is **no** ownership-transfer or co-owner mechanism by design — do not
   add one during an incident.
5. If the Owner account cannot be recovered, this is an out-of-band operational
   decision documented in `docs/SECURITY_MODEL.md`; it must not create a second
   Owner or weaken the invariant.
