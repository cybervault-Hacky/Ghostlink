"""Encrypted, checksummed, verifiable backups (Phase 13M).

A backup is a single ``.glbak`` file with two newline-delimited sections:

1. a JSON **manifest**: format version, backend, schema version, timestamp,
   checksum algorithm/hash, encryption flags, retention, and the table list;
2. a base64 **payload** (raw JSON rows, or the ChaCha20-Poly1305 ciphertext of
   that JSON when encryption is enabled).

Verification recomputes the checksum over the raw payload. Restore is
explicitly dry-run by default and never performs a destructive automatic
restore: recovery writes into a fresh, isolated database (see docs/DR.md).

Never log database credentials; the backup directory path is safe to print.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import secrets
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from portal_server.db.base import DatabaseBackend

FORMAT_VERSION = 1
BACKUP_EXT = ".glbak"

# Logical table set dumped for a backup (schema_meta carries only the version,
# tracked in the manifest instead).
_TABLES = (
    "users",
    "tokens",
    "sessions",
    "credentials",
    "projects",
    "security_events",
    "mfa_totp",
    "mfa_recovery_codes",
    "webauthn_credentials",
    "developer_devices",
    "pairing_codes",
    "api_credentials",
    "api_tokens",
    "api_activity",
)


class BackupError(Exception):
    """Raised when a backup cannot be created, verified, or restored."""


@dataclass
class BackupManifest:
    format_version: int
    backend: str
    schema_version: int
    created_at: str
    checksum_algorithm: str
    checksum: str
    encrypted: bool
    tables: list[str] = field(default_factory=list)
    retention_count: int = 30

    def to_json(self) -> dict[str, Any]:
        return {
            "format": "ghostlink-backup",
            "format_version": self.format_version,
            "backend": self.backend,
            "schema_version": self.schema_version,
            "created_at": self.created_at,
            "checksum_algorithm": self.checksum_algorithm,
            "checksum": self.checksum,
            "encrypted": self.encrypted,
            "tables": self.tables,
            "retention_count": self.retention_count,
        }


def _dump_rows(db: DatabaseBackend) -> list[dict[str, Any]]:
    data: list[dict[str, Any]] = []
    for table in _TABLES:
        try:
            rows = db.query(f"SELECT * FROM {table}")
        except Exception as exc:  # pragma: no cover - defensive
            raise BackupError(f"Cannot read table {table!r}: {exc}") from exc
        data.append({"table": table, "rows": rows})
    return data


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    hkdf = HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=salt,
        info=b"ghostlink-backup-v1",
    )
    return hkdf.derive(passphrase.encode("utf-8"))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def create_backup(
    db: DatabaseBackend,
    *,
    backup_dir: str | Path,
    schema_version: int,
    encrypt: bool = False,
    passphrase: str = "",
    retention_count: int = 30,
    created_at: str | None = None,
) -> dict[str, Any]:
    """Create a backup file and return its manifest + path."""
    if encrypt and not passphrase:
        raise BackupError("A passphrase is required for encrypted backups.")
    directory = Path(backup_dir)
    directory.mkdir(parents=True, exist_ok=True)
    timestamp = created_at or _now_iso()
    safe_name = timestamp.replace(":", "-").replace("+00:00", "Z").replace(".", "-")
    unique = secrets.token_hex(3)
    path = directory / f"backup-{safe_name}-{unique}{BACKUP_EXT}"

    payload_raw = json.dumps(_dump_rows(db), separators=(",", ":")).encode("utf-8")
    checksum = _sha256(payload_raw)
    manifest = BackupManifest(
        format_version=FORMAT_VERSION,
        backend=db.backend_name,
        schema_version=schema_version,
        created_at=timestamp,
        checksum_algorithm="sha256",
        checksum=checksum,
        encrypted=encrypt,
        tables=list(_TABLES),
        retention_count=retention_count,
    )
    manifest_line = json.dumps(manifest.to_json(), separators=(",", ":")).encode("utf-8")

    if encrypt:
        salt = secrets.token_bytes(16)
        nonce = secrets.token_bytes(12)
        key = _derive_key(passphrase, salt)
        ciphertext = ChaCha20Poly1305(key).encrypt(nonce, payload_raw, manifest_line)
        payload_line = json.dumps(
            {
                "enc": "chacha20poly1305",
                "salt": base64.b64encode(salt).decode("ascii"),
                "nonce": base64.b64encode(nonce).decode("ascii"),
                "ciphertext": base64.b64encode(ciphertext).decode("ascii"),
            },
            separators=(",", ":"),
        ).encode("utf-8")
    else:
        payload_line = base64.b64encode(payload_raw)

    path.write_bytes(manifest_line + b"\n" + payload_line + b"\n")
    return {"path": str(path), "manifest": manifest.to_json()}


def _read_backup(
    path: str | Path,
) -> tuple[dict[str, Any], str, str, tuple[bytes, bytes, bytes] | None]:
    """Read a backup file: (manifest, enc_scheme, payload_b64, secret_material).

    ``enc_scheme`` is ``"plain"`` or ``"chacha20poly1305"``. For encrypted
    backups ``secret_material`` is (salt, nonce, ciphertext) bytes.
    """
    raw = Path(path).read_bytes()
    try:
        manifest_line, _, payload_line = raw.decode("utf-8").partition("\n")
    except UnicodeDecodeError as exc:
        raise BackupError("Backup file is not valid UTF-8 (corrupt).") from exc
    try:
        manifest = json.loads(manifest_line)
    except json.JSONDecodeError as exc:
        raise BackupError("Backup manifest is corrupt.") from exc
    if manifest.get("format") != "ghostlink-backup":
        raise BackupError("Not a GhostLink backup file.")
    if manifest.get("format_version") != FORMAT_VERSION:
        raise BackupError(f"Unsupported backup format version {manifest.get('format_version')}.")
    payload_b64 = payload_line.strip()
    if not manifest.get("encrypted"):
        # Validate the plain payload base64 early (incomplete/corrupt file).
        try:
            base64.b64decode(payload_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise BackupError("Backup payload is not valid base64 (corrupt).") from exc
        return manifest, "plain", payload_b64, None
    try:
        enc = json.loads(payload_b64)
    except json.JSONDecodeError as exc:
        raise BackupError("Encrypted backup payload is corrupt.") from exc
    if not isinstance(enc, dict) or enc.get("enc") != "chacha20poly1305":
        raise BackupError(f"Unsupported encryption scheme: {enc.get('enc')!r}")
    try:
        secret = (
            base64.b64decode(enc["salt"], validate=True),
            base64.b64decode(enc["nonce"], validate=True),
            base64.b64decode(enc["ciphertext"], validate=True),
        )
    except (binascii.Error, ValueError, KeyError) as exc:
        raise BackupError("Encrypted backup payload is corrupt.") from exc
    return manifest, "chacha20poly1305", payload_b64, secret


def _decrypt_payload(
    manifest: dict[str, Any],
    scheme: str,
    payload_b64: str,
    secret: tuple[bytes, bytes, bytes] | None,
    passphrase: str,
) -> bytes:
    if scheme == "plain":
        return base64.b64decode(payload_b64)
    if not passphrase:
        raise BackupError("Passphrase required to decrypt this backup.")
    if secret is None:
        raise BackupError("Encrypted backup is missing key material.")
    salt, nonce, ciphertext = secret
    manifest_bytes = json.dumps(manifest, separators=(",", ":")).encode("utf-8")
    key = _derive_key(passphrase, salt)
    try:
        return ChaCha20Poly1305(key).decrypt(nonce, ciphertext, manifest_bytes)
    except Exception as exc:
        raise BackupError(
            "Encrypted backup failed authentication (bad passphrase or corrupt)."
        ) from exc


def verify_backup(path: str | Path, *, passphrase: str = "") -> dict[str, Any]:
    """Verify a backup's integrity and report a safe summary.

    Raises BackupError on any integrity failure. An encrypted backup requires
    the passphrase to decrypt and verify its checksum.
    """
    manifest, scheme, payload_b64, secret = _read_backup(path)
    payload_raw = _decrypt_payload(manifest, scheme, payload_b64, secret, passphrase)
    actual = _sha256(payload_raw)
    expected = manifest.get("checksum")
    if actual != expected:
        raise BackupError(
            f"Checksum mismatch (expected {expected}, got {actual}). Backup is corrupt."
        )
    try:
        data = json.loads(payload_raw.decode("utf-8"))
    except Exception as exc:
        raise BackupError("Payload is not valid JSON.") from exc
    if not isinstance(data, list):
        raise BackupError("Payload is malformed.")
    return {
        "path": str(path),
        "valid": True,
        "backend": manifest.get("backend"),
        "schema_version": manifest.get("schema_version"),
        "created_at": manifest.get("created_at"),
        "encrypted": bool(manifest.get("encrypted")),
        "checksum": expected,
        "tables": manifest.get("tables", []),
    }


def _load_rows(path: str | Path, passphrase: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest, scheme, payload_b64, secret = _read_backup(path)
    payload_raw = _decrypt_payload(manifest, scheme, payload_b64, secret, passphrase)
    data = json.loads(payload_raw.decode("utf-8"))
    return manifest, data


def list_backups(backup_dir: str | Path) -> list[dict[str, Any]]:
    """List backups in a directory (newest first); best-effort manifest read."""
    directory = Path(backup_dir)
    items: list[dict[str, Any]] = []
    if not directory.exists():
        return items
    for path in sorted(
        directory.glob(f"*{BACKUP_EXT}"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    ):
        entry: dict[str, Any] = {"path": str(path), "size": path.stat().st_size}
        try:
            manifest, _scheme, _payload_b64, _secret = _read_backup(path)
            entry.update(
                {
                    "created_at": manifest.get("created_at"),
                    "schema_version": manifest.get("schema_version"),
                    "backend": manifest.get("backend"),
                    "encrypted": bool(manifest.get("encrypted")),
                    "checksum": manifest.get("checksum"),
                }
            )
        except Exception:
            entry["valid"] = False
        items.append(entry)
    return items


def _insert_rows(db: DatabaseBackend, table: str, rows: list[dict[str, Any]]) -> int:
    if not rows:
        return 0
    columns = list(rows[0].keys())
    cols_sql = ", ".join(columns)
    placeholders = ", ".join("?" for _ in columns)
    sql = f"INSERT INTO {table} ({cols_sql}) VALUES ({placeholders})"
    for row in rows:
        db.execute(sql, tuple(row.get(c) for c in columns))
    return len(rows)


def restore_backup(
    db: DatabaseBackend,
    path: str | Path,
    *,
    passphrase: str = "",
    dry_run: bool = True,
) -> dict[str, Any]:
    """Plan (dry-run) or perform a restore into an isolated/fresh database.

    Dry-run (default) verifies and reports what would be restored without
    writing. A real restore only runs with ``dry_run=False`` and is intended to
    target a fresh, isolated database; it loads rows table-by-table in a single
    transaction and never clears active state elsewhere. Revoked statuses are
    preserved exactly as backed up — recovery never reactivates them.
    """
    result = verify_backup(path, passphrase=passphrase)
    manifest, data = _load_rows(path, passphrase)
    table_plans = {entry["table"]: len(entry["rows"]) for entry in data}
    if dry_run:
        return {**result, "dry_run": True, "restore_plan": table_plans}
    if db.schema_version != manifest.get("schema_version"):
        raise BackupError(
            f"Schema version mismatch: backup is v{manifest.get('schema_version')}, "
            f"target is v{db.schema_version}. Apply migrations first."
        )
    loaded: dict[str, int] = {}
    with db.transaction():
        for entry in data:
            loaded[entry["table"]] = _insert_rows(db, entry["table"], entry["rows"])
    return {**result, "dry_run": False, "restored": loaded}


__all__ = [
    "BACKUP_EXT",
    "BackupError",
    "BackupManifest",
    "create_backup",
    "list_backups",
    "restore_backup",
    "verify_backup",
]
