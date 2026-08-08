"""Invite tokens & gl://join links: entropy, format, parsing."""

from __future__ import annotations

import string
from collections import Counter

import pytest

from ghostlink.constants.net import INVITE_LINK_TOKEN_LENGTH, ROOM_ID_ALPHABET
from ghostlink.exceptions.invites import InviteValidationError
from ghostlink.invites.tokens import (
    format_invite_link,
    generate_invite_token,
    invite_id_for_token,
    is_valid_invite_id,
    is_valid_invite_token,
    normalize_invite_token,
    parse_invite_link,
    token_hash_for,
)


class TestTokenGeneration:
    def test_format_is_strict(self) -> None:
        token = generate_invite_token()
        assert len(token) == INVITE_LINK_TOKEN_LENGTH
        assert all(char in ROOM_ID_ALPHABET for char in token)
        assert is_valid_invite_token(token)

    def test_alphabet_excludes_confusables(self) -> None:
        assert not set("01IO") & set(ROOM_ID_ALPHABET)
        for _ in range(100):
            assert not set("01IO") & set(generate_invite_token())

    def test_tokens_are_not_sequential(self) -> None:
        from itertools import pairwise

        tokens = [generate_invite_token() for _ in range(8)]
        for first, second in pairwise(tokens):
            assert first[:-1] != second[:-1]  # no shared long prefix → non-sequential

    def test_uniqueness_over_volume(self) -> None:
        tokens = {generate_invite_token() for _ in range(2000)}
        assert len(tokens) == 2000

    def test_entropy_distribution(self) -> None:
        """Every alphabet character shows up in a big sample — uniform-ish."""
        sample = "".join(generate_invite_token() for _ in range(500))
        counts = Counter(sample)
        assert set(counts) >= set(ROOM_ID_ALPHABET[: len(ROOM_ID_ALPHABET) - 2])
        average = len(sample) / len(ROOM_ID_ALPHABET)
        for char in ROOM_ID_ALPHABET:
            assert counts.get(char, 0) > average * 0.2  # no systematic gaps


class TestTokenValidation:
    def test_normalization_uppercases(self) -> None:
        token = generate_invite_token()
        assert normalize_invite_token(token.lower()) == token

    def test_invalid_candidates(self) -> None:
        assert not is_valid_invite_token("")
        assert not is_valid_invite_token("A" * (INVITE_LINK_TOKEN_LENGTH - 1))
        assert not is_valid_invite_token("A" * (INVITE_LINK_TOKEN_LENGTH + 1))
        assert not is_valid_invite_token("0" * INVITE_LINK_TOKEN_LENGTH)  # confusable
        assert not is_valid_invite_token("ABCD-EFGH-JKMN-PQRS")
        assert not is_valid_invite_token("gli_" + "a" * 32)  # legacy Phase-2 token

    def test_normalize_rejects_malformed(self) -> None:
        with pytest.raises(InviteValidationError):
            normalize_invite_token("SHORT")
        with pytest.raises(InviteValidationError):
            normalize_invite_token("!" * INVITE_LINK_TOKEN_LENGTH)


class TestPublicIdentifiers:
    def test_invite_id_shape(self) -> None:
        invite_id = invite_id_for_token(generate_invite_token())
        assert invite_id.startswith("gi_")
        assert len(invite_id) == 13
        assert is_valid_invite_id(invite_id)

    def test_invite_id_is_deterministic_and_never_the_token(self) -> None:
        token = generate_invite_token()
        invite_id = invite_id_for_token(token)
        assert invite_id_for_token(token) == invite_id
        assert token not in invite_id
        assert invite_id not in token

    def test_token_hash_is_not_the_token(self) -> None:
        token = generate_invite_token()
        hashed = token_hash_for(token)
        assert len(hashed) == 64
        assert all(char in string.hexdigits.lower() for char in hashed)
        assert hashed != token


class TestInviteLinks:
    def test_round_trip(self) -> None:
        token = generate_invite_token()
        link = format_invite_link(token)
        assert link.startswith("gl://join/")
        assert parse_invite_link(link) == token

    def test_parse_accepts_mixed_case_prefix(self) -> None:
        token = generate_invite_token()
        link = format_invite_link(token).replace("gl://", "GL://")
        assert parse_invite_link(link) == token

    def test_links_carry_only_the_token(self) -> None:
        link = format_invite_link(generate_invite_token())
        assert "ws" not in link
        assert "@" not in link
        assert ":" not in link.removeprefix("gl://")
        assert "/" not in link.removeprefix("gl://join/")

    def test_bad_scheme(self) -> None:
        token = generate_invite_token()
        with pytest.raises(InviteValidationError, match="gl://"):
            parse_invite_link(f"http://join/{token}")
        with pytest.raises(InviteValidationError, match="gl://"):
            parse_invite_link(token)

    def test_bad_host(self) -> None:
        token = generate_invite_token()
        with pytest.raises(InviteValidationError, match="join"):
            parse_invite_link(f"gl://room/{token}")

    def test_malformed_token(self) -> None:
        with pytest.raises(InviteValidationError, match="malformed"):
            parse_invite_link("gl://join/SHORTY")
        with pytest.raises(InviteValidationError, match="malformed"):
            parse_invite_link("gl://join/" + generate_invite_token() + "AA")
        with pytest.raises(InviteValidationError, match="malformed"):
            parse_invite_link("gl://join/" + "0" * 20)  # confusable characters

    def test_non_string_and_garbage(self) -> None:
        with pytest.raises(InviteValidationError):
            parse_invite_link("")
        with pytest.raises(InviteValidationError):
            parse_invite_link("   ")
        with pytest.raises(InviteValidationError):
            parse_invite_link("gl://join/")
