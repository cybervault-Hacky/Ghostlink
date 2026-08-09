"""Production administration CLI for the GhostLink portal (Phase 13).

Run: ``python -m portal_server.manage <command>``

Commands (deterministic exit codes; 0 ok / 1 error / 2 usage):
  db status                 show schema version, migrations, checksum health
  db migrate                apply pending migrations
  db verify                 verify migration checksums (fail-closed)
  backup create             create a backup (optionally encrypted)
  backup verify <file>      verify a backup's integrity
  backup list               list backups in the configured directory
  backup restore <file>     restore into an isolated DB (dry-run by default)
  system health             safe operational summary (DB + schema)
  system readiness          readiness probe (DB connectivity + migrations)
  security-audit            run the deterministic offline security audit

No command prints secrets (database URLs, credentials, tokens). DATABASE_URL is
read from the environment; it is never echoed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from portal_server.config import ConfigError, load_config
from portal_server.db import (
    DatabaseBackend,
    DatabaseMigrationError,
    migration_status,
)
from portal_server.db.pool import PoolConfig, build_database
from portal_server.ops.backup import (
    BackupError,
    create_backup,
    list_backups,
    restore_backup,
    verify_backup,
)


def _connect(args: argparse.Namespace) -> DatabaseBackend:
    cfg = load_config()
    url = getattr(args, "db", None) or cfg.db_url
    pool = PoolConfig(
        pool_min=cfg.pool_min,
        pool_max=cfg.pool_max,
        connect_timeout=cfg.database_connect_timeout,
        statement_timeout_ms=cfg.database_statement_timeout_ms,
    )
    return build_database(url, pool)


def _out(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _fail(msg: str, code: int = 1) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return code


def cmd_db_status(args: argparse.Namespace) -> int:
    try:
        db = _connect(args)
        applied = db.schema_version
        checksums = _read_checksums(db)
    except (DatabaseMigrationError, ConfigError) as exc:
        return _fail(str(exc))
    except Exception as exc:  # connection failures
        return _fail(f"cannot connect to database: {exc}")
    _out(migration_status(applied, checksums))
    return 0


def cmd_db_migrate(args: argparse.Namespace) -> int:
    try:
        db = _connect(args)  # construction applies pending migrations
        _out({"applied_schema_version": db.schema_version, "migrated": True})
    except (DatabaseMigrationError, ConfigError) as exc:
        return _fail(str(exc))
    return 0


def cmd_db_verify(args: argparse.Namespace) -> int:
    try:
        db = _connect(args)
        applied = db.schema_version
        checksums = _read_checksums(db)
        status = migration_status(applied, checksums)
    except (DatabaseMigrationError, ConfigError) as exc:
        return _fail(str(exc))
    except Exception as exc:
        return _fail(f"cannot connect to database: {exc}")
    _out(status)
    return 0 if status["healthy"] else _fail("migration checksum/schema verification failed")


def _read_checksums(db: DatabaseBackend) -> dict[int, str]:
    rows = db.query("SELECT value FROM schema_meta WHERE key LIKE 'migration_%_checksum'")
    out: dict[int, str] = {}
    for r in rows:
        ver_s, _, cksum = str(r["value"]).partition(":")
        try:
            out[int(ver_s)] = cksum
        except ValueError:
            continue
    return out


def cmd_backup_create(args: argparse.Namespace) -> int:
    try:
        cfg = load_config()
        db = _connect(args)
    except (ConfigError, DatabaseMigrationError) as exc:
        return _fail(str(exc))
    try:
        result = create_backup(
            db,
            backup_dir=args.dir or cfg.backup_dir,
            schema_version=db.schema_version,
            encrypt=args.encrypt,
            passphrase=args.passphrase or cfg.backup_passphrase,
            retention_count=cfg.backup_retention_count,
        )
    except BackupError as exc:
        return _fail(str(exc))
    _out(result)
    return 0


def cmd_backup_verify(args: argparse.Namespace) -> int:
    try:
        result = verify_backup(args.path, passphrase=args.passphrase or "")
    except (BackupError, OSError, json.JSONDecodeError) as exc:
        return _fail(str(exc))
    _out(result)
    return 0


def cmd_backup_list(args: argparse.Namespace) -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        return _fail(str(exc))
    _out(list_backups(args.dir or cfg.backup_dir))
    return 0


def cmd_backup_restore(args: argparse.Namespace) -> int:
    try:
        cfg = load_config()
        target = args.to or cfg.db_url
    except ConfigError as exc:
        return _fail(str(exc))
    try:
        target_db = build_database(target, PoolConfig())
        result = restore_backup(
            target_db,
            args.path,
            passphrase=args.passphrase or cfg.backup_passphrase,
            dry_run=not args.apply,
        )
    except (BackupError, DatabaseMigrationError, ConfigError) as exc:
        return _fail(str(exc))
    _out(result)
    return 0


def cmd_system_health(args: argparse.Namespace) -> int:
    try:
        db = _connect(args)
        payload = {
            "status": "ok",
            "database_backend": db.backend_name,
            "schema_version": db.schema_version,
        }
        _out(payload)
        return 0
    except Exception as exc:
        return _fail(f"health check failed: {exc}")


def cmd_system_readiness(args: argparse.Namespace) -> int:
    try:
        db = _connect(args)
        ready = db.schema_version >= 3
    except Exception as exc:
        _out({"status": "not_ready", "checks": [str(exc)]})
        return 1
    _out(
        {
            "status": "ready" if ready else "not_ready",
            "database_backend": db.backend_name,
            "schema_version": db.schema_version,
        }
    )
    return 0 if ready else 1


def cmd_security_audit(args: argparse.Namespace) -> int:
    import subprocess

    root = Path(__file__).resolve().parents[2]  # portal/backend
    checker = root / ".." / ".." / "scripts" / "security_check.py"
    if not checker.exists():
        return _fail("scripts/security_check.py not found in repository.")
    proc = subprocess.run(
        [sys.executable, str(checker)],
        cwd=str(root.parents[1]),
    )
    return proc.returncode


def _add_db_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db", default=None, help="override DATABASE_URL (never logged)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="portal_server.manage")
    sub = parser.add_subparsers(dest="command", required=True)

    p_db = sub.add_parser("db")
    db_sub = p_db.add_subparsers(dest="db_command", required=True)
    for name, handler, help_text in (
        ("status", cmd_db_status, "show schema/migration status"),
        ("migrate", cmd_db_migrate, "apply pending migrations"),
        ("verify", cmd_db_verify, "verify migration checksums"),
    ):
        pp = db_sub.add_parser(name, help=help_text)
        _add_db_args(pp)
        pp.set_defaults(func=handler)

    p_backup = sub.add_parser("backup")
    bk_sub = p_backup.add_subparsers(dest="backup_command", required=True)

    pc = bk_sub.add_parser("create", help="create a backup")
    _add_db_args(pc)
    pc.add_argument("--dir", default=None, help="backup directory")
    pc.add_argument("--encrypt", action="store_true", help="encrypt the backup")
    pc.add_argument("--passphrase", default="", help="encryption passphrase (never logged)")
    pc.set_defaults(func=cmd_backup_create)

    pv = bk_sub.add_parser("verify", help="verify a backup")
    pv.add_argument("path")
    pv.add_argument("--passphrase", default="")
    pv.set_defaults(func=cmd_backup_verify)

    pl = bk_sub.add_parser("list", help="list backups")
    pl.add_argument("--dir", default=None)
    pl.set_defaults(func=cmd_backup_list)

    pr = bk_sub.add_parser("restore", help="restore a backup (dry-run by default)")
    pr.add_argument("path")
    pr.add_argument("--to", default=None, help="target DATABASE_URL (isolated/fresh DB)")
    pr.add_argument("--passphrase", default="")
    pr.add_argument("--apply", action="store_true", help="actually apply (non-dry-run)")
    pr.set_defaults(func=cmd_backup_restore)

    p_sys = sub.add_parser("system")
    sys_sub = p_sys.add_subparsers(dest="system_command", required=True)
    for name, handler, help_text in (
        ("health", cmd_system_health, "safe operational summary"),
        ("readiness", cmd_system_readiness, "readiness probe"),
    ):
        ps = sys_sub.add_parser(name, help=help_text)
        _add_db_args(ps)
        ps.set_defaults(func=handler)

    sa = sub.add_parser("security-audit", help="run the offline security audit")
    sa.set_defaults(func=cmd_security_audit)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        return 2
    return int(args.func(args))


if __name__ == "__main__":
    sys.exit(main())
