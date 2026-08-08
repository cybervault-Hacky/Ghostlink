"""Safe filesystem handling for transfers (Phase 4).

The remote peer is never trusted:

* filenames are sanitized (basename rules, no separators, no control chars,
  no dot-files, length cap) — never any path from the wire
* destinations resolve inside the dedicated GhostLink download directory,
  verified after resolution (no traversal, no absolute-path writes)
* existing files are never silently overwritten — a unique ``name (2).ext``
  style destination is generated
* temp ``.part`` files live only in the controlled transfer directory and
  become visible downloads exclusively after integrity verification, via an
  atomic rename
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path

from ghostlink.exceptions.storage import StorageError
from ghostlink.exceptions.transfer import TransferLimitError, TransferValidationError

TRANSFER_TEMP_DIR_NAME: str = "transfers"
DEFAULT_DOWNLOAD_SUBDIR: str = "Download/GhostLink"
TEMP_SUFFIX: str = ".part"
MAX_SAFE_NAME_LENGTH: int = 120
_COLLISION_PATTERN = re.compile(r"^(?P<stem>.*?)(?: \((?P<n>\d+)\))?(?P<suffix>\.[^.]*)?$")

CONTROL_CHARS = frozenset(chr(code) for code in range(32)) | {chr(127)}


def sanitize_filename(name: str) -> str | None:
    """Reduce a peer-supplied name to a safe basename, or None if unusable.

    Strips any directory components (both separator styles and unicode
    variants), control characters, and leading dots; normalizes whitespace.
    """

    # Take the basename under every separator convention up front, including
    # the unicode path-separator lookalikes U+2044 and U+2215.
    for separator in ("\\", "/", "\u2044", "\u2215"):
        name = name.rsplit(separator, 1)[-1]
    name = unicodedata.normalize("NFC", name)
    name = "".join(char for char in name if char not in CONTROL_CHARS and char.isprintable())
    name = name.strip().strip(".")
    name = re.sub(r"\s+", " ", name)
    if not name or name in (".", ".."):
        return None
    reserved = {
        "con",
        "prn",
        "aux",
        "nul",
        *(f"com{i}" for i in range(1, 10)),
        *(f"lpt{i}" for i in range(1, 10)),
    }
    stem = name.split(".", 1)[0]
    if stem.lower() in reserved:
        name = f"_{name}"
    if len(name) > MAX_SAFE_NAME_LENGTH:
        suffix = Path(name).suffix[:10]
        name = name[: MAX_SAFE_NAME_LENGTH - len(suffix)].rstrip(". ") + suffix
    return name


class TransferStorage:
    """Owns the temp area and the download directory for one app run."""

    def __init__(self, state_dir: Path, *, download_dir: str = "") -> None:
        self._temp_dir = state_dir / TRANSFER_TEMP_DIR_NAME
        configured = download_dir.strip()
        if configured:
            self._download_dir = Path(configured).expanduser()
        else:
            self._download_dir = Path.home() / DEFAULT_DOWNLOAD_SUBDIR

    @property
    def temp_dir(self) -> Path:
        return self._temp_dir

    @property
    def download_dir(self) -> Path:
        return self._download_dir

    def ensure_directories(self) -> None:
        try:
            self._temp_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self._temp_dir, 0o700)
            self._download_dir.mkdir(parents=True, exist_ok=True)
            os.chmod(self._download_dir, 0o700)
        except OSError as exc:
            raise StorageError(
                f"Could not create transfer directories: {exc.strerror or exc}.",
                hint="Check that the state and download locations are writable.",
            ) from exc

    # ------------------------------------------------------------- temp files

    def temp_path(self, transfer_id: str) -> Path:
        if not transfer_id or "/" in transfer_id or ".." in transfer_id:
            raise TransferValidationError(
                f"Transfer id '{transfer_id}' cannot name a temp file.",
                hint="Temp paths are derived from validated transfer ids only.",
            )
        path = (self._temp_dir / f"{transfer_id}{TEMP_SUFFIX}").resolve()
        if self._temp_dir.resolve() not in path.parents:
            raise TransferValidationError(
                "Temp path escaped the transfer directory.",
                hint="This is a GhostLink defect; please report it.",
            )
        return path

    def open_temp(self, transfer_id: str) -> Path:
        """Create (or reuse) the ``.part`` file for an incoming transfer."""

        self.ensure_directories()
        path = self.temp_path(transfer_id)
        fd = os.open(path, os.O_CREAT | os.O_WRONLY, 0o600)
        os.close(fd)
        return path

    def write_chunk(self, transfer_id: str, n: int, chunk_size: int, data: bytes) -> Path:
        """Write one verified chunk at its offset inside the ``.part`` file."""

        path = self.temp_path(transfer_id)
        try:
            with path.open("r+b") as handle:
                handle.seek((n - 1) * chunk_size)
                handle.write(data)
        except OSError as exc:
            raise StorageError(
                f"Could not write to the temp file '{path.name}'.",
                hint="Check free space and permissions in the state directory.",
            ) from exc
        return path

    def temp_usage_bytes(self) -> int:
        """Total bytes currently held in transfer temp files."""

        if not self._temp_dir.is_dir():
            return 0
        total = 0
        for entry in self._temp_dir.iterdir():
            if entry.suffix == TEMP_SUFFIX and entry.is_file():
                total += entry.stat().st_size
        return total

    def check_quota(self, incoming_size: int, *, limit_bytes: int) -> None:
        """Reject an offer that would exceed the temp storage limit."""

        projected = self.temp_usage_bytes() + incoming_size
        if projected > limit_bytes:
            raise TransferLimitError(
                f"Accepting {incoming_size} bytes would exceed the temp storage "
                f"limit ({limit_bytes} bytes).",
                hint="Free up downloads, raise transfer.temp_storage_limit_mb, "
                "or ask the peer for a smaller file.",
            )

    def delete_temp(self, transfer_id: str) -> None:
        try:
            self.temp_path(transfer_id).unlink(missing_ok=True)
        except OSError as exc:
            raise StorageError(
                f"Could not delete the temp file for transfer {transfer_id}.",
                hint="Remove it manually from the state/transfers directory.",
            ) from exc

    # ------------------------------------------------------------ destinations

    def destination_for(self, filename: str) -> Path:
        """A unique, traversal-proof destination inside the download dir."""

        safe = sanitize_filename(filename)
        if safe is None:
            safe = "ghostlink-file"
        self.ensure_directories()
        base = self._download_dir.resolve()
        candidate = (base / safe).resolve()
        if base not in candidate.parents and candidate != base:
            raise TransferValidationError(
                f"Refusing destination outside the download directory: '{safe}'.",
                hint="The peer sent a hostile filename; it was sanitized.",
            )
        if not candidate.exists():
            return candidate
        # Never overwrite: find the first free "name (n).ext" variant.
        match = _COLLISION_PATTERN.match(safe)
        stem = match.group("stem") if match else safe
        suffix = match.group("suffix") if match else ""
        for number in range(2, 100):
            variant = (base / f"{stem} ({number}){suffix or ''}").resolve()
            if (base in variant.parents or variant == base) and not variant.exists():
                return variant
        raise StorageError(
            f"Too many files named '{safe}' in the download directory.",
            hint="Rename or clean up older downloads to make room.",
        )

    def finalize(self, transfer_id: str, destination: Path) -> Path:
        """Atomically promote the verified ``.part`` file to its destination."""

        temp = self.temp_path(transfer_id)
        if not temp.is_file():
            raise StorageError(
                "The completed temp file is missing.",
                hint="The transfer cannot be finalized; request the file again.",
            )
        resolved = destination.resolve()
        base = self._download_dir.resolve()
        if base not in resolved.parents:
            raise TransferValidationError(
                "Refusing to finalize outside the download directory.",
                hint="Destinations are always resolved inside GhostLink downloads.",
            )
        try:
            os.replace(temp, resolved)
            os.chmod(resolved, 0o600)
        except OSError as exc:
            raise StorageError(
                f"Could not move the completed download into place: {exc.strerror or exc}.",
                hint="Check that the download directory is writable.",
            ) from exc
        return resolved
