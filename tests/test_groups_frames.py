"""Unit tests for the Phase 6C sealed inner frames (docs/GROUPS.md §19-§22).

Every parser path must fail closed with a typed error, never crash; all
validation is eager. No networking here.
"""

from __future__ import annotations

import json

import pytest

from ghostlink.constants.net import (
    GROUP_GSEQ_MAX_GAP,
    GROUP_MSG_MAX_BYTES,
    GROUP_SEEN_IDS_PER_SENDER,
)
from ghostlink.exceptions.groups import GroupMessageError, GroupValidationError
from ghostlink.groups.frames import (
    DeliveryState,
    FrameType,
    GroupMessageLedger,
    GseqTracker,
    SeenMessageIds,
    gack_frame,
    generate_message_id,
    gmsg_frame,
    gread_frame,
    is_valid_message_id,
    parse_inner_frame,
)

GROUP = "gl-group-AAAA-BBBB-CCCC"
FP_A = "GLFP-1111-1111-1111"
FP_B = "GLFP-2222-2222-2222"
FP_C = "GLFP-3333-3333-3333"


def _gmsg(**overrides: object) -> bytes:
    kwargs: dict[str, object] = {
        "message_id": generate_message_id(),
        "gseq": 1,
        "display_name": "Alice",
        "text": "hello group",
        "ts": 1700000000.0,
    }
    kwargs.update(overrides)
    return gmsg_frame(GROUP, 1, FP_A, FP_B, **kwargs)  # type: ignore[arg-type]


# ------------------------------------------------------------ message ids


class TestMessageIds:
    def test_format(self) -> None:
        message_id = generate_message_id()
        assert message_id.startswith("gmsg_")
        assert len(message_id) == 5 + 16
        assert is_valid_message_id(message_id)

    def test_unique(self) -> None:
        assert generate_message_id() != generate_message_id()

    def test_not_timestamp_based(self) -> None:
        # §18.1: sequence/ids never derive from time.
        ids = {generate_message_id() for _ in range(50)}
        assert len(ids) == 50

    @pytest.mark.parametrize(
        "candidate",
        [
            "",
            "gmsg_",
            "gmsg_1234",
            "msg_0123456789abcdef",  # 1:1 ids are not group ids
            "gmsg_0123456789abcdef0",
            "gmsg_0123456789abcdeg",
            "gmsg_0123456789ABCDEF",  # uppercase hex rejected
            "ggmsg_0123456789abcdef",
        ],
    )
    def test_invalid(self, candidate: str) -> None:
        assert not is_valid_message_id(candidate)


# ------------------------------------------------------------ frame build


class TestFrameBuilders:
    def test_gmsg_roundtrip(self) -> None:
        raw = _gmsg()
        frame = parse_inner_frame(raw)
        assert frame.frame_type is FrameType.GMSG
        assert frame.group_id == GROUP
        assert frame.epoch == 1
        assert frame.sender == FP_A
        assert frame.recipient == FP_B
        assert frame.text == "hello group"
        assert frame.display_name == "Alice"
        assert frame.gseq == 1

    def test_gack_roundtrip(self) -> None:
        message_id = generate_message_id()
        raw = gack_frame(GROUP, 2, FP_B, FP_A, message_id=message_id)
        frame = parse_inner_frame(raw)
        assert frame.frame_type is FrameType.GACK
        assert frame.message_id == message_id

    def test_gread_roundtrip(self) -> None:
        raw = gread_frame(GROUP, 3, FP_B, FP_A, upto_gseq=41)
        frame = parse_inner_frame(raw)
        assert frame.frame_type is FrameType.GREAD
        assert frame.gseq == 41

    def test_max_text_boundary(self) -> None:
        text = "x" * GROUP_MSG_MAX_BYTES
        frame = parse_inner_frame(_gmsg(text=text))
        assert len(frame.text.encode("utf-8")) == GROUP_MSG_MAX_BYTES
        with pytest.raises(GroupMessageError):
            _gmsg(text=text + "x")

    def test_multibyte_text_counts_bytes(self) -> None:
        text = "हिं" * 2000  # 6 bytes/char → far over the cap
        with pytest.raises(GroupMessageError):
            _gmsg(text=text)

    def test_invalid_group_rejected(self) -> None:
        with pytest.raises(GroupMessageError):
            gmsg_frame(
                "not-a-group",
                1,
                FP_A,
                FP_B,
                message_id=generate_message_id(),
                gseq=1,
                display_name="A",
                text="t",
                ts=1.0,
            )

    def test_epoch_zero_rejected(self) -> None:
        with pytest.raises(GroupMessageError):
            _gmsg() if False else gack_frame(GROUP, 0, FP_A, FP_B, message_id=generate_message_id())

    def test_bad_message_id_rejected(self) -> None:
        with pytest.raises(GroupMessageError):
            _gmsg(message_id="msg_0123456789abcdef")

    def test_control_characters_in_name_rejected(self) -> None:
        with pytest.raises(GroupValidationError):
            _gmsg(display_name="Ali\x1b[0mce")

    def test_frame_carries_context_twice(self) -> None:
        # §19 note: the sealed frame duplicates the envelope context so the
        # receiver can cross-check equality (§18.2 step 3).
        fields = json.loads(_gmsg().decode("utf-8"))
        for key in ("group", "epoch", "from", "to"):
            assert key in fields


# --------------------------------------------------------------- parsing


def _fields(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "v": 1,
        "t": "GMSG",
        "group": GROUP,
        "epoch": 1,
        "from": FP_A,
        "to": FP_B,
        "id": generate_message_id(),
        "gseq": 7,
        "name": "Alice",
        "text": "hi",
        "ts": 1700000000.0,
    }
    fields.update(overrides)
    return fields


class TestParsing:
    def test_empty(self) -> None:
        with pytest.raises(GroupMessageError):
            parse_inner_frame(b"")

    def test_oversized(self) -> None:
        with pytest.raises(GroupMessageError):
            parse_inner_frame(b"x" * (GROUP_MSG_MAX_BYTES * 3 + 1))

    def test_not_json(self) -> None:
        with pytest.raises(GroupMessageError):
            parse_inner_frame(b"\x89\xa1\xff not json")

    def test_not_object(self) -> None:
        with pytest.raises(GroupMessageError):
            parse_inner_frame(b"[1,2,3]")

    def test_unknown_type(self) -> None:
        with pytest.raises(GroupMessageError):
            parse_inner_frame(json.dumps(_fields(t="GMONEY")).encode())

    def test_bad_version(self) -> None:
        with pytest.raises(GroupMessageError):
            parse_inner_frame(json.dumps(_fields(v=2)).encode())

    def test_missing_field(self) -> None:
        fields = _fields()
        del fields["gseq"]
        with pytest.raises(GroupMessageError):
            parse_inner_frame(json.dumps(fields).encode())

    def test_unknown_extra_field_rejected(self) -> None:
        with pytest.raises(GroupMessageError):
            parse_inner_frame(json.dumps(_fields(evil="x")).encode())

    def test_wrong_types_rejected(self) -> None:
        with pytest.raises(GroupMessageError):
            parse_inner_frame(json.dumps(_fields(epoch="-one")).encode())

    def test_never_crashes_on_fuzz(self) -> None:
        corpus = [
            b"\x00" * 10,
            b"{}",
            b'{"v":1}',
            b'{"v":1,"t":"GMSG"}',
            b'{"v":1,"t":"GACK","id":"gmsg_x"}',
            json.dumps(_fields(gSeq=1, gseq=None)).encode(),
            json.dumps(_fields(ts="not-a-float")).encode(),
            json.dumps(_fields(name="")).encode(),
            json.dumps(_fields(name="\x00bad")).encode(),
            json.dumps(_fields(text=123)).encode(),
        ]
        for blob in corpus:
            try:
                parse_inner_frame(blob)
            except GroupMessageError:
                pass  # typed failure is the only acceptable outcome
            except (ValueError, AttributeError, TypeError) as exc:
                raise AssertionError(f"parser leaked a raw exception on {blob!r}: {exc}") from exc


# ------------------------------------------------------------- dedupe LRU


class TestSeenMessageIds:
    def test_first_seen_false_then_true(self) -> None:
        seen = SeenMessageIds(GROUP_SEEN_IDS_PER_SENDER)
        assert seen.seen(FP_A, "gmsg_1") is False
        assert seen.seen(FP_A, "gmsg_1") is True

    def test_scoped_per_sender(self) -> None:
        seen = SeenMessageIds(8)
        assert seen.seen(FP_A, "gmsg_1") is False
        assert seen.seen(FP_B, "gmsg_1") is False  # same id, different sender

    def test_bounded_per_sender(self) -> None:
        capacity = 16
        seen = SeenMessageIds(capacity)
        ids = [f"gmsg_{index:016x}" for index in range(capacity * 3)]
        for message_id in ids:
            seen.seen(FP_A, message_id)
        # the newest window survives and stays tracked (True = known/seen)
        assert seen.seen(FP_A, ids[-1]) is True
        # … while the oldest ids were evicted, safely reported as unknown
        assert seen.seen(FP_A, ids[0]) is False
        kept = sum(1 for message_id in ids[1:] if seen.seen(FP_A, message_id))
        assert kept <= capacity + 1  # ids[0]/ids[-1] re-marked above

    def test_whole_cache_bound(self) -> None:
        seen = SeenMessageIds(4)
        senders = [f"GLFP-0000-0000-000{i}" for i in range(64)]
        for round_ in range(8):
            for sender in senders:
                seen.seen(sender, f"gmsg_{round_}")
        assert len(seen) <= 4 * 64  # capacity × worst-case roster bound


# ------------------------------------------------------------ gseq tracker


class TestGseqTracker:
    def test_monotone_accept(self) -> None:
        tracker = GseqTracker(GROUP_GSEQ_MAX_GAP)
        assert tracker.observe(FP_A, 1) == "new"
        assert tracker.observe(FP_A, 2) == "new"
        assert tracker.observe(FP_A, 3) == "new"

    def test_gap_flag(self) -> None:
        tracker = GseqTracker(GROUP_GSEQ_MAX_GAP)
        tracker.observe(FP_A, 1)
        assert tracker.observe(FP_A, 5) == "gap"
        assert tracker.cursor(FP_A) == 5

    def test_duplicate_rollback(self) -> None:
        tracker = GseqTracker(GROUP_GSEQ_MAX_GAP)
        tracker.observe(FP_A, 4)
        assert tracker.observe(FP_A, 4) == "duplicate"
        assert tracker.observe(FP_A, 3) == "duplicate"

    def test_excessive_gap(self) -> None:
        tracker = GseqTracker(GROUP_GSEQ_MAX_GAP)
        tracker.observe(FP_A, 1)
        assert tracker.observe(FP_A, 1 + GROUP_GSEQ_MAX_GAP + 1) == "excessive-gap"

    def test_per_sender_isolation(self) -> None:
        tracker = GseqTracker(GROUP_GSEQ_MAX_GAP)
        tracker.observe(FP_A, 10)
        assert tracker.observe(FP_B, 1) == "new"
        assert tracker.observe(FP_A, 10) == "duplicate"


# ----------------------------------------------------------- ledger states


def _ledger() -> GroupMessageLedger:
    ledger = GroupMessageLedger(
        message_id=generate_message_id(),
        group_id=GROUP,
        gseq=1,
        text="hello",
        ts=1700000000.0,
    )
    ledger.recipients = {
        FP_A: DeliveryState.SENT,
        FP_B: DeliveryState.SENT,
        FP_C: DeliveryState.QUEUED,
    }
    return ledger


class TestLedger:
    def test_status_line_partial(self) -> None:
        ledger = _ledger()
        ledger.recipients[FP_A] = DeliveryState.DELIVERED
        assert ledger.status_line() == "delivered 1/3, offline 1"

    def test_status_line_full(self) -> None:
        ledger = _ledger()
        for fp in ledger.recipients:
            ledger.recipients[fp] = DeliveryState.DELIVERED
        assert ledger.status_line() == "delivered 3/3"

    def test_status_line_failed(self) -> None:
        ledger = _ledger()
        ledger.recipients[FP_C] = DeliveryState.FAILED
        assert ledger.status_line() == "delivered 0/3, failed 1"

    def test_never_full_when_partial(self) -> None:
        # §18.3: one DELIVERED must never read as full delivery.
        ledger = _ledger()
        ledger.recipients[FP_A] = DeliveryState.DELIVERED
        assert ledger.delivered_count() == 1
        assert ledger.status_line() != f"delivered {ledger.total()}/{ledger.total()}"

    def test_read_cursor_advances_delivered(self) -> None:
        ledger = _ledger()
        ledger.recipients[FP_A] = DeliveryState.DELIVERED
        ledger.apply_read_cursor(1)
        assert ledger.recipients[FP_A] is DeliveryState.READ
        assert ledger.recipients[FP_B] is DeliveryState.SENT  # untouched

    def test_pending_and_failed_lists(self) -> None:
        ledger = _ledger()
        assert ledger.pending_recipients() == [FP_A, FP_B, FP_C]
        ledger.recipients[FP_A] = DeliveryState.FAILED
        assert ledger.failed_recipients() == [FP_A]
        assert ledger.pending_recipients() == [FP_B, FP_C]
