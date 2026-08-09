"""Developer-account error types (Phase 10A).

The canonical definitions live in ``ghostlink.exceptions.developer`` so they
participate in the shared exception/exit-code framework; this module
re-exports them so the developer package has its own ``errors`` namespace.
"""

from __future__ import annotations

from ghostlink.exceptions.developer import (
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

__all__ = [
    "DeveloperAccountExistsError",
    "DeveloperAccountNotFoundError",
    "DeveloperCredentialInvalidError",
    "DeveloperCredentialNotFoundError",
    "DeveloperError",
    "DeveloperMaxCredentialsError",
    "DeveloperPermissionError",
    "DeveloperRateLimitError",
    "DeveloperStateError",
    "DeveloperVerificationError",
]
