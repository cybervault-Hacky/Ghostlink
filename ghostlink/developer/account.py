"""Developer-account lifecycle & credential management (Phase 10A).

This is the service layer: it owns the account state machine and all
credential operations. It is **local-only** — no network, no telemetry, no
upload. The raw developer secret is returned to the caller exactly once at
creation/rotation and is never stored or logged.

Bounded model (production-safe): an account may hold at most
``MAX_ACTIVE_CREDENTIALS`` active credentials. Rotation revokes the old key
and issues a new one, so the ceiling is never exceeded.

Local verification is rate-limited (in-memory, per process) so repeated
failed attempts are bounded and deterministic.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from ghostlink.core.logging import get_logger, register_secret
from ghostlink.developer import keys
from ghostlink.developer.errors import (
    DeveloperAccountExistsError,
    DeveloperAccountNotFoundError,
    DeveloperCredentialInvalidError,
    DeveloperCredentialNotFoundError,
    DeveloperMaxCredentialsError,
    DeveloperRateLimitError,
    DeveloperStateError,
    DeveloperVerificationError,
)
from ghostlink.developer.models import DeveloperAccount, DeveloperCredential
from ghostlink.developer.storage import DeveloperStore

MAX_ACTIVE_CREDENTIALS = 4

# Rate limiting (in-memory, per process; deterministic).
DEFAULT_MAX_FAILED_ATTEMPTS = 5
DEFAULT_WINDOW_SECONDS = 300.0
DEFAULT_COOLDOWN_SECONDS = 60.0

_logger = get_logger("developer")


class DeveloperManager:
    """Local developer account + credential manager."""

    def __init__(
        self,
        developer_dir: Path,
        *,
        store: DeveloperStore | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        max_failed_attempts: int = DEFAULT_MAX_FAILED_ATTEMPTS,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
    ) -> None:
        self._store = store if store is not None else DeveloperStore(developer_dir)
        self._lock = threading.RLock()  # serializes read-modify-write mutations
        self._monotonic = monotonic
        self._max_failed = max_failed_attempts
        self._window = window_seconds
        self._cooldown = cooldown_seconds
        self._failed: dict[str, list[float]] = {}  # key_id -> failure timestamps
        self._cooldown_until: dict[str, float] = {}

    # ------------------------------------------------------------- account

    def init_account(self) -> tuple[DeveloperAccount, str]:
        """Create the developer account and a first credential.

        Returns ``(account, credential)``. ``credential`` is the full
        ``gl_dev_…`` key shown exactly once.
        """
        with self._lock:
            return self._init_account_locked()

    def _init_account_locked(self) -> tuple[DeveloperAccount, str]:
        existing = self._load_account()
        if existing is not None:
            raise DeveloperAccountExistsError(
                "A developer account already exists.",
                hint="Use 'ghostlink developer key create' to add another "
                "credential, or 'key rotate' to replace one.",
            )
        account = DeveloperAccount(
            developer_id=keys.generate_developer_id(),
            created_at=datetime.now(UTC),
            status="active",
        )
        key_id, credential = keys.issue_credential()
        account.credentials[key_id] = self._make_credential(key_id, credential)
        self._save(account)
        register_secret(credential)  # Phase 8 log-redaction backstop
        _logger.info(
            "developer_account_created dev=%s key=%s",
            account.developer_id,
            key_id,
        )
        return account, credential

    def require_account(self) -> DeveloperAccount:
        account = self._load_account()
        if account is None:
            raise DeveloperAccountNotFoundError(
                "No developer account exists yet.",
                hint="Run 'ghostlink developer init' to create one.",
            )
        return account

    def status(self) -> DeveloperAccount | None:
        return self._load_account()

    # ---------------------------------------------------------- credentials

    def create_credential(self) -> tuple[DeveloperAccount, str]:
        """Add a new active credential (max ``MAX_ACTIVE_CREDENTIALS``)."""
        with self._lock:
            return self._create_credential_locked()

    def _create_credential_locked(self) -> tuple[DeveloperAccount, str]:
        account = self.require_account()
        active = [c for c in account.credentials.values() if c.is_active]
        if len(active) >= MAX_ACTIVE_CREDENTIALS:
            raise DeveloperMaxCredentialsError(
                f"This account already holds the maximum {MAX_ACTIVE_CREDENTIALS} "
                "active credentials.",
                hint="Revoke one with 'key revoke <key-id>' or use 'key rotate'.",
            )
        key_id, credential = keys.issue_credential()
        account.credentials[key_id] = self._make_credential(key_id, credential)
        self._save(account)
        register_secret(credential)
        _logger.info("developer_key_created dev=%s key=%s", account.developer_id, key_id)
        return account, credential

    def rotate_credential(self) -> tuple[DeveloperAccount, str]:
        """Revoke every active credential and issue one fresh one.

        The new credential always has a distinct key_id and secret; the old
        secret is never shown again. Persisted atomically.
        """
        with self._lock:
            return self._rotate_credential_locked()

    def _rotate_credential_locked(self) -> tuple[DeveloperAccount, str]:
        account = self.require_account()
        new_key_id, credential = keys.issue_credential()
        new_cred = self._make_credential(new_key_id, credential)
        # Revoke all previously active credentials.
        for kid, existing in account.credentials.items():
            if existing.is_active:
                account.credentials[kid] = DeveloperCredential(
                    key_id=kid,
                    status="revoked",
                    created_at=existing.created_at,
                    verifier=existing.verifier,
                    salt_b64=existing.salt_b64,
                    hash_b64=existing.hash_b64,
                    last_used_at=existing.last_used_at,
                )
                _logger.info("developer_key_revoked dev=%s key=%s", account.developer_id, kid)
        account.credentials[new_key_id] = new_cred
        self._save(account)
        register_secret(credential)
        _logger.info("developer_key_rotated dev=%s key=%s", account.developer_id, new_key_id)
        return account, credential

    def revoke_credential(self, key_id: str) -> DeveloperAccount:
        """Revoke one credential by key_id (irreversible until reissued)."""
        with self._lock:
            return self._revoke_credential_locked(key_id)

    def _revoke_credential_locked(self, key_id: str) -> DeveloperAccount:
        account = self.require_account()
        cleaned = key_id.strip()
        if cleaned not in account.credentials:
            raise DeveloperCredentialNotFoundError(
                f"No credential with key id '{cleaned}' exists.",
                hint="Use 'ghostlink developer key list' to see key ids.",
            )
        existing = account.credentials[cleaned]
        account.credentials[cleaned] = DeveloperCredential(
            key_id=cleaned,
            status="revoked",
            created_at=existing.created_at,
            verifier=existing.verifier,
            salt_b64=existing.salt_b64,
            hash_b64=existing.hash_b64,
            last_used_at=existing.last_used_at,
        )
        self._save(account)
        _logger.info("developer_key_revoked dev=%s key=%s", account.developer_id, cleaned)
        return account

    def list_credentials(self) -> list[DeveloperCredential]:
        account = self.require_account()
        return sorted(account.credentials.values(), key=lambda c: c.created_at)

    # ---------------------------------------------------------- verification

    def verify_credential(self, credential: str) -> str:
        """Verify a supplied credential; returns the matching key_id on success.

        Raises ``DeveloperCredentialInvalidError`` (malformed), a rate-limit
        error, or ``DeveloperVerificationError`` (mismatch/revoked/unknown).
        """
        with self._lock:
            return self._verify_credential_locked(credential)

    def _verify_credential_locked(self, credential: str) -> str:
        key_id, secret = keys.parse_credential(credential)
        self._check_rate_limit(key_id)
        account = self._load_account()
        if account is None:
            raise DeveloperVerificationError(
                "No developer account exists — cannot verify a key.",
                hint="Run 'ghostlink developer init' first.",
            )
        entry = account.credentials.get(key_id)
        if entry is None or not entry.is_active:
            self._record_failure(key_id)
            _logger.info(
                "developer_key_verification_failed key=%s reason=revoked_or_unknown", key_id
            )
            raise DeveloperVerificationError(
                "The developer key does not match an active credential.",
                hint="The key may be revoked or mistyped.",
            )
        try:
            keys.verify_secret(secret, entry.verifier, entry.salt_b64, entry.hash_b64)
        except DeveloperVerificationError:
            self._record_failure(key_id)
            _logger.info("developer_key_verification_failed key=%s reason=mismatch", key_id)
            raise
        except DeveloperCredentialInvalidError:
            self._record_failure(key_id)
            raise
        # Success: clear failures and refresh last_used metadata.
        self._failed.pop(key_id, None)
        self._cooldown_until.pop(key_id, None)
        account.credentials[key_id] = DeveloperCredential(
            key_id=entry.key_id,
            status=entry.status,
            created_at=entry.created_at,
            verifier=entry.verifier,
            salt_b64=entry.salt_b64,
            hash_b64=entry.hash_b64,
            last_used_at=datetime.now(UTC),
        )
        self._save(account)
        return key_id

    # ------------------------------------------------------------ internals

    def _make_credential(self, key_id: str, credential: str) -> DeveloperCredential:
        _key_id, secret = keys.parse_credential(credential)
        assert _key_id == key_id
        verifier, salt, digest = keys.make_verifier(secret)
        return DeveloperCredential(
            key_id=key_id,
            status="active",
            created_at=datetime.now(UTC),
            verifier=verifier,
            salt_b64=salt,
            hash_b64=digest,
        )

    def _load_account(self) -> DeveloperAccount | None:
        try:
            account = self._store.load_account()
            if account is None:
                return None
            account.credentials = self._store.load_credentials()
            return account
        except DeveloperStateError:
            raise
        except Exception as exc:
            raise DeveloperStateError(
                "The developer account could not be read.",
                hint="Check <data-dir>/developer permissions and integrity.",
            ) from exc

    def _save(self, account: DeveloperAccount) -> None:
        # Load current credentials (preserves any not held in memory).
        try:
            stored_credentials = self._store.load_credentials()
        except DeveloperStateError:
            stored_credentials = {}
        merged = {**stored_credentials, **account.credentials}
        self._store.save_account(account)
        self._store.save_credentials(merged)

    # ---------------------------------------------------------- rate limiting

    def _check_rate_limit(self, key_id: str) -> None:
        now = self._monotonic()
        cooldown = self._cooldown_until.get(key_id, 0.0)
        if now < cooldown:
            raise DeveloperRateLimitError(
                "Too many failed verification attempts — locked out temporarily.",
                hint="Wait and try again, or reset the developer account.",
            )
        failures = [t for t in self._failed.get(key_id, []) if now - t < self._window]
        if len(failures) >= self._max_failed:
            self._cooldown_until[key_id] = now + self._cooldown
            self._failed[key_id] = []
            raise DeveloperRateLimitError(
                "Too many failed verification attempts — locked out temporarily.",
                hint="Wait and try again, or reset the developer account.",
            )
        self._failed[key_id] = failures

    def _record_failure(self, key_id: str) -> None:
        now = self._monotonic()
        failures = self._failed.get(key_id, [])
        failures.append(now)
        self._failed[key_id] = failures


__all__ = [
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_MAX_FAILED_ATTEMPTS",
    "DEFAULT_WINDOW_SECONDS",
    "MAX_ACTIVE_CREDENTIALS",
    "DeveloperManager",
]
