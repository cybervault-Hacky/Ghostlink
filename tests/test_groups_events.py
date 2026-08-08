"""Group events: canonical forms and Ed25519 signatures (Phase 6B).

No new cryptography — only the Phase 5 identity primitive (Ed25519) is
reused, and every test constructs/verifies real signatures.
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest

from ghostlink.exceptions.groups import GroupValidationError
from ghostlink.groups.events import (
    GroupEvent,
    GroupEventKind,
    canonical_attest_form,
    canonical_create_form,
    canonical_event_form,
    canonical_event_string,
    fingerprint_for_key_hex,
    require_valid_fingerprint,
    require_valid_group_id,
    verify_signature,
)
from ghostlink.identity.identity import LocalIdentity

STAMP = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
GROUP = "gl-group-AAAA-BBBB-CCCC"
FP = "GLFP-0123-4567-89AB"
FP2 = "GLFP-FEDC-BA98-7654"


class TestCanonicalForms:
    def test_create_form_shape(self) -> None:
        assert canonical_create_form("Night Watch", "nonce123") == (
            b"ghostlink/group-create/v1|Night Watch|nonce123"
        )

    def test_attest_form_shape(self) -> None:
        assert canonical_attest_form(GROUP, FP, "n0") == (
            f"ghostlink/group-attest/v1|{GROUP}|{FP}|n0".encode()
        )

    def test_event_form_shape(self) -> None:
        form = canonical_event_form(GROUP, 7, "join", FP, STAMP)
        assert form == (
            f"ghostlink/group-event/v1|{GROUP}|7|join|{FP}|{STAMP.isoformat()}".encode()
        )

    def test_event_form_normalizes_naive_datetime_to_utc(self) -> None:
        naive = datetime(2026, 1, 2, 3, 4, 5)
        assert canonical_event_form(GROUP, 1, "left", FP, naive) == canonical_event_form(
            GROUP, 1, "left", FP, STAMP
        )

    def test_event_string_is_verbatim_wire_form(self) -> None:
        stamp = "2026-01-02T03:04:05+00:00"
        assert canonical_event_string(GROUP, 3, "removed", FP, stamp) == (
            f"ghostlink/group-event/v1|{GROUP}|3|removed|{FP}|{stamp}"
        )

    def test_distinct_kinds_produce_distinct_forms(self) -> None:
        forms = {canonical_event_form(GROUP, 1, kind.value, FP, STAMP) for kind in GroupEventKind}
        assert len(forms) == len(GroupEventKind)


class TestSignatureVerification:
    def test_roundtrip(self) -> None:
        identity = LocalIdentity.generate()
        message = canonical_event_form(GROUP, 2, "join", FP, STAMP)
        signature = identity.sign(message)
        assert verify_signature(identity.public_key_bytes, signature, message)

    def test_wrong_key_fails_closed(self) -> None:
        one, other = LocalIdentity.generate(), LocalIdentity.generate()
        message = canonical_create_form("n", "x")
        assert not verify_signature(other.public_key_bytes, one.sign(message), message)

    def test_wrong_message_fails_closed(self) -> None:
        identity = LocalIdentity.generate()
        message = canonical_create_form("n", "x")
        signature = identity.sign(message)
        assert not verify_signature(identity.public_key_bytes, signature, b"tampered")

    def test_malformed_inputs_fail_closed(self) -> None:
        identity = LocalIdentity.generate()
        message = canonical_create_form("n", "x")
        assert not verify_signature(b"\x01" * 31, identity.sign(message), message)
        assert not verify_signature(identity.public_key_bytes, b"", message)
        assert not verify_signature(identity.public_key_bytes, b"\x00" * 63, message)


class TestGroupEventModel:
    def _event(self, **overrides: object) -> GroupEvent:
        payload: dict[str, object] = {
            "group_id": GROUP,
            "epoch": 2,
            "kind": GroupEventKind.JOIN,
            "subject_fingerprint": FP,
            "wall_ts": STAMP,
            "signer_fingerprint": FP2,
            "signature": b"\x01" * 64,
        }
        payload.update(overrides)
        return GroupEvent(**payload)  # type: ignore[arg-type]

    def test_canonical_roundtrip_matches_form(self) -> None:
        event = self._event()
        assert event.canonical() == canonical_event_form(GROUP, 2, "join", FP, STAMP)
        assert event.canonical_string().startswith("ghostlink/group-event/v1|")

    def test_rejects_bad_group_id(self) -> None:
        with pytest.raises(GroupValidationError):
            self._event(group_id="nope")

    def test_rejects_zero_epoch(self) -> None:
        with pytest.raises(GroupValidationError):
            self._event(epoch=0)

    def test_rejects_bad_fingerprints(self) -> None:
        with pytest.raises(GroupValidationError):
            self._event(subject_fingerprint="nope")
        with pytest.raises(GroupValidationError):
            self._event(signer_fingerprint="nope")

    def test_serialization_roundtrip(self) -> None:
        event = self._event(signature=b"\x02" * 64)
        restored = GroupEvent.from_dict(event.to_dict())
        assert restored.group_id == event.group_id
        assert restored.epoch == event.epoch
        assert restored.kind is event.kind
        assert restored.subject_fingerprint == event.subject_fingerprint
        assert restored.signer_fingerprint == event.signer_fingerprint
        assert restored.signature == event.signature
        assert restored.canonical_string() == event.canonical_string()

    def test_from_dict_rejects_malformed(self) -> None:
        with pytest.raises(GroupValidationError):
            GroupEvent.from_dict({"kind": "join"})
        with pytest.raises(GroupValidationError):
            GroupEvent.from_dict(
                {
                    "group_id": GROUP,
                    "epoch": "NaN-ish",
                    "kind": "join",
                    "subject": FP,
                    "wall_ts": STAMP.isoformat(),
                    "signer": FP2,
                    "signature": base64.b64encode(b"\x00" * 64).decode(),
                }
            )

    def test_naive_wall_ts_is_treated_as_utc(self) -> None:
        event = self._event(wall_ts=datetime(2026, 1, 2, 3, 4, 5))
        assert event.wall_ts.tzinfo is not None


class TestFieldValidation:
    def test_fingerprint_for_key_hex_roundtrip(self) -> None:
        identity = LocalIdentity.generate()
        fp = fingerprint_for_key_hex(identity.public_key_hex)
        assert fp.startswith("GLFP-")

    def test_fingerprint_for_key_hex_rejects_bad_hex(self) -> None:
        with pytest.raises(GroupValidationError):
            fingerprint_for_key_hex("not-hex")

    def test_fingerprint_for_key_hex_rejects_short_key(self) -> None:
        with pytest.raises(GroupValidationError):
            fingerprint_for_key_hex("ab" * 16)

    def test_require_valid_fingerprint_normalizes(self) -> None:
        assert require_valid_fingerprint(" glfp-0123-4567-89ab ") == FP

    def test_require_valid_fingerprint_rejects(self) -> None:
        with pytest.raises(GroupValidationError):
            require_valid_fingerprint("GL-nope")

    def test_require_valid_group_id(self) -> None:
        assert require_valid_group_id(" GL-GROUP-aaaa-bbbb-cccc ") == GROUP
        with pytest.raises(GroupValidationError):
            require_valid_group_id("gl-room-AAAA-BBBB-CCCC")

    def test_require_valid_group_id_never_resurrects_prefix(self) -> None:
        # Regression: normalize_group_id unconditionally prepends the
        # prefix, so "gl-group" alone or a prefix-only input must surface
        # the canonical typed error — not a doubled-prefix string.
        for bad in ("gl-group", "gl-group-", "", "gl-group-gl-group-AAAA-BBBB-CCCC"):
            with pytest.raises(GroupValidationError):
                require_valid_group_id(bad)
        assert require_valid_group_id("gl-group-AAAA-BBBB-CCCC") == GROUP
