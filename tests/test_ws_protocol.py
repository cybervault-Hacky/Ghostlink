"""RFC 6455 framing and handshakes over real loopback stream pairs."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass

import pytest

from ghostlink.exceptions.transport import HandshakeError, ProtocolError
from ghostlink.transport.websocket.protocol import (
    Opcode,
    WebSocketConnection,
    perform_client_handshake,
    perform_server_handshake,
)
from tests.conftest import run


@dataclass(slots=True)
class PairEnds:
    """Both sides of a connected raw stream pair."""

    client_reader: asyncio.StreamReader
    client_writer: asyncio.StreamWriter
    server_reader: asyncio.StreamReader
    server_writer: asyncio.StreamWriter


@asynccontextmanager
async def raw_pair() -> AsyncIterator[PairEnds]:
    accepted: asyncio.Future[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = (
        asyncio.get_running_loop().create_future()
    )
    writers: list[asyncio.StreamWriter] = []

    async def handler(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if not accepted.done():
            accepted.set_result((reader, writer))
        await asyncio.sleep(30)  # keep the connection task alive for the test

    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        port = int(server.sockets[0].getsockname()[1])  # type: ignore[index]
        client_reader, client_writer = await asyncio.open_connection("127.0.0.1", port)
        writers.append(client_writer)
        server_reader, server_writer = await asyncio.wait_for(accepted, timeout=2.0)
        writers.append(server_writer)
        yield PairEnds(client_reader, client_writer, server_reader, server_writer)
    finally:
        server.close()
        await server.wait_closed()
        for writer in writers:  # deterministic teardown, even on failing paths
            writer.close()
        for writer in writers:
            with suppress(TimeoutError, OSError, ConnectionError):
                await asyncio.wait_for(writer.wait_closed(), timeout=1.0)


@asynccontextmanager
async def ws_pair() -> AsyncIterator[tuple[WebSocketConnection, WebSocketConnection]]:
    """A loopback WebSocket pair: masked client end, unmasked server end."""

    async with raw_pair() as ends:
        client = WebSocketConnection(
            ends.client_reader, ends.client_writer, mask_outgoing=True, expect_masked=False
        )
        server = WebSocketConnection(
            ends.server_reader, ends.server_writer, mask_outgoing=False, expect_masked=True
        )
        try:
            yield client, server
        finally:
            await client.close()
            await server.close()


class TestHandshakes:
    def test_client_server_handshake_success(self) -> None:
        async def scenario() -> str:
            async with raw_pair() as ends:
                client_task = asyncio.create_task(
                    perform_client_handshake(
                        ends.client_reader,
                        ends.client_writer,
                        host="127.0.0.1",
                        port=9,
                        resource="/relay",
                        timeout_seconds=2.0,
                    )
                )
                resource = await perform_server_handshake(
                    ends.server_reader, ends.server_writer, timeout_seconds=2.0
                )
                await asyncio.wait_for(client_task, timeout=2.0)
                return resource

        assert run(scenario()) == "/relay"

    def test_client_rejects_non_101_status(self) -> None:
        async def scenario() -> None:
            async with raw_pair() as ends:

                async def fake_server() -> None:
                    await ends.server_reader.readuntil(b"\r\n\r\n")
                    ends.server_writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
                    await ends.server_writer.drain()

                server_task = asyncio.create_task(fake_server())
                with pytest.raises(HandshakeError, match="400"):
                    await perform_client_handshake(
                        ends.client_reader,
                        ends.client_writer,
                        host="127.0.0.1",
                        port=9,
                        resource="/relay",
                        timeout_seconds=2.0,
                    )
                await server_task

        run(scenario())

    def test_client_rejects_wrong_accept_key(self) -> None:
        async def scenario() -> None:
            async with raw_pair() as ends:

                async def fake_server() -> None:
                    await ends.server_reader.readuntil(b"\r\n\r\n")
                    bogus = base64.b64encode(b"sixteen-byte-key").decode("ascii")
                    ends.server_writer.write(
                        (
                            "HTTP/1.1 101 Switching Protocols\r\n"
                            "Upgrade: websocket\r\n"
                            "Connection: Upgrade\r\n"
                            f"Sec-WebSocket-Accept: {bogus}\r\n"
                            "\r\n"
                        ).encode("ascii")
                    )
                    await ends.server_writer.drain()

                server_task = asyncio.create_task(fake_server())
                with pytest.raises(HandshakeError, match="Sec-WebSocket-Accept"):
                    await perform_client_handshake(
                        ends.client_reader,
                        ends.client_writer,
                        host="127.0.0.1",
                        port=9,
                        resource="/relay",
                        timeout_seconds=2.0,
                    )
                await server_task

        run(scenario())

    def test_client_rejects_missing_upgrade_headers(self) -> None:
        async def scenario() -> None:
            async with raw_pair() as ends:

                async def fake_server() -> None:
                    await ends.server_reader.readuntil(b"\r\n\r\n")
                    ends.server_writer.write(
                        b"HTTP/1.1 101 Switching Protocols\r\nSec-WebSocket-Accept: x\r\n\r\n"
                    )
                    await ends.server_writer.drain()

                server_task = asyncio.create_task(fake_server())
                with pytest.raises(HandshakeError, match="Upgrade headers"):
                    await perform_client_handshake(
                        ends.client_reader,
                        ends.client_writer,
                        host="127.0.0.1",
                        port=9,
                        resource="/relay",
                        timeout_seconds=2.0,
                    )
                await server_task

        run(scenario())

    def test_server_rejects_plain_http_get(self) -> None:
        async def scenario() -> None:
            async with raw_pair() as ends:
                ends.client_writer.write(b"GET / HTTP/1.1\r\nHost: x\r\n\r\n")
                await ends.client_writer.drain()
                with pytest.raises(HandshakeError, match="key or uses an unsupported"):
                    await perform_server_handshake(
                        ends.server_reader, ends.server_writer, timeout_seconds=2.0
                    )

        run(scenario())

    def test_server_rejects_malformed_header_line(self) -> None:
        async def scenario() -> None:
            async with raw_pair() as ends:
                ends.client_writer.write(b"GET / HTTP/1.1\r\nbroken-line\r\n\r\n")
                await ends.client_writer.drain()
                with pytest.raises(HandshakeError, match="Malformed HTTP header"):
                    await perform_server_handshake(
                        ends.server_reader, ends.server_writer, timeout_seconds=2.0
                    )

        run(scenario())


class TestFrames:
    def test_text_round_trip(self) -> None:
        async def scenario() -> object:
            async with ws_pair() as (client, server):
                await client.send_text("hello relay")
                message = await asyncio.wait_for(server.read_message(), timeout=2.0)
                assert message is not None
                return message.data

        assert run(scenario()) == "hello relay"

    def test_binary_round_trip(self) -> None:
        async def scenario() -> object:
            async with ws_pair() as (client, server):
                await server.send_bytes(b"\x00\x01\x02binary")
                message = await asyncio.wait_for(client.read_message(), timeout=2.0)
                assert message is not None
                return message.data

        assert run(scenario()) == b"\x00\x01\x02binary"

    def test_fragmented_message_is_reassembled(self) -> None:
        async def scenario() -> object:
            async with ws_pair() as (client, server):
                await client._send_frame(Opcode.TEXT, b"Gh", fin=False)
                await client._send_frame(Opcode.CONTINUATION, b"ost", fin=False)
                await client._send_frame(Opcode.CONTINUATION, b"Link", fin=True)
                message = await asyncio.wait_for(server.read_message(), timeout=2.0)
                assert message is not None
                return message.data

        assert run(scenario()) == "GhostLink"

    def test_large_payload_uses_extended_lengths(self) -> None:
        async def scenario() -> bytes:
            async with ws_pair() as (client, server):
                payload = bytes(70_000)  # forces the 64-bit length form > 0xFFFF
                await client.send_bytes(payload)
                message = await asyncio.wait_for(server.read_message(), timeout=5.0)
                assert message is not None
                return message.data  # type: ignore[return-value]

        assert len(run(scenario())) == 70_000

    def test_wire_ping_is_answered_transparently(self) -> None:
        async def scenario() -> object:
            async with ws_pair() as (client, server):
                await server._send_frame(Opcode.PING, b"xy")
                await server.send_text("after ping")
                # the client silently pongs, then surfaces the real message
                message = await asyncio.wait_for(client.read_message(), timeout=2.0)
                assert message is not None
                return message.data

        assert run(scenario()) == "after ping"

    def test_unmasked_frame_from_client_is_rejected(self) -> None:
        async def scenario() -> None:
            async with raw_pair() as ends:
                server = WebSocketConnection(
                    ends.server_reader,
                    ends.server_writer,
                    mask_outgoing=False,
                    expect_masked=True,
                )
                try:
                    ends.client_writer.write(b"\x81\x05ghost")  # FIN|TEXT, mask bit clear
                    await ends.client_writer.drain()
                    with pytest.raises(ProtocolError, match="must be masked"):
                        await server.read_message()
                finally:
                    await server.close()

        run(scenario())

    def test_masked_frame_from_server_is_rejected(self) -> None:
        async def scenario() -> None:
            async with raw_pair() as ends:
                client = WebSocketConnection(
                    ends.client_reader,
                    ends.client_writer,
                    mask_outgoing=True,
                    expect_masked=False,
                )
                try:
                    key = b"\x01\x02\x03\x04"
                    masked = bytes(byte ^ key[i & 3] for i, byte in enumerate(b"ghost"))
                    ends.server_writer.write(b"\x81\x85" + key + masked)
                    await ends.server_writer.drain()
                    with pytest.raises(ProtocolError, match="must be unmasked"):
                        await client.read_message()
                finally:
                    await client.close()

        run(scenario())

    def test_rsv_bits_are_rejected(self) -> None:
        async def scenario() -> None:
            async with raw_pair() as ends:
                server = WebSocketConnection(
                    ends.server_reader,
                    ends.server_writer,
                    mask_outgoing=False,
                    expect_masked=True,
                )
                try:
                    key = b"\x05\x06\x07\x08"
                    payload = bytes(byte ^ key[i & 3] for i, byte in enumerate(b"hi"))
                    ends.client_writer.write(b"\xc1\x82" + key + payload)  # RSV1 set
                    await ends.client_writer.drain()
                    with pytest.raises(ProtocolError, match="Reserved frame bits"):
                        await server.read_message()
                finally:
                    await server.close()

        run(scenario())

    def test_stray_continuation_frame_is_rejected(self) -> None:
        async def scenario() -> None:
            async with ws_pair() as (client, server):
                await client._send_frame(Opcode.CONTINUATION, b"orphan", fin=True)
                with pytest.raises(ProtocolError, match="without an active message"):
                    await server.read_message()

        run(scenario())

    def test_close_handshake_is_completed(self) -> None:
        async def scenario() -> tuple[int | None, str]:
            async with raw_pair() as ends:
                client = WebSocketConnection(
                    ends.client_reader,
                    ends.client_writer,
                    mask_outgoing=True,
                    expect_masked=False,
                )
                server = WebSocketConnection(
                    ends.server_reader,
                    ends.server_writer,
                    mask_outgoing=False,
                    expect_masked=True,
                )
                await server.close(code=1001, reason="going away")
                message = await asyncio.wait_for(client.read_message(), timeout=2.0)
                assert message is None  # clean close, close frame echoed
                code, reason = client.close_code, client.close_reason
                await client.close()
                return code, reason

        code, reason = run(scenario())
        assert code == 1001
        assert reason == "going away"

    def test_oversize_frame_is_rejected_at_receive(self) -> None:
        async def scenario() -> None:
            async with raw_pair() as ends:
                server = WebSocketConnection(
                    ends.server_reader,
                    ends.server_writer,
                    mask_outgoing=False,
                    expect_masked=True,
                    max_message_bytes=16,
                )
                try:
                    client = WebSocketConnection(
                        ends.client_reader,
                        ends.client_writer,
                        mask_outgoing=True,
                        expect_masked=False,
                    )
                    await client.send_text("x" * 64)
                    with pytest.raises(ProtocolError, match="exceeds 16 bytes"):
                        await server.read_message()
                    await client.close()
                finally:
                    await server.close()

        run(scenario())
