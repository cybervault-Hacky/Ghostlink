"""Secure-channel frames (Phase 3).

Small, strictly validated JSON frames exchanged *inside* relay FORWARD
bodies between the two conversation peers. The relay only ever sees these
frames as base64 ciphertext — it routes them without understanding them.

Envelope:  {"v": 1, "t": "MESSAGE", "seq": 3, "data": { … per-type fields }}

Frame kinds:

    KEX_HELLO / KEX_REPLY   key exchange (cleartext DH material, AEAD proof)
    MESSAGE                 one encrypted chat message (AEAD ciphertext)
    MESSAGE_ACK             delivery acknowledgement for a message
    READ_RECEIPT            read acknowledgement (cumulative, up to seq)
    TYPING_START / TYPING_STOP   typing indicators (no content)
    ERROR                   protocol-level failure notice for the peer

Inbound and outbound frames pass through the same validators. Nothing here
logs plaintext or ciphertext — only frames, kinds and sequence numbers.
"""

from __future__ import annotations

import base64
import binascii
import json
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, NoReturn

from ghostlink.constants.net import MAX_FORWARD_PAYLOAD_BYTES
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.messaging import MessageValidationError

_logger = get_logger("messaging.frames")

FRAME_VERSION: int = 1
MAX_FRAME_BYTES: int = MAX_FORWARD_PAYLOAD_BYTES
MAX_MESSAGE_ID_LENGTH: int = 32
MAX_SESSION_ID_LENGTH: int = 32
MAX_KEY_HEX_LENGTH: int = 64
MAX_HELLO_NONCE_HEX: int = 32
MAX_PROOF_B64_LENGTH: int = 128
MAX_CIPHERTEXT_B64_LENGTH: int = 4096
MAX_ERROR_MESSAGE_LENGTH: int = 256
MAX_CHANNEL_LENGTH: int = 64
MAX_PEER_NAME_LENGTH: int = 24
MAX_TRANSFER_ID_LENGTH: int = 24
MAX_OFFER_B64_LENGTH: int = 1200
MAX_CHUNK_B64_LENGTH: int = 5600
MAX_BITMAP_B64_LENGTH: int = 5600
MAX_CANCEL_REASON_LENGTH: int = 64


class FrameType(str, Enum):
    """Secure-channel frame kinds (frame version 1)."""

    KEX_HELLO = "KEX_HELLO"
    KEX_REPLY = "KEX_REPLY"
    MESSAGE = "MESSAGE"
    MESSAGE_ACK = "MESSAGE_ACK"
    READ_RECEIPT = "READ_RECEIPT"
    TYPING_START = "TYPING_START"
    TYPING_STOP = "TYPING_STOP"
    ERROR = "ERROR"
    FILE_OFFER = "FILE_OFFER"
    FILE_ACCEPT = "FILE_ACCEPT"
    FILE_REJECT = "FILE_REJECT"
    FILE_CHUNK = "FILE_CHUNK"
    FILE_ACK = "FILE_ACK"
    FILE_PAUSE = "FILE_PAUSE"
    FILE_RESUME = "FILE_RESUME"
    FILE_CANCEL = "FILE_CANCEL"
    FILE_COMPLETE = "FILE_COMPLETE"
    FILE_ERROR = "FILE_ERROR"


FILE_FRAME_TYPES: frozenset[FrameType] = frozenset(
    {
        FrameType.FILE_OFFER,
        FrameType.FILE_ACCEPT,
        FrameType.FILE_REJECT,
        FrameType.FILE_CHUNK,
        FrameType.FILE_ACK,
        FrameType.FILE_PAUSE,
        FrameType.FILE_RESUME,
        FrameType.FILE_CANCEL,
        FrameType.FILE_COMPLETE,
        FrameType.FILE_ERROR,
    }
)


@dataclass(frozen=True, slots=True)
class Frame:
    """One validated secure-channel frame."""

    type: FrameType
    data: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0
    frame_id: str = field(default_factory=lambda: secrets.token_hex(6))
    sent_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": FRAME_VERSION,
            "t": self.type.value,
            "seq": self.sequence,
            "id": self.frame_id,
            "ts": self.sent_at,
            "data": self.data,
        }


# --------------------------------------------------------------------- builders


def kex_hello_frame(payload: dict[str, str]) -> Frame:
    return Frame(FrameType.KEX_HELLO, dict(payload))


def kex_reply_frame(payload: dict[str, str]) -> Frame:
    return Frame(FrameType.KEX_REPLY, dict(payload))


def message_frame(message_id: str, sequence: int, ciphertext_b64: str) -> Frame:
    return Frame(
        FrameType.MESSAGE,
        {"id": message_id, "ct": ciphertext_b64},
        sequence=sequence,
    )


def message_ack_frame(message_id: str, sequence: int) -> Frame:
    return Frame(
        FrameType.MESSAGE_ACK,
        {"id": message_id, "ack": sequence},
    )


def read_receipt_frame(up_to_sequence: int) -> Frame:
    return Frame(FrameType.READ_RECEIPT, {"up_to": up_to_sequence})


def typing_start_frame() -> Frame:
    return Frame(FrameType.TYPING_START, {})


def typing_stop_frame() -> Frame:
    return Frame(FrameType.TYPING_STOP, {})


def error_frame(code: str, message: str) -> Frame:
    return Frame(FrameType.ERROR, {"code": code, "message": message})


# ------------------------------------------------------------------- validation


def _fail(reason: str) -> NoReturn:
    _logger.debug("frame validation failed: %s", reason)
    raise MessageValidationError(
        f"Frame validation failed: {reason}.",
        hint="The peer sent a secure-channel frame outside protocol v1.",
    )


def _require(condition: bool, reason: str) -> None:
    if not condition:
        _fail(reason)


def _require_str(data: dict[str, Any], field: str, *, max_length: int) -> str:
    value = data.get(field)
    if not (isinstance(value, str) and 0 < len(value) <= max_length):
        _fail(f"'{field}' must be a string of 1..{max_length} characters")
    return value


def _require_int(data: dict[str, Any], field: str, *, minimum: int) -> int:
    value = data.get(field)
    if not (isinstance(value, int) and not isinstance(value, bool) and value >= minimum):
        _fail(f"'{field}' must be an integer ≥ {minimum}")
    return value


def _require_hex(data: dict[str, Any], field: str, *, length_hex: int) -> None:
    value = data.get(field)
    if not isinstance(value, str) or len(value) != length_hex:
        _fail(f"'{field}' must be {length_hex} hex characters")
    try:
        bytes.fromhex(value)
    except ValueError:
        _fail(f"'{field}' is not hexadecimal")


def _require_b64(data: dict[str, Any], field: str, *, max_length: int) -> None:
    value = _require_str(data, field, max_length=max_length)
    try:
        base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError):
        _fail(f"'{field}' must be base64")


def _require_optional_name(data: dict[str, Any]) -> None:
    """Handshake display names are optional cosmetics, capped and printable."""

    value = data.get("name")
    if value is None:
        return
    if not (isinstance(value, str) and 0 < len(value) <= MAX_PEER_NAME_LENGTH):
        _fail(f"'name' must be a string of 1..{MAX_PEER_NAME_LENGTH} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        _fail("'name' must be printable text")


def _require_optional_idpub(data: dict[str, Any]) -> None:
    """Handshake identity public keys ('idpub') are optional 32-byte hex."""

    value = data.get("idpub")
    if value is None:
        return
    _require_hex(data, "idpub", length_hex=MAX_KEY_HEX_LENGTH)


def _require_transfer_id(data: dict[str, Any]) -> None:
    value = data.get("id")
    if not (isinstance(value, str) and 1 <= len(value) <= MAX_TRANSFER_ID_LENGTH):
        _fail(f"transfer 'id' must be a string of 1..{MAX_TRANSFER_ID_LENGTH} characters")


def _require_optional_reason(data: dict[str, Any]) -> None:
    value = data.get("reason")
    if value is not None and not (
        isinstance(value, str) and 0 < len(value) <= MAX_CANCEL_REASON_LENGTH
    ):
        _fail(f"'reason' must be a string of 1..{MAX_CANCEL_REASON_LENGTH} characters")


def _require_optional_bitmap(data: dict[str, Any]) -> None:
    value = data.get("bm")
    if value is None:
        return
    if not (isinstance(value, str) and len(value) <= MAX_BITMAP_B64_LENGTH):
        _fail(f"'bm' must be at most {MAX_BITMAP_B64_LENGTH} characters")
    try:
        base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError):
        _fail("'bm' must be base64")


def _require_transfer(data: dict[str, Any], frame_type: FrameType) -> None:
    """Schema checks for file-transfer frames (Phase 4).

    Only presence, types, lengths, and alphabets live here; semantic rules
    (size limits, manifest cross-checks, chunk/decryption verification) are
    enforced by the transfer layer before any processing."""

    _require_transfer_id(data)
    if frame_type is FrameType.FILE_OFFER:
        _require_b64(data, "ct", max_length=MAX_OFFER_B64_LENGTH)
    elif frame_type is FrameType.FILE_ACCEPT:
        _require_optional_bitmap(data)
    elif frame_type is FrameType.FILE_REJECT:
        _require_optional_reason(data)
    elif frame_type is FrameType.FILE_CHUNK:
        _require_int(data, "n", minimum=1)
        _require_b64(data, "ct", max_length=MAX_CHUNK_B64_LENGTH)
    elif frame_type is FrameType.FILE_ACK:
        _require_int(data, "n", minimum=1)
    elif frame_type in (FrameType.FILE_PAUSE, FrameType.FILE_RESUME):
        _require(set(data) == {"id"}, f"'data' for {frame_type.value} must hold 'id' only")
    elif frame_type is FrameType.FILE_CANCEL:
        _require_optional_reason(data)
        _require(
            set(data) <= {"id", "reason"},
            f"'data' for {frame_type.value} must hold 'id'/'reason' only",
        )
    elif frame_type is FrameType.FILE_COMPLETE:
        _require_hex(data, "sha256", length_hex=64)
    elif frame_type is FrameType.FILE_ERROR:
        _require_str(data, "code", max_length=64)
        _require_str(data, "message", max_length=MAX_ERROR_MESSAGE_LENGTH)
        _require(
            set(data) <= {"id", "code", "message"},
            f"'data' for {frame_type.value} must hold 'id'/'code'/'message' only",
        )


def validate_frame(frame_type: FrameType, sequence: int, data: dict[str, Any]) -> None:
    """Validate ``data`` against the schema of ``frame_type``."""

    _require(isinstance(data, dict), "'data' must be a JSON object")
    _require(
        isinstance(sequence, int) and not isinstance(sequence, bool) and sequence >= 0,
        "'seq' must be an integer ≥ 0",
    )

    if frame_type is FrameType.KEX_HELLO:
        _require_hex(data, "pub", length_hex=MAX_KEY_HEX_LENGTH)
        _require_hex(data, "nonce", length_hex=MAX_HELLO_NONCE_HEX)
        _require_optional_name(data)
        _require_optional_idpub(data)
    elif frame_type is FrameType.KEX_REPLY:
        _require_hex(data, "pub", length_hex=MAX_KEY_HEX_LENGTH)
        _require_hex(data, "nonce", length_hex=MAX_HELLO_NONCE_HEX)
        _require_b64(data, "proof", max_length=MAX_PROOF_B64_LENGTH)
        _require_optional_name(data)
        _require_optional_idpub(data)
    elif frame_type is FrameType.MESSAGE:
        _require_str(data, "id", max_length=MAX_MESSAGE_ID_LENGTH)
        _require_b64(data, "ct", max_length=MAX_CIPHERTEXT_B64_LENGTH)
    elif frame_type is FrameType.MESSAGE_ACK:
        _require_str(data, "id", max_length=MAX_MESSAGE_ID_LENGTH)
        _require_int(data, "ack", minimum=0)
    elif frame_type is FrameType.READ_RECEIPT:
        _require_int(data, "up_to", minimum=0)
    elif frame_type is FrameType.ERROR:
        _require_str(data, "code", max_length=64)
        _require_str(data, "message", max_length=MAX_ERROR_MESSAGE_LENGTH)
    elif frame_type in (FrameType.TYPING_START, FrameType.TYPING_STOP):
        _require(data == {}, f"'data' for {frame_type.value} must be empty")
    elif frame_type in FILE_FRAME_TYPES:
        _require_transfer(data, frame_type)


# ------------------------------------------------------------------- codec


def encode_frame(frame: Frame) -> bytes:
    """Validate and serialize one frame into compact JSON bytes."""

    validate_frame(frame.type, frame.sequence, frame.data)
    encoded = json.dumps(frame.to_dict(), separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    _require(
        len(encoded) <= MAX_FRAME_BYTES,
        f"encoded frame exceeds {MAX_FRAME_BYTES} bytes",
    )
    return encoded


def decode_frame(raw: bytes, *, peer: str = "peer") -> Frame:
    """Parse and strictly validate one inbound secure-channel frame."""

    if len(raw) > MAX_FRAME_BYTES:
        _fail(f"inbound frame exceeds {MAX_FRAME_BYTES} bytes")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _logger.debug("frame from %s is not valid JSON: %s", peer, exc)
        raise MessageValidationError(
            "Inbound frame is not valid JSON.",
            hint="The peer sent bytes outside the secure-channel protocol.",
        ) from exc
    _require(isinstance(document, dict), "frame must be a JSON object")
    _require(document.get("v") == FRAME_VERSION, "unsupported frame version")

    type_name = document.get("t")
    try:
        frame_type = FrameType(type_name)
    except ValueError:
        _fail(f"unknown frame type {type_name!r}")
    sequence = document.get("seq")
    _require(
        isinstance(sequence, int) and not isinstance(sequence, bool) and sequence >= 0,
        "'seq' must be an integer ≥ 0",
    )
    frame_id = document.get("id")
    if not (isinstance(frame_id, str) and 1 <= len(frame_id) <= 16):
        _fail("'id' must be a short string")
    sent_at = document.get("ts")
    if not (isinstance(sent_at, int | float) and not isinstance(sent_at, bool) and sent_at > 0):
        _fail("'ts' must be a positive timestamp")
    data = document.get("data")
    _require(isinstance(data, dict), "'data' must be an object")

    validate_frame(frame_type, sequence, data)
    return Frame(
        type=frame_type,
        data=dict(data),
        sequence=sequence,
        frame_id=frame_id,
        sent_at=float(sent_at),
    )
