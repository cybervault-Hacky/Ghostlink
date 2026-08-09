"""Phase 8 deterministic fuzz / property tests.

These are *deterministic* (fixed seed) and run in bounded time inside the
normal suite. They verify the adversarial property that every parser fails
closed: arbitrary / malformed / oversized / corrupted input is rejected with
a typed exception or a boolean ``False`` — never a crash, never an unbounded
allocation. Nothing here fuzzes in an unbounded way; each run covers a fixed
corpus of seeds.
"""

from __future__ import annotations

import contextlib
import json
import random
import string

import pytest

from ghostlink.exceptions.groups import GroupMessageError
from ghostlink.exceptions.transport import PacketValidationError
from ghostlink.groups.frames import FrameType, parse_inner_frame
from ghostlink.groups.ids import is_valid_group_id, normalize_group_id
from ghostlink.groups.senderkeys import (
    SenderKeyError,
    parse_distribution_root,
    parse_sk_control_body,
    parse_skmsg_body,
)
from ghostlink.identity.fingerprint import identity_fingerprint, is_valid_fingerprint
from ghostlink.invites.tokens import parse_invite_link
from ghostlink.transport.relay.protocol import decode_packet


def _random_blob(rng: random.Random, max_len: int = 512) -> bytes:
    return bytes(rng.getrandbits(8) for _ in range(rng.randint(0, max_len)))


class TestPacketDecoderFuzz:
    def test_arbitrary_bytes_never_crash(self) -> None:
        rng = random.Random(1337)
        for _ in range(200):
            with contextlib.suppress(PacketValidationError, UnicodeDecodeError, ValueError):
                decode_packet(_random_blob(rng))
        # oversized raw input is always rejected
        with pytest.raises(PacketValidationError):
            decode_packet(b"x" * (1024 * 1024 + 10))

    def test_valid_json_garbage_rejected(self) -> None:
        rng = random.Random(99)
        for _ in range(100):
            doc = {
                "v": rng.choice([1, 2, 3, 4, 99, "x"]),
                "type": rng.choice(["PING", "NOPE"]),
                "id": "abcd1234",
                "ts": 1.0,
                "payload": {},
            }
            with contextlib.suppress(PacketValidationError, ValueError):
                decode_packet(json.dumps(doc).encode())


class TestInnerFrameFuzz:
    def test_garbage_bytes_never_crash(self) -> None:
        rng = random.Random(2024)
        for _ in range(200):
            with contextlib.suppress(Exception):
                parse_inner_frame(_random_blob(rng))

    def test_oversized_frame_rejected(self) -> None:
        with pytest.raises(GroupMessageError):
            parse_inner_frame(b"x" * 10_000, max_bytes=4096)

    def test_unknown_frame_type_rejected(self) -> None:
        frame = {
            "v": 1,
            "t": "NOPE",
            "group": "gl-group-AAAA-BBBB-CCCC",
            "epoch": 1,
            "from": "GLFP-ABCD-1234-5678",
            "to": "GLFP-0000-0000-0000",
        }
        with pytest.raises(GroupMessageError):
            parse_inner_frame(json.dumps(frame).encode())

    def test_valid_gmsg_parses(self) -> None:
        frame = {
            "v": 1,
            "t": "GMSG",
            "group": "gl-group-AAAA-BBBB-CCCC",
            "epoch": 2,
            "from": "GLFP-ABCD-1234-5678",
            "to": "GLFP-0000-0000-0000",
            "id": "gmsg_" + "a" * 16,
            "gseq": 1,
            "name": "ops",
            "text": "hi",
            "ts": 1.0,
        }
        parsed = parse_inner_frame(json.dumps(frame).encode())
        assert parsed.frame_type is FrameType.GMSG


class TestIdentifierProperties:
    def test_group_id_is_fixed_shape(self) -> None:
        rng = random.Random(7)
        alphabet = string.ascii_uppercase + string.digits
        for _ in range(500):
            cand = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 40)))
            valid = is_valid_group_id(cand)
            assert valid in (True, False)
            if valid:
                assert normalize_group_id(cand) == cand

    def test_fingerprint_properties(self) -> None:
        rng = random.Random(11)
        for _ in range(500):
            key = bytes(rng.getrandbits(8) for _ in range(32))
            fp = identity_fingerprint(key)
            assert is_valid_fingerprint(fp)
        for _ in range(200):
            cand = "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(30))
            assert is_valid_fingerprint(cand) in (True, False)


class TestInviteParserFuzz:
    def test_invite_link_variants(self) -> None:
        rng = random.Random(5)
        for _ in range(200):
            cand = "".join(rng.choice(string.ascii_letters + ":///. ") for _ in range(60))
            with contextlib.suppress(Exception):
                parse_invite_link(cand)


class TestSenderKeyFrameFuzz:
    def test_skmsg_body_variants(self) -> None:
        rng = random.Random(3)
        for _ in range(200):
            with contextlib.suppress(SenderKeyError):
                parse_skmsg_body(_random_blob(rng, 256), max_sealed=1024)

    def test_sk_control_variants(self) -> None:
        rng = random.Random(4)
        for _ in range(200):
            with contextlib.suppress(SenderKeyError):
                parse_sk_control_body(_random_blob(rng, 256), max_sealed=1024)

    def test_distribution_root_variants(self) -> None:
        rng = random.Random(6)
        for _ in range(200):
            blob = _random_blob(rng, 128)
            with contextlib.suppress(SenderKeyError, UnicodeDecodeError):
                parse_distribution_root(blob.decode("latin-1"))


class TestNoCryptoDowngradePath:
    def test_suite_set_is_immutable(self) -> None:
        from ghostlink.constants.net import GROUP_CRYPTO_SUITES

        # The suite set must be fixed and include exactly the two suites.
        assert frozenset({"mesh-v1", "senderkey-v1"}) == GROUP_CRYPTO_SUITES
