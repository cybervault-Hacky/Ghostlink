"""Room model: identifiers, lifecycle, derived state, serialization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ghostlink.exceptions.config import ConfigValidationError
from ghostlink.models.room import (
    ROOM_ID_PATTERN,
    Room,
    RoomEffectiveState,
    RoomState,
    generate_room_id,
    is_valid_room_id,
    normalize_room_id,
)

NOW = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)


class TestRoomIds:
    def test_generated_ids_match_canonical_format(self) -> None:
        for _ in range(50):
            room_id = generate_room_id()
            assert ROOM_ID_PATTERN.fullmatch(room_id), room_id
            assert is_valid_room_id(room_id)

    def test_generated_ids_are_unique(self) -> None:
        assert generate_room_id() != generate_room_id()

    def test_alphabet_excludes_ambiguous_characters(self) -> None:
        for _ in range(200):
            room_id = generate_room_id()
            assert not any(ch in room_id for ch in "01IO")

    @pytest.mark.parametrize(
        "candidate",
        [
            "gl-room-ABCD-EFGH-JKMN",
            "gl-room-2345-WXYZ-ABCD",
        ],
    )
    def test_valid_ids_accepted(self, candidate: str) -> None:
        assert is_valid_room_id(candidate)

    @pytest.mark.parametrize(
        "candidate",
        [
            "gl-room-ABCD-EFGH-JKM1",  # '1' is not in the alphabet
            "gl-room-ABCD-EFGH-JKMO",  # 'O' is not in the alphabet
            "GL-ROOM-ABCD-EFGH-JKMN",  # prefix is lowercase canonically
            "gl-room-ABCDE-FGHJ-KMNQ",  # group lengths are strict
            "gl-room-ABCD-EFGH",  # missing a group
            "gl-room-ABCD-EFGH-JKMN-X",  # trailing junk
            "room-ABCD-EFGH-JKMN",  # wrong prefix
            "",
        ],
    )
    def test_invalid_ids_rejected(self, candidate: str) -> None:
        assert not is_valid_room_id(candidate)


class TestNormalize:
    def test_lowercase_full_id_becomes_canonical(self) -> None:
        assert normalize_room_id("gl-room-abcd-efgh-jkmn") == "gl-room-ABCD-EFGH-JKMN"

    def test_bare_groups_gain_the_prefix(self) -> None:
        assert normalize_room_id("ABCD-EFGH-JKMN") == "gl-room-ABCD-EFGH-JKMN"

    def test_surrounding_whitespace_is_ignored(self) -> None:
        assert normalize_room_id("  GL-ROOM-ABCD-EFGH-JKMN  ") == "gl-room-ABCD-EFGH-JKMN"

    def test_normalized_generated_id_round_trips(self) -> None:
        room_id = generate_room_id()
        assert normalize_room_id(room_id.lower().replace("gl-room-", "")) == room_id


class TestCreate:
    def test_defaults_create_open_room_without_expiry(self) -> None:
        room = Room.create(now=NOW)
        assert room.state is RoomState.OPEN
        assert room.expires_at is None
        assert room.host_id.startswith("host_")
        assert room.guests == ()
        assert is_valid_room_id(room.room_id)

    def test_named_room_with_lifetime(self) -> None:
        room = Room.create(name="  Midnight Lounge  ", lifetime_minutes=30, now=NOW)
        assert room.name == "Midnight Lounge"
        assert room.expires_at == NOW + timedelta(minutes=30)

    def test_blank_name_becomes_none(self) -> None:
        assert Room.create(name="   ", now=NOW).name is None

    def test_oversized_name_rejected(self) -> None:
        with pytest.raises(ConfigValidationError, match="at most"):
            Room.create(name="x" * 100, now=NOW)

    def test_negative_lifetime_rejected(self) -> None:
        with pytest.raises(ConfigValidationError, match="≥ 0"):
            Room.create(lifetime_minutes=-1, now=NOW)

    def test_display_name_fallback(self) -> None:
        assert Room.create(now=NOW).display_name == "Unnamed room"
        assert Room.create(name="Nook", now=NOW).display_name == "Nook"


class TestEffectiveState:
    def test_open_room_is_open(self) -> None:
        room = Room.create(now=NOW)
        assert room.effective_state() is RoomEffectiveState.OPEN

    def test_expired_room_reports_expired(self) -> None:
        room = Room.create(lifetime_minutes=10, now=NOW)
        later = NOW + timedelta(minutes=11)
        assert room.is_expired(later)
        assert room.effective_state(later) is RoomEffectiveState.EXPIRED

    def test_close_wins_over_expiry(self) -> None:
        room = Room.create(lifetime_minutes=10, now=NOW).close()
        later = NOW + timedelta(minutes=11)
        assert room.state is RoomState.CLOSED
        assert room.effective_state(later) is RoomEffectiveState.CLOSED

    def test_ttl_seconds_counts_down(self) -> None:
        room = Room.create(lifetime_minutes=10, now=datetime.now(UTC))
        ttl = room.ttl_seconds
        assert ttl is not None and 550 <= ttl <= 600
        assert Room.create().ttl_seconds is None


class TestSerialization:
    def test_round_trip_full_document(self) -> None:
        room = Room.create(name="Lounge", lifetime_minutes=45, host_id="host_abc123", now=NOW)
        restored = Room.from_dict(room.to_dict())
        assert restored == room

    def test_round_trip_minimal_document(self) -> None:
        room = Room.create(now=NOW)
        restored = Room.from_dict(room.to_dict())
        assert restored == room
        assert restored.expires_at is None
        assert restored.name is None

    def test_missing_fields_rejected(self) -> None:
        with pytest.raises(ConfigValidationError, match="missing field"):
            Room.from_dict({"room_id": generate_room_id()})

    def test_malformed_id_rejected(self) -> None:
        document = Room.create(now=NOW).to_dict()
        document["room_id"] = "gl-room-INVALID-INVALID-INVALID"
        with pytest.raises(ConfigValidationError, match="malformed"):
            Room.from_dict(document)

    def test_unknown_state_rejected(self) -> None:
        document = Room.create(now=NOW).to_dict()
        document["state"] = "haunted"
        with pytest.raises(ValueError):
            Room.from_dict(document)
