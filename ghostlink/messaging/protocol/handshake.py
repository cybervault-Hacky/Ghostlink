"""Conversation key exchange (Phase 3).

A two-message authenticated key exchange inside the already-encrypted
anonymous channel:

    host   → KEX_HELLO  (ephemeral public key, nonce, [identity key])
    guest  → KEX_REPLY  (ephemeral public key, nonce, AEAD proof, [identity key])

Both sides derive the same session key from X25519 ECDH + HKDF bound to the
full transcript (channel, both public keys, both nonces, and each side's
identity public key — Phase 5 — when presented). The responder proves
possession of the matching key by sealing a confirmation of the transcript
hash under it — a tampered or mismatched exchange fails verification loudly
(:class:`HandshakeFailedError`), never silently.

The responder's proof gives key-confirmation; a separate out-of-band
fingerprint comparison (:func:`transcript_fingerprint`) is the MITM check.
Because presented identity keys join the transcript, two Phase 5 peers can
also compare GLFP identity fingerprints out-of-band — a relay that tampers
with a presented identity key breaks key-confirmation.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey

from ghostlink.exceptions.messaging import HandshakeFailedError
from ghostlink.messaging.protocol.crypto import (
    derive_session_key,
    generate_ephemeral_keypair,
    generate_handshake_nonce,
    open_sealed,
    seal,
    transcript_fingerprint,
)

CONFIRM_PREFIX: bytes = b"ghostlink-confirm/v1|"
_TRANSCRIPT_PREFIX: bytes = b"GL3|"


@dataclass(frozen=True, slots=True)
class SecureSession:
    """The result of a verified handshake — lives in memory only."""

    session_key: bytes
    transcript: bytes
    conversation_id: str
    fingerprint: str
    initiator: bool
    identity_bound: bool = False


def _build_transcript(
    channel_id: str,
    hello_pub: bytes,
    hello_nonce: bytes,
    reply_pub: bytes,
    reply_nonce: bytes,
    hello_idpub: bytes | None = None,
    reply_idpub: bytes | None = None,
) -> bytes:
    """Transcript committed into the AEAD proof.

    Each side's identity public key (Phase 5) joins the transcript exactly
    when its payload carried one, so a Phase 5 client still interoperates
    with a peer that presents no identity — while a relay tampering with a
    presented key flips the transcript and breaks the proof loudly.
    """

    transcript = (
        _TRANSCRIPT_PREFIX
        + channel_id.encode("utf-8")
        + b"|"
        + hello_pub
        + hello_nonce
        + reply_pub
        + reply_nonce
    )
    if hello_idpub is not None:
        transcript += b"|" + hello_idpub
    if reply_idpub is not None:
        transcript += b"|" + reply_idpub
    return transcript


def _decode_hex(payload: dict[str, Any], field: str) -> bytes:
    """Strict hex decoding of one handshake field (proof is base64)."""

    raw = payload.get(field)
    if not isinstance(raw, str):
        raise HandshakeFailedError(
            f"Handshake field '{field}' is missing.",
            hint="The peer does not speak the GhostLink handoff protocol.",
        )
    if field == "proof":
        try:
            return base64.b64decode(raw.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError) as exc:
            raise HandshakeFailedError(
                "Handshake proof is not valid base64.",
                hint="The peer does not speak the GhostLink handoff protocol.",
            ) from exc
    try:
        return bytes.fromhex(raw)
    except ValueError as exc:
        raise HandshakeFailedError(
            f"Handshake field '{field}' is not valid hex.",
            hint="The peer does not speak the GhostLink handoff protocol.",
        ) from exc


def _optional_idpub(payload: dict[str, Any]) -> bytes | None:
    """Decode the optional identity public key field ('idpub')."""

    raw = payload.get("idpub")
    if raw is None:
        return None
    decoded = _decode_hex(payload, "idpub")
    if len(decoded) != 32:
        raise HandshakeFailedError(
            "Handshake identity key must be 32 bytes.",
            hint="The peer does not speak the GhostLink handoff protocol.",
        )
    return decoded


class HandshakeInitiator:
    """Host side: sends KEX_HELLO, verifies the reply's proof."""

    def __init__(
        self,
        channel_id: str,
        *,
        display_name: str = "",
        identity_public_key_hex: str | None = None,
    ) -> None:
        self._channel_id = channel_id
        self._display_name = display_name.strip()
        self._idpub_hex = identity_public_key_hex
        self._private_key: X25519PrivateKey | None
        self._private_key, self._public_raw = generate_ephemeral_keypair()
        self._nonce = generate_handshake_nonce()
        self._used = False

    def hello_payload(self) -> dict[str, str]:
        payload = {"pub": self._public_raw.hex(), "nonce": self._nonce.hex()}
        if self._display_name:
            payload["name"] = self._display_name
        if self._idpub_hex:
            payload["idpub"] = self._idpub_hex
        return payload

    def complete(self, reply_payload: dict[str, Any]) -> SecureSession:
        if self._used:
            raise HandshakeFailedError(
                "Handshake instances are single-use.",
                hint="Create a fresh handshake for every conversation.",
            )
        self._used = True
        reply_pub = _decode_hex(reply_payload, "pub")
        reply_nonce = _decode_hex(reply_payload, "nonce")
        proof = _decode_hex(reply_payload, "proof")
        reply_idpub = _optional_idpub(reply_payload)
        hello_idpub = bytes.fromhex(self._idpub_hex) if self._idpub_hex else None
        transcript = _build_transcript(
            self._channel_id,
            self._public_raw,
            self._nonce,
            reply_pub,
            reply_nonce,
            hello_idpub=hello_idpub,
            reply_idpub=reply_idpub,
        )
        assert self._private_key is not None  # single-use guard above
        try:
            session_key = derive_session_key(self._private_key, reply_pub, transcript)
        except (ValueError, TypeError) as exc:
            raise HandshakeFailedError(
                "The peer's handshake key is invalid.",
                hint="Abort the conversation; the peer is not running GhostLink v3 handshake.",
            ) from exc
        try:
            opened = open_sealed(session_key, proof, aad=transcript)
        except Exception as exc:
            raise HandshakeFailedError(
                "Handshake proof failed AEAD verification.",
                hint="The keys derived by both sides differ — possible MITM or "
                "protocol mismatch. Compare safety codes out-of-band.",
            ) from exc
        expected = CONFIRM_PREFIX + hashlib.sha256(transcript).digest()
        if opened != expected:
            raise HandshakeFailedError(
                "Handshake proof does not commit to this conversation.",
                hint="Abort the conversation and verify the room identifier.",
            )
        self._private_key = None  # ephemeral key discarded; HKDF output lives on
        return SecureSession(
            session_key=session_key,
            transcript=transcript,
            conversation_id="conv_" + hashlib.sha256(transcript).hexdigest()[:12],
            fingerprint=transcript_fingerprint(transcript),
            initiator=True,
            identity_bound=hello_idpub is not None and reply_idpub is not None,
        )


class HandshakeResponder:
    """Guest side: answers KEX_HELLO with KEX_REPLY + AEAD proof."""

    def __init__(
        self,
        channel_id: str,
        *,
        display_name: str = "",
        identity_public_key_hex: str | None = None,
    ) -> None:
        self._channel_id = channel_id
        self._display_name = display_name.strip()
        self._idpub_hex = identity_public_key_hex
        self._private_key: X25519PrivateKey | None
        self._private_key, self._public_raw = generate_ephemeral_keypair()
        self._nonce = generate_handshake_nonce()
        self._used = False

    def answer(self, hello_payload: dict[str, Any]) -> tuple[dict[str, str], SecureSession]:
        if self._used:
            raise HandshakeFailedError(
                "Handshake instances are single-use.",
                hint="Create a fresh handshake for every conversation.",
            )
        self._used = True
        hello_pub = _decode_hex(hello_payload, "pub")
        hello_nonce = _decode_hex(hello_payload, "nonce")
        hello_idpub = _optional_idpub(hello_payload)
        reply_idpub = bytes.fromhex(self._idpub_hex) if self._idpub_hex else None
        transcript = _build_transcript(
            self._channel_id,
            hello_pub,
            hello_nonce,
            self._public_raw,
            self._nonce,
            hello_idpub=hello_idpub,
            reply_idpub=reply_idpub,
        )
        assert self._private_key is not None  # single-use guard above
        try:
            session_key = derive_session_key(self._private_key, hello_pub, transcript)
        except (ValueError, TypeError) as exc:
            raise HandshakeFailedError(
                "The peer's handshake key is invalid.",
                hint="Abort the conversation; the peer is not running GhostLink v3 handshake.",
            ) from exc
        proof = seal(
            session_key,
            CONFIRM_PREFIX + hashlib.sha256(transcript).digest(),
            aad=transcript,
        )
        self._private_key = None
        session = SecureSession(
            session_key=session_key,
            transcript=transcript,
            conversation_id="conv_" + hashlib.sha256(transcript).hexdigest()[:12],
            fingerprint=transcript_fingerprint(transcript),
            initiator=False,
            identity_bound=hello_idpub is not None and reply_idpub is not None,
        )
        payload = {
            "pub": self._public_raw.hex(),
            "nonce": self._nonce.hex(),
            "proof": base64.b64encode(proof).decode("ascii"),
        }
        if self._display_name:
            payload["name"] = self._display_name
        if self._idpub_hex:
            payload["idpub"] = self._idpub_hex
        return payload, session
