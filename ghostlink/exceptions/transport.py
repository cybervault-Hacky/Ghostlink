"""Transport and relay failures (Phase 2: Secure Networking)."""

from __future__ import annotations

from ghostlink.exceptions.base import ExitCode, GhostLinkError


class TransportError(GhostLinkError):
    """A transport could not connect, send, receive, or shut down cleanly."""

    exit_code = ExitCode.NETWORK
    error_title = "Transport error"


class ConnectionTimeoutError(TransportError):
    """A connect or handshake did not complete within its deadline."""

    error_title = "Connection timed out"


class HandshakeError(TransportError):
    """The relay handshake was rejected or malformed."""

    error_title = "Handshake failed"


class ProtocolError(TransportError):
    """The remote peer violated the wire protocol."""

    error_title = "Protocol violation"


class PacketValidationError(ProtocolError):
    """A packet failed schema validation, inbound or outbound."""

    error_title = "Invalid packet"


class RelayError(TransportError):
    """The relay reported an error or became unreachable."""

    error_title = "Relay error"
