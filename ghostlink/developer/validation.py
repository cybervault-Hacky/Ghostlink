"""Developer-account input & environment validation (Phase 10A).

Best-effort local security checks for the developer storage area: data
directory and credential file ownership/permissions, symlink safety, and
corruption detection. These are documented as *best-effort* — if the
Termux/Linux environment itself is compromised, no local check can protect
the secret.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path

from ghostlink.developer.errors import DeveloperPermissionError, DeveloperStateError


@dataclass(frozen=True, slots=True)
class DevSecurityReport:
    """Read-only result of a developer-storage security inspection."""

    configured: bool  # account document exists
    dir_secure: bool
    account_file_secure: bool
    credentials_file_secure: bool
    account_owner_mismatch: bool
    issues: tuple[str, ...]

    @property
    def healthy(self) -> bool:
        return self.configured and self.dir_secure and self.account_file_secure


def inspect_security(developer_dir: Path) -> DevSecurityReport:
    """Best-effort security report over the developer storage area."""
    issues: list[str] = []
    account_path = developer_dir / "account.json"
    creds_path = developer_dir / "credentials.json"
    configured = account_path.is_file()

    dir_secure = _dir_ok(developer_dir, issues)
    account_secure = _file_ok(account_path, issues, required=configured)
    creds_secure = _file_ok(creds_path, issues, required=False)
    owner_mismatch = False
    if account_path.exists() and account_path.is_file():
        try:
            st = account_path.stat()
            owner_mismatch = st.st_uid != os.getuid()
        except OSError:
            owner_mismatch = True

    return DevSecurityReport(
        configured=configured,
        dir_secure=dir_secure,
        account_file_secure=account_secure,
        credentials_file_secure=creds_secure,
        account_owner_mismatch=owner_mismatch,
        issues=tuple(issues),
    )


def _dir_ok(path: Path, issues: list[str]) -> bool:
    if path.is_symlink():
        issues.append("developer directory is a symlink")
        return False
    if not path.exists():
        issues.append("developer directory missing")
        return False
    try:
        st = path.stat()
    except OSError as exc:
        issues.append(f"cannot stat developer directory: {exc}")
        return False
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        issues.append("developer directory is group/world accessible")
        return False
    if st.st_uid != os.getuid():
        issues.append("developer directory owned by another user")
        return False
    return True


def _file_ok(path: Path, issues: list[str], *, required: bool) -> bool:
    if path.is_symlink():
        issues.append(f"{path.name} is a symlink")
        return False
    if not path.exists():
        if required:
            issues.append(f"{path.name} missing")
            return False
        return True
    try:
        st = path.stat()
    except OSError as exc:
        issues.append(f"cannot stat {path.name}: {exc}")
        return False
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o022:
        issues.append(f"{path.name} is group/world writable")
        return False
    if st.st_uid != os.getuid():
        issues.append(f"{path.name} owned by another user")
        return False
    return True


def ensure_writable_account(developer_dir: Path) -> None:
    """Fail fast if the developer directory cannot be secured/written."""
    try:
        developer_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DeveloperPermissionError(
            f"Could not create developer storage directory '{developer_dir}'.",
            hint="Check filesystem permissions for the data directory.",
        ) from exc
    if developer_dir.is_symlink():
        raise DeveloperStateError(
            "The developer storage directory is a symlink.",
            hint="Refusing credential storage behind a symlink.",
        )


__all__ = ["DevSecurityReport", "ensure_writable_account", "inspect_security"]
