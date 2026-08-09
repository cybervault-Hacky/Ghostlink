"""Email provider abstraction for the Developer Portal (Phase 11E).

The portal never sends passwords, secrets, or full recovery material in
email. It sends only short-lived, single-use tokens embedded in URLs; the
tokens themselves are stored hashed at rest and never logged.

Provider model:
* ``DevEmailProvider`` — records messages in memory / to a local log (for
  local development and tests). Never claims real delivery.
* ``SMTPEmailProvider`` — a thin, dependency-light SMTP client built on the
  standard library ``smtplib``. Configured via the ``EMAIL_*`` environment
  variables; used in production. It is intentionally minimal and is a clear
  extension point for a transactional API provider.
* ``get_email_provider(config)`` — factory that returns the provider named
  by ``EMAIL_PROVIDER`` (``dev`` or ``smtp``) and fails closed on anything
  unknown.
"""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from email.message import EmailMessage

from portal_server.config import PortalConfig

_logger = logging.getLogger("ghostlink.portal.email")

_DEFAULT_FROM = "no-reply@ghostlink.local"


class EmailProvider:
    """Base interface for sending portal emails (metadata + short tokens only)."""

    enabled: bool = True

    def send_verification(self, to_email: str, token: str) -> None:
        raise NotImplementedError

    def send_password_reset(self, to_email: str, token: str) -> None:
        raise NotImplementedError


@dataclass
class DevEmailProvider(EmailProvider):
    """Records messages in memory; never sends. For dev/testing only."""

    sent: list[dict[str, str]] = field(default_factory=list)
    enabled: bool = True

    def send_verification(self, to_email: str, token: str) -> None:
        if not self.enabled:
            return
        self.sent.append({"kind": "email_verify", "to": to_email, "token": token})
        _logger.info("dev email (verification) queued for %s", to_email)

    def send_password_reset(self, to_email: str, token: str) -> None:
        if not self.enabled:
            return
        self.sent.append({"kind": "password_reset", "to": to_email, "token": token})
        _logger.info("dev email (password reset) queued for %s", to_email)


@dataclass
class SMTPEmailProvider(EmailProvider):
    """A standard-library SMTP provider for production.

    Reads SMTP host/port/user/password/from from the environment. Messages
    are plain-text, TLS-protected (STARTTLS) when available, and carry only
    a short-lived token.
    """

    host: str
    port: int = 587
    username: str = ""
    password: str = ""
    from_email: str = _DEFAULT_FROM
    starttls: bool = True
    enabled: bool = True

    def _send(self, to_email: str, subject: str, body: str) -> None:
        message = EmailMessage()
        message["Subject"] = subject
        message["From"] = self.from_email
        message["To"] = to_email
        message.set_content(body)
        context = ssl.create_default_context()
        with smtplib.SMTP(self.host, self.port, timeout=10) as server:
            if self.starttls:
                server.starttls(context=context)
            if self.username:
                server.login(self.username, self.password)
            server.send_message(message)
        _logger.info("email sent to %s (subject=%r)", to_email, subject)

    def send_verification(self, to_email: str, token: str) -> None:
        body = f"Your GhostLink verification token: {token}\nIt expires in 24h and is single-use."
        self._send(to_email, "Verify your GhostLink developer account", body)

    def send_password_reset(self, to_email: str, token: str) -> None:
        body = f"Your GhostLink password reset token: {token}\nIt expires in 1h and is single-use."
        self._send(to_email, "Reset your GhostLink password", body)


def get_email_provider(config: PortalConfig) -> EmailProvider:
    """Factory: return the provider named by ``config.email_provider``.

    Fails closed on unknown providers so production never silently falls
    back to the dev adapter.
    """
    name = config.email_provider if config else "dev"
    if name == "dev":
        return DevEmailProvider(enabled=True)
    if name == "smtp":
        return SMTPEmailProvider(
            host=config.smtp_host,
            port=config.smtp_port,
            username=config.smtp_user,
            password=config.smtp_pass,
            from_email=config.smtp_from,
        )
    raise ValueError(f"Unknown EMAIL_PROVIDER {name!r}")


__all__ = ["DevEmailProvider", "EmailProvider", "SMTPEmailProvider", "get_email_provider"]
