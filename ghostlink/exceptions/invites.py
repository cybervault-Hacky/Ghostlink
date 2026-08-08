"""Invitation failures (Phase 5).

Invite operations can fail for user-facing reasons (expired, already used,
revoked, malformed) that deserve a dedicated process exit code so scripts
and shells can tell "the invite was refused" apart from generic errors.
"""

from __future__ import annotations

from typing import ClassVar

from ghostlink.exceptions.base import ExitCode, GhostLinkError


class InviteError(GhostLinkError):
    """Base class for every invite lifecycle failure."""

    exit_code: ClassVar[ExitCode] = ExitCode.INVITE
    error_title: ClassVar[str] = "Invite error"


class InviteValidationError(InviteError):
    """A link or token failed format validation before any network use."""

    error_title: ClassVar[str] = "Invalid invite"


class InviteStateError(InviteError):
    """An invalid invite state transition was attempted."""

    error_title: ClassVar[str] = "Invite state error"


class InviteUnknownError(InviteError):
    """The relay has no record of that invite."""

    error_title: ClassVar[str] = "Invite not found"


class InviteExpiredError(InviteError):
    """The invite passed its deadline; redemption is refused."""

    error_title: ClassVar[str] = "Invite expired"


class InviteAlreadyUsedError(InviteError):
    """The one-time invite has already been redeemed."""

    error_title: ClassVar[str] = "Invite already used"


class InviteRevokedError(InviteError):
    """The invite was explicitly revoked by its creator."""

    error_title: ClassVar[str] = "Invite revoked"


class InvitePermissionError(InviteError):
    """Only the inviting session may revoke or fully inspect an invite."""

    error_title: ClassVar[str] = "Invite permission denied"
