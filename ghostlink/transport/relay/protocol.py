"""Relay wire protocol: packet model, validation, and JSON codec (v2+).

Every relay message is a JSON object:

    {"v": 4, "id": "<8 hex>", "type": "PING", "ts": 1754560800.123,
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

Protocol v4 adds secure-group lifecycle (Phase 6B): GROUP_CREATE (owner
proof-of-possession), GROUP_ATTEST (identity↔session binding), GROUP_STATE
re-sync, GROUP_LEAVE / GROUP_REMOVE / GROUP_DISSOLVE (two-phase signed
commits via GROUP_SIGN_REQUEST / GROUP_SIGN), GROUP_EVENT broadcasts of
owner/member-signed roster mutations, and — Phase 6C — GROUP_FORWARD:
addressed routing of opaque end-to-end group envelopes (pairwise-mesh
sealed frames and the handshake payloads that build the links). The relay
validates framing/authorization/size only; bodies are ciphertext it cannot
read. Group invites reuse the v3 invite packets with kind=group; the
authority enforces roster capacity atomically at redemption.
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
    GROUP_DISPLAY_NAME_MAX_LEN,
    GROUP_EVENTS_KEPT,
    GROUP_FORWARD_BODY_MAX,
    GROUP_KEX_BODY_MAX,
    GROUP_NAME_MAX_LEN,
    GROUP_OP_ID_LENGTH,
    GROUP_PUBKEY_HEX_LENGTH,
    GROUP_SIGNATURE_B64_MAX,
    INVITE_MAX_REDEMPTIONS,
    INVITE_MAX_TTL_SECONDS,
    INVITE_MIN_TTL_SECONDS,
    MAX_FORWARD_BODY_LENGTH,
    MAX_FORWARD_PAYLOAD_BYTES,
    MAX_GROUP_MEMBERS,
    MAX_INVITE_FIELD_LENGTH,
    MAX_MESSAGE_BYTES,
    PEER_EVENTS,
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
)
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.transport import PacketValidationError
from ghostlink.groups.events import EVENT_KINDS
from ghostlink.groups.frames import GROUP_FORWARD_KINDS, KIND_MSG
from ghostlink.groups.ids import is_valid_group_id
from ghostlink.identity.fingerprint import is_valid_fingerprint
from ghostlink.models.room import is_valid_room_id

_logger = get_logger("transport.protocol")

MAX_CLIENT_NAME_LENGTH = 64
MAX_REASON_LENGTH = 256
MAX_ERROR_MESSAGE_LENGTH = 256
MAX_NONCE_LENGTH = 64
MAX_PACKET_ID_LENGTH = 16

# base64 ceiling for a GROUP_FORWARD body (6144 raw → 8192 b64 + slack).
_GROUP_FORWARD_BODY_B64_MAX = 8240

INVITE_KINDS: frozenset[str] = frozenset({"chat", "group"})
GROUP_ROLES: frozenset[str] = frozenset({"owner", "member"})
GROUP_ATTEST_ROLES: frozenset[str] = frozenset({"owner", "member", "candidate"})
GROUP_STATES: frozenset[str] = frozenset({"active", "dissolved"})


class PacketType(str, Enum):
    """Relay protocol packet kinds (protocol version 4)."""

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
    # Secure-group lifecycle (v4)
    GROUP_CREATE = "GROUP_CREATE"
    GROUP_GRANTED = "GROUP_GRANTED"
    GROUP_ATTEST = "GROUP_ATTEST"
    GROUP_ATTESTED = "GROUP_ATTESTED"
    GROUP_STATE = "GROUP_STATE"
    GROUP_ROSTER = "GROUP_ROSTER"
    GROUP_LEAVE = "GROUP_LEAVE"
    GROUP_REMOVE = "GROUP_REMOVE"
    GROUP_DISSOLVE = "GROUP_DISSOLVE"
    GROUP_SIGN_REQUEST = "GROUP_SIGN_REQUEST"
    GROUP_SIGN = "GROUP_SIGN"
    GROUP_EVENT = "GROUP_EVENT"
    GROUP_FORWARD = "GROUP_FORWARD"


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
    attest_nonce: str | None = None,
) -> Packet:
    payload: dict[str, Any] = {
        "session_id": session_id,
        "server": server_name,
        "server_time": time.time(),
        "heartbeat_interval": heartbeat_interval_seconds,
        "protocol": PROTOCOL_VERSION,
    }
    if attest_nonce is not None:
        # Per-session challenge for group proof-of-possession/attestation (v4).
        payload["attest_nonce"] = attest_nonce
    return Packet(PacketType.WELCOME, payload)


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


def invite_create_packet(
    token: str,
    room_id: str,
    ttl_seconds: float,
    uses: int,
    *,
    kind: str = "chat",
    group_id: str = "",
) -> Packet:
    payload: dict[str, Any] = {
        "token": token,
        "room": room_id,
        "ttl": ttl_seconds,
        "uses": uses,
    }
    if kind != "chat":
        payload["kind"] = kind
        payload["group"] = group_id
    return Packet(PacketType.INVITE_CREATE, payload)


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
    kind: str = "chat",
    group_id: str | None = None,
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
    if kind != "chat":
        payload["kind"] = kind
    if group_id is not None:
        payload["group"] = group_id
    return Packet(PacketType.INVITE_STATE, payload)


def invite_revoke_packet(token: str) -> Packet:
    return Packet(PacketType.INVITE_REVOKE, {"token": token})


def redeem_packet(token: str) -> Packet:
    return Packet(PacketType.REDEEM, {"token": token})


def redeemed_packet(
    invite_id: str,
    room_id: str,
    expires_at: float,
    *,
    kind: str = "chat",
    group_id: str = "",
    group_name: str = "",
    owner_fingerprint: str = "",
    owner_public_key_hex: str = "",
    members: list[dict[str, Any]] | None = None,
    epoch: int = 0,
) -> Packet:
    payload: dict[str, Any] = {"invite": invite_id, "expires_at": expires_at}
    if kind == "group":
        # Group-kind redemption: no room channel is attached; the payload
        # carries the roster snapshot the joiner must pin (docs/GROUPS.md §12).
        payload["kind"] = "group"
        payload["group"] = group_id
        payload["name"] = group_name
        payload["owner"] = owner_fingerprint
        payload["owner_key"] = owner_public_key_hex
        payload["members"] = members or []
        payload["epoch"] = epoch
    else:
        payload["room"] = room_id
    return Packet(PacketType.REDEEMED, payload)


INVITE_STATES: frozenset[str] = frozenset({"active", "expired", "redeemed", "revoked"})


# ---------------------------------------------- secure-group lifecycle (v4)


def group_create_packet(
    name: str,
    public_key_hex: str,
    pop_signature_b64: str,
    handle: str,
    display_name: str,
) -> Packet:
    return Packet(
        PacketType.GROUP_CREATE,
        {
            "name": name,
            "pubkey": public_key_hex,
            "pop": pop_signature_b64,
            "handle": handle,
            "display": display_name,
        },
    )


def group_granted_packet(group_id: str, epoch: int) -> Packet:
    return Packet(PacketType.GROUP_GRANTED, {"group": group_id, "epoch": epoch})


def group_attest_packet(
    group_id: str,
    public_key_hex: str,
    signature_b64: str,
    handle: str,
    display_name: str,
) -> Packet:
    return Packet(
        PacketType.GROUP_ATTEST,
        {
            "group": group_id,
            "pubkey": public_key_hex,
            "sig": signature_b64,
            "handle": handle,
            "display": display_name,
        },
    )


def group_attested_packet(
    group_id: str,
    role: str,
    epoch: int,
    members: list[dict[str, Any]] | None = None,
    events: list[dict[str, Any]] | None = None,
) -> Packet:
    payload: dict[str, Any] = {"group": group_id, "role": role, "epoch": epoch}
    if members is not None:
        payload["members"] = members
    if events is not None:
        payload["events"] = events
    return Packet(PacketType.GROUP_ATTESTED, payload)


def group_state_packet(group_id: str) -> Packet:
    return Packet(PacketType.GROUP_STATE, {"group": group_id})


def group_roster_packet(
    group_id: str,
    epoch: int,
    state: str,
    members: list[dict[str, Any]],
    events: list[dict[str, Any]],
) -> Packet:
    return Packet(
        PacketType.GROUP_ROSTER,
        {
            "group": group_id,
            "epoch": epoch,
            "state": state,
            "members": members,
            "events": events,
        },
    )


def group_leave_packet(group_id: str) -> Packet:
    return Packet(PacketType.GROUP_LEAVE, {"group": group_id})


def group_remove_packet(group_id: str, subject_fingerprint: str) -> Packet:
    return Packet(PacketType.GROUP_REMOVE, {"group": group_id, "subject": subject_fingerprint})


def group_dissolve_packet(group_id: str) -> Packet:
    return Packet(PacketType.GROUP_DISSOLVE, {"group": group_id})


def group_sign_request_packet(
    group_id: str,
    op_id: str,
    kind: str,
    subject_fingerprint: str,
    epoch: int,
    message_b64: str,
) -> Packet:
    return Packet(
        PacketType.GROUP_SIGN_REQUEST,
        {
            "group": group_id,
            "op": op_id,
            "kind": kind,
            "subject": subject_fingerprint,
            "epoch": epoch,
            "message": message_b64,
        },
    )


def group_sign_packet(group_id: str, op_id: str, signature_b64: str) -> Packet:
    return Packet(
        PacketType.GROUP_SIGN,
        {"group": group_id, "op": op_id, "sig": signature_b64},
    )


def group_event_packet(
    group_id: str,
    epoch: int,
    kind: str,
    subject_fingerprint: str,
    wall_ts_iso: str,
    signer_fingerprint: str,
    signature_b64: str,
    *,
    member: dict[str, Any] | None = None,
) -> Packet:
    payload: dict[str, Any] = {
        "group": group_id,
        "epoch": epoch,
        "kind": kind,
        "subject": subject_fingerprint,
        "wall_ts": wall_ts_iso,
        "signer": signer_fingerprint,
        "sig": signature_b64,
    }
    if member is not None:
        payload["member"] = member
    return Packet(PacketType.GROUP_EVENT, payload)


def group_forward_packet(
    group_id: str,
    epoch: int,
    from_fingerprint: str,
    to_fingerprint: str,
    *,
    kind: str,
    body_b64: str,
) -> Packet:
    """One addressed, opaque group envelope (Phase 6C — §19, §28.3).

    ``kind`` is a small routing-class hint only (``msg`` = sealed inner
    frame; ``kex`` = pairwise-link handshake payload); ``body_b64`` is
    always ciphertext/public handshake material the relay cannot read.
    """

    return Packet(
        PacketType.GROUP_FORWARD,
        {
            "group": group_id,
            "epoch": epoch,
            "from": from_fingerprint,
            "to": to_fingerprint,
            "kind": kind,
            "body": body_b64,
        },
    )


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
        if "attest_nonce" in payload:
            _require_str(payload, "attest_nonce", max_length=MAX_NONCE_LENGTH)
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
        kind = str(payload.get("kind", "chat"))
        if kind == "group":
            # Group invites carry no room channel; they bind a group id.
            _require_group_field(payload)
        else:
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
        if "kind" in payload:
            kind = _require_str(payload, "kind", max_length=8)
            _require(kind in INVITE_KINDS, "'kind' must be 'chat' or 'group'")
        if "group" in payload:
            _require_group_field(payload)
    elif packet_type is PacketType.INVITE_REVOKE or packet_type is PacketType.REDEEM:
        _require_str(payload, "token", max_length=MAX_INVITE_FIELD_LENGTH)
    elif packet_type is PacketType.REDEEMED:
        _require_str(payload, "invite", max_length=MAX_INVITE_FIELD_LENGTH)
        _require_number(payload, "expires_at")
        kind = str(payload.get("kind", "chat"))
        if kind == "group":
            _require_group_field(payload)
            _require_str(payload, "name", max_length=GROUP_NAME_MAX_LEN)
            _require_fingerprint_field(payload, "owner")
            key = _require_str(payload, "owner_key", max_length=GROUP_PUBKEY_HEX_LENGTH)
            _require(len(key) == GROUP_PUBKEY_HEX_LENGTH, "'owner_key' must be 64 hex")
            _require_members_field(payload, "members")
            _require_int(payload, "epoch", minimum=1)
        else:
            _require_room_field(payload)
    elif packet_type in GROUP_PACKET_TYPES:
        _validate_group_packet(packet_type, payload)


def _require_channel(payload: dict[str, Any]) -> None:
    channel = _require_str(payload, "channel", max_length=64)
    _require(is_valid_room_id(channel), "'channel' is not a valid room identifier")


def _require_group_field(payload: dict[str, Any], field: str = "group") -> None:
    group = _require_str(payload, field, max_length=64)
    _require(is_valid_group_id(group), f"'{field}' is not a valid group identifier")


def _require_fingerprint_field(payload: dict[str, Any], field: str) -> None:
    value = _require_str(payload, field, max_length=40)
    _require(is_valid_fingerprint(value), f"'{field}' is not a valid GLFP fingerprint")


def _require_member_payload(member: Any) -> None:
    _require(isinstance(member, dict), "member entries must be objects")
    _require_fingerprint_field(member, "fingerprint")
    _require_str(member, "handle", max_length=16)
    _require_str(member, "display_name", max_length=GROUP_DISPLAY_NAME_MAX_LEN)
    pubkey = _require_str(member, "public_key_hex", max_length=GROUP_PUBKEY_HEX_LENGTH)
    _require(len(pubkey) == GROUP_PUBKEY_HEX_LENGTH, "'public_key_hex' must be 64 hex")
    role = _require_str(member, "role", max_length=8)
    _require(role in GROUP_ROLES, "'role' must be 'owner' or 'member'")
    _require_int(member, "joined_epoch", minimum=0)


def _require_members_field(payload: dict[str, Any], field: str) -> None:
    members = payload.get(field)
    if not isinstance(members, list):
        _fail(f"'{field}' must be a list")
    if len(members) > MAX_GROUP_MEMBERS:
        _fail(f"'{field}' may hold at most {MAX_GROUP_MEMBERS} entries")
    for member in members:
        _require_member_payload(member)


def _require_event_payload(event: Any) -> None:
    _require(isinstance(event, dict), "event entries must be objects")
    _require_group_field(event, "group_id")
    _require_int(event, "epoch", minimum=1)
    kind = _require_str(event, "kind", max_length=16)
    _require(kind in EVENT_KINDS, f"'kind' must be one of {sorted(EVENT_KINDS)}")
    _require_fingerprint_field(event, "subject")
    _require_str(event, "wall_ts", max_length=64)
    _require_fingerprint_field(event, "signer")
    _require_str(event, "signature", max_length=GROUP_SIGNATURE_B64_MAX)


def _require_events_field(payload: dict[str, Any], field: str) -> None:
    events = payload.get(field)
    if not isinstance(events, list):
        _fail(f"'{field}' must be a list")
    if len(events) > GROUP_EVENTS_KEPT:
        _fail(f"'{field}' may hold at most {GROUP_EVENTS_KEPT} entries")
    for event in events:
        _require_event_payload(event)


GROUP_PACKET_TYPES: frozenset[PacketType] = frozenset(
    {
        PacketType.GROUP_CREATE,
        PacketType.GROUP_GRANTED,
        PacketType.GROUP_ATTEST,
        PacketType.GROUP_ATTESTED,
        PacketType.GROUP_STATE,
        PacketType.GROUP_ROSTER,
        PacketType.GROUP_LEAVE,
        PacketType.GROUP_REMOVE,
        PacketType.GROUP_DISSOLVE,
        PacketType.GROUP_SIGN_REQUEST,
        PacketType.GROUP_SIGN,
        PacketType.GROUP_EVENT,
        PacketType.GROUP_FORWARD,
    }
)


def _validate_group_packet(packet_type: PacketType, payload: dict[str, Any]) -> None:
    """Schema validation for the v4 group-lifecycle packet family."""

    if packet_type is not PacketType.GROUP_CREATE:
        # GROUP_CREATE carries no group id yet — the authority mints it.
        _require_group_field(payload)
    if packet_type is PacketType.GROUP_CREATE:
        _require_str(payload, "name", max_length=GROUP_NAME_MAX_LEN)
        pubkey = _require_str(payload, "pubkey", max_length=GROUP_PUBKEY_HEX_LENGTH)
        _require(len(pubkey) == GROUP_PUBKEY_HEX_LENGTH, "'pubkey' must be 64 hex")
        _require_str(payload, "pop", max_length=GROUP_SIGNATURE_B64_MAX)
        _require_str(payload, "handle", max_length=16)
        _require_str(payload, "display", max_length=GROUP_DISPLAY_NAME_MAX_LEN)
    elif packet_type is PacketType.GROUP_GRANTED:
        _require_int(payload, "epoch", minimum=1)
    elif packet_type is PacketType.GROUP_ATTEST:
        pubkey = _require_str(payload, "pubkey", max_length=GROUP_PUBKEY_HEX_LENGTH)
        _require(len(pubkey) == GROUP_PUBKEY_HEX_LENGTH, "'pubkey' must be 64 hex")
        _require_str(payload, "sig", max_length=GROUP_SIGNATURE_B64_MAX)
        _require_str(payload, "handle", max_length=16)
        _require_str(payload, "display", max_length=GROUP_DISPLAY_NAME_MAX_LEN)
    elif packet_type is PacketType.GROUP_ATTESTED:
        role = _require_str(payload, "role", max_length=16)
        _require(
            role in GROUP_ATTEST_ROLES,
            f"'role' must be one of {sorted(GROUP_ATTEST_ROLES)}",
        )
        _require_int(payload, "epoch", minimum=0)
        if "members" in payload:
            _require_members_field(payload, "members")
        if "events" in payload:
            _require_events_field(payload, "events")
    elif packet_type is PacketType.GROUP_STATE:
        pass
    elif packet_type is PacketType.GROUP_ROSTER:
        _require_int(payload, "epoch", minimum=1)
        state = _require_str(payload, "state", max_length=16)
        _require(state in GROUP_STATES, f"'state' must be one of {sorted(GROUP_STATES)}")
        _require_members_field(payload, "members")
        _require_events_field(payload, "events")
    elif packet_type is PacketType.GROUP_LEAVE or packet_type is PacketType.GROUP_DISSOLVE:
        pass
    elif packet_type is PacketType.GROUP_REMOVE:
        _require_fingerprint_field(payload, "subject")
    elif packet_type is PacketType.GROUP_SIGN_REQUEST:
        op = _require_str(payload, "op", max_length=GROUP_OP_ID_LENGTH)
        _require(len(op) >= 8, "'op' must be a short operation id")
        kind = _require_str(payload, "kind", max_length=16)
        _require(kind in EVENT_KINDS, f"'kind' must be one of {sorted(EVENT_KINDS)}")
        _require_fingerprint_field(payload, "subject")
        _require_int(payload, "epoch", minimum=2)
        # The canonical event string the signer must sign exactly (chars are
        # ASCII by construction); bounded like a short message.
        message = _require_str(payload, "message", max_length=512)
        _require_b64(message, 512)
    elif packet_type is PacketType.GROUP_SIGN:
        op = _require_str(payload, "op", max_length=GROUP_OP_ID_LENGTH)
        _require(len(op) >= 8, "'op' must be a short operation id")
        _require_str(payload, "sig", max_length=GROUP_SIGNATURE_B64_MAX)
    elif packet_type is PacketType.GROUP_EVENT:
        _require_int(payload, "epoch", minimum=1)
        kind = _require_str(payload, "kind", max_length=16)
        _require(kind in EVENT_KINDS, f"'kind' must be one of {sorted(EVENT_KINDS)}")
        _require_fingerprint_field(payload, "subject")
        _require_str(payload, "wall_ts", max_length=64)
        _require_fingerprint_field(payload, "signer")
        _require_str(payload, "sig", max_length=GROUP_SIGNATURE_B64_MAX)
        if "member" in payload:
            _require_member_payload(payload["member"])
    elif packet_type is PacketType.GROUP_FORWARD:
        # Phase 6C addressed opaque envelope (§19): the relay validates
        # routing metadata and byte ceilings only; the body is ciphertext
        # (or public handshake material) it must never inspect beyond size.
        _require_int(payload, "epoch", minimum=1)
        _require_fingerprint_field(payload, "from")
        _require_fingerprint_field(payload, "to")
        kind = _require_str(payload, "kind", max_length=8)
        _require(
            kind in GROUP_FORWARD_KINDS,
            f"'kind' must be one of {sorted(GROUP_FORWARD_KINDS)}",
        )
        body = _require_str(payload, "body", max_length=_GROUP_FORWARD_BODY_B64_MAX)
        _require_b64(
            body,
            GROUP_FORWARD_BODY_MAX if kind == KIND_MSG else GROUP_KEX_BODY_MAX,
        )


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


def _require_b64(value: str, max_decoded: int) -> None:
    """A field that must be base64 decoding to at most ``max_decoded`` bytes."""

    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError):
        _fail("value must be base64")
    _require(0 < len(raw) <= max_decoded, f"value must decode to 1..{max_decoded} bytes")


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
