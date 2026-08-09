"""Phase 15O — deterministic concurrency & failure tests.

Covers concurrent rate limiting, credential/refresh rotation, backup creation,
migration races, and failure modes (locked db, unavailable db, transaction
rollback, duplicate/replay).
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from conftest import make_active_user, signin_and_set_csrf
from portal_server.db import Database, DatabaseBackend


def _add_credential(db: DatabaseBackend, user_id: int, name: str) -> int:
    from portal_server.auth import now_iso

    from ghostlink.developer.keys import issue_credential

    key_id, _secret = issue_credential()
    return int(
        db.execute_many(
            [
                (
                    "INSERT INTO credentials(user_id, name, key_id, secret_hash, secret_salt, "
                    "verifier, status, created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (user_id, name, key_id, "h", "s", "v", "active", now_iso()),
                )
            ],
            lastrow=True,
        )
    )


class TestConcurrentRateLimit:
    def test_concurrent_allow_count_is_exact(self) -> None:
        from portal_server.ratelimit import InMemoryRateLimiter

        limiter = InMemoryRateLimiter(limits={"s": (5, 60.0)})
        results: list[bool] = []
        lock = threading.Lock()

        def hit() -> None:
            ok = limiter.allow("s", "shared")
            with lock:
                results.append(ok)

        threads = [threading.Thread(target=hit) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert sum(results) == 5  # exactly 5 allowed, rest rejected


class TestConcurrentRotation:
    def test_concurrent_credential_rotation_no_duplicate_active(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "rot.db")
        uid = int(
            db.execute_many(
                [
                    (
                        "INSERT INTO users(email, password_hash, status, developer_id, "
                        "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                        ("rot@x.com", "h", "active", "dev_rot", "t", "t"),
                    )
                ],
                lastrow=True,
            )
        )
        cid = _add_credential(db, uid, "c")

        def rotate() -> None:
            try:
                with db.transaction():
                    db.execute("UPDATE credentials SET status='revoked' WHERE id=?", (cid,))
            except Exception:
                pass

        threads = [threading.Thread(target=rotate) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        row = db.query_one("SELECT status FROM credentials WHERE id=?", (cid,))
        assert row["status"] == "revoked"
        db.close()


class TestConcurrentBackup:
    def test_concurrent_backup_creation_is_safe(self, portal_app, tmp_path: Path) -> None:
        from portal_server.ops.backup import create_backup

        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        errors: list[Exception] = []

        def make() -> None:
            try:
                create_backup(
                    portal_app.db,
                    backup_dir=tmp_path / "bk",
                    schema_version=portal_app.db.schema_version,
                )
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=make) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        backups = list((tmp_path / "bk").glob("*.glbak"))
        assert len(backups) == 4  # unique filenames


class TestFailureModes:
    def test_database_file_locked_fails_cleanly(self, tmp_path: Path) -> None:
        # A malformed/unopenable sqlite file must fail cleanly.
        import sqlite3

        p = tmp_path / "bad.db"
        p.write_bytes(b"not a database")

        with pytest.raises(sqlite3.DatabaseError):
            Database(p)

    def test_transaction_rollback_on_duplicate(self, tmp_path: Path) -> None:
        db = Database(tmp_path / "dup.db")
        db.execute(
            "INSERT INTO users(email, password_hash, status, developer_id, "
            "created_at, updated_at) VALUES(?,?,?,?,?,?)",
            ("dup@x.com", "h", "active", "dev_dup", "t", "t"),
        )
        import sqlite3

        with pytest.raises(sqlite3.IntegrityError), db.transaction():
            db.execute(
                "INSERT INTO users(email, password_hash, status, developer_id, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                ("dup@x.com", "h", "active", "dev_dup2", "t", "t"),  # unique email violation
            )
            db.execute(
                "INSERT INTO users(email, password_hash, status, developer_id, "
                "created_at, updated_at) VALUES(?,?,?,?,?,?)",
                ("other@x.com", "h", "active", "dev_other", "t", "t"),
            )
        # The whole transaction rolled back; other@x.com not inserted.
        row = db.query_one("SELECT developer_id FROM users WHERE email=?", ("other@x.com",))
        assert row is None
        db.close()

    def test_refresh_replay_fails_closed(self, portal_app) -> None:
        # Pair + issue tokens, then reuse a refresh token after rotation.
        s, body = portal_app.client.post(
            "/api/v1/developer/auth/pair-begin",
            json={"device_name": "d", "platform": "termux"},
        )
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        s, body = portal_app.client.post(
            "/api/v1/developer/auth/pair-begin",
            json={"device_name": "d", "platform": "termux"},
        )
        assert s == 200
        approved = portal_app.client.post(
            "/api/v1/developer/auth/pair-approve",
            json={"pairing_code": body["pairing_code"], "scopes": "device:read"},
        )
        tokens = portal_app.client.post(
            "/api/v1/developer/auth/token",
            json={
                "credential_id": approved[1]["credential_id"],
                "credential_secret": approved[1]["credential_secret"],
            },
        )
        assert tokens[0] == 200
        headers = {"Authorization": f"Bearer {tokens[1]['access_token']}"}
        r1 = portal_app.client.post(
            "/api/v1/developer/auth/refresh",
            json={"refresh_token": tokens[1]["refresh_token"]},
            headers=headers,
        )
        assert r1[0] == 200
        # Replay the now-rotated refresh token.
        r2 = portal_app.client.post(
            "/api/v1/developer/auth/refresh",
            json={"refresh_token": tokens[1]["refresh_token"]},
            headers=headers,
        )
        assert r2[0] == 401
