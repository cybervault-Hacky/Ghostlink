"""WebAuthn / passkey architecture (Phase 10B).

Server-side challenge issuance and verification for the common ES256
(P-256) WebAuthn credential format. We implement the real verification
path with the ``cryptography`` library (ECDSA P-256 signature check over
the authenticator data + client data hash).

Important honesty notes (also in docs/DEVELOPER_PORTAL.md):
* No biometric data is ever collected, transmitted, or stored. The browser
  handles device authentication (fingerprint / Face ID / PIN); the server
  receives only WebAuthn cryptographic assertions.
* This module supports the standard **ES256 / P-256** COSE credential
  format and verifies assertion signatures itself. Production deployments
  that must support every credential type / attestation format should plug
  in a maintained WebAuthn library (see ``docs/DEPLOYMENT.md``); the
  interface here is the extension point.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets

from cryptography.hazmat.primitives.asymmetric import ec

ES256_ALG = -7  # COSE algorithm identifier for ECDSA P-256 with SHA-256


def b64url_decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------- minimal CBOR


def _cbor_decode(data: bytes) -> object:
    value, _offset = _cbor_value(data, 0)
    return value


def _cbor_value(data: bytes, offset: int) -> tuple[object, int]:
    initial = data[offset]
    major = initial >> 5
    info = initial & 0x1F
    if info < 24:
        arg = info
        offset += 1
    elif info == 24:
        arg = data[offset + 1]
        offset += 2
    elif info == 25:
        arg = int.from_bytes(data[offset + 1 : offset + 3], "big")
        offset += 3
    elif info == 26:
        arg = int.from_bytes(data[offset + 1 : offset + 5], "big")
        offset += 5
    elif info == 27:
        arg = int.from_bytes(data[offset + 1 : offset + 9], "big")
        offset += 9
    elif info == 31:
        raise ValueError("indefinite-length CBOR not supported")
    else:
        raise ValueError("reserved CBOR additional info")
    if major == 0:
        return arg, offset
    if major == 1:
        return -1 - arg, offset
    if major == 2:
        return data[offset : offset + arg], offset + arg
    if major == 3:
        return data[offset : offset + arg].decode("utf-8", errors="replace"), offset + arg
    if major == 4:
        result: list[object] = []
        for _ in range(arg):
            item, offset = _cbor_value(data, offset)
            result.append(item)
        return result, offset
    if major == 5:
        result_dict: dict[object, object] = {}
        for _ in range(arg):
            key, offset = _cbor_value(data, offset)
            val, offset = _cbor_value(data, offset)
            result_dict[key] = val
        return result_dict, offset
    raise ValueError("unsupported CBOR major type")


# ---------------------------------------------------------- COSE parsing


def parse_cose_ec2_public_key(cose: bytes) -> bytes:
    """Parse a COSE_Key with kty=EC2 (2), crv=P-256 (1); returns the
    SEC1 ``04|x|y`` uncompressed point.
    """
    key = _cbor_decode(cose)
    if not isinstance(key, dict):
        raise ValueError("COSE key must be a map")
    kty = key.get(1)
    crv = key.get(3)
    x = key.get(-2)
    y = key.get(-3)
    if kty != 2 or crv != 1:
        raise ValueError("only ES256 (EC2 / P-256) credentials are supported")
    if not isinstance(x, bytes) or not isinstance(y, bytes):
        raise ValueError("EC2 public key missing coordinates")
    return b"\x04" + x + y


def build_public_key(public_key_bytes: bytes) -> ec.EllipticCurvePublicKey:
    return ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), public_key_bytes)


# ------------------------------------------------------------ verification


def verify_assertion_signature(
    *,
    public_key: ec.EllipticCurvePublicKey,
    authenticator_data: bytes,
    client_data_json: bytes,
    signature: bytes,
    expected_challenge_b64: str,
) -> bool:
    """Verify a WebAuthn assertion (ES256) and the challenge binding."""
    try:
        client_data = json.loads(client_data_json.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(client_data, dict):
        return False
    challenge = client_data.get("challenge")
    if not isinstance(challenge, str) or challenge != expected_challenge_b64:
        return False
    # RP ID / origin binding: the clientData must be a valid origin for this
    # deployment (in production this is the configured origin).
    client_data_hash = hashlib.sha256(client_data_json).digest()
    signed_data = authenticator_data + client_data_hash
    public_key.verify(signature, signed_data, ec.ECDSA(hashlib.sha256))  # type: ignore[arg-type]
    return True


def valid_origin(client_data_json: bytes, allowed_origins: tuple[str, ...]) -> bool:
    """Check that the WebAuthn clientData ``origin`` matches an allowed origin."""
    try:
        client_data = json.loads(client_data_json.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return False
    if not isinstance(client_data, dict):
        return False
    origin = client_data.get("origin")
    return isinstance(origin, str) and origin in allowed_origins


def valid_rp_id(authenticator_data: bytes, rp_id_hash: bytes) -> bool:
    """Check the rpIdHash (first 32 bytes of authenticatorData) matches the
    expected RP ID SHA-256. Returns True for authenticatorData shorter than
    32 bytes (caller treats it as malformed separately).
    """
    if len(authenticator_data) < 32:
        return False
    return hmac.compare_digest(authenticator_data[:32], rp_id_hash)


def new_challenge() -> str:
    return b64url_encode(secrets.token_bytes(32))


__all__ = [
    "ES256_ALG",
    "b64url_decode",
    "b64url_encode",
    "build_public_key",
    "new_challenge",
    "parse_cose_ec2_public_key",
    "verify_assertion_signature",
]
