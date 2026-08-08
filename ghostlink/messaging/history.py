"""Message history (Phase 3) — optional, privacy-first.

Three modes, configured via ``chat.history_mode``:

* **disabled** — nothing is retained anywhere
* **session** — retained in memory for the running chat only, wiped on exit
* **encrypted** — additionally persisted to an encrypted local file, locked
  by a passphrase (scrypt → ChaCha20-Poly1305). The file never leaves the
  device and cannot be read without the passphrase.

Plaintext retention is *the* user-visible decision in a messenger, so the
default is off and every write goes through this module only.
"""

from __future__ import annotations

import json
import os
import secrets
import tempfile
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

from ghostlink.core.logging import get_logger
from ghostlink.exceptions.messaging import HistoryError, HistoryPassphraseError
from ghostlink.messaging.models.message import Message

_logger = get_logger("messaging.history")

MAX_HISTORY_ENTRIES: int = 2000
HISTORY_FILE_MAGIC: bytes = b"GLH1"
HISTORY_FILE_NAME: str = "chat-history.gle"
_SCRYPT_SALT_BYTES: int = 16
_NONCE_BYTES: int = 12
_SCRYPT_N: int = 2**15
_SCRYPT_R: int = 8
_SCRYPT_P: int = 1
_KEY_BYTES: int = 32


class HistoryMode(str, Enum):
    """Retention policy for message history."""

    DISABLED = "disabled"
    SESSION = "session"
    ENCRYPTED = "encrypted"


HISTORY_MODES: tuple[str, ...] = tuple(mode.value for mode in HistoryMode)


def parse_history_mode(value: str) -> HistoryMode:
    try:
        return HistoryMode(value.strip().lower())
    except ValueError as exc:
        raise HistoryError(
            f"Unknown history mode '{value}'.",
            hint=f"Valid modes: {', '.join(HISTORY_MODES)}.",
        ) from exc


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One retained snapshot: a message, or a file-transfer metadata record.

    Transfer entries (``kind == "transfer"``, Phase 4) reuse the same fields:
    ``message_id`` holds the transfer id, ``author`` the peer, ``text`` a
    metadata-only summary (filename and size — never contents, never keys).
    """

    message_id: str
    conversation_id: str
    direction: str  # "outgoing" | "incoming"
    author: str
    text: str
    sent_at: float
    status: str
    kind: str = "message"  # "message" | "transfer"

    @classmethod
    def from_message(cls, message: Message) -> HistoryEntry:
        return cls(
            message_id=message.message_id,
            conversation_id=message.conversation_id,
            direction=message.direction.value,
            author=message.sender,
            text=message.text,
            sent_at=message.created_at,
            status=message.status.value,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.message_id,
            "conv": self.conversation_id,
            "dir": self.direction,
            "author": self.author,
            "text": self.text,
            "ts": self.sent_at,
            "status": self.status,
            "kind": self.kind,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> HistoryEntry:
        try:
            return cls(
                message_id=str(data["id"]),
                conversation_id=str(data["conv"]),
                direction=str(data["dir"]),
                author=str(data["author"]),
                text=str(data["text"]),
                sent_at=float(data["ts"]),  # type: ignore[arg-type]
                status=str(data["status"]),
                kind=str(data.get("kind", "message")),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise HistoryError(
                "A stored history entry is malformed.",
                hint="The history file may be corrupt; wipe it with /clear "
                "history or delete it from the state directory.",
            ) from exc


def format_timestamp(epoch: float, timestamp_format: str) -> str:
    """Render one entry's time: '24h' → ``[22:10]``, '12h' → ``[10:10 PM]``."""

    moment = datetime.fromtimestamp(epoch, tz=UTC).astimezone()
    if timestamp_format == "12h":
        return moment.strftime("[%I:%M %p]").lstrip("0").replace("[0", "[")
    return moment.strftime("[%H:%M]")


class BaseHistory:
    """Common contract every retention mode honours."""

    def __init__(self) -> None:
        self._entries: list[HistoryEntry] = []

    @property
    def enabled(self) -> bool:
        return True

    @property
    def persistent(self) -> bool:
        return False

    def record(self, message: Message) -> None:
        self._append(HistoryEntry.from_message(message))

    def record_transfer(
        self,
        *,
        transfer_id: str,
        conversation_id: str,
        direction: str,
        peer: str,
        summary: str,
        status: str,
    ) -> None:
        """Retain one completed transfer's *metadata* (Phase 4).

        ``summary`` carries the sanitized filename and size only — file
        contents and key material never enter history.
        """

        self._append(
            HistoryEntry(
                message_id=transfer_id,
                conversation_id=conversation_id,
                direction=direction,
                author=peer,
                text=summary,
                sent_at=time.time(),
                status=status,
                kind="transfer",
            )
        )

    def _append(self, entry: HistoryEntry) -> None:
        self._entries.append(entry)
        if len(self._entries) > MAX_HISTORY_ENTRIES:
            del self._entries[: len(self._entries) - MAX_HISTORY_ENTRIES]

    def entries(self) -> list[HistoryEntry]:
        return list(self._entries)

    def __len__(self) -> int:
        return len(self._entries)

    def export_text(self, *, timestamp_format: str = "24h") -> str:
        """Human-readable transcript of everything retained."""

        lines: list[str] = []
        current_day = ""
        for entry in self._entries:
            day = datetime.fromtimestamp(entry.sent_at, tz=UTC).astimezone().strftime("%Y-%m-%d")
            if day != current_day:
                current_day = day
                lines.append(f"── {day} ──")
            stamp = format_timestamp(entry.sent_at, timestamp_format)
            if entry.kind == "transfer":
                verb = "sent to" if entry.direction == "outgoing" else "received from"
                lines.append(f"{stamp} 📎 {entry.text}")
                lines.append(f"  {verb} {entry.author} — {entry.status}")
            else:
                lines.append(f"{stamp} {entry.author}:")
                lines.append(f"  {entry.text}")
        return "\n".join(lines)

    def write_export(self, path: Path, *, timestamp_format: str = "24h") -> Path:
        """Write the transcript to ``path`` (user-chosen, unencrypted!)."""

        text = self.export_text(timestamp_format=timestamp_format)
        try:
            path.write_text(text + "\n", encoding="utf-8")
        except OSError as exc:
            raise HistoryError(
                f"Could not write the export to '{path}'.",
                hint="Check the directory exists and is writable.",
            ) from exc
        _logger.info("history exported — %d entries → %s", len(self._entries), path)
        return path

    def close(self) -> None:
        """End of session; non-persistent modes wipe here."""

        self._entries.clear()


class NullHistory(BaseHistory):
    """History disabled: nothing is ever retained."""

    @property
    def enabled(self) -> bool:
        return False

    def record(self, message: Message) -> None:
        del message  # deliberately not retained

    def record_transfer(
        self,
        *,
        transfer_id: str,
        conversation_id: str,
        direction: str,
        peer: str,
        summary: str,
        status: str,
    ) -> None:
        del transfer_id, conversation_id, direction, peer, summary, status  # not retained


class SessionHistory(BaseHistory):
    """In-memory history for the running chat; wiped on close."""


class EncryptedHistory(BaseHistory):
    """Passphrase-locked, encrypted-at-rest history.

    File layout: ``GLH1`` magic ‖ 16-byte scrypt salt ‖ 12-byte nonce ‖
    ChaCha20-Poly1305 ciphertext of the JSON entry list. The passphrase is
    read interactively by the caller; this module only ever sees it in
    memory. Writes are atomic (temp file + rename) and 0600-permissioned.
    """

    def __init__(self, path: Path, *, key: bytes, salt: bytes) -> None:
        super().__init__()
        self._path = path
        self._key = key
        self._salt = salt

    @property
    def persistent(self) -> bool:
        return True

    @property
    def path(self) -> Path:
        return self._path

    @staticmethod
    def _derive_key(passphrase: str, salt: bytes) -> bytes:
        if not passphrase:
            raise HistoryPassphraseError(
                "A passphrase is required for encrypted history.",
                hint="Choose a passphrase when enabling encrypted history.",
            )
        kdf = Scrypt(salt=salt, length=_KEY_BYTES, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P)
        return kdf.derive(passphrase.encode("utf-8"))

    @classmethod
    def open(cls, path: Path, *, passphrase: str) -> EncryptedHistory:
        """Unlock an existing history file, or create a fresh one."""

        if path.exists():
            raw = cls._read_file(path)
            salt = raw[len(HISTORY_FILE_MAGIC) : len(HISTORY_FILE_MAGIC) + _SCRYPT_SALT_BYTES]
            key = cls._derive_key(passphrase, salt)
            history = cls(path, key=key, salt=salt)
            history._entries = history._decrypt_entries(raw, key)
            _logger.info("encrypted history unlocked — %d entries", len(history._entries))
            return history
        salt = secrets.token_bytes(_SCRYPT_SALT_BYTES)
        key = cls._derive_key(passphrase, salt)
        history = cls(path, key=key, salt=salt)
        history._flush()
        _logger.info("encrypted history created at %s", path)
        return history

    @staticmethod
    def _read_file(path: Path) -> bytes:
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise HistoryError(
                f"Could not read the history file '{path}'.",
                hint="Check file permissions, or delete it to start fresh.",
            ) from exc
        header = len(HISTORY_FILE_MAGIC) + _SCRYPT_SALT_BYTES + _NONCE_BYTES
        if len(raw) <= header or not raw.startswith(HISTORY_FILE_MAGIC):
            raise HistoryError(
                f"The history file '{path}' is not a GhostLink history.",
                hint="Delete the file to start a fresh encrypted history.",
            )
        return raw

    def _decrypt_entries(self, raw: bytes, key: bytes) -> list[HistoryEntry]:
        prefix = len(HISTORY_FILE_MAGIC) + _SCRYPT_SALT_BYTES
        nonce = raw[prefix : prefix + _NONCE_BYTES]
        ciphertext = raw[prefix + _NONCE_BYTES :]
        try:
            plaintext = ChaCha20Poly1305(key).decrypt(nonce, ciphertext, HISTORY_FILE_MAGIC)
        except InvalidTag as exc:
            raise HistoryPassphraseError(
                "The history could not be unlocked with that passphrase.",
                hint="Retry with the passphrase used when history was first enabled.",
            ) from exc
        try:
            document = json.loads(plaintext.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise HistoryError(
                "The decrypted history is not valid JSON.",
                hint="The file was modified outside GhostLink; start fresh.",
            ) from exc
        if not isinstance(document, list):
            raise HistoryError(
                "The decrypted history has an unexpected shape.",
                hint="The file was modified outside GhostLink; start fresh.",
            )
        return [HistoryEntry.from_dict(item) for item in document if isinstance(item, dict)]

    def record(self, message: Message) -> None:
        super().record(message)
        self._flush()

    def record_transfer(
        self,
        *,
        transfer_id: str,
        conversation_id: str,
        direction: str,
        peer: str,
        summary: str,
        status: str,
    ) -> None:
        super().record_transfer(
            transfer_id=transfer_id,
            conversation_id=conversation_id,
            direction=direction,
            peer=peer,
            summary=summary,
            status=status,
        )
        self._flush()

    def _flush(self) -> None:
        document = json.dumps(
            [entry.to_dict() for entry in self._entries], ensure_ascii=False
        ).encode("utf-8")
        nonce = secrets.token_bytes(_NONCE_BYTES)
        ciphertext = ChaCha20Poly1305(self._key).encrypt(nonce, document, HISTORY_FILE_MAGIC)
        blob = HISTORY_FILE_MAGIC + self._salt + nonce + ciphertext
        self._write_atomic(blob)

    def _write_atomic(self, blob: bytes) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp_name = tempfile.mkstemp(
                dir=self._path.parent, prefix=".history-", suffix=".tmp"
            )
            try:
                os.write(fd, blob)
                os.fchmod(fd, 0o600)
                os.close(fd)
                fd = -1
                os.replace(temp_name, self._path)
                os.chmod(self._path, 0o600)
            finally:
                if fd >= 0:
                    os.close(fd)
                if os.path.exists(temp_name):
                    os.unlink(temp_name)
        except OSError as exc:
            raise HistoryError(
                f"Could not persist the history file '{self._path}'.",
                hint="Check that the state directory is writable.",
            ) from exc

    def wipe(self) -> None:
        """Destroy the encrypted file and every retained entry."""

        self._entries.clear()
        try:
            self._path.unlink(missing_ok=True)
        except OSError as exc:
            raise HistoryError(
                f"Could not delete the history file '{self._path}'.",
                hint="Remove it manually from the state directory.",
            ) from exc
        _logger.info("encrypted history wiped — %s", self._path)

    def close(self) -> None:
        """Persistent mode keeps entries on disk; the key leaves memory."""

        self._key = b"\x00" * _KEY_BYTES
        self._entries.clear()


@dataclass(frozen=True, slots=True)
class HistoryRequest:
    """What a chat needs from history, decided once at startup."""

    mode: HistoryMode
    path: Path | None = None  # set when mode is ENCRYPTED


def open_history(
    mode: HistoryMode,
    *,
    state_dir: Path,
    passphrase: str | None = None,
) -> BaseHistory:
    """Build the history backend for one chat session."""

    if mode is HistoryMode.DISABLED:
        return NullHistory()
    if mode is HistoryMode.SESSION:
        return SessionHistory()
    path = state_dir / "history"
    return EncryptedHistory.open(path / HISTORY_FILE_NAME, passphrase=passphrase or "")


def export_file_name(prefix: str = "ghostlink-chat") -> str:
    """Timestamped default name for /export writes."""

    stamp = time.strftime("%Y%m%d-%H%M%S")
    return f"{prefix}-{stamp}.txt"
