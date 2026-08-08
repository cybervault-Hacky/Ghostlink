"""Secure-channel frame builders, validation, and codec (Phase 3)."""

from __future__ import annotations

import base64
import json

import pytest

from ghostlink.exceptions.messaging import MessageValidationError
from ghostlink.messaging.packets.frames import (
    FRAME_VERSION,
    MAX_FRAME_BYTES,
    Frame,
    FrameType,
    decode_frame,
    encode_frame,
    error_frame,
    kex_hello_frame,
    kex_reply_frame,
    message_ack_frame,
    message_frame,
    read_receipt_frame,
    typing_start_frame,
    typing_stop_frame,
)

PUB_HEX = "ab" * 32
NONCE_HEX = "cd" * 16
PROOF_B64 = base64.b64encode(b"p" * 40).decode("ascii")
CT_B64 = base64.b64encode(b"ciphertext-bytes").decode("ascii")


# ------------------------------------------------------------------- builders


class TestBuilders:
    def test_kex_hello(self) -> None:
        frame = kex_hello_frame({"pub": PUB_HEX, "nonce": NONCE_HEX})
        assert frame.type is FrameType.KEX_HELLO
        assert frame.data == {"pub": PUB_HEX, "nonce": NONCE_HEX}

    def test_builders_copy_payload(self) -> None:
        payload = {"pub": PUB_HEX, "nonce": NONCE_HEX}
        frame = kex_hello_frame(payload)
        payload["pub"] = "evil"
        assert frame.data["pub"] == PUB_HEX

    def test_message_frame_shape(self) -> None:
        frame = message_frame("msg_0123456789abcdef", 7, CT_B64)
        assert frame.type is FrameType.MESSAGE
        assert frame.sequence == 7
        assert frame.data == {"id": "msg_0123456789abcdef", "ct": CT_B64}

    def test_ack_frame_shape(self) -> None:
        frame = message_ack_frame("msg_0123456789abcdef", 7)
        assert frame.type is FrameType.MESSAGE_ACK
        assert frame.data == {"id": "msg_0123456789abcdef", "ack": 7}
        assert frame.sequence == 0

    def test_read_receipt_shape(self) -> None:
        frame = read_receipt_frame(12)
        assert frame.type is FrameType.READ_RECEIPT
        assert frame.data == {"up_to": 12}

    def test_typing_frames_have_empty_data(self) -> None:
        assert typing_start_frame().data == {}
        assert typing_stop_frame().data == {}

    def test_error_frame_shape(self) -> None:
        frame = error_frame("decrypt_failed", "integrity check failed")
        assert frame.type is FrameType.ERROR
        assert frame.data == {"code": "decrypt_failed", "message": "integrity check failed"}


# ------------------------------------------------------------------ round trip


class TestCodec:
    @pytest.mark.parametrize(
        "frame",
        [
            kex_hello_frame({"pub": PUB_HEX, "nonce": NONCE_HEX}),
            kex_reply_frame({"pub": PUB_HEX, "nonce": NONCE_HEX, "proof": PROOF_B64}),
            message_frame("msg_0123456789abcdef", 3, CT_B64),
            message_ack_frame("msg_0123456789abcdef", 3),
            read_receipt_frame(9),
            typing_start_frame(),
            typing_stop_frame(),
            error_frame("boom", "something failed"),
        ],
    )
    def test_round_trip_preserves_all_fields(self, frame: Frame) -> None:
        decoded = decode_frame(encode_frame(frame))
        assert decoded.type is frame.type
        assert decoded.data == frame.data
        assert decoded.sequence == frame.sequence
        assert decoded.frame_id == frame.frame_id
        assert decoded.sent_at == pytest.approx(frame.sent_at)

    def test_envelope_uses_compact_json(self) -> None:
        encoded = encode_frame(message_frame("msg_0123456789abcdef", 1, CT_B64))
        document = json.loads(encoded.decode("utf-8"))
        assert document["v"] == FRAME_VERSION
        assert document["t"] == "MESSAGE"
        assert document["seq"] == 1
        assert set(document) == {"v", "t", "seq", "id", "ts", "data"}
        assert b", " not in encoded  # compact separators

    def test_fresh_frame_ids(self) -> None:
        first, second = typing_start_frame(), typing_start_frame()
        assert first.frame_id != second.frame_id


# ----------------------------------------------------------------- validation


class TestDecodeValidation:
    def test_rejects_non_json(self) -> None:
        with pytest.raises(MessageValidationError, match="JSON"):
            decode_frame(b"{not json")

    def test_rejects_non_object_document(self) -> None:
        with pytest.raises(MessageValidationError, match="object"):
            decode_frame(json.dumps(["MESSAGE"]).encode())

    def test_rejects_wrong_version(self) -> None:
        frame = message_frame("msg_0123456789abcdef", 1, CT_B64).to_dict()
        frame["v"] = 99
        with pytest.raises(MessageValidationError, match="version"):
            decode_frame(json.dumps(frame).encode())

    def test_rejects_unknown_type(self) -> None:
        frame = typing_start_frame().to_dict()
        frame["t"] = "EXPLODE"
        with pytest.raises(MessageValidationError, match="unknown"):
            decode_frame(json.dumps(frame).encode())

    @pytest.mark.parametrize("bad_seq", [-1, 1.5, "3", True, None])
    def test_rejects_bad_sequence(self, bad_seq: object) -> None:
        frame = message_frame("msg_0123456789abcdef", 1, CT_B64).to_dict()
        frame["seq"] = bad_seq
        with pytest.raises(MessageValidationError, match="seq"):
            decode_frame(json.dumps(frame).encode())

    @pytest.mark.parametrize("bad_id", ["", "x" * 17, 42, None])
    def test_rejects_bad_frame_id(self, bad_id: object) -> None:
        frame = typing_start_frame().to_dict()
        frame["id"] = bad_id
        with pytest.raises(MessageValidationError, match="'id'"):
            decode_frame(json.dumps(frame).encode())

    @pytest.mark.parametrize("bad_ts", [0, -5, "now", True, None])
    def test_rejects_bad_timestamp(self, bad_ts: object) -> None:
        frame = typing_start_frame().to_dict()
        frame["ts"] = bad_ts
        with pytest.raises(MessageValidationError, match="'ts'"):
            decode_frame(json.dumps(frame).encode())

    def test_rejects_non_object_data(self) -> None:
        frame = typing_start_frame().to_dict()
        frame["data"] = ["not", "a", "dict"]
        with pytest.raises(MessageValidationError, match="'data'"):
            decode_frame(json.dumps(frame).encode())

    def test_rejects_oversized_frame(self) -> None:
        big = {"v": 1, "t": "TYPING_START", "seq": 0, "id": "x", "ts": 1, "data": {}}
        raw = json.dumps(big).encode()
        with pytest.raises(MessageValidationError, match="exceeds"):
            decode_frame(raw + b" " * MAX_FRAME_BYTES)


class TestPerTypeValidation:
    def test_hello_requires_key_and_nonce_hex(self) -> None:
        with pytest.raises(MessageValidationError, match="hex"):
            encode_frame(kex_hello_frame({"pub": "nope", "nonce": NONCE_HEX}))

    def test_hello_wrong_key_length(self) -> None:
        with pytest.raises(MessageValidationError, match="64"):
            encode_frame(kex_hello_frame({"pub": "ab" * 31, "nonce": NONCE_HEX}))

    def test_reply_requires_proof_base64(self) -> None:
        with pytest.raises(MessageValidationError, match="base64"):
            encode_frame(kex_reply_frame({"pub": PUB_HEX, "nonce": NONCE_HEX, "proof": "!!bad!!"}))

    def test_message_requires_message_id(self) -> None:
        with pytest.raises(MessageValidationError, match="'id'"):
            encode_frame(Frame(FrameType.MESSAGE, {"ct": CT_B64}))

    def test_message_ciphertext_size_cap(self) -> None:
        oversized = base64.b64encode(b"x" * 4096).decode("ascii")  # > 4096 chars
        with pytest.raises(MessageValidationError, match="4096"):
            encode_frame(message_frame("msg_0123456789abcdef", 1, oversized))

    def test_ack_requires_message_id_and_seq(self) -> None:
        with pytest.raises(MessageValidationError, match="'ack'"):
            encode_frame(Frame(FrameType.MESSAGE_ACK, {"id": "msg_0123456789abcdef"}))

    def test_read_receipt_requires_nonnegative_cursor(self) -> None:
        with pytest.raises(MessageValidationError, match="up_to"):
            encode_frame(read_receipt_frame(-1))

    def test_typing_frames_reject_payload(self) -> None:
        with pytest.raises(MessageValidationError, match="empty"):
            encode_frame(Frame(FrameType.TYPING_START, {"extra": True}))

    def test_error_frame_requires_code_and_message(self) -> None:
        with pytest.raises(MessageValidationError, match="'code'"):
            encode_frame(Frame(FrameType.ERROR, {"message": "where is the code"}))

    def test_encode_validates_too(self) -> None:
        frame = Frame(FrameType.MESSAGE, {"id": "msg_0123456789abcdef", "ct": CT_B64}, sequence=-2)
        with pytest.raises(MessageValidationError, match="seq"):
            encode_frame(frame)
