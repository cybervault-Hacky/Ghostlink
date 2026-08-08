"""GroupAuthority: the relay-side roster/epoch registry (Phase 6B).

Clocks are injected so every timeout path is deterministic; all identity
material is real Ed25519 from the Phase 5 identity module.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta

import pytest

from ghostlink.constants.net import MAX_GROUP_MEMBERS
from ghostlink.exceptions.groups import (
    GroupConflictError,
    GroupDissolvedError,
    GroupFullError,
    GroupNotMemberError,
    GroupPermissionError,
    GroupUnknownError,
    GroupValidationError,
)
from ghostlink.groups.authority import GroupAuthority, OpKind
from ghostlink.identity.identity import LocalIdentity
from tests.group_helpers import (
    attest_b64,
    fingerprint_of,
    pop_b64,
    sign_b64,
)

T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
NONCE = "session-nonce-1"


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture()
def clock() -> Clock:
    return Clock()


@pytest.fixture()
def authority(clock: Clock) -> GroupAuthority:
    return GroupAuthority(
        monotonic=clock.monotonic,
        wall_clock=lambda: T0 + timedelta(seconds=clock.now),
        join_pending_seconds=300.0,
        sign_timeout_seconds=60.0,
        retention_hours=24.0,
    )


def _owner() -> LocalIdentity:
    return LocalIdentity.generate()


def _create(authority: GroupAuthority, owner: LocalIdentity, *, session: str = "owner-s") -> str:
    snapshot = authority.create_group(
        session_id=session,
        name="Ops",
        public_key_hex=owner.public_key_hex,
        pop_signature_b64=pop_b64(owner, "Ops", NONCE),
        attest_nonce=NONCE,
        handle=owner.identity_id,
        display_name="",
    )
    return snapshot.group_id


def _reserve(authority: GroupAuthority, group_id: str, session: str) -> None:
    authority.begin_join_redemption(group_id, redeem_session=session, invite_id="gi_x")


def _attest_candidate(
    authority: GroupAuthority, group_id: str, guest: LocalIdentity, session: str
) -> None:
    authority.attest(
        session_id=session,
        group_id=group_id,
        public_key_hex=guest.public_key_hex,
        signature_b64=attest_b64(guest, group_id, NONCE),
        attest_nonce=NONCE,
        handle=guest.identity_id,
        display_name="",
    )


def _admit(
    authority: GroupAuthority,
    group_id: str,
    guest: LocalIdentity,
    *,
    session: str,
    owner: LocalIdentity,
    owner_session: str = "owner-s",
) -> None:
    """Reserve → attest → owner countersigns the admission."""

    _reserve(authority, group_id, session)
    _attest_candidate(authority, group_id, guest, session)
    task = authority.next_sign_task(group_id)
    assert task is not None
    authority.submit_signature(
        group_id,
        owner_session,
        task.op_id,
        sign_b64(owner, base64.b64decode(task.message_b64)),
    )


class TestCreation:
    def test_create_happy_path(self, authority: GroupAuthority) -> None:
        owner = _owner()
        snapshot = authority.create_group(
            session_id="s1",
            name="  Ops  ",
            public_key_hex=owner.public_key_hex,
            pop_signature_b64=pop_b64(owner, "Ops", NONCE),
            attest_nonce=NONCE,
            handle=owner.identity_id,
            display_name="Boss",
        )
        assert snapshot.epoch == 1
        assert snapshot.state == "active"
        assert snapshot.owner_fingerprint == fingerprint_of(owner)
        assert len(snapshot.members) == 1
        member = snapshot.members[0]
        assert member.role == "owner"
        assert member.joined_epoch == 1
        assert snapshot.group_id.startswith("gl-group-")

    def test_create_ids_never_collide(self, authority: GroupAuthority) -> None:
        owner = _owner()
        ids = {_create(authority, owner, session=f"s{i}") for i in range(8)}
        assert len(ids) == 8

    def test_bad_pop_signature_refused(self, authority: GroupAuthority) -> None:
        owner, other = LocalIdentity.generate(), LocalIdentity.generate()
        with pytest.raises(GroupPermissionError):
            authority.create_group(
                session_id="s1",
                name="Ops",
                public_key_hex=owner.public_key_hex,
                pop_signature_b64=pop_b64(other, "Ops", NONCE),  # signed by another key
                attest_nonce=NONCE,
                handle=owner.identity_id,
                display_name="",
            )

    def test_pop_over_wrong_nonce_refused(self, authority: GroupAuthority) -> None:
        owner = _owner()
        with pytest.raises(GroupPermissionError):
            authority.create_group(
                session_id="s1",
                name="Ops",
                public_key_hex=owner.public_key_hex,
                pop_signature_b64=pop_b64(owner, "Ops", "different-nonce"),
                attest_nonce=NONCE,
                handle=owner.identity_id,
                display_name="",
            )

    def test_pop_over_wrong_name_refused(self, authority: GroupAuthority) -> None:
        owner = _owner()
        with pytest.raises(GroupPermissionError):
            authority.create_group(
                session_id="s1",
                name="Ops",
                public_key_hex=owner.public_key_hex,
                pop_signature_b64=pop_b64(owner, "OtherName", NONCE),
                attest_nonce=NONCE,
                handle=owner.identity_id,
                display_name="",
            )

    def test_handle_key_mismatch_refused(self, authority: GroupAuthority) -> None:
        owner, other = LocalIdentity.generate(), LocalIdentity.generate()
        with pytest.raises(GroupValidationError):
            authority.create_group(
                session_id="s1",
                name="Ops",
                public_key_hex=owner.public_key_hex,
                pop_signature_b64=pop_b64(owner, "Ops", NONCE),
                attest_nonce=NONCE,
                handle=other.identity_id,  # borrowed handle
                display_name="",
            )

    def test_malformed_inputs_refused(self, authority: GroupAuthority) -> None:
        owner = _owner()
        with pytest.raises(GroupValidationError):
            authority.create_group(
                session_id="s1",
                name="",  # empty name
                public_key_hex=owner.public_key_hex,
                pop_signature_b64=pop_b64(owner, "", NONCE),
                attest_nonce=NONCE,
                handle=owner.identity_id,
                display_name="",
            )
        with pytest.raises(GroupValidationError):
            authority.create_group(
                session_id="s1",
                name="Ops",
                public_key_hex="abc",  # short key
                pop_signature_b64=pop_b64(owner, "Ops", NONCE),
                attest_nonce=NONCE,
                handle=owner.identity_id,
                display_name="",
            )
        with pytest.raises(GroupValidationError):
            authority.create_group(
                session_id="s1",
                name="Ops",
                public_key_hex=owner.public_key_hex,
                pop_signature_b64="!!! not base64 !!!",
                attest_nonce=NONCE,
                handle=owner.identity_id,
                display_name="",
            )


class TestInviteAllowance:
    def test_owner_may_mint_within_headroom(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        authority.allow_invite_mint(group_id, "owner-s", uses=MAX_GROUP_MEMBERS - 1)

    def test_non_owner_cannot_mint(self, authority: GroupAuthority) -> None:
        owner, guest = _owner(), LocalIdentity.generate()
        group_id = _create(authority, owner)
        _admit(authority, group_id, guest, session="guest-s", owner=owner)
        with pytest.raises(GroupPermissionError):
            authority.allow_invite_mint(group_id, "guest-s", uses=1)

    def test_uses_beyond_headroom_refused(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        _reserve(authority, group_id, "p1")
        with pytest.raises(GroupFullError):
            authority.allow_invite_mint(group_id, "owner-s", uses=MAX_GROUP_MEMBERS - 1)
        # Within the (smaller) remaining headroom it is fine.
        authority.allow_invite_mint(group_id, "owner-s", uses=MAX_GROUP_MEMBERS - 2)

    def test_unknown_group_refused(self, authority: GroupAuthority) -> None:
        with pytest.raises(GroupUnknownError):
            authority.allow_invite_mint("gl-group-AAAA-BBBB-CCCC", "s", uses=1)


class TestRedemptionReservations:
    def test_pending_join_reserves_a_seat(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        snapshot = _reserve_and_get(authority, group_id, "r1")
        assert snapshot.group_id == group_id
        assert authority.pending_join_sessions(group_id) == ("r1",)

    def test_duplicate_pending_session_conflicts(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        _reserve(authority, group_id, "r1")
        with pytest.raises(GroupConflictError):
            _reserve(authority, group_id, "r1")

    def test_capacity_is_atomic_across_pending_and_members(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        # Owner + 7 reservations = 8 seats; the 9th must be refused.
        for index in range(MAX_GROUP_MEMBERS - 1):
            _reserve(authority, group_id, f"r{index}")
        with pytest.raises(GroupFullError):
            _reserve(authority, group_id, "r-overflow")
        # Rolling a slot back frees capacity again.
        authority.cancel_pending_join(group_id, "r0")
        _reserve(authority, group_id, "r-overflow")

    def test_snapshot_counts_active_members_only(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        guest = LocalIdentity.generate()
        _reserve(authority, group_id, "g1")
        _attest_candidate(authority, group_id, guest, "g1")
        snapshot = authority.inspect(group_id)
        assert snapshot is not None
        assert len(snapshot.members) == 1  # candidate not yet admitted


def _reserve_and_get(authority: GroupAuthority, group_id: str, session: str):  # GroupSnapshot
    return authority.begin_join_redemption(group_id, redeem_session=session, invite_id="gi_x")


class TestAttestation:
    def test_candidate_needs_pending_admission(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        guest = LocalIdentity.generate()
        with pytest.raises(GroupNotMemberError):
            _attest_candidate(authority, group_id, guest, "g1")

    def test_candidate_attest_enqueues_owner_signed_op(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        guest = LocalIdentity.generate()
        _reserve(authority, group_id, "g1")
        outcome = _attest_candidate_and_get(authority, group_id, guest, "g1")
        assert outcome.role == "candidate"
        assert outcome.snapshot is None
        task = authority.next_sign_task(group_id)
        assert task is not None
        assert task.kind == OpKind.JOIN.value
        assert task.subject_fingerprint == fingerprint_of(guest)
        assert task.signer_fingerprint == fingerprint_of(owner)  # owner countersigns
        assert task.epoch == 2

    def test_member_re_attest_rebinds_session(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        outcome = authority.attest(
            session_id="owner-s-2",
            group_id=group_id,
            public_key_hex=owner.public_key_hex,
            signature_b64=attest_b64(owner, group_id, NONCE),
            attest_nonce=NONCE,
            handle=owner.identity_id,
            display_name="",
        )
        assert outcome.role == "owner"
        assert outcome.snapshot is not None
        assert authority.session_fingerprint(group_id, "owner-s-2") == fingerprint_of(owner)

    def test_bad_attestation_signature_refused(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        other = LocalIdentity.generate()
        with pytest.raises(GroupPermissionError):
            authority.attest(
                session_id="g1",
                group_id=group_id,
                public_key_hex=other.public_key_hex,
                signature_b64=attest_b64(owner, group_id, NONCE),  # wrong key signed
                attest_nonce=NONCE,
                handle=other.identity_id,
                display_name="",
            )

    def test_attest_over_wrong_nonce_refused(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        with pytest.raises(GroupPermissionError):
            authority.attest(
                session_id="owner-s-2",
                group_id=group_id,
                public_key_hex=owner.public_key_hex,
                signature_b64=attest_b64(owner, group_id, "another-nonce"),
                attest_nonce=NONCE,
                handle=owner.identity_id,
                display_name="",
            )

    def test_unknown_and_malformed_groups(self, authority: GroupAuthority) -> None:
        guest = LocalIdentity.generate()
        with pytest.raises(GroupValidationError):
            _attest_candidate(authority, "not-a-group", guest, "g1")
        with pytest.raises(GroupUnknownError):
            _attest_candidate(authority, "gl-group-AAAA-BBBB-CCCC", guest, "g1")


def _attest_candidate_and_get(authority, group_id, guest, session):  # AttestOutcome
    return authority.attest(
        session_id=session,
        group_id=group_id,
        public_key_hex=guest.public_key_hex,
        signature_b64=attest_b64(guest, group_id, NONCE),
        attest_nonce=NONCE,
        handle=guest.identity_id,
        display_name="",
    )


class TestSignedCommits:
    def test_owner_countersign_admission_commits(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        guest = LocalIdentity.generate()
        _admit(authority, group_id, guest, session="g1", owner=owner)
        snapshot = authority.inspect(group_id)
        assert snapshot is not None
        assert snapshot.epoch == 2
        assert len(snapshot.members) == 2
        admitted = [m for m in snapshot.members if m.fingerprint == fingerprint_of(guest)]
        assert admitted and admitted[0].joined_epoch == 2
        assert snapshot.events and snapshot.events[-1]["kind"] == "join"

    def test_candidate_can_never_self_admit(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        guest = LocalIdentity.generate()
        _reserve(authority, group_id, "g1")
        _attest_candidate(authority, group_id, guest, "g1")
        task = authority.next_sign_task(group_id)
        assert task is not None
        assert task.signer_fingerprint == fingerprint_of(owner)
        # The candidate's own session cannot settle the op.
        with pytest.raises(GroupPermissionError):
            authority.submit_signature(
                group_id, "g1", task.op_id, sign_b64(guest, base64.b64decode(task.message_b64))
            )
        group = authority.inspect(group_id)
        assert group is not None and group.epoch == 1  # admission did not commit

    def test_invalid_signature_aborts_op(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        guest = LocalIdentity.generate()
        _reserve(authority, group_id, "g1")
        _attest_candidate(authority, group_id, guest, "g1")
        task = authority.next_sign_task(group_id)
        assert task is not None
        # Sign something else with the right key — still the wrong message.
        with pytest.raises(GroupPermissionError):
            authority.submit_signature(
                group_id, "owner-s", task.op_id, sign_b64(owner, b"wrong bytes")
            )
        assert authority.next_sign_task(group_id) is None  # op aborted
        snapshot = authority.inspect(group_id)
        assert snapshot is not None and snapshot.epoch == 1
        # The aborted candidate is gone — a fresh redemption is required.
        assert fingerprint_of(guest) not in {m.fingerprint for m in snapshot.members}

    def test_stale_op_id_conflicts(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        guest = LocalIdentity.generate()
        _admit(authority, group_id, guest, session="g1", owner=owner)
        with pytest.raises(GroupConflictError):
            authority.submit_signature(group_id, "owner-s", "op-does-not-exist", "AAAA")

    def test_epochs_increment_once_per_commit(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        guests = [LocalIdentity.generate() for _ in range(3)]
        for index, guest in enumerate(guests):
            _admit(authority, group_id, guest, session=f"g{index}", owner=owner)
        snapshot = authority.inspect(group_id)
        assert snapshot is not None
        assert snapshot.epoch == 4  # 1 + 3 admissions, strictly sequential
        epochs = [event["epoch"] for event in snapshot.events]
        assert epochs == sorted(epochs) == [2, 3, 4]  # no duplicates, no gaps

    def test_parallel_admissions_serialize_deterministically(
        self, authority: GroupAuthority
    ) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        guests = [LocalIdentity.generate() for _ in range(2)]
        for index, guest in enumerate(guests):
            _reserve(authority, group_id, f"g{index}")
            _attest_candidate(authority, group_id, guest, f"g{index}")
        # Only the queue head is promotable at a time.
        first = authority.next_sign_task(group_id)
        assert first is not None and first.epoch == 2
        again = authority.next_sign_task(group_id)
        assert again is not None and again.op_id == first.op_id  # idempotent promotion
        committed = authority.submit_signature(
            group_id, "owner-s", first.op_id, sign_b64(owner, base64.b64decode(first.message_b64))
        )
        assert committed.event.epoch == 2
        second = authority.next_sign_task(group_id)
        assert second is not None and second.epoch == 3
        assert second.subject_fingerprint != first.subject_fingerprint
        authority.submit_signature(
            group_id,
            "owner-s",
            second.op_id,
            sign_b64(owner, base64.b64decode(second.message_b64)),
        )
        snapshot = authority.inspect(group_id)
        assert snapshot is not None
        assert snapshot.epoch == 3
        assert {m.fingerprint for m in snapshot.members} == {
            fingerprint_of(owner),
            fingerprint_of(guests[0]),
            fingerprint_of(guests[1]),
        }

    def test_removed_member_receives_final_event(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        guest = LocalIdentity.generate()
        _admit(authority, group_id, guest, session="g1", owner=owner)
        authority.request_remove(group_id, "owner-s", fingerprint_of(guest))
        task = authority.next_sign_task(group_id)
        assert task is not None and task.kind == OpKind.REMOVED.value
        committed = authority.submit_signature(
            group_id, "owner-s", task.op_id, sign_b64(owner, base64.b64decode(task.message_b64))
        )
        # The departed member's session is still notified and then unbound.
        assert "g1" in committed.notify_sessions
        assert "g1" in committed.unbind_sessions
        assert authority.session_fingerprint(group_id, "g1") is None


class TestLeaveRemoveDissolve:
    def test_member_leave_self_signed(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        guest = LocalIdentity.generate()
        _admit(authority, group_id, guest, session="g1", owner=owner)
        authority.request_leave(group_id, "g1")
        task = authority.next_sign_task(group_id)
        assert task is not None
        assert task.kind == OpKind.LEFT.value
        assert task.signer_fingerprint == fingerprint_of(guest)  # leaver signs
        committed = authority.submit_signature(
            group_id, "g1", task.op_id, sign_b64(guest, base64.b64decode(task.message_b64))
        )
        assert committed.event.epoch == 3
        snapshot = authority.inspect(group_id)
        assert snapshot is not None and len(snapshot.members) == 1

    def test_owner_cannot_leave(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        with pytest.raises(GroupPermissionError):
            authority.request_leave(group_id, "owner-s")

    def test_non_member_cannot_leave(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        with pytest.raises(GroupNotMemberError):
            authority.request_leave(group_id, "stranger")

    def test_remove_requires_owner(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        guest = LocalIdentity.generate()
        _admit(authority, group_id, guest, session="g1", owner=owner)
        with pytest.raises(GroupPermissionError):
            authority.request_remove(group_id, "g1", fingerprint_of(owner))

    def test_remove_unknown_member_refused(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        stranger = LocalIdentity.generate()
        with pytest.raises(GroupNotMemberError):
            authority.request_remove(group_id, "owner-s", fingerprint_of(stranger))

    def test_owner_cannot_remove_self(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        with pytest.raises(GroupPermissionError):
            authority.request_remove(group_id, "owner-s", fingerprint_of(owner))

    def test_removed_identity_cannot_re_attest_without_invite(
        self, authority: GroupAuthority
    ) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        guest = LocalIdentity.generate()
        _admit(authority, group_id, guest, session="g1", owner=owner)
        authority.request_remove(group_id, "owner-s", fingerprint_of(guest))
        task = authority.next_sign_task(group_id)
        assert task is not None
        authority.submit_signature(
            group_id, "owner-s", task.op_id, sign_b64(owner, base64.b64decode(task.message_b64))
        )
        with pytest.raises(GroupNotMemberError):
            _attest_candidate(authority, group_id, guest, "g1-b")

    def test_dissolve_is_owner_only_and_terminal(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        guest = LocalIdentity.generate()
        _admit(authority, group_id, guest, session="g1", owner=owner)
        with pytest.raises(GroupPermissionError):
            authority.request_dissolve(group_id, "g1")
        authority.request_dissolve(group_id, "owner-s")
        task = authority.next_sign_task(group_id)
        assert task is not None and task.kind == OpKind.DISSOLVED.value
        committed = authority.submit_signature(
            group_id, "owner-s", task.op_id, sign_b64(owner, base64.b64decode(task.message_b64))
        )
        assert committed.event.kind.value == "dissolved"
        assert committed.unbind_sessions  # everyone unbound
        assert authority.group_state_name(group_id) == "dissolved"
        with pytest.raises(GroupDissolvedError):
            authority.request_dissolve(group_id, "owner-s")
        with pytest.raises(GroupDissolvedError):
            _attest_candidate(authority, group_id, guest, "g1-z")


class TestSweep:
    def test_pending_join_times_out(self, authority: GroupAuthority, clock: Clock) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        _reserve(authority, group_id, "stale")
        clock.advance(301.0)
        notices = authority.sweep()
        assert [n.code for n in notices] == ["group/join-timeout"]
        assert notices[0].session_id == "stale"
        assert authority.pending_join_sessions(group_id) == ()

    def test_unsigned_admission_aborts_after_timeout(
        self, authority: GroupAuthority, clock: Clock
    ) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        guest = LocalIdentity.generate()
        _reserve(authority, group_id, "g1")
        _attest_candidate(authority, group_id, guest, "g1")
        assert authority.next_sign_task(group_id) is not None
        clock.advance(61.0)
        notices = authority.sweep()
        assert any(n.code == "group/join-timeout" and n.session_id == "g1" for n in notices)
        assert authority.next_sign_task(group_id) is None
        snapshot = authority.inspect(group_id)
        assert snapshot is not None and len(snapshot.members) == 1

    def test_dissolved_tombstones_purge_after_retention(
        self, authority: GroupAuthority, clock: Clock
    ) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        authority.request_dissolve(group_id, "owner-s")
        task = authority.next_sign_task(group_id)
        assert task is not None
        authority.submit_signature(
            group_id, "owner-s", task.op_id, sign_b64(owner, base64.b64decode(task.message_b64))
        )
        assert authority.group_state_name(group_id) == "dissolved"
        clock.advance(24 * 3600 + 1)
        authority.sweep()
        assert authority.group_state_name(group_id) is None
        assert authority.inspect(group_id) is None
        with pytest.raises(GroupUnknownError):
            authority.roster_view(group_id, "owner-s")

    def test_disbind_session_keeps_membership(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        authority.disbind_session("owner-s")
        assert authority.session_fingerprint(group_id, "owner-s") is None
        snapshot = authority.inspect(group_id)
        assert snapshot is not None and len(snapshot.members) == 1  # membership survives


class TestObservability:
    def test_roster_view_requires_membership(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        with pytest.raises(GroupNotMemberError):
            authority.roster_view(group_id, "stranger")
        snapshot = authority.roster_view(group_id, "owner-s")
        assert snapshot.epoch == 1

    def test_event_tail_is_bounded(self, authority: GroupAuthority) -> None:
        owner = _owner()
        group_id = _create(authority, owner)
        for index in range(MAX_GROUP_MEMBERS - 1):
            _admit(
                authority,
                group_id,
                LocalIdentity.generate(),
                session=f"g{index}",
                owner=owner,
            )
        # Leave + restructure events beyond the tail bound.
        snapshot = authority.inspect(group_id)
        assert snapshot is not None
        assert len(snapshot.events) <= 16

    def test_pending_op_queue_is_bounded(self, authority: GroupAuthority) -> None:
        from ghostlink.constants.net import GROUP_MAX_PENDING_OPS

        owner = _owner()
        group_id = _create(authority, owner)
        guests = [LocalIdentity.generate() for _ in range(MAX_GROUP_MEMBERS - 1)]
        for index, guest in enumerate(guests):
            _admit(authority, group_id, guest, session=f"x{index}", owner=owner)
        # Queue one removal per member (none signed yet), then dissolve.
        for guest in guests[: GROUP_MAX_PENDING_OPS - 1]:
            authority.request_remove(group_id, "owner-s", fingerprint_of(guest))
        authority.request_dissolve(group_id, "owner-s")
        assert len(authority.pending_op_kinds(group_id)) == GROUP_MAX_PENDING_OPS
        # A full queue refuses new operations (leave conflicts on capacity).
        with pytest.raises(GroupConflictError):
            authority.request_leave(group_id, "x0")
