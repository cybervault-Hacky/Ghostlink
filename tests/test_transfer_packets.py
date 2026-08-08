"""FILE_* frame builders and their wire validation rules (Phase 4)."""

from __future__ import annotations

import base64

import pytest

from ghostlink.exceptions.messaging import MessageValidationError
from ghostlink.messaging.packets.frames import (
    FILE_FRAME_TYPES,
    Frame,
    FrameType,
    decode_frame,
    encode_frame,
)
from ghostlink.transfer import packets

TID = "tf_ab12cd34"


def _sealed(length: int) -> bytes:
    return bytes(index % 256 for index in range(length))


class TestBuilders:
    def test_offer_frame(self) -> None:
        frame = packets.file_offer_frame(TID, _sealed(64))
        assert frame.type is FrameType.FILE_OFFER
        assert frame.data["id"] == TID
        assert base64.b64decode(str(frame.data["ct"])) == _sealed(64)
        encode_frame(frame)  # validates

    def test_accept_frame_with_and_without_bitmap(self) -> None:
        plain = packets.file_accept_frame(TID)
        assert set(plain.data) == {"id"}
        encode_frame(plain)
        resumed = packets.file_accept_frame(TID, bitmap_b64=base64.b64encode(b"\xff").decode())
        assert resumed.data["bm"]
        encode_frame(resumed)

    def test_reject_frame(self) -> None:
        encode_frame(packets.file_reject_frame(TID))
        frame = packets.file_reject_frame(TID, reason="no space")
        assert frame.data["reason"] == "no space"
        encode_frame(frame)

    def test_chunk_and_ack_frames(self) -> None:
        chunk = packets.file_chunk_frame(TID, 3, _sealed(1052))
        assert chunk.data["n"] == 3
        encode_frame(chunk)
        ack = packets.file_ack_frame(TID, 3)
        encode_frame(ack)

    def test_lifecycle_frames(self) -> None:
        for frame in (
            packets.file_pause_frame(TID),
            packets.file_resume_frame(TID),
            packets.file_cancel_frame(TID),
            packets.file_cancel_frame(TID, reason="changed my mind"),
            packets.file_complete_frame(TID, "ab" * 32),
            packets.file_error_frame(TID, "integrity", "chunk failed verification"),
        ):
            encode_frame(frame)

    def test_round_trip_through_codec(self) -> None:
        frame = packets.file_chunk_frame(TID, 9, _sealed(4124))
        clone = decode_frame(encode_frame(frame))
        assert clone.type is FrameType.FILE_CHUNK
        assert clone.data["id"] == TID
        assert clone.data["n"] == 9


class TestValidation:
    def _encode(self, frame_type: FrameType, data: dict) -> bytes:
        return encode_frame(Frame(frame_type, data))

    def test_offer_ct_must_be_base64(self) -> None:
        with pytest.raises(MessageValidationError):
            self._encode(FrameType.FILE_OFFER, {"id": TID, "ct": "!!!"})

    def test_offer_ct_size_bounded(self) -> None:
        with pytest.raises(MessageValidationError):
            self._encode(
                FrameType.FILE_OFFER,
                {"id": TID, "ct": base64.b64encode(_sealed(2000)).decode()},
            )

    def test_chunk_ct_size_bounded(self) -> None:
        with pytest.raises(MessageValidationError):
            self._encode(
                FrameType.FILE_CHUNK,
                {"id": TID, "n": 1, "ct": base64.b64encode(_sealed(5000)).decode()},
            )

    def test_chunk_number_minimum(self) -> None:
        with pytest.raises(MessageValidationError):
            self._encode(FrameType.FILE_CHUNK, {"id": TID, "n": 0, "ct": "QUJD"})
        with pytest.raises(MessageValidationError):
            self._encode(FrameType.FILE_ACK, {"id": TID, "n": 0})

    def test_transfer_id_length_bounded(self) -> None:
        with pytest.raises(MessageValidationError):
            self._encode(FrameType.FILE_PAUSE, {"id": "tf_" + "a" * 40})

    def test_pause_carries_no_other_fields(self) -> None:
        with pytest.raises(MessageValidationError):
            self._encode(FrameType.FILE_PAUSE, {"id": TID, "extra": "x"})

    def test_cancel_reason_bounded(self) -> None:
        with pytest.raises(MessageValidationError):
            self._encode(FrameType.FILE_CANCEL, {"id": TID, "reason": "x" * 65})

    def test_complete_requires_64_hex(self) -> None:
        with pytest.raises(MessageValidationError):
            self._encode(FrameType.FILE_COMPLETE, {"id": TID, "sha256": "abcd"})
        with pytest.raises(MessageValidationError):
            self._encode(FrameType.FILE_COMPLETE, {"id": TID, "sha256": "zz" * 32})

    def test_error_frame_shape(self) -> None:
        with pytest.raises(MessageValidationError):
            self._encode(
                FrameType.FILE_ERROR,
                {"id": TID, "code": "x", "message": "y", "payload": "z" * 9000},
            )
        with pytest.raises(MessageValidationError):
            self._encode(FrameType.FILE_ERROR, {"id": TID, "code": "x"})

    def test_bitmap_must_be_base64(self) -> None:
        with pytest.raises(MessageValidationError):
            self._encode(FrameType.FILE_ACCEPT, {"id": TID, "bm": "!!!"})

    def test_every_file_type_validates_through_decode(self) -> None:
        frames = [
            packets.file_offer_frame(TID, _sealed(64)),
            packets.file_accept_frame(TID),
            packets.file_reject_frame(TID),
            packets.file_chunk_frame(TID, 1, _sealed(64)),
            packets.file_ack_frame(TID, 1),
            packets.file_pause_frame(TID),
            packets.file_resume_frame(TID),
            packets.file_cancel_frame(TID),
            packets.file_complete_frame(TID, "ab" * 32),
            packets.file_error_frame(TID, "code", "message"),
        ]
        assert {frame.type for frame in frames} == set(FILE_FRAME_TYPES)
        for frame in frames:
            clone = decode_frame(encode_frame(frame))
            assert clone.type is frame.type
