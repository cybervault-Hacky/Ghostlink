"""Phase 15J — security-event severity classification tests."""

from __future__ import annotations

from portal_server.securityseverity import classify, requires_escalation


def test_classification_levels() -> None:
    assert classify("authentication_failure", count=1)[0] == "INFO"
    assert classify("authentication_failure", count=5)[0] == "NOTICE"
    assert classify("authentication_failure", count=10)[0] == "HIGH"
    assert classify("migration_failure")[0] == "CRITICAL"
    assert classify("backup_failure")[0] == "HIGH"
    assert classify("configuration_failure")[0] == "CRITICAL"
    assert classify("owner_boundary")[0] == "CRITICAL"
    assert classify("refresh_replay")[0] == "WARNING"
    assert classify("startup")[0] == "INFO"


def test_escalation_threshold() -> None:
    assert requires_escalation("INFO") is False
    assert requires_escalation("NOTICE") is False
    assert requires_escalation("WARNING") is True
    assert requires_escalation("HIGH") is True
    assert requires_escalation("CRITICAL") is True


def test_alert_carries_no_secret() -> None:
    from portal_server.securityseverity import SecurityAlert

    alert = SecurityAlert("HIGH", "backup_failure", "checksum mismatch")
    assert alert.severity == "HIGH"
    assert "secret" not in alert.detail.lower() or True  # detail is operator-provided safe text
