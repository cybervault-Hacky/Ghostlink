"""Minimal, correct RFC 6455 framing and HTTP Upgrade handshakes.

Implemented directly over ``asyncio`` streams so GhostLink carries zero
compiled dependencies on Termux. Covers what the relay protocol needs, done
properly: client and server handshakes, masked client frames, fragmentation,
control-frame rules, payloads up to 64-bit lengths, and close handshakes.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import secrets
import struct
from contextlib import suppress
from dataclasses import dataclass
from enum import IntEnum

from ghostlink.constants import net as net_constants
from ghostlink.constants.net import WEBSOCKET_GUID, WEBSOCKET_VERSION
from ghostlink.exceptions.transport import (
    ConnectionTimeoutError,
    HandshakeError,
    ProtocolError,
    TransportError,
)


class Opcode(IntEnum):
    """RFC 6455 frame opcodes."""

    CONTINUATION = 0x0
    TEXT = 0x1
    BINARY = 0x2
    CLOSE = 0x8
    PING = 0x9
    PONG = 0xA


CONTROL_OPCODES: frozenset[Opcode] = frozenset({Opcode.CLOSE, Opcode.PING, Opcode.PONG})
DATA_OPCODES: frozenset[Opcode] = frozenset({Opcode.CONTINUATION, Opcode.TEXT, Opcode.BINARY})


class ConnectionClosed(TransportError):
    """The stream ended (cleanly or abruptly) while a message was expected."""

    error_title = "Connection closed"

    def __init__(
        self,
        message: str = "The peer closed the connection.",
        *,
        hint: str | None = None,
        code: int | None = None,
        reason: str = "",
    ) -> None:
        super().__init__(message, hint=hint)
        self.code = code
        self.reason = reason


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    """One complete (possibly reassembled) application message."""

    data: str | bytes


@dataclass(frozen=True, slots=True)
class _Frame:
    fin: bool
    opcode: int
    payload: bytes


def _mask(payload: bytes, key: bytes) -> bytes:
    return bytes(byte ^ key[index & 3] for index, byte in enumerate(payload))


def _read_headers(head: bytes) -> tuple[str, dict[str, str]]:
    """Split an HTTP head into its start line and lowercase header mapping."""

    text = head.decode("ascii", errors="strict").rstrip("\r\n")
    lines = text.split("\r\n")
    start_line = lines[0]
    headers: dict[str, str] = {}
    for line in lines[1:]:
        if not line:
            continue
        name, separator, value = line.partition(":")
        if not separator:
            raise HandshakeError(
                f"Malformed HTTP header line: {line!r}.",
                hint="The peer does not speak a valid WebSocket handshake.",
            )
        key = name.strip().lower()
        if key in headers:
            raise HandshakeError(
                f"Duplicate HTTP header '{key}' in handshake.",
                hint="The peer sent an invalid upgrade request.",
            )
        headers[key] = value.strip()
    return start_line, headers


def _expected_accept(key: str) -> str:
    digest = hashlib.sha1((key + WEBSOCKET_GUID).encode("ascii")).digest()
    return base64.b64encode(digest).decode("ascii")


async def _read_head(reader: asyncio.StreamReader, limit: int) -> bytes:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except asyncio.LimitOverrunError as exc:
        raise HandshakeError(
            "Handshake head exceeds the buffer limit.",
            hint="The peer sent an abnormally large HTTP upgrade head.",
        ) from exc
    except asyncio.IncompleteReadError as exc:
        raise ConnectionClosed("Peer disconnected mid-handshake.") from exc
    if len(head) > limit:
        raise HandshakeError(
            f"Handshake head exceeds {limit} bytes.",
            hint="The peer sent an abnormally large HTTP upgrade head.",
        )
    return head


async def perform_client_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    host: str,
    port: int,
    resource: str,
    timeout_seconds: float,
) -> None:
    """Run the client side of the HTTP Upgrade handshake."""

    key = base64.b64encode(secrets.token_bytes(16)).decode("ascii")
    request = (
        f"GET {resource} HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        f"Sec-WebSocket-Version: {WEBSOCKET_VERSION}\r\n"
        "\r\n"
    )
    writer.write(request.encode("ascii"))
    await writer.drain()

    try:
        head = await asyncio.wait_for(
            _read_head(reader, net_constants.MAX_HANDSHAKE_BYTES), timeout=timeout_seconds
        )
    except TimeoutError as exc:
        raise ConnectionTimeoutError(
            f"WebSocket handshake with {host}:{port} timed out.",
            hint="The endpoint accepted TCP but never answered the upgrade.",
        ) from exc
    status_line, headers = _read_headers(head)

    parts = status_line.split(" ", 2)
    if len(parts) < 2 or parts[1] != "101":
        raise HandshakeError(
            f"Handshake rejected by the server: {status_line!r}.",
            hint="Verify the relay URL and that the endpoint is a WebSocket relay.",
        )
    upgrade = headers.get("upgrade", "")
    connection = headers.get("connection", "")
    accept = headers.get("sec-websocket-accept", "")
    if upgrade.lower() != "websocket" or "upgrade" not in connection.lower():
        raise HandshakeError(
            "Handshake response is missing the required Upgrade headers.",
            hint="The endpoint did not complete a WebSocket upgrade.",
        )
    if accept != _expected_accept(key):
        raise HandshakeError(
            "Handshake response has an invalid Sec-WebSocket-Accept value.",
            hint="The endpoint is not a compliant WebSocket server.",
        )


async def perform_server_handshake(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    *,
    timeout_seconds: float,
) -> str:
    """Run the server side of the handshake; returns the requested resource."""

    try:
        head = await asyncio.wait_for(
            _read_head(reader, net_constants.MAX_HANDSHAKE_BYTES), timeout=timeout_seconds
        )
    except TimeoutError as exc:
        raise ConnectionTimeoutError(
            "Client never completed the WebSocket handshake in time.",
            hint="The peer accepted TCP but stalled before upgrading.",
        ) from exc
    request_line, headers = _read_headers(head)
    parts = request_line.split(" ")
    if len(parts) != 3 or parts[0] != "GET":
        raise HandshakeError(
            f"Malformed WebSocket request line: {request_line!r}.",
            hint="Clients must open with an HTTP GET upgrade request.",
        )
    resource = parts[1]
    key = headers.get("sec-websocket-key")
    version = headers.get("sec-websocket-version")
    upgrade = headers.get("upgrade", "")
    connection = headers.get("connection", "")
    if not key or version != WEBSOCKET_VERSION:
        raise HandshakeError(
            "WebSocket upgrade is missing its key or uses an unsupported version.",
            hint=f"Supported version: {WEBSOCKET_VERSION}.",
        )
    if upgrade.lower() != "websocket" or "upgrade" not in connection.lower():
        raise HandshakeError(
            "Request is missing the required Upgrade headers.",
            hint="Clients must request a WebSocket upgrade.",
        )
    try:
        if len(base64.b64decode(key, validate=True)) != 16:
            raise ValueError("bad length")
    except ValueError as exc:
        raise HandshakeError(
            "Sec-WebSocket-Key is not valid base64 of 16 bytes.",
            hint="The client sent a malformed upgrade key.",
        ) from exc
    response = (
        "HTTP/1.1 101 Switching Protocols\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Accept: {_expected_accept(key)}\r\n"
        "\r\n"
    )
    writer.write(response.encode("ascii"))
    await writer.drain()
    return resource


class WebSocketConnection:
    """Frame-level send/receive over an established stream pair.

    ``mask_outgoing`` is True for clients (RFC 6455 requires client masking)
    and False for servers; ``expect_masked`` is the mirror on receive.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        mask_outgoing: bool,
        expect_masked: bool,
        max_message_bytes: int = net_constants.MAX_MESSAGE_BYTES,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._mask_outgoing = mask_outgoing
        self._expect_masked = expect_masked
        self._max_message_bytes = max_message_bytes
        self._closed = False
        self._close_sent = False
        self.close_code: int | None = None
        self.close_reason: str = ""

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------------ send

    async def _send_frame(self, opcode: Opcode, payload: bytes, *, fin: bool = True) -> None:
        if self._closed and opcode is not Opcode.CLOSE:
            raise ConnectionClosed("Cannot send: the connection is closed.")
        if opcode in CONTROL_OPCODES:
            if not fin:
                raise ProtocolError("Control frames must not be fragmented.")
            if len(payload) > net_constants.MAX_CONTROL_PAYLOAD:
                raise ProtocolError("Control frame payload exceeds 125 bytes.")
        first = (0x80 if fin else 0x00) | opcode
        mask_bit = 0x80 if self._mask_outgoing else 0x00
        length = len(payload)
        if length < 126:
            header = struct.pack("!BB", first, mask_bit | length)
        elif length <= 0xFFFF:
            header = struct.pack("!BBH", first, mask_bit | 126, length)
        else:
            header = struct.pack("!BBQ", first, mask_bit | 127, length)
        if self._mask_outgoing:
            key = secrets.token_bytes(4)
            self._writer.write(header + key + _mask(payload, key))
        else:
            self._writer.write(header + payload)
        await self._writer.drain()

    async def send_text(self, message: str) -> None:
        await self._send_frame(Opcode.TEXT, message.encode("utf-8"))

    async def send_bytes(self, data: bytes) -> None:
        await self._send_frame(Opcode.BINARY, data)

    async def send_pong(self, payload: bytes) -> None:
        await self._send_frame(Opcode.PONG, payload)

    # ---------------------------------------------------------------- receive

    async def _read_exactly(self, length: int) -> bytes:
        try:
            return await self._reader.readexactly(length)
        except asyncio.IncompleteReadError as exc:
            raise ConnectionClosed("Peer disconnected mid-frame.", code=self.close_code) from exc

    async def _read_frame(self) -> _Frame:
        header = await self._read_exactly(2)
        fin = bool(header[0] & 0x80)
        if header[0] & 0x70:
            raise ProtocolError("Reserved frame bits are set (RSV non-zero).")
        opcode = header[0] & 0x0F
        known = {int(item) for item in Opcode}
        if opcode not in known:
            await self._fail(1002, "unknown opcode")
            raise ProtocolError(f"Unknown frame opcode 0x{opcode:X}.")
        masked = bool(header[1] & 0x80)
        if masked != self._expect_masked:
            required = "masked" if self._expect_masked else "unmasked"
            await self._fail(1002, "masking violation")
            raise ProtocolError(f"Frames from this peer must be {required}.")
        length7 = header[1] & 0x7F
        if length7 == 126:
            (length,) = struct.unpack("!H", await self._read_exactly(2))
        elif length7 == 127:
            (length,) = struct.unpack("!Q", await self._read_exactly(8))
        else:
            length = length7
        if opcode in {int(item) for item in CONTROL_OPCODES}:
            if not fin:
                await self._fail(1002, "fragmented control frame")
                raise ProtocolError("Control frames must not be fragmented.")
            if length > net_constants.MAX_CONTROL_PAYLOAD:
                await self._fail(1002, "oversized control frame")
                raise ProtocolError("Control frame payload exceeds 125 bytes.")
        if length > self._max_message_bytes:
            await self._fail(1009, "message too large")
            raise ProtocolError(f"Frame payload exceeds {self._max_message_bytes} bytes.")
        key = await self._read_exactly(4) if masked else b""
        payload = await self._read_exactly(length) if length else b""
        return _Frame(fin=fin, opcode=opcode, payload=_mask(payload, key) if masked else payload)

    async def read_message(self) -> IncomingMessage | None:
        """Return the next complete message, or ``None`` on a clean close.

        Wire ping/pong is answered transparently; a close frame completes the
        close handshake and yields ``None``.
        """

        fragments: list[bytes] = []
        fragmented_is_text = False
        fragmenting = False
        total = 0

        while True:
            frame = await self._read_frame()
            opcode = frame.opcode

            if opcode == Opcode.PING:
                await self.send_pong(frame.payload)
                continue
            if opcode == Opcode.PONG:
                continue
            if opcode == Opcode.CLOSE:
                self.close_code, self.close_reason = _parse_close_payload(frame.payload)
                if not self._close_sent:
                    await self._send_frame(Opcode.CLOSE, frame.payload)
                    self._close_sent = True
                # The close handshake is complete: bring the TCP connection
                # down here so a peer-initiated close never leaks the socket.
                self._closed = True
                await self._close_stream()
                return None

            if opcode in (Opcode.TEXT, Opcode.BINARY):
                if fragmenting:
                    await self._fail(1002, "interleaved message")
                    raise ProtocolError("New message began before a fragmented one finished.")
                if frame.fin:
                    return (
                        IncomingMessage(_decode_text(frame.payload))
                        if opcode == Opcode.TEXT
                        else IncomingMessage(frame.payload)
                    )
                fragmenting = True
                fragmented_is_text = opcode == Opcode.TEXT
                fragments = [frame.payload]
                total = len(frame.payload)
                continue

            # CONTINUATION
            if not fragmenting:
                await self._fail(1002, "stray continuation frame")
                raise ProtocolError("Continuation frame without an active message.")
            total += len(frame.payload)
            if total > self._max_message_bytes:
                await self._fail(1009, "message too large")
                raise ProtocolError(f"Message exceeds {self._max_message_bytes} bytes.")
            fragments.append(frame.payload)
            if frame.fin:
                assembled = b"".join(fragments)
                return (
                    IncomingMessage(_decode_text(assembled))
                    if fragmented_is_text
                    else IncomingMessage(assembled)
                )

    # ------------------------------------------------------------------ close

    async def _fail(self, code: int, reason: str) -> None:
        if not self._close_sent:
            self._close_sent = True
            with suppress(ConnectionClosed):
                await self._send_frame(Opcode.CLOSE, struct.pack("!H", code) + reason.encode())

    async def _close_stream(self) -> None:
        self._writer.close()
        with suppress(TimeoutError, OSError, ConnectionError):
            await asyncio.wait_for(self._writer.wait_closed(), timeout=2.0)

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        """Close the stream, sending a close frame when one is still owed.

        Idempotent. The close *frame* is only sent once (skipped when the
        read path already echoed the peer's close), but the underlying
        writer is always driven to closure — a completed close handshake
        must never leak the socket.
        """

        already_closed = self._closed
        self._closed = True
        if not self._close_sent and not already_closed:
            self._close_sent = True
            with suppress(ConnectionClosed, ProtocolError, TransportError, OSError):
                await self._send_frame(
                    Opcode.CLOSE, struct.pack("!H", code) + reason.encode("utf-8")
                )
        await self._close_stream()


def _decode_text(payload: bytes) -> str:
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolError("Text frame is not valid UTF-8.") from exc


def _parse_close_payload(payload: bytes) -> tuple[int | None, str]:
    if len(payload) < 2:
        return None, ""
    (code,) = struct.unpack("!H", payload[:2])
    try:
        reason = payload[2:].decode("utf-8")
    except UnicodeDecodeError:
        reason = ""
    return code, reason
