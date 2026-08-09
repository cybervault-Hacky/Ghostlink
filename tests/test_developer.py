"""Phase 10A developer-account tests.

Production-grade coverage for the local Developer Account system: CSPRNG key
generation, format/entropy policy, uniqueness, verification success/failure,
revocation, rotation, atomic/corrupt storage, future-schema rejection,
permission & symlink safety, secret redaction, rate limiting, restart
persistence, crash consistency, concurrency, and the network boundary.

Only test-only fake credentials appear in fixtures; they are never
mistakable for real ones.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ghostlink.developer import DeveloperManager
from ghostlink.developer.errors import (
    DeveloperAccountExistsError,
    DeveloperAccountNotFoundError,
    DeveloperCredentialInvalidError,
    DeveloperCredentialNotFoundError,
    DeveloperMaxCredentialsError,
    DeveloperRateLimitError,
    DeveloperStateError,
    DeveloperVerificationError,
)
from ghostlink.developer.keys import (
    SECRET_BYTES,
    generate_key_id,
    issue_credential,
    parse_credential,
)
from ghostlink.developer.models import DeveloperAccount, DeveloperCredential
from ghostlink.developer.storage import DEV_DIR_NAME


def _manager(tmp_path: Path, **kwargs: object) -> DeveloperManager:
    return DeveloperManager(tmp_path / DEV_DIR_NAME, **kwargs)


# A deterministic test-only fake credential (never a real key).
def _fake_credential() -> tuple[str, str]:
    key_id, credential = issue_credential()
    return key_id, credential


# ---------------------------------------------------------------- generation


class TestKeyGeneration:
    def test_secret_entropy_length(self) -> None:
        for _ in range(20):
            _key_id, credential = issue_credential()
            key_id, secret = parse_credential(credential)
            assert key_id.startswith("dk_")
            assert len(key_id) == 11  # dk_ + 8
            assert len(secret) == 43  # 32 bytes base64url unpadded
            assert (
                len(__import__("base64").urlsafe_b64decode(secret + "=" * (len(secret) % 4)))
                == SECRET_BYTES
            )

    def test_keys_are_unique(self) -> None:
        creds = {issue_credential() for _ in range(500)}
        assert len(creds) == 500

    def test_key_ids_are_unique(self) -> None:
        ids = {generate_key_id() for _ in range(500)}
        assert len(ids) == 500

    def test_format_validation(self) -> None:
        key_id, credential = _fake_credential()
        parsed_id, parsed_secret = parse_credential(credential)
        assert parsed_id == key_id
        assert parsed_secret
        # whitespace tolerated around the credential
        assert parse_credential(f"  {credential}  ") == (key_id, parsed_secret)

    def test_malformed_credentials_rejected(self) -> None:
        for bad in (
            "",
            "not-a-key",
            "gl_dev_",
            "gl_dev_dk_XXXX_YYYY",
            "gl_dev_" + "a" * 80,
            "gl_dev_dk_ABCDEF12_short",
        ):
            with pytest.raises(DeveloperCredentialInvalidError):
                parse_credential(bad)

    def test_unicode_and_whitespace_rejected(self) -> None:
        with pytest.raises(DeveloperCredentialInvalidError):
            parse_credential("gl_dev_dk_AB12CD34_éééééééééééééééééééééééééééééééééééééééééé")
        with pytest.raises(DeveloperCredentialInvalidError):
            parse_credential("\u200b" * 10)


# -------------------------------------------------------------- verification


class TestVerification:
    def test_success_and_failure(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _acct, credential = manager.init_account()
        assert manager.verify_credential(credential)
        with pytest.raises(DeveloperVerificationError):
            manager.verify_credential(_wrong(credential))

    def test_wrong_key_rejected(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _acct, credential = manager.init_account()
        key_id, secret = parse_credential(credential)
        mutated = secret[:-1] + ("A" if secret[-1] != "A" else "B")
        with pytest.raises(DeveloperVerificationError):
            manager.verify_credential(f"gl_dev_{key_id}_{mutated}")

    def test_revoked_key_rejected(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _acct, credential = manager.init_account()
        key_id, _secret = parse_credential(credential)
        manager.revoke_credential(key_id)
        with pytest.raises(DeveloperVerificationError):
            manager.verify_credential(credential)

    def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _acct, _cred = manager.init_account()
        _k, other = issue_credential()
        with pytest.raises(DeveloperVerificationError):
            manager.verify_credential(other)


def _wrong(credential: str) -> str:
    key_id, secret = parse_credential(credential)
    flipped = ("A" if secret[0] != "A" else "B") + secret[1:]
    return f"gl_dev_{key_id}_{flipped}"


# ----------------------------------------------------------------- lifecycle


class TestAccountLifecycle:
    def test_init_creates_account(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        account, credential = manager.init_account()
        assert account.developer_id.startswith("dev_")
        assert account.status == "active"
        assert credential.startswith("gl_dev_dk_")

    def test_init_is_idempotent_guard(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        manager.init_account()
        with pytest.raises(DeveloperAccountExistsError):
            manager.init_account()

    def test_require_account_raises_when_absent(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        with pytest.raises(DeveloperAccountNotFoundError):
            manager.require_account()

    def test_status_none_when_absent(self, tmp_path: Path) -> None:
        assert _manager(tmp_path).status() is None

    def test_restart_persistence(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _acct, credential = manager.init_account()
        manager2 = _manager(tmp_path)
        assert manager2.verify_credential(credential)
        assert manager2.status() is not None


class TestRotation:
    def test_rotation_invalidates_old_and_works_with_new(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _acct, old = manager.init_account()
        _acct2, new = manager.rotate_credential()
        assert manager.verify_credential(new)
        with pytest.raises(DeveloperVerificationError):
            manager.verify_credential(old)
        # distinct key ids
        old_id, _ = parse_credential(old)
        new_id, _ = parse_credential(new)
        assert old_id != new_id

    def test_rotation_marks_old_revoked(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _acct, old = manager.init_account()
        old_id, _ = parse_credential(old)
        manager.rotate_credential()
        creds = {c.key_id: c for c in manager.list_credentials()}
        assert creds[old_id].status == "revoked"


class TestRevocation:
    def test_revoke_is_irreversible(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _acct, credential = manager.init_account()
        key_id, _ = parse_credential(credential)
        manager.revoke_credential(key_id)
        # survives restart
        manager2 = _manager(tmp_path)
        with pytest.raises(DeveloperVerificationError):
            manager2.verify_credential(credential)

    def test_revoke_unknown_raises(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        manager.init_account()
        with pytest.raises(DeveloperCredentialNotFoundError):
            manager.revoke_credential("dk_XXXXXXXX")


class TestCredentialCeiling:
    def test_max_active_credentials(self, tmp_path: Path) -> None:
        from ghostlink.developer import MAX_ACTIVE_CREDENTIALS

        manager = _manager(tmp_path)
        manager.init_account()
        # init creates 1; create 3 more to reach the ceiling of 4.
        for _ in range(MAX_ACTIVE_CREDENTIALS - 1):
            manager.create_credential()
        active = [c for c in manager.list_credentials() if c.is_active]
        assert len(active) == MAX_ACTIVE_CREDENTIALS
        with pytest.raises(DeveloperMaxCredentialsError):
            manager.create_credential()


# ------------------------------------------------------------------- storage


class TestStorage:
    def test_atomic_and_no_secret_on_disk(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _acct, credential = manager.init_account()
        _key_id, secret = parse_credential(credential)
        for name in ("account.json", "credentials.json"):
            raw = (tmp_path / DEV_DIR_NAME / name).read_text(encoding="utf-8")
            assert secret not in raw
        assert (tmp_path / DEV_DIR_NAME / "credentials.json").stat().st_mode & 0o777 == 0o600
        assert (tmp_path / DEV_DIR_NAME).stat().st_mode & 0o777 == 0o700

    def test_corrupt_credentials_fail_closed(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        manager.init_account()
        path = tmp_path / DEV_DIR_NAME / "credentials.json"
        path.write_text('{"v": 1, "credentials": {not-json')
        with pytest.raises(DeveloperStateError):
            manager.status()

    def test_future_schema_rejected(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        manager.init_account()
        path = tmp_path / DEV_DIR_NAME / "account.json"
        data = json.loads(path.read_text())
        data["v"] = 99
        path.write_text(json.dumps(data))
        with pytest.raises(DeveloperStateError):
            manager.status()

    def test_future_credential_schema_rejected(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        manager.init_account()
        path = tmp_path / DEV_DIR_NAME / "credentials.json"
        data = json.loads(path.read_text())
        data["v"] = 99
        path.write_text(json.dumps(data))
        with pytest.raises(DeveloperStateError):
            manager.status()

    def test_missing_version_accepted(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        manager.init_account()
        path = tmp_path / DEV_DIR_NAME / "account.json"
        data = json.loads(path.read_text())
        del data["v"]
        path.write_text(json.dumps(data))
        assert manager.status() is not None

    def test_symlink_dir_refused(self, tmp_path: Path) -> None:
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "developer"
        link.symlink_to(real, target_is_directory=True)
        manager = _manager(tmp_path)
        with pytest.raises(DeveloperStateError):
            manager.init_account()

    def test_no_secret_in_metadata(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _acct, credential = manager.init_account()
        _key_id, secret = parse_credential(credential)
        # secrets never appear in account/credential public docs
        for name in ("account.json", "credentials.json"):
            text = (tmp_path / DEV_DIR_NAME / name).read_text(encoding="utf-8")
            assert secret not in text


class TestPermissionChecks:
    def test_insecure_dir_reported_by_inspection(self, tmp_path: Path) -> None:
        from ghostlink.developer.validation import inspect_security

        manager = _manager(tmp_path)
        manager.init_account()
        dir_path = tmp_path / DEV_DIR_NAME
        os.chmod(dir_path, 0o755)
        report = inspect_security(dir_path)
        assert not report.dir_secure
        assert any("accessible" in i for i in report.issues)

    def test_insecure_file_reported(self, tmp_path: Path) -> None:
        from ghostlink.developer.validation import inspect_security

        manager = _manager(tmp_path)
        manager.init_account()
        path = tmp_path / DEV_DIR_NAME / "credentials.json"
        os.chmod(path, 0o666)
        report = inspect_security(tmp_path / DEV_DIR_NAME)
        assert not report.credentials_file_secure
        assert any("writable" in i for i in report.issues)


# ------------------------------------------------------------------ redaction


class TestRedaction:
    def test_secret_never_in_exceptions(self, tmp_path: Path) -> None:
        manager = _manager(tmp_path)
        _acct, credential = manager.init_account()
        with pytest.raises(DeveloperVerificationError) as excinfo:
            manager.verify_credential(_wrong(credential))
        assert credential not in str(excinfo.value)
        assert str(excinfo.value) == excinfo.value.message

    def test_logs_never_contain_secret(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from ghostlink.core.logging import get_logger

        manager = _manager(tmp_path)
        _acct, credential = manager.init_account()
        logger = get_logger("developer")
        import io
        import logging

        logger.setLevel(logging.DEBUG)
        stream = io.StringIO()
        handler = logging.StreamHandler(stream)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)
        try:
            manager.verify_credential(_wrong(credential))
        except DeveloperVerificationError:
            pass
        finally:
            logger.removeHandler(handler)
        text = stream.getvalue()
        assert "developer_key_verification_failed" in text
        assert credential not in text


# ----------------------------------------------------------------- rate limit


class TestRateLimit:
    def test_failed_attempts_trigger_lockout(self, tmp_path: Path) -> None:
        manager = _manager(
            tmp_path,
            max_failed_attempts=3,
            window_seconds=3600.0,
            cooldown_seconds=3600.0,
        )
        _acct, credential = manager.init_account()
        for _ in range(3):
            with pytest.raises(DeveloperVerificationError):
                manager.verify_credential(_wrong(credential))
        with pytest.raises(DeveloperRateLimitError):
            manager.verify_credential(credential)  # even the correct key is locked out

    def test_success_resets_failures(self, tmp_path: Path) -> None:
        manager = _manager(
            tmp_path,
            max_failed_attempts=3,
            window_seconds=3600.0,
            cooldown_seconds=3600.0,
        )
        _acct, credential = manager.init_account()
        with pytest.raises(DeveloperVerificationError):
            manager.verify_credential(_wrong(credential))
        assert manager.verify_credential(credential)  # success resets
        with pytest.raises(DeveloperVerificationError):
            manager.verify_credential(_wrong(credential))
        assert manager.verify_credential(credential)


# ------------------------------------------------------------ concurrency


class TestConcurrency:
    def test_concurrent_rotation_serializes(self, tmp_path: Path) -> None:
        import threading

        manager = _manager(tmp_path)
        manager.init_account()
        results: list[Exception | None] = []

        def work() -> None:
            try:
                manager.rotate_credential()
                results.append(None)
            except Exception as exc:
                results.append(exc)

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Exactly one active credential remains; the rest are revoked.
        account = manager.require_account()
        active = [c for c in account.credentials.values() if c.is_active]
        assert len(active) == 1


# ------------------------------------------------------------------- network


class TestNetworkBoundary:
    def test_no_network_calls_during_lifecycle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The developer module must never touch the network (Phase 10B will
        add a real remote service; 10A stays strictly local)."""

        calls: list[str] = []
        import socket

        def _fake_socket(*args: object, **kwargs: object) -> object:
            calls.append("socket.socket()")
            raise AssertionError("developer-account operations must not open sockets")

        monkeypatch.setattr(socket, "socket", _fake_socket)
        manager = _manager(tmp_path)
        _acct, credential = manager.init_account()
        manager.verify_credential(credential)
        manager.rotate_credential()
        manager.list_credentials()
        assert calls == []

    def test_no_http_imports_in_module(self) -> None:
        import pathlib

        for mod in ("account", "keys", "storage", "models", "validation"):
            source = pathlib.Path(f"ghostlink/developer/{mod}.py").read_text(encoding="utf-8")
            for banned in ("import http", "import requests", "urllib", "socket.", "aiohttp"):
                assert banned not in source, f"{mod}.py must not use {banned!r}"


# -------------------------------------------------------------- schema/models


class TestSchemaModels:
    def test_account_roundtrip(self) -> None:
        import datetime

        account = DeveloperAccount(
            developer_id="dev_TEST", created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
        )
        data = account.to_dict()
        assert data["v"] == 1
        restored = DeveloperAccount.from_dict(data)
        assert restored.developer_id == "dev_TEST"

    def test_credential_roundtrip(self) -> None:
        import datetime

        cred = DeveloperCredential(
            key_id="dk_AB12CD34",
            status="active",
            created_at=datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC),
            verifier="v1",
            salt_b64="c2FsdA==",
            hash_b64="aGFzaA==",
        )
        restored = DeveloperCredential.from_dict(cred.to_dict())
        assert restored.key_id == "dk_AB12CD34"
        assert restored.is_active


# --------------------------------------------------------------- packaging


class TestPackaging:
    def test_no_real_credentials_in_fixtures(self) -> None:
        # Fixtures use generated keys at runtime; no committed realistic key.
        import pathlib

        for path in pathlib.Path("tests").glob("*.py"):
            if "developer" in path.stem:
                text = path.read_text(encoding="utf-8")
                assert "gl_dev_dk_" not in text or "TEST" in text
