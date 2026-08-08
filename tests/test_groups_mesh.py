"""Unit tests for the pairwise mesh manager (Phase 6C — §16.3/§17/§26).

No networking: handshakes are driven in-memory through the mesh manager;
the relay interaction is covered by the e2e suite. Clocks are injected so
the §16.5 drain boundary is exact.
"""

from __future__ import annotations

import pytest

from ghostlink.constants.net import (
    GROUP_AEAD_FAILURES_NOTICE,
    GROUP_EPOCH_DRAIN_SECONDS,
    MAX_GROUP_MEMBERS,
)
from ghostlink.exceptions.groups import GroupMessageError
from ghostlink.exceptions.messaging import DecryptionError, HandshakeFailedError
from ghostlink.groups.events import fingerprint_for_key_hex
from ghostlink.groups.mesh import (
    GroupMeshManager,
    group_link_context,
    group_message_aad,
)
from ghostlink.identity.identity import LocalIdentity

GROUP = "gl-group-AAAA-BBBB-CCCC"
GROUP2 = "gl-group-DDDD-EEEE-FFFF"


def _identity() -> LocalIdentity:
    return LocalIdentity.generate()


def _fp(identity: LocalIdentity) -> str:
    return fingerprint_for_key_hex(identity.public_key_hex)


class _Pair:
    """Two mesh managers (self↔peer) with helper to complete a handshake."""

    def __init__(self) -> None:
        self.clock = [1000.0]
        self.a_id, self.b_id = _identity(), _identity()
        self.a_fp, self.b_fp = _fp(self.a_id), _fp(self.b_id)
        self.a = GroupMeshManager(monotonic=lambda: self.clock[0])
        self.b = GroupMeshManager(monotonic=lambda: self.clock[0])
        # deterministic roles from the real keys
        self.initiator_is_a = GroupMeshManager.initiates(
            self.a_id.public_key_hex, self.b_id.public_key_hex
        )

    def link(self, epoch: int = 1) -> None:
        """Drive one full hello/reply handshake in initiator order."""

        if self.initiator_is_a:
            hello = self.a.hello_for(
                group_id=GROUP,
                epoch=epoch,
                peer_fingerprint=self.b_fp,
                peer_pubkey_hex=self.b_id.public_key_hex,
                own_pubkey_hex=self.a_id.public_key_hex,
            )
            assert hello is not None
            reply = self.b.handle_hello(
                group_id=GROUP,
                epoch=epoch,
                peer_fingerprint=self.a_fp,
                peer_pubkey_hex=self.a_id.public_key_hex,
                own_pubkey_hex=self.b_id.public_key_hex,
                hello_payload=hello,
            )
            self.a.handle_reply(
                group_id=GROUP, epoch=epoch, peer_fingerprint=self.b_fp, reply_payload=reply
            )
        else:
            hello = self.b.hello_for(
                group_id=GROUP,
                epoch=epoch,
                peer_fingerprint=self.a_fp,
                peer_pubkey_hex=self.a_id.public_key_hex,
                own_pubkey_hex=self.b_id.public_key_hex,
            )
            assert hello is not None
            reply = self.a.handle_hello(
                group_id=GROUP,
                epoch=epoch,
                peer_fingerprint=self.b_fp,
                peer_pubkey_hex=self.b_id.public_key_hex,
                own_pubkey_hex=self.a_id.public_key_hex,
                hello_payload=hello,
            )
            self.b.handle_reply(
                group_id=GROUP, epoch=epoch, peer_fingerprint=self.a_fp, reply_payload=reply
            )

    def seal_a_to_b(self, epoch: int, plaintext: bytes) -> bytes:
        link = self.a.link_for(GROUP, self.b_fp, epoch)
        assert link is not None
        return link.seal(plaintext, group_message_aad(GROUP, epoch, self.a_fp, self.b_fp))


class TestContextStrings:
    def test_link_context_exact(self) -> None:
        assert group_link_context(GROUP, 3) == f"ghostlink/group/v1|{GROUP}|3"

    def test_message_aad_exact(self) -> None:
        expected = f"ghostlink/group-msg/v1|{GROUP}|2|{FP_A}|{FP_B}".encode()
        assert group_message_aad(GROUP, 2, FP_A, FP_B) == expected


FP_A = "GLFP-1111-1111-1111"
FP_B = "GLFP-2222-2222-2222"


class TestHandshake:
    def test_deterministic_initiator(self) -> None:
        assert GroupMeshManager.initiates("0aaa", "0bbb") is True
        assert GroupMeshManager.initiates("0bbb", "0aaa") is False

    def test_hello_only_for_initiator(self) -> None:
        pair = _Pair()
        responder_mesh, responder_id, peer_fp, peer_key = (
            (pair.b, pair.b_id, pair.a_fp, pair.a_id.public_key_hex)
            if pair.initiator_is_a
            else (pair.a, pair.a_id, pair.b_fp, pair.b_id.public_key_hex)
        )
        with pytest.raises(GroupMessageError):
            responder_mesh.hello_for(
                group_id=GROUP,
                epoch=1,
                peer_fingerprint=peer_fp,
                peer_pubkey_hex=peer_key,
                own_pubkey_hex=responder_id.public_key_hex,
            )

    def test_reply_without_pending_refused(self) -> None:
        pair = _Pair()
        with pytest.raises(HandshakeFailedError):
            pair.a.handle_reply(
                group_id=GROUP,
                epoch=1,
                peer_fingerprint=pair.b_fp,
                reply_payload={},
            )

    def test_wrong_idpub_refused(self) -> None:
        """Relay/peer idpub substitution must break the link (§17.2.1)."""

        pair = _Pair()
        initiator_mesh, initiator_id = (
            (pair.a, pair.a_id) if pair.initiator_is_a else (pair.b, pair.b_id)
        )
        other_mesh, other_id = (pair.b, pair.b_id) if pair.initiator_is_a else (pair.a, pair.a_id)
        other_fp = pair.b_fp if pair.initiator_is_a else pair.a_fp
        evil = _identity()
        hello = initiator_mesh.hello_for(
            group_id=GROUP,
            epoch=1,
            peer_fingerprint=other_fp,
            peer_pubkey_hex=other_id.public_key_hex,
            own_pubkey_hex=initiator_id.public_key_hex,
        )
        assert hello is not None
        # the roster claims the peer is `other_id`; substitute hello idpub
        substituted = dict(hello)
        substituted["idpub"] = evil.public_key_hex
        with pytest.raises(HandshakeFailedError):
            other_mesh.handle_hello(
                group_id=GROUP,
                epoch=1,
                peer_fingerprint=pair.a_fp if pair.initiator_is_a else pair.b_fp,
                peer_pubkey_hex=initiator_id.public_key_hex,
                own_pubkey_hex=other_id.public_key_hex,
                hello_payload=substituted,
            )

    def test_wrong_role_hello_refused(self) -> None:
        """A hello arriving at the initiator-side violates roles (§17.2.3)."""

        pair = _Pair()
        if not pair.initiator_is_a:
            with pytest.raises(HandshakeFailedError):
                pair.a.handle_hello(
                    group_id=GROUP,
                    epoch=1,
                    peer_fingerprint=pair.b_fp,
                    peer_pubkey_hex=pair.b_id.public_key_hex,
                    own_pubkey_hex=pair.a_id.public_key_hex,
                    hello_payload={
                        "pub": "00" * 32,
                        "nonce": "00" * 16,
                        "idpub": pair.b_id.public_key_hex,
                    },
                )

    def test_one_pending_per_pair(self) -> None:
        pair = _Pair()
        initiator_mesh, initiator_id = (
            (pair.a, pair.a_id) if pair.initiator_is_a else (pair.b, pair.b_id)
        )
        other_fp = pair.b_fp if pair.initiator_is_a else pair.a_fp
        other_key = pair.b_id.public_key_hex if pair.initiator_is_a else pair.a_id.public_key_hex
        first = initiator_mesh.hello_for(
            group_id=GROUP,
            epoch=1,
            peer_fingerprint=other_fp,
            peer_pubkey_hex=other_key,
            own_pubkey_hex=initiator_id.public_key_hex,
        )
        second = initiator_mesh.hello_for(
            group_id=GROUP,
            epoch=1,
            peer_fingerprint=other_fp,
            peer_pubkey_hex=other_key,
            own_pubkey_hex=initiator_id.public_key_hex,
        )
        assert first is not None and second is None

    def test_force_repair_replaces_live_link(self) -> None:
        """§27.2: an explicit re-pair request zeroizes and supersedes the old key."""

        pair = _Pair()
        pair.link(epoch=1)
        initiator_mesh = pair.a if pair.initiator_is_a else pair.b
        initiator_id = pair.a_id if pair.initiator_is_a else pair.b_id
        other_id = pair.b_id if pair.initiator_is_a else pair.a_id
        other_fp = pair.b_fp if pair.initiator_is_a else pair.a_fp
        old_link = initiator_mesh.link_for(GROUP, other_fp, 1)
        assert old_link is not None
        # without force the live link is reused (no new hello)…
        assert (
            initiator_mesh.hello_for(
                group_id=GROUP,
                epoch=1,
                peer_fingerprint=other_fp,
                peer_pubkey_hex=other_id.public_key_hex,
                own_pubkey_hex=initiator_id.public_key_hex,
            )
            is None
        )
        # …with force a fresh hello is minted and the stale key dies.
        hello = initiator_mesh.hello_for(
            group_id=GROUP,
            epoch=1,
            peer_fingerprint=other_fp,
            peer_pubkey_hex=other_id.public_key_hex,
            own_pubkey_hex=initiator_id.public_key_hex,
            force=True,
        )
        assert hello is not None
        assert all(byte == 0 for byte in old_link._key)
        assert initiator_mesh.link_for(GROUP, other_fp, 1) is None  # pending only

    def test_first_observation_still_drains_old_links(self) -> None:
        """The very first epoch a client ever observes engages the drain
        window — old-epoch links may never linger 'live' (§16.5)."""

        pair = _Pair()
        pair.link(epoch=1)
        pair.a.observe_epoch(GROUP, 2)  # first observation is 2 directly
        # the epoch-1 link opens only inside the drain window, at epoch e-1
        assert pair.a.link_for(GROUP, pair.b_fp, 1) is None  # not 'live'
        assert pair.a.accepting_link(GROUP, 1, pair.b_fp) is not None
        # beyond the window it is expired — nothing left that can open it
        pair.clock[0] += GROUP_EPOCH_DRAIN_SECONDS + 1
        assert pair.a.accepting_link(GROUP, 1, pair.b_fp) is None
        assert pair.a.link_for(GROUP, pair.b_fp, 1) is None

    def test_drain_window_is_exactly_thirty_seconds(self) -> None:
        pair = _Pair()
        pair.link(epoch=1)
        pair.a.observe_epoch(GROUP, 2)
        pair.clock[0] += GROUP_EPOCH_DRAIN_SECONDS - 0.001
        assert pair.a.accepting_link(GROUP, 1, pair.b_fp) is not None
        pair.clock[0] += 0.002
        assert pair.a.accepting_link(GROUP, 1, pair.b_fp) is None

    def test_accepting_link_rejects_out_of_window_epochs(self) -> None:
        pair = _Pair()
        pair.link(epoch=2)
        pair.a.observe_epoch(GROUP, 3)
        assert pair.a.accepting_link(GROUP, 4, pair.b_fp) is None  # future
        assert pair.a.accepting_link(GROUP, 1, pair.b_fp) is None  # two epochs back
        pair.clock[0] += GROUP_EPOCH_DRAIN_SECONDS + 1
        assert pair.a.accepting_link(GROUP, 2, pair.b_fp) is None  # expired e-1

    def test_epoch_regression_observation_ignored(self) -> None:
        pair = _Pair()
        pair.link(epoch=2)
        assert pair.a.observe_epoch(GROUP, 1) == []  # never regresses
        assert pair.a.link_for(GROUP, pair.b_fp, 2) is not None

    def test_cross_epoch_open_fails(self) -> None:
        """A frame sealed for epoch 1 never opens as an epoch-2 frame."""

        pair = _Pair()
        pair.link(epoch=1)
        sealed = pair.seal_a_to_b(1, b"epoch one content")
        pair.a.observe_epoch(GROUP, 2)
        pair.b.observe_epoch(GROUP, 2)
        # epoch label tampered in the envelope → AAD mismatch → reject
        with pytest.raises((DecryptionError, GroupMessageError)):
            pair.b.open_inbound(
                group_id=GROUP, epoch=2, sender=pair.a_fp, recipient=pair.b_fp, sealed=sealed
            )


class TestSealingAndContext:
    def test_roundtrip(self) -> None:
        pair = _Pair()
        pair.link(epoch=1)
        sealed = pair.seal_a_to_b(1, b"group hello")
        opened = pair.b.open_inbound(
            group_id=GROUP, epoch=1, sender=pair.a_fp, recipient=pair.b_fp, sealed=sealed
        )
        assert opened == b"group hello"

    def test_wrong_recipient_fails(self) -> None:
        pair = _Pair()
        pair.link(epoch=1)
        sealed = pair.seal_a_to_b(1, b"for b only")
        outsider = _identity()
        with pytest.raises(DecryptionError):
            pair.b.open_inbound(
                group_id=GROUP, epoch=1, sender=pair.a_fp, recipient=_fp(outsider), sealed=sealed
            )

    def test_wrong_group_fails(self) -> None:
        """§16.3: keys for (g,e) open nothing from (g-prime != g, e)."""

        pair = _Pair()
        pair.link(epoch=1)
        sealed = pair.seal_a_to_b(1, b"one group only")
        # (a) no link for the other group at all → mesh gate rejects
        with pytest.raises(GroupMessageError):
            pair.b.open_inbound(
                group_id=GROUP2, epoch=1, sender=pair.a_fp, recipient=pair.b_fp, sealed=sealed
            )
        # (b) now link the SAME pair in the other group; the group id is in
        # the HKDF context, so the ciphertext still fails cryptographically.
        if pair.initiator_is_a:
            hello = pair.a.hello_for(
                group_id=GROUP2,
                epoch=1,
                peer_fingerprint=pair.b_fp,
                peer_pubkey_hex=pair.b_id.public_key_hex,
                own_pubkey_hex=pair.a_id.public_key_hex,
            )
            assert hello is not None
            reply = pair.b.handle_hello(
                group_id=GROUP2,
                epoch=1,
                peer_fingerprint=pair.a_fp,
                peer_pubkey_hex=pair.a_id.public_key_hex,
                own_pubkey_hex=pair.b_id.public_key_hex,
                hello_payload=hello,
            )
            pair.a.handle_reply(
                group_id=GROUP2, epoch=1, peer_fingerprint=pair.b_fp, reply_payload=reply
            )
        else:
            hello = pair.b.hello_for(
                group_id=GROUP2,
                epoch=1,
                peer_fingerprint=pair.a_fp,
                peer_pubkey_hex=pair.a_id.public_key_hex,
                own_pubkey_hex=pair.b_id.public_key_hex,
            )
            assert hello is not None
            reply = pair.a.handle_hello(
                group_id=GROUP2,
                epoch=1,
                peer_fingerprint=pair.b_fp,
                peer_pubkey_hex=pair.b_id.public_key_hex,
                own_pubkey_hex=pair.a_id.public_key_hex,
                hello_payload=hello,
            )
            pair.b.handle_reply(
                group_id=GROUP2, epoch=1, peer_fingerprint=pair.a_fp, reply_payload=reply
            )
        with pytest.raises(DecryptionError):
            pair.b.open_inbound(
                group_id=GROUP2, epoch=1, sender=pair.a_fp, recipient=pair.b_fp, sealed=sealed
            )

    def test_wrong_epoch_fails(self) -> None:
        pair = _Pair()
        pair.link(epoch=1)
        sealed = pair.seal_a_to_b(1, b"epoch one")
        with pytest.raises(GroupMessageError):
            pair.b.open_inbound(
                group_id=GROUP, epoch=2, sender=pair.a_fp, recipient=pair.b_fp, sealed=sealed
            )

    def test_wrong_sender_fails(self) -> None:
        pair = _Pair()
        pair.link(epoch=1)
        sealed = pair.seal_a_to_b(1, b"from a")
        with pytest.raises(GroupMessageError):
            pair.b.open_inbound(
                group_id=GROUP, epoch=1, sender=pair.b_fp, recipient=pair.b_fp, sealed=sealed
            )

    def test_cross_ciphertext_swap_fails(self) -> None:
        """B's copy is a distinct key: sealing for C never opens at B."""

        pair = _Pair()
        pair.link(epoch=1)
        c_id = _identity()
        c_link = GroupMeshManager(monotonic=lambda: 0.0)
        # A↔C link built the same way, then C-direction ciphertext tried at B.
        if not GroupMeshManager.initiates(pair.a_id.public_key_hex, c_id.public_key_hex):
            pytest.skip("role order made A the responder for this draw")
        hello = pair.a.hello_for(
            group_id=GROUP,
            epoch=1,
            peer_fingerprint=_fp(c_id),
            peer_pubkey_hex=c_id.public_key_hex,
            own_pubkey_hex=pair.a_id.public_key_hex,
        )
        assert hello is not None
        reply = c_link.handle_hello(
            group_id=GROUP,
            epoch=1,
            peer_fingerprint=pair.a_fp,
            peer_pubkey_hex=pair.a_id.public_key_hex,
            own_pubkey_hex=c_id.public_key_hex,
            hello_payload=hello,
        )
        pair.a.handle_reply(
            group_id=GROUP, epoch=1, peer_fingerprint=_fp(c_id), reply_payload=reply
        )
        link_to_c = pair.a.link_for(GROUP, _fp(c_id), 1)
        assert link_to_c is not None
        sealed_for_c = link_to_c.seal(
            b"secret for c", group_message_aad(GROUP, 1, pair.a_fp, _fp(c_id))
        )
        with pytest.raises(DecryptionError):
            pair.b.open_inbound(
                group_id=GROUP, epoch=1, sender=pair.a_fp, recipient=pair.b_fp, sealed=sealed_for_c
            )

    def test_one_to_one_ciphertext_never_opens_as_group(self) -> None:
        """§20.2: protocol-string separation — a 1:1 frame fails as a group message."""

        from ghostlink.messaging.protocol.crypto import seal as seal_1to1
        from ghostlink.messaging.protocol.handshake import (
            HandshakeInitiator,
            HandshakeResponder,
        )

        initiator = HandshakeInitiator("ghostlink/room/gl-room-test")
        reply_payload, _responder_session = HandshakeResponder(
            "ghostlink/room/gl-room-test"
        ).answer(initiator.hello_payload())
        session = initiator.complete(reply_payload)
        sealed_1to1 = seal_1to1(session.session_key, b"one to one", b"some-aad")
        pair = _Pair()
        pair.link(epoch=1)
        with pytest.raises(DecryptionError):
            pair.b.open_inbound(
                group_id=GROUP, epoch=1, sender=pair.a_fp, recipient=pair.b_fp, sealed=sealed_1to1
            )


class TestEpochDrain:
    def test_drain_window_boundary(self) -> None:
        """§16.5 closed form: accept during 30 s, reject after — exactly."""

        pair = _Pair()
        pair.link(epoch=1)
        sealed_epoch_1 = pair.seal_a_to_b(1, b"pre-leap message")
        # leap to epoch 2: old link starts draining
        pair.b.observe_epoch(GROUP, 2)
        # t = leap+29 s → still acceptable
        pair.clock[0] += GROUP_EPOCH_DRAIN_SECONDS - 1
        opened = pair.b.open_inbound(
            group_id=GROUP, epoch=1, sender=pair.a_fp, recipient=pair.b_fp, sealed=sealed_epoch_1
        )
        assert opened == b"pre-leap message"
        # t = leap+31 s → reject
        pair.clock[0] += 2.0 + 1.0  # now leap+31
        with pytest.raises(GroupMessageError):
            pair.b.open_inbound(
                group_id=GROUP,
                epoch=1,
                sender=pair.a_fp,
                recipient=pair.b_fp,
                sealed=sealed_epoch_1,
            )

    def test_drain_rejects_wrong_epoch_live_link(self) -> None:
        pair = _Pair()
        pair.link(epoch=1)
        pair.b.observe_epoch(GROUP, 2)
        # a brand-new epoch-3 claim: ahead of the observed epoch — refuse
        with pytest.raises(GroupMessageError):
            pair.b.open_inbound(
                group_id=GROUP, epoch=3, sender=pair.a_fp, recipient=pair.b_fp, sealed=b"xx"
            )
        # and epoch 0 is never a frame epoch
        with pytest.raises(GroupMessageError):
            pair.b.open_inbound(
                group_id=GROUP, epoch=0, sender=pair.a_fp, recipient=pair.b_fp, sealed=b"xx"
            )

    def test_expire_zeroizes_drained(self) -> None:
        pair = _Pair()
        pair.link(epoch=1)
        link_b = pair.b.link_for(GROUP, pair.a_fp, 1)
        assert link_b is not None
        pair.b.observe_epoch(GROUP, 2)
        pair.clock[0] += GROUP_EPOCH_DRAIN_SECONDS + 1
        expired = pair.b.expire()
        assert (GROUP, pair.a_fp) in expired
        assert all(byte == 0 for byte in link_b._key)  # zeroized (§26.4)

    def test_pending_dropped_on_leap(self) -> None:
        pair = _Pair()
        initiator_mesh, initiator_id = (
            (pair.a, pair.a_id) if pair.initiator_is_a else (pair.b, pair.b_id)
        )
        other_fp = pair.b_fp if pair.initiator_is_a else pair.a_fp
        other_key = pair.b_id.public_key_hex if pair.initiator_is_a else pair.a_id.public_key_hex
        hello = initiator_mesh.hello_for(
            group_id=GROUP,
            epoch=1,
            peer_fingerprint=other_fp,
            peer_pubkey_hex=other_key,
            own_pubkey_hex=initiator_id.public_key_hex,
        )
        assert hello is not None
        dropped = initiator_mesh.observe_epoch(GROUP, 2)
        assert (GROUP, other_fp) in dropped
        assert initiator_mesh.pending_handshake_count() == 0

    def test_epoch_never_regresses(self) -> None:
        mesh = GroupMeshManager()
        mesh.observe_epoch(GROUP, 5)
        mesh.observe_epoch(GROUP, 3)  # stale observation — ignored
        assert mesh.observed_epoch(GROUP) == 5


class TestTeardownZeroize:
    def test_teardown_link_zeroizes(self) -> None:
        pair = _Pair()
        pair.link(epoch=1)
        link = pair.b.link_for(GROUP, pair.a_fp, 1)
        assert link is not None
        pair.b.teardown_link(GROUP, pair.a_fp)
        assert all(byte == 0 for byte in link._key)
        assert pair.b.link_for(GROUP, pair.a_fp, 1) is None

    def test_teardown_group_clears_all(self) -> None:
        pair = _Pair()
        pair.link(epoch=1)
        assert pair.b.teardown_group(GROUP) >= 1
        assert pair.b.link_count() == 0

    def test_teardown_all(self) -> None:
        pair = _Pair()
        pair.link(epoch=1)
        assert pair.a.teardown_all() >= 1
        assert pair.a.link_count() == 0

    def test_old_session_ciphertext_never_accepted_after_rehandshake(self) -> None:
        """§27: a fresh link's keys can't fit stale ciphertext."""

        pair = _Pair()
        pair.link(epoch=1)
        sealed_old = pair.seal_a_to_b(1, b"old session bytes")
        pair.a.teardown_link(GROUP, pair.b_fp)
        pair.b.teardown_link(GROUP, pair.a_fp)
        pair.link(epoch=1)  # fresh ephemeral keys on the same pair+epoch
        with pytest.raises(DecryptionError):
            pair.b.open_inbound(
                group_id=GROUP, epoch=1, sender=pair.a_fp, recipient=pair.b_fp, sealed=sealed_old
            )


class TestAbuseBounds:
    def test_aead_failure_counter_and_suspect(self) -> None:
        pair = _Pair()
        pair.link(epoch=1)
        for _ in range(GROUP_AEAD_FAILURES_NOTICE):
            with pytest.raises(DecryptionError):
                pair.b.open_inbound(
                    group_id=GROUP,
                    epoch=1,
                    sender=pair.a_fp,
                    recipient=pair.b_fp,
                    sealed=b"\x00" * 40,
                )
        assert pair.b.link_suspect(GROUP, pair.a_fp)

    def test_link_capacity_bound(self) -> None:
        """§26.4/§32: at most 7 live links per group (members minus one), ever."""

        own = _identity()
        home = GroupMeshManager()
        peers: list[tuple[LocalIdentity, GroupMeshManager]] = []
        for _ in range(MAX_GROUP_MEMBERS - 1):
            peer = _identity()
            peer_mesh = GroupMeshManager()
            if GroupMeshManager.initiates(own.public_key_hex, peer.public_key_hex):
                hello = home.hello_for(
                    group_id=GROUP,
                    epoch=1,
                    peer_fingerprint=_fp(peer),
                    peer_pubkey_hex=peer.public_key_hex,
                    own_pubkey_hex=own.public_key_hex,
                )
                assert hello is not None
                reply = peer_mesh.handle_hello(
                    group_id=GROUP,
                    epoch=1,
                    peer_fingerprint=_fp(own),
                    peer_pubkey_hex=own.public_key_hex,
                    own_pubkey_hex=peer.public_key_hex,
                    hello_payload=hello,
                )
                home.handle_reply(
                    group_id=GROUP, epoch=1, peer_fingerprint=_fp(peer), reply_payload=reply
                )
            else:
                hello = peer_mesh.hello_for(
                    group_id=GROUP,
                    epoch=1,
                    peer_fingerprint=_fp(own),
                    peer_pubkey_hex=own.public_key_hex,
                    own_pubkey_hex=peer.public_key_hex,
                )
                assert hello is not None
                reply = home.handle_hello(
                    group_id=GROUP,
                    epoch=1,
                    peer_fingerprint=_fp(peer),
                    peer_pubkey_hex=peer.public_key_hex,
                    own_pubkey_hex=own.public_key_hex,
                    hello_payload=hello,
                )
                peer_mesh.handle_reply(
                    group_id=GROUP, epoch=1, peer_fingerprint=_fp(own), reply_payload=reply
                )
            peers.append((peer, peer_mesh))
        assert home.link_count() == MAX_GROUP_MEMBERS - 1
        # a 7th peer (8th member → 7 live links held) must be refused (#9 key)
        extra = _identity()
        if GroupMeshManager.initiates(own.public_key_hex, extra.public_key_hex):
            with pytest.raises(GroupMessageError):
                home.hello_for(
                    group_id=GROUP,
                    epoch=1,
                    peer_fingerprint=_fp(extra),
                    peer_pubkey_hex=extra.public_key_hex,
                    own_pubkey_hex=own.public_key_hex,
                )
        else:
            extra_mesh = GroupMeshManager()
            hello = extra_mesh.hello_for(
                group_id=GROUP,
                epoch=1,
                peer_fingerprint=_fp(own),
                peer_pubkey_hex=own.public_key_hex,
                own_pubkey_hex=extra.public_key_hex,
            )
            assert hello is not None
            with pytest.raises(GroupMessageError):
                home.handle_hello(
                    group_id=GROUP,
                    epoch=1,
                    peer_fingerprint=_fp(extra),
                    peer_pubkey_hex=extra.public_key_hex,
                    own_pubkey_hex=own.public_key_hex,
                    hello_payload=hello,
                )
