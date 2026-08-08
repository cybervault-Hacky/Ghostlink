"""Local group membership models (Phase 6B): epochs, transitions, roster."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from ghostlink.exceptions.config import ConfigValidationError
from ghostlink.exceptions.groups import (
    GroupEpochError,
    GroupStateError,
    GroupValidationError,
)
from ghostlink.groups.events import GroupEvent, GroupEventKind, fingerprint_for_key_hex
from ghostlink.groups.models import (
    GroupMember,
    GroupRole,
    LocalGroupRecord,
    LocalGroupState,
    validate_group_display_name,
    validate_group_name,
)
from ghostlink.identity.identity import LocalIdentity
from tests.group_helpers import fingerprint_of

GROUP = "gl-group-TEST-AAAA-BBBB".replace("TEST", "W7PP")  # gl-group-W7PP-AAAA-BBBB
T0 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)


def _member(identity: LocalIdentity, *, role: GroupRole, epoch: int) -> GroupMember:
    return GroupMember(
        fingerprint=fingerprint_of(identity),
        handle=identity.identity_id,
        display_name=identity.identity_id,
        public_key_hex=identity.public_key_hex,
        role=role,
        joined_epoch=epoch,
    )


def _record(
    owner: LocalIdentity,
    *,
    members: dict[str, GroupMember] | None = None,
    epoch: int = 1,
    state: LocalGroupState = LocalGroupState.ACTIVE,
) -> LocalGroupRecord:
    owner_member = _member(owner, role=GroupRole.OWNER, epoch=1)
    roster = {owner_member.fingerprint: owner_member}
    if members:
        roster.update(members)
    return LocalGroupRecord(
        group_id=GROUP,
        name="Night Watch",
        owner_fingerprint=owner_member.fingerprint,
        owner_public_key_hex=owner.public_key_hex,
        my_fingerprint=owner_member.fingerprint,
        epoch=epoch,
        members=roster,
        state=state,
        relay_url="ws://127.0.0.1:8787/relay",
        created_at=T0,
        updated_at=T0,
    )


def _event(
    owner: LocalIdentity,
    *,
    epoch: int,
    kind: GroupEventKind,
    subject: str,
    signer: str | None = None,
) -> GroupEvent:
    return GroupEvent(
        group_id=GROUP,
        epoch=epoch,
        kind=kind,
        subject_fingerprint=subject,
        wall_ts=T0 + timedelta(seconds=epoch),
        signer_fingerprint=signer if signer is not None else fingerprint_of(owner),
        signature=b"\x42" * 64,
    )


class TestNameValidation:
    def test_name_trims_and_limits(self) -> None:
        assert validate_group_name("  Ops  ") == "Ops"
        with pytest.raises(GroupValidationError):
            validate_group_name("")
        with pytest.raises(GroupValidationError):
            validate_group_name("x" * 49)

    def test_name_rejects_control_characters(self) -> None:
        with pytest.raises(GroupValidationError):
            validate_group_name("bad\nname")
        with pytest.raises(GroupValidationError):
            validate_group_name("bad\x7fname")

    def test_display_name_validation(self) -> None:
        assert validate_group_display_name(" Ghost ") == "Ghost"
        with pytest.raises(GroupValidationError):
            validate_group_display_name("x" * 25)
        with pytest.raises(GroupValidationError):
            validate_group_display_name("")


class TestGroupMember:
    def test_owner_must_join_at_epoch_one(self) -> None:
        identity = LocalIdentity.generate()
        with pytest.raises(GroupValidationError):
            _member(identity, role=GroupRole.OWNER, epoch=3)

    def test_joined_epoch_floor(self) -> None:
        identity = LocalIdentity.generate()
        with pytest.raises(GroupValidationError):
            _member(identity, role=GroupRole.MEMBER, epoch=0)

    def test_fingerprint_must_match_format(self) -> None:
        identity = LocalIdentity.generate()
        with pytest.raises(GroupValidationError):
            GroupMember(
                fingerprint="not-a-fingerprint",
                handle=identity.identity_id,
                display_name="x",
                public_key_hex=identity.public_key_hex,
                role=GroupRole.MEMBER,
                joined_epoch=2,
            )

    def test_serialization_roundtrip(self) -> None:
        identity = LocalIdentity.generate()
        member = _member(identity, role=GroupRole.MEMBER, epoch=4)
        restored = GroupMember.from_dict(member.to_dict())
        assert restored.fingerprint == member.fingerprint
        assert restored.role is GroupRole.MEMBER
        assert restored.joined_epoch == 4

    def test_from_dict_rejects_malformed(self) -> None:
        with pytest.raises(ConfigValidationError):
            GroupMember.from_dict({"fingerprint": "x"})


class TestRecordConstruction:
    def test_rejects_bad_group_id(self) -> None:
        owner = LocalIdentity.generate()
        with pytest.raises(GroupValidationError):
            LocalGroupRecord(
                group_id="gl-room-AAAA-BBBB-CCCC",
                name="x",
                owner_fingerprint=fingerprint_of(owner),
                owner_public_key_hex=owner.public_key_hex,
                my_fingerprint=fingerprint_of(owner),
                epoch=1,
                members={},
                state=LocalGroupState.ACTIVE,
                relay_url="",
                created_at=T0,
                updated_at=T0,
            )

    def test_rejects_naive_timestamps(self) -> None:
        owner = LocalIdentity.generate()
        with pytest.raises(ConfigValidationError):
            LocalGroupRecord(
                group_id=GROUP,
                name="x",
                owner_fingerprint=fingerprint_of(owner),
                owner_public_key_hex=owner.public_key_hex,
                my_fingerprint=fingerprint_of(owner),
                epoch=1,
                members={},
                state=LocalGroupState.ACTIVE,
                relay_url="",
                created_at=datetime(2026, 1, 1),  # naive
                updated_at=T0,
            )

    def test_rejects_epoch_zero(self) -> None:
        owner = LocalIdentity.generate()
        with pytest.raises(GroupValidationError):
            _record(owner, epoch=0)


class TestEventApplication:
    def test_join_adds_member_at_next_epoch(self) -> None:
        owner, guest = LocalIdentity.generate(), LocalIdentity.generate()
        record = _record(owner)
        guest_member = _member(guest, role=GroupRole.MEMBER, epoch=2)
        event = _event(owner, epoch=2, kind=GroupEventKind.JOIN, subject=guest_member.fingerprint)
        notice = record.apply_verified_event(event, join_member=guest_member)
        assert record.epoch == 2
        assert record.member_count() == 2
        assert "joined" in notice
        assert record.epoch_leap_at is not None

    def test_stale_and_duplicate_epochs_are_rejected(self) -> None:
        owner, guest = LocalIdentity.generate(), LocalIdentity.generate()
        record = _record(owner)
        guest_member = _member(guest, role=GroupRole.MEMBER, epoch=2)
        record.apply_verified_event(
            _event(owner, epoch=2, kind=GroupEventKind.JOIN, subject=guest_member.fingerprint),
            join_member=guest_member,
        )
        # Same event again — never re-applied.
        with pytest.raises(GroupEpochError):
            record.apply_verified_event(
                _event(owner, epoch=2, kind=GroupEventKind.JOIN, subject=guest_member.fingerprint),
                join_member=guest_member,
            )
        # An older epoch (rollback attempt) is refused too.
        with pytest.raises(GroupEpochError):
            record.apply_verified_event(
                _event(owner, epoch=1, kind=GroupEventKind.LEFT, subject=guest_member.fingerprint)
            )
        assert record.epoch == 2
        assert not record.suspect

    def test_epoch_gap_marks_suspect_and_refuses(self) -> None:
        owner, guest = LocalIdentity.generate(), LocalIdentity.generate()
        record = _record(owner)
        guest_member = _member(guest, role=GroupRole.MEMBER, epoch=9)
        with pytest.raises(GroupEpochError):
            record.apply_verified_event(
                _event(owner, epoch=9, kind=GroupEventKind.JOIN, subject=guest_member.fingerprint),
                join_member=guest_member,
            )
        assert record.suspect
        assert record.epoch == 1  # unchanged
        with pytest.raises(GroupStateError):
            record.require_unsuspecting()

    def test_join_requires_owner_signer(self) -> None:
        owner, guest = LocalIdentity.generate(), LocalIdentity.generate()
        record = _record(owner)
        guest_fp = fingerprint_of(guest)
        with pytest.raises(GroupStateError):
            record.apply_verified_event(
                _event(
                    owner,
                    epoch=2,
                    kind=GroupEventKind.JOIN,
                    subject=guest_fp,
                    signer=guest_fp,
                ),
                join_member=_member(guest, role=GroupRole.MEMBER, epoch=2),
            )

    def test_duplicate_join_subject_is_rejected(self) -> None:
        owner, guest = LocalIdentity.generate(), LocalIdentity.generate()
        guest_member = _member(guest, role=GroupRole.MEMBER, epoch=2)
        record = _record(owner, members={guest_member.fingerprint: guest_member}, epoch=2)
        with pytest.raises(GroupStateError):
            record.apply_verified_event(
                _event(owner, epoch=3, kind=GroupEventKind.JOIN, subject=guest_member.fingerprint),
                join_member=guest_member,
            )

    def test_join_requires_member_descriptor(self) -> None:
        owner, guest = LocalIdentity.generate(), LocalIdentity.generate()
        record = _record(owner)
        with pytest.raises(GroupStateError):
            record.apply_verified_event(
                _event(owner, epoch=2, kind=GroupEventKind.JOIN, subject=fingerprint_of(guest)),
                join_member=None,
            )

    def test_left_requires_self_signature_and_removes(self) -> None:
        owner, guest = LocalIdentity.generate(), LocalIdentity.generate()
        guest_member = _member(guest, role=GroupRole.MEMBER, epoch=2)
        record = _record(owner, members={guest_member.fingerprint: guest_member}, epoch=2)
        # Signed by the owner instead of the leaver — refused.
        with pytest.raises(GroupStateError):
            record.apply_verified_event(
                _event(owner, epoch=3, kind=GroupEventKind.LEFT, subject=guest_member.fingerprint)
            )
        notice = record.apply_verified_event(
            _event(
                owner,
                epoch=3,
                kind=GroupEventKind.LEFT,
                subject=guest_member.fingerprint,
                signer=guest_member.fingerprint,
            )
        )
        assert "left" in notice
        assert record.member_count() == 1
        assert record.state is LocalGroupState.ACTIVE  # observer view

    def test_own_leave_archives_the_record(self) -> None:
        owner, me = LocalIdentity.generate(), LocalIdentity.generate()
        me_member = _member(me, role=GroupRole.MEMBER, epoch=2)
        record = _record(owner, members={me_member.fingerprint: me_member}, epoch=2)
        record.my_fingerprint = me_member.fingerprint
        record.apply_verified_event(
            _event(
                owner,
                epoch=3,
                kind=GroupEventKind.LEFT,
                subject=me_member.fingerprint,
                signer=me_member.fingerprint,
            )
        )
        assert record.state is LocalGroupState.LEFT

    def test_removal_rules(self) -> None:
        owner, guest = LocalIdentity.generate(), LocalIdentity.generate()
        guest_member = _member(guest, role=GroupRole.MEMBER, epoch=2)
        record = _record(owner, members={guest_member.fingerprint: guest_member}, epoch=2)
        # Members cannot author removals.
        with pytest.raises(GroupStateError):
            record.apply_verified_event(
                _event(
                    owner,
                    epoch=3,
                    kind=GroupEventKind.REMOVED,
                    subject=guest_member.fingerprint,
                    signer=guest_member.fingerprint,
                )
            )
        # Unknown member — nothing to remove.
        with pytest.raises(GroupStateError):
            record.apply_verified_event(
                _event(
                    owner,
                    epoch=3,
                    kind=GroupEventKind.REMOVED,
                    subject=fingerprint_for_key_hex("ab" * 32),
                )
            )
        # The owner can never be removed.
        with pytest.raises(GroupStateError):
            record.apply_verified_event(
                _event(
                    owner, epoch=3, kind=GroupEventKind.REMOVED, subject=record.owner_fingerprint
                )
            )
        notice = record.apply_verified_event(
            _event(owner, epoch=3, kind=GroupEventKind.REMOVED, subject=guest_member.fingerprint)
        )
        assert "removed" in notice
        assert guest_member.fingerprint not in record.members

    def test_my_removal_archives_as_removed(self) -> None:
        owner, me = LocalIdentity.generate(), LocalIdentity.generate()
        me_member = _member(me, role=GroupRole.MEMBER, epoch=2)
        record = _record(owner, members={me_member.fingerprint: me_member}, epoch=2)
        record.my_fingerprint = me_member.fingerprint
        record.apply_verified_event(
            _event(owner, epoch=3, kind=GroupEventKind.REMOVED, subject=me_member.fingerprint)
        )
        assert record.state is LocalGroupState.REMOVED

    def test_dissolve_is_terminal(self) -> None:
        owner = LocalIdentity.generate()
        record = _record(owner)
        notice = record.apply_verified_event(
            _event(
                owner,
                epoch=2,
                kind=GroupEventKind.DISSOLVED,
                subject=record.owner_fingerprint,
            )
        )
        assert "dissolved" in notice
        assert record.state is LocalGroupState.DISSOLVED
        # Terminal records refuse further application.
        with pytest.raises(GroupStateError):
            record.apply_verified_event(
                _event(
                    owner,
                    epoch=3,
                    kind=GroupEventKind.LEFT,
                    subject=record.owner_fingerprint,
                )
            )

    def test_event_for_other_group_is_refused(self) -> None:
        owner = LocalIdentity.generate()
        record = _record(owner)
        foreign = GroupEvent(
            group_id="gl-group-XXXX-YYYY-ZZZZ",
            epoch=2,
            kind=GroupEventKind.DISSOLVED,
            subject_fingerprint=record.owner_fingerprint,
            wall_ts=T0,
            signer_fingerprint=record.owner_fingerprint,
            signature=b"\x42" * 64,
        )
        with pytest.raises(GroupStateError):
            record.apply_verified_event(foreign)


class TestRecordLifecycleGuards:
    def test_require_active_on_terminal(self) -> None:
        owner = LocalIdentity.generate()
        record = _record(owner, state=LocalGroupState.LEFT)
        with pytest.raises(GroupStateError):
            record.require_active()

    def test_require_terminal_for_archive(self) -> None:
        owner = LocalIdentity.generate()
        active = _record(owner)
        with pytest.raises(GroupStateError):
            active.require_terminal_for_archive()
        done = _record(owner, state=LocalGroupState.DISSOLVED)
        done.require_terminal_for_archive()  # no raise

    def test_mark_defunct_from_active_only(self) -> None:
        owner = LocalIdentity.generate()
        record = _record(owner)
        record.mark_defunct()
        assert record.state is LocalGroupState.DEFUNCT
        with pytest.raises(GroupStateError):
            record.mark_defunct()
        with pytest.raises(GroupStateError):
            record.require_active()

    def test_event_history_is_bounded(self) -> None:
        owner = LocalIdentity.generate()
        record = _record(owner)
        for epoch in range(2, 40):
            guest = LocalIdentity.generate()
            member = GroupMember(
                fingerprint=fingerprint_of(guest),
                handle=guest.identity_id,
                display_name=guest.identity_id,
                public_key_hex=guest.public_key_hex,
                role=GroupRole.MEMBER,
                joined_epoch=epoch,
            )
            record.apply_verified_event(
                _event(owner, epoch=epoch, kind=GroupEventKind.JOIN, subject=member.fingerprint),
                join_member=member,
            )
            record.members.pop(member.fingerprint)  # keep roster small; history grows
            record.epoch = epoch  # history trimming is what we exercise here
        assert len(record.events) <= 16


class TestRecordSerialization:
    def test_roundtrip_preserves_everything(self) -> None:
        owner, guest = LocalIdentity.generate(), LocalIdentity.generate()
        record = _record(owner)
        member = _member(guest, role=GroupRole.MEMBER, epoch=2)
        record.apply_verified_event(
            _event(owner, epoch=2, kind=GroupEventKind.JOIN, subject=member.fingerprint),
            join_member=member,
            now=T0 + timedelta(minutes=1),
        )
        restored = LocalGroupRecord.from_dict(record.to_dict())
        assert restored.group_id == record.group_id
        assert restored.epoch == record.epoch
        assert restored.state is record.state
        assert restored.member_count() == 2
        assert len(restored.events) == 1
        assert restored.epoch_leap_at == record.epoch_leap_at
        assert restored.members[member.fingerprint].public_key_hex == guest.public_key_hex

    def test_from_dict_rejects_missing_fields(self) -> None:
        owner = LocalIdentity.generate()
        document = _record(owner).to_dict()
        del document["epoch"]
        with pytest.raises(ConfigValidationError):
            LocalGroupRecord.from_dict(document)

    def test_from_dict_rejects_unknown_state(self) -> None:
        owner = LocalIdentity.generate()
        document = _record(owner).to_dict()
        document["state"] = "zombie"
        with pytest.raises(ConfigValidationError):
            LocalGroupRecord.from_dict(document)

    def test_from_dict_rejects_broken_roster(self) -> None:
        owner = LocalIdentity.generate()
        document = _record(owner).to_dict()
        document["members"] = "nope"
        with pytest.raises(ConfigValidationError):
            LocalGroupRecord.from_dict(document)
