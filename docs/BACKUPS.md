# Backups (Phase 13M)

## Status

**IMPLEMENTED & TESTED** (SQLite runtime; PostgreSQL adapter covered by static
tests). Backups are created through the `DatabaseBackend` abstraction, so the
same logical format works for both backends.

## Format

A `.glbak` file has two newline-delimited sections:

1. a JSON **manifest** — format version, backend, schema version, timestamp,
   checksum algorithm + hash, encryption flag, retention, table list;
2. a base64 **payload** — the JSON rows, or (when encrypted) the
   ChaCha20-Poly1305 ciphertext of those rows (key derived from the passphrase
   via HKDF-SHA256 with a random salt; the manifest is authenticated as AAD).

Sensitive columns hold only verifiers/hashes, so a backup contains no
plaintext secrets. Revoked statuses are preserved exactly as backed up.

## Commands

```
ghostlink backup create [--dir DIR] [--encrypt] [--passphrase ...]
ghostlink backup verify <file> [--passphrase ...]
ghostlink backup list [--dir DIR]
ghostlink backup restore <file> [--to DB] [--passphrase ...] [--apply]
```

- `verify` recomputes the checksum and rejects tampering; an encrypted backup
  requires the passphrase.
- `restore` is **dry-run by default** and never performs a destructive
  automatic restore. With `--apply` it writes into an **isolated/fresh**
  database in a single transaction; it never clears or overwrites active state
  elsewhere. Recovery never reactivates revoked credentials.
- Retention: `BACKUP_RETENTION_COUNT` keeps the newest N backups. Backups are
  **not** auto-deleted without an explicit retention configuration.

## Safety

- No database credentials are ever printed by the CLI.
- The backup directory path is safe to print; secrets are never.
- Encrypted backups must be protected with a strong passphrase.
