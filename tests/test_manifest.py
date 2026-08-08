"""File manifest: construction, serialization, validation (Phase 4)."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from ghostlink.constants.app import TRANSFER_MAX_CHUNKS_PER_FILE
from ghostlink.exceptions.transfer import TransferValidationError
from ghostlink.transfer.manifest import (
    MAX_MANIFEST_FILENAME_LENGTH,
    FileManifest,
    guess_mime,
)

LIMITS = {
    "max_file_size_bytes": 100 * 1024 * 1024,
    "max_chunk_size": 4096,
    "min_chunk_size": 1024,
}


def _manifest(**overrides: object) -> FileManifest:
    fields: dict[str, object] = {
        "protocol": "gf1",
        "transfer_id": "tf_ab12cd34",
        "filename": "report.pdf",
        "size_bytes": 10_000,
        "mime": "application/pdf",
        "chunk_size": 1024,
        "total_chunks": 10,  # ceil(10000/1024)
        "sha256": "ab" * 32,
    }
    fields.update(overrides)
    return FileManifest(**fields)  # type: ignore[arg-type]


class TestFromFile:
    def test_streams_hash_size_and_geometry(self, tmp_path: Path) -> None:
        payload = bytes(range(256)) * 20  # 5120 bytes
        source = tmp_path / "photo.jpg"
        source.write_bytes(payload)
        manifest = FileManifest.from_file(
            source, transfer_id="tf_ab12cd34", safe_name="photo.jpg", chunk_size=1024
        )
        assert manifest.size_bytes == 5120
        assert manifest.total_chunks == 5
        assert manifest.chunk_size == 1024
        assert manifest.mime == "image/jpeg"
        assert manifest.sha256 == hashlib.sha256(payload).hexdigest()
        assert manifest.protocol == "gf1"

    def test_empty_file_rejected(self, tmp_path: Path) -> None:
        source = tmp_path / "empty.txt"
        source.write_bytes(b"")
        with pytest.raises(TransferValidationError, match="empty"):
            FileManifest.from_file(
                source, transfer_id="tf_ab12cd34", safe_name="empty.txt", chunk_size=1024
            )

    def test_missing_file_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(TransferValidationError, match="stat"):
            FileManifest.from_file(
                tmp_path / "ghost.bin",
                transfer_id="tf_ab12cd34",
                safe_name="ghost.bin",
                chunk_size=1024,
            )


class TestSerialization:
    def test_json_round_trip(self) -> None:
        manifest = _manifest()
        clone = FileManifest.from_json(manifest.to_json())
        assert clone == manifest

    def test_manifest_contains_no_local_paths(self, tmp_path: Path) -> None:
        secret_dir = tmp_path / "home-user-secret"
        secret_dir.mkdir()
        source = secret_dir / "report.pdf"
        source.write_bytes(b"payload")
        manifest = FileManifest.from_file(
            source, transfer_id="tf_ab12cd34", safe_name="report.pdf", chunk_size=1024
        )
        document = manifest.to_json().decode("utf-8")
        assert str(secret_dir) not in document
        assert "tmp" not in manifest.filename
        assert set(json.loads(document)) == {
            "v",
            "id",
            "name",
            "size",
            "mime",
            "cs",
            "chunks",
            "sha256",
        }

    @pytest.mark.parametrize(
        "raw",
        [b"not json", b"[1,2]", b'{"v":"gf1"}', b'{"v":1,"id":"tf_a"}'],
    )
    def test_malformed_json_rejected(self, raw: bytes) -> None:
        with pytest.raises(TransferValidationError):
            FileManifest.from_json(raw)


class TestValidation:
    def test_valid_manifest_passes(self) -> None:
        _manifest().validate(**LIMITS)  # type: ignore[arg-type]

    def test_wrong_protocol_rejected(self) -> None:
        with pytest.raises(TransferValidationError, match="protocol"):
            _manifest(protocol="gf2").validate(**LIMITS)  # type: ignore[arg-type]

    def test_malformed_transfer_id_rejected(self) -> None:
        with pytest.raises(TransferValidationError, match="transfer id"):
            _manifest(transfer_id="nope").validate(**LIMITS)  # type: ignore[arg-type]

    @pytest.mark.parametrize("name", ["../etc/passwd", "a/b.txt", "\\win\\x", "..", "."])
    def test_path_components_rejected(self, name: str) -> None:
        with pytest.raises(TransferValidationError, match="path"):
            _manifest(filename=name).validate(**LIMITS)  # type: ignore[arg-type]

    def test_overlong_and_control_filenames_rejected(self) -> None:
        with pytest.raises(TransferValidationError):
            _manifest(filename="x" * (MAX_MANIFEST_FILENAME_LENGTH + 1)).validate(**LIMITS)  # type: ignore[arg-type]
        with pytest.raises(TransferValidationError):
            _manifest(filename="a\x01b").validate(**LIMITS)  # type: ignore[arg-type]

    def test_oversize_file_rejected(self) -> None:
        with pytest.raises(TransferValidationError, match="limit"):
            _manifest(size_bytes=LIMITS["max_file_size_bytes"] + 1).validate(**LIMITS)  # type: ignore[arg-type]

    def test_zero_size_rejected(self) -> None:
        with pytest.raises(TransferValidationError):
            _manifest(size_bytes=0, total_chunks=0).validate(**LIMITS)  # type: ignore[arg-type]

    def test_chunk_size_bounds_enforced(self) -> None:
        with pytest.raises(TransferValidationError, match="chunk size"):
            _manifest(chunk_size=8192, total_chunks=2).validate(**LIMITS)  # type: ignore[arg-type]
        with pytest.raises(TransferValidationError, match="chunk size"):
            _manifest(chunk_size=512, total_chunks=20).validate(**LIMITS)  # type: ignore[arg-type]

    def test_chunk_count_must_match_geometry(self) -> None:
        with pytest.raises(TransferValidationError, match="chunk count"):
            _manifest(total_chunks=7).validate(**LIMITS)  # type: ignore[arg-type]

    def test_chunk_count_above_bitmap_capacity_rejected(self) -> None:
        too_many = TRANSFER_MAX_CHUNKS_PER_FILE + 1
        with pytest.raises(TransferValidationError, match="bitmap"):
            _manifest(
                size_bytes=too_many * 1024,  # geometry stays self-consistent
                chunk_size=1024,
                total_chunks=too_many,
            ).validate(**LIMITS)  # type: ignore[arg-type]

    def test_malformed_hash_rejected(self) -> None:
        with pytest.raises(TransferValidationError, match="hex"):
            _manifest(sha256="zz" * 32).validate(**LIMITS)  # type: ignore[arg-type]
        with pytest.raises(TransferValidationError, match="64"):
            _manifest(sha256="abcd").validate(**LIMITS)  # type: ignore[arg-type]


class TestMime:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("report.pdf", "application/pdf"),
            ("photo.jpg", "image/jpeg"),
            ("notes.txt", "text/plain"),
            ("no-extension", "application/octet-stream"),
        ],
    )
    def test_common_types(self, name: str, expected: str) -> None:
        assert guess_mime(name) == expected

    def test_mime_is_capped(self) -> None:
        assert len(guess_mime("x.weird-extension")) <= 100
