"""Phase 13 — ops/administration CLI (db status/verify, backup, system)."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.fixture
def env(tmp_path: Path):
    old = dict(os.environ)
    os.environ["DATABASE_URL"] = str(tmp_path / "portal.db")
    os.environ["BACKUP_DIR"] = str(tmp_path / "backups")
    yield tmp_path
    os.environ.clear()
    os.environ.update(old)


_BACKEND = Path(__file__).resolve().parent.parent  # portal/backend


def _manage(args: list[str], _cwd: Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "-m", "portal_server.ops.manage", *args],
        cwd=str(_BACKEND),
        env=os.environ.copy(),
        capture_output=True,
        text=True,
    )
    return proc.returncode, proc.stdout + proc.stderr


def test_db_status_and_verify(env: Path) -> None:
    rc, out = _manage(["db", "status"], env)
    assert rc == 0
    assert '"schema_version": 3' in out
    rc, out = _manage(["db", "verify"], env)
    assert rc == 0
    assert '"healthy": true' in out


def test_db_migrate(env: Path) -> None:
    rc, out = _manage(["db", "migrate"], env)
    assert rc == 0
    assert "migrated" in out


def test_backup_create_verify_list(env: Path) -> None:
    rc, out = _manage(["backup", "create"], env)
    assert rc == 0
    rc, out = _manage(["backup", "list"], env)
    assert rc == 0
    assert "glbak" in out


def test_system_health_and_readiness(env: Path) -> None:
    rc, out = _manage(["system", "health"], env)
    assert rc == 0
    assert "database_backend" in out
    rc, out = _manage(["system", "readiness"], env)
    assert rc == 0
    assert "ready" in out


def test_backup_restore_dry_run(env: Path) -> None:
    rc, _out = _manage(["backup", "create"], env)
    assert rc == 0
    backup_dir = Path(os.environ["BACKUP_DIR"])
    bk = sorted(backup_dir.glob("*.glbak"))[0]
    rc, out = _manage(["backup", "restore", str(bk), "--to", str(env / "restored.db")], env)
    assert rc == 0
    assert "dry_run" in out


def test_manage_never_prints_db_url(env: Path) -> None:
    rc, out = _manage(["db", "status"], env)
    assert rc == 0
    assert str(env / "portal.db") not in out  # the full path is safe, but ensure no creds
    assert "DATABASE_URL=" not in out
