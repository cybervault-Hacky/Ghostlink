"""Security-event severity classification (Phase 15J).

An internal, deterministic severity model for operational security events.
Severity levels: INFO, NOTICE, WARNING, HIGH, CRITICAL. Classification is
stateless and metadata-only — it never logs or stores sensitive payloads and
does not introduce behavioural tracking.

Each classifier returns (severity, safe_detail). The mapping is used by the
ops/alerting layer to decide which events to escalate.
"""

from __future__ import annotations

from typing import Any

SEVERITY_ORDER = {"INFO": 0, "NOTICE": 1, "WARNING": 2, "HIGH": 3, "CRITICAL": 4}


class SecurityAlert(Exception):
    """A classified security alert (never carries secret material)."""

    def __init__(self, severity: str, category: str, detail: str = "") -> None:
        super().__init__(detail)
        self.severity = severity
        self.category = category
        self.detail = detail


def classify(event: str, *, count: int = 0, **ctx: Any) -> tuple[str, str]:
    """Return (severity, safe_detail) for a security/operational event."""
    if event == "authentication_failure":
        if count >= 10:
            return "HIGH", "repeated authentication failures"
        if count >= 5:
            return "NOTICE", "authentication failures"
        return "INFO", "authentication failure"
    if event == "rate_limit":
        if count >= 100:
            return "WARNING", "rate-limit exhaustion"
        return "NOTICE", "rate-limit hit"
    if event == "refresh_replay":
        return "WARNING", "invalid refresh-token replay detected"
    if event == "credential_revocation":
        return "NOTICE", "credential revoked"
    if event == "device_revocation":
        return "NOTICE", "device revoked"
    if event == "suspicious_scope":
        return "WARNING", "suspicious scope request"
    if event == "malformed_auth":
        return "NOTICE", "malformed authentication attempt"
    if event == "migration_failure":
        return "CRITICAL", "migration integrity failure"
    if event == "backup_failure":
        return "HIGH", "backup integrity failure"
    if event == "configuration_failure":
        return "CRITICAL", "configuration failure (fail closed)"
    if event == "request_error":
        if count >= 100:
            return "WARNING", "repeated 5xx failures"
        return "INFO", "request error"
    if event == "owner_boundary":
        return "CRITICAL", "owner-boundary violation attempted"
    if event in ("startup", "shutdown", "migration", "backup", "restore"):
        return "INFO", event
    return "INFO", event


def requires_escalation(severity: str, *, threshold: str = "WARNING") -> bool:
    """True if a severity is at or above the escalation threshold."""
    return SEVERITY_ORDER.get(severity, 0) >= SEVERITY_ORDER.get(threshold, 2)


__all__ = ["SEVERITY_ORDER", "SecurityAlert", "classify", "requires_escalation"]
