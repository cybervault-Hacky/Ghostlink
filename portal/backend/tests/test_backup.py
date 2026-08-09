"""Phase 13 — backup create/verify/list/restore + integrity tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from portal_server.db import Database, DatabaseBackend
from portal_server.ops.backup import (
    BackupError,
    create_backup,
    list_backups,
    restore_backup,
    verify_backup,
)


@pytest.fixture
def seeded_db(tmp_path: Path) -> tuple[DatabaseBackend, Path]:
    db = Database(tmp_path / "src.db")
    db.execute_many(
        [
            (
                "INSERT INTO users(email, password_hash, status, developer_id, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                ("alice@example.com", "h", "active", "dev_alice", "t", "t"),
            ),
            (
                "INSERT INTO users(email, password_hash, status, developer_id, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                ("bob@example.com", "h", "revoked", "dev_bob", "t", "t"),
            ),
        ]
    )
    return db, tmp_path


def test_create_and_verify_backup(seeded_db: tuple[DatabaseBackend, Path]) -> None:
    db, tmp = seeded_db
    result = create_backup(db, backup_dir=tmp / "bk", schema_version=db.schema_version)
    assert result["manifest"]["schema_version"] == db.schema_version
    info = verify_backup(result["path"])
    assert info["valid"] is True
    assert info["schema_version"] == 3


def test_backup_tamper_detection(seeded_db: tuple[DatabaseBackend, Path]) -> None:
    db, tmp = seeded_db
    result = create_backup(db, backup_dir=tmp / "bk", schema_version=db.schema_version)
    path = Path(result["path"])
    data = path.read_bytes()
    # Flip one byte in the payload region.
    tampered = (
        data[: len(data) // 2] + bytes([data[len(data) // 2] ^ 0xFF]) + data[len(data) // 2 + 1 :]
    )
    path.write_bytes(tampered)
    with pytest.raises(BackupError):
        verify_backup(path)


def test_encrypted_backup_roundtrip(seeded_db: tuple[DatabaseBackend, Path]) -> None:
    db, tmp = seeded_db
    result = create_backup(
        db, backup_dir=tmp / "bk", schema_version=db.schema_version, encrypt=True, passphrase="pw"
    )
    with pytest.raises(BackupError):
        verify_backup(result["path"], passphrase="wrong")
    info = verify_backup(result["path"], passphrase="pw")
    assert info["valid"] is True
    assert info["encrypted"] is True


def test_encrypted_requires_passphrase(seeded_db: tuple[DatabaseBackend, Path]) -> None:
    db, tmp = seeded_db
    with pytest.raises(BackupError):
        create_backup(db, backup_dir=tmp / "bk", schema_version=1, encrypt=True, passphrase="")


def test_list_backups(seeded_db: tuple[DatabaseBackend, Path]) -> None:
    db, tmp = seeded_db
    create_backup(db, backup_dir=tmp / "bk", schema_version=db.schema_version)
    create_backup(db, backup_dir=tmp / "bk", schema_version=db.schema_version)
    entries = list_backups(tmp / "bk")
    assert len(entries) == 2
    assert all(e["schema_version"] == 3 for e in entries)


def test_restore_dry_run_plans_without_writing(seeded_db: tuple[DatabaseBackend, Path]) -> None:
    db, tmp = seeded_db
    result = create_backup(db, backup_dir=tmp / "bk", schema_version=db.schema_version)
    target = Database(tmp / "target.db")
    plan = restore_backup(target, result["path"], dry_run=True)
    assert plan["dry_run"] is True
    assert plan["restore_plan"]["users"] == 2
    # Nothing written to the target.
    assert target.query_one("SELECT COUNT(*) AS n FROM users") is None or (
        int(target.query_one("SELECT COUNT(*) AS n FROM users")["n"]) == 0
    )
    target.close()


def test_restore_never_reactivates_revoked(seeded_db: tuple[DatabaseBackend, Path]) -> None:
    db, tmp = seeded_db
    result = create_backup(db, backup_dir=tmp / "bk", schema_version=db.schema_version)
    target = Database(tmp / "target2.db")
    restored = restore_backup(target, result["path"], dry_run=False)
    assert restored["dry_run"] is False
    assert restored["restored"]["users"] == 2
    bob = target.query_one("SELECT status FROM users WHERE developer_id=?", ("dev_bob",))
    alice = target.query_one("SELECT status FROM users WHERE developer_id=?", ("dev_alice",))
    assert bob["status"] == "revoked"
    assert alice["status"] == "active"
    target.close()
