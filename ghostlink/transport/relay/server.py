"""Reference GhostLink relay.

A real, minimal relay used for local development and the test suite. It
accepts WebSocket clients, enforces the HELLO/WELCOME handshake, answers
PING with PONG, acknowledges HEARTBEAT, tracks one session per client with
inactivity expiry, and closes politely with DISCONNECT.

Run it locally:

    python -m ghostlink.transport.relay.server --host 127.0.0.1 --port 8787
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from contextlib import suppress
from dataclasses import dataclass, field
from typing import cast

from ghostlink.constants.net import (
    CHANNEL_CAPACITY,
    DEFAULT_RELAY_PORT,
    HEARTBEAT_INTERVAL_SECONDS,
    RELAY_SESSION_TTL_SECONDS,
    SERVER_NAME,
    SESSION_SWEEP_INTERVAL_SECONDS,
)
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.transport import (
    ConnectionTimeoutError,
    HandshakeError,
    PacketValidationError,
    TransportError,
)
from ghostlink.transport.relay.protocol import (
    Packet,
    PacketType,
    attached_packet,
    decode_packet,
    disconnect_packet,
    encode_packet,
    error_packet,
    forward_packet,
    heartbeat_packet,
    peer_packet,
    pong_packet,
    welcome_packet,
)
from ghostlink.transport.session import Session, SessionRegistry
from ghostlink.transport.websocket.protocol import (
    ConnectionClosed,
    WebSocketConnection,
    perform_server_handshake,
)

SERVER_PROTOCOL_VERSION = 2
HELLO_TIMEOUT_SECONDS = 5.0
HANDSHAKE_TIMEOUT_SECONDS = 5.0


@dataclass(slots=True)
class _ClientContext:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    remote: str
    connection: WebSocketConnection | None = None
    session: Session | None = None
    handler_task: asyncio.Task[None] | None = field(default=None, repr=False)
    # channel -> role this connection is attached as (rendezvous routing)
    attachments: dict[str, str] = field(default_factory=dict)

    @property
    def conn(self) -> WebSocketConnection:
        if self.connection is None:
            raise TransportError("Client channel used before the upgrade completed.")
        return self.connection


@dataclass(slots=True)
class _Channel:
    """One rendezvous: at most two attached connections (host + guest)."""

    channel_id: str
    members: dict[int, str] = field(default_factory=dict)  # client_id -> role

    def counterparty(self, client_id: int) -> tuple[int, str] | None:
        for other_id, other_role in self.members.items():
            if other_id != client_id:
                return other_id, other_role
        return None


class RelayServer:
    """A real relay endpoint for development and integration tests."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = DEFAULT_RELAY_PORT,
        session_ttl_seconds: float = RELAY_SESSION_TTL_SECONDS,
        heartbeat_interval_seconds: float = HEARTBEAT_INTERVAL_SECONDS,
        server_name: str = SERVER_NAME,
    ) -> None:
        self._host = host
        self._requested_port = port
        self._session_ttl = session_ttl_seconds
        self._heartbeat_interval = heartbeat_interval_seconds
        self._server_name = server_name
        self._logger = get_logger("transport.relay.server")
        self._server: asyncio.AbstractServer | None = None
        self._clients: dict[int, _ClientContext] = {}
        self.sessions = SessionRegistry(
            sweep_interval_seconds=SESSION_SWEEP_INTERVAL_SECONDS,
            logger_name="transport.relay.server.sessions",
        )
        self._next_client_id = 0
        self._channels: dict[str, _Channel] = {}

    # ------------------------------------------------------------- properties

    @property
    def port(self) -> int:
        server = self._server
        sockets = None if server is None else cast("asyncio.base_events.Server", server).sockets
        if not sockets:
            return self._requested_port
        return int(sockets[0].getsockname()[1])

    @property
    def host(self) -> str:
        return self._host

    @property
    def url(self) -> str:
        return f"ws://{self._host}:{self.port}/relay"

    @property
    def client_count(self) -> int:
        return len(self._clients)

    @property
    def channel_count(self) -> int:
        return len(self._channels)

    def channel_peers(self, channel_id: str) -> tuple[str, ...]:
        """Roles currently attached to ``channel_id`` (observability/tests)."""

        channel = self._channels.get(channel_id)
        if channel is None:
            return ()
        return tuple(sorted(channel.members.values()))

    # -------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Bind the listener and start the session sweeper."""

        self._server = await asyncio.start_server(
            self._on_connection, self._host, self._requested_port
        )
        self.sessions.start_sweeper()
        self._logger.info("relay listening on %s (ttl=%.0fs)", self.url, self._session_ttl)

    async def serve_forever(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        async with self._server:
            await self._server.serve_forever()

    async def aclose(self) -> None:
        """Stop accepting, say goodbye to clients, and settle all tasks."""

        await self.sessions.stop_sweeper()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

        clients = list(self._clients.items())
        tasks = [context.handler_task for _, context in clients if context.handler_task is not None]
        for _, context in clients:
            await self._disconnect_client(context, "relay shutting down")
        if tasks:
            with suppress(asyncio.CancelledError):
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3.0)
        self._logger.info("relay stopped")

    async def __aenter__(self) -> RelayServer:
        await self.start()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        await self.aclose()

    # -------------------------------------------------------------- accept loop

    def _on_connection(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._next_client_id += 1
        client_id = self._next_client_id
        peer = writer.get_extra_info("peername")
        remote = f"{peer[0]}:{peer[1]}" if peer else "unknown"
        context = _ClientContext(reader=reader, writer=writer, remote=remote)
        self._clients[client_id] = context
        task = asyncio.create_task(
            self._serve_client(client_id, context),
            name=f"ghostlink-relay-client-{client_id}",
        )
        context.handler_task = task

    async def _serve_client(self, client_id: int, context: _ClientContext) -> None:
        try:
            await self._handle_client(client_id, context)
        except ConnectionClosed as exc:
            self._logger.debug("client %s dropped — %s", context.remote, exc)
        except (ConnectionTimeoutError, HandshakeError) as exc:
            self._logger.debug("client %s handshake failed — %s", context.remote, exc)
        except PacketValidationError as exc:
            self._logger.debug("client %s protocol error — %s", context.remote, exc.message)
        except TransportError as exc:
            self._logger.debug("client %s transport error — %s", context.remote, exc)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a single client must never kill the relay
            self._logger.info("client %s failed unexpectedly — %s", context.remote, exc)
        finally:
            await self._detach_client(client_id, context)

    async def _handle_client(self, client_id: int, context: _ClientContext) -> None:
        await perform_server_handshake(
            context.reader, context.writer, timeout_seconds=HANDSHAKE_TIMEOUT_SECONDS
        )
        context.connection = WebSocketConnection(
            context.reader, context.writer, mask_outgoing=False, expect_masked=True
        )
        self._logger.debug("websocket upgraded — %s", context.remote)

        session = await self._expect_hello(client_id, context)
        context.session = session

        while True:
            raw = await context.conn.read_message()
            if raw is None:
                self._logger.debug("websocket closed — %s", context.remote)
                return
            data = raw.data.encode("utf-8") if isinstance(raw.data, str) else raw.data
            await self._dispatch(client_id, context, data)

    async def _expect_hello(self, client_id: int, context: _ClientContext) -> Session:
        try:
            raw = await asyncio.wait_for(context.conn.read_message(), timeout=HELLO_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            raise ConnectionTimeoutError(
                "Client never sent HELLO after upgrading.",
                hint="Clients must open the session with a HELLO packet.",
            ) from exc
        if raw is None:
            raise HandshakeError("Client closed before completing HELLO.")
        data = raw.data.encode("utf-8") if isinstance(raw.data, str) else raw.data
        packet = decode_packet(data, peer=context.remote)
        if packet.type is not PacketType.HELLO:
            await self._send(
                context,
                error_packet("protocol/expected-hello", "First packet must be HELLO."),
            )
            raise HandshakeError(
                f"Client {context.remote} opened with {packet.type.value}, expected HELLO."
            )
        session = self.sessions.register(
            Session.create(
                metadata={
                    "remote": context.remote,
                    "client": str(packet.payload["client"]),
                    "server": self._server_name,
                    "protocol": str(SERVER_PROTOCOL_VERSION),
                },
                ttl_seconds=self._session_ttl,
            )
        )
        await self._send(
            context,
            welcome_packet(
                session.session_id,
                server_name=self._server_name,
                heartbeat_interval_seconds=self._heartbeat_interval,
            ),
        )
        self._logger.info("client attached — %s as %s", context.remote, session.session_id)
        return session

    async def _dispatch(self, client_id: int, context: _ClientContext, data: bytes) -> None:
        try:
            packet = decode_packet(data, peer=context.remote)
        except PacketValidationError as exc:
            self._logger.debug("invalid packet from %s — %s", context.remote, exc.message)
            await self._send(
                context,
                error_packet("protocol/invalid-packet", f"Invalid packet: {exc.message}"),
            )
            return

        if context.session is not None:
            context.session.touch(activity=packet.type.value)
        self._logger.debug(
            "packet from %s — %s id=%s", context.remote, packet.type.value, packet.packet_id
        )

        if packet.type is PacketType.PING:
            await self._send(
                context,
                pong_packet(
                    str(packet.payload["nonce"]),
                    sent_at=float(packet.payload["sent_at"]),
                ),
            )
        elif packet.type is PacketType.HEARTBEAT:
            await self._send(context, heartbeat_packet(int(packet.payload["seq"])))
        elif packet.type is PacketType.DISCONNECT:
            self._logger.info(
                "client %s says goodbye — %s",
                context.remote,
                packet.payload.get("reason", ""),
            )
            await self._disconnect_client(context, "client requested disconnect")
            raise ConnectionClosed("Client requested disconnect.")
        elif packet.type is PacketType.ATTACH:
            await self._handle_attach(client_id, context, packet)
        elif packet.type is PacketType.DETACH:
            await self._handle_detach(client_id, context, str(packet.payload["channel"]))
        elif packet.type is PacketType.FORWARD:
            await self._handle_forward(client_id, context, packet)
        elif packet.type in (PacketType.HELLO, PacketType.WELCOME):
            await self._send(
                context,
                error_packet(
                    "protocol/unexpected-type",
                    f"{packet.type.value} is not valid mid-session.",
                ),
            )
        else:
            await self._send(
                context,
                error_packet(
                    "protocol/unsupported",
                    f"Packet type {packet.type.value} is not handled by this relay.",
                ),
            )

    # ---------------------------------------------------- rendezvous routing

    async def _handle_attach(self, client_id: int, context: _ClientContext, packet: Packet) -> None:
        channel_id = str(packet.payload["channel"])
        role = str(packet.payload["role"])
        channel = self._channels.setdefault(channel_id, _Channel(channel_id))

        if client_id in channel.members:
            channel.members[client_id] = role  # re-attach refreshes the role
        elif len(channel.members) >= CHANNEL_CAPACITY:
            await self._channel_error(
                context,
                "relay/channel-full",
                f"Channel {channel_id} already has {CHANNEL_CAPACITY} participants.",
                channel_id,
            )
            return
        else:
            channel.members[client_id] = role

        context.attachments[channel_id] = role
        other_count = len(channel.members) - 1
        await self._send(context, attached_packet(channel_id, role, other_count))
        self._logger.info(
            "channel attach — %s as %s on %s (%d member(s))",
            context.remote,
            role,
            channel_id,
            len(channel.members),
        )
        counterparty = channel.counterparty(client_id)
        if counterparty is not None:
            await self._notify_peer_joined(client_id, channel_id)

    async def _handle_detach(
        self, client_id: int, context: _ClientContext, channel_id: str
    ) -> None:
        if channel_id not in context.attachments:
            await self._channel_error(
                context, "relay/not-attached", f"Not attached to channel {channel_id}.", channel_id
            )
            return
        await self._leave_channel(client_id, channel_id)

    async def _handle_forward(
        self, client_id: int, context: _ClientContext, packet: Packet
    ) -> None:
        channel_id = str(packet.payload["channel"])
        channel = self._channels.get(channel_id)
        if channel is None or client_id not in channel.members:
            await self._channel_error(
                context,
                "relay/not-attached",
                f"Attach to channel {channel_id} before forwarding.",
                channel_id,
            )
            return
        counterparty = channel.counterparty(client_id)
        if counterparty is None:
            await self._channel_error(
                context,
                "relay/no-peer",
                f"No peer is attached to channel {channel_id}; payload dropped.",
                channel_id,
            )
            return
        target = self._clients.get(counterparty[0])
        if target is None or target.connection is None or target.connection.closed:
            await self._channel_error(
                context,
                "relay/no-peer",
                f"The peer on channel {channel_id} is unreachable; payload dropped.",
                channel_id,
            )
            return
        # The body is end-to-end ciphertext by contract; the relay routes it
        # untouched and never inspects it.
        await self._send(target, forward_packet(channel_id, str(packet.payload["body"])))
        self._logger.debug(
            "forwarded %d bytes on %s — %s → %s",
            len(str(packet.payload["body"])),
            channel_id,
            context.remote,
            target.remote,
        )

    async def _notify_peer_joined(self, client_id: int, channel_id: str) -> None:
        channel = self._channels.get(channel_id)
        if channel is None:
            return
        counterparty = channel.counterparty(client_id)
        if counterparty is None:
            return
        target = self._clients.get(counterparty[0])
        if target is not None:
            with suppress(TransportError, ConnectionError, OSError):
                await self._send(target, peer_packet(channel_id, "joined"))

    async def _notify_peer_left(self, client_id: int, channel_id: str) -> None:
        channel = self._channels.get(channel_id)
        if channel is None:
            return
        remaining = next(iter(channel.members), None)
        if remaining is None:
            return
        target = self._clients.get(remaining)
        if target is not None:
            with suppress(TransportError, ConnectionError, OSError):
                await self._send(target, peer_packet(channel_id, "left"))

    async def _leave_channel(self, client_id: int, channel_id: str) -> None:
        channel = self._channels.get(channel_id)
        if channel is None or client_id not in channel.members:
            return
        del channel.members[client_id]
        context = self._clients.get(client_id)
        if context is not None:
            context.attachments.pop(channel_id, None)
        self._logger.debug("channel detach — client %d left %s", client_id, channel_id)
        if not channel.members:
            del self._channels[channel_id]
            return
        await self._notify_peer_left(client_id, channel_id)

    # ---------------------------------------------------------------- teardown

    async def _channel_error(
        self, context: _ClientContext, code: str, message: str, channel_id: str
    ) -> None:
        """ERROR packet scoped to a channel — the channel id travels so the
        client can reject the matching pending operation."""

        await self._send(
            context,
            Packet(
                PacketType.ERROR,
                {"code": code, "message": message, "channel": channel_id},
            ),
        )

    async def _send(self, context: _ClientContext, packet: Packet) -> None:
        await context.conn.send_text(encode_packet(packet).decode("utf-8"))

    async def _disconnect_client(self, context: _ClientContext, reason: str) -> None:
        if context.connection is None or context.connection.closed:
            return
        if context.session is not None:
            with suppress(TransportError, ConnectionError, OSError):
                await self._send(context, disconnect_packet(reason))
        await context.conn.close()

    async def _detach_client(self, client_id: int, context: _ClientContext) -> None:
        self._clients.pop(client_id, None)
        for channel_id in list(context.attachments):
            await self._leave_channel(client_id, channel_id)
        context.attachments.clear()
        if context.session is not None:
            self.sessions.remove(context.session.session_id)
            context.session = None
        if context.connection is not None and not context.connection.closed:
            await context.connection.close()


def main(argv: list[str] | None = None) -> int:
    """Console entry for ``python -m ghostlink.transport.relay.server``."""

    parser = argparse.ArgumentParser(
        prog="ghostlink-relay",
        description="Run the reference GhostLink relay (development use).",
    )
    parser.add_argument("--host", default="127.0.0.1", help="bind address")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_RELAY_PORT, help="bind port (0 = ephemeral)"
    )
    parser.add_argument(
        "--session-ttl",
        type=float,
        default=RELAY_SESSION_TTL_SECONDS,
        metavar="SECONDS",
        help="idle session time-to-live",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    logger = get_logger("transport.relay.server")
    server = RelayServer(host=args.host, port=args.port, session_ttl_seconds=args.session_ttl)
    try:
        asyncio.run(server.serve_forever())
    except KeyboardInterrupt:
        logger.info("relay interrupted; exiting")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
