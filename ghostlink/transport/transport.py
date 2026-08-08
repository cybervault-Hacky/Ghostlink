"""Abstract transport contract.

A transport is one live, framed byte channel to a remote peer: open it, send
and receive framed payloads, close it. Anything richer (handshakes, packets,
heartbeats, reconnects) is layered on top — transports stay deliberately
small so future backends (TLS sockets, QUIC, Tor) slot in behind the same
interface without touching relay code.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum


class TransportState(Enum):
    """Lifecycle states of a single transport instance (one-shot, no reuse)."""

    INITIALIZING = "initializing"
    OPEN = "open"
    CLOSING = "closing"
    CLOSED = "closed"
    FAILED = "failed"


@dataclass(slots=True)
class TransportStats:
    """Mutable counters maintained by a transport while it runs."""

    bytes_sent: int = 0
    bytes_received: int = 0
    messages_sent: int = 0
    messages_received: int = 0
    opened_at: datetime | None = None
    closed_at: datetime | None = None
    extra: dict[str, str] = field(default_factory=dict)

    @property
    def uptime_seconds(self) -> float:
        if self.opened_at is None:
            return 0.0
        end = self.closed_at if self.closed_at is not None else datetime.now(UTC)
        return max(0.0, (end - self.opened_at).total_seconds())


class Transport(ABC):
    """The contract every concrete transport implements."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short transport identifier, e.g. ``websocket``."""

    @property
    @abstractmethod
    def state(self) -> TransportState:
        """Current lifecycle state."""

    @property
    @abstractmethod
    def stats(self) -> TransportStats:
        """Live statistics counters."""

    @property
    @abstractmethod
    def remote_description(self) -> str:
        """Human-readable remote endpoint, used in logs and dashboards."""

    @abstractmethod
    async def open(self) -> None:
        """Establish the channel. Raises :class:`TransportError` on failure."""

    @abstractmethod
    async def close(self) -> None:
        """Close the channel gracefully. Idempotent."""

    @abstractmethod
    async def send(self, data: bytes) -> None:
        """Send one framed message."""

    @abstractmethod
    async def receive(self) -> bytes | None:
        """Wait for the next framed message.

        Returns the message bytes, or ``None`` when the remote peer closed
        the channel cleanly. Raises :class:`TransportError` on violations or
        abrupt loss.
        """
