"""Pairwise-mesh link manager (Phase 6C — docs/GROUPS.md §17, §26).

One Phase 3 session per ordered pair (group, epoch, self↔peer), built
with the *verbatim* Phase 3 handshake (``HandshakeInitiator`` /
``HandshakeResponder``) — no new cryptographic primitive anywhere:

* the handshake's channel id is the group/epoch context string
  ``ghostlink/group/v1|<group_id>|<epoch>``, so the HKDF transcript (and
  thus the key) is bound to exactly that group and epoch (§16.3,
  §17.2.2); the KEX wire format is unchanged;
* ``idpub`` (Ed25519 identity key) is **mandatory** in both directions
  (§17.2.1) — links that do not bind both identity keys are refused;
* the initiator is deterministic: the member with the lexicographically
  smaller identity-public-key hex initiates (§17.2.3);
* message AAD is exactly ``ghostlink/group-msg/v1|group|epoch|from|to``
  (§20.2) — cross-context opens fail the AEAD tag;
* epoch leaps move live links to a 30 s receive-only drain (§16.5),
  then zeroize them; teardown paths zeroize key bytes before references
  drop (§26.4).

Keys live in ``bytearray`` slots owned here so teardowns actually
overwrite memory. Nothing in this module touches disk or logs secrets.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

from ghostlink.constants.net import (
    GROUP_AEAD_FAILURES_NOTICE,
    GROUP_EPOCH_DRAIN_SECONDS,
    GROUP_KEX_TIMEOUT_SECONDS,
    MAX_GROUP_MEMBERS,
)
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.groups import GroupMessageError
from ghostlink.exceptions.messaging import HandshakeFailedError
from ghostlink.messaging.protocol.crypto import open_sealed, seal
from ghostlink.messaging.protocol.handshake import (
    HandshakeInitiator,
    HandshakeResponder,
)

_logger = get_logger("groups.mesh")

_MAX_LINKS = MAX_GROUP_MEMBERS - 1  # live per group; drain/registry is bounded alike
_STRAY_KEX_EVICTION = 256  # bound for kex traffic from unknown senders


def group_link_context(group_id: str, epoch: int) -> str:
    """The exact §16.3/§17.2 handshake binding context."""

    return f"ghostlink/group/v1|{group_id}|{epoch}"


def group_message_aad(group_id: str, epoch: int, sender: str, recipient: str) -> bytes:
    """The exact §20.2 message AAD (context bound into every ciphertext)."""

    return f"ghostlink/group-msg/v1|{group_id}|{epoch}|{sender}|{recipient}".encode()


def group_sk_key_aad(group_id: str, epoch: int, sender: str, recipient: str, gen: int) -> bytes:
    """AAD for a GSK distribution frame (sealed over a pairwise link).

    Binds group, epoch, sender, recipient and generation so a distribution
    minted for one pair/context can never be opened elsewhere (§37).
    """

    return f"ghostlink/group-sk-key/v1|{group_id}|{epoch}|{sender}|{recipient}|{gen}".encode()


def group_sk_msg_aad(group_id: str, epoch: int, sender: str, gen: int, seq: int) -> bytes:
    """AAD for a sender-key-sealed GMSG (the *same* for every recipient).

    Deliberately excludes the recipient: one ciphertext is broadcast to the
    whole roster, so every recipient must open it with the identical AAD.
    Cross-recipient reuse is safe because the inner frame re-carries the
    context and the sender key itself is epoch/sender/generation-bound.
    """

    return f"ghostlink/group-sk-msg/v1|{group_id}|{epoch}|{sender}|{gen}|{seq}".encode()


class PairwiseLink:
    """One live or draining pairwise link (key in a zeroizable slot)."""

    __slots__ = (
        "_key",
        "aead_failures",
        "created_mono",
        "epoch",
        "group_id",
        "initiator",
        "peer_fingerprint",
        "peer_pubkey_hex",
    )

    def __init__(
        self,
        *,
        group_id: str,
        epoch: int,
        peer_fingerprint: str,
        peer_pubkey_hex: str,
        session_key: bytes,
        initiator: bool,
        created_mono: float,
    ) -> None:
        self.group_id = group_id
        self.epoch = epoch
        self.peer_fingerprint = peer_fingerprint
        self.peer_pubkey_hex = peer_pubkey_hex
        self._key = bytearray(session_key)
        self.initiator = initiator
        self.created_mono = created_mono
        self.aead_failures = 0

    def seal(self, plaintext: bytes, aad: bytes) -> bytes:
        return seal(bytes(self._key), plaintext, aad)

    def open(self, sealed: bytes, aad: bytes) -> bytes:
        return open_sealed(bytes(self._key), sealed, aad)

    def zeroize(self) -> None:
        for index in range(len(self._key)):
            self._key[index] = 0


class _PendingHandshake:
    __slots__ = ("deadline_mono", "epoch", "hello", "peer_fingerprint", "peer_pubkey_hex")

    def __init__(
        self,
        *,
        hello: HandshakeInitiator,
        peer_fingerprint: str,
        peer_pubkey_hex: str,
        epoch: int,
        deadline_mono: float,
    ) -> None:
        self.hello = hello
        self.peer_fingerprint = peer_fingerprint
        self.peer_pubkey_hex = peer_pubkey_hex
        self.epoch = epoch
        self.deadline_mono = deadline_mono


class GroupMeshManager:
    """Owns every pairwise link for this client (bounded, zeroizable)."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        drain_seconds: float = GROUP_EPOCH_DRAIN_SECONDS,
        kex_timeout_seconds: float = GROUP_KEX_TIMEOUT_SECONDS,
    ) -> None:
        self._monotonic = monotonic
        self._drain_seconds = drain_seconds
        self._kex_timeout = kex_timeout_seconds
        self._links: dict[tuple[str, str], PairwiseLink] = {}
        self._draining: dict[tuple[str, str], tuple[PairwiseLink, float]] = {}
        self._pending: dict[tuple[str, str], _PendingHandshake] = {}
        self._group_epochs: dict[str, int] = {}

    # ------------------------------------------------------------- roles

    @staticmethod
    def initiates(own_pubkey_hex: str, peer_pubkey_hex: str) -> bool:
        """Deterministic initiator (§17.2.3): smaller pubkey hex starts."""

        return own_pubkey_hex.lower() < peer_pubkey_hex.lower()

    # ----------------------------------------------------- epoch tracking

    def observed_epoch(self, group_id: str) -> int | None:
        return self._group_epochs.get(group_id)

    def observe_epoch(self, group_id: str, epoch: int) -> list[tuple[str, str]]:
        """Possibly-leaping epoch observation.

        Every live link pinned to a *strictly older* epoch starts draining
        (``GROUP_EPOCH_DRAIN_SECONDS``, §16.5) — including on the *first*
        observation of a group that already holds links (the leap caught up
        late, so a drain deadline must be armed then too); pending
        handshakes pinning old epochs drop. Links pinned *ahead* of the
        observed epoch are left untouched: the observed clock never
        regresses, so an out-of-order or replayed observation cannot kill
        keys minted for the true current epoch (§16.1).

        Returns the ``(group, peer)`` keys whose pending handshakes were
        dropped by the leap (their context pinned an older epoch).
        """

        current = self._group_epochs.get(group_id)
        if current is not None and epoch <= current:
            return []  # epochs never regress (§16.1)
        self._group_epochs[group_id] = epoch
        deadline = self._monotonic() + self._drain_seconds
        dropped: list[tuple[str, str]] = []
        moved = 0
        for key, link in list(self._links.items()):
            if key[0] == group_id and link.epoch < epoch:
                self._draining[key] = (link, deadline)
                del self._links[key]
                moved += 1
        for key in list(self._pending):
            if key[0] == group_id and self._pending[key].epoch < epoch:
                del self._pending[key]
                dropped.append(key)
        if moved or dropped:
            _logger.info(
                "group %s epoch leap %s → %d — %d link(s) draining for %.0fs, "
                "%d pending handshake(s) dropped",
                group_id,
                current if current is not None else "?",
                epoch,
                moved,
                self._drain_seconds,
                len(dropped),
            )
        return dropped

    # --------------------------------------------------------- handshakes

    def hello_for(
        self,
        *,
        group_id: str,
        epoch: int,
        peer_fingerprint: str,
        peer_pubkey_hex: str,
        own_pubkey_hex: str,
        force: bool = False,
    ) -> dict[str, str] | None:
        """KEX hello payload to send the peer — None if already pending.

        Only the deterministic initiator (smaller identity-key hex, §17.2.3)
        may ever start a handshake; callers must send a knock instead when
        they hold the larger key (the service enforces that flow). ``force``
        re-pairs even when a link is already live at this epoch: a peer
        asking for pairing (knock, §27.2) has lost its matching key, and
        the verification below binds the fresh hello to its roster identity.
        """

        if not self.initiates(own_pubkey_hex, peer_pubkey_hex):
            raise GroupMessageError(
                "Only the smaller identity key may initiate a group link.",
                hint="Deterministic initiator selection keeps one link per pair.",
            )
        key = (group_id, peer_fingerprint)
        existing = self._pending.get(key)
        if existing is not None and self._monotonic() < existing.deadline_mono:
            return None  # one outstanding handshake per pair (§17.3 bound)
        link = self._links.get(key)
        if link is not None and link.epoch == epoch:
            if not force:
                return None  # already live; nothing to do
            self._drop_link(key)  # explicit re-pair: old key dies with the session
        self._require_capacity(group_id)
        hello = HandshakeInitiator(
            group_link_context(group_id, epoch),
            identity_public_key_hex=own_pubkey_hex,
        )
        self._pending[key] = _PendingHandshake(
            hello=hello,
            peer_fingerprint=peer_fingerprint,
            peer_pubkey_hex=peer_pubkey_hex.lower(),
            epoch=epoch,
            deadline_mono=self._monotonic() + self._kex_timeout,
        )
        return hello.hello_payload()

    def handle_hello(
        self,
        *,
        group_id: str,
        epoch: int,
        peer_fingerprint: str,
        peer_pubkey_hex: str,
        own_pubkey_hex: str,
        hello_payload: dict[str, Any],
    ) -> dict[str, str]:
        """Answer one KEX hello; installs the completed responder link."""

        if self.initiates(own_pubkey_hex, peer_pubkey_hex):
            raise HandshakeFailedError(
                "Handshake role violation — the smaller key must initiate.",
                hint="A peer violating deterministic roles is not to be trusted.",
            )
        presented = hello_payload.get("idpub")
        if not isinstance(presented, str) or presented.lower() != peer_pubkey_hex.lower():
            raise HandshakeFailedError(
                "Handshake identity key does not match the roster identity.",
                hint="A relay or peer substituted identity keys — link refused.",
            )
        self._require_capacity(group_id)
        key = (group_id, peer_fingerprint)
        self._drop_link(key)  # a fresh hello supersedes any old link/pending
        responder = HandshakeResponder(
            group_link_context(group_id, epoch),
            identity_public_key_hex=own_pubkey_hex,
        )
        reply_payload, session = responder.answer(hello_payload)
        if not session.identity_bound:
            raise HandshakeFailedError(  # pragma: no cover - idpub check above guards
                "Group links require identity keys in both directions.",
                hint="Ask the peer to upgrade — group links bind identity keys.",
            )
        self._install_link(
            key,
            session.session_key,
            epoch,
            peer_fingerprint,
            peer_pubkey_hex,
            initiator=False,
        )
        return reply_payload

    def handle_reply(
        self,
        *,
        group_id: str,
        epoch: int,
        peer_fingerprint: str,
        reply_payload: dict[str, Any],
    ) -> PairwiseLink:
        """Complete a pending initiator handshake; returns the live link."""

        key = (group_id, peer_fingerprint)
        pending = self._pending.get(key)
        if pending is None:
            raise HandshakeFailedError(
                "Handshake reply without a pending hello.",
                hint="Stale or spoofed handshake traffic is dropped.",
            )
        if pending.epoch != epoch:
            raise HandshakeFailedError(
                "Handshake reply pins a different epoch.",
                hint="A peer or relay re-labeling epochs is not to be trusted.",
            )
        presented = reply_payload.get("idpub")
        if not isinstance(presented, str) or presented.lower() != pending.peer_pubkey_hex:
            raise HandshakeFailedError(
                "Handshake identity key does not match the roster identity.",
                hint="A relay or peer substituted identity keys — link refused.",
            )
        try:
            session = pending.hello.complete(reply_payload)
        finally:
            self._pending.pop(key, None)
        if not session.identity_bound:
            raise HandshakeFailedError(
                "Group links require identity keys in both directions.",
                hint="Ask the peer to upgrade — group links bind identity keys.",
            )
        return self._install_link(
            key,
            session.session_key,
            epoch,
            peer_fingerprint,
            pending.peer_pubkey_hex,
            initiator=True,
        )

    def pending_handshake_count(self) -> int:
        self._sweep_pending()
        return len(self._pending)

    def _install_link(
        self,
        key: tuple[str, str],
        session_key: bytes,
        epoch: int,
        peer_fingerprint: str,
        peer_pubkey_hex: str,
        *,
        initiator: bool,
    ) -> PairwiseLink:
        self._drop_link(key)
        link = PairwiseLink(
            group_id=key[0],
            epoch=epoch,
            peer_fingerprint=peer_fingerprint,
            peer_pubkey_hex=peer_pubkey_hex.lower(),
            session_key=session_key,
            initiator=initiator,
            created_mono=self._monotonic(),
        )
        self._links[key] = link
        _logger.info(
            "mesh link up — %s ↔ %s epoch %d (%s)",
            key[0],
            peer_fingerprint,
            epoch,
            "initiator" if initiator else "responder",
        )
        return link

    # ------------------------------------------------------------- crypto

    def link_for(self, group_id: str, peer_fingerprint: str, epoch: int) -> PairwiseLink | None:
        link = self._links.get((group_id, peer_fingerprint))
        if link is not None and link.epoch == epoch:
            return link
        return None

    def is_handshake_pending(self, group_id: str, peer_fingerprint: str) -> bool:
        self._sweep_pending()
        return (group_id, peer_fingerprint) in self._pending

    def accepting_link(self, group_id: str, epoch: int, sender: str) -> PairwiseLink | None:
        """The link allowed to carry a frame of ``epoch`` — the §16.5
        closed form, exactly:

        accept iff ``frame.epoch == current`` (live link) OR
        (``frame.epoch == current - 1`` AND ``now < leap + DRAIN`` AND the
        old-epoch link is still alive, i.e. draining). Anything older,
        anything newer, and anything past the deadline: no link.
        """

        current = self._group_epochs.get(group_id)
        if current is not None and (epoch > current or epoch < current - 1):
            return None
        link = self._links.get((group_id, sender))
        if link is not None and link.epoch != epoch:
            link = None  # current link pinned to another epoch — not it
        if link is None:
            draining = self._draining.get((group_id, sender))
            if (
                draining is not None
                and draining[0].epoch == epoch
                and self._monotonic() < draining[1]
            ):
                link = draining[0]
        return link

    def open_inbound(
        self,
        *,
        group_id: str,
        epoch: int,
        sender: str,
        recipient: str,
        sealed: bytes,
    ) -> bytes:
        """Open an inbound sealed frame applying the §16.5 epoch rule.

        Accepts iff the frame pins the current-epoch live link, or the
        previous-epoch draining link whose 30 s window is still open.
        Raises :class:`DecryptionError` (tag failure — counted) or
        :class:`GroupMessageError` (no acceptable link).
        """

        link = self.accepting_link(group_id, epoch, sender)
        if link is None:
            raise GroupMessageError(
                f"No pairwise link accepts epoch {epoch} from that member.",
                hint="The frame pins an epoch with no live/draining link — reject.",
            )
        aad = group_message_aad(group_id, epoch, sender, recipient)
        try:
            return link.open(sealed, aad)
        except Exception:
            link.aead_failures += 1
            if link.aead_failures == GROUP_AEAD_FAILURES_NOTICE:
                _logger.info(
                    "mesh link %s ↔ %s marked suspect (%d AEAD failures)",
                    group_id,
                    sender,
                    link.aead_failures,
                )
            raise

    def link_suspect(self, group_id: str, peer_fingerprint: str) -> bool:
        link = (
            self._links.get((group_id, peer_fingerprint))
            or self._draining.get((group_id, peer_fingerprint), (None, 0.0))[0]
        )
        return link is not None and link.aead_failures >= GROUP_AEAD_FAILURES_NOTICE

    # ----------------------------------------------------------- lifecycle

    def teardown_link(self, group_id: str, peer_fingerprint: str) -> bool:
        """Zeroize every link/pending state for one peer (§26.4)."""

        key = (group_id, peer_fingerprint)
        dropped = self._drop_link(key)
        if dropped:
            _logger.info("mesh link %s ↔ %s torn down and zeroized", group_id, peer_fingerprint)
        return dropped

    def teardown_group(self, group_id: str) -> int:
        count = 0
        for key in [k for k in self._links if k[0] == group_id]:
            count += int(self._drop_link(key))
        for key in [k for k in self._draining if k[0] == group_id]:
            link, _ = self._draining.pop(key)
            link.zeroize()
            count += 1
        for key in [k for k in self._pending if k[0] == group_id]:
            del self._pending[key]
            count += 1
        self._group_epochs.pop(group_id, None)
        return count

    def teardown_all(self) -> int:
        count = 0
        for key in list(self._links):
            count += int(self._drop_link(key))
        for key, (link, _deadline) in list(self._draining.items()):
            link.zeroize()
            del self._draining[key]
            count += 1
        count += len(self._pending)
        self._pending.clear()
        self._group_epochs.clear()
        return count

    def expire(self) -> list[tuple[str, str]]:
        """Zeroize drain-expired links and time out dead pending handshakes."""

        now = self._monotonic()
        expired: list[tuple[str, str]] = []
        for key, (link, deadline) in list(self._draining.items()):
            if now >= deadline:
                link.zeroize()
                del self._draining[key]
                expired.append(key)
                _logger.info("drain window closed — %s ↔ %s link zeroized", key[0], key[1])
        for key, pending in list(self._pending.items()):
            if now >= pending.deadline_mono:
                del self._pending[key]
                expired.append(key)
        return expired

    def link_count(self) -> int:
        return len(self._links)

    def _drop_link(self, key: tuple[str, str]) -> bool:
        dropped = False
        link = self._links.pop(key, None)
        if link is not None:
            link.zeroize()
            dropped = True
        drained = self._draining.pop(key, None)
        if drained is not None:
            drained[0].zeroize()
            dropped = True
        dropped |= self._pending.pop(key, None) is not None
        return dropped

    def _sweep_pending(self) -> None:
        now = self._monotonic()
        for key, pending in list(self._pending.items()):
            if now >= pending.deadline_mono:
                del self._pending[key]

    def _require_capacity(self, group_id: str) -> None:
        live = sum(1 for key in self._links if key[0] == group_id)
        if live >= _MAX_LINKS:
            raise GroupMessageError(
                f"The mesh already holds the maximum {_MAX_LINKS} links for this group.",
                hint="Link capacity is bounded at members-1 by design.",
            )
        # Stranger-KEX flood guard across all groups.
        if len(self._links) + len(self._pending) > _STRAY_KEX_EVICTION:
            self._sweep_pending()
            if len(self._links) + len(self._pending) > _STRAY_KEX_EVICTION:
                raise GroupMessageError(
                    "Too many outstanding mesh handshakes.",
                    hint="KEX flood guard tripped — try again shortly.",
                )


__all__ = [
    "GroupMeshManager",
    "PairwiseLink",
    "group_link_context",
    "group_message_aad",
    "group_sk_key_aad",
    "group_sk_msg_aad",
]
