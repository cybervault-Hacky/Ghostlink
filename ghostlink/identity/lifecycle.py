"""Identity lifecycle management (Phase 5).

Load-or-create semantics for the single local identity plus nickname
management. The manager is the only place application code should touch
identity state; it guarantees the private key never surfaces in logs or
user-facing output.
"""

from __future__ import annotations

from ghostlink.core.logging import get_logger
from ghostlink.identity.fingerprint import identity_fingerprint
from ghostlink.identity.identity import LocalIdentity, validate_nickname
from ghostlink.identity.storage import IdentityStore

_logger = get_logger("identity.lifecycle")


class IdentityManager:
    """Loads, creates and updates the local installation identity."""

    def __init__(self, store: IdentityStore) -> None:
        self._store = store

    # ----------------------------------------------------------------- access

    def load(self) -> LocalIdentity | None:
        """The current identity, or ``None`` before first launch."""

        return self._store.load()

    def ensure(self) -> LocalIdentity:
        """The current identity, generating and persisting one on first use."""

        identity = self._store.load()
        if identity is not None:
            return identity
        identity = LocalIdentity.generate()
        self._store.save(identity)
        _logger.info("local identity created — %s", identity.identity_id)
        return identity

    # -------------------------------------------------------------- nickname

    def set_nickname(self, nickname: str) -> LocalIdentity:
        """Choose (or replace) the display nickname."""

        identity = self.ensure().renamed(validate_nickname(nickname))
        self._store.save(identity)
        _logger.info("identity nickname updated — %s", identity.identity_id)
        return identity

    def clear_nickname(self) -> LocalIdentity:
        identity = self.ensure().with_nickname_cleared()
        self._store.save(identity)
        _logger.info("identity nickname cleared — %s", identity.identity_id)
        return identity

    # -------------------------------------------------------------- rotation

    def reset(self) -> LocalIdentity:
        """Replace the local identity with a freshly generated one.

        Past public keys are gone for good — peers that saved the old
        fingerprint will need to verify again. Chat history is untouched;
        it never contained identity material in the first place.
        """

        self._store.delete()
        identity = LocalIdentity.generate()
        self._store.save(identity)
        _logger.info("local identity rotated — %s", identity.identity_id)
        return identity

    # ------------------------------------------------------------------ views

    def fingerprint(self, identity: LocalIdentity | None = None) -> str:
        """``GLFP-…`` fingerprint of the current (or given) identity."""

        current = identity if identity is not None else self.ensure()
        return identity_fingerprint(current.public_key_bytes)


def fingerprint_for_hex(public_key_hex: str) -> str | None:
    """Fingerprint for a hex-encoded public key, ``None`` when malformed."""

    try:
        public_key = bytes.fromhex(public_key_hex)
    except ValueError:
        return None
    if len(public_key) != 32:
        return None
    return identity_fingerprint(public_key)
