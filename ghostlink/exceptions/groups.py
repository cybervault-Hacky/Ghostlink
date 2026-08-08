"""Group lifecycle failures (Phase 6B).

Group operations can fail for user-facing reasons (unknown group, not the
owner, roster full, stale epoch, …) that deserve a dedicated process exit
code so scripts and shells can tell "the group operation was refused" apart
from generic errors. Messages and hints are metadata-only: no tokens, keys,
or other secret material ever appears in these exceptions.
"""

from __future__ import annotations

from typing import ClassVar

from ghostlink.exceptions.base import ExitCode, GhostLinkError


class GroupError(GhostLinkError):
    """Base class for every group lifecycle failure."""

    exit_code: ClassVar[ExitCode] = ExitCode.GROUP
    error_title: ClassVar[str] = "Group error"


class GroupValidationError(GroupError):
    """A group id, name, fingerprint, or payload failed local validation."""

    error_title: ClassVar[str] = "Invalid group input"


class GroupUnknownError(GroupError):
    """The relay (or local store) has no record of that group."""

    error_title: ClassVar[str] = "Group not found"


class GroupFullError(GroupError):
    """The roster (plus pending joins) already holds 8 members."""

    error_title: ClassVar[str] = "Group is full"


class GroupPermissionError(GroupError):
    """Only the owner may perform that operation."""

    error_title: ClassVar[str] = "Group permission denied"


class GroupNotMemberError(GroupError):
    """The identity is (no longer) a member of the group."""

    error_title: ClassVar[str] = "Not a group member"


class GroupDissolvedError(GroupError):
    """The group was dissolved by its owner; the record is terminal."""

    error_title: ClassVar[str] = "Group dissolved"


class GroupStateError(GroupError):
    """An invalid group/membership state transition was attempted."""

    error_title: ClassVar[str] = "Group state error"


class GroupEpochError(GroupStateError):
    """Stale, duplicate, gapped, or rolled-back epoch was supplied."""

    error_title: ClassVar[str] = "Group epoch conflict"


class GroupConflictError(GroupError):
    """A concurrent membership operation is already pending for the group."""

    error_title: ClassVar[str] = "Group operation conflict"


class GroupJoinTimeoutError(GroupError):
    """The owner did not countersign the admission before the deadline."""

    error_title: ClassVar[str] = "Group join timed out"


class GroupDefunctError(GroupError):
    """The relay lost the group record (e.g. relay restart)."""

    error_title: ClassVar[str] = "Group no longer hosted"


class GroupMessageError(GroupError):
    """A group message failed validation, encryption, or delivery."""

    error_title: ClassVar[str] = "Group message error"


class GroupOfflineError(GroupMessageError):
    """The addressed member is not currently reachable."""

    error_title: ClassVar[str] = "Group member offline"


class GroupRateLimitError(GroupError):
    """The relay rate limiter refused the operation (fail loud)."""

    error_title: ClassVar[str] = "Group rate limited"
