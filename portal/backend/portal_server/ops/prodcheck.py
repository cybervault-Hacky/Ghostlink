"""Production readiness check (Phase 15P).

``ghostlink production check`` validates the environment, required
configuration, database/rate-limiter/migration state, backup configuration and
version consistency. It reports ``PASS`` / ``WARN`` / ``FAIL`` per category and
returns a **non-zero exit code when any mandatory requirement fails**.

It never prints secret values: database URLs are shown redacted, and session
secrets / SMTP passwords / backup keys are only ever reported as present or
absent.
"""

from __future__ import annotations

import json
import os
import platform
from dataclasses import dataclass
from typing import Any

from portal_server.config import ConfigError, PortalConfig, load_config


@dataclass
class CheckResult:
    name: str
    status: str  # PASS | WARN | FAIL
    detail: str = ""
    mandatory: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "status": self.status, "detail": self.detail}


def _redact_url(url: str) -> str:
    if "://" in url:
        head, _, tail = url.partition("://")
        rest = tail.split("@", 1)
        host = rest[-1]
        return f"{head}://***@{host}"
    return "<redacted>"


def run_production_check() -> dict[str, Any]:
    results: list[CheckResult] = []

    # Environment
    env = os.environ.get("APP_ENV", "").strip().lower()
    if env == "production":
        results.append(CheckResult("environment", "PASS", "production", mandatory=True))
    elif env == "staging":
        results.append(CheckResult("environment", "PASS", "staging"))
    elif env == "development":
        results.append(CheckResult("environment", "WARN", "development is not a production env"))
    else:
        results.append(
            CheckResult(
                "environment", "FAIL", f"APP_ENV is not set or invalid ({env!r})", mandatory=True
            )
        )

    # Load + validate config (fail closed)
    try:
        cfg = load_config()
        results.append(
            CheckResult("configuration", "PASS", "config loaded and validated", mandatory=True)
        )
    except ConfigError as exc:
        cfg = None
        results.append(CheckResult("configuration", "FAIL", str(exc), mandatory=True))

    if cfg is not None:
        # Database
        if cfg.database_backend == "postgresql":
            results.append(CheckResult("database", "PASS", "PostgreSQL configured", mandatory=True))
        else:
            results.append(
                CheckResult(
                    "database",
                    "FAIL",
                    "production requires PostgreSQL (SQLite rejected)",
                    mandatory=True,
                )
            )
        # Rate limiter
        if cfg.rate_limit_backend == "postgresql":
            results.append(
                CheckResult(
                    "rate_limiter", "PASS", "distributed PostgreSQL limiter", mandatory=True
                )
            )
        else:
            results.append(
                CheckResult(
                    "rate_limiter",
                    "FAIL",
                    "in-memory limiter rejected in production",
                    mandatory=True,
                )
            )
        # Secure cookies
        results.append(
            CheckResult(
                "secure_cookies",
                "PASS" if cfg.secure_cookies else "FAIL",
                "" if cfg.secure_cookies else "PORTAL_SECURE_COOKIES must be true",
                mandatory=True,
            )
        )
        # HTTPS / public base URL
        if cfg.public_base_url.startswith("https://"):
            results.append(CheckResult("https", "PASS", "PUBLIC_BASE_URL is https"))
        elif cfg.public_base_url:
            results.append(
                CheckResult("https", "FAIL", "PUBLIC_BASE_URL must be https", mandatory=True)
            )
        else:
            results.append(CheckResult("https", "WARN", "PUBLIC_BASE_URL not set"))
        # Allowed hosts
        if cfg.allowed_hosts and "*" not in cfg.allowed_hosts:
            results.append(
                CheckResult("allowed_hosts", "PASS", ",".join(cfg.allowed_hosts), mandatory=True)
            )
        elif cfg.allowed_hosts:
            results.append(
                CheckResult("allowed_hosts", "FAIL", "wildcard not allowed", mandatory=True)
            )
        else:
            results.append(
                CheckResult("allowed_hosts", "FAIL", "ALLOWED_HOSTS required", mandatory=True)
            )
        # Trusted proxy
        if cfg.trusted_proxy and cfg.trusted_proxies:
            results.append(CheckResult("trusted_proxy", "PASS", "proxy configured"))
        elif cfg.trusted_proxy:
            results.append(
                CheckResult("trusted_proxy", "WARN", "proxy trusted but TRUSTED_PROXIES empty")
            )
        else:
            results.append(CheckResult("trusted_proxy", "WARN", "no trusted proxy configured"))
        # Email provider
        if cfg.email_provider not in ("", "dev"):
            results.append(CheckResult("email", "PASS", cfg.email_provider, mandatory=True))
        else:
            results.append(
                CheckResult(
                    "email", "FAIL", "dev email provider rejected in production", mandatory=True
                )
            )
        # Secret presence (never the value)
        if cfg.session_secret and cfg.session_secret != "development-secret":
            results.append(
                CheckResult("session_secret", "PASS", "present and not default", mandatory=True)
            )
        else:
            results.append(
                CheckResult(
                    "session_secret", "FAIL", "SESSION_SECRET missing or default", mandatory=True
                )
            )
        # Backup configuration
        if cfg.backup_encrypt and cfg.backup_passphrase:
            results.append(CheckResult("backup", "PASS", "encrypted backups configured"))
        elif cfg.backup_encrypt:
            results.append(
                CheckResult(
                    "backup",
                    "FAIL",
                    "BACKUP_ENCRYPTION_KEY required when encrypted",
                    mandatory=True,
                )
            )
        else:
            results.append(CheckResult("backup", "WARN", "backup encryption not enabled"))
        # Version consistency
        pkg_version = _package_version()
        if pkg_version and cfg.deployment_version and pkg_version == cfg.deployment_version:
            results.append(CheckResult("version", "PASS", pkg_version, mandatory=True))
        else:
            results.append(
                CheckResult(
                    "version",
                    "WARN",
                    f"package={pkg_version or 'unknown'} deployment={cfg.deployment_version!r}",
                )
            )

    # Migration state — requires DB connectivity; best-effort, non-fatal warn.
    migration_result = _migration_check(cfg)
    if migration_result is not None:
        results.append(migration_result)

    status_counts: dict[str, int] = {"PASS": 0, "WARN": 0, "FAIL": 0}
    for r in results:
        status_counts[r.status] = status_counts.get(r.status, 0) + 1

    mandatory_fail = any(r.status == "FAIL" and r.mandatory for r in results)
    overall = "FAIL" if mandatory_fail else ("WARN" if status_counts["FAIL"] else "PASS")

    return {
        "status": overall,
        "environment": env,
        "package_version": _package_version(),
        "python": platform.python_version(),
        "checks": [r.to_dict() for r in results],
        "counts": status_counts,
    }


def _package_version() -> str:
    try:
        import ghostlink

        return ghostlink.__version__
    except Exception:
        return ""


def _migration_check(cfg: PortalConfig | None) -> CheckResult | None:
    if cfg is None:
        return CheckResult("migrations", "WARN", "config failed to load; migration state unknown")
    try:
        from portal_server.db.pool import PoolConfig, build_database

        db = build_database(
            cfg.db_url,
            PoolConfig(
                pool_min=cfg.pool_min,
                pool_max=cfg.pool_max,
                connect_timeout=cfg.database_connect_timeout,
                statement_timeout_ms=cfg.database_statement_timeout_ms,
            ),
        )
        try:
            version = db.schema_version
            db.close()
            return CheckResult(
                "migrations",
                "PASS" if version >= 3 else "WARN",
                f"schema v{version}",
            )
        except Exception as exc:
            return CheckResult("migrations", "WARN", f"DB unreachable: {exc.__class__.__name__}")
    except Exception as exc:
        return CheckResult("migrations", "WARN", f"cannot build DB: {exc.__class__.__name__}")


def production_check_as_json() -> str:
    return json.dumps(run_production_check(), indent=2, default=str)


__all__ = ["production_check_as_json", "run_production_check"]
