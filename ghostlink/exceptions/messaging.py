"""Messaging, encryption, and history failures (Phase 3: Secure Messaging).

These errors are first-class: the chat layer turns expected failures into
clear, actionable terminal output, never a traceback. Plaintext message
content is never embedded in any error message.
"""

from __future__ import annotations

from ghostlink.exceptions.base import ExitCode, GhostLinkError


class MessagingError(GhostLinkError):
    """A messaging operation failed (queue, protocol, lifecycle)."""

    exit_code = ExitCode.NETWORK
    error_title = "Messaging error"


class SecureChannelError(MessagingError):
    """The end-to-end encrypted channel could not be established or used."""

    error_title = "Secure channel error"


class HandshakeFailedError(SecureChannelError):
    """Key exchange verification failed — keys must not be used."""

    error_title = "Secure handshake failed"


class DecryptionError(SecureChannelError):
    """A frame failed AEAD verification — tampering or key mismatch."""

    error_title = "Integrity check failed"


class MessageValidationError(MessagingError):
    """A message or secure-channel frame failed schema validation."""

    error_title = "Invalid message"


class PeerUnavailableError(MessagingError):
    """The conversation has no reachable peer to route to."""

    error_title = "Peer unavailable"


class HistoryError(GhostLinkError):
    """Message history could not be read, written, or decrypted."""

    exit_code = ExitCode.STORAGE
    error_title = "History error"


class HistoryPassphraseError(HistoryError):
    """The stored history could not be unlocked with the given passphrase."""

    error_title = "History locked"
