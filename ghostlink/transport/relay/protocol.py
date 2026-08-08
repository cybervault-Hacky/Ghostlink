"""Relay wire protocol: packet model, validation, and JSON codec (v2).

Every relay message is a JSON object:

    {"v": 2, "id": "<8 hex>", "type": "PING", "ts": 1754560800.123,
     "payload": { ... per-type fields ... }}

Inbound AND outbound packets pass through the same validators, so a malformed
message can never reach application code on either side of the wire.

Protocol v2 adds rendezvous routing: ATTACH/DETACH subscribe a client to a
channel, FORWARD routes one opaque (end-to-end encrypted) payload to the
channel counterparty, and PEER announces counterparty joins/leaves. The
relay routes bytes it cannot read — channel bodies are end-to-end ciphertext.

Protocol v3 adds one-time invites: INVITE_CREATE registers an invite with
the relay's invite authority, REDEEM consumes one atomically (admitting the
redeemer to the invite's room channel), INVITE_QUERY reports safe metadata,
and INVITE_REVOKE cancels. The authority enforces expiry, single use,
revocation, and session binding.
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

from ghostlink.constants.net import (
    CHANNEL_ROLES,
    INVITE_MAX_REDEMPTIONS,
    INVITE_MAX_TTL_SECONDS,
    INVITE_MIN_TTL_SECONDS,
    MAX_FORWARD_BODY_LENGTH,
    MAX_FORWARD_PAYLOAD_BYTES,
    MAX_INVITE_FIELD_LENGTH,
    MAX_MESSAGE_BYTES,
    PEER_EVENTS,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
)
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.transport import PacketValidationError
from ghostlink.models.room import is_valid_room_id

_logger = get_logger("transport.protocol")

MAX_CLIENT_NAME_LENGTH = 64
MAX_REASON_LENGTH = 256
MAX_ERROR_MESSAGE_LENGTH = 256
MAX_NONCE_LENGTH = 64
MAX_PACKET_ID_LENGTH = 16


class PacketType(str, Enum):
    """Relay protocol packet kinds (protocol version 3)."""

    HELLO = "HELLO"
    WELCOME = "WELCOME"
    PING = "PING"
    PONG = "PONG"
    DISCONNECT = "DISCONNECT"
    ERROR = "ERROR"
    HEARTBEAT = "HEARTBEAT"
    # Rendezvous routing (v2)
    ATTACH = "ATTACH"
    ATTACHED = "ATTACHED"
    DETACH = "DETACH"
    FORWARD = "FORWARD"
    PEER = "PEER"
    # One-time invite rendezvous (v3)
    INVITE_CREATE = "INVITE_CREATE"
    INVITE_GRANTED = "INVITE_GRANTED"
    INVITE_QUERY = "INVITE_QUERY"
    INVITE_STATE = "INVITE_STATE"
    INVITE_REVOKE = "INVITE_REVOKE"
    REDEEM = "REDEEM"
    REDEEMED = "REDEEMED"


@dataclass(frozen=True, slots=True)
class Packet:
    """One validated relay packet."""

    type: PacketType
    payload: dict[str, Any] = field(default_factory=dict)
    packet_id: str = field(default_factory=lambda: secrets.token_hex(4))
    sent_at: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": PROTOCOL_VERSION,
            "id": self.packet_id,
            "type": self.type.value,
            "ts": self.sent_at,
            "payload": self.payload,
        }


# --------------------------------------------------------------------- builders


def hello_packet(client_name: str, *, session_id: str | None = None) -> Packet:
    payload: dict[str, Any] = {
        "protocol": PROTOCOL_VERSION,
        "client": client_name,
    }
    if session_id:
        payload["session_id"] = session_id
    return Packet(PacketType.HELLO, payload)


def welcome_packet(
    session_id: str,
    *,
    server_name: str,
    heartbeat_interval_seconds: float,
) -> Packet:
    return Packet(
        PacketType.WELCOME,
        {
            "session_id": session_id,
            "server": server_name,
            "server_time": time.time(),
            "heartbeat_interval": heartbeat_interval_seconds,
            "protocol": PROTOCOL_VERSION,
        },
    )


def ping_packet(nonce: str, *, sent_at: float | None = None) -> Packet:
    return Packet(
        PacketType.PING,
        {"nonce": nonce, "sent_at": sent_at if sent_at is not None else time.time()},
    )


def pong_packet(nonce: str, *, sent_at: float) -> Packet:
    return Packet(
        PacketType.PONG,
        {"nonce": nonce, "sent_at": sent_at, "answered_at": time.time()},
    )


def heartbeat_packet(sequence: int) -> Packet:
    return Packet(PacketType.HEARTBEAT, {"seq": sequence, "timestamp": time.time()})


def disconnect_packet(reason: str, *, code: int = 1000) -> Packet:
    return Packet(PacketType.DISCONNECT, {"reason": reason, "code": code})


def error_packet(code: str, message: str) -> Packet:
    return Packet(PacketType.ERROR, {"code": code, "message": message})


# ------------------------------------------------------- rendezvous (v2)


def attach_packet(channel: str, role: str) -> Packet:
    return Packet(PacketType.ATTACH, {"channel": channel, "role": role})


def attached_packet(channel: str, role: str, peers: int) -> Packet:
    return Packet(PacketType.ATTACHED, {"channel": channel, "role": role, "peers": peers})


def detach_packet(channel: str) -> Packet:
    return Packet(PacketType.DETACH, {"channel": channel})


def forward_packet(channel: str, body: str) -> Packet:
    return Packet(PacketType.FORWARD, {"channel": channel, "body": body})


def peer_packet(channel: str, event: str) -> Packet:
    return Packet(PacketType.PEER, {"channel": channel, "event": event})


# ---------------------------------------------- one-time invites (v3)


def invite_create_packet(token: str, room_id: str, ttl_seconds: float, uses: int) -> Packet:
    return Packet(
        PacketType.INVITE_CREATE,
        {"token": token, "room": room_id, "ttl": ttl_seconds, "uses": uses},
    )


def invite_granted_packet(invite_id: str, expires_at: float, uses: int) -> Packet:
    return Packet(
        PacketType.INVITE_GRANTED,
        {"invite": invite_id, "expires_at": expires_at, "uses": uses},
    )


def invite_query_packet(token: str) -> Packet:
    return Packet(PacketType.INVITE_QUERY, {"token": token})


def invite_state_packet(
    invite_id: str,
    state: str,
    *,
    uses: int,
    max_uses: int,
    expires_in: float,
    room_id: str | None = None,
) -> Packet:
    payload: dict[str, Any] = {
        "invite": invite_id,
        "state": state,
        "uses": uses,
        "max_uses": max_uses,
        "expires_in": max(0.0, expires_in),
    }
    if room_id is not None:
        payload["room"] = room_id
    return Packet(PacketType.INVITE_STATE, payload)


def invite_revoke_packet(token: str) -> Packet:
    return Packet(PacketType.INVITE_REVOKE, {"token": token})


def redeem_packet(token: str) -> Packet:
    return Packet(PacketType.REDEEM, {"token": token})


def redeemed_packet(invite_id: str, room_id: str, expires_at: float) -> Packet:
    return Packet(
        PacketType.REDEEMED,
        {"invite": invite_id, "room": room_id, "expires_at": expires_at},
    )


INVITE_STATES: frozenset[str] = frozenset({"active", "expired", "redeemed", "revoked"})


# ------------------------------------------------------------------- validation


def _fail(reason: str) -> NoReturn:
    _logger.debug("packet validation failed: %s", reason)
    raise PacketValidationError(
        f"Packet validation failed: {reason}.",
        hint="The peer sent a message that does not match relay protocol v2.",
    )


def _require(condition: bool, reason: str) -> None:
    if not condition:
        _fail(reason)


def _require_str(payload: dict[str, Any], field: str, *, max_length: int) -> str:
    value = payload.get(field)
    if not (isinstance(value, str) and 0 < len(value) <= max_length):
        _fail(f"'{field}' must be a string of 1..{max_length} characters")
    return value


def _require_number(payload: dict[str, Any], field: str) -> float:
    value = payload.get(field)
    if not (isinstance(value, int | float) and not isinstance(value, bool)):
        _fail(f"'{field}' must be a number")
    return float(value)


def _require_int(payload: dict[str, Any], field: str, *, minimum: int) -> int:
    value = payload.get(field)
    if not (isinstance(value, int) and not isinstance(value, bool) and value >= minimum):
        _fail(f"'{field}' must be an integer ≥ {minimum}")
    return value


def validate_packet(packet_type: PacketType, payload: dict[str, Any]) -> None:
    """Validate ``payload`` against the schema of ``packet_type``."""

    _require(isinstance(payload, dict), "payload must be a JSON object")

    if packet_type is PacketType.HELLO:
        protocol = _require_int(payload, "protocol", minimum=1)
        _require(
            protocol in SUPPORTED_PROTOCOL_VERSIONS,
            f"unsupported protocol version {protocol}",
        )
        _require_str(payload, "client", max_length=MAX_CLIENT_NAME_LENGTH)
        if "session_id" in payload:
            _require_str(payload, "session_id", max_length=64)
    elif packet_type is PacketType.WELCOME:
        _require_str(payload, "session_id", max_length=64)
        _require_str(payload, "server", max_length=MAX_CLIENT_NAME_LENGTH)
        _require_number(payload, "server_time")
        _require_number(payload, "heartbeat_interval")
        protocol = _require_int(payload, "protocol", minimum=1)
        _require(
            protocol in SUPPORTED_PROTOCOL_VERSIONS,
            f"unsupported protocol version {protocol}",
        )
    elif packet_type is PacketType.PING:
        _require_str(payload, "nonce", max_length=MAX_NONCE_LENGTH)
        _require_number(payload, "sent_at")
    elif packet_type is PacketType.PONG:
        _require_str(payload, "nonce", max_length=MAX_NONCE_LENGTH)
        _require_number(payload, "sent_at")
        _require_number(payload, "answered_at")
    elif packet_type is PacketType.DISCONNECT:
        _require_str(payload, "reason", max_length=MAX_REASON_LENGTH)
        _require_int(payload, "code", minimum=0)
    elif packet_type is PacketType.ERROR:
        _require_str(payload, "code", max_length=64)
        _require_str(payload, "message", max_length=MAX_ERROR_MESSAGE_LENGTH)
    elif packet_type is PacketType.HEARTBEAT:
        _require_int(payload, "seq", minimum=0)
        _require_number(payload, "timestamp")
    elif packet_type is PacketType.ATTACH:
        _require_channel(payload)
        role = _require_str(payload, "role", max_length=8)
        _require(role in CHANNEL_ROLES, "'role' must be 'host' or 'guest'")
    elif packet_type is PacketType.ATTACHED:
        _require_channel(payload)
        role = _require_str(payload, "role", max_length=8)
        _require(role in CHANNEL_ROLES, "'role' must be 'host' or 'guest'")
        _require_int(payload, "peers", minimum=0)
    elif packet_type is PacketType.DETACH:
        _require_channel(payload)
    elif packet_type is PacketType.FORWARD:
        _require_channel(payload)
        body = _require_str(payload, "body", max_length=MAX_FORWARD_BODY_LENGTH)
        _require_forward_body(body)
    elif packet_type is PacketType.PEER:
        _require_channel(payload)
        event = _require_str(payload, "event", max_length=8)
        _require(event in PEER_EVENTS, "'event' must be 'joined' or 'left'")
    elif packet_type is PacketType.INVITE_CREATE:
        _require_str(payload, "token", max_length=MAX_INVITE_FIELD_LENGTH)
        _require_room_field(payload)
        ttl = _require_number(payload, "ttl")
        _require(
            INVITE_MIN_TTL_SECONDS <= ttl <= INVITE_MAX_TTL_SECONDS,
            f"'ttl' must be {INVITE_MIN_TTL_SECONDS}..{INVITE_MAX_TTL_SECONDS} seconds",
        )
        uses = _require_int(payload, "uses", minimum=1)
        _require(uses <= INVITE_MAX_REDEMPTIONS, f"'uses' must be ≤ {INVITE_MAX_REDEMPTIONS}")
    elif packet_type is PacketType.INVITE_GRANTED:
        _require_str(payload, "invite", max_length=MAX_INVITE_FIELD_LENGTH)
        _require_number(payload, "expires_at")
        _require_int(payload, "uses", minimum=1)
    elif packet_type is PacketType.INVITE_QUERY:
        _require_str(payload, "token", max_length=MAX_INVITE_FIELD_LENGTH)
    elif packet_type is PacketType.INVITE_STATE:
        _require_str(payload, "invite", max_length=MAX_INVITE_FIELD_LENGTH)
        state = _require_str(payload, "state", max_length=16)
        _require(state in INVITE_STATES, f"'state' must be one of {sorted(INVITE_STATES)}")
        _require_int(payload, "uses", minimum=0)
        _require_int(payload, "max_uses", minimum=1)
        _require_number(payload, "expires_in")
        if "room" in payload:
            _require_room_field(payload)
    elif packet_type is PacketType.INVITE_REVOKE or packet_type is PacketType.REDEEM:
        _require_str(payload, "token", max_length=MAX_INVITE_FIELD_LENGTH)
    elif packet_type is PacketType.REDEEMED:
        _require_str(payload, "invite", max_length=MAX_INVITE_FIELD_LENGTH)
        _require_room_field(payload)
        _require_number(payload, "expires_at")


def _require_channel(payload: dict[str, Any]) -> None:
    channel = _require_str(payload, "channel", max_length=64)
    _require(is_valid_room_id(channel), "'channel' is not a valid room identifier")


def _require_room_field(payload: dict[str, Any]) -> None:
    room = _require_str(payload, "room", max_length=64)
    _require(is_valid_room_id(room), "'room' is not a valid room identifier")


def _require_forward_body(body: str) -> None:
    try:
        raw = base64.b64decode(body.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError):
        _fail("'body' must be base64")
    _require(
        0 < len(raw) <= MAX_FORWARD_PAYLOAD_BYTES,
        f"'body' must decode to 1..{MAX_FORWARD_PAYLOAD_BYTES} bytes",
    )


# ------------------------------------------------------------------- codec


def encode_packet(packet: Packet) -> bytes:
    """Validate and serialize a packet into compact JSON bytes."""

    validate_packet(packet.type, packet.payload)
    encoded = json.dumps(packet.to_dict(), separators=(",", ":"), ensure_ascii=True).encode("utf-8")
    _require(
        len(encoded) <= MAX_MESSAGE_BYTES,
        f"encoded packet exceeds {MAX_MESSAGE_BYTES} bytes",
    )
    return encoded


def decode_packet(raw: bytes, *, peer: str = "peer") -> Packet:
    """Parse and strictly validate one inbound packet."""

    if len(raw) > MAX_MESSAGE_BYTES:
        _fail(f"inbound message exceeds {MAX_MESSAGE_BYTES} bytes")
    try:
        document = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        _logger.debug("packet from %s is not valid JSON: %s", peer, exc)
        raise PacketValidationError(
            "Inbound packet is not valid JSON.",
            hint="The peer sent bytes outside the relay protocol.",
        ) from exc
    _require(isinstance(document, dict), "packet must be a JSON object")

    version = document.get("v")
    _require(version in SUPPORTED_PROTOCOL_VERSIONS, f"unsupported envelope version {version!r}")

    packet_id = document.get("id")
    if not (isinstance(packet_id, str) and 1 <= len(packet_id) <= MAX_PACKET_ID_LENGTH):
        _fail("'id' must be a short string")
    type_name = document.get("type")
    try:
        packet_type = PacketType(type_name)
    except ValueError:
        _fail(f"unknown packet type {type_name!r}")

    sent_at = document.get("ts")
    if not (isinstance(sent_at, int | float) and not isinstance(sent_at, bool) and sent_at > 0):
        _fail("'ts' must be a positive timestamp")
    payload = document.get("payload")
    if not isinstance(payload, dict):
        _fail("'payload' must be an object")

    validate_packet(packet_type, payload)
    return Packet(
        type=packet_type,
        payload=payload,
        packet_id=packet_id,
        sent_at=float(sent_at),
    )
