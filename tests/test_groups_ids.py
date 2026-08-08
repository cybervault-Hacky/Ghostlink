"""Group identifiers: shape, normalization, and validation (Phase 6B)."""

from __future__ import annotations

from ghostlink.constants.net import GROUP_ID_PREFIX
from ghostlink.groups.ids import (
    generate_group_id,
    is_valid_group_id,
    normalize_group_id,
)


class TestGroupIdGeneration:
    def test_generate_matches_canonical_shape(self) -> None:
        for _ in range(64):
            candidate = generate_group_id()
            assert is_valid_group_id(candidate), candidate

    def test_generate_uses_group_prefix(self) -> None:
        assert generate_group_id().startswith(f"{GROUP_ID_PREFIX}-")

    def test_generate_produces_distinct_ids(self) -> None:
        produced = {generate_group_id() for _ in range(256)}
        assert len(produced) == 256

    def test_ids_are_never_room_ids(self) -> None:
        from ghostlink.models.room import is_valid_room_id

        for _ in range(32):
            assert not is_valid_room_id(generate_group_id())


class TestGroupIdNormalization:
    def test_normalize_lowercase_and_whitespace(self) -> None:
        raw = "  gl-group-abcd-efgh-jkmn "
        normalized = normalize_group_id(raw)
        assert normalized == "gl-group-ABCD-EFGH-JKMN"
        assert is_valid_group_id(normalized)

    def test_normalize_accepts_missing_prefix_case(self) -> None:
        normalized = normalize_group_id("GL-GROUP-W7PP-3FSJ-T78L")
        assert normalized == "gl-group-W7PP-3FSJ-T78L"


class TestGroupIdValidation:
    def test_valid_examples(self) -> None:
        assert is_valid_group_id("gl-group-AAAA-BBBB-CCCC")
        assert is_valid_group_id("gl-group-2345-6789-ABCD")

    def test_rejects_room_prefix(self) -> None:
        assert not is_valid_group_id("gl-room-AAAA-BBBB-CCCC")

    def test_rejects_bad_length(self) -> None:
        assert not is_valid_group_id("gl-group-AAAA-BBBB-CCC")
        assert not is_valid_group_id("gl-group-AAAA-BBBB-CCCCC")

    def test_rejects_ambiguous_characters(self) -> None:
        # The alphabet excludes 0/O/1/I — ids must never contain them.
        assert not is_valid_group_id("gl-group-0AAA-BBBB-CCCC")
        assert not is_valid_group_id("gl-group-IAAA-BBBB-CCCC")

    def test_rejects_lowercase_groups(self) -> None:
        # Canonical form has uppercase groups; lowercase must normalize first.
        assert not is_valid_group_id("gl-group-aaaa-bbbb-cccc")
        assert is_valid_group_id(normalize_group_id("gl-group-aaaa-bbbb-cccc"))

    def test_rejects_empty_and_garbage(self) -> None:
        assert not is_valid_group_id("")
        assert not is_valid_group_id("hello")
        assert not is_valid_group_id("gl-group")
