"""WebSocketTransport against real loopback endpoints (no mocks)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import pytest

from ghostlink.exceptions.transport import HandshakeError, TransportError
from ghostlink.transport.transport import TransportState
from ghostlink.transport.websocket.protocol import (
    WebSocketConnection,
    perform_client_handshake,
    perform_server_handshake,
)
from ghostlink.transport.websocket.transport import WebSocketTransport
from tests.conftest import run


@asynccontextmanager
async def echo_endpoint(*, reply_400: bool = False) -> AsyncIterator[int]:
    """Run a loopback WebSocket echo endpoint; yields its bound port."""

    tasks: set[asyncio.Task[None]] = set()

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        task = asyncio.current_task()
        assert task is not None
        tasks.add(task)
        try:
            if reply_400:
                await reader.readuntil(b"\r\n\r\n")
                writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                await writer.drain()
                writer.close()
                return
            await perform_server_handshake(reader, writer, timeout_seconds=2.0)
            connection = WebSocketConnection(
                reader, writer, mask_outgoing=False, expect_masked=True
            )
            while True:
                message = await connection.read_message()
                if message is None:
                    break
                if isinstance(message.data, str):
                    await connection.send_text(message.data)
                else:
                    await connection.send_bytes(message.data)
        except (TransportError, ConnectionError, OSError, asyncio.IncompleteReadError):
            pass
        finally:
            tasks.discard(task)
            writer.close()

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        yield int(server.sockets[0].getsockname()[1])  # type: ignore[index]
    finally:
        server.close()
        await server.wait_closed()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=3.0)


def _dial(port: int, *, timeout: float = 2.0) -> WebSocketTransport:
    return WebSocketTransport.dial(
        "127.0.0.1", port, resource="/relay", secure=False, timeout_seconds=timeout
    )


class TestDialAndEcho:
    def test_full_round_trip_with_stats(self) -> None:
        async def scenario() -> WebSocketTransport:
            async with echo_endpoint() as port:
                transport = _dial(port)
                await transport.open()
                assert transport.state is TransportState.OPEN
                assert transport.name == "websocket"
                assert transport.remote_description.endswith("/relay")

                await transport.send(b"ping-payload")
                echoed = await asyncio.wait_for(transport.receive(), timeout=2.0)
                assert echoed == b"ping-payload"
                assert transport.stats.messages_sent == 1
                assert transport.stats.messages_received == 1
                assert transport.stats.bytes_sent == len(b"ping-payload")
                assert transport.stats.bytes_received == len(b"ping-payload")

                await transport.send_text("json-payload")
                echoed_text = await asyncio.wait_for(transport.receive(), timeout=2.0)
                assert echoed_text == b"json-payload"
                await transport.close()
                return transport

        transport = run(scenario())
        assert transport.state is TransportState.CLOSED
        assert transport.stats.closed_at is not None
        assert transport.stats.uptime_seconds >= 0.0

    def test_close_is_idempotent(self) -> None:
        async def scenario() -> WebSocketTransport:
            async with echo_endpoint() as port:
                transport = _dial(port)
                await transport.open()
                await transport.close()
                await transport.close()
                return transport

        assert run(scenario()).state is TransportState.CLOSED

    def test_open_failure_marks_failed(self) -> None:
        async def scenario() -> WebSocketTransport:
            transport = _dial(1, timeout=0.5)  # port 1: nothing listens there
            with pytest.raises(TransportError, match="Could not reach"):
                await transport.open()
            return transport

        transport = run(scenario())
        assert transport.state is TransportState.FAILED

    def test_non_websocket_endpoint_fails_the_handshake(self) -> None:
        async def scenario() -> WebSocketTransport:
            async with echo_endpoint(reply_400=True) as port:
                transport = _dial(port)
                with pytest.raises(HandshakeError, match="400"):
                    await transport.open()
                return transport

        assert run(scenario()).state is TransportState.FAILED

    def test_operations_require_open(self) -> None:
        async def scenario() -> None:
            transport = _dial(1)
            with pytest.raises(TransportError, match="not open yet"):
                await transport.send(b"x")
            with pytest.raises(TransportError, match="not open yet"):
                await transport.receive()

        run(scenario())


@asynccontextmanager
async def upgraded_server_side(
    *,
    register: list[WebSocketConnection],
) -> AsyncIterator[int]:
    """Accept one raw client, upgrade it, and hand the server side to ``register``."""

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await perform_server_handshake(reader, writer, timeout_seconds=2.0)
        register.append(
            WebSocketConnection(reader, writer, mask_outgoing=False, expect_masked=True)
        )
        await asyncio.sleep(30)  # kept alive; accept() teardown closes the writer

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        yield int(server.sockets[0].getsockname()[1])  # type: ignore[index]
    finally:
        server.close()
        await server.wait_closed()


class TestAccept:
    def test_accept_wraps_an_upgraded_connection(self) -> None:
        async def scenario() -> WebSocketTransport:
            server_side: list[WebSocketConnection] = []
            async with upgraded_server_side(register=server_side) as port:
                reader, writer = await asyncio.open_connection("127.0.0.1", port)
                handshake = asyncio.create_task(
                    perform_client_handshake(
                        reader,
                        writer,
                        host="127.0.0.1",
                        port=port,
                        resource="/relay",
                        timeout_seconds=2.0,
                    )
                )
                while not server_side:
                    await asyncio.sleep(0.005)
                await handshake

                transport = WebSocketTransport.accept(server_side[0], remote="loopback-peer")
                assert transport.state is TransportState.OPEN
                assert transport.name == "websocket"
                assert transport.remote_description == "loopback-peer"
                assert transport.stats.opened_at is not None
                with pytest.raises(TransportError, match="not created as a client"):
                    await transport.open()
                await transport.close()
                assert transport.state is TransportState.CLOSED
                await transport.close()  # idempotent

                writer.close()
                await writer.wait_closed()
                return transport

        run(scenario())
