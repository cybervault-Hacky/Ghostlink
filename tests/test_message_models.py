"""Message model: identifiers, validation, lifecycle transitions."""

from __future__ import annotations

import pytest

from ghostlink.exceptions.messaging import MessageValidationError
from ghostlink.messaging.models.message import (
    MAX_MESSAGE_TEXT_BYTES,
    EncryptedPayload,
    Message,
    MessageDirection,
    MessageStatus,
    generate_message_id,
    is_valid_message_id,
    message_aad,
    validate_display_name,
    validate_message_text,
)

CONV = "conv_0123456789ab"


def make_outgoing(**overrides: object) -> Message:
    fields: dict[str, object] = {
        "message_id": generate_message_id(),
        "conversation_id": CONV,
        "direction": MessageDirection.OUTGOING,
        "sender": "Nova",
        "recipient": "Peer",
        "text": "hello",
        "sequence": 1,
    }
    fields.update(overrides)
    return Message(**fields)  # type: ignore[arg-type]


def make_incoming(**overrides: object) -> Message:
    fields: dict[str, object] = {
        "message_id": generate_message_id(),
        "conversation_id": CONV,
        "direction": MessageDirection.INCOMING,
        "sender": "Peer",
        "recipient": "Nova",
        "text": "hi",
        "sequence": 1,
        "status": MessageStatus.DELIVERED,
    }
    fields.update(overrides)
    return Message(**fields)  # type: ignore[arg-type]


class TestIdentifiers:
    def test_generated_ids_match_format(self) -> None:
        message_id = generate_message_id()
        assert is_valid_message_id(message_id)
        assert message_id.startswith("msg_")

    def test_ids_are_unique(self) -> None:
        assert generate_message_id() != generate_message_id()

    @pytest.mark.parametrize("bad", ["msg_short", "msg_" + "g" * 16, "x" * 20, ""])
    def test_invalid_ids_rejected(self, bad: str) -> None:
        assert not is_valid_message_id(bad)
        with pytest.raises(MessageValidationError, match="msg_"):
            make_outgoing(message_id=bad)

    def test_conversation_id_prefix_enforced(self) -> None:
        with pytest.raises(MessageValidationError, match="Conversation id"):
            make_outgoing(conversation_id="room-1")


class TestTextAndNames:
    def test_empty_text_rejected(self) -> None:
        with pytest.raises(MessageValidationError, match="empty"):
            validate_message_text("   ")

    def test_long_text_rejected_by_chars(self) -> None:
        with pytest.raises(MessageValidationError, match="2048"):
            validate_message_text("x" * 2049)

    def test_long_text_rejected_by_bytes(self) -> None:
        heavy = "字" * 1000  # 3 bytes each → 3000 bytes, under the char cap
        with pytest.raises(MessageValidationError, match="bytes"):
            validate_message_text(heavy)

    def test_max_size_text_accepted(self) -> None:
        text = "y" * MAX_MESSAGE_TEXT_BYTES
        assert validate_message_text(text) == text

    def test_display_name_rules(self) -> None:
        assert validate_display_name("  Nova  ") == "Nova"
        with pytest.raises(MessageValidationError):
            validate_display_name("")
        with pytest.raises(MessageValidationError):
            validate_display_name("n" * 25)
        with pytest.raises(MessageValidationError, match="control"):
            validate_display_name("bad\nname")

    def test_bad_sender_rejected(self) -> None:
        with pytest.raises(MessageValidationError):
            make_outgoing(sender="")

    def test_sequence_must_be_positive_int(self) -> None:
        with pytest.raises(MessageValidationError, match="sequence"):
            make_outgoing(sequence=0)
        with pytest.raises(MessageValidationError, match="sequence"):
            make_outgoing(sequence=True)


class TestLifecycle:
    def test_outgoing_happy_path(self) -> None:
        message = make_outgoing()
        assert message.status is MessageStatus.QUEUED
        message = message.with_status(MessageStatus.SENDING)
        message = message.with_status(MessageStatus.SENT)
        message = message.with_status(MessageStatus.DELIVERED)
        assert message.delivered_at is not None
        message = message.with_status(MessageStatus.READ)
        assert message.read_at is not None
        assert message.is_terminal

    def test_requeue_after_connection_loss(self) -> None:
        message = make_outgoing()
        message = message.with_status(MessageStatus.SENDING)
        message = message.with_status(MessageStatus.QUEUED)
        assert message.status is MessageStatus.QUEUED

    def test_sent_can_fail(self) -> None:
        message = make_outgoing()
        message = message.with_status(MessageStatus.SENDING)
        message = message.with_status(MessageStatus.SENT)
        failed = message.with_status(MessageStatus.FAILED, error="no ack")
        assert failed.error == "no ack"
        assert failed.is_terminal

    def test_illegal_transitions_rejected(self) -> None:
        message = make_outgoing()
        with pytest.raises(MessageValidationError, match="cannot move"):
            message.with_status(MessageStatus.DELIVERED)
        with pytest.raises(MessageValidationError, match="cannot move"):
            message.with_status(MessageStatus.READ)

    def test_terminal_states_are_final(self) -> None:
        message = make_outgoing().with_status(MessageStatus.SENDING)
        failed = message.with_status(MessageStatus.FAILED)
        with pytest.raises(MessageValidationError, match="cannot move"):
            failed.with_status(MessageStatus.QUEUED)

    def test_incoming_lifecycle(self) -> None:
        message = make_incoming()
        read = message.with_status(MessageStatus.READ)
        assert read.is_terminal

    @pytest.mark.parametrize(
        "status",
        [MessageStatus.QUEUED, MessageStatus.SENDING, MessageStatus.SENT, MessageStatus.FAILED],
    )
    def test_incoming_cannot_hold_outgoing_states(self, status: MessageStatus) -> None:
        with pytest.raises(MessageValidationError, match="Incoming"):
            make_incoming(status=status)

    def test_glyph_and_redaction(self) -> None:
        message = make_outgoing()
        assert message.glyph == "…"
        redacted = message.redacted_for_log()
        assert "hello" not in redacted  # no content in logs
        assert message.message_id in redacted


class TestEncryptedPayload:
    def test_round_trip(self) -> None:
        payload = EncryptedPayload(sealed=b"nonce+ciphertext", aad=b"aad")
        restored = EncryptedPayload.from_b64(payload.to_b64(), aad=b"aad")
        assert restored.sealed == payload.sealed
        assert restored.aad == b"aad"

    def test_empty_sealed_rejected(self) -> None:
        with pytest.raises(MessageValidationError):
            EncryptedPayload(sealed=b"", aad=b"aad")

    def test_bad_base64_rejected(self) -> None:
        with pytest.raises(MessageValidationError, match="base64"):
            EncryptedPayload.from_b64("!!!", aad=b"aad")

    def test_aad_binds_identity_and_sequence(self) -> None:
        assert message_aad("msg_" + "0" * 16, 7) == f"msg_{'0' * 16}|7".encode()
        assert message_aad("msg_" + "0" * 16, 7) != message_aad("msg_" + "0" * 16, 8)

    def test_payload_attached_via_with_payload(self) -> None:
        message = make_outgoing()
        payload = EncryptedPayload(sealed=b"sealed", aad=b"aad")
        updated = message.with_payload(payload)
        assert updated.payload is payload
        assert message.payload is None  # original untouched (immutable)
