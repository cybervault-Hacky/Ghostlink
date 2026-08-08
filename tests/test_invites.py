"""Invite model: tokens, one-time redemption, expiry, serialization."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ghostlink.exceptions.config import ConfigValidationError
from ghostlink.models.invite import (
    Invite,
    InviteEffectiveState,
    InviteState,
    generate_invite_token,
    is_valid_invite_token,
)

# Anchored to the real wall clock so no-argument effective-state calls
# (``is_redeemable()``, ``redeem()`` …) stay inside the invite's lifetime.
NOW = datetime.now(UTC)


def _invite(**overrides: object) -> Invite:
    options: dict[str, object] = {
        "room_id": "gl-room-ABCD-EFGH-JKMN",
        "lifetime_minutes": 15,
        "now": NOW,
    }
    options.update(overrides)
    return Invite.create(**options)  # type: ignore[arg-type]


class TestTokens:
    def test_generated_token_format(self) -> None:
        for _ in range(50):
            token = generate_invite_token()
            assert token.startswith("gli_")
            assert len(token) == 36
            assert is_valid_invite_token(token)

    def test_generated_tokens_are_unique(self) -> None:
        assert generate_invite_token() != generate_invite_token()

    @pytest.mark.parametrize(
        "candidate",
        [
            "gli_short",
            "gli_" + "a" * 31,
            "GLI_" + "a" * 32,
            "xxx_" + "a" * 32,
            "gli_" + "a!" * 16,
            "",
        ],
    )
    def test_invalid_tokens_rejected(self, candidate: str) -> None:
        assert not is_valid_invite_token(candidate)


class TestCreate:
    def test_default_invite_is_active_and_one_time(self) -> None:
        invite = _invite()
        assert invite.state is InviteState.ACTIVE
        assert invite.one_time is True
        assert invite.uses == 0
        assert invite.expires_at == NOW + timedelta(minutes=15)

    def test_custom_lifetime(self) -> None:
        invite = _invite(lifetime_minutes=120)
        assert invite.expires_at == NOW + timedelta(minutes=120)

    def test_lifetime_below_one_minute_rejected(self) -> None:
        with pytest.raises(ConfigValidationError, match="at least 1 minute"):
            _invite(lifetime_minutes=0)

    def test_is_redeemable_when_active(self) -> None:
        invite = _invite()
        assert invite.is_redeemable()
        assert not invite.is_redeemable(NOW + timedelta(minutes=16))


class TestRedemption:
    def test_one_time_invite_becomes_redeemed(self) -> None:
        invite = _invite().redeem()
        assert invite.state is InviteState.REDEEMED
        assert invite.uses == 1
        assert invite.effective_state() is InviteEffectiveState.REDEEMED

    def test_one_time_invite_cannot_be_redeemed_twice(self) -> None:
        invite = _invite().redeem()
        with pytest.raises(ConfigValidationError, match="not redeemable"):
            invite.redeem()

    def test_multi_use_invite_stays_active(self) -> None:
        invite = _invite(one_time=False)
        first = invite.redeem()
        second = first.redeem()
        assert first.state is InviteState.ACTIVE
        assert second.uses == 2
        assert second.effective_state() is InviteEffectiveState.ACTIVE

    def test_expired_invite_cannot_be_redeemed(self) -> None:
        expired = Invite(
            token=generate_invite_token(),
            room_id="gl-room-ABCD-EFGH-JKMN",
            created_at=NOW - timedelta(hours=2),
            expires_at=NOW - timedelta(hours=1),
            one_time=True,
            state=InviteState.ACTIVE,
        )
        assert expired.effective_state(NOW) is InviteEffectiveState.EXPIRED
        with pytest.raises(ConfigValidationError, match="not redeemable"):
            expired.redeem()


class TestRevocation:
    def test_revoked_invite_reports_revoked(self) -> None:
        invite = _invite().revoke()
        assert invite.state is InviteState.REVOKED
        assert invite.effective_state() is InviteEffectiveState.REVOKED

    def test_revoked_invite_cannot_be_redeemed(self) -> None:
        with pytest.raises(ConfigValidationError, match="not redeemable"):
            _invite().revoke().redeem()

    def test_revoke_wins_over_redeemed_and_expiry(self) -> None:
        invite = _invite().redeem().revoke()
        assert invite.effective_state() is InviteEffectiveState.REVOKED


class TestExpiry:
    def test_expiry_boundary(self) -> None:
        invite = _invite()
        assert not invite.is_expired(NOW + timedelta(minutes=14, seconds=59))
        assert invite.is_expired(NOW + timedelta(minutes=15))

    def test_expired_reports_expired_only_while_active(self) -> None:
        invite = _invite().redeem()
        later = NOW + timedelta(hours=1)
        assert invite.effective_state(later) is InviteEffectiveState.REDEEMED


class TestSerialization:
    def test_round_trip(self) -> None:
        invite = _invite(one_time=False).redeem()
        restored = Invite.from_dict(invite.to_dict())
        assert restored == invite

    def test_missing_fields_rejected(self) -> None:
        with pytest.raises(ConfigValidationError, match="missing field"):
            Invite.from_dict({"token": generate_invite_token()})

    def test_malformed_token_rejected(self) -> None:
        document = _invite().to_dict()
        document["token"] = "not-an-invite"
        with pytest.raises(ConfigValidationError, match="malformed"):
            Invite.from_dict(document)

    def test_uses_defaults_to_zero_for_legacy_documents(self) -> None:
        document = _invite().to_dict()
        del document["uses"]
        assert Invite.from_dict(document).uses == 0
