"""Shared helpers for the Phase 6B group tests.

Every relay-touching test dials a live :class:`RelayServer` — nothing here
mocks the authority; helpers only cut down the wiring boilerplate.
"""

from __future__ import annotations

import base64
from pathlib import Path

from ghostlink.groups.events import (
    canonical_attest_form,
    canonical_create_form,
    fingerprint_for_key_hex,
)
from ghostlink.groups.lifecycle import LocalGroupManager
from ghostlink.groups.registry import LocalGroupRegistry
from ghostlink.identity.identity import LocalIdentity
from ghostlink.identity.lifecycle import IdentityManager
from ghostlink.identity.storage import IdentityStore
from ghostlink.invites.lifecycle import SecureInviteManager
from ghostlink.invites.registry import LocalInviteRegistry
from ghostlink.storage.manager import StorageManager
from ghostlink.transport.relay.client import RelayClient, RelayClientConfig
from ghostlink.transport.relay.endpoint import RelayEndpoint
from ghostlink.transport.relay.server import RelayServer

FAST = RelayClientConfig(
    connect_timeout_seconds=2.0,
    handshake_timeout_seconds=2.0,
    heartbeat_interval_seconds=3600.0,
    heartbeat_timeout_seconds=1.0,
    reconnect_attempts=0,
    reconnect_base_delay_seconds=0.05,
)


def fresh_identity() -> LocalIdentity:
    return LocalIdentity.generate()


def fingerprint_of(identity: LocalIdentity) -> str:
    return fingerprint_for_key_hex(identity.public_key_hex)


def sign_b64(identity: LocalIdentity, message: bytes) -> str:
    return base64.b64encode(identity.sign(message)).decode("ascii")


def pop_b64(identity: LocalIdentity, name: str, nonce: str) -> str:
    return sign_b64(identity, canonical_create_form(name, nonce))


def attest_b64(identity: LocalIdentity, group_id: str, nonce: str) -> str:
    return sign_b64(identity, canonical_attest_form(group_id, fingerprint_of(identity), nonce))


async def client_for(server: RelayServer, name: str) -> RelayClient:
    client = RelayClient(RelayEndpoint.from_url(server.url), client_name=name, config=FAST)
    await client.connect()
    return client


class GroupHome:
    """One device: isolated storage, identity, and group/invite managers."""

    def __init__(self, root: Path) -> None:
        self.storage = StorageManager(root)
        self.identities = IdentityManager(IdentityStore(self.storage))
        self.groups = LocalGroupManager(LocalGroupRegistry(self.storage), self.identities)
        self.invites = SecureInviteManager(LocalInviteRegistry(self.storage))

    def fingerprint(self) -> str:
        return self.groups._identity_fingerprint(self.identities.ensure())

    def reload(self) -> None:
        """Simulate an app restart: fresh managers over the same storage."""

        self.identities = IdentityManager(IdentityStore(self.storage))
        self.groups = LocalGroupManager(LocalGroupRegistry(self.storage), self.identities)
        self.invites = SecureInviteManager(LocalInviteRegistry(self.storage))
