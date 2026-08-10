"""Message model (Phase 3).

One :class:`Message` is the canonical in-memory representation of a single
chat message in both directions. Outgoing messages travel a strict
status lifecycle::

    QUEUED → SENDING → SENT → DELIVERED → READ
       ↑         │        │        │
       └── requeue  FAILED  FAILED  (FAILED is terminal)

Incoming messages are created already DELIVERED (their transport is the
ack we send back) and can only advance to READ.

Plaintext lives in ``text`` *in memory only* — never logged, never written
to disk by this module. The encrypted representation is carried separately
in :class:`EncryptedPayload` so the two never mix.
"""

from __future__ import annotations

import base64
import binascii
import re
import secrets
import time
from dataclasses import dataclass, field, replace
from enum import Enum

from ghostlink.exceptions.messaging import MessageValidationError

MESSAGE_ID_PREFIX: str = "msg_"
_MESSAGE_ID_PATTERN = re.compile(r"^msg_[0-9a-f]{16}$")
MAX_DISPLAY_NAME_LENGTH: int = 24
MAX_MESSAGE_TEXT_LENGTH: int = 2048  # characters
MAX_MESSAGE_TEXT_BYTES: int = 2048  # UTF-8 bytes (fits the MESSAGE frame cap)


class MessageStatus(str, Enum):
    """Delivery lifecycle states of one message."""

    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


class MessageDirection(str, Enum):
    """Which side of the conversation created the message."""

    OUTGOING = "outgoing"
    INCOMING = "incoming"


# Transitions valid for an outgoing message. FAILED is terminal; READ is
# terminal. Re-queueing from SENDING/SENT happens on connection loss so a
# fresh session key can seal a new frame.
_OUTGOING_TRANSITIONS: dict[MessageStatus, frozenset[MessageStatus]] = {
    MessageStatus.QUEUED: frozenset({MessageStatus.SENDING, MessageStatus.FAILED}),
    MessageStatus.SENDING: frozenset(
        {MessageStatus.SENT, MessageStatus.QUEUED, MessageStatus.FAILED}
    ),
    MessageStatus.SENT: frozenset(
        {MessageStatus.DELIVERED, MessageStatus.QUEUED, MessageStatus.FAILED}
    ),
    MessageStatus.DELIVERED: frozenset({MessageStatus.READ}),
    MessageStatus.READ: frozenset(),
    MessageStatus.FAILED: frozenset(),
}

_INCOMING_TRANSITIONS: dict[MessageStatus, frozenset[MessageStatus]] = {
    MessageStatus.QUEUED: frozenset(),
    MessageStatus.SENDING: frozenset(),
    MessageStatus.SENT: frozenset(),
    MessageStatus.DELIVERED: frozenset({MessageStatus.READ}),
    MessageStatus.READ: frozenset(),
    MessageStatus.FAILED: frozenset(),
}

STATUS_GLYPHS: dict[MessageStatus, str] = {
    MessageStatus.QUEUED: "…",
    MessageStatus.SENDING: "↗",
    MessageStatus.SENT: "✓",
    MessageStatus.DELIVERED: "✓✓",
    MessageStatus.READ: "✓✓",
    MessageStatus.FAILED: "✕",
}


def generate_message_id() -> str:
    """Unique message identifier: ``msg_`` + 16 lowercase hex characters."""

    return f"{MESSAGE_ID_PREFIX}{secrets.token_hex(8)}"


def is_valid_message_id(candidate: str) -> bool:
    return bool(_MESSAGE_ID_PATTERN.fullmatch(candidate))


def message_aad(message_id: str, sequence: int) -> bytes:
    """Associated data binding a ciphertext to message identity and order."""

    return f"{message_id}|{sequence}".encode("ascii")


def validate_display_name(name: str, *, field: str = "display name") -> str:
    """A display name must be 1..24 printable, non-control characters."""

    cleaned = name.strip()
    if not (0 < len(cleaned) <= MAX_DISPLAY_NAME_LENGTH):
        raise MessageValidationError(
            f"A {field} must be 1..{MAX_DISPLAY_NAME_LENGTH} characters, got {len(cleaned)}.",
            hint="Pick a short printable name, e.g. --name Nova.",
        )
    if any(ord(char) < 32 or ord(char) == 127 for char in cleaned):
        raise MessageValidationError(
            f"A {field} must not contain control characters.",
            hint="Use plain printable text only.",
        )
    return cleaned


def validate_message_text(text: str) -> str:
    """Message text: non-empty and small enough to fit one MESSAGE frame."""

    if not text or not text.strip():
        raise MessageValidationError(
            "Message text must not be empty.",
            hint="Type a message after the '>' prompt and press Enter.",
        )
    if len(text) > MAX_MESSAGE_TEXT_LENGTH:
        raise MessageValidationError(
            f"Message is {len(text)} characters; the limit is {MAX_MESSAGE_TEXT_LENGTH}.",
            hint="Split long notes into several messages.",
        )
    encoded = text.encode("utf-8")
    if len(encoded) > MAX_MESSAGE_TEXT_BYTES:
        raise MessageValidationError(
            f"Message encodes to {len(encoded)} bytes; the limit is {MAX_MESSAGE_TEXT_BYTES}.",
            hint="Emoji and wide scripts take several bytes per character — shorten the message.",
        )
    return text


@dataclass(frozen=True, slots=True)
class EncryptedPayload:
    """The sealed (nonce + ciphertext + tag) form of one message.

    ``aad`` is authenticated but not encrypted; both sides recompute it from
    message identity so it never travels on the wire.
    """

    sealed: bytes
    aad: bytes

    def __post_init__(self) -> None:
        if not self.sealed:
            raise MessageValidationError(
                "Encrypted payload must not be empty.",
                hint="Seal plaintext with the active session key first.",
            )

    def to_b64(self) -> str:
        return base64.b64encode(self.sealed).decode("ascii")

    @classmethod
    def from_b64(cls, encoded: str, *, aad: bytes) -> EncryptedPayload:
        try:
            sealed = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError) as exc:
            raise MessageValidationError(
                "Encrypted payload is not valid base64.",
                hint="The frame was corrupted in transit.",
            ) from exc
        return cls(sealed=sealed, aad=aad)


@dataclass(frozen=True, slots=True)
class Message:
    """One chat message and its delivery state (immutable; copy-on-change)."""

    message_id: str
    conversation_id: str
    direction: MessageDirection
    sender: str
    recipient: str
    text: str
    sequence: int
    status: MessageStatus = MessageStatus.QUEUED
    created_at: float = field(default_factory=time.time)
    delivered_at: float | None = None
    read_at: float | None = None
    payload: EncryptedPayload | None = None
    error: str | None = None

    def __post_init__(self) -> None:
        if not is_valid_message_id(self.message_id):
            raise MessageValidationError(
                f"Message id '{self.message_id}' must be 'msg_' + 16 hex characters.",
                hint="Generate identifiers with generate_message_id().",
            )
        if not self.conversation_id.startswith("conv_"):
            raise MessageValidationError(
                f"Conversation id '{self.conversation_id}' is malformed.",
                hint="Conversation identifiers come from a verified handshake.",
            )
        validate_message_text(self.text)
        validate_display_name(self.sender, field="sender name")
        validate_display_name(self.recipient, field="recipient name")
        if not isinstance(self.sequence, int) or isinstance(self.sequence, bool):
            raise MessageValidationError(
                "Message sequence must be an integer ≥ 1.",
                hint="Sequences are assigned by the sending chat session.",
            )
        if self.sequence < 1:
            raise MessageValidationError(
                "Message sequence must be an integer ≥ 1.",
                hint="Sequences are assigned by the sending chat session.",
            )
        if self.direction is MessageDirection.INCOMING and self.status not in (
            MessageStatus.DELIVERED,
            MessageStatus.READ,
        ):
            raise MessageValidationError(
                f"Incoming messages exist only as delivered/read, not '{self.status.value}'.",
                hint="Incoming messages are created once decryption succeeds.",
            )

    # ------------------------------------------------------------------ helpers

    @property
    def is_outgoing(self) -> bool:
        return self.direction is MessageDirection.OUTGOING

    @property
    def is_terminal(self) -> bool:
        return not _transitions_for(self.direction)[self.status]

    @property
    def glyph(self) -> str:
        return STATUS_GLYPHS[self.status]

    # -------------------------------------------------------------- transitions

    def with_status(self, status: MessageStatus, *, error: str | None = None) -> Message:
        """Advance the lifecycle, rejecting illegal transitions loudly."""

        allowed = _transitions_for(self.direction)[self.status]
        if status not in allowed:
            raise MessageValidationError(
                f"{self.direction.value} message cannot move {self.status.value} → {status.value}.",
                hint="Follow the queued→sending→sent→delivered→read lifecycle.",
            )
        now = time.time()
        return replace(
            self,
            status=status,
            error=error,
            delivered_at=now if status is MessageStatus.DELIVERED else self.delivered_at,
            read_at=now if status is MessageStatus.READ else self.read_at,
        )

    def with_payload(self, payload: EncryptedPayload) -> Message:
        """Attach the sealed form (frames carry only this, never plaintext)."""

        return replace(self, payload=payload)

    def redacted_for_log(self) -> str:
        """Safe one-line reference for logs: ids and states, never content."""

        return f"message {self.message_id} seq={self.sequence} status={self.status.value}"


def _transitions_for(
    direction: MessageDirection,
) -> dict[MessageStatus, frozenset[MessageStatus]]:
    if direction is MessageDirection.OUTGOING:
        return _OUTGOING_TRANSITIONS
    return _INCOMING_TRANSITIONS
