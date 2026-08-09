"""Production configuration layer for the Developer Portal (Phase 11).

Strict, environment-driven configuration with explicit environments:

* ``APP_ENV`` — ``development`` (safe defaults) or ``production`` (fail
  closed on missing required values). Anything else is rejected.
* Production **rejects** development conveniences (e.g. a non-empty
  ``EMAIL_PROVIDER=dev``) and requires a real session secret, a database
  URL, WebAuthn RP id/origin, and secure cookies.

No secret value is ever printed or logged at startup.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field


class ConfigError(Exception):
    """Raised when the environment configuration is invalid or incomplete."""


@dataclass(frozen=True, slots=True)
class PortalConfig:
    app_env: str
    db_url: str
    session_secret: str
    secure_cookies: bool
    email_provider: str
    webauthn_rp_id: str
    webauthn_origin: str
    trusted_proxy: bool
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_pass: str = ""
    smtp_from: str = ""
    csrf_header: str = "X-CSRF-Token"
    cookie_name: str = "gl_portal_session"
    session_ttl_seconds: int = 7 * 24 * 3600
    idle_timeout_seconds: int = 30 * 60
    verify_token_ttl_seconds: int = 24 * 3600
    reset_token_ttl_seconds: int = 3600
    # Optional overrides (dev/testing).
    rate_limit_overrides: dict[str, tuple[int, float]] = field(default_factory=dict)
    # ---- Phase 13 production infrastructure ---------------------------
    database_backend: str = "sqlite"  # sqlite | postgresql (derived from db_url)
    pool_min: int = 1
    pool_max: int = 10
    database_connect_timeout: int = 5
    database_statement_timeout_ms: int = 15_000
    database_idle_timeout: int = 300
    rate_limit_backend: str = "memory"  # memory | postgresql
    allowed_hosts: tuple[str, ...] = ()
    trusted_proxies: tuple[str, ...] = ()
    log_level: str = "info"
    log_format: str = "json"
    request_id_header: str = "X-Request-ID"
    deployment_version: str = ""
    public_base_url: str = ""
    # Data retention (days) — 0 disables that cleanup.
    retention_sessions_days: int = 90
    retention_expired_tokens_days: int = 7
    retention_security_events_days: int = 365
    retention_api_activity_days: int = 180
    retention_pairing_days: int = 30
    retention_webauthn_challenges_days: int = 1
    retention_verification_tokens_days: int = 7
    # Backup configuration.
    backup_dir: str = "./backups"
    backup_retention_count: int = 30
    backup_encrypt: bool = False
    backup_passphrase: str = ""

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


_ENV_MAP = {
    "development": "development",
    "staging": "staging",
    "production": "production",
}


def _require_env(source: Mapping[str, str], name: str, *, required_in: tuple[str, ...]) -> str:
    value = source.get(name, "").strip()
    if not value and source.get("APP_ENV", "development") in required_in:
        raise ConfigError(f"Missing required environment variable: {name}")
    return value


def load_config(env: dict[str, str] | None = None) -> PortalConfig:
    """Load and validate portal configuration from the environment.

    ``env`` is injectable for tests (defaults to ``os.environ``).
    """
    source = env if env is not None else os.environ
    app_env_raw = source.get("APP_ENV", "development").strip().lower()
    if app_env_raw not in _ENV_MAP:
        raise ConfigError(f"APP_ENV must be one of {sorted(_ENV_MAP)}, got {app_env_raw!r}.")
    app_env = _ENV_MAP[app_env_raw]

    is_prod = app_env == "production"
    db_url = (
        _require_env(source, "DATABASE_URL", required_in=("production", "staging")) or "portal.db"
    )
    session_secret = _require_env(source, "SESSION_SECRET", required_in=("production", "staging"))
    secure_cookies = source.get("PORTAL_SECURE_COOKIES", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    email_provider = source.get("EMAIL_PROVIDER", "dev").strip().lower() or "dev"
    rp_id = _require_env(source, "WEBAUTHN_RP_ID", required_in=("production",))
    origin = _require_env(source, "WEBAUTHN_ORIGIN", required_in=("production",))
    public_base_url = _require_env(source, "PUBLIC_BASE_URL", required_in=("production",)).strip()
    trusted_proxy = source.get("PORTAL_TRUSTED_PROXY", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    # ---- Phase 13 production infrastructure -----------------------------
    def _csv(name: str) -> tuple[str, ...]:
        return tuple(x.strip() for x in source.get(name, "").split(",") if x.strip())

    allowed_hosts = _csv("ALLOWED_HOSTS")
    trusted_proxies = _csv("TRUSTED_PROXIES")
    rate_limit_backend = source.get("RATE_LIMIT_BACKEND", "memory").strip().lower() or "memory"
    log_level = source.get("LOG_LEVEL", "info").strip().lower() or "info"
    log_format = source.get("LOG_FORMAT", "json").strip().lower() or "json"
    request_id_header = source.get("REQUEST_ID_HEADER", "X-Request-ID").strip() or "X-Request-ID"
    deployment_version = source.get("DEPLOYMENT_VERSION", "").strip()

    def _pool_int(name: str, default: int) -> int:
        raw = source.get(name, "").strip()
        try:
            return int(raw) if raw else default
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer.") from exc

    # Phase 14 pool naming (DB_*) with Phase 13 aliases (DATABASE_*) retained.
    def _int_alias(name: str, alias: str, default: int) -> int:
        raw = source.get(name, "").strip() or source.get(alias, "").strip()
        try:
            return int(raw) if raw else default
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer.") from exc

    pool_min = _int_alias("DB_POOL_MIN", "DATABASE_POOL_MIN", 1)
    pool_max = _int_alias("DB_POOL_MAX", "DATABASE_POOL_MAX", 10)
    connect_timeout = _int_alias("DB_CONNECT_TIMEOUT", "DATABASE_CONNECT_TIMEOUT", 5)
    statement_timeout_ms = _int_alias("DB_STATEMENT_TIMEOUT", "DATABASE_STATEMENT_TIMEOUT", 15_000)
    db_idle_timeout = _int_alias("DB_IDLE_TIMEOUT", "DATABASE_IDLE_TIMEOUT", 300)

    def _retention(name: str, default: int) -> int:
        raw = source.get(name, "").strip()
        try:
            return int(raw) if raw else default
        except ValueError as exc:
            raise ConfigError(f"{name} must be an integer (days).") from exc

    retention = {
        "retention_sessions_days": _retention("RETENTION_SESSIONS_DAYS", 90),
        "retention_expired_tokens_days": _retention("RETENTION_EXPIRED_TOKENS_DAYS", 7),
        "retention_security_events_days": _retention("RETENTION_SECURITY_EVENTS_DAYS", 365),
        "retention_api_activity_days": _retention("RETENTION_API_ACTIVITY_DAYS", 180),
        "retention_pairing_days": _retention("RETENTION_PAIRING_DAYS", 30),
        "retention_webauthn_challenges_days": _retention("RETENTION_WEBAUTHN_CHALLENGES_DAYS", 1),
        "retention_verification_tokens_days": _retention("RETENTION_VERIFICATION_TOKENS_DAYS", 7),
    }
    backup_dir = source.get("BACKUP_DIR", "./backups").strip() or "./backups"
    backup_retention_count = _pool_int("BACKUP_RETENTION_COUNT", 30)
    backup_encrypt = source.get("BACKUP_ENCRYPT", "false").strip().lower() in ("1", "true", "yes")
    # Phase 14 encryption-key naming with Phase 13 passphrase alias.
    backup_passphrase = (
        source.get("BACKUP_ENCRYPTION_KEY", "").strip()
        or source.get("BACKUP_PASSPHRASE", "").strip()
    )

    if rate_limit_backend not in ("memory", "postgresql"):
        raise ConfigError("RATE_LIMIT_BACKEND must be 'memory' or 'postgresql'.")
    if log_level not in ("debug", "info", "warning", "error", "critical"):
        raise ConfigError("LOG_LEVEL must be one of debug/info/warning/error/critical.")
    if log_format not in ("json", "text"):
        raise ConfigError("LOG_FORMAT must be 'json' or 'text'.")
    if pool_min < 1:
        raise ConfigError("DB_POOL_MIN must be >= 1.")
    if pool_max < pool_min:
        raise ConfigError("DB_POOL_MAX must be >= DB_POOL_MIN.")
    if db_idle_timeout < 1:
        raise ConfigError("DB_IDLE_TIMEOUT must be >= 1.")
    if backup_retention_count < 1:
        raise ConfigError("BACKUP_RETENTION_COUNT must be >= 1.")

    if is_prod:
        # Production must never run with development conveniences.
        if email_provider in ("dev", ""):
            raise ConfigError("EMAIL_PROVIDER must be a real provider in production (not 'dev').")
        if not secure_cookies:
            raise ConfigError("PORTAL_SECURE_COOKIES must be true in production.")
        if session_secret == "development-secret":
            raise ConfigError("SESSION_SECRET must not be the development default in production.")
        if not allowed_hosts:
            raise ConfigError("ALLOWED_HOSTS must be configured in production.")
        if "*" in allowed_hosts:
            raise ConfigError("ALLOWED_HOSTS must not contain a wildcard in production.")
        if public_base_url and not public_base_url.startswith("https://"):
            raise ConfigError("PUBLIC_BASE_URL must be an https:// URL in production.")
        # Production must never run on SQLite (single-file, per-process).
        if not db_url.lower().startswith("postgres"):
            raise ConfigError(
                "DATABASE_URL must point to PostgreSQL in production; SQLite is not "
                "an allowed production database."
            )
        if rate_limit_backend == "memory":
            raise ConfigError(
                "RATE_LIMIT_BACKEND must be 'postgresql' in production (fail closed; "
                "per-process limits are unsafe)."
            )
        if backup_encrypt and not backup_passphrase:
            raise ConfigError(
                "BACKUP_ENCRYPTION_KEY is required when BACKUP_ENCRYPT=true in production."
            )

    # Session secret default is safe only in non-production.
    if not session_secret:
        session_secret = "development-secret"

    # Email (SMTP) settings — required in production when EMAIL_PROVIDER=smtp.
    smtp_host = source.get("EMAIL_SMTP_HOST", "").strip()
    smtp_port = 587
    try:
        smtp_port = int(source.get("EMAIL_SMTP_PORT", "587"))
    except ValueError as exc:
        raise ConfigError("EMAIL_SMTP_PORT must be an integer.") from exc
    smtp_user = (
        source.get("EMAIL_SMTP_USERNAME", "").strip() or source.get("EMAIL_SMTP_USER", "").strip()
    )
    smtp_pass = source.get("EMAIL_SMTP_PASSWORD", "").strip()
    smtp_from = source.get("EMAIL_FROM", "").strip()
    if is_prod and email_provider == "smtp" and not smtp_host:
        raise ConfigError("EMAIL_SMTP_HOST is required when EMAIL_PROVIDER=smtp.")

    # Parse optional rate-limit overrides (testing): RATE_LIMIT_<SCOPE>=<n>:<window>
    overrides: dict[str, tuple[int, float]] = {}
    for key, value in source.items():
        if key == "RATE_LIMIT_BACKEND":
            continue
        if key.startswith("RATE_LIMIT_") and value:
            scope = key[len("RATE_LIMIT_") :].lower()
            try:
                count_s, _, window_s = value.partition(":")
                overrides[scope] = (int(count_s), float(window_s))
            except ValueError as exc:
                raise ConfigError(
                    f"Invalid RATE_LIMIT_{key[len('RATE_LIMIT_') :]} value: {value!r}"
                ) from exc

    database_backend = "postgresql" if db_url.lower().startswith("postgres") else "sqlite"

    return PortalConfig(
        app_env=app_env,
        db_url=db_url,
        session_secret=session_secret,
        secure_cookies=secure_cookies or is_prod,
        email_provider=email_provider,
        webauthn_rp_id=rp_id,
        webauthn_origin=origin,
        trusted_proxy=trusted_proxy,
        smtp_host=smtp_host,
        smtp_port=smtp_port,
        smtp_user=smtp_user,
        smtp_pass=smtp_pass,
        smtp_from=smtp_from,
        rate_limit_overrides=overrides,
        database_backend=database_backend,
        pool_min=pool_min,
        pool_max=pool_max,
        database_connect_timeout=connect_timeout,
        database_statement_timeout_ms=statement_timeout_ms,
        rate_limit_backend=rate_limit_backend,
        allowed_hosts=allowed_hosts,
        trusted_proxies=trusted_proxies,
        log_level=log_level,
        log_format=log_format,
        request_id_header=request_id_header,
        deployment_version=deployment_version,
        public_base_url=public_base_url,
        database_idle_timeout=db_idle_timeout,
        retention_sessions_days=retention["retention_sessions_days"],
        retention_expired_tokens_days=retention["retention_expired_tokens_days"],
        retention_security_events_days=retention["retention_security_events_days"],
        retention_api_activity_days=retention["retention_api_activity_days"],
        retention_pairing_days=retention["retention_pairing_days"],
        retention_webauthn_challenges_days=retention["retention_webauthn_challenges_days"],
        retention_verification_tokens_days=retention["retention_verification_tokens_days"],
        backup_dir=backup_dir,
        backup_retention_count=backup_retention_count,
        backup_encrypt=backup_encrypt,
        backup_passphrase=backup_passphrase,
    )


__all__ = ["ConfigError", "PortalConfig", "load_config"]
