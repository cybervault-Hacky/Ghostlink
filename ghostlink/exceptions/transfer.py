"""File-transfer failures (Phase 4: Secure File Transfer).

Operational errors surface in the chat UI as clear notices — never as
tracebacks, and never containing file contents or key material.
"""

from __future__ import annotations

from ghostlink.exceptions.base import ExitCode, GhostLinkError


class TransferError(GhostLinkError):
    """A file-transfer operation failed (offer, pump, receive, lifecycle)."""

    exit_code = ExitCode.NETWORK
    error_title = "File transfer error"


class TransferValidationError(TransferError):
    """A manifest, packet, or local file failed validation."""

    error_title = "Invalid transfer"


class TransferStateError(TransferError):
    """An invalid transfer state transition was attempted."""

    error_title = "Invalid transfer state"


class TransferLimitError(TransferError):
    """A size, count, or quota limit rejected the transfer."""

    error_title = "Transfer limit exceeded"
