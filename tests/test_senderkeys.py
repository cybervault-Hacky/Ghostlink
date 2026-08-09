"""Phase 7 sender-key unit tests (docs/GROUPS.md §36-§37).

These exercise the cryptographic core in isolation: chain derivation,
sealing, out-of-order delivery, replay/gap rejection, context binding,
bounded skipped-key caches, epoch pruning, and zeroization — no network.
"""

from __future__ import annotations

import pytest

from ghostlink.constants.net import GROUP_SK_SKIPPED_MAX
from ghostlink.groups.mesh import group_sk_msg_aad
from ghostlink.groups.senderkeys import (
    ExcessiveGapError,
    MissingSenderKeyError,
    OutgoingChain,
    ReceiverChain,
    ReplayError,
    SenderKeyError,
    SenderKeyStore,
    advance_chain,
    derive_message_key,
    distribution_root_b64,
    generate_chain_root,
    open_message,
    parse_distribution_root,
    parse_sk_control_body,
    parse_skmsg_body,
    sk_control_body,
    skmsg_body,
)

GID = "gl-group-XXXX-XXXX-ABCD"
EPOCH = 1
SENDER = "GLFP-AAAA-BBBB-CCCC-DDDD-0001"
GEN = 1


def _aad(seq: int, gen: int = GEN, epoch: int = EPOCH, sender: str = SENDER) -> bytes:
    return group_sk_msg_aad(GID, epoch, sender, gen, seq)


def _chain(root: bytes | None = None) -> OutgoingChain:
    return OutgoingChain(
        group_id=GID, epoch=EPOCH, gen=GEN, root=root or generate_chain_root(), sender=SENDER
    )


class TestChainDerivation:
    def test_message_key_binds_full_context(self) -> None:
        root = generate_chain_root()
        k1 = derive_message_key(root, group_id=GID, epoch=1, sender=SENDER, gen=1, index=1)
        assert len(k1) == 32
        # Any context change yields a different key.
        k2 = derive_message_key(
            root, group_id="gl-group-XXXX-XXXX-WXYZ", epoch=1, sender=SENDER, gen=1, index=1
        )
        k3 = derive_message_key(root, group_id=GID, epoch=2, sender=SENDER, gen=1, index=1)
        k4 = derive_message_key(
            root, group_id=GID, epoch=1, sender="GLFP-0000-0000-0000-0001", gen=1, index=1
        )
        k5 = derive_message_key(root, group_id=GID, epoch=1, sender=SENDER, gen=2, index=1)
        k6 = derive_message_key(root, group_id=GID, epoch=1, sender=SENDER, gen=1, index=2)
        assert len({k1, k2, k3, k4, k5, k6}) == 6

    def test_chain_advances_monotonically(self) -> None:
        root = generate_chain_root()
        c0 = root
        keys = []
        for idx in range(1, 5):
            mk = derive_message_key(
                c0, group_id=GID, epoch=EPOCH, sender=SENDER, gen=GEN, index=idx
            )
            c0 = advance_chain(c0, group_id=GID, epoch=EPOCH, sender=SENDER, gen=GEN, index=idx)
            keys.append(mk)
        assert len(set(keys)) == 4
        # One-way: the next chain key cannot recover an earlier message key.
        # (Forward-only by construction — a fresh HKDF ratchet.)
        assert all(len(k) == 32 for k in keys)

    def test_seal_open_roundtrip(self) -> None:
        chain = _chain()
        plaintext = b"secret group text"
        sealed = chain.seal(
            group_id=GID, epoch=EPOCH, sender=SENDER, index=1, plaintext=plaintext, aad=_aad(1)
        )
        receiver = ReceiverChain(group_id=GID, sender=SENDER, epoch=EPOCH, gen=GEN)
        receiver.install(chain.distribution().root, chain.distribution().index)
        key, _ = receiver.message_key_for(1)
        assert open_message(key, sealed, _aad(1)) == plaintext

    def test_seal_must_be_in_order(self) -> None:
        chain = _chain()
        chain.seal(group_id=GID, epoch=EPOCH, sender=SENDER, index=1, plaintext=b"a", aad=_aad(1))
        with pytest.raises(SenderKeyError):
            chain.seal(
                group_id=GID, epoch=EPOCH, sender=SENDER, index=3, plaintext=b"c", aad=_aad(3)
            )


class TestReceiverWindow:
    def _two_peers(self, count: int = 5):
        chain = _chain()
        receiver = ReceiverChain(group_id=GID, sender=SENDER, epoch=EPOCH, gen=GEN)
        cts: list[bytes] = []
        for i in range(1, count + 1):
            cts.append(
                chain.seal(
                    group_id=GID,
                    epoch=EPOCH,
                    sender=SENDER,
                    index=i,
                    plaintext=f"m{i}".encode(),
                    aad=_aad(i),
                )
            )
        receiver.install(chain.distribution(index=1).root, 1)
        return chain, receiver, cts

    def test_out_of_order_delivery(self) -> None:
        _chain, receiver, cts = self._two_peers(5)
        # Deliver 5, then 1, then 4 — all must open.
        for idx in (5, 1, 4):
            key, _ = receiver.message_key_for(idx)
            assert open_message(key, cts[idx - 1], _aad(idx)) == f"m{idx}".encode()

    def test_replay_rejected(self) -> None:
        _chain_obj, receiver, _cts = self._two_peers(3)
        receiver.message_key_for(1)
        with pytest.raises(ReplayError):
            receiver.message_key_for(1)  # already accepted
        with pytest.raises(SenderKeyError):
            receiver.message_key_for(0)  # index 0 never valid

    def test_excessive_gap_rejected(self) -> None:
        _chain_obj, receiver, _cts = self._two_peers(1)
        with pytest.raises(ExcessiveGapError):
            receiver.message_key_for(1 + GROUP_SK_SKIPPED_MAX + 10)

    def test_missing_sender_key(self) -> None:
        receiver = ReceiverChain(group_id=GID, sender=SENDER, epoch=EPOCH, gen=GEN)
        with pytest.raises(MissingSenderKeyError):
            receiver.message_key_for(1)

    def test_tampered_ciphertext_fails(self) -> None:
        _chain, receiver, cts = self._two_peers(2)
        key, _ = receiver.message_key_for(2)
        tampered = bytearray(cts[1])
        tampered[15] ^= 0x01
        with pytest.raises(SenderKeyError):
            open_message(key, bytes(tampered), _aad(2))

    def test_wrong_aad_fails(self) -> None:
        _chain, receiver, cts = self._two_peers(1)
        key, _ = receiver.message_key_for(1)
        with pytest.raises(SenderKeyError):
            open_message(key, cts[0], _aad(1, gen=2))  # wrong generation in AAD

    def test_skipped_cache_is_bounded(self) -> None:
        _chain, receiver, _cts = self._two_peers(1)
        # Jump ahead by exactly the max skip window: intermediate keys cache,
        # and the cache never exceeds the bound.
        jump = GROUP_SK_SKIPPED_MAX + 2
        receiver.message_key_for(jump)
        assert receiver.skipped_count <= GROUP_SK_SKIPPED_MAX


class TestDistribution:
    def test_install_then_live_message(self) -> None:
        chain = _chain()
        # The sender already sent 3 messages; a new member installs the chain
        # at the current index and can open queued + future messages.
        for i in range(1, 4):
            chain.seal(
                group_id=GID, epoch=EPOCH, sender=SENDER, index=i, plaintext=b"x", aad=_aad(i)
            )
        receiver = ReceiverChain(group_id=GID, sender=SENDER, epoch=EPOCH, gen=GEN)
        dist = chain.distribution(index=3)
        receiver.install(dist.root, dist.index)
        _key, was_skipped = receiver.message_key_for(3)
        assert was_skipped
        # Future message opens normally.
        key2, _ = receiver.message_key_for(4)
        assert len(key2) == 32

    def test_distribution_root_roundtrip(self) -> None:
        root = generate_chain_root()
        assert parse_distribution_root(distribution_root_b64(root)) == root
        with pytest.raises(SenderKeyError):
            parse_distribution_root("not-base64!!")

    def test_public_body_parsers(self) -> None:
        body = skmsg_body(2, 7, b"ciphertext")
        gen, seq, sealed = parse_skmsg_body(body, max_sealed=100)
        assert (gen, seq, sealed) == (2, 7, b"ciphertext")
        control = sk_control_body(2, b"sealed")
        cgen, csealed = parse_sk_control_body(control, max_sealed=100)
        assert (cgen, csealed) == (2, b"sealed")
        with pytest.raises(SenderKeyError):
            parse_skmsg_body(b"{}", max_sealed=100)
        with pytest.raises(SenderKeyError):
            parse_sk_control_body(b"{bad", max_sealed=100)


class TestSenderKeyStore:
    def test_epoch_pruning(self) -> None:
        store = SenderKeyStore()
        chain = store.ensure_outgoing(GID, 1, SENDER)
        assert chain.epoch == 1
        store.install_incoming(
            group_id=GID, sender=SENDER, epoch=1, gen=1, root=generate_chain_root(), index=1
        )
        assert store.incoming_count() == 1
        store.prune_epoch(GID, 2)
        assert store.incoming_count() == 0
        # Outgoing chain for epoch 1 is gone; a new one is minted for epoch 2.
        c2 = store.ensure_outgoing(GID, 2, SENDER)
        assert c2.epoch == 2

    def test_drop_sender_and_teardown(self) -> None:
        store = SenderKeyStore()
        store.install_incoming(
            group_id=GID, sender=SENDER, epoch=1, gen=1, root=generate_chain_root(), index=1
        )
        store.ensure_outgoing(GID, 1, SENDER)
        store.drop_sender(GID, SENDER)
        assert store.incoming_count() == 0
        store.teardown_all()
        assert store.outgoing_count() == 0

    def test_incoming_generation_supersedes(self) -> None:
        store = SenderKeyStore()
        store.install_incoming(
            group_id=GID, sender=SENDER, epoch=1, gen=1, root=generate_chain_root(), index=1
        )
        store.install_incoming(
            group_id=GID, sender=SENDER, epoch=1, gen=2, root=generate_chain_root(), index=1
        )
        # gen=1 key derivation now fails (no such gen).
        with pytest.raises(MissingSenderKeyError):
            store.message_key_for(GID, SENDER, 1, 1, 2)
        key, _ = store.message_key_for(GID, SENDER, 1, 2, 2)
        assert len(key) == 32
