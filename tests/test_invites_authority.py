"""The relay-side invite authority: expiry, one-shot redemption, revocation.

A fake monotonic clock drives the exact clock-boundary cases; the real
loopback integration lives in test_invites_relay.py.
"""

from __future__ import annotations

import pytest

from ghostlink.invites.authority import InviteAuthority, RedemptionVerdict
from ghostlink.invites.tokens import generate_invite_token
from ghostlink.models.room import generate_room_id


class FakeClock:
    """Controllable monotonic source for deterministic boundary tests."""

    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> FakeClock:
    return FakeClock()


def _register(
    authority: InviteAuthority, *, ttl: float = 60.0, uses: int = 1, session: str = "sess_creator"
) -> str:
    token = generate_invite_token()
    authority.register(
        token,
        room_id=generate_room_id(),
        ttl_seconds=ttl,
        max_redemptions=uses,
        creator_session=session,
    )
    return token


class TestRegistration:
    def test_grant_returns_public_id(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        room = generate_room_id()
        grant = authority.register(
            generate_invite_token(),
            room_id=room,
            ttl_seconds=30.0,
            max_redemptions=1,
            creator_session="sess_a",
        )
        assert grant.invite_id.startswith("gi_")
        assert grant.max_redemptions == 1
        assert grant.expires_at.tzinfo is not None

    def test_rejects_malformed_inputs(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        with pytest.raises(ValueError):
            authority.register(
                "BAD!",
                room_id=generate_room_id(),
                ttl_seconds=10,
                max_redemptions=1,
                creator_session="x",
            )
        with pytest.raises(ValueError):
            authority.register(
                generate_invite_token(),
                room_id="gl-room-AAAA",
                ttl_seconds=10,
                max_redemptions=1,
                creator_session="x",
            )
        with pytest.raises(ValueError):
            authority.register(
                generate_invite_token(),
                room_id=generate_room_id(),
                ttl_seconds=0.2,
                max_redemptions=1,
                creator_session="x",
            )
        with pytest.raises(ValueError):
            authority.register(
                generate_invite_token(),
                room_id=generate_room_id(),
                ttl_seconds=999_999,
                max_redemptions=1,
                creator_session="x",
            )
        with pytest.raises(ValueError):
            authority.register(
                generate_invite_token(),
                room_id=generate_room_id(),
                ttl_seconds=10,
                max_redemptions=0,
                creator_session="x",
            )

    def test_same_token_twice_fails_closed(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        token = generate_invite_token()
        room = generate_room_id()
        authority.register(
            token, room_id=room, ttl_seconds=10, max_redemptions=1, creator_session="sess_a"
        )
        with pytest.raises(ValueError, match="already registered"):
            authority.register(
                token, room_id=room, ttl_seconds=10, max_redemptions=1, creator_session="sess_a"
            )

    def test_different_tokens_different_invites(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        first = authority.register(
            generate_invite_token(),
            room_id=generate_room_id(),
            ttl_seconds=10,
            max_redemptions=1,
            creator_session="a",
        )
        second = authority.register(
            generate_invite_token(),
            room_id=generate_room_id(),
            ttl_seconds=10,
            max_redemptions=1,
            creator_session="a",
        )
        assert first.invite_id != second.invite_id


class TestRedemption:
    def test_one_time_redemption_succeeds_once(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        token = _register(authority)
        result = authority.redeem(token, redeem_session="sess_guest")
        assert result.verdict is RedemptionVerdict.REDEEMED
        assert result.room_id is not None

    def test_second_redemption_is_already_used(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        token = _register(authority)
        assert authority.redeem(token, redeem_session="g1").verdict is RedemptionVerdict.REDEEMED
        retry = authority.redeem(token, redeem_session="g1")
        assert retry.verdict is RedemptionVerdict.ALREADY_USED
        other = authority.redeem(token, redeem_session="g2")
        assert other.verdict is RedemptionVerdict.ALREADY_USED

    def test_concurrent_attempts_only_one_wins(self, clock: FakeClock) -> None:
        """The atomic transition grants exactly one success — no interleaving."""
        authority = InviteAuthority(monotonic=clock)
        token = _register(authority)
        # Back-to-back (the await-free transition is what real concurrent
        # connections reduce to on a single event loop).
        verdicts = [
            authority.redeem(token, redeem_session=f"sess_{index}").verdict for index in range(8)
        ]
        assert verdicts.count(RedemptionVerdict.REDEEMED) == 1
        assert verdicts.count(RedemptionVerdict.ALREADY_USED) == 7

    def test_multi_use_invite_allows_configured_count(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        token = _register(authority, uses=3)
        sessions = ["g1", "g2", "g3", "g4"]
        verdicts = [authority.redeem(token, redeem_session=s).verdict for s in sessions]
        assert verdicts == ([RedemptionVerdict.REDEEMED] * 3 + [RedemptionVerdict.ALREADY_USED])

    def test_redemption_binds_the_session(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        token = _register(authority)
        authority.redeem(token, redeem_session="sess_bound")
        status = authority.status(token, requester_session="sess_creator")
        assert status is not None
        assert status.bound_session == "sess_bound"
        assert status.state == "redeemed"

    def test_unknown_token_fails_closed(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        result = authority.redeem(generate_invite_token(), redeem_session="g")
        assert result.verdict is RedemptionVerdict.UNKNOWN
        assert result.room_id is None

    def test_malformed_token_fails_closed(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        assert authority.redeem("!!!", redeem_session="g").verdict is RedemptionVerdict.UNKNOWN


class TestExpiration:
    def test_expired_invite_refuses_redemption(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        token = _register(authority, ttl=2.0)
        clock.advance(2.0)
        result = authority.redeem(token, redeem_session="g")
        assert result.verdict is RedemptionVerdict.EXPIRED
        # …and stays that way, no matter how often retried.
        assert authority.redeem(token, redeem_session="g").verdict is RedemptionVerdict.EXPIRED

    def test_redemption_exactly_at_the_deadline_fails(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        token = _register(authority, ttl=5.0)
        clock.advance(4.999)
        assert authority.redeem(token, redeem_session="g1").verdict is RedemptionVerdict.REDEEMED
        token_two = _register(authority, ttl=5.0)
        clock.advance(5.0)
        assert authority.redeem(token_two, redeem_session="g2").verdict is (
            RedemptionVerdict.EXPIRED
        )

    def test_expiry_enforced_even_if_system_time_looks_earlier(self, clock: FakeClock) -> None:
        """Monotonic enforcement ignores wall-clock drift between peers."""
        authority = InviteAuthority(monotonic=clock)
        token = _register(authority, ttl=1.0)
        clock.advance(1.5)  # monotonic moved on; a peer's wall clock may not have
        assert authority.redeem(token, redeem_session="g").verdict is RedemptionVerdict.EXPIRED

    def test_status_reports_remaining_lifetime(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        token = _register(authority, ttl=10.0)
        clock.advance(4.0)
        status = authority.status(token, requester_session="sess_creator")
        assert status is not None
        assert status.state == "active"
        assert 5.9 <= status.remaining_seconds <= 6.0

    def test_status_of_expired_is_terminal(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        token = _register(authority, ttl=1.0)
        clock.advance(2.0)
        status = authority.status(token, requester_session="sess_creator")
        assert status is not None
        assert status.state == "expired"


class TestRevocation:
    def test_revoked_invite_refuses_redemption(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        token = _register(authority)
        assert authority.revoke(token, requester_session="sess_creator") is not None
        assert authority.redeem(token, redeem_session="g").verdict is RedemptionVerdict.REVOKED
        assert authority.redeem(token, redeem_session="g").verdict is RedemptionVerdict.REVOKED

    def test_only_the_creator_may_revoke(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        token = _register(authority)
        with pytest.raises(PermissionError):
            authority.revoke(token, requester_session="sess_intruder")
        # The invite survives the failed attempt untouched.
        assert authority.redeem(token, redeem_session="g").verdict is RedemptionVerdict.REDEEMED

    def test_revoke_unknown_is_none(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        assert authority.revoke(generate_invite_token(), requester_session="x") is None

    def test_revoked_state_visible_in_status(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        token = _register(authority)
        status = authority.revoke(token, requester_session="sess_creator")
        assert status is not None
        assert status.state == "revoked"


class TestPrivacyAndViews:
    def test_room_visible_to_creator_only(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        token = _register(authority)
        creator_status = authority.status(token, requester_session="sess_creator")
        outsider_status = authority.status(token, requester_session="sess_stranger")
        assert creator_status is not None and creator_status.room_id is not None
        assert outsider_status is not None and outsider_status.room_id is None

    def test_registry_never_stores_or_returns_tokens(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock)
        token = _register(authority)
        body = repr(vars(authority))
        assert token not in body


class TestRetention:
    def test_terminal_entries_purge_after_retention(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock, retention_hours=1.0)
        token = _register(authority, ttl=60.0)
        authority.redeem(token, redeem_session="g")
        assert authority.size == 1
        clock.advance(3600 + 61)
        assert authority.purge_terminal() == 1
        assert authority.size == 0

    def test_active_entries_survive_purges(self, clock: FakeClock) -> None:
        authority = InviteAuthority(monotonic=clock, retention_hours=1.0)
        _register(authority, ttl=7200.0)
        clock.advance(3700)
        assert authority.purge_terminal() == 0
        assert authority.size == 1
