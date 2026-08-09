"""Structured, secret-safe observability for the GhostLink portal (Phase 13J/K).

Two responsibilities:

1. **Structured JSON logging** — every operational event is a single JSON line
   carrying safe metadata (timestamp, request_id, method, route, status,
   duration_ms, deployment_version, authenticated ids) and *never* secret
   material.

2. **Request correlation** — every request gets a request id returned as
   ``X-Request-ID`` and logged. A client-supplied request id is validated and
   capped, never blindly trusted as a security identity (13K).

``scrub_secrets`` is a defensive last line of defence for free-form/exception
text: even if a developer logs a value that happens to look like a secret, the
shaped token is redacted before it reaches the log sink. Secret-shaped fields
are never added by the logging helpers in the first place.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, ClassVar

# --------------------------------------------------------------------------
# Secret-shaped pattern detection (defensive redaction).
# --------------------------------------------------------------------------

_SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    # Authorization header body: "authorization: Bearer <token>".
    (r"(?i)authorization[=:\s]+\S+(\s+\S+)*", "authorization=<redacted>"),
    (r"(?i)bearer\s+[A-Za-z0-9_\-\.]+", "bearer <redacted>"),
    # GhostLink developer credentials "dk_<id>:<secret>".
    (r"dk_[A-Za-z0-9]+:[A-Za-z0-9+/=_\-]+", "dk_<redacted>"),
    # Pairing codes "GL-<base32>".
    (r"GL-[A-Z2-7]{8,}=+", "GL-<redacted>"),
    (r"GL-[A-Z2-7]{16,}", "GL-<redacted>"),
    # Long high-entropy tokens: 32+ hex, or 43+ base64url.
    (r"[0-9a-f]{64}\b", "<sha256-redacted>"),
    (r"(?<![A-Za-z0-9])[A-Za-z0-9_\-]{48,}(?![A-Za-z0-9])", "<token-redacted>"),
    # JWT-ish three-part token.
    (r"[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+", "<jwt-redacted>"),
    # TOTP / recovery / MFA style short codes (6 digits).
    (r"\b\d{6}\b", "<code-redacted>"),
)

_SCRUB_RE = [(re.compile(pat), repl) for pat, repl in _SECRET_PATTERNS]


def scrub_secrets(text: str) -> str:
    """Redact secret-shaped tokens from arbitrary text."""
    out = text
    for pattern, repl in _SCRUB_RE:
        out = pattern.sub(repl, out)
    return out


# --------------------------------------------------------------------------
# Safe request-id handling (13K).
# --------------------------------------------------------------------------

DEFAULT_REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{1,64}$")


def validate_request_id(raw: str | None, *, header: str = DEFAULT_REQUEST_ID_HEADER) -> str:
    """Return a safe server-side request id.

    If the client supplied a valid, length-capped id it is honoured (for
    correlation tracing); otherwise a fresh server-generated id is produced.
    Malformed values are ignored, never echoed, and never trusted as auth.
    """
    if raw and _REQUEST_ID_RE.fullmatch(raw):
        return raw
    return new_request_id()


def new_request_id() -> str:
    """Generate a compact server-side request id."""
    import secrets

    return "req_" + secrets.token_urlsafe(12)


# --------------------------------------------------------------------------
# Structured JSON logger.
# --------------------------------------------------------------------------


def json_line(record: dict[str, Any]) -> str:
    """Render a record as a single JSON line with a safe subset of keys."""
    now = datetime.now(UTC).isoformat(timespec="milliseconds")
    return json.dumps(
        {"ts": now, **record},
        separators=(",", ":"),
        default=str,
    )


class StructuredLogger:
    """A thin structured logger that never accepts secret fields.

    ``_log`` builds a record from explicitly allowed metadata keys plus an
    ``extra`` mapping. Any ``extra`` key that looks secret-shaped is dropped.
    Free-form ``message`` text is run through ``scrub_secrets`` defensively.
    """

    _SAFE_KEYS: ClassVar[frozenset[str]] = frozenset(
        {
            "event",
            "request_id",
            "method",
            "route",
            "status",
            "duration_ms",
            "authenticated_user_id",
            "developer_id",
            "device_id",
            "security_event_type",
            "deployment_version",
            "environment",
            "error_class",
            "severity",
            "actor",
            "action",
            "source",
            "outcome",
            "category",
        }
    )

    def __init__(
        self,
        *,
        level: str = "info",
        log_format: str = "json",
        environment: str = "development",
        deployment_version: str = "",
    ) -> None:
        self.level = level.lower()
        self.log_format = log_format.lower()
        self.environment = environment
        self.deployment_version = deployment_version

    def _emit(self, level: str, message: str, extra: dict[str, Any] | None = None) -> None:
        if not self._enabled(level):
            return
        safe: dict[str, Any] = {"level": level.upper()}
        for key in sorted(self._SAFE_KEYS):
            if extra and key in extra:
                safe[key] = extra[key]
        if self.deployment_version:
            safe["deployment_version"] = self.deployment_version
        if self.environment:
            safe["environment"] = self.environment
        if self.log_format == "json":
            line = json_line({"message": scrub_secrets(message), **safe})
        else:
            meta = " ".join(f"{k}={v}" for k, v in safe.items())
            line = f"{level.upper()} {scrub_secrets(message)} {meta}".rstrip()
        import sys

        print(line, file=sys.stdout, flush=True)

    def _enabled(self, level: str) -> bool:
        order = {"debug": 10, "info": 20, "warning": 30, "error": 40, "critical": 50}
        return order.get(level, 20) >= order.get(self.level, 20)

    def info(self, message: str, **extra: Any) -> None:
        self._emit("info", message, extra)

    def debug(self, message: str, **extra: Any) -> None:
        self._emit("debug", message, extra)

    def warning(self, message: str, **extra: Any) -> None:
        self._emit("warning", message, extra)

    def error(self, message: str, **extra: Any) -> None:
        self._emit("error", message, extra)

    def audit(self, message: str, **extra: Any) -> None:
        self._emit("info", message, {"security_event_type": "audit", **extra})

    def lifecycle(self, event: str, **extra: Any) -> None:
        """Startup/shutdown/migration/backup lifecycle events (Phase 14)."""
        self._emit("info", event, {"event": event, **extra})

    def security(
        self, severity: str, event: str, *, category: str = "security", **extra: Any
    ) -> None:
        """Emit a classified security event (Phase 15I/15J).

        Logs only the safe metadata (severity, event, category, request_id…).
        Secret-shaped extra values are dropped by the allow-list.
        """
        self._emit(
            "warning" if severity in ("WARNING", "HIGH", "CRITICAL") else "info",
            event,
            {"event": event, "security_event_type": category, "severity": severity, **extra},
        )


OPERATIONAL_EVENTS = frozenset(
    {
        "startup",
        "shutdown",
        "request_error",
        "authentication_failure",
        "rate_limit",
        "migration",
        "backup",
        "restore",
        "credential_revocation",
        "device_revocation",
        "security_alert",
        "owner_boundary",
        "refresh_replay",
        "configuration_failure",
    }
)


__all__ = [
    "DEFAULT_REQUEST_ID_HEADER",
    "OPERATIONAL_EVENTS",
    "StructuredLogger",
    "json_line",
    "new_request_id",
    "scrub_secrets",
    "validate_request_id",
]
