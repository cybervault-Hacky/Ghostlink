"""Streaming chunk I/O and chunk bookkeeping (Phase 4).

Large files are never loaded whole:

* the sender reads chunk ``n`` on demand from an open descriptor (one short
  seek per chunk; sequential transfer is a plain linear read)
* the receiver writes each verified chunk straight into a ``.part`` file at
  its offset — out-of-order delivery needs zero memory buffering

:class:`ChunkBitmap` tracks received/acknowledged chunks for duplicate and
missing-chunk detection, and serializes to the resume bitmap carried by
FILE_ACCEPT.
"""

from __future__ import annotations

import base64
import binascii
from pathlib import Path
from types import TracebackType

from ghostlink.exceptions.transfer import TransferValidationError


class ChunkReader:
    """Read numbered chunks from one open file descriptor."""

    def __init__(self, path: Path, *, chunk_size: int, total_chunks: int) -> None:
        self._path = path
        self._chunk_size = chunk_size
        self._total_chunks = total_chunks
        self._handle = path.open("rb")
        self._cursor = 0  # current read offset in bytes

    def expected_length(self, n: int) -> int:
        """Plaintext length of chunk ``n`` (short only for the final chunk)."""

        if not 1 <= n <= self._total_chunks:
            raise TransferValidationError(
                f"Chunk number {n} is outside 1..{self._total_chunks}.",
                hint="Remote chunk numbers are validated before any disk access.",
            )
        file_size = self._path.stat().st_size
        if n == self._total_chunks:
            tail = file_size - (self._total_chunks - 1) * self._chunk_size
            return max(1, tail)
        return self._chunk_size

    def read(self, n: int) -> bytes:
        """Return plaintext chunk ``n`` (1-based), seeking when needed."""

        self.expected_length(n)
        offset = (n - 1) * self._chunk_size
        if offset != self._cursor:
            self._handle.seek(offset)
        data = self._handle.read(self._chunk_size)
        self._cursor = offset + len(data)
        if not data:
            raise TransferValidationError(
                f"Chunk {n} of '{self._path.name}' read back empty.",
                hint="The source file changed or was truncated mid-transfer. Cancel and resend.",
            )
        return data

    def close(self) -> None:
        self._handle.close()

    def __enter__(self) -> ChunkReader:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


class ChunkBitmap:
    """Bit-per-chunk received/acked set with resume (de)serialization."""

    def __init__(self, total_chunks: int, bitmap: bytes | None = None) -> None:
        if total_chunks < 1:
            raise TransferValidationError("Chunk bitmap needs at least one chunk.")
        self._total = total_chunks
        self._bits = bytearray((total_chunks + 7) // 8)
        self._count = 0
        if bitmap is not None:
            self._load(bitmap)

    def _load(self, bitmap: bytes) -> None:
        if len(bitmap) != len(self._bits):
            raise TransferValidationError(
                "Resume bitmap length does not match the chunk geometry.",
                hint="Re-offer the file; chunk geometry must be identical.",
            )
        self._bits = bytearray(bitmap)
        # Remote bitmaps are untrusted: mask padding bits beyond the last
        # chunk so they can never inflate the count or fake completeness.
        padding = len(self._bits) * 8 - self._total
        if padding:
            self._bits[-1] &= (1 << (8 - padding)) - 1
        self._count = sum(bin(byte).count("1") for byte in self._bits)

    @property
    def count(self) -> int:
        return self._count

    @property
    def total(self) -> int:
        return self._total

    def has(self, n: int) -> bool:
        return bool(self._bits[(n - 1) >> 3] & (1 << ((n - 1) & 7)))

    def mark(self, n: int) -> bool:
        """Mark chunk ``n`` present; returns False when it already was."""

        if not 1 <= n <= self._total:
            raise TransferValidationError(
                f"Chunk number {n} is outside 1..{self._total}.",
                hint="Reject out-of-range chunk numbers before touching state.",
            )
        if self.has(n):
            return False
        self._bits[(n - 1) >> 3] |= 1 << ((n - 1) & 7)
        self._count += 1
        return True

    @property
    def is_complete(self) -> bool:
        return self._count == self._total

    def missing(self) -> list[int]:
        return [n for n in range(1, self._total + 1) if not self.has(n)]

    def next_missing(self) -> int | None:
        """The smallest unreceived chunk number, or None when complete."""

        for n in range(1, self._total + 1):
            if not self.has(n):
                return n
        return None

    def to_b64(self) -> str:
        return base64.b64encode(bytes(self._bits)).decode("ascii")

    @classmethod
    def from_b64(cls, encoded: str, *, total_chunks: int) -> ChunkBitmap:
        try:
            raw = base64.b64decode(encoded.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise TransferValidationError(
                "Resume bitmap is not valid base64.",
                hint="The peer sent a malformed FILE_ACCEPT.",
            ) from exc
        return cls(total_chunks, bitmap=raw)


def chunk_plaintext_aad(transfer_id: str, n: int) -> bytes:
    """Associated data binding one sealed chunk to transfer id + number."""

    return f"{transfer_id}|{n}".encode("ascii")
