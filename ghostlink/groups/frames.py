"""Sealed inner frames for group messaging (Phase 6C).

These are the **secret** data class of docs/GROUPS.md §19: every byte in
this module is encrypted per-recipient with the pairwise link key and the
AAD of §20.2 — the relay never sees frame types, ids, sequences, or
text. The envelope carries its `(group, epoch, from, to)` context twice:
in the public AAD/recipient copies and again *inside* the seal, and the
receiver cross-checks sealed-context equality with the envelope context
(§18.2 step 3) — this is the first and only inclusion of that inner
context representation.

All validation is eager and fail-closed. Nothing here may panic; parsing
raises typed group exceptions only.
"""

from __future__ import annotations

import json
import secrets as _secrets
from dataclasses import dataclass, field
from enum import Enum

from ghostlink.constants.net import (
    GROUP_MESSAGE_ID_HEX,
    GROUP_MESSAGE_ID_PREFIX,
    GROUP_MSG_MAX_BYTES,
    GROUP_SEQ_STATE_MAX,
    MAX_GROUP_MEMBERS,
)
from ghostlink.exceptions.groups import GroupMessageError
from ghostlink.groups.events import require_valid_fingerprint
from ghostlink.groups.ids import is_valid_group_id
from ghostlink.groups.models import validate_group_display_name


class FrameType(str, Enum):
    """Inner frame families (§18.1, §18.4; Phase 7 adds sender-key frames)."""

    GMSG = "GMSG"
    GACK = "GACK"
    GREAD = "GREAD"
    # Phase 7 sender-key control frames — these stay on the pairwise mesh
    # (docs/GROUPS.md §36: "control frames stay mesh").
    GSK = "GSK"  # sender-key distribution (chain root + gen + index)
    GSKREQ = "GSKREQ"  # pull a fresh distribution from a sender


REQUIRED_FIELDS: dict[FrameType, frozenset[str]] = {
    FrameType.GMSG: frozenset(
        {"v", "t", "group", "epoch", "from", "to", "id", "gseq", "name", "text", "ts"}
    ),
    FrameType.GACK: frozenset({"v", "t", "group", "epoch", "from", "to", "id"}),
    FrameType.GREAD: frozenset({"v", "t", "group", "epoch", "from", "to", "upto"}),
    FrameType.GSK: frozenset({"v", "t", "group", "epoch", "from", "to", "gen", "root", "index"}),
    FrameType.GSKREQ: frozenset({"v", "t", "group", "epoch", "from", "to"}),
}

# KEX payloads ride the same GROUP_FORWARD envelope as sealed frames,
# distinct from the sealed "msg" routing class. Phase 7 adds:
#   KIND_SK    — sender-key *distribution* (a GSK inner frame) whose public
#                header carries the generation, sealed over a pairwise link.
#   KIND_SKMSG — a sender-key-*sealed* GMSG broadcast whose public header
#                carries {generation, index}; one ciphertext for the whole
#                roster (O(1) seal per sender message).
KIND_KEX: str = "kex"
KIND_MSG: str = "msg"
KIND_SK: str = "sk"
KIND_SKMSG: str = "skmsg"
GROUP_FORWARD_KINDS: frozenset[str] = frozenset({KIND_KEX, KIND_MSG, KIND_SK, KIND_SKMSG})

# The `to` fingerprint used in a *broadcast* sender-key GMSG inner frame.
# It is not a real recipient; receivers cross-check this sentinel instead of
# their own fingerprint (§ sender-key messaging is recipient-agnostic).
GROUP_BROADCAST_RECIPIENT: str = "*"

#
# Since the sealed bytes are opaque, a KEX payload carries a small scheme
# discriminator only (public/never trusted for confidentiality).
KEX_HELLO: str = "hello"
KEX_REPLY: str = "reply"


def generate_message_id() -> str:
    """``gmsg_`` + 16 lowercase hex chars from a CSPRNG (§18.1 step 2)."""

    return f"{GROUP_MESSAGE_ID_PREFIX}{_secrets.token_hex(GROUP_MESSAGE_ID_HEX // 2)}"


def is_valid_message_id(candidate: str) -> bool:
    if not isinstance(candidate, str):
        return False
    if not candidate.startswith(GROUP_MESSAGE_ID_PREFIX):
        return False
    body = candidate[len(GROUP_MESSAGE_ID_PREFIX) :]
    if len(body) != GROUP_MESSAGE_ID_HEX:
        return False
    return all(char in "0123456789abcdef" for char in body)


@dataclass(frozen=True, slots=True)
class GroupInnerFrame:
    """One validated sealed inner frame (context + payload, decrypted)."""

    frame_type: FrameType
    group_id: str
    epoch: int
    sender: str
    recipient: str
    message_id: str = ""
    gseq: int = 0
    display_name: str = ""
    text: str = ""
    ts: float = 0.0
    # Phase 7 sender-key fields (GSK / GSKREQ only; empty otherwise).
    gen: int = 0
    root: str = ""
    index: int = 0


def _base_fields(
    frame_type: FrameType, group_id: str, epoch: int, sender: str, recipient: str
) -> dict[str, object]:
    if not is_valid_group_id(group_id):
        raise GroupMessageError(
            f"Inner-frame group id '{group_id}' is malformed.",
            hint="Group ids look like gl-group-XXXX-XXXX-XXXX.",
        )
    if epoch < 1:
        raise GroupMessageError("Inner-frame epoch must be ≥ 1.", hint="Epochs start at 1.")
    return {
        "v": 1,
        "t": frame_type.value,
        "group": group_id,
        "epoch": epoch,
        "from": require_valid_fingerprint(sender, field="sender fingerprint"),
        "to": require_valid_fingerprint(recipient, field="recipient fingerprint"),
    }


def gmsg_frame(
    group_id: str,
    epoch: int,
    sender: str,
    recipient: str,
    *,
    message_id: str,
    gseq: int,
    display_name: str,
    text: str,
    ts: float,
    broadcast: bool = False,
) -> bytes:
    """Build a GMSG inner frame; validates eagerly, never crashes.

    ``broadcast=True`` (sender-key mode) emits a recipient-agnostic frame
    addressed to ``GROUP_BROADCAST_RECIPIENT`` — the same ciphertext is
    fanned out to the whole roster and every receiver accepts it.
    """

    if broadcast:
        if recipient != GROUP_BROADCAST_RECIPIENT:
            raise GroupMessageError(
                "A broadcast GMSG must target the broadcast recipient.",
                hint="Sender-key frames are sealed once for the roster.",
            )
        fields = {
            "v": 1,
            "t": FrameType.GMSG.value,
            "group": group_id,
            "epoch": epoch,
            "from": require_valid_fingerprint(sender, field="sender fingerprint"),
            "to": GROUP_BROADCAST_RECIPIENT,
        }
    else:
        fields = _base_fields(FrameType.GMSG, group_id, epoch, sender, recipient)
    if not is_valid_message_id(message_id):
        raise GroupMessageError(
            f"Inner-frame message id '{message_id}' is malformed.",
            hint="Message ids look like gmsg_<16 hex>.",
        )
    if gseq < 0:
        raise GroupMessageError("Inner-frame gseq must be ≥ 0.", hint="")
    encoded_name = validate_group_display_name(display_name or sender)
    raw_text = text.encode("utf-8")
    if len(raw_text) > GROUP_MSG_MAX_BYTES:
        raise GroupMessageError(
            f"Group message must be ≤ {GROUP_MSG_MAX_BYTES} UTF-8 bytes, got {len(raw_text)}.",
            hint="Split the message or shorten it.",
        )
    fields.update(
        {
            "id": message_id,
            "gseq": gseq,
            "name": encoded_name,
            "text": text,
            "ts": float(ts),
        }
    )
    return json.dumps(fields, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def gack_frame(group_id: str, epoch: int, sender: str, recipient: str, *, message_id: str) -> bytes:
    """Build a GACK delivery acknowledgement for ``message_id``."""

    fields = _base_fields(FrameType.GACK, group_id, epoch, sender, recipient)
    if not is_valid_message_id(message_id):
        raise GroupMessageError(
            f"Inner-frame message id '{message_id}' is malformed.",
            hint="Message ids look like gmsg_<16 hex>.",
        )
    fields["id"] = message_id
    return json.dumps(fields, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def gread_frame(group_id: str, epoch: int, sender: str, recipient: str, *, upto_gseq: int) -> bytes:
    """Build a GREAD cumulative read cursor (per sender's gseq)."""

    fields = _base_fields(FrameType.GREAD, group_id, epoch, sender, recipient)
    if upto_gseq < 0:
        raise GroupMessageError("GREAD cursor must be ≥ 0.", hint="")
    fields["upto"] = upto_gseq
    return json.dumps(fields, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def gsk_frame(
    group_id: str,
    epoch: int,
    sender: str,
    recipient: str,
    *,
    gen: int,
    root: str,
    index: int,
) -> bytes:
    """Build a GSK sender-key distribution inner frame (Phase 7).

    ``root`` is the base64 chain root — a secret. This frame is always
    sealed over an identity-bound pairwise link, so it never travels as
    plaintext; only the link's AEAD envelope exposes it to the recipient.
    """

    fields = _base_fields(FrameType.GSK, group_id, epoch, sender, recipient)
    if gen < 1:
        raise GroupMessageError("GSK generation must be ≥ 1.", hint="")
    if index < 1:
        raise GroupMessageError("GSK index must be ≥ 1.", hint="")
    fields["gen"] = gen
    fields["root"] = root
    fields["index"] = index
    return json.dumps(fields, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def gskreq_frame(group_id: str, epoch: int, sender: str, recipient: str) -> bytes:
    """Build a GSKREQ key-request inner frame (pull a fresh distribution)."""

    fields = _base_fields(FrameType.GSKREQ, group_id, epoch, sender, recipient)
    return json.dumps(fields, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def parse_inner_frame(data: bytes, *, max_bytes: int = GROUP_MSG_MAX_BYTES * 3) -> GroupInnerFrame:
    """Parse+validate decrypted frame bytes; any defect raises once."""

    if not data or len(data) > max_bytes:
        raise GroupMessageError(
            "Decrypted inner frame is empty or oversized.",
            hint="The sealed payload failed shape validation — treat as hostile.",
        )
    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise GroupMessageError(
            "Decrypted inner frame is not valid JSON.",
            hint="The sealed payload failed shape validation — treat as hostile.",
        ) from exc
    if not isinstance(obj, dict):
        raise GroupMessageError(
            "Decrypted inner frame is not an object.",
            hint="The sealed payload failed shape validation — treat as hostile.",
        )
    try:
        frame_type = FrameType(str(obj.get("t", "")))
    except ValueError as exc:
        raise GroupMessageError(
            "Unknown inner frame type.",
            hint="The sealed payload failed shape validation — treat as hostile.",
        ) from exc
    required = REQUIRED_FIELDS[frame_type]
    missing = required - set(obj)
    if missing or obj.get("v") != 1:
        raise GroupMessageError(
            f"Inner frame is missing field(s): {', '.join(sorted(missing)) or 'v'}.",
            hint="The sealed payload failed shape validation — treat as hostile.",
        )
    extra = set(obj) - required
    if extra:
        raise GroupMessageError(
            f"Inner frame carries unknown field(s): {', '.join(sorted(extra))}.",
            hint="The sealed payload failed shape validation — treat as hostile.",
        )
    group_id = str(obj["group"])
    if not is_valid_group_id(group_id):
        raise GroupMessageError("Inner-frame group id is malformed.", hint="")
    try:
        epoch = int(str(obj["epoch"]))
    except ValueError as exc:
        raise GroupMessageError("Inner-frame epoch is not an integer.", hint="") from exc
    if epoch < 1:
        raise GroupMessageError("Inner-frame epoch must be ≥ 1.", hint="")
    sender = require_valid_fingerprint(str(obj["from"]), field="sender fingerprint")
    raw_to = str(obj["to"])
    if frame_type is FrameType.GMSG and raw_to == GROUP_BROADCAST_RECIPIENT:
        # Sender-key broadcast frame: recipient is the roster sentinel.
        recipient = GROUP_BROADCAST_RECIPIENT
    else:
        recipient = require_valid_fingerprint(raw_to, field="recipient fingerprint")
    if frame_type is FrameType.GACK:
        message_id = str(obj["id"])
        if not is_valid_message_id(message_id):
            raise GroupMessageError("GACK message id is malformed.", hint="")
        return GroupInnerFrame(frame_type, group_id, epoch, sender, recipient, message_id)
    if frame_type is FrameType.GREAD:
        try:
            upto = int(str(obj["upto"]))
        except ValueError as exc:
            raise GroupMessageError("GREAD cursor is not an integer.", hint="") from exc
        if upto < 0:
            raise GroupMessageError("GREAD cursor must be ≥ 0.", hint="")
        return GroupInnerFrame(frame_type, group_id, epoch, sender, recipient, gseq=upto)
    if frame_type is FrameType.GSK:
        try:
            gen = int(str(obj["gen"]))
            index = int(str(obj["index"]))
        except ValueError as exc:
            raise GroupMessageError("GSK gen/index are not integers.", hint="") from exc
        root = str(obj["root"])
        if gen < 1 or index < 1:
            raise GroupMessageError("GSK gen/index must be ≥ 1.", hint="")
        if not (0 < len(root) <= 128):
            raise GroupMessageError("GSK root is malformed.", hint="")
        return GroupInnerFrame(
            frame_type, group_id, epoch, sender, recipient, gen=gen, root=root, index=index
        )
    if frame_type is FrameType.GSKREQ:
        return GroupInnerFrame(frame_type, group_id, epoch, sender, recipient)
    # GMSG
    message_id = str(obj["id"])
    if not is_valid_message_id(message_id):
        raise GroupMessageError("Inner-frame message id is malformed.", hint="")
    try:
        gseq = int(str(obj["gseq"]))
        ts = float(str(obj["ts"]))
    except ValueError as exc:
        raise GroupMessageError("Inner-frame gseq/ts are malformed.", hint="") from exc
    name = str(obj["name"])
    if any(ord(char) < 32 or ord(char) == 127 for char in name) or not (0 < len(name) <= 24):
        raise GroupMessageError("Inner-frame display name is malformed.", hint="")
    text = str(obj["text"])
    if len(text.encode("utf-8")) > GROUP_MSG_MAX_BYTES:
        raise GroupMessageError("Inner-frame text exceeds the byte ceiling.", hint="")
    return GroupInnerFrame(
        frame_type,
        group_id,
        epoch,
        sender,
        recipient,
        message_id,
        gseq,
        name,
        text,
        ts,
    )


# ---------------------------------------------------------------- dedupe


class SeenMessageIds:
    """Bounded LRU dedupe for (sender, message_id) — §21 layer 2.

    Capacity 512 per sender; evicted+unknown arrivals are safe-direction
    dropped by callers (``False`` from :meth:`seen`). Memory-only by
    design (§21.5: nothing here needs to outlive the session).
    """

    def __init__(self, capacity: int) -> None:
        from collections import OrderedDict

        self._capacity = max(1, capacity)
        self._seen: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._senders: dict[str, int] = {}

    def seen(self, sender: str, message_id: str) -> bool:
        """True (and mark) iff (sender, id) was already seen."""

        key = (sender, message_id)
        if key in self._seen:
            self._seen.move_to_end(key)
            return True
        self._seen[key] = None
        self._senders[sender] = self._senders.get(sender, 0) + 1
        self._evict_if_needed(sender)
        # Defensive whole-cache bound on top of per-sender discipline.
        while len(self._seen) > self._capacity * MAX_GROUP_MEMBERS:
            evicted_sender, _mid = next(iter(self._seen))
            self._seen.popitem(last=False)
            self._senders[evicted_sender] = self._senders.get(evicted_sender, 1) - 1
        return False

    def _evict_if_needed(self, sender: str) -> None:
        while self._senders.get(sender, 0) > self._capacity:
            for key in self._seen:
                if key[0] == sender:
                    del self._seen[key]
                    self._senders[sender] -= 1
                    break

    def __len__(self) -> int:
        return len(self._seen)


# ---------------------------------------------------------------- gseq


class GseqTracker:
    """Per-sender monotone gseq acceptance with bounded gaps (§22).

    * accept when gseq > last_seen (gap ≤ 64 ⇒ flag; > 64 ⇒ suspect-drop);
    * gseq ≤ last_seen ⇒ duplicate/replay marker.
    * Bounded: at most one cursor per roster fingerprint.
    """

    def __init__(self, max_gap: int, *, max_senders: int = GROUP_SEQ_STATE_MAX) -> None:
        self._max_gap = max_gap
        self._max_senders = max_senders
        self._last: dict[str, int] = {}

    def observe(self, sender: str, gseq: int) -> str:
        """→ "new" | "gap" | "duplicate" | "excessive-gap"."""

        if sender not in self._last and len(self._last) >= self._max_senders:
            return "excessive-gap"  # unknown beyond capacity — fail safe
        last = self._last.get(sender)
        if last is None:
            self._last[sender] = gseq
            return "new"
        if gseq <= last:
            return "duplicate"
        gap = gseq - last
        if gap > self._max_gap:
            return "excessive-gap"
        self._last[sender] = gseq
        return "gap" if gap > 1 else "new"

    def cursor(self, sender: str) -> int:
        return self._last.get(sender, 0)


# ------------------------------------------------------- delivery ledger


class DeliveryState(str, Enum):
    """Per-recipient delivery state for one group message (§18.1 step 5)."""

    QUEUED = "queued"
    SENDING = "sending"
    SENT = "sent"
    DELIVERED = "delivered"
    READ = "read"
    FAILED = "failed"


TERMINAL_DELIVERY_STATES: frozenset[DeliveryState] = frozenset(
    {DeliveryState.DELIVERED, DeliveryState.READ}
)


@dataclass(slots=True)
class GroupMessageLedger:
    """One outbound group message + per-recipient delivery states."""

    message_id: str
    group_id: str
    gseq: int
    text: str
    ts: float
    recipients: dict[str, DeliveryState] = field(default_factory=dict)

    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for state in self.recipients.values():
            counts[state.value] = counts.get(state.value, 0) + 1
        return counts

    def total(self) -> int:
        return len(self.recipients)

    def delivered_count(self) -> int:
        return sum(1 for state in self.recipients.values() if state in TERMINAL_DELIVERY_STATES)

    def failed_count(self) -> int:
        return sum(1 for state in self.recipients.values() if state is DeliveryState.FAILED)

    def pending_recipients(self) -> list[str]:
        return sorted(
            fp
            for fp, state in self.recipients.items()
            if state not in TERMINAL_DELIVERY_STATES and state is not DeliveryState.FAILED
        )

    def failed_recipients(self) -> list[str]:
        return sorted(fp for fp, state in self.recipients.items() if state is DeliveryState.FAILED)

    def apply_read_cursor(self, sender_cursor: int) -> None:
        """GREAD cursor from one recipient covers ≤ ``sender_cursor`` gseq."""

        for fp, state in self.recipients.items():
            if state is DeliveryState.DELIVERED and self.gseq <= sender_cursor:
                self.recipients[fp] = DeliveryState.READ

    def status_line(self) -> str:
        total = self.total()
        if total == 0:
            return "no recipients"
        delivered = self.delivered_count()
        failed = self.failed_count()
        if delivered == total:
            return f"delivered {total}/{total}"
        parts = [f"delivered {delivered}/{total}"]
        if failed:
            parts.append(f"failed {failed}")
        queued = self.counts().get(DeliveryState.QUEUED.value, 0)
        if queued:
            parts.append(f"offline {queued}")
        return ", ".join(parts)


__all__ = [
    "GROUP_BROADCAST_RECIPIENT",
    "GROUP_FORWARD_KINDS",
    "KEX_HELLO",
    "KEX_REPLY",
    "KIND_KEX",
    "KIND_MSG",
    "KIND_SK",
    "KIND_SKMSG",
    "DeliveryState",
    "FrameType",
    "GroupInnerFrame",
    "GroupMessageLedger",
    "GseqTracker",
    "SeenMessageIds",
    "gack_frame",
    "generate_message_id",
    "gmsg_frame",
    "gread_frame",
    "gsk_frame",
    "gskreq_frame",
    "is_valid_message_id",
    "parse_inner_frame",
]
