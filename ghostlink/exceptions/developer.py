"""Developer-account failures (Phase 10A).

A dedicated exception family keeps developer-credential problems
distinguishable from messaging, transport, config and storage errors, and
gives them a stable process exit code. Messages and hints are metadata-only:
no developer key, no secret, no verification hash ever appears here.
"""

from __future__ import annotations

from typing import ClassVar

from ghostlink.exceptions.base import ExitCode, GhostLinkError


class DeveloperError(GhostLinkError):
    """Base class for every developer-account failure."""

    exit_code: ClassVar[ExitCode] = ExitCode.CONFIGURATION
    error_title: ClassVar[str] = "Developer account error"


class DeveloperAccountNotFoundError(DeveloperError):
    """No developer account exists yet (run ``ghostlink developer init``)."""

    error_title: ClassVar[str] = "No developer account"


class DeveloperAccountExistsError(DeveloperError):
    """A developer account already exists; init is refused (idempotent-guard)."""

    error_title: ClassVar[str] = "Developer account exists"


class DeveloperCredentialNotFoundError(DeveloperError):
    """No credential with the given key_id exists (or it is unknown)."""

    error_title: ClassVar[str] = "Credential not found"


class DeveloperCredentialInvalidError(DeveloperError):
    """A supplied developer key string is malformed or unparseable."""

    error_title: ClassVar[str] = "Invalid developer key"


class DeveloperVerificationError(DeveloperError):
    """A supplied developer key did not verify against any active credential."""

    error_title: ClassVar[str] = "Verification failed"


class DeveloperRateLimitError(DeveloperError):
    """Too many failed local verification attempts; retry later."""

    error_title: ClassVar[str] = "Verification rate limited"


class DeveloperStateError(DeveloperError):
    """The local developer-account state is corrupt or unreadable."""

    error_title: ClassVar[str] = "Developer state error"


class DeveloperPermissionError(DeveloperError):
    """The developer storage directory or credential file has insecure
    permissions (world/group-writable, owned by another user, or a symlink)."""

    error_title: ClassVar[str] = "Developer storage permissions"


class DeveloperMaxCredentialsError(DeveloperError):
    """The active-credential ceiling was reached; revoke one or rotate."""

    error_title: ClassVar[str] = "Credential limit reached"
