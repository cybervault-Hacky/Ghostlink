"""Chunk streaming, resume bitmaps, and AAD binding (Phase 4)."""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from ghostlink.exceptions.transfer import TransferValidationError
from ghostlink.transfer.chunking import ChunkBitmap, ChunkReader, chunk_plaintext_aad


class TestChunkReader:
    def _file(self, tmp_path: Path, size: int = 10_000) -> Path:
        path = tmp_path / "data.bin"
        path.write_bytes(bytes(index % 251 for index in range(size)))
        return path

    def test_sequential_read(self, tmp_path: Path) -> None:
        path = self._file(tmp_path)
        data = path.read_bytes()
        with ChunkReader(path, chunk_size=1024, total_chunks=10) as reader:
            chunks = [reader.read(n) for n in range(1, 11)]
        assert b"".join(chunks) == data
        assert all(len(chunk) == 1024 for chunk in chunks[:-1])
        assert len(chunks[-1]) == 10_000 - 9 * 1024

    def test_random_access_via_seek(self, tmp_path: Path) -> None:
        path = self._file(tmp_path)
        data = path.read_bytes()
        with ChunkReader(path, chunk_size=1024, total_chunks=10) as reader:
            assert reader.read(5) == data[4 * 1024 : 5 * 1024]
            assert reader.read(2) == data[1024 : 2 * 1024]
            assert reader.read(10) == data[9 * 1024 :]

    def test_out_of_range_chunk_numbers_rejected(self, tmp_path: Path) -> None:
        path = self._file(tmp_path)
        with ChunkReader(path, chunk_size=1024, total_chunks=10) as reader:
            with pytest.raises(TransferValidationError, match="outside"):
                reader.read(0)
            with pytest.raises(TransferValidationError, match="outside"):
                reader.read(11)
            with pytest.raises(TransferValidationError, match="outside"):
                reader.expected_length(-3)

    def test_expected_lengths(self, tmp_path: Path) -> None:
        path = self._file(tmp_path, size=2500)
        with ChunkReader(path, chunk_size=1024, total_chunks=3) as reader:
            assert reader.expected_length(1) == 1024
            assert reader.expected_length(2) == 1024
            assert reader.expected_length(3) == 452

    def test_truncated_source_detected(self, tmp_path: Path) -> None:
        path = self._file(tmp_path)
        with ChunkReader(path, chunk_size=1024, total_chunks=10) as reader:
            path.write_bytes(b"")
            with pytest.raises(TransferValidationError, match="empty"):
                reader.read(5)


class TestChunkBitmap:
    def test_mark_has_missing(self) -> None:
        bitmap = ChunkBitmap(5)
        assert bitmap.next_missing() == 1
        assert bitmap.mark(3)
        assert bitmap.mark(1)
        assert bitmap.missing() == [2, 4, 5]
        assert bitmap.next_missing() == 2
        assert bitmap.count == 2
        assert not bitmap.is_complete

    def test_duplicate_mark_returns_false(self) -> None:
        bitmap = ChunkBitmap(3)
        assert bitmap.mark(2)
        assert not bitmap.mark(2)
        assert bitmap.count == 1

    def test_out_of_range_mark_rejected(self) -> None:
        bitmap = ChunkBitmap(3)
        with pytest.raises(TransferValidationError, match="outside"):
            bitmap.mark(0)
        with pytest.raises(TransferValidationError, match="outside"):
            bitmap.mark(4)

    def test_completion(self) -> None:
        bitmap = ChunkBitmap(4)
        for n in (1, 2, 3):
            bitmap.mark(n)
        assert not bitmap.is_complete
        bitmap.mark(4)
        assert bitmap.is_complete
        assert bitmap.missing() == []
        assert bitmap.next_missing() is None

    def test_b64_round_trip(self) -> None:
        bitmap = ChunkBitmap(20)
        for n in (1, 2, 8, 9, 16, 20):
            bitmap.mark(n)
        clone = ChunkBitmap.from_b64(bitmap.to_b64(), total_chunks=20)
        assert clone.count == 6
        assert clone.missing() == bitmap.missing()
        raw = base64.b64decode(bitmap.to_b64())
        assert len(raw) == 3  # ceil(20/8)

    def test_geometry_mismatch_rejected(self) -> None:
        bitmap = ChunkBitmap(9)  # 2 bytes
        with pytest.raises(TransferValidationError, match="geometry"):
            ChunkBitmap.from_b64(bitmap.to_b64(), total_chunks=8)  # wants 1 byte

    def test_malformed_base64_rejected(self) -> None:
        with pytest.raises(TransferValidationError, match="base64"):
            ChunkBitmap.from_b64("&&&%%%", total_chunks=8)

    def test_bits_do_not_bleed_into_padding_bit_count(self) -> None:
        clone = ChunkBitmap.from_b64(ChunkBitmap(5).to_b64(), total_chunks=5)
        assert clone.count == 0
        assert not clone.is_complete

    def test_remote_padding_bits_are_masked(self) -> None:
        # 0xE0 has every padding bit set but no real chunk bit — a hostile
        # bitmap must not inflate the count or fake completeness.
        hostile = base64.b64encode(bytes([0xE0])).decode()
        clone = ChunkBitmap.from_b64(hostile, total_chunks=5)
        assert clone.count == 0
        assert not clone.is_complete
        assert clone.missing() == [1, 2, 3, 4, 5]


def test_chunk_aad_binds_id_and_number() -> None:
    assert chunk_plaintext_aad("tf_ab12cd34", 7) == b"tf_ab12cd34|7"
    assert chunk_plaintext_aad("tf_ab12cd34", 7) != chunk_plaintext_aad("tf_ab12cd34", 8)
