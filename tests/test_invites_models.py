"""Invite record: lifecycle state machine, expiry folding, serialization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ghostlink.exceptions.config import ConfigValidationError
from ghostlink.exceptions.invites import InviteStateError
from ghostlink.invites.models import (
    TERMINAL_STATES,
    InviteRecord,
    InviteState,
    can_transition,
    is_terminal_state,
)
from ghostlink.invites.tokens import generate_invite_token, invite_id_for_token, token_hash_for
from ghostlink.models.room import generate_room_id

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def _record(**overrides: object) -> InviteRecord:
    token = generate_invite_token()
    base: dict[str, object] = {
        "invite_id": invite_id_for_token(token),
        "room_id": generate_room_id(),
        "token_hash": token_hash_for(token),
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=15),
        "max_redemptions": 1,
    }
    base.update(overrides)
    return InviteRecord(**base)  # type: ignore[arg-type]


class TestStateMachine:
    def test_happy_path(self) -> None:
        record = _record()
        record.transition(InviteState.ACTIVE)
        record.transition(InviteState.REDEEMING)
        record.transition(InviteState.REDEEMED)
        assert record.state is InviteState.REDEEMED
        assert record.is_terminal()

    def test_expiry_paths(self) -> None:
        record = _record()
        record.transition(InviteState.ACTIVE)
        record.transition(InviteState.EXPIRED)
        assert record.state in TERMINAL_STATES

    def test_expired_can_be_revoked(self) -> None:
        record = _record()
        record.transition(InviteState.ACTIVE)
        record.transition(InviteState.EXPIRED)
        record.transition(InviteState.REVOKED)
        assert record.state is InviteState.REVOKED

    def test_redeemed_is_final(self) -> None:
        record = _record(state=InviteState.REDEEMED, redemptions=1)
        for target in InviteState:
            if target is InviteState.REDEEMED:
                continue
            with pytest.raises(InviteStateError):
                record.transition(target)

    def test_invalid_transitions_fail_safely(self) -> None:
        record = _record()
        with pytest.raises(InviteStateError, match="cannot move"):
            record.transition(InviteState.REDEEMED)
        with pytest.raises(InviteStateError):
            record.transition(InviteState.REDEEMING)
        assert record.state is InviteState.CREATED

    def test_transition_table_is_exhausted(self) -> None:
        for state in InviteState:
            assert (
                state in TERMINAL_STATES
                or can_transition(state, InviteState.EXPIRED)
                or (state is InviteState.REDEEMING and can_transition(state, InviteState.REDEEMED))
            )

    def test_terminal_helper(self) -> None:
        assert is_terminal_state(InviteState.REVOKED)
        assert is_terminal_state(InviteState.REDEEMED)
        assert not is_terminal_state(InviteState.ACTIVE)


class TestExpiryFolding:
    def test_effective_state_folds_the_deadline(self) -> None:
        record = _record()
        record.transition(InviteState.ACTIVE)
        inside = NOW + timedelta(minutes=14, seconds=59)
        after = NOW + timedelta(minutes=15)
        assert record.effective_state(inside) is InviteState.ACTIVE
        assert record.effective_state(after) is InviteState.EXPIRED
        assert record.state is InviteState.ACTIVE  # folding never mutates

    def test_boundary_is_exclusive_of_the_deadline(self) -> None:
        record = _record()
        just_before = NOW + timedelta(minutes=15) - timedelta(microseconds=1)
        assert not record.is_due(just_before)
        assert record.is_due(NOW + timedelta(minutes=15))

    def test_remaining_seconds_clamps_at_zero(self) -> None:
        record = _record()
        assert record.remaining_seconds(NOW + timedelta(hours=1)) == 0.0
        assert record.remaining_seconds(NOW) == 900.0

    def test_revoked_and_redeemed_never_fold_to_expired(self) -> None:
        revoked = _record(state=InviteState.REVOKED)
        redeemed = _record(state=InviteState.REDEEMED, redemptions=1)
        later = NOW + timedelta(days=7)
        assert revoked.effective_state(later) is InviteState.REVOKED
        assert redeemed.effective_state(later) is InviteState.REDEEMED


class TestConstruction:
    def test_timezone_aware_required(self) -> None:
        naive = datetime(2026, 3, 1, 12, 0, 0)
        with pytest.raises(ConfigValidationError, match="timezone-aware"):
            _record(created_at=naive, expires_at=naive + timedelta(minutes=1))

    def test_deadline_must_follow_creation(self) -> None:
        with pytest.raises(ConfigValidationError, match="deadline"):
            _record(expires_at=NOW)

    def test_redemption_counters_bounded(self) -> None:
        with pytest.raises(ConfigValidationError):
            _record(max_redemptions=0)
        with pytest.raises(ConfigValidationError):
            _record(redemptions=2)
        with pytest.raises(ConfigValidationError):
            _record(redemptions=-1)

    def test_bad_ids_rejected(self) -> None:
        with pytest.raises(ConfigValidationError):
            _record(invite_id="bogus")
        with pytest.raises(ConfigValidationError):
            _record(room_id="gl-room-AAAA")


class TestSerialization:
    def test_round_trip(self) -> None:
        record = _record(
            session_binding="conv_abcdef012345",
            relay_url="ws://127.0.0.1:8787/relay",
        )
        record.transition(InviteState.ACTIVE)
        restored = InviteRecord.from_dict(record.to_dict())
        assert restored.invite_id == record.invite_id
        assert restored.state is InviteState.ACTIVE
        assert restored.expires_at == record.expires_at
        assert restored.created_at.tzinfo is not None
        assert restored.session_binding == "conv_abcdef012345"
        assert restored.protocol_version == record.protocol_version

    def test_no_secret_token_persisted(self) -> None:
        token = generate_invite_token()
        record = _record(token_hash=token_hash_for(token))
        document = record.to_dict()
        assert token not in str(document)
        assert "token" not in document
        assert document["token_hash"] != token

    def test_malformed_documents_rejected(self) -> None:
        with pytest.raises(ConfigValidationError, match="missing"):
            InviteRecord.from_dict({"invite_id": "gi_0000000000"})
        with pytest.raises(ConfigValidationError, match="lifecycle"):
            base = _record().to_dict()
            base["state"] = "quantum"
            InviteRecord.from_dict(base)

    def test_naive_stored_timestamps_become_utc(self) -> None:
        document = _record().to_dict()
        document["created_at"] = "2026-03-01T12:00:00"
        document["expires_at"] = "2026-03-01T12:15:00"
        restored = InviteRecord.from_dict(document)
        assert restored.created_at.tzinfo is UTC
        assert restored.expires_at.tzinfo is UTC
