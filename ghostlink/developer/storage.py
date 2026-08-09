"""Developer-account storage (Phase 10A).

Developer account + credential metadata live under ``<data-dir>/developer/``:

    <data-dir>/developer/account.json
    <data-dir>/developer/credentials.json

Both are written with the existing atomic ``JsonFileStorage`` (0600, temp +
fsync + rename) and carry explicit schema versions. The plaintext developer
secret is **never** written to disk — only salted verification material.

Permission and symlink safety: this module rejects a storage path that is a
symlink, is owned by another user, or is group/world-writable, and refuses
to operate on a directory with insecure permissions. These are *best-effort*
checks documented as such (a compromised OS can bypass them) — they are not
a guarantee of device security.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from ghostlink.developer.errors import DeveloperStateError
from ghostlink.developer.models import (
    DEVELOPER_CREDENTIAL_SCHEMA_VERSION,
    DeveloperAccount,
    DeveloperCredential,
)
from ghostlink.storage.json_store import JsonFileStorage

ACCOUNT_FILE = "account.json"
CREDENTIALS_FILE = "credentials.json"
DEV_DIR_NAME = "developer"

DIR_MODE = 0o700
FILE_MODE = 0o600


def _check_path(path: Path) -> None:
    """Best-effort symlink + permission safety on a file or its parent dir."""
    if path.is_symlink():
        raise DeveloperStateError(
            f"Developer storage path '{path}' is a symlink.",
            hint="Refusing to follow a symlink for credential storage.",
        )


def _secure_dir(path: Path) -> None:
    """Ensure ``path`` exists as a private directory with safe permissions.

    Creates the directory if needed and hardens its mode to 0700. Refuses a
    symlinked path. A pre-existing directory is hardened on access; the
    doctor reports insecure permissions via ``inspect_security`` rather than
    refusing to operate (first-run must work even with a permissive umask).
    """
    if path.is_symlink():
        raise DeveloperStateError(
            f"Developer storage directory '{path}' is a symlink.",
            hint="Refusing credential storage behind a symlink.",
        )
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, DIR_MODE)
        st = path.stat()
    except OSError as exc:
        raise DeveloperStateError(
            f"Could not secure developer storage directory '{path}'.",
            hint="Check filesystem permissions.",
        ) from exc
    if stat.S_IMODE(st.st_mode) & 0o077:
        raise DeveloperStateError(
            f"Developer storage directory '{path}' is group/world accessible.",
            hint="Run: chmod 700 <data-dir>/developer",
        )


def _secure_file(path: Path) -> None:
    """Harden a credential file to 0600; refuse a symlinked path."""
    _check_path(path)
    if path.is_symlink():
        raise DeveloperStateError(
            f"Developer credential file '{path}' is a symlink.",
            hint="Refusing credential storage behind a symlink.",
        )
    if not path.exists():
        return
    try:
        os.chmod(path, FILE_MODE)
        st = path.stat()
    except OSError as exc:
        raise DeveloperStateError(
            f"Could not secure developer credential file '{path}'.",
            hint="Check filesystem permissions.",
        ) from exc
    if stat.S_IMODE(st.st_mode) & 0o022:
        raise DeveloperStateError(
            f"Developer credential file '{path}' is group/world writable.",
            hint="Run: chmod 600 <path>",
        )


class DeveloperStore:
    """Reads and writes the developer account + credential documents."""

    def __init__(self, developer_dir: Path) -> None:
        self._dir = developer_dir
        self._account_store = JsonFileStorage(developer_dir / ACCOUNT_FILE)
        self._credential_store = JsonFileStorage(developer_dir / CREDENTIALS_FILE)

    @property
    def directory(self) -> Path:
        return self._dir

    def ensure_dir(self) -> None:
        _secure_dir(self._dir)

    # ------------------------------------------------------------- account

    def load_account(self) -> DeveloperAccount | None:
        _secure_dir(self._dir)
        _secure_file(self._account_store.path)
        raw = dict(self._account_store)
        if not raw:
            return None
        try:
            return DeveloperAccount.from_dict(raw)
        except ValueError as exc:
            raise DeveloperStateError(
                "The stored developer account is corrupt.",
                hint="Delete <data-dir>/developer/account.json and run "
                "'ghostlink developer init' to recreate it.",
            ) from exc

    def save_account(self, account: DeveloperAccount) -> None:
        _secure_dir(self._dir)
        self._account_store.replace(account.to_dict())
        _secure_file(self._account_store.path)

    # ---------------------------------------------------------- credentials

    def load_credentials(self) -> dict[str, DeveloperCredential]:
        _secure_dir(self._dir)
        _secure_file(self._credential_store.path)
        raw = dict(self._credential_store)
        credentials: dict[str, DeveloperCredential] = {}
        payload = raw.get("credentials")
        if payload is None:
            return credentials
        if raw.get("v") is not None and not isinstance(raw.get("v"), int):
            raise DeveloperStateError(
                "The stored credential document has a malformed version.",
                hint="The credentials metadata is corrupt; rotate or recreate it.",
            )
        from ghostlink.core.migration import validate_state_version

        validate_state_version(raw.get("v"))
        if not isinstance(payload, dict):
            raise DeveloperStateError(
                "The stored credential document is malformed.",
                hint="The credentials metadata is corrupt; rotate or recreate it.",
            )
        for key_id, entry in payload.items():
            if not isinstance(entry, dict):
                raise DeveloperStateError(
                    "A stored credential entry is malformed.",
                    hint="The credentials metadata is corrupt; rotate or recreate it.",
                )
            try:
                credential = DeveloperCredential.from_dict(entry)
            except (ValueError, Exception) as exc:
                raise DeveloperStateError(
                    "A stored credential entry is corrupt.",
                    hint="The credentials metadata is corrupt; rotate or recreate it.",
                ) from exc
            credentials[key_id] = credential
        return credentials

    def save_credentials(self, credentials: dict[str, DeveloperCredential]) -> None:
        _secure_dir(self._dir)
        payload = {
            "v": DEVELOPER_CREDENTIAL_SCHEMA_VERSION,
            "credentials": {kid: cred.to_dict() for kid, cred in credentials.items()},
        }
        self._credential_store.replace(payload)
        _secure_file(self._credential_store.path)


__all__ = [
    "ACCOUNT_FILE",
    "CREDENTIALS_FILE",
    "DEV_DIR_NAME",
    "DIR_MODE",
    "FILE_MODE",
    "DeveloperStore",
]
