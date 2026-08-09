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
            )
        )
        assert cfg.is_production
        assert cfg.secure_cookies
        assert cfg.webauthn_rp_id == "portal.example.com"

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
