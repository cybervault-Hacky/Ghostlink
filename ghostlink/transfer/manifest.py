"""File manifest (Phase 4).

The manifest is the *only* metadata the sender reveals, and it travels
sealed under the per-transfer key — the relay sees neither it nor any path.
It deliberately contains no local filesystem information: just the name the
file will be saved under, its size, chunk geometry, integrity hash, and the
protocol version.
"""

from __future__ import annotations

import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from ghostlink.constants.app import TRANSFER_MAX_CHUNKS_PER_FILE
from ghostlink.exceptions.transfer import TransferValidationError
from ghostlink.transfer.integrity import hash_file
from ghostlink.transfer.models import TRANSFER_PROTOCOL, is_valid_transfer_id

MAX_MANIFEST_FILENAME_LENGTH: int = 120
MAX_MANIFEST_MIME_LENGTH: int = 100


def _fail(reason: str) -> NoReturn:
    raise TransferValidationError(
        f"File manifest rejected: {reason}.",
        hint="The peer sent metadata outside transfer protocol gf1.",
    )


@dataclass(frozen=True, slots=True)
class FileManifest:
    """Everything the receiver needs to reassemble and verify one file."""

    protocol: str
    transfer_id: str
    filename: str  # already sanitized by the sender's storage layer
    size_bytes: int
    mime: str
    chunk_size: int
    total_chunks: int
    sha256: str

    # ------------------------------------------------------------ construction

    @classmethod
    def from_file(
        cls,
        path: Path,
        *,
        transfer_id: str,
        safe_name: str,
        chunk_size: int,
    ) -> FileManifest:
        """Build a manifest by streaming the file once (hash + size)."""

        try:
            size = path.stat().st_size
        except OSError as exc:
            raise TransferValidationError(
                f"Could not stat '{path.name}': {exc.strerror or exc}.",
                hint="Check that the file still exists and is readable.",
            ) from exc
        if size < 1:
            raise TransferValidationError(
                f"'{path.name}' is empty.",
                hint="GhostLink transfers files of at least one byte.",
            )
        sha256 = hash_file(path)
        mime = guess_mime(safe_name)
        total_chunks = (size + chunk_size - 1) // chunk_size
        return cls(
            protocol=TRANSFER_PROTOCOL,
            transfer_id=transfer_id,
            filename=safe_name,
            size_bytes=size,
            mime=mime,
            chunk_size=chunk_size,
            total_chunks=total_chunks,
            sha256=sha256,
        )

    # ----------------------------------------------------------- serialization

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": self.protocol,
            "id": self.transfer_id,
            "name": self.filename,
            "size": self.size_bytes,
            "mime": self.mime,
            "cs": self.chunk_size,
            "chunks": self.total_chunks,
            "sha256": self.sha256,
        }

    def to_json(self) -> bytes:
        return json.dumps(self.to_dict(), separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_json(cls, raw: bytes) -> FileManifest:
        try:
            document = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise TransferValidationError(
                "The sealed manifest is not valid JSON.",
                hint="The peer is not running transfer protocol gf1.",
            ) from exc
        if not isinstance(document, dict):
            _fail("manifest must be a JSON object")
        try:
            return cls(
                protocol=str(document["v"]),
                transfer_id=str(document["id"]),
                filename=str(document["name"]),
                size_bytes=int(document["size"]),
                mime=str(document["mime"]),
                chunk_size=int(document["cs"]),
                total_chunks=int(document["chunks"]),
                sha256=str(document["sha256"]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            _fail(f"manifest is missing or mis-typing fields ({exc})")

    # -------------------------------------------------------------- validation

    def validate(
        self,
        *,
        max_file_size_bytes: int,
        max_chunk_size: int,
        min_chunk_size: int,
    ) -> None:
        """Semantic validation against configured limits and cross-checks."""

        if self.protocol != TRANSFER_PROTOCOL:
            _fail(f"unsupported protocol version '{self.protocol}'")
        if not is_valid_transfer_id(self.transfer_id):
            _fail("transfer id is malformed")
        if not (0 < len(self.filename) <= MAX_MANIFEST_FILENAME_LENGTH):
            _fail("filename length is out of bounds")
        if any(ord(char) < 32 or ord(char) == 127 for char in self.filename):
            _fail("filename contains control characters")
        if "/" in self.filename or "\\" in self.filename or self.filename in (".", ".."):
            _fail("filename contains path components")
        if not (1 <= self.size_bytes <= max_file_size_bytes):
            _fail(f"size {self.size_bytes} exceeds the limit of {max_file_size_bytes} bytes")
        if not (min_chunk_size <= self.chunk_size <= max_chunk_size):
            _fail(f"chunk size {self.chunk_size} is outside the negotiated range")
        expected_chunks = (self.size_bytes + self.chunk_size - 1) // self.chunk_size
        if self.total_chunks != expected_chunks:
            _fail("total chunk count does not match size and chunk geometry")
        if self.total_chunks > TRANSFER_MAX_CHUNKS_PER_FILE:
            _fail("file needs more chunks than the resume bitmap can carry")
        if len(self.mime) > MAX_MANIFEST_MIME_LENGTH or any(
            ord(char) < 32 or ord(char) == 127 for char in self.mime
        ):
            _fail("mime information is malformed")
        if len(self.sha256) != 64:
            _fail("integrity hash must be 64 hex characters")
        try:
            bytes.fromhex(self.sha256)
        except ValueError:
            _fail("integrity hash is not hexadecimal")


def guess_mime(filename: str) -> str:
    """Best-effort type hint from the filename; never leaks anything local."""

    guessed, _ = mimetypes.guess_type(filename)
    if not guessed:
        return "application/octet-stream"
    return guessed[:MAX_MANIFEST_MIME_LENGTH]
