"""Bounded, idempotent data-retention cleanup (Phase 13O).

Cleanup never deletes *active* security state: it only removes expired tokens,
revoked/expired sessions and pairing codes, old security/API activity, stale
WebAuthn challenges and expired verification tokens. All deletes are bounded
by an age window from configuration and are safe to re-run.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from portal_server.config import PortalConfig
from portal_server.db.base import DatabaseBackend


@dataclass(frozen=True, slots=True)
class RetentionPolicy:
    sessions_days: int
    expired_tokens_days: int
    security_events_days: int
    api_activity_days: int
    pairing_days: int
    webauthn_challenges_days: int
    verification_tokens_days: int


def policy_from_config(config: PortalConfig) -> RetentionPolicy:
    return RetentionPolicy(
        sessions_days=config.retention_sessions_days,
        expired_tokens_days=config.retention_expired_tokens_days,
        security_events_days=config.retention_security_events_days,
        api_activity_days=config.retention_api_activity_days,
        pairing_days=config.retention_pairing_days,
        webauthn_challenges_days=config.retention_webauthn_challenges_days,
        verification_tokens_days=config.retention_verification_tokens_days,
    )


def _cutoff(days: int) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")


def run_retention(db: DatabaseBackend, policy: RetentionPolicy) -> dict[str, int]:
    """Delete expired/old records per policy; returns a summary of deletions.

    Each statement is a single DELETE bounded by an age cutoff. A cutoff of 0
    days disables that cleanup category (the caller supplies days >= 0).
    """
    counts: dict[str, int] = {}
    with db.transaction():
        if policy.expired_tokens_days > 0:
            counts["tokens_expired"] = _delete(
                db,
                "DELETE FROM tokens WHERE expires_at < ?",
                _cutoff(policy.expired_tokens_days),
            )
        if policy.sessions_days > 0:
            counts["sessions_revoked_old"] = _delete(
                db,
                "DELETE FROM sessions WHERE revoked_at IS NOT NULL AND created_at < ?",
                _cutoff(policy.sessions_days),
            )
        if policy.security_events_days > 0:
            counts["security_events_old"] = _delete(
                db,
                "DELETE FROM security_events WHERE created_at < ?",
                _cutoff(policy.security_events_days),
            )
        if policy.api_activity_days > 0:
            counts["api_activity_old"] = _delete(
                db,
                "DELETE FROM api_activity WHERE created_at < ?",
                _cutoff(policy.api_activity_days),
            )
        if policy.pairing_days > 0:
            counts["pairing_old"] = _delete(
                db,
                "DELETE FROM pairing_codes WHERE status <> 'pending' AND created_at < ?",
                _cutoff(policy.pairing_days),
            )
        if policy.webauthn_challenges_days > 0:
            # WebAuthn challenges are in-memory in this build; nothing to purge
            # in the DB. Recorded for API symmetry.
            counts["webauthn_challenges_old"] = 0
        if policy.verification_tokens_days > 0:
            counts["verification_tokens_old"] = _delete(
                db,
                "DELETE FROM tokens WHERE expires_at < ? "
                "AND kind IN ('email_verify', 'password_reset')",
                _cutoff(policy.verification_tokens_days),
            )
    return counts


def _delete(db: DatabaseBackend, sql: str, cutoff: str) -> int:
    from portal_server.db.base import _translate_placeholders

    if db.backend_name == "postgresql":
        sql = _translate_placeholders(sql)
        row = db.query_one(sql + " RETURNING COUNT(*)", (cutoff,))
        return int(row["count"]) if row else 0
    # SQLite: count then delete in the same transaction.
    count = db.query_one("SELECT COUNT(*) AS n " + sql[sql.index("FROM") :], (cutoff,))
    db.execute(sql, (cutoff,))
    return int(count["n"]) if count else 0


__all__ = ["RetentionPolicy", "policy_from_config", "run_retention"]
