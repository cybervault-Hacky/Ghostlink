"""History modes: disabled, session, encrypted storage (Phase 3)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from ghostlink.exceptions.messaging import HistoryError, HistoryPassphraseError
from ghostlink.messaging.history import (
    EncryptedHistory,
    HistoryEntry,
    HistoryMode,
    NullHistory,
    SessionHistory,
    export_file_name,
    format_timestamp,
    open_history,
    parse_history_mode,
)
from ghostlink.messaging.models.message import (
    Message,
    MessageDirection,
    MessageStatus,
    generate_message_id,
)


def _message(text: str, *, direction: MessageDirection = MessageDirection.OUTGOING) -> Message:
    outgoing = direction is MessageDirection.OUTGOING
    status = MessageStatus.SENT if outgoing else MessageStatus.DELIVERED
    return Message(
        message_id=generate_message_id(),
        conversation_id="conv_0123456789ab",
        direction=direction,
        sender="Nova" if direction is MessageDirection.OUTGOING else "Ravi",
        recipient="Ravi" if direction is MessageDirection.OUTGOING else "Nova",
        text=text,
        sequence=1,
        status=status,
        created_at=1_780_000_000.0,
    )


class TestModes:
    def test_parse_modes(self) -> None:
        assert parse_history_mode("disabled") is HistoryMode.DISABLED
        assert parse_history_mode(" Session ") is HistoryMode.SESSION
        assert parse_history_mode("encrypted") is HistoryMode.ENCRYPTED

    def test_unknown_mode_rejected(self) -> None:
        with pytest.raises(HistoryError, match="Unknown history mode"):
            parse_history_mode("forever")

    def test_open_history_selects_backend(self, tmp_path: Path) -> None:
        assert isinstance(open_history(HistoryMode.DISABLED, state_dir=tmp_path), NullHistory)
        assert isinstance(open_history(HistoryMode.SESSION, state_dir=tmp_path), SessionHistory)
        encrypted = open_history(HistoryMode.ENCRYPTED, state_dir=tmp_path, passphrase="pw")
        assert isinstance(encrypted, EncryptedHistory)


class TestNullHistory:
    def test_records_nothing(self) -> None:
        history = NullHistory()
        history.record(_message("gone"))
        assert len(history) == 0
        assert history.enabled is False
        assert history.export_text() == ""


class TestSessionHistory:
    def test_records_and_wipes(self) -> None:
        history = SessionHistory()
        history.record(_message("one"))
        history.record(_message("two"))
        assert len(history) == 2
        history.close()
        assert len(history) == 0

    def test_export_renders_blocks(self) -> None:
        history = SessionHistory()
        history.record(_message("hello export"))
        text = history.export_text(timestamp_format="24h")
        assert "hello export" in text
        assert "Nova:" in text
        assert "[" in text  # timestamps

    def test_write_export(self, tmp_path: Path) -> None:
        history = SessionHistory()
        history.record(_message("exported"))
        path = history.write_export(tmp_path / "out.txt")
        content = path.read_text(encoding="utf-8")
        assert "exported" in content

    def test_write_export_failure(self, tmp_path: Path) -> None:
        history = SessionHistory()
        history.record(_message("x"))
        with pytest.raises(HistoryError, match="export"):
            history.write_export(tmp_path / "missing" / "deep" / "out.txt")


class TestEncryptedHistory:
    def test_round_trip_across_restarts(self, tmp_path: Path) -> None:
        path = tmp_path / "chat-history.gle"
        first = EncryptedHistory.open(path, passphrase="s3cret")
        first.record(_message("persist me"))
        first.close()

        second = EncryptedHistory.open(path, passphrase="s3cret")
        entries = second.entries()
        assert [entry.text for entry in entries] == ["persist me"]
        second.close()

    def test_plaintext_never_touches_disk(self, tmp_path: Path) -> None:
        path = tmp_path / "chat-history.gle"
        history = EncryptedHistory.open(path, passphrase="pw")
        history.record(_message("classified content"))
        blob = path.read_bytes()
        assert b"classified content" not in blob
        history.close()

    def test_wrong_passphrase_fails_clearly(self, tmp_path: Path) -> None:
        path = tmp_path / "chat-history.gle"
        EncryptedHistory.open(path, passphrase="right").close()
        with pytest.raises(HistoryPassphraseError, match="passphrase"):
            EncryptedHistory.open(path, passphrase="wrong")

    def test_corrupt_file_fails_clearly(self, tmp_path: Path) -> None:
        path = tmp_path / "chat-history.gle"
        path.write_bytes(b"garbage")
        with pytest.raises(HistoryError, match="not a GhostLink history"):
            EncryptedHistory.open(path, passphrase="pw")

    def test_tampered_ciphertext_fails_clearly(self, tmp_path: Path) -> None:
        path = tmp_path / "chat-history.gle"
        history = EncryptedHistory.open(path, passphrase="pw")
        history.record(_message("integrity"))
        blob = bytearray(path.read_bytes())
        blob[-1] ^= 0x01
        path.write_bytes(bytes(blob))
        with pytest.raises(HistoryPassphraseError):
            EncryptedHistory.open(path, passphrase="pw")

    def test_file_permissions_are_owner_only(self, tmp_path: Path) -> None:
        path = tmp_path / "chat-history.gle"
        history = EncryptedHistory.open(path, passphrase="pw")
        history.record(_message("perm check"))
        mode = stat.S_IMODE(os.stat(path).st_mode)
        assert mode == 0o600
        history.close()

    def test_wipe_removes_everything(self, tmp_path: Path) -> None:
        path = tmp_path / "chat-history.gle"
        history = EncryptedHistory.open(path, passphrase="pw")
        history.record(_message("ephemeral"))
        history.wipe()
        assert not path.exists()
        assert len(history) == 0

    def test_empty_passphrase_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(HistoryPassphraseError):
            EncryptedHistory.open(tmp_path / "chat-history.gle", passphrase="")

    def test_entries_cap(self, tmp_path: Path) -> None:
        from ghostlink.messaging import history as history_module

        path = tmp_path / "chat-history.gle"
        history = EncryptedHistory.open(path, passphrase="pw")
        cap = history_module.MAX_HISTORY_ENTRIES
        for index in range(cap + 5):
            history._entries.append(
                HistoryEntry(
                    message_id=generate_message_id(),
                    conversation_id="conv_0123456789ab",
                    direction="outgoing",
                    author="Nova",
                    text=f"m{index}",
                    sent_at=1_780_000_000.0,
                    status="sent",
                )
            )
        history.record(_message("trigger flush"))
        assert len(history) <= cap
        history.close()
        reopened = EncryptedHistory.open(path, passphrase="pw")
        assert len(reopened) <= cap
        reopened.close()


class TestHelpers:
    def test_timestamp_formats(self) -> None:
        epoch = 1_780_000_000.0
        assert format_timestamp(epoch, "24h").startswith("[")
        assert format_timestamp(epoch, "12h").startswith("[")
        assert format_timestamp(epoch, "24h") != format_timestamp(epoch, "12h")

    def test_export_file_name_is_timestamped(self) -> None:
        name = export_file_name()
        assert name.startswith("ghostlink-chat-")
        assert name.endswith(".txt")
