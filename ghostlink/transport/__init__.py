"""Transport layer (Phase 2: Secure Networking).

A modular, transport-agnostic networking foundation:

* :mod:`ghostlink.transport.transport` — the abstract ``Transport`` contract
  every concrete transport implements (WebSocket today; relays can add more).
* :mod:`ghostlink.transport.websocket` — a dependency-free RFC 6455 client
  and server framing implementation over raw asyncio streams.
* :mod:`ghostlink.transport.connection` — the connection state machine with
  timeouts, reconnect backoff, and lifecycle statistics.
* :mod:`ghostlink.transport.heartbeat` — keepalive scheduling and RTT
  measurement.
* :mod:`ghostlink.transport.session` — session objects with expiration and a
  sweeping registry.
* :mod:`ghostlink.transport.relay` — the validated relay packet protocol, the
  real client, and a reference relay used for development and tests.
"""

from __future__ import annotations

from ghostlink.transport.connection import (
    ConnectionManager,
    ConnectionState,
)
from ghostlink.transport.session import Session, SessionRegistry, SessionStatus
from ghostlink.transport.transport import Transport, TransportState, TransportStats

__all__ = [
    "ConnectionManager",
    "ConnectionState",
    "Session",
    "SessionRegistry",
    "SessionStatus",
    "Transport",
    "TransportState",
    "TransportStats",
]
