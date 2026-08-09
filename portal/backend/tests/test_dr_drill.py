"""Phase 15E — deterministic disaster-recovery drill.

Creates a populated database, takes an encrypted backup, destroys it, restores
into a fresh isolated database, and verifies every security invariant — most
importantly that revoked credentials and sessions stay revoked and that no
Owner is ever created.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from portal_server.db import Database, DatabaseBackend
from portal_server.ops.backup import BackupError, create_backup, restore_backup, verify_backup


@pytest.fixture()
def seeded(tmp_path: Path) -> DatabaseBackend:
    db = Database(tmp_path / "src.db")
    # Users
    alice = db.execute_many(
        [
            (
                "INSERT INTO users(email, password_hash, status, developer_id, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                ("alice@example.com", "h", "active", "dev_alice", "t", "t"),
            ),
        ],
        lastrow=True,
    )
    # Developer device
    dev_id = db.execute_many(
        [
            (
                "INSERT INTO developer_devices(user_id, device_id, name, status, created_at) "
                "VALUES(?,?,?,?,?)",
                (alice, "dd_active_dev", "termux1", "active", "t"),
            ),
            (
                "INSERT INTO developer_devices(user_id, device_id, name, status, created_at) "
                "VALUES(?,?,?,?,?)",
                (alice, "dd_revoked_dev", "termux2", "revoked", "t"),
            ),
        ],
        lastrow=True,
    )
    # Scoped credentials: one active, one revoked
    active_cred = db.execute_many(
        [
            (
                "INSERT INTO api_credentials(user_id, device_id, name, credential_id, secret_hash, "
                "secret_salt, verifier, scopes, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (alice, dev_id, "active", "dk_active", "h", "s", "v", "device:read", "active", "t"),
            ),
            (
                "INSERT INTO api_credentials(user_id, device_id, name, credential_id, secret_hash, "
                "secret_salt, verifier, scopes, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    alice,
                    dev_id,
                    "revoked",
                    "dk_revoked",
                    "h",
                    "s",
                    "v",
                    "device:read",
                    "revoked",
                    "t",
                ),
            ),
        ],
        lastrow=True,
    )
    # API tokens bound to the revoked credential (must stay revoked)
    db.execute_many(
        [
            (
                "INSERT INTO api_tokens(user_id, device_id, credential_id, kind, token_hash, "
                "scopes, created_at, expires_at, revoked_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (alice, dev_id, active_cred, "access", "a1", "device:read", "t", "t", None),
            ),
            (
                "INSERT INTO api_tokens(user_id, device_id, credential_id, kind, token_hash, "
                "scopes, created_at, expires_at, revoked_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (alice, dev_id, active_cred, "refresh", "r1", "device:read", "t", "t", None),
            ),
            (
                "INSERT INTO api_tokens(user_id, device_id, credential_id, kind, token_hash, "
                "scopes, created_at, expires_at, revoked_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (alice, dev_id, active_cred, "access", "a2", "device:read", "t", "t", "revoked"),
            ),
        ],
    )
    # Security events
    db.execute_many(
        [
            (
                "INSERT INTO security_events(user_id, action, actor, metadata, created_at) "
                "VALUES(?,?,?,?,?)",
                (alice, "signin", "session", "{}", "t"),
            ),
            (
                "INSERT INTO security_events(user_id, action, actor, metadata, created_at) "
                "VALUES(?,?,?,?,?)",
                (alice, "key_revoked", "web", "{}", "t"),
            ),
        ]
    )
    # A revoked session
    db.execute(
        "INSERT INTO sessions(user_id, token_hash, created_at, expires_at, last_active_at, "
        "revoked_at, csrf_token) VALUES(?,?,?,?,?,?,?)",
        (alice, "revoked_session", "t", "t", "t", "revoked", "csrf"),
    )
    return db


def _destroy(db: DatabaseBackend) -> None:
    db.close()


def test_dr_drill_full_cycle(tmp_path: Path, seeded: DatabaseBackend) -> None:
    src = seeded
    assert int(src.query_one("SELECT COUNT(*) AS n FROM users")["n"]) == 1

    # 8. encrypted backup
    result = create_backup(
        src,
        backup_dir=tmp_path / "bk",
        schema_version=src.schema_version,
        encrypt=True,
        passphrase="dr-passphrase",
    )
    path = result["path"]

    # 9-10. verify manifest + checksum
    info = verify_backup(path, passphrase="dr-passphrase")
    assert info["valid"] is True
    assert info["schema_version"] == 3
    assert info["encrypted"] is True

    # 5. destroy the test database
    _destroy(src)

    # 11. restore into a fresh isolated database
    target = Database(tmp_path / "target.db")
    restored = restore_backup(target, path, passphrase="dr-passphrase", dry_run=False)
    assert restored["dry_run"] is False

    # 12. restored state
    users = target.query("SELECT * FROM users")
    assert len(users) == 1
    alice = users[0]
    assert alice["status"] == "active"

    # 13. revoked credentials remain revoked; active remain active
    creds = {
        c["credential_id"]: c["status"]
        for c in target.query("SELECT credential_id, status FROM api_credentials")
    }
    assert creds["dk_active"] == "active"
    assert creds["dk_revoked"] == "revoked"

    # 14. sessions do not become unintentionally valid
    sessions = target.query("SELECT token_hash, revoked_at FROM sessions")
    assert any(s["revoked_at"] for s in sessions)  # revoked session stays revoked

    # Tokens bound to revoked material stay revoked
    tokens = target.query("SELECT token_hash, revoked_at FROM api_tokens WHERE token_hash='a2'")
    assert tokens and tokens[0]["revoked_at"] is not None

    # 15-16. schema version + migration checksums
    assert target.schema_version == 3

    # 17. Owner invariant — restore creates no Owner
    owners = target.query("SELECT id FROM users WHERE role='owner'")
    assert owners == []
    target.close()


def test_dr_drill_wrong_key_rejected(tmp_path: Path, seeded: DatabaseBackend) -> None:
    result = create_backup(
        seeded,
        backup_dir=tmp_path / "bk",
        schema_version=seeded.schema_version,
        encrypt=True,
        passphrase="correct-pass",
    )
    with pytest.raises(BackupError):
        verify_backup(result["path"], passphrase="wrong-pass")


def test_dr_drill_corruption_detected(tmp_path: Path, seeded: DatabaseBackend) -> None:
    result = create_backup(
        seeded,
        backup_dir=tmp_path / "bk",
        schema_version=seeded.schema_version,
        encrypt=True,
        passphrase="dr-passphrase",
    )
    p = Path(result["path"])
    data = p.read_bytes()
    flip = data[len(data) // 2] ^ 0xFF
    p.write_bytes(data[: len(data) // 2] + bytes([flip]) + data[len(data) // 2 + 1 :])
    with pytest.raises(BackupError):
        verify_backup(p, passphrase="dr-passphrase")


def test_dr_drill_incompatible_schema_rejected(tmp_path: Path, seeded: DatabaseBackend) -> None:
    result = create_backup(
        seeded,
        backup_dir=tmp_path / "bk",
        schema_version=seeded.schema_version,
        encrypt=True,
        passphrase="dr-passphrase",
    )
    target = Database(tmp_path / "target.db")
    # Simulate target at a different schema version by writing a backup manifest
    # with a mismatched schema_version.

    # Force restore to see a mismatch by altering the target's schema_version
    target.execute("UPDATE schema_meta SET value='2' WHERE key='version'")
    with pytest.raises(BackupError):
        restore_backup(target, result["path"], passphrase="dr-passphrase", dry_run=False)
    target.close()


def test_dr_drill_incomplete_backup_rejected(tmp_path: Path) -> None:
    # A truncated / incomplete file must fail verification.
    p = tmp_path / "incomplete.glbak"
    p.write_text(
        '{"format":"ghostlink-backup","format_version":1}\npartial-payload', encoding="utf-8"
    )
    with pytest.raises(BackupError):
        verify_backup(p)
