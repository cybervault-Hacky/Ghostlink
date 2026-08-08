"""Relay packet protocol: builders, validation, and the JSON codec."""

from __future__ import annotations

import json

import pytest

from ghostlink.constants.net import PROTOCOL_VERSION
from ghostlink.exceptions.transport import PacketValidationError
from ghostlink.transport.relay.protocol import (
    PacketType,
    decode_packet,
    disconnect_packet,
    encode_packet,
    error_packet,
    heartbeat_packet,
    hello_packet,
    ping_packet,
    pong_packet,
    validate_packet,
    welcome_packet,
)


class TestBuilders:
    def test_every_type_builds_valid_packet(self) -> None:
        packets = [
            hello_packet("GhostLink/0.2.0"),
            welcome_packet("sess_1", server_name="relay", heartbeat_interval_seconds=10.0),
            ping_packet("nonce123"),
            pong_packet("nonce123", sent_at=123.0),
            disconnect_packet("bye"),
            error_packet("protocol/test", "message"),
            heartbeat_packet(7),
        ]
        for packet in packets:
            validate_packet(packet.type, packet.payload)

    def test_default_ids_are_unique(self) -> None:
        assert ping_packet("a").packet_id != ping_packet("a").packet_id


class TestRoundTrip:
    def test_encode_decode_all_types(self) -> None:
        samples = [
            hello_packet("client-x", session_id="sess_resume1"),
            welcome_packet("sess_abc", server_name="relay-1", heartbeat_interval_seconds=5.0),
            ping_packet("deadbeef"),
            pong_packet("deadbeef", sent_at=42.5),
            disconnect_packet("shutdown", code=1001),
            error_packet("protocol/invalid", "nope"),
            heartbeat_packet(0),
        ]
        for original in samples:
            decoded = decode_packet(encode_packet(original))
            assert decoded.type is original.type
            assert decoded.payload == original.payload
            assert decoded.packet_id == original.packet_id

    def test_envelope_shape(self) -> None:
        document = json.loads(encode_packet(ping_packet("zz")).decode())
        assert document["v"] == PROTOCOL_VERSION
        assert document["type"] == "PING"
        assert set(document) == {"v", "id", "type", "ts", "payload"}


class TestValidationRejects:
    @pytest.mark.parametrize(
        ("packet_type", "payload", "reason"),
        [
            (PacketType.HELLO, {"protocol": 0, "client": "x"}, "protocol"),
            (PacketType.HELLO, {"protocol": 99, "client": "x"}, "unsupported protocol"),
            (PacketType.HELLO, {"protocol": 1, "client": ""}, "client"),
            (
                PacketType.WELCOME,
                {"session_id": "s", "server": "r", "server_time": 1},
                "heartbeat_interval",
            ),
            (PacketType.PING, {"nonce": "x" * 65, "sent_at": 1.0}, "nonce"),
            (PacketType.PONG, {"nonce": "n", "sent_at": 1.0}, "answered_at"),
            (PacketType.DISCONNECT, {"reason": "x" * 300, "code": 1000}, "reason"),
            (PacketType.DISCONNECT, {"reason": "ok", "code": True}, "code"),
            (PacketType.ERROR, {"code": "c", "message": ""}, "message"),
            (PacketType.HEARTBEAT, {"seq": -1, "timestamp": 1.0}, "seq"),
            (PacketType.HEARTBEAT, {"seq": "1", "timestamp": 1.0}, "seq"),
        ],
    )
    def test_invalid_payloads(self, packet_type, payload, reason) -> None:
        with pytest.raises(PacketValidationError, match=reason):
            validate_packet(packet_type, payload)


class TestDecodeRejects:
    def test_garbage_bytes(self) -> None:
        with pytest.raises(PacketValidationError, match="not valid JSON"):
            decode_packet(b"\x00\xff{nope")

    def test_non_object_document(self) -> None:
        with pytest.raises(PacketValidationError, match="must be a JSON object"):
            decode_packet(b'["HELLO"]')

    def test_unknown_type(self) -> None:
        raw = json.dumps(
            {"v": 1, "id": "aa", "type": "TELEPORT", "ts": 1.0, "payload": {}}
        ).encode()
        with pytest.raises(PacketValidationError, match="unknown packet type"):
            decode_packet(raw)

    def test_wrong_envelope_version(self) -> None:
        raw = json.dumps(
            {
                "v": 7,
                "id": "aa",
                "type": "PING",
                "ts": 1.0,
                "payload": {"nonce": "n", "sent_at": 1.0},
            }
        ).encode()
        with pytest.raises(PacketValidationError, match="envelope version"):
            decode_packet(raw)

    def test_missing_fields(self) -> None:
        raw = json.dumps({"v": 1, "type": "PING", "ts": 1.0, "payload": {}}).encode()
        with pytest.raises(PacketValidationError, match="'id'"):
            decode_packet(raw)


class TestErrorMetadata:
    def test_error_has_hint_and_exit_code_6(self) -> None:
        with pytest.raises(PacketValidationError) as captured:
            validate_packet(PacketType.HELLO, {"protocol": 1})
        assert captured.value.hint is not None
        assert int(captured.value.exit_code) == 6
