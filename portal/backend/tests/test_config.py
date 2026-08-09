"""Production configuration tests (Phase 11A)."""

from __future__ import annotations

import pytest
from portal_server.config import ConfigError, load_config


def _env(**overrides: str) -> dict[str, str]:
    base = {"APP_ENV": "development"}
    base.update({k: v for k, v in overrides.items() if v is not None})
    return base


class TestConfigValidation:
    def test_dev_defaults(self) -> None:
        cfg = load_config(_env(APP_ENV="development"))
        assert cfg.app_env == "development"
        assert cfg.db_url == "portal.db"
        assert not cfg.secure_cookies

    def test_invalid_env_rejected(self) -> None:
        with pytest.raises(ConfigError):
            load_config(_env(APP_ENV="banana"))

    def test_production_requires_session_secret(self) -> None:
        with pytest.raises(ConfigError):
            load_config(
                _env(APP_ENV="production", DATABASE_URL="postgres://x", SESSION_SECRET=None)
            )

    def test_production_requires_real_email_provider(self) -> None:
        with pytest.raises(ConfigError):
            load_config(
                _env(
                    APP_ENV="production",
                    DATABASE_URL="postgres://x",
                    SESSION_SECRET="a-long-random-secret-123",
                    EMAIL_PROVIDER="dev",
                )
            )

    def test_production_requires_secure_cookies(self) -> None:
        with pytest.raises(ConfigError):
            load_config(
                _env(
                    APP_ENV="production",
                    DATABASE_URL="postgres://x",
                    SESSION_SECRET="a-long-random-secret-123",
                    EMAIL_PROVIDER="smtp",
                    PORTAL_SECURE_COOKIES="false",
                )
            )

    def test_production_requires_webauthn_rp(self) -> None:
        with pytest.raises(ConfigError):
            load_config(
                _env(
                    APP_ENV="production",
                    DATABASE_URL="postgres://x",
                    SESSION_SECRET="a-long-random-secret-123",
                    EMAIL_PROVIDER="smtp",
                    PORTAL_SECURE_COOKIES="true",
                )
            )

    def test_valid_production_config(self) -> None:
        cfg = load_config(
            _env(
                APP_ENV="production",
                DATABASE_URL="postgres://user:pass@db/ghostlink",
                SESSION_SECRET="a-long-random-secret-123",
                EMAIL_PROVIDER="smtp",
                PORTAL_SECURE_COOKIES="true",
                EMAIL_SMTP_HOST="smtp.example.com",
                WEBAUTHN_RP_ID="portal.example.com",
                WEBAUTHN_ORIGIN="https://portal.example.com",
                ALLOWED_HOSTS="portal.example.com",
                RATE_LIMIT_BACKEND="postgresql",
                PUBLIC_BASE_URL="https://portal.example.com",
            )
        )
        assert cfg.is_production
        assert cfg.secure_cookies
        assert cfg.webauthn_rp_id == "portal.example.com"
        assert cfg.public_base_url == "https://portal.example.com"
        assert cfg.database_backend == "postgresql"

    def test_dev_rejects_production_secret_default_only_in_prod(self) -> None:
        # In development the fallback secret is allowed.
        cfg = load_config(_env(APP_ENV="development"))
        assert cfg.session_secret == "development-secret"

    def test_production_rejects_dev_secret(self) -> None:
        with pytest.raises(ConfigError):
            load_config(
                _env(
                    APP_ENV="production",
                    DATABASE_URL="postgres://x",
                    SESSION_SECRET="development-secret",
                    EMAIL_PROVIDER="smtp",
                    PORTAL_SECURE_COOKIES="true",
                    EMAIL_SMTP_HOST="smtp.example.com",
                    WEBAUTHN_RP_ID="portal.example.com",
                    WEBAUTHN_ORIGIN="https://portal.example.com",
                )
            )

    def test_rate_limit_override(self) -> None:
        cfg = load_config(_env(APP_ENV="development", RATE_LIMIT_SIGN_IN="3:60"))
        assert cfg.rate_limit_overrides["sign_in"] == (3, 60.0)


_PROD_REQUIRED = {
    "APP_ENV": "production",
    "DATABASE_URL": "postgres://u:p@db/ghostlink",
    "SESSION_SECRET": "a-long-random-secret-123",
    "EMAIL_PROVIDER": "smtp",
    "EMAIL_SMTP_HOST": "smtp.example.com",
    "PORTAL_SECURE_COOKIES": "true",
    "WEBAUTHN_RP_ID": "portal.example.com",
    "WEBAUTHN_ORIGIN": "https://portal.example.com",
    "ALLOWED_HOSTS": "portal.example.com",
    "RATE_LIMIT_BACKEND": "postgresql",
    "PUBLIC_BASE_URL": "https://portal.example.com",
}


def _prod(**overrides: str) -> dict[str, str]:
    base = dict(_PROD_REQUIRED)
    for k, v in overrides.items():
        if v is None:
            base.pop(k, None)
        else:
            base[k] = v
    return base


class TestProductionFailClosed:
    """Phase 14 — production configuration must fail closed (section 3)."""

    def test_valid_production_uses_postgres(self) -> None:
        cfg = load_config(_prod())
        assert cfg.database_backend == "postgresql"

    def test_sqlite_rejected_in_production(self) -> None:
        with pytest.raises(ConfigError):
            load_config(_prod(DATABASE_URL="portal.db"))

    def test_memory_rate_limiter_rejected_in_production(self) -> None:
        with pytest.raises(ConfigError):
            load_config(_prod(RATE_LIMIT_BACKEND="memory"))

    def test_wildcard_hosts_rejected(self) -> None:
        with pytest.raises(ConfigError):
            load_config(_prod(ALLOWED_HOSTS="*"))

    def test_public_base_url_required_https(self) -> None:
        with pytest.raises(ConfigError):
            load_config(_prod(PUBLIC_BASE_URL=None))
        with pytest.raises(ConfigError):
            load_config(_prod(PUBLIC_BASE_URL="http://portal.example.com"))

    def test_insecure_cookies_rejected(self) -> None:
        with pytest.raises(ConfigError):
            load_config(_prod(PORTAL_SECURE_COOKIES="false"))

    def test_dev_email_provider_rejected(self) -> None:
        with pytest.raises(ConfigError):
            load_config(_prod(EMAIL_PROVIDER="dev"))

    def test_dev_secret_rejected(self) -> None:
        with pytest.raises(ConfigError):
            load_config(_prod(SESSION_SECRET="development-secret"))

    def test_encrypted_backup_requires_key(self) -> None:
        with pytest.raises(ConfigError):
            load_config(_prod(BACKUP_ENCRYPT="true"))

    def test_db_pool_naming(self) -> None:
        cfg = load_config(_prod(DB_POOL_MIN="2", DB_POOL_MAX="8", DB_IDLE_TIMEOUT="120"))
        assert cfg.pool_min == 2
        assert cfg.pool_max == 8
        assert cfg.database_idle_timeout == 120

    def test_backup_encryption_key_naming(self) -> None:
        cfg = load_config(_prod(BACKUP_ENCRYPT="true", BACKUP_ENCRYPTION_KEY="k" * 32))
        assert cfg.backup_passphrase == "k" * 32

    def test_email_smtp_username_naming(self) -> None:
        cfg = load_config(_prod(EMAIL_SMTP_USERNAME="sender@example.com"))
        assert cfg.smtp_user == "sender@example.com"
