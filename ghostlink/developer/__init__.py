"""Local Developer Account & Secure Credential Infrastructure (Phase 10A).

A GhostLink developer can establish a local developer identity and manage
cryptographically random API credentials that a future developer portal
(Phase 10B) can authenticate against — without ever exposing the local
secret.

This module is **local-only**. It makes zero network requests, uploads
nothing, and never stores the plaintext developer secret. See
docs/DEVELOPER_ACCOUNTS.md for the full architecture, threat model, and the
Phase 10B integration contract.
"""

from __future__ import annotations

from ghostlink.developer.account import (
    DEFAULT_COOLDOWN_SECONDS,
    DEFAULT_MAX_FAILED_ATTEMPTS,
    DEFAULT_WINDOW_SECONDS,
    MAX_ACTIVE_CREDENTIALS,
    DeveloperManager,
)
from ghostlink.developer.errors import (
    DeveloperAccountExistsError,
    DeveloperAccountNotFoundError,
    DeveloperCredentialInvalidError,
    DeveloperCredentialNotFoundError,
    DeveloperError,
    DeveloperMaxCredentialsError,
    DeveloperPermissionError,
    DeveloperRateLimitError,
    DeveloperStateError,
    DeveloperVerificationError,
)
from ghostlink.developer.keys import (
    CREDENTIAL_PREFIX,
    SECRET_BYTES,
    issue_credential,
    parse_credential,
    verify_secret,
)
from ghostlink.developer.models import DeveloperAccount, DeveloperCredential
from ghostlink.developer.storage import DeveloperStore

__all__ = [
    "CREDENTIAL_PREFIX",
    "DEFAULT_COOLDOWN_SECONDS",
    "DEFAULT_MAX_FAILED_ATTEMPTS",
    "DEFAULT_WINDOW_SECONDS",
    "MAX_ACTIVE_CREDENTIALS",
    "SECRET_BYTES",
    "DeveloperAccount",
    "DeveloperAccountExistsError",
    "DeveloperAccountNotFoundError",
    "DeveloperCredential",
    "DeveloperCredentialInvalidError",
    "DeveloperCredentialNotFoundError",
    "DeveloperError",
    "DeveloperManager",
    "DeveloperMaxCredentialsError",
    "DeveloperPermissionError",
    "DeveloperRateLimitError",
    "DeveloperStateError",
    "DeveloperStore",
    "DeveloperVerificationError",
    "issue_credential",
    "parse_credential",
    "verify_secret",
]
