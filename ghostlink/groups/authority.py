"""The relay-side group authority (Phase 6B).

Single source of truth for group rosters and epochs, modeled on the
verified Phase 5 invite authority: one in-process registry; every
check-and-commit happens inside one synchronous call with **no ``await``
between check and write**, so concurrent operations from one event loop
serialize deterministically; verdicts fail closed; logs carry public
identifiers only.

Enforced, authoritatively:

* **creation** — owner proof-of-possession (Ed25519) over the session's
  attestation nonce; collision-free ``gl-group-…`` id minted relay-side;
* **authorization** — invites/removal/dissolution are owner-only (owner is
  bound to an attested session), leave is self-service; authorization keys
  are identity *fingerprints*, never display names;
* **epochs** — start at 1, increment by exactly one on each committed
  roster mutation, assigned only by this registry — clients cannot pick,
  skip, or roll back epochs;
* **signed events** — mutations commit only after the authorized signer's
  Ed25519 signature over the canonical event form verifies; one
  outstanding signature request per group keeps the signed epoch equal to
  the committed epoch;
* **capacity** — the roster plus pending joins never exceeds
  ``MAX_GROUP_MEMBERS``; checked atomically at redemption time.

Nothing here persists across relay restarts (matching the invite
authority); clients handle that as *defunct* groups (docs/GROUPS.md §28.4).
No plaintext, message keys, or invite tokens ever appear here.
"""

from __future__ import annotations

import base64
import binascii
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import Enum

from ghostlink.constants.net import (
    GROUP_EVENTS_KEPT,
    GROUP_JOIN_PENDING_SECONDS,
    GROUP_MAX_PENDING_OPS,
    GROUP_NAME_MAX_LEN,
    GROUP_PUBKEY_HEX_LENGTH,
    GROUP_RETENTION_HOURS,
    GROUP_SIGN_TIMEOUT_SECONDS,
    MAX_GROUP_MEMBERS,
)
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.groups import (
    GroupConflictError,
    GroupDissolvedError,
    GroupFullError,
    GroupNotMemberError,
    GroupPermissionError,
    GroupStateError,
    GroupUnknownError,
    GroupValidationError,
)
from ghostlink.groups.events import (
    GroupEvent,
    GroupEventKind,
    canonical_attest_form,
    canonical_create_form,
    fingerprint_for_key_hex,
    verify_signature,
)
from ghostlink.groups.ids import generate_group_id, is_valid_group_id
from ghostlink.groups.models import GroupRole, validate_group_name
from ghostlink.identity.fingerprint import identity_id_for, is_valid_identity_id

_logger = get_logger("groups.authority")


class MemberStatus(str, Enum):
    """Roster membership status inside the authority."""

    ACTIVE = "active"
    CANDIDATE = "candidate"  # redeemed, attested, awaiting owner countersign


class OpKind(str, Enum):
    """Pending signature-operation kinds (two-phase commits)."""

    JOIN = "join"
    LEFT = "left"
    REMOVED = "removed"
    DISSOLVED = "dissolved"

    def event_kind(self) -> GroupEventKind:
        return GroupEventKind(self.value)


@dataclass(frozen=True, slots=True)
class MemberView:
    """Safe roster projection shared with members (public identity data)."""

    fingerprint: str
    handle: str
    display_name: str
    public_key_hex: str
    role: str
    joined_epoch: int

    def to_payload(self) -> dict[str, object]:
        return {
            "fingerprint": self.fingerprint,
            "handle": self.handle,
            "display_name": self.display_name,
            "public_key_hex": self.public_key_hex,
            "role": self.role,
            "joined_epoch": self.joined_epoch,
        }


@dataclass(frozen=True, slots=True)
class GroupSnapshot:
    """Roster + epoch view for join payloads and re-sync answers."""

    group_id: str
    name: str
    state: str
    epoch: int
    owner_fingerprint: str
    owner_public_key_hex: str
    members: tuple[MemberView, ...]
    events: tuple[dict[str, object], ...]

    def members_payload(self) -> list[dict[str, object]]:
        return [member.to_payload() for member in self.members]


@dataclass(frozen=True, slots=True)
class AttestOutcome:
    """Result of a GROUP_ATTEST: role for the caller + roster snapshot."""

    group_id: str
    role: str  # "owner" | "member" | "candidate"
    epoch: int
    snapshot: GroupSnapshot | None
    admitted_event: GroupEvent | None = None  # self-admission replay (re-attest)


@dataclass(frozen=True, slots=True)
class SignTask:
    """A signature the authority requests from a bound signer session."""

    op_id: str
    group_id: str
    kind: str
    subject_fingerprint: str
    epoch: int
    signer_fingerprint: str
    message_b64: str
    signer_session: str | None


@dataclass(frozen=True, slots=True)
class CommittedOp:
    """A committed mutation: the event plus routing instructions."""

    event: GroupEvent
    join_member: MemberView | None  # populated for join events
    notify_sessions: tuple[str, ...]
    unbind_sessions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ExpiredNotice:
    """A swept timeout the server must surface to a client."""

    group_id: str
    code: str
    message: str
    session_id: str | None


@dataclass(slots=True)
class _Member:
    fingerprint: str
    handle: str
    display_name: str
    public_key_hex: str
    role: GroupRole
    joined_epoch: int
    status: MemberStatus
    session_id: str | None = None

    def view(self) -> MemberView:
        return MemberView(
            fingerprint=self.fingerprint,
            handle=self.handle,
            display_name=self.display_name,
            public_key_hex=self.public_key_hex,
            role=self.role.value,
            joined_epoch=self.joined_epoch,
        )


@dataclass(slots=True)
class _PendingJoin:
    redeem_session: str
    invite_id: str
    created_monotonic: float


@dataclass(slots=True)
class _Op:
    op_id: str
    kind: OpKind
    subject_fingerprint: str
    signer_fingerprint: str
    requested_monotonic: float
    epoch: int = 0
    canonical: bytes = b""
    wall_ts: datetime | None = None  # pinned at promotion; the committed event reuses it
    awaiting_since: float | None = None
    join_candidate: _Member | None = None


@dataclass(slots=True)
class _Group:
    group_id: str
    name: str
    state: str  # "active" | "dissolved"
    owner_fingerprint: str
    owner_public_key_hex: str
    epoch: int
    members: dict[str, _Member] = field(default_factory=dict)
    pending_joins: dict[str, _PendingJoin] = field(default_factory=dict)  # keyed by session
    ops: list[_Op] = field(default_factory=list)  # FIFO; only head may await signature
    events: list[GroupEvent] = field(default_factory=list)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    dissolved_at_mono: float | None = None


def _op_id() -> str:
    import secrets

    return secrets.token_hex(6)


class GroupAuthority:
    """Authoritative, race-free group roster registry for one relay process."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
        join_pending_seconds: float = GROUP_JOIN_PENDING_SECONDS,
        sign_timeout_seconds: float = GROUP_SIGN_TIMEOUT_SECONDS,
        retention_hours: float = GROUP_RETENTION_HOURS,
    ) -> None:
        self._monotonic = monotonic
        self._wall = wall_clock or (lambda: datetime.now(UTC))
        self._join_pending = join_pending_seconds
        self._sign_timeout = sign_timeout_seconds
        self._retention = timedelta(hours=retention_hours)
        self._groups: dict[str, _Group] = {}
        self._dissolved: dict[str, _Group] = {}

    # ------------------------------------------------------------- observability

    def group_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._groups))

    def inspect(self, group_id: str) -> GroupSnapshot | None:
        group = self._groups.get(group_id)
        if group is None:
            return None
        return self._snapshot(group)

    def pending_op_kinds(self, group_id: str) -> tuple[str, ...]:
        group = self._require(group_id)
        return tuple(op.kind.value for op in group.ops)

    def pending_join_sessions(self, group_id: str) -> tuple[str, ...]:
        group = self._require(group_id)
        return tuple(sorted(group.pending_joins))

    def sign_task_age(self, group_id: str) -> float | None:
        """Seconds the queue-head op has awaited a signature (None if none)."""

        group = self._groups.get(group_id)
        if group is None or not group.ops:
            return None
        head = group.ops[0]
        if head.awaiting_since is None:
            return 0.0
        return self._monotonic() - head.awaiting_since

    def session_fingerprint(self, group_id: str, session_id: str) -> str | None:
        group = self._groups.get(group_id)
        if group is None:
            return None
        for member in group.members.values():
            if member.session_id == session_id and member.status is MemberStatus.ACTIVE:
                return member.fingerprint
        return None

    def bound_sessions(self, group_id: str) -> tuple[str, ...]:
        group = self._groups.get(group_id)
        if group is None:
            return ()
        return tuple(
            member.session_id for member in group.members.values() if member.session_id is not None
        )

    def authorize_forward(
        self, group_id: str, from_session_id: str, to_fingerprint: str, epoch: int
    ) -> str | None:
        """ACL for GROUP_FORWARD (Phase 6C — docs/GROUPS.md §28.2/§28.3).

        Returns the recipient's bound session id, or ``None`` when the
        addressed member is offline (no relay-side queue — §29). Raises
        typed group errors for every refusal: unknown/dissolved group,
        sender not an attested ACTIVE member, recipient not on the roster,
        self-addressed envelopes, or an epoch outside the §16.5 window
        (current, or current-1 while the drain could still be open).

        The relay routes bytes it cannot read; this method checks only
        routing metadata — never message contents.
        """

        group = self._require(group_id)
        self._require_active_group(group)
        sender = self._require_member_session(group, from_session_id)
        to_fingerprint = to_fingerprint.strip().upper()
        if to_fingerprint == sender.fingerprint:
            raise GroupValidationError(
                "A group envelope may not be addressed to its own sender.",
                hint="Senders fan out to the *other* roster members only.",
            )
        if epoch < group.epoch - 1 or epoch > group.epoch:
            raise GroupStateError(
                f"Envelope epoch {epoch} is outside the acceptable window "
                f"({group.epoch - 1}..{group.epoch}).",
                hint="Only the current epoch and its 30 s drain predecessor live.",
            )
        member = group.members.get(to_fingerprint)
        if member is None or member.status is not MemberStatus.ACTIVE:
            raise GroupNotMemberError(
                "The addressed identity is not an ACTIVE member of this group.",
                hint="Roster membership is enforced at routing time.",
            )
        return member.session_id

    # ------------------------------------------------------------ group create

    def create_group(
        self,
        *,
        session_id: str,
        name: str,
        public_key_hex: str,
        pop_signature_b64: str,
        attest_nonce: str,
        handle: str,
        display_name: str,
    ) -> GroupSnapshot:
        """Create a group; the creator becomes owner at epoch 1 (atomic)."""

        clean_name = validate_group_name(name)
        fingerprint = self._verify_identity_material(public_key_hex, handle)
        pop = self._decode_signature(pop_signature_b64)
        message = canonical_create_form(clean_name, attest_nonce)
        try:
            public_key = bytes.fromhex(public_key_hex)
        except ValueError as exc:  # fingerprint check already guards; defensive
            raise GroupValidationError(
                "The owner public key is not valid hex.",
                hint="Group keys are 64 lowercase hex characters.",
            ) from exc
        if not verify_signature(public_key, pop, message):
            raise GroupPermissionError(
                "The proof-of-possession signature does not verify.",
                hint="Group creation requires signing with your identity key.",
            )
        group_id = self._mint_unique_id()
        owner = _Member(
            fingerprint=fingerprint,
            handle=handle,
            display_name=(display_name or handle)[:GROUP_NAME_MAX_LEN],
            public_key_hex=public_key_hex.lower(),
            role=GroupRole.OWNER,
            joined_epoch=1,
            status=MemberStatus.ACTIVE,
            session_id=session_id,
        )
        group = _Group(
            group_id=group_id,
            name=clean_name,
            state="active",
            owner_fingerprint=fingerprint,
            owner_public_key_hex=public_key_hex.lower(),
            epoch=1,
            members={fingerprint: owner},
            created_at=self._wall(),
        )
        self._groups[group_id] = group
        _logger.info("group created — %s owner=%s", group_id, fingerprint)
        return self._snapshot(group)

    def _mint_unique_id(self) -> str:
        for _ in range(64):
            candidate = generate_group_id()
            if candidate not in self._groups and candidate not in self._dissolved:
                return candidate
        raise GroupStateError(  # pragma: no cover - 60-bit space makes this unreachable
            "Could not mint a unique group id.",
            hint="Retry the operation.",
        )

    # ------------------------------------------------------ invite integration

    def allow_invite_mint(self, group_id: str, session_id: str, uses: int) -> None:
        """INVITE_CREATE guard: owner-bound session, headroom for ``uses``."""

        group = self._require(group_id)
        self._require_active_group(group)
        member = self._member_by_session(group, session_id)
        if member is None or member.fingerprint != group.owner_fingerprint:
            raise GroupPermissionError(
                "Only the owner may mint invites for this group.",
                hint="Attest as the group owner before creating invites.",
            )
        headroom = MAX_GROUP_MEMBERS - self._reserved_count(group)
        if uses > headroom:
            raise GroupFullError(
                f"This group can admit at most {max(0, headroom)} more member(s).",
                hint="The roster plus pending joins may never exceed 8 members.",
            )

    def group_state_name(self, group_id: str) -> str | None:
        if group_id in self._groups:
            return str(self._groups[group_id].state)
        if group_id in self._dissolved:
            return "dissolved"
        return None

    # ------------------------------------------------------ redemption (join)

    def begin_join_redemption(
        self, group_id: str, *, redeem_session: str, invite_id: str
    ) -> GroupSnapshot:
        """Reserve a pending-join slot (atomic with invite consumption).

        Called by the relay in the same await-free section that consumes
        the invite; :meth:`cancel_pending_join` rolls the slot back if the
        invite verdict is unfavorable, so a capacity-lost race never burns
        an invite.
        """

        group = self._require(group_id)
        self._require_active_group(group)
        if redeem_session in group.pending_joins:
            raise GroupConflictError(
                "This session already holds a pending join for the group.",
                hint="Finish (or let expire) the pending admission before retrying.",
            )
        if self._reserved_count(group) >= MAX_GROUP_MEMBERS:
            raise GroupFullError(
                "This group is full.",
                hint="Groups hold at most 8 members, including pending joins.",
            )
        group.pending_joins[redeem_session] = _PendingJoin(
            redeem_session=redeem_session,
            invite_id=invite_id,
            created_monotonic=self._monotonic(),
        )
        _logger.info("group %s — pending join reserved (%s)", group_id, invite_id)
        return self._snapshot(group)

    def cancel_pending_join(self, group_id: str, redeem_session: str) -> None:
        group = self._groups.get(group_id)
        if group is not None:
            group.pending_joins.pop(redeem_session, None)

    # -------------------------------------------------------------- attest

    def attest(
        self,
        *,
        session_id: str,
        group_id: str,
        public_key_hex: str,
        signature_b64: str,
        attest_nonce: str,
        handle: str,
        display_name: str,
    ) -> AttestOutcome:
        """Bind an identity to this session (owner/member rebind or candidate)."""

        group = self._require(group_id)
        fingerprint = self._verify_identity_material(public_key_hex, handle)
        signature = self._decode_signature(signature_b64)
        message = canonical_attest_form(group_id, fingerprint, attest_nonce)
        public_key = bytes.fromhex(public_key_hex)
        if not verify_signature(public_key, signature, message):
            raise GroupPermissionError(
                "The attestation signature does not verify.",
                hint="Attestation proves possession of your identity key.",
            )

        member = group.members.get(fingerprint)
        if member is not None and member.status is MemberStatus.ACTIVE:
            self._require_active_group(group)
            member.session_id = session_id
            member.handle = handle
            if display_name:
                member.display_name = display_name
            _logger.info("group %s — %s re-attested", group_id, fingerprint)
            self._refresh_sign_task(group)
            role = "owner" if fingerprint == group.owner_fingerprint else "member"
            return AttestOutcome(group.group_id, role, group.epoch, self._snapshot(group))

        if group_id in self._dissolved or group.state == "dissolved":
            raise GroupDissolvedError(
                "This group was dissolved.",
                hint="Dissolved groups are terminal; create a new group instead.",
            )
        self._require_active_group(group)

        pending = group.pending_joins.get(session_id)
        if pending is None:
            if self.session_was_removed(group, fingerprint):
                raise GroupNotMemberError(
                    "This identity was removed from the group.",
                    hint="Re-joining requires a fresh invite from the owner.",
                )
            raise GroupNotMemberError(
                "No pending admission or membership for this identity.",
                hint="Redeem a group invite first (ghostlink group join gl://join/…).",
            )
        del group.pending_joins[session_id]
        candidate = _Member(
            fingerprint=fingerprint,
            handle=handle,
            display_name=(display_name or handle)[:GROUP_NAME_MAX_LEN],
            public_key_hex=public_key_hex.lower(),
            role=GroupRole.MEMBER,
            joined_epoch=0,
            status=MemberStatus.CANDIDATE,
            session_id=session_id,
        )
        group.members[fingerprint] = candidate
        # Admission is countersigned by the OWNER (docs/GROUPS.md §13.2) —
        # a candidate can never sign their own way in.
        self._enqueue_op(
            group, OpKind.JOIN, fingerprint, group.owner_fingerprint, join_candidate=candidate
        )
        _logger.info("group %s — %s awaiting owner admission", group_id, fingerprint)
        return AttestOutcome(group.group_id, "candidate", group.epoch, None)

    def session_was_removed(self, group: _Group, fingerprint: str) -> bool:
        """True if a removed-event exists for ``fingerprint`` with no later join."""

        last_kind: GroupEventKind | None = None
        for event in group.events:
            if event.subject_fingerprint == fingerprint:
                last_kind = event.kind
        return last_kind is GroupEventKind.REMOVED

    # ------------------------------------------------------- leave/remove/etc.

    def request_leave(self, group_id: str, session_id: str) -> None:
        group = self._require(group_id)
        self._require_active_group(group)
        member = self._require_member_session(group, session_id)
        if member.fingerprint == group.owner_fingerprint:
            raise GroupPermissionError(
                "The owner cannot leave their own group.",
                hint="Dissolve the group instead (ghostlink group dissolve).",
            )
        self._enqueue_op(group, OpKind.LEFT, member.fingerprint, member.fingerprint)

    def request_remove(self, group_id: str, session_id: str, subject_fingerprint: str) -> None:
        group = self._require(group_id)
        self._require_active_group(group)
        subject_fingerprint = subject_fingerprint.strip().upper()
        member = self._require_member_session(group, session_id)
        if member.fingerprint != group.owner_fingerprint:
            raise GroupPermissionError(
                "Only the owner may remove members.",
                hint="Membership authorization is pinned to the owner's identity key.",
            )
        target = group.members.get(subject_fingerprint)
        if target is None or target.status is not MemberStatus.ACTIVE:
            raise GroupNotMemberError(
                f"{subject_fingerprint} is not an active member of this group.",
                hint="Only current members can be removed.",
            )
        if target.fingerprint == group.owner_fingerprint:
            raise GroupPermissionError(
                "The owner cannot remove themself.",
                hint="Owner departure dissolves the group instead.",
            )
        self._enqueue_op(group, OpKind.REMOVED, target.fingerprint, group.owner_fingerprint)

    def request_dissolve(self, group_id: str, session_id: str) -> None:
        group = self._require(group_id)
        self._require_active_group(group)
        member = self._require_member_session(group, session_id)
        if member.fingerprint != group.owner_fingerprint:
            raise GroupPermissionError(
                "Only the owner may dissolve the group.",
                hint="Membership authorization is pinned to the owner's identity key.",
            )
        self._enqueue_op(group, OpKind.DISSOLVED, group.owner_fingerprint, group.owner_fingerprint)

    # ------------------------------------------------------------- sign/commit

    def next_sign_task(self, group_id: str) -> SignTask | None:
        """The signature the authority is waiting for (promoting the queue head)."""

        group = self._groups.get(group_id)
        if group is None or not group.ops:
            return None
        head = group.ops[0]
        if head.awaiting_since is None:
            head.epoch = group.epoch + 1
            head.wall_ts = self._wall()
            head.canonical = GroupEvent(
                group_id=group.group_id,
                epoch=head.epoch,
                kind=head.kind.event_kind(),
                subject_fingerprint=head.subject_fingerprint,
                wall_ts=head.wall_ts,
                signer_fingerprint=head.signer_fingerprint,
                signature=b"\x00" * 64,
            ).canonical()
            head.awaiting_since = self._monotonic()
        signer_session = self._session_for_fingerprint(group, head.signer_fingerprint)
        return SignTask(
            op_id=head.op_id,
            group_id=group.group_id,
            kind=head.kind.value,
            subject_fingerprint=head.subject_fingerprint,
            epoch=head.epoch,
            signer_fingerprint=head.signer_fingerprint,
            message_b64=base64.b64encode(head.canonical).decode("ascii"),
            signer_session=signer_session,
        )

    def submit_signature(
        self, group_id: str, session_id: str, op_id: str, signature_b64: str
    ) -> CommittedOp:
        """Verify + commit the pending op (atomic); returns the routed event."""

        group = self._require(group_id)
        if not group.ops or group.ops[0].op_id != op_id:
            raise GroupConflictError(
                "No such pending operation (stale or already settled).",
                hint="Operations settle in order; the client may be out of sync.",
            )
        op = group.ops[0]
        if op.awaiting_since is None:
            raise GroupConflictError(
                "This operation is not awaiting a signature yet.",
                hint="Wait for the signature request before responding.",
            )
        signer_session = self._session_for_fingerprint(group, op.signer_fingerprint)
        if signer_session != session_id:
            raise GroupPermissionError(
                "Only the authorized signer's session may settle this operation.",
                hint="Membership operations are signed by their authorized identity.",
            )
        signer = group.members.get(op.signer_fingerprint)
        assert signer is not None  # signers are roster members by construction
        signature = self._decode_signature(signature_b64)
        if not verify_signature(bytes.fromhex(signer.public_key_hex), signature, op.canonical):
            self._abort_head_op(group, "invalid signature")
            raise GroupPermissionError(
                "The submitted signature does not verify; the operation was aborted.",
                hint="Sign exactly the canonical form the relay presented.",
            )
        group.ops.pop(0)
        # Capture the subject's session BEFORE the commit drops the member —
        # the departed member must still receive (and be unbound from) the
        # final event. Join/dissolve routing works post-commit by design.
        subject_session = self._session_for_fingerprint(group, op.subject_fingerprint)
        event = self._commit(group, op, signature)
        notify, unbind, join_member = self._routing_for(group, op, subject_session)
        self._refresh_sign_task(group)
        return CommittedOp(
            event=event, join_member=join_member, notify_sessions=notify, unbind_sessions=unbind
        )

    def _commit(self, group: _Group, op: _Op, signature: bytes) -> GroupEvent:
        group.epoch += 1
        # The committed event must be byte-identical to what the signer
        # signed — reuse the wall_ts pinned when the op was promoted.
        wall_ts = op.wall_ts if op.wall_ts is not None else self._wall()
        event = GroupEvent(
            group_id=group.group_id,
            epoch=group.epoch,
            kind=op.kind.event_kind(),
            subject_fingerprint=op.subject_fingerprint,
            wall_ts=wall_ts,
            signer_fingerprint=op.signer_fingerprint,
            signature=signature,
        )
        if op.kind is OpKind.JOIN:
            assert op.join_candidate is not None
            op.join_candidate.status = MemberStatus.ACTIVE
            op.join_candidate.joined_epoch = event.epoch
        elif op.kind in (OpKind.LEFT, OpKind.REMOVED):
            group.members.pop(op.subject_fingerprint, None)
        elif op.kind is OpKind.DISSOLVED:
            group.state = "dissolved"
            group.dissolved_at_mono = self._monotonic()
        group.events.append(event)
        del group.events[:-GROUP_EVENTS_KEPT]
        _logger.info(
            "group %s — %s %s at epoch %d",
            group.group_id,
            op.kind.value,
            op.subject_fingerprint,
            event.epoch,
        )
        if op.kind is OpKind.DISSOLVED:
            self._dissolved[group.group_id] = self._groups.pop(group.group_id)
        return event

    def _routing_for(
        self, group: _Group, op: _Op, subject_session: str | None
    ) -> tuple[tuple[str, ...], tuple[str, ...], MemberView | None]:
        notify: list[str] = []
        unbind: list[str] = []
        join_member: MemberView | None = None
        if op.kind is OpKind.JOIN:
            assert op.join_candidate is not None
            join_member = op.join_candidate.view()
        if op.kind is OpKind.DISSOLVED:
            unbind = [member.session_id for member in group.members.values() if member.session_id]
        elif op.kind in (OpKind.LEFT, OpKind.REMOVED) and subject_session is not None:
            unbind.append(subject_session)
        for member in group.members.values():
            if member.session_id and member.session_id not in unbind:
                notify.append(member.session_id)
        notify.extend(unbind)  # the departed also receives the final event
        return tuple(dict.fromkeys(notify)), tuple(unbind), join_member

    def _abort_head_op(self, group: _Group, reason: str) -> None:
        op = group.ops.pop(0)
        if op.kind is OpKind.JOIN and op.join_candidate is not None:
            group.members.pop(op.join_candidate.fingerprint, None)
        _logger.info("group %s — op %s aborted (%s)", group.group_id, op.op_id, reason)
        self._refresh_sign_task(group)

    def _refresh_sign_task(self, group: _Group) -> None:
        """Reset promotion state so the next head is offered with a fresh epoch."""

        for op in group.ops:
            if op.awaiting_since is not None:
                return  # the queue is already mid-signature; leave it alone
        return

    # ------------------------------------------------------------------ sweep

    def sweep(self) -> list[ExpiredNotice]:
        """Expire pending joins, stale signature waits, and old tombstones."""

        notices: list[ExpiredNotice] = []
        now = self._monotonic()
        for group in list(self._groups.values()):
            for session, pending in list(group.pending_joins.items()):
                if now - pending.created_monotonic > self._join_pending:
                    del group.pending_joins[session]
                    notices.append(
                        ExpiredNotice(
                            group.group_id,
                            "group/join-timeout",
                            "The pending admission expired before attestation.",
                            session,
                        )
                    )
            if group.ops:
                head = group.ops[0]
                if (
                    head.awaiting_since is not None
                    and now - head.awaiting_since > self._sign_timeout
                ):
                    candidate_session = (
                        head.join_candidate.session_id if head.join_candidate else None
                    )
                    kind = head.kind
                    self._abort_head_op(group, "signature timeout")
                    if kind is OpKind.JOIN:
                        notices.append(
                            ExpiredNotice(
                                group.group_id,
                                "group/join-timeout",
                                "The owner did not countersign the admission in time.",
                                candidate_session,
                            )
                        )
        for group_id, group in list(self._dissolved.items()):
            if (
                group.dissolved_at_mono is not None
                and now - group.dissolved_at_mono > self._retention.total_seconds()
            ):
                del self._dissolved[group_id]
                _logger.info("group %s tombstone purged", group_id)
        return notices

    def disbind_session(self, session_id: str) -> None:
        """Drop session bindings when a client disconnects (membership stays)."""

        for group in self._groups.values():
            for member in group.members.values():
                if member.session_id == session_id:
                    member.session_id = None
            for pending in list(group.pending_joins.values()):
                if pending.redeem_session == session_id:
                    del group.pending_joins[pending.redeem_session]

    # -------------------------------------------------------------- view/sync

    def roster_view(self, group_id: str, session_id: str) -> GroupSnapshot:
        group = self._require(group_id)
        if group.state == "dissolved":
            raise GroupDissolvedError(
                "This group was dissolved.",
                hint="Dissolved groups are terminal; create a new group instead.",
            )
        self._require_member_session(group, session_id)
        return self._snapshot(group)

    def _snapshot(self, group: _Group) -> GroupSnapshot:
        members = tuple(
            member.view()
            for member in sorted(group.members.values(), key=lambda m: m.joined_epoch)
            if member.status is MemberStatus.ACTIVE
        )
        events = tuple(event.to_dict() for event in group.events)
        return GroupSnapshot(
            group_id=group.group_id,
            name=group.name,
            state=str(group.state),
            epoch=group.epoch,
            owner_fingerprint=group.owner_fingerprint,
            owner_public_key_hex=group.owner_public_key_hex,
            members=members,
            events=events,
        )

    # ------------------------------------------------------------- internals

    def _enqueue_op(
        self,
        group: _Group,
        kind: OpKind,
        subject_fingerprint: str,
        signer_fingerprint: str,
        *,
        join_candidate: _Member | None = None,
    ) -> _Op:
        if len(group.ops) >= GROUP_MAX_PENDING_OPS:
            raise GroupConflictError(
                "Too many pending membership operations for this group.",
                hint="Wait for the pending operations to settle, then retry.",
            )
        for existing in group.ops:
            if existing.kind is kind and existing.subject_fingerprint == subject_fingerprint:
                return existing  # idempotent re-request of an already-pending op
        op = _Op(
            op_id=_op_id(),
            kind=kind,
            subject_fingerprint=subject_fingerprint,
            signer_fingerprint=signer_fingerprint,
            requested_monotonic=self._monotonic(),
            join_candidate=join_candidate,
        )
        group.ops.append(op)
        return op

    def _verify_identity_material(self, public_key_hex: str, handle: str) -> str:
        if len(public_key_hex) != GROUP_PUBKEY_HEX_LENGTH:
            raise GroupValidationError(
                "A member public key must be 64 hex characters.",
                hint="Group member keys are Ed25519 public keys.",
            )
        fingerprint = fingerprint_for_key_hex(public_key_hex)
        if not is_valid_identity_id(handle) or handle != identity_id_for(
            bytes.fromhex(public_key_hex)
        ):
            raise GroupValidationError(
                "The member handle does not match the public key.",
                hint="Handles are derived from the identity key (GL-XXXX).",
            )
        return fingerprint

    def _decode_signature(self, signature_b64: str) -> bytes:
        try:
            signature = base64.b64decode(signature_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise GroupValidationError(
                "The signature is not valid base64.",
                hint="Signatures are Ed25519 (64 bytes, base64 encoded).",
            ) from exc
        if len(signature) != 64:
            raise GroupValidationError(
                "The signature must decode to exactly 64 bytes.",
                hint="Signatures are Ed25519 (64 bytes, base64 encoded).",
            )
        return signature

    def _require(self, group_id: str) -> _Group:
        if not is_valid_group_id(group_id):
            raise GroupValidationError(
                f"Group id '{group_id}' is malformed.",
                hint="Group ids look like gl-group-XXXX-XXXX-XXXX.",
            )
        group = self._groups.get(group_id)
        if group is None:
            if group_id in self._dissolved:
                raise GroupDissolvedError(
                    "This group was dissolved.",
                    hint="Dissolved groups are terminal; create a new group instead.",
                )
            raise GroupUnknownError(
                f"The relay has no record of group {group_id}.",
                hint="Groups are hosted per relay; after a relay restart they are gone.",
            )
        return group

    def _require_active_group(self, group: _Group) -> None:
        if group.state != "active":
            raise GroupDissolvedError(
                "This group was dissolved.",
                hint="Dissolved groups are terminal; create a new group instead.",
            )

    def _member_by_session(self, group: _Group, session_id: str) -> _Member | None:
        for member in group.members.values():
            if member.session_id == session_id and member.status is MemberStatus.ACTIVE:
                return member
        return None

    def _require_member_session(self, group: _Group, session_id: str) -> _Member:
        member = self._member_by_session(group, session_id)
        if member is None:
            raise GroupNotMemberError(
                "You are not an attested member of this group.",
                hint="Attest your identity to the relay first (re-join or re-sync).",
            )
        return member

    def _session_for_fingerprint(self, group: _Group, fingerprint: str) -> str | None:
        member = group.members.get(fingerprint)
        if member is not None and member.status is MemberStatus.ACTIVE:
            return member.session_id
        return None

    def _reserved_count(self, group: _Group) -> int:
        active = sum(1 for m in group.members.values() if m.status is MemberStatus.ACTIVE)
        candidates = sum(1 for m in group.members.values() if m.status is MemberStatus.CANDIDATE)
        return active + candidates + len(group.pending_joins)


__all__ = [
    "AttestOutcome",
    "CommittedOp",
    "ExpiredNotice",
    "GroupAuthority",
    "GroupSnapshot",
    "MemberStatus",
    "MemberView",
    "SignTask",
]
