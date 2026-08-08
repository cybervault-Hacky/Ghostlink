"""GhostLink exception framework.

Every recoverable failure raised by GhostLink derives from
:class:`GhostLinkError`, which carries a human-readable message, an
actionable hint, and a deterministic process exit code. The CLI entrypoint
hands any escaped exception to :func:`render_exception`, producing a
consistent, well-formatted error panel instead of a raw traceback.
"""

from __future__ import annotations

from ghostlink.exceptions.base import ExitCode, GhostLinkError
from ghostlink.exceptions.config import (
    ConfigParseError,
    ConfigurationError,
    ConfigValidationError,
)
from ghostlink.exceptions.environment import (
    UnsupportedPlatformError,
    UnsupportedPythonVersionError,
)
from ghostlink.exceptions.handler import render_exception
from ghostlink.exceptions.invites import (
    InviteAlreadyUsedError,
    InviteError,
    InviteExpiredError,
    InvitePermissionError,
    InviteRevokedError,
    InviteStateError,
    InviteUnknownError,
    InviteValidationError,
)
from ghostlink.exceptions.messaging import (
    DecryptionError,
    HandshakeFailedError,
    HistoryError,
    HistoryPassphraseError,
    MessageValidationError,
    MessagingError,
    PeerUnavailableError,
    SecureChannelError,
)
from ghostlink.exceptions.services import ServiceNotRegisteredError
from ghostlink.exceptions.storage import (
    StorageCorruptionError,
    StorageError,
    StorageReadError,
    StorageWriteError,
)
from ghostlink.exceptions.themes import ThemeNotFoundError
from ghostlink.exceptions.transfer import (
    TransferError,
    TransferLimitError,
    TransferStateError,
    TransferValidationError,
)
from ghostlink.exceptions.transport import (
    ConnectionTimeoutError,
    HandshakeError,
    PacketValidationError,
    ProtocolError,
    RelayError,
    TransportError,
)

__all__ = [
    "ConfigParseError",
    "ConfigValidationError",
    "ConfigurationError",
    "ConnectionTimeoutError",
    "DecryptionError",
    "ExitCode",
    "GhostLinkError",
    "HandshakeError",
    "HandshakeFailedError",
    "HistoryError",
    "HistoryPassphraseError",
    "InviteAlreadyUsedError",
    "InviteError",
    "InviteExpiredError",
    "InvitePermissionError",
    "InviteRevokedError",
    "InviteStateError",
    "InviteUnknownError",
    "InviteValidationError",
    "MessageValidationError",
    "MessagingError",
    "PacketValidationError",
    "PeerUnavailableError",
    "ProtocolError",
    "RelayError",
    "SecureChannelError",
    "ServiceNotRegisteredError",
    "StorageCorruptionError",
    "StorageError",
    "StorageReadError",
    "StorageWriteError",
    "ThemeNotFoundError",
    "TransferError",
    "TransferLimitError",
    "TransferStateError",
    "TransferValidationError",
    "TransportError",
    "UnsupportedPlatformError",
    "UnsupportedPythonVersionError",
    "render_exception",
]
