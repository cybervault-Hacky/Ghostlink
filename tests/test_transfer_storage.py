"""Safe filesystem handling: sanitization, destinations, temps (Phase 4)."""

from __future__ import annotations

from pathlib import Path

import pytest

from ghostlink.exceptions.storage import StorageError
from ghostlink.exceptions.transfer import TransferLimitError, TransferValidationError
from ghostlink.transfer.cleanup import cleanup_orphan_temps
from ghostlink.transfer.storage import (
    MAX_SAFE_NAME_LENGTH,
    TEMP_SUFFIX,
    TransferStorage,
    sanitize_filename,
)


class TestSanitizeFilename:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("report.pdf", "report.pdf"),
            ("my  notes.txt", "my notes.txt"),
            ("../../etc/passwd", "passwd"),
            ("..\\..\\windows\\system32\\cmd.exe", "cmd.exe"),
            ("a/b/c.txt", "c.txt"),
            ("\u2044evil\u2044file.bin", "file.bin"),  # unicode fraction slash
            ("\u2215evil\u2215file.bin", "file.bin"),  # unicode division slash
            ("   spaced.txt  ", "spaced.txt"),
            ("..hidden", "hidden"),
            ("CON", "_CON"),
            ("com1.txt", "_com1.txt"),
            ("LPT9", "_LPT9"),
            ("normal.CON.txt", "normal.CON.txt"),
        ],
    )
    def test_sanitization_matrix(self, raw: str, expected: str) -> None:
        assert sanitize_filename(raw) == expected

    @pytest.mark.parametrize("raw", ["", "...", "..", "/", "\\", "\x00", "\u2044"])
    def test_unusable_names_rejected(self, raw: str) -> None:
        assert sanitize_filename(raw) is None

    def test_control_characters_stripped(self) -> None:
        assert sanitize_filename("a\x01\x1fb.txt") == "ab.txt"

    def test_length_capped_with_suffix_kept(self) -> None:
        raw = "x" * 200 + ".pdf"
        safe = sanitize_filename(raw)
        assert safe is not None
        assert len(safe) <= MAX_SAFE_NAME_LENGTH
        assert safe.endswith(".pdf")

    def test_unicode_preserved(self) -> None:
        assert sanitize_filename("résumé ☕.pdf") == "résumé ☕.pdf"


@pytest.fixture()
def storage(tmp_path: Path, isolated_home: Path) -> TransferStorage:
    store = TransferStorage(tmp_path / "state")
    store.ensure_directories()
    return store


class TestTransferStorage:
    def test_default_download_dir_is_ghostlink_downloads(
        self, tmp_path: Path, isolated_home: Path
    ) -> None:
        store = TransferStorage(tmp_path / "state")
        assert store.download_dir == isolated_home / "Download" / "GhostLink"

    def test_configured_download_dir(self, tmp_path: Path) -> None:
        target = tmp_path / "custom"
        store = TransferStorage(tmp_path / "state", download_dir=str(target))
        assert store.download_dir == target

    def test_directories_are_private(self, storage: TransferStorage) -> None:
        assert storage.temp_dir.is_dir()
        assert storage.download_dir.is_dir()
        assert (storage.temp_dir.stat().st_mode & 0o777) == 0o700
        assert (storage.download_dir.stat().st_mode & 0o777) == 0o700

    def test_temp_path_rejects_hostile_ids(self, storage: TransferStorage) -> None:
        with pytest.raises(TransferValidationError):
            storage.temp_path("../escape")
        with pytest.raises(TransferValidationError):
            storage.temp_path("a/b")
        with pytest.raises(TransferValidationError):
            storage.temp_path("")

    def test_open_temp_is_private(self, storage: TransferStorage) -> None:
        path = storage.open_temp("tf_ab12cd34")
        assert path.name == "tf_ab12cd34.part"
        assert (path.stat().st_mode & 0o777) == 0o600
        assert path.parent == storage.temp_dir

    def test_write_chunk_places_bytes_at_offsets(self, storage: TransferStorage) -> None:
        storage.open_temp("tf_ab12cd34")
        storage.write_chunk("tf_ab12cd34", 2, 4, b"EFGH")
        storage.write_chunk("tf_ab12cd34", 1, 4, b"ABCD")
        path = storage.temp_path("tf_ab12cd34")
        assert path.read_bytes()[:8] == b"ABCDEFGH"

    def test_destination_never_overwrites(self, storage: TransferStorage) -> None:
        first = storage.destination_for("song.mp3")
        first.write_bytes(b"one")
        second = storage.destination_for("song.mp3")
        assert second != first
        assert second.name == "song (2).mp3"
        second.write_bytes(b"two")
        third = storage.destination_for("song.mp3")
        assert third.name == "song (3).mp3"

    def test_destination_collision_with_existing_numbered_names(
        self, storage: TransferStorage
    ) -> None:
        first = storage.destination_for("song.mp3")
        first.write_bytes(b"one")
        (storage.download_dir / "song (2).mp3").write_bytes(b"taken")
        assert storage.destination_for("song.mp3").name == "song (3).mp3"

    def test_destination_stays_inside_download_dir(self, storage: TransferStorage) -> None:
        for hostile in ("../../etc/cron.d/x", "/etc/passwd", "..\\..\\evil"):
            path = storage.destination_for(hostile)
            assert storage.download_dir.resolve() in path.resolve().parents

    def test_unusable_remote_name_falls_back(self, storage: TransferStorage) -> None:
        assert storage.destination_for("..").name == "ghostlink-file"
        assert storage.destination_for("\u2044").name == "ghostlink-file"

    def test_finalize_moves_atomically_and_privately(self, storage: TransferStorage) -> None:
        storage.open_temp("tf_ab12cd34")
        storage.write_chunk("tf_ab12cd34", 1, 4, b"DATA")
        destination = storage.destination_for("out.bin")
        saved = storage.finalize("tf_ab12cd34", destination)
        assert saved.read_bytes() == b"DATA"
        assert (saved.stat().st_mode & 0o777) == 0o600
        assert not storage.temp_path("tf_ab12cd34").exists()

    def test_finalize_missing_temp_fails(self, storage: TransferStorage) -> None:
        with pytest.raises(StorageError, match="missing"):
            storage.finalize("tf_ab12cd34", storage.destination_for("out.bin"))

    def test_finalize_refuses_escape(self, storage: TransferStorage) -> None:
        storage.open_temp("tf_ab12cd34")
        with pytest.raises(TransferValidationError, match="outside"):
            storage.finalize("tf_ab12cd34", Path("/tmp/ghostlink-escape"))

    def test_temp_usage_and_quota(self, storage: TransferStorage) -> None:
        storage.open_temp("tf_00000001")
        storage.write_chunk("tf_00000001", 1, 1024, b"x" * 1024)
        assert storage.temp_usage_bytes() == 1024
        storage.check_quota(512, limit_bytes=2048)
        with pytest.raises(TransferLimitError):
            storage.check_quota(2048, limit_bytes=2048)

    def test_delete_temp_is_quiet_when_missing(self, storage: TransferStorage) -> None:
        storage.delete_temp("tf_0000000f")  # must not raise


class TestOrphanCleanup:
    def test_orphans_removed_and_live_temps_kept(self, storage: TransferStorage) -> None:
        storage.open_temp("tf_00000001")
        storage.open_temp("tf_00000002")
        stray = storage.temp_dir / "notes.txt"  # not a transfer temp
        stray.write_text("keep me")
        removed = cleanup_orphan_temps(storage.temp_dir, active_ids=frozenset({"tf_00000001"}))
        assert removed == 1
        assert storage.temp_path("tf_00000001").exists()
        assert not storage.temp_path("tf_00000002").exists()
        assert stray.exists()

    def test_all_leftovers_removed_on_startup(self, storage: TransferStorage) -> None:
        storage.open_temp("tf_00000001")
        storage.open_temp("tf_00000002")
        removed = cleanup_orphan_temps(storage.temp_dir)
        assert removed == 2

    def test_missing_dir_is_noop(self, tmp_path: Path) -> None:
        assert cleanup_orphan_temps(tmp_path / "nope") == 0

    def test_temp_suffix_constant(self) -> None:
        assert TEMP_SUFFIX == ".part"
