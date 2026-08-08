"""Secure groups (Phase 6B) — membership lifecycle foundation.

Implements the Phase 6A design (docs/GROUPS.md): group identity, the
relay-authoritative roster and epoch model, owner-signed membership
events, Phase 5 invite integration, local metadata persistence, and the
client-side lifecycle manager. Group *messaging/encryption* is the next
stage and is deliberately absent here.
"""

from ghostlink.groups.authority import (
    AttestOutcome,
    CommittedOp,
    ExpiredNotice,
    GroupAuthority,
    GroupSnapshot,
    MemberStatus,
    MemberView,
    SignTask,
)
from ghostlink.groups.events import (
    GroupEvent,
    GroupEventKind,
    canonical_attest_form,
    canonical_create_form,
    canonical_event_form,
    fingerprint_for_key_hex,
    verify_signature,
)
from ghostlink.groups.ids import generate_group_id, is_valid_group_id, normalize_group_id
from ghostlink.groups.lifecycle import LocalGroupManager
from ghostlink.groups.models import (
    GroupMember,
    GroupRole,
    LocalGroupRecord,
    LocalGroupState,
    validate_group_display_name,
    validate_group_name,
)
from ghostlink.groups.registry import LocalGroupRegistry

__all__ = [
    "AttestOutcome",
    "CommittedOp",
    "ExpiredNotice",
    "GroupAuthority",
    "GroupEvent",
    "GroupEventKind",
    "GroupMember",
    "GroupRole",
    "GroupSnapshot",
    "LocalGroupManager",
    "LocalGroupRecord",
    "LocalGroupRegistry",
    "LocalGroupState",
    "MemberStatus",
    "MemberView",
    "SignTask",
    "canonical_attest_form",
    "canonical_create_form",
    "canonical_event_form",
    "fingerprint_for_key_hex",
    "generate_group_id",
    "is_valid_group_id",
    "normalize_group_id",
    "validate_group_display_name",
    "validate_group_name",
    "verify_signature",
]
