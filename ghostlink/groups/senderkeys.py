"""Sender-key chains for group messaging (Phase 7 — docs/GROUPS.md §36-§37).

Phase 6C encrypts every group message once *per recipient* (O(n) seals).
Phase 7 replaces the per-recipient message seal with a **sender-key**: each
sender derives a forward-evolving hash-ratchet chain for the current
(group, epoch); every message is sealed **once** with the sender's current
message key (O(1) per message), broadcast to the roster as the same
ciphertext, and opened by every recipient that holds that sender's chain.

No new cryptographic primitive is introduced — only the Phase 3 stack
(X25519 for the pairwise distribution channel, HKDF-SHA256 for the chain,
ChaCha20-Poly1305 for sealing) is reused, with an explicit domain
separation string: ``ghostlink/group/senderkey/v1``.

Key lifecycle / secrecy:

* Chain keys and message keys are secrets. They live only in process
  memory; chain keys sit in ``bytearray`` slots so teardown paths actually
  overwrite the bytes. Nothing here touches disk, logs, exceptions, or
  debug output.
* Sender chains are **epoch-scoped**: a roster mutation bumps the epoch and
  every member derives a fresh chain; old-epoch chains are pruned. A
  removed member never receives the new epoch's distribution; a joiner
  receives only the current epoch's chains (the hash ratchet gives no
  backward derivation).
* The chain is a one-way HKDF ratchet: ``chain_key(n)`` yields
  ``message_key(n)`` and ``chain_key(n+1)`` but cannot be reversed to
  recover earlier message keys. This gives per-message forward secrecy at
  the epoch granularity — honestly stated: compromising the *live* chain
  reveals that sender's current-and-future messages, not its past ones
  (and not other senders').
* Message-key derivation binds group id, epoch, sender identity, generation
  and the message index (defense in depth on top of the AEAD AAD).

Out-of-order delivery uses a **bounded skipped-key cache** per sender
(``GROUP_SK_SKIPPED_MAX``): when a message jumps ahead by ≤ the bound, the
intermediate message keys are derived and cached; messages too far ahead
are rejected instead of allocating unbounded memory.
"""

from __future__ import annotations

import base64
import secrets
from collections import OrderedDict
from dataclasses import dataclass
from typing import NoReturn

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.hashes import SHA256
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from ghostlink.constants.net import (
    GROUP_SK_MAX_RECEIVER_STATE,
    GROUP_SK_ROOT_BYTES,
    GROUP_SK_SKIPPED_MAX,
)

SK_INFO: bytes = b"ghostlink/group/senderkey/v1"
_NONCE_BYTES: int = 12
_KEY_BYTES: int = 32


class SenderKeyError(Exception):
    """Base class for sender-key failures (never carries key material)."""

    def __init__(self, message: str, *, hint: str = "") -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint


class MissingSenderKeyError(SenderKeyError):
    """The recipient holds no valid chain for this sender/epoch/generation."""


class ReplayError(SenderKeyError):
    """The message index is stale or already accepted (replay/duplicate)."""


class ExcessiveGapError(SenderKeyError):
    """The message jumped too far ahead of the accepted sequence."""


def _hkdf(seed: bytes, parts: list[bytes]) -> bytes:
    """One HKDF-SHA256 derivation bound to the sender-key domain."""
    info = SK_INFO + b"|" + b"|".join(parts)
    return HKDF(algorithm=SHA256(), length=_KEY_BYTES, salt=None, info=info).derive(seed)


def _b(value: int | str) -> bytes:
    return str(value).encode("ascii")


def generate_chain_root() -> bytes:
    """A fresh 32-byte chain root from the CSPRNG (the ratchet's start)."""
    return secrets.token_bytes(GROUP_SK_ROOT_BYTES)


def derive_message_key(
    chain_key: bytes,
    *,
    group_id: str,
    epoch: int,
    sender: str,
    gen: int,
    index: int,
) -> bytes:
    """The message key for one chain position, bound to the full context."""
    return _hkdf(
        chain_key,
        [b"msg", _b(group_id), _b(epoch), _b(sender), _b(gen), _b(index)],
    )


def advance_chain(
    chain_key: bytes,
    *,
    group_id: str,
    epoch: int,
    sender: str,
    gen: int,
    index: int,
) -> bytes:
    """The next chain key after ``index`` (one-way ratchet step)."""
    return _hkdf(
        chain_key,
        [b"chain", _b(group_id), _b(epoch), _b(sender), _b(gen), _b(index)],
    )


def seal_message(message_key: bytes, plaintext: bytes, aad: bytes) -> bytes:
    """ChaCha20-Poly1305 seal with a fresh random 96-bit nonce."""
    nonce = secrets.token_bytes(_NONCE_BYTES)
    return nonce + ChaCha20Poly1305(message_key).encrypt(nonce, plaintext, aad)


def open_message(message_key: bytes, sealed: bytes, aad: bytes) -> bytes:
    """Open+verify a sender-key-sealed message; tampering raises SenderKeyError."""
    if len(sealed) <= _NONCE_BYTES:
        raise SenderKeyError("Sender-key payload is too short.", hint="Truncated or corrupt frame.")
    nonce, ciphertext = sealed[:_NONCE_BYTES], sealed[_NONCE_BYTES:]
    try:
        return ChaCha20Poly1305(message_key).decrypt(nonce, ciphertext, aad)
    except InvalidTag as exc:
        raise SenderKeyError(
            "Sender-key message integrity check failed.",
            hint="The ciphertext was modified or the message key does not match.",
        ) from exc


def distribution_root_b64(root: bytes) -> str:
    """Base64 form of the chain root for the (sealed) distribution frame."""
    return base64.b64encode(root).decode("ascii")


def skmsg_body(gen: int, seq: int, sealed: bytes) -> bytes:
    """Public envelope body for a sender-key message: ``{g, s, f}``.

    ``gen``/``seq`` ride in the public header so the receiver can derive the
    correct message key (the AEAD AAD authenticates them — tampering breaks
    the tag). ``sealed`` is the ChaCha20-Poly1305 ciphertext.
    """
    if gen < 1 or seq < 1:
        raise SenderKeyError(
            "Sender-key gen/seq must be ≥ 1.", hint="Malformed sender-key message."
        )
    document = {
        "g": gen,
        "s": seq,
        "f": base64.b64encode(sealed).decode("ascii"),
    }
    import json

    return json.dumps(document, separators=(",", ":")).encode("utf-8")


def parse_skmsg_body(data: bytes, *, max_sealed: int) -> tuple[int, int, bytes]:
    """Parse+validate a sender-key message envelope body."""
    import json

    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SenderKeyError(
            "Sender-key message body is not valid JSON.",
            hint="Malformed sender-key frame.",
        ) from exc
    if not isinstance(obj, dict):
        raise SenderKeyError("Sender-key message body is not an object.", hint="")
    gen = obj.get("g")
    seq = obj.get("s")
    frame_b64 = obj.get("f")
    if not (isinstance(gen, int) and not isinstance(gen, bool) and gen >= 1):
        raise SenderKeyError("Sender-key generation is malformed.", hint="")
    if not (isinstance(seq, int) and not isinstance(seq, bool) and seq >= 1):
        raise SenderKeyError("Sender-key index is malformed.", hint="")
    if not isinstance(frame_b64, str):
        raise SenderKeyError("Sender-key ciphertext is malformed.", hint="")
    try:
        sealed = base64.b64decode(frame_b64.encode("ascii"), validate=True)
    except Exception as exc:
        raise SenderKeyError(
            "Sender-key ciphertext is not valid base64.", hint="Malformed frame."
        ) from exc
    if not (0 < len(sealed) <= max_sealed):
        raise SenderKeyError(
            "Sender-key ciphertext has an invalid length.", hint="Malformed frame."
        )
    return gen, seq, sealed


def sk_control_body(gen: int, sealed: bytes) -> bytes:
    """Public envelope body for a GSK distribution: ``{g, f}``."""
    if gen < 1:
        raise SenderKeyError("Sender-key generation must be ≥ 1.", hint="")
    document = {"g": gen, "f": base64.b64encode(sealed).decode("ascii")}
    import json

    return json.dumps(document, separators=(",", ":")).encode("utf-8")


def parse_sk_control_body(data: bytes, *, max_sealed: int) -> tuple[int, bytes]:
    """Parse+validate a GSK distribution envelope body."""
    import json

    try:
        obj = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SenderKeyError(
            "Sender-key control body is not valid JSON.",
            hint="Malformed sender-key frame.",
        ) from exc
    if not isinstance(obj, dict):
        raise SenderKeyError("Sender-key control body is not an object.", hint="")
    gen = obj.get("g")
    frame_b64 = obj.get("f")
    if not (isinstance(gen, int) and not isinstance(gen, bool) and gen >= 1):
        raise SenderKeyError("Sender-key generation is malformed.", hint="")
    if not isinstance(frame_b64, str):
        raise SenderKeyError("Sender-key distribution is malformed.", hint="")
    try:
        sealed = base64.b64decode(frame_b64.encode("ascii"), validate=True)
    except Exception as exc:
        raise SenderKeyError(
            "Sender-key distribution is not valid base64.", hint="Malformed frame."
        ) from exc
    if not (0 < len(sealed) <= max_sealed):
        raise SenderKeyError(
            "Sender-key distribution has an invalid length.", hint="Malformed frame."
        )
    return gen, sealed


def parse_distribution_root(value: str) -> bytes:
    """Decode+validate a distribution root; raises SenderKeyError on any defect."""
    try:
        raw = base64.b64decode(value.encode("ascii"), validate=True)
    except Exception as exc:
        raise SenderKeyError(
            "Distribution root is not valid base64.", hint="Malformed sender-key frame."
        ) from exc
    if len(raw) != GROUP_SK_ROOT_BYTES:
        raise SenderKeyError(
            f"Distribution root must be {GROUP_SK_ROOT_BYTES} bytes.",
            hint="Malformed sender-key frame.",
        )
    return raw


@dataclass(slots=True)
class Distribution:
    """One sender-key distribution payload (root + current index + gen)."""

    gen: int
    index: int
    root: bytes


def _require_index(index: int) -> None:
    if not isinstance(index, int) or index < 1:
        raise SenderKeyError(
            "Sender-key index must be a positive integer.", hint="Malformed frame."
        )


class OutgoingChain:
    """One sender's outgoing ratchet for (group, epoch, gen)."""

    __slots__ = ("_chain", "_root", "epoch", "gen", "group_id", "next_index")

    def __init__(
        self,
        *,
        group_id: str,
        epoch: int,
        gen: int,
        root: bytes,
        sender: str,
        start_index: int = 1,
    ) -> None:
        self.group_id = group_id
        self.epoch = epoch
        self.gen = gen
        self._root = bytearray(root)
        start = start_index if start_index >= 1 else 1
        # The chain begins at `start_index` so a fresh per-epoch chain stays
        # aligned with the per-group transcript counter (which persists across
        # epochs). Positions 1..start_index-1 were never sealed, so pre-advance
        # the chain through them — this keeps the ratchet in lockstep with a
        # recipient that replays 1..start_index on install (same message keys).
        if start > 1:
            chain = root
            for idx in range(1, start):
                chain = advance_chain(
                    chain,
                    group_id=group_id,
                    epoch=epoch,
                    sender=sender,
                    gen=gen,
                    index=idx,
                )
            root = chain
        self._chain = bytearray(root)
        self.next_index = start

    def seal(
        self,
        *,
        group_id: str,
        epoch: int,
        sender: str,
        index: int,
        plaintext: bytes,
        aad: bytes,
    ) -> bytes:
        """Seal one message with message_key(index) and advance the ratchet.

        ``index`` must equal ``next_index`` (messages seal in order).
        """
        _require_index(index)
        if index != self.next_index:
            raise SenderKeyError(
                f"Sender-key index {index} out of order (next is {self.next_index}).",
                hint="Messages must be sealed in chain order.",
            )
        chain = bytes(self._chain)
        message_key = derive_message_key(
            chain, group_id=group_id, epoch=epoch, sender=sender, gen=self.gen, index=index
        )
        nxt = advance_chain(
            chain, group_id=group_id, epoch=epoch, sender=sender, gen=self.gen, index=index
        )
        self._overwrite_chain(nxt)
        self.next_index = index + 1
        return seal_message(message_key, plaintext, aad)

    def distribution(self, *, index: int | None = None) -> Distribution:
        """Payload to hand a recipient: gen + live index + chain root."""
        target = self.next_index if index is None else index
        return Distribution(gen=self.gen, index=target, root=bytes(self._root))

    def _overwrite_chain(self, nxt: bytes) -> None:
        for pos in range(len(self._chain)):
            self._chain[pos] = nxt[pos]

    def zeroize(self) -> None:
        for buf in (self._chain, self._root):
            for pos in range(len(buf)):
                buf[pos] = 0


class ReceiverChain:
    """One recipient's view of one sender's ratchet for (group, epoch, gen).

    Bounded state: ``_skipped`` (message keys cached for out-of-order
    delivery) holds at most ``GROUP_SK_SKIPPED_MAX`` entries and is pruned
    oldest-first. All other state is O(1) per sender.
    """

    __slots__ = ("_chain", "_skipped", "epoch", "gen", "group_id", "last_seen", "sender")

    def __init__(self, *, group_id: str, sender: str, epoch: int, gen: int) -> None:
        self.group_id = group_id
        self.sender = sender
        self.epoch = epoch
        self.gen = gen
        self.last_seen = 0
        self._chain: bytearray | None = None
        self._skipped: OrderedDict[int, bytes] = OrderedDict()

    def install(self, root: bytes, index: int) -> None:
        """(Re)initialize from a distribution root at the given live index.

        Replays the ratchet 1..index, caching the trailing message keys so
        late/queued messages within the skipped bound can be opened.
        """
        _require_index(index)
        chain = bytearray(root)
        if self._chain is None:
            start = 1
        else:
            if index <= self.last_seen:
                # Distribution is behind what we already hold — ignore it.
                return
            start = self.last_seen + 1
            chain = bytearray(self._chain)
        for k in range(start, index + 1):
            cur = bytes(chain)
            message_key = derive_message_key(
                cur,
                group_id=self.group_id,
                epoch=self.epoch,
                sender=self.sender,
                gen=self.gen,
                index=k,
            )
            self._cache_skipped(k, message_key)
            nxt = advance_chain(
                cur,
                group_id=self.group_id,
                epoch=self.epoch,
                sender=self.sender,
                gen=self.gen,
                index=k,
            )
            chain[:] = bytearray(nxt)
        self.last_seen = index
        if self._chain is None:
            self._chain = chain
        else:
            self._chain[:] = chain

    def message_key_for(self, index: int) -> tuple[bytes, bool]:
        """Resolve the message key for ``index``; returns (key, is_skipped).

        Verdicts via exceptions:
        * ``ReplayError`` — index already accepted (or an unknown replay).
        * ``ExcessiveGapError`` — index is too far ahead of the live chain.
        * ``MissingSenderKeyError`` — no chain state installed yet.
        """
        _require_index(index)
        if self._chain is None:
            raise MissingSenderKeyError(
                "No sender key installed for this sender/epoch/generation.",
                hint="Request a fresh distribution from the sender.",
            )
        if index in self._skipped:
            return self._skipped.pop(index), True
        if index <= self.last_seen:
            raise ReplayError(
                f"Message index {index} ≤ last seen {self.last_seen}.",
                hint="Stale or replayed sender-key message — dropped.",
            )
        gap = index - self.last_seen - 1
        if gap > GROUP_SK_SKIPPED_MAX:
            raise ExcessiveGapError(
                f"Message index {index} jumped ahead by {gap} (> {GROUP_SK_SKIPPED_MAX}).",
                hint="Reject the frame; request a fresh distribution if needed.",
            )
        for k in range(self.last_seen + 1, index):
            cur = bytes(self._chain)
            mk = derive_message_key(
                cur,
                group_id=self.group_id,
                epoch=self.epoch,
                sender=self.sender,
                gen=self.gen,
                index=k,
            )
            self._cache_skipped(k, mk)
            nxt = advance_chain(
                cur,
                group_id=self.group_id,
                epoch=self.epoch,
                sender=self.sender,
                gen=self.gen,
                index=k,
            )
            self._overwrite_chain(nxt)
        cur = bytes(self._chain)
        message_key = derive_message_key(
            cur,
            group_id=self.group_id,
            epoch=self.epoch,
            sender=self.sender,
            gen=self.gen,
            index=index,
        )
        nxt = advance_chain(
            cur,
            group_id=self.group_id,
            epoch=self.epoch,
            sender=self.sender,
            gen=self.gen,
            index=index,
        )
        self._overwrite_chain(nxt)
        self.last_seen = index
        return message_key, False

    def _cache_skipped(self, index: int, message_key: bytes) -> None:
        if index in self._skipped:
            self._skipped.move_to_end(index)
        else:
            self._skipped[index] = message_key
            while len(self._skipped) > GROUP_SK_SKIPPED_MAX:
                self._skipped.popitem(last=False)

    def _overwrite_chain(self, nxt: bytes) -> None:
        assert self._chain is not None
        for pos in range(len(self._chain)):
            self._chain[pos] = nxt[pos]

    def zeroize(self) -> None:
        if self._chain is not None:
            for pos in range(len(self._chain)):
                self._chain[pos] = 0
            self._chain = None
        self._skipped.clear()

    @property
    def skipped_count(self) -> int:
        return len(self._skipped)


class SenderKeyStore:
    """Per-service registry of outgoing + incoming chains (bounded, zeroizable).

    * ``_outgoing`` keyed by ``(group_id, epoch)`` — my own sending chain.
    * ``_incoming`` keyed by ``(group_id, sender_fp)`` — other senders'
      chains, each carrying its own ``epoch``/``gen`` (pruned on epoch leap).
    * ``_generations`` — my current generation per ``(group_id, epoch)``.
    """

    def __init__(self) -> None:
        self._outgoing: dict[tuple[str, int], OutgoingChain] = {}
        self._incoming: dict[tuple[str, str], ReceiverChain] = {}
        self._generations: dict[tuple[str, int], int] = {}
        self._start_index: dict[tuple[str, int], int] = {}

    # ------------------------------------------------------------- outgoing

    def ensure_outgoing(
        self, group_id: str, epoch: int, sender: str, start_index: int | None = None
    ) -> OutgoingChain:
        """My sending chain for (group, epoch), creating one if needed.

        ``start_index`` pins the chain's first message index so a fresh
        per-epoch chain stays aligned with the persistent transcript counter;
        the first value seen for an epoch is kept for consistency.
        """
        key = (group_id, epoch)
        chain = self._outgoing.get(key)
        if chain is None:
            if start_index is None:
                start_index = self._start_index.get(key, 1)
            self._start_index[key] = start_index
            gen = self._generations.get(key, 0) + 1
            self._generations[key] = gen
            chain = OutgoingChain(
                group_id=group_id,
                epoch=epoch,
                gen=gen,
                root=generate_chain_root(),
                sender=sender,
                start_index=start_index,
            )
            self._outgoing[key] = chain
        return chain

    def outgoing_distribution(
        self, group_id: str, epoch: int, sender: str, *, index: int | None = None
    ) -> Distribution:
        return self.ensure_outgoing(group_id, epoch, sender).distribution(index=index)

    def outgoing_chain(self, group_id: str, epoch: int) -> OutgoingChain | None:
        return self._outgoing.get((group_id, epoch))

    # ------------------------------------------------------------- incoming

    def install_incoming(
        self,
        *,
        group_id: str,
        sender: str,
        epoch: int,
        gen: int,
        root: bytes,
        index: int,
    ) -> None:
        """Install/resume a sender's chain from a distribution."""
        if (
            len(self._incoming) >= GROUP_SK_MAX_RECEIVER_STATE
            and (
                group_id,
                sender,
            )
            not in self._incoming
        ):
            raise SenderKeyError(
                "Too many active sender-key chains.", hint="Resource bound exceeded."
            )
        key = (group_id, sender)
        existing = self._incoming.get(key)
        if existing is not None and existing.epoch == epoch and existing.gen == gen:
            existing.install(root, index)
            return
        if existing is not None:
            existing.zeroize()
        receiver = ReceiverChain(group_id=group_id, sender=sender, epoch=epoch, gen=gen)
        receiver.install(root, index)
        self._incoming[key] = receiver

    def message_key_for(
        self, group_id: str, sender: str, epoch: int, gen: int, index: int
    ) -> tuple[bytes, bool]:
        """Resolve a message key, raising typed SenderKeyError on rejection."""
        receiver = self._incoming.get((group_id, sender))
        if receiver is None or receiver.epoch != epoch or receiver.gen != gen:
            raise MissingSenderKeyError(
                "No valid sender key for this epoch/generation.",
                hint="Request a fresh distribution from the sender.",
            )
        return receiver.message_key_for(index)

    def receiver_state(self, group_id: str, sender: str) -> ReceiverChain | None:
        return self._incoming.get((group_id, sender))

    # ------------------------------------------------------------- lifecycle

    def prune_epoch(self, group_id: str, epoch: int) -> None:
        """Drop chains pinned to any epoch other than the current one (§16.5)."""
        for out_key in [k for k in self._outgoing if k[0] == group_id and k[1] != epoch]:
            self._outgoing.pop(out_key).zeroize()
        for in_key, chain in list(self._incoming.items()):
            if in_key[0] == group_id and chain.epoch != epoch:
                self._incoming.pop(in_key).zeroize()

    def drop_sender(self, group_id: str, sender: str) -> None:
        """Drop incoming state for one sender (e.g. a removed member)."""
        chain = self._incoming.pop((group_id, sender), None)
        if chain is not None:
            chain.zeroize()

    def teardown_group(self, group_id: str) -> None:
        for out_key in [k for k in self._outgoing if k[0] == group_id]:
            self._outgoing.pop(out_key).zeroize()
        for in_key in [k for k in self._incoming if k[0] == group_id]:
            self._incoming.pop(in_key).zeroize()
        for gen_key in [k for k in self._generations if k[0] == group_id]:
            del self._generations[gen_key]
        for start_key in [k for k in self._start_index if k[0] == group_id]:
            del self._start_index[start_key]

    def teardown_all(self) -> None:
        for out_chain in self._outgoing.values():
            out_chain.zeroize()
        for in_chain in self._incoming.values():
            in_chain.zeroize()
        self._outgoing.clear()
        self._incoming.clear()
        self._generations.clear()
        self._start_index.clear()

    def outgoing_count(self) -> int:
        return len(self._outgoing)

    def incoming_count(self) -> int:
        return len(self._incoming)


def _fail(message: str, *, hint: str = "") -> NoReturn:
    raise SenderKeyError(message, hint=hint)


__all__ = [
    "Distribution",
    "ExcessiveGapError",
    "MissingSenderKeyError",
    "OutgoingChain",
    "ReceiverChain",
    "ReplayError",
    "SenderKeyError",
    "SenderKeyStore",
    "advance_chain",
    "derive_message_key",
    "distribution_root_b64",
    "generate_chain_root",
    "open_message",
    "parse_distribution_root",
    "parse_sk_control_body",
    "parse_skmsg_body",
    "seal_message",
    "sk_control_body",
    "skmsg_body",
]
