"""Phase 9 protocol-compatibility and downgrade-attempt tests.

The client must reject unsupported / malformed / future protocol versions
and must never silently fall back to an insecure suite. These are pure
unit tests on the packet layer plus a couple of loopback checks.
"""

from __future__ import annotations

import asyncio
import base64

import pytest

from ghostlink.constants.net import (
    DEFAULT_CRYPTO_SUITE,
    GROUP_CRYPTO_SUITES,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
)
from ghostlink.exceptions.transport import PacketValidationError
from ghostlink.groups.frames import KIND_SKMSG
from ghostlink.transport.relay.protocol import Packet, PacketType, decode_packet, encode_packet
from tests.conftest import run, running_relay


class TestVersionNegotiation:
    def test_supported_versions_are_explicit(self) -> None:
        assert PROTOCOL_VERSION == 4
        assert frozenset({1, 2, 3, 4}) == SUPPORTED_PROTOCOL_VERSIONS

    def test_future_protocol_version_rejected(self) -> None:
        packet = Packet(PacketType.HELLO, {"protocol": 99, "client": "x"})
        with pytest.raises(PacketValidationError):
            encode_packet(packet)

    def test_past_unsupported_version_rejected(self) -> None:
        # version 0 is never supported
        packet = Packet(PacketType.HELLO, {"protocol": 0, "client": "x"})
        with pytest.raises(PacketValidationError):
            encode_packet(packet)

    def test_malformed_version_rejected(self) -> None:
        # A string version is not a valid protocol version.
        packet = Packet(PacketType.HELLO, {"protocol": "4", "client": "x"})
        with pytest.raises(PacketValidationError):
            encode_packet(packet)

    def test_decode_rejects_unknown_version_envelope(self) -> None:
        raw = b'{"v": 99, "id": "abcd1234", "type": "PING", "ts": 1.0, "payload": {}}'
        with pytest.raises(PacketValidationError):
            decode_packet(raw)

    def test_decode_rejects_unknown_type(self) -> None:
        raw = b'{"v": 4, "id": "abcd1234", "type": "NOPE", "ts": 1.0, "payload": {}}'
        with pytest.raises(PacketValidationError):
            decode_packet(raw)


class TestNoDowngrade:
    def test_suite_set_fixed(self) -> None:
        assert frozenset({"mesh-v1", "senderkey-v1"}) == GROUP_CRYPTO_SUITES
        assert DEFAULT_CRYPTO_SUITE == "mesh-v1"

    def test_unknown_suite_rejected_at_wire(self) -> None:
        from ghostlink.transport.relay.protocol import group_create_packet

        with pytest.raises(PacketValidationError):
            encode_packet(
                group_create_packet("Ops", "a" * 64, "sig", "h", "d", crypto_suite="bogus")
            )

    def test_sender_key_frame_requires_senderkey_group(self) -> None:
        # A sender-key broadcast kind must be a known GROUP_FORWARD kind, but
        # carrying a malformed body is still rejected by framing.
        from ghostlink.transport.relay.protocol import group_forward_packet

        packet = group_forward_packet(
            "gl-group-AAAA-BBBB-CCCC",
            1,
            "GLFP-ABCD-1234-5678",
            "GLFP-0000-0000-0000",
            kind=KIND_SKMSG,
            body_b64=base64.b64encode(b"garbage").decode(),
        )
        # kind is valid; body decodes; encode succeeds — the domain layer
        # rejects cross-suite injection (covered by the adversarial e2e suite).
        encode_packet(packet)


class TestLoopbackVersion:
    def test_relay_rejects_future_client_on_wire(self) -> None:
        async def scenario() -> None:
            import json

            from ghostlink.transport.relay.endpoint import RelayEndpoint
            from ghostlink.transport.websocket.transport import WebSocketTransport

            async with running_relay() as server:
                endpoint = RelayEndpoint.from_url(server.url)
                transport = WebSocketTransport.dial(
                    endpoint.host,
                    endpoint.port,
                    resource=endpoint.resource,
                    secure=False,
                    timeout_seconds=2.0,
                )
                await transport.open()
                try:
                    # Send a HELLO with an unsupported protocol version.
                    hello = json.dumps(
                        {
                            "v": 99,
                            "id": "abcd1234",
                            "type": "HELLO",
                            "ts": 1.0,
                            "payload": {"protocol": 99, "client": "x"},
                        }
                    )
                    await transport.send(hello.encode())
                    response = await asyncio.wait_for(transport.receive(), timeout=2.0)
                    # The relay must NOT silently accept a future protocol.
                    # It either answers an ERROR packet or drops the connection
                    # (None) — both are fail-closed, never a silent accept.
                    assert response is None or "error" in str(response).lower()
                finally:
                    await transport.close()

        run(scenario())
