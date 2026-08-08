"""WebSocket-backed implementation of the :class:`Transport` contract."""

from __future__ import annotations

import asyncio
import ssl
from datetime import UTC, datetime

from ghostlink.exceptions.transport import ConnectionTimeoutError, TransportError
from ghostlink.transport.transport import Transport, TransportState, TransportStats
from ghostlink.transport.websocket.protocol import (
    ConnectionClosed,
    WebSocketConnection,
    perform_client_handshake,
)


class WebSocketTransport(Transport):
    """A :class:`Transport` over one WebSocket connection.

    Client instances are created with :meth:`dial`; server-side instances
    with :meth:`accept` around an already-upgraded stream pair.
    """

    def __init__(
        self,
        connection: WebSocketConnection | None,
        *,
        remote: str,
        pending: tuple[str, int, str, float, ssl.SSLContext | None] | None = None,
    ) -> None:
        self._connection = connection
        self._remote = remote
        self._pending = pending  # (host, port, resource, timeout, ssl_context) for client
        self._state = TransportState.INITIALIZING
        self._stats = TransportStats()

    # ------------------------------------------------------------- constructors

    @classmethod
    def dial(
        cls,
        host: str,
        port: int,
        *,
        resource: str,
        secure: bool,
        timeout_seconds: float,
    ) -> WebSocketTransport:
        """Create an unopened client transport for ``ws(s)://host:port/resource``."""

        secure_label = "wss" if secure else "ws"
        ssl_context = ssl.create_default_context() if secure else None
        return cls(
            None,
            remote=f"{secure_label}://{host}:{port}{resource}",
            pending=(host, port, resource, timeout_seconds, ssl_context),
        )

    @classmethod
    def accept(
        cls,
        connection: WebSocketConnection,
        *,
        remote: str,
    ) -> WebSocketTransport:
        """Wrap a server-side connection that completed the WS handshake."""

        transport = cls(connection, remote=remote)
        transport._state = TransportState.OPEN
        transport._stats.opened_at = datetime.now(UTC)
        return transport

    # ------------------------------------------------------------- properties

    @property
    def name(self) -> str:
        return "websocket"

    @property
    def state(self) -> TransportState:
        return self._state

    @property
    def stats(self) -> TransportStats:
        return self._stats

    @property
    def remote_description(self) -> str:
        return self._remote

    @property
    def connection(self) -> WebSocketConnection:
        if self._connection is None:
            raise TransportError(
                "WebSocket transport is not open yet.",
                hint="Call open() (client) or build via WebSocketTransport.accept().",
            )
        return self._connection

    # ------------------------------------------------------------- lifecycle

    async def open(self) -> None:
        """Dial TCP, upgrade to WebSocket, and mark the transport open."""

        if self._pending is None:
            raise TransportError(
                "This transport was not created as a client.",
                hint="Server-side transports are already open after accept().",
            )
        host, port, resource, timeout, ssl_context = self._pending
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port, ssl=ssl_context),
                timeout=timeout,
            )
        except TimeoutError as exc:
            self._state = TransportState.FAILED
            raise ConnectionTimeoutError(
                f"TCP connect to {host}:{port} timed out after {timeout:.1f}s.",
                hint="Check that the relay is online and reachable from this network.",
            ) from exc
        except OSError as exc:
            self._state = TransportState.FAILED
            raise TransportError(
                f"Could not reach {host}:{port}: {exc.strerror or exc}.",
                hint="Check that the relay is online and reachable from this network.",
            ) from exc
        try:
            await perform_client_handshake(
                reader,
                writer,
                host=host,
                port=port,
                resource=resource,
                timeout_seconds=timeout,
            )
        except Exception:
            writer.close()
            self._state = TransportState.FAILED
            raise
        self._connection = WebSocketConnection(
            reader, writer, mask_outgoing=True, expect_masked=False
        )
        self._state = TransportState.OPEN
        self._stats.opened_at = datetime.now(UTC)

    async def close(self) -> None:
        if self._state in (TransportState.CLOSING, TransportState.CLOSED):
            return
        self._state = TransportState.CLOSING
        if self._connection is not None:
            await self._connection.close()
        self._state = TransportState.CLOSED
        self._stats.closed_at = datetime.now(UTC)

    # ------------------------------------------------------------------- I/O

    async def send(self, data: bytes) -> None:
        connection = self.connection
        await connection.send_bytes(data)
        self._stats.messages_sent += 1
        self._stats.bytes_sent += len(data)

    async def send_text(self, message: str) -> None:
        """Text-frame variant used by the relay client (JSON payloads)."""

        connection = self.connection
        await connection.send_text(message)
        self._stats.messages_sent += 1
        self._stats.bytes_sent += len(message.encode("utf-8"))

    async def receive(self) -> bytes | None:
        connection = self.connection
        try:
            message = await connection.read_message()
        except ConnectionClosed:
            self._state = TransportState.CLOSED
            self._stats.closed_at = datetime.now(UTC)
            raise
        if message is None:
            self._state = TransportState.CLOSED
            self._stats.closed_at = datetime.now(UTC)
            return None
        data = message.data.encode("utf-8") if isinstance(message.data, str) else message.data
        self._stats.messages_received += 1
        self._stats.bytes_received += len(data)
        return data
