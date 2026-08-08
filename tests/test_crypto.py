"""Encryption primitives and the conversation key exchange (Phase 3)."""

from __future__ import annotations

import base64
import hashlib
import re

import pytest

from ghostlink.exceptions.messaging import DecryptionError, HandshakeFailedError
from ghostlink.messaging.protocol.crypto import (
    NONCE_BYTES,
    SESSION_KEY_BYTES,
    derive_session_key,
    generate_ephemeral_keypair,
    generate_handshake_nonce,
    open_sealed,
    seal,
    transcript_fingerprint,
)
from ghostlink.messaging.protocol.handshake import (
    CONFIRM_PREFIX,
    HandshakeInitiator,
    HandshakeResponder,
)

CHANNEL = "gl-room-ABCD-EFGH-JKLM"


# ----------------------------------------------------------------- primitives


class TestKeyGeneration:
    def test_keypair_has_raw_32_byte_public_key(self) -> None:
        _, public_raw = generate_ephemeral_keypair()
        assert len(public_raw) == 32

    def test_keypairs_are_unique(self) -> None:
        first = generate_ephemeral_keypair()[1]
        second = generate_ephemeral_keypair()[1]
        assert first != second

    def test_handshake_nonce_is_16_bytes_and_unique(self) -> None:
        first, second = generate_handshake_nonce(), generate_handshake_nonce()
        assert len(first) == len(second) == 16
        assert first != second


class TestSessionKeyDerivation:
    def test_both_sides_derive_the_same_key(self) -> None:
        private_a, public_a = generate_ephemeral_keypair()
        private_b, public_b = generate_ephemeral_keypair()
        transcript = b"transcript"
        key_a = derive_session_key(private_a, public_b, transcript)
        key_b = derive_session_key(private_b, public_a, transcript)
        assert key_a == key_b
        assert len(key_a) == SESSION_KEY_BYTES

    def test_transcript_binding_changes_the_key(self) -> None:
        private_a, _public_a = generate_ephemeral_keypair()
        private_b, public_b = generate_ephemeral_keypair()
        del private_b
        key_one = derive_session_key(private_a, public_b, b"conversation-one")
        key_two = derive_session_key(private_a, public_b, b"conversation-two")
        assert key_one != key_two

    def test_wrong_public_key_length_rejected(self) -> None:
        private_a, _ = generate_ephemeral_keypair()
        with pytest.raises(ValueError, match="32 bytes"):
            derive_session_key(private_a, b"short", b"transcript")


class TestSealedBox:
    def test_round_trip(self) -> None:
        key = b"k" * 32
        sealed = seal(key, b"hello ghost", aad=b"meta")
        assert sealed.startswith(sealed[:NONCE_BYTES])
        assert open_sealed(key, sealed, aad=b"meta") == b"hello ghost"

    def test_nonce_prefix_and_randomized_output(self) -> None:
        key = b"k" * 32
        first, second = seal(key, b"same", aad=b""), seal(key, b"same", aad=b"")
        assert first != second  # fresh nonce per message
        assert len(first) == NONCE_BYTES + len(b"same") + 16  # AEAD tag

    def test_tampered_ciphertext_fails_loudly(self) -> None:
        key = b"k" * 32
        sealed = bytearray(seal(key, b"integrity", aad=b""))
        sealed[-1] ^= 0x01
        with pytest.raises(DecryptionError, match="integrity"):
            open_sealed(key, bytes(sealed), aad=b"")

    def test_wrong_key_fails_loudly(self) -> None:
        sealed = seal(b"a" * 32, b"secret", aad=b"")
        with pytest.raises(DecryptionError):
            open_sealed(b"b" * 32, sealed, aad=b"")

    def test_wrong_aad_fails_loudly(self) -> None:
        key = b"k" * 32
        sealed = seal(key, b"secret", aad=b"expected")
        with pytest.raises(DecryptionError):
            open_sealed(key, sealed, aad=b"other")

    def test_truncated_payload_rejected_before_decrypt(self) -> None:
        with pytest.raises(DecryptionError, match="too short"):
            open_sealed(b"k" * 32, b"tiny", aad=b"")


class TestFingerprint:
    def test_format_is_eight_groups_of_four_hex(self) -> None:
        fingerprint = transcript_fingerprint(b"some transcript")
        assert re.fullmatch(r"([0-9A-F]{4} ){7}[0-9A-F]{4}", fingerprint)

    def test_distinct_transcripts_have_distinct_fingerprints(self) -> None:
        assert transcript_fingerprint(b"one") != transcript_fingerprint(b"two")


# ------------------------------------------------------------------ handshake


def _perform_handshake(channel: str = CHANNEL) -> tuple[bytes, bytes, str, str]:
    """Run a full initiator↔responder exchange; return keys + fingerprints."""

    initiator = HandshakeInitiator(channel)
    responder = HandshakeResponder(channel)
    hello = initiator.hello_payload()
    reply, responder_session = responder.answer(hello)
    initiator_session = initiator.complete(reply)
    return (
        initiator_session.session_key,
        responder_session.session_key,
        initiator_session.fingerprint,
        responder_session.fingerprint,
    )


class TestHandshake:
    def test_full_exchange_derives_matching_keys(self) -> None:
        key_a, key_b, fp_a, fp_b = _perform_handshake()
        assert key_a == key_b
        assert fp_a == fp_b

    def test_sessions_share_conversation_id_and_side_flags(self) -> None:
        initiator = HandshakeInitiator(CHANNEL)
        responder = HandshakeResponder(CHANNEL)
        reply, responder_session = responder.answer(initiator.hello_payload())
        initiator_session = initiator.complete(reply)
        assert initiator_session.conversation_id == responder_session.conversation_id
        assert initiator_session.conversation_id.startswith("conv_")
        assert initiator_session.initiator is True
        assert responder_session.initiator is False

    def test_derived_key_encrypts_messages_both_ways(self) -> None:
        key_a, key_b, _, _ = _perform_handshake()
        sealed = seal(key_a, b"first message", aad=b"msg-1")
        assert open_sealed(key_b, sealed, aad=b"msg-1") == b"first message"
        sealed = seal(key_b, b"reply", aad=b"msg-2")
        assert open_sealed(key_a, sealed, aad=b"msg-2") == b"reply"

    def test_each_exchange_uses_fresh_keys(self) -> None:
        key_one, _, _, _ = _perform_handshake()
        key_two, _, _, _ = _perform_handshake()
        assert key_one != key_two

    def test_channel_mismatch_fails_proof_verification(self) -> None:
        initiator = HandshakeInitiator(CHANNEL)
        responder = HandshakeResponder("gl-room-ZZZZ-YYYY-XXXX")
        reply, _ = responder.answer(initiator.hello_payload())
        with pytest.raises(HandshakeFailedError, match="AEAD"):
            initiator.complete(reply)

    def test_tampered_reply_public_key_fails(self) -> None:
        initiator = HandshakeInitiator(CHANNEL)
        responder = HandshakeResponder(CHANNEL)
        reply, _ = responder.answer(initiator.hello_payload())
        other_private, other_public = generate_ephemeral_keypair()
        del other_private
        tampered = dict(reply, pub=other_public.hex())
        with pytest.raises(HandshakeFailedError):
            initiator.complete(tampered)

    def test_tampered_hello_public_key_fails(self) -> None:
        """A swapped-but-valid hello key passes responder decode but the
        initiator's AEAD proof check must reject the mismatch."""

        initiator = HandshakeInitiator(CHANNEL)
        responder = HandshakeResponder(CHANNEL)
        hello = initiator.hello_payload()
        _, other_public = generate_ephemeral_keypair()
        reply, responder_session = responder.answer(dict(hello, pub=other_public.hex()))
        # Responder derived a key against the attacker's key — unusable pair.
        with pytest.raises(HandshakeFailedError, match="AEAD"):
            initiator.complete(reply)
        del responder_session

    def test_tampered_proof_fails(self) -> None:
        initiator = HandshakeInitiator(CHANNEL)
        responder = HandshakeResponder(CHANNEL)
        reply, _ = responder.answer(initiator.hello_payload())
        proof = bytearray(base64.b64decode(reply["proof"]))
        proof[-1] ^= 0x01
        forged = dict(reply, proof=base64.b64encode(bytes(proof)).decode("ascii"))
        with pytest.raises(HandshakeFailedError, match="AEAD"):
            initiator.complete(forged)

    def test_replayed_proof_from_other_conversation_fails(self) -> None:
        _, responder_one = HandshakeInitiator(CHANNEL), HandshakeResponder(CHANNEL)
        initiator_two = HandshakeInitiator(CHANNEL)
        reply_one, _ = responder_one.answer(initiator_two.hello_payload())
        # Attacker replays reply_one's proof verbatim — but it commits to
        # responder_one's key+transcript, so initiator_two must reject it.
        responder_two = HandshakeResponder(CHANNEL)
        reply_two, _ = responder_two.answer(initiator_two.hello_payload())
        forged = dict(reply_two, proof=reply_one["proof"])
        with pytest.raises(HandshakeFailedError):
            initiator_two.complete(forged)

    @pytest.mark.parametrize("field", ["pub", "nonce", "proof"])
    def test_missing_reply_fields_rejected(self, field: str) -> None:
        initiator = HandshakeInitiator(CHANNEL)
        responder = HandshakeResponder(CHANNEL)
        reply, _ = responder.answer(initiator.hello_payload())
        broken = {key: value for key, value in reply.items() if key != field}
        with pytest.raises(HandshakeFailedError):
            initiator.complete(broken)

    def test_malformed_hex_rejected(self) -> None:
        initiator = HandshakeInitiator(CHANNEL)
        responder = HandshakeResponder(CHANNEL)
        reply, _ = responder.answer(initiator.hello_payload())
        with pytest.raises(HandshakeFailedError, match="hex"):
            initiator.complete(dict(reply, pub="zz" * 32))

    def test_malformed_proof_base64_rejected(self) -> None:
        initiator = HandshakeInitiator(CHANNEL)
        responder = HandshakeResponder(CHANNEL)
        reply, _ = responder.answer(initiator.hello_payload())
        with pytest.raises(HandshakeFailedError, match="base64"):
            initiator.complete(dict(reply, proof="!!!not base64!!!"))

    def test_wrong_proof_plaintext_fails_commitment(self) -> None:
        """A proof that decrypts but commits to nothing must be rejected."""

        initiator = HandshakeInitiator(CHANNEL)
        responder = HandshakeResponder(CHANNEL)
        hello = initiator.hello_payload()
        reply, responder_session = responder.answer(hello)
        forged_proof = seal(
            responder_session.session_key, b"nothing", aad=responder_session.transcript
        )
        with pytest.raises(HandshakeFailedError, match="commit"):
            initiator.complete(dict(reply, proof=base64.b64encode(forged_proof).decode("ascii")))

    def test_correct_transcript_commitment_is_accepted_shape(self) -> None:
        """The responder's real proof commits to CONFIRM_PREFIX + sha256(transcript)."""

        initiator = HandshakeInitiator(CHANNEL)
        responder = HandshakeResponder(CHANNEL)
        reply, responder_session = responder.answer(initiator.hello_payload())
        session = initiator.complete(reply)
        expected = CONFIRM_PREFIX + hashlib.sha256(responder_session.transcript).digest()
        assert expected[: len(CONFIRM_PREFIX)] == CONFIRM_PREFIX
        assert session.transcript == responder_session.transcript

    def test_initiator_is_single_use(self) -> None:
        initiator = HandshakeInitiator(CHANNEL)
        responder = HandshakeResponder(CHANNEL)
        reply, _ = responder.answer(initiator.hello_payload())
        initiator.complete(reply)
        with pytest.raises(HandshakeFailedError, match="single-use"):
            initiator.complete(reply)

    def test_responder_is_single_use(self) -> None:
        initiator = HandshakeInitiator(CHANNEL)
        responder = HandshakeResponder(CHANNEL)
        hello = initiator.hello_payload()
        responder.answer(hello)
        with pytest.raises(HandshakeFailedError, match="single-use"):
            responder.answer(hello)
