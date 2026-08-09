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
    trusted_proxy = source.get("PORTAL_TRUSTED_PROXY", "false").strip().lower() in (
        "1",
        "true",
        "yes",
    )

    if is_prod:
        # Production must never run with development conveniences.
        if email_provider in ("dev", ""):
            raise ConfigError("EMAIL_PROVIDER must be a real provider in production (not 'dev').")
        if not secure_cookies:
            raise ConfigError("PORTAL_SECURE_COOKIES must be true in production.")
        if session_secret == "development-secret":
            raise ConfigError("SESSION_SECRET must not be the development default in production.")

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
    smtp_user = source.get("EMAIL_SMTP_USER", "").strip()
    smtp_pass = source.get("EMAIL_SMTP_PASSWORD", "").strip()
    smtp_from = source.get("EMAIL_FROM", "").strip()
    if is_prod and email_provider == "smtp" and not smtp_host:
        raise ConfigError("EMAIL_SMTP_HOST is required when EMAIL_PROVIDER=smtp.")

    # Parse optional rate-limit overrides (testing): RATE_LIMIT_<SCOPE>=<n>:<window>
    overrides: dict[str, tuple[int, float]] = {}
    for key, value in source.items():
        if key.startswith("RATE_LIMIT_") and value:
            scope = key[len("RATE_LIMIT_") :].lower()
            try:
                count_s, _, window_s = value.partition(":")
                overrides[scope] = (int(count_s), float(window_s))
            except ValueError as exc:
                raise ConfigError(
                    f"Invalid RATE_LIMIT_{key[len('RATE_LIMIT_') :]} value: {value!r}"
                ) from exc

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
    )


__all__ = ["ConfigError", "PortalConfig", "load_config"]
