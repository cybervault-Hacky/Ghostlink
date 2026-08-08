"""Storage-related failures."""

from __future__ import annotations

from ghostlink.exceptions.base import ExitCode, GhostLinkError


class StorageError(GhostLinkError):
    """A storage backend could not complete an operation."""

    exit_code = ExitCode.STORAGE
    error_title = "Storage error"


class StorageReadError(StorageError):
    """A value or document could not be read from a storage backend."""

    error_title = "Storage read error"


class StorageWriteError(StorageError):
    """A value or document could not be persisted by a storage backend."""

    error_title = "Storage write error"


class StorageCorruptionError(StorageReadError):
    """A storage document exists but is malformed or has an invalid shape."""

    error_title = "Corrupt storage document"
