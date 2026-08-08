"""Ephemeral identity: creation, persistence, loading, nickname, secrecy.

Identity material must live only in the owner-only identity store — never
in logs, chat history, or user-facing output.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ghostlink.exceptions.invites import InviteValidationError
from ghostlink.exceptions.storage import StorageCorruptionError
from ghostlink.identity import (
    IdentityManager,
    IdentityStore,
    LocalIdentity,
    identity_fingerprint,
    identity_id_for,
    is_valid_fingerprint,
    is_valid_identity_id,
    validate_nickname,
)
from ghostlink.storage.manager import StorageManager


@pytest.fixture()
def storage(tmp_path: Path) -> StorageManager:
    return StorageManager(tmp_path / "state")


class TestIdentityCreation:
    def test_generated_identities_are_distinct(self) -> None:
        first = LocalIdentity.generate()
        second = LocalIdentity.generate()
        assert first.public_key_hex != second.public_key_hex
        assert first.identity_id != second.identity_id

    def test_identity_id_format(self) -> None:
        identity = LocalIdentity.generate()
        assert is_valid_identity_id(identity.identity_id)
        assert identity.identity_id.startswith("GL-")

    def test_created_at_is_timezone_aware(self) -> None:
        identity = LocalIdentity.generate()
        assert identity.created_at.tzinfo is not None

    def test_generation_with_fixed_clock(self) -> None:
        moment = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
        identity = LocalIdentity.generate(now=moment)
        assert identity.created_at == moment

    def test_public_key_is_32_bytes(self) -> None:
        identity = LocalIdentity.generate()
        assert len(identity.public_key_bytes) == 32
        assert len(identity.public_key_hex) == 64


class TestFingerprint:
    def test_glfp_shape(self) -> None:
        fingerprint = identity_fingerprint(LocalIdentity.generate().public_key_bytes)
        assert fingerprint.startswith("GLFP-")
        assert is_valid_fingerprint(fingerprint)

    def test_deterministic_for_same_key(self) -> None:
        identity = LocalIdentity.generate()
        assert identity_fingerprint(identity.public_key_bytes) == identity_fingerprint(
            identity.public_key_bytes
        )

    def test_distinct_for_distinct_keys(self) -> None:
        assert identity_fingerprint(
            LocalIdentity.generate().public_key_bytes
        ) != identity_fingerprint(LocalIdentity.generate().public_key_bytes)

    def test_handle_is_derived_from_key(self) -> None:
        identity = LocalIdentity.generate()
        assert identity_id_for(identity.public_key_bytes) == identity.identity_id

    def test_rejects_wrong_key_length(self) -> None:
        with pytest.raises(ValueError):
            identity_fingerprint(b"short")
        with pytest.raises(ValueError):
            identity_id_for(b"\x00" * 16)

    def test_fingerprint_format_validation(self) -> None:
        assert is_valid_fingerprint("GLFP-7A92-31CF-88B4")
        assert is_valid_fingerprint("glfp-7a92-31cf-88b4")
        assert not is_valid_fingerprint("GLFP-7A92-31CF")
        assert not is_valid_fingerprint("7A92-31CF-88B4")
        assert not is_valid_fingerprint("GLFP-ZZZZ-31CF-88B4")


class TestPersistence:
    def test_round_trip_survives_a_fresh_manager(self, storage: StorageManager) -> None:
        manager = IdentityManager(IdentityStore(storage))
        created = manager.ensure()
        again = IdentityManager(IdentityStore(storage)).ensure()
        assert again.public_key_hex == created.public_key_hex
        assert again.identity_id == created.identity_id
        assert again.nickname == created.nickname

    def test_ensure_creates_exactly_once(self, storage: StorageManager) -> None:
        manager = IdentityManager(IdentityStore(storage))
        first = manager.ensure()
        assert manager.ensure().public_key_hex == first.public_key_hex

    def test_store_file_has_owner_only_permissions(self, storage: StorageManager) -> None:
        IdentityManager(IdentityStore(storage)).ensure()
        path = IdentityStore(storage).store_path
        assert path.exists()
        assert path.stat().st_mode & 0o777 == 0o600

    def test_load_returns_none_before_first_run(self, storage: StorageManager) -> None:
        assert IdentityStore(storage).load() is None

    def test_storage_dict_contains_only_required_fields(self) -> None:
        document = LocalIdentity.generate().to_storage_dict()
        assert set(document) == {"v", "private_key", "nickname", "created_at"}

    def test_corrupt_documents_raise_storage_errors(self) -> None:
        with pytest.raises(StorageCorruptionError):
            LocalIdentity.from_storage_dict(
                {"private_key": "zz", "nickname": "", "created_at": "x"}
            )
        with pytest.raises(StorageCorruptionError):
            LocalIdentity.from_storage_dict({"nickname": ""})
        with pytest.raises(StorageCorruptionError):
            LocalIdentity.from_storage_dict(
                {
                    "private_key": "0f" * 32,
                    "nickname": "",
                    "created_at": "not-a-date",
                }
            )


class TestNickname:
    def test_set_and_reload(self, storage: StorageManager) -> None:
        manager = IdentityManager(IdentityStore(storage))
        manager.set_nickname("ShadowUser")
        reloaded = IdentityManager(IdentityStore(storage)).load()
        assert reloaded is not None
        assert reloaded.nickname == "ShadowUser"

    def test_invalid_nicknames_rejected(self) -> None:
        with pytest.raises(InviteValidationError):
            validate_nickname("")
        with pytest.raises(InviteValidationError):
            validate_nickname("x" * 25)
        with pytest.raises(InviteValidationError):
            validate_nickname("bad\x07name")

    def test_reset_rotates_the_key(self, storage: StorageManager) -> None:
        manager = IdentityManager(IdentityStore(storage))
        first = manager.ensure()
        rotated = manager.reset()
        assert rotated.public_key_hex != first.public_key_hex
        assert manager.ensure().public_key_hex == rotated.public_key_hex

    def test_rename_keeps_the_key(self, storage: StorageManager) -> None:
        manager = IdentityManager(IdentityStore(storage))
        first = manager.ensure()
        renamed = manager.set_nickname("Ghost")
        assert renamed.public_key_hex == first.public_key_hex


class TestSecrecy:
    def test_private_key_never_in_logs(
        self, storage: StorageManager, caplog: pytest.LogCaptureFixture
    ) -> None:
        manager = IdentityManager(IdentityStore(storage))
        with caplog.at_level(logging.DEBUG):
            identity = manager.ensure()
            manager.set_nickname("LeakyTest")
            manager.fingerprint()
            manager.reset()
        private_hex = identity.to_storage_dict()["private_key"]
        for record in caplog.records:
            assert str(private_hex) not in record.getMessage()
            assert "private" not in record.getMessage().lower()

    def test_public_views_exclude_private_material(self) -> None:
        identity = LocalIdentity.generate()
        private_hex = str(identity.to_storage_dict()["private_key"])
        for view in (identity.identity_id, identity.public_key_hex, identity.nickname):
            assert private_hex != view
        # The private key is never part of the public representation surface.
        assert "private" not in identity.identity_id.lower()
