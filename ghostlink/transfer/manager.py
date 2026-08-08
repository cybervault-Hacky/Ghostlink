"""Transfer manager (Phase 4) — the orchestrator for secure file transfer.

One :class:`TransferManager` rides on a live
:class:`~ghostlink.messaging.session.chat.ChatSession`: the session feeds it
validated ``FILE_*`` frames, lends it the session key for per-transfer HKDF
derivation, and reports connectivity changes. Everything else — offers,
accept/reject, the sliding-window send pump, chunk verification, resume
bitmaps, pausing, expiry, temp hygiene, and metadata-only history — lives
here, so the messaging layer stays untouched by file semantics.

Delivery guarantees:

* chunks travel AEAD-sealed under a per-transfer key, bound to
  ``transfer_id|n`` as associated data — tampering, truncation, replay and
  mis-numbering are all detected, never silently accepted
* the receiver's chunk bitmap is the single source of truth for resume;
  senders adopt it on every reconnect instead of trusting their own ACK log
* the relay only ever forwards sealed payloads (this module logs transfer
  ids, states and counters — never contents or keys)
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import time
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ghostlink.constants.app import (
    TRANSFER_MAX_CHUNK_KB,
    TRANSFER_MAX_CHUNKS_PER_FILE,
    TRANSFER_MIN_CHUNK_KB,
    TRANSFER_WINDOW_CHUNKS,
)
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.base import GhostLinkError
from ghostlink.exceptions.messaging import DecryptionError
from ghostlink.exceptions.storage import StorageError
from ghostlink.exceptions.transfer import (
    TransferError,
    TransferLimitError,
    TransferStateError,
    TransferValidationError,
)
from ghostlink.exceptions.transport import TransportError
from ghostlink.messaging.history import BaseHistory
from ghostlink.messaging.packets.frames import Frame, FrameType
from ghostlink.messaging.session.chat import ChatEvent, ChatEventKind
from ghostlink.models.settings import TransferSettings
from ghostlink.transfer import packets
from ghostlink.transfer.chunking import ChunkBitmap, ChunkReader
from ghostlink.transfer.cleanup import cleanup_orphan_temps
from ghostlink.transfer.integrity import (
    derive_transfer_key,
    hash_temp,
    open_chunk,
    open_manifest,
    seal_chunk,
    seal_manifest,
)
from ghostlink.transfer.manifest import FileManifest
from ghostlink.transfer.models import (
    ACTIVE_STATES,
    Transfer,
    TransferDirection,
    TransferSnapshot,
    TransferState,
    generate_transfer_id,
    is_valid_transfer_id,
)
from ghostlink.transfer.storage import TransferStorage, sanitize_filename
from ghostlink.utils.text import format_bytes

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ghostlink.messaging.session.chat import ChatSession

_logger = get_logger("transfer.manager")

_SWEEP_TICK_SECONDS: float = 5.0
_PUMP_PACE_SECONDS: float = 0.25
_PROGRESS_MIN_INTERVAL_SECONDS: float = 0.5
_COMPLETE_PROBE_MULTIPLIER: int = 4
_OFFER_RESEND_MULTIPLIER: int = 4  # × ack_timeout between offer re-sends
_DIRECTION_TO_HISTORY: dict[TransferDirection, str] = {
    TransferDirection.SENDING: "outgoing",
    TransferDirection.RECEIVING: "incoming",
}

TransferListener = Callable[["TransferEvent"], None]


class TransferEventKind(str, Enum):
    """Everything the UI can observe about file transfers."""

    OFFER = "offer"  # incoming offer awaits a local decision
    STATE = "state"  # lifecycle state changed
    PROGRESS = "progress"  # bytes moved (throttled)
    SAVED = "saved"  # receiver: verified and moved into the download dir
    NOTICE = "notice"  # operational notices (never file contents)


@dataclass(frozen=True, slots=True)
class TransferEvent:
    kind: TransferEventKind
    transfer: TransferSnapshot | None = None
    detail: str = ""
    at: float = field(default_factory=time.time)


@dataclass(frozen=True, slots=True)
class TransferLimits:
    """Validated, byte-denominated knobs for one manager."""

    max_file_size_bytes: int
    max_concurrent: int
    chunk_size_bytes: int
    ack_timeout_seconds: float
    retry_limit: int
    temp_limit_bytes: int
    transfer_expiry_seconds: float
    offer_timeout_seconds: float = 120.0
    terminal_ttl_seconds: float = 300.0

    @classmethod
    def from_settings(cls, settings: TransferSettings) -> TransferLimits:
        return cls(
            max_file_size_bytes=settings.max_file_size_mb * 1024 * 1024,
            max_concurrent=settings.max_concurrent_transfers,
            chunk_size_bytes=settings.chunk_size_kb * 1024,
            ack_timeout_seconds=settings.ack_timeout_seconds,
            retry_limit=settings.retry_limit,
            temp_limit_bytes=settings.temp_storage_limit_mb * 1024 * 1024,
            transfer_expiry_seconds=settings.transfer_expiry_minutes * 60.0,
        )


class TransferManager:
    """Owns every transfer of one conversation, in both directions."""

    def __init__(
        self,
        session: ChatSession,
        storage: TransferStorage,
        limits: TransferLimits,
        *,
        history: BaseHistory | None = None,
    ) -> None:
        self._session = session
        self._storage = storage
        self._limits = limits
        self._history = history

        self._transfers: dict[str, Transfer] = {}
        self._manifests: dict[str, FileManifest] = {}
        self._bitmaps: dict[str, ChunkBitmap] = {}
        self._keys: dict[str, bytearray] = {}
        self._destinations: dict[str, Path] = {}
        self._offer_attempts: dict[str, int] = {}
        self._pumps: dict[str, asyncio.Task[None]] = {}
        self._background: set[asyncio.Task[None]] = set()
        self._listeners: list[TransferListener] = []
        self._last_progress: dict[str, float] = {}
        self._wake = asyncio.Event()
        self._sweeper: asyncio.Task[None] | None = None
        self._closing = False
        self._started = False

    # --------------------------------------------------------------- events

    def add_listener(self, listener: TransferListener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener: TransferListener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def _emit(
        self,
        kind: TransferEventKind,
        transfer: Transfer | None = None,
        detail: str = "",
    ) -> None:
        event = TransferEvent(
            kind=kind,
            transfer=transfer.snapshot() if transfer is not None else None,
            detail=detail,
        )
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception as exc:  # a UI bug must not break transfers
                _logger.debug("transfer event listener failed: %s", exc)

    def _maybe_progress(self, transfer: Transfer, *, force: bool = False) -> None:
        now = time.monotonic()
        last = self._last_progress.get(transfer.transfer_id, 0.0)
        if not force and now - last < _PROGRESS_MIN_INTERVAL_SECONDS:
            return
        self._last_progress[transfer.transfer_id] = now
        self._emit(TransferEventKind.PROGRESS, transfer)

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        """Wire into the chat session and start the housekeeping sweeper."""

        if self._started:
            raise TransferError(
                "Transfer managers are single-use.",
                hint="Create one manager per chat session.",
            )
        self._started = True
        self._storage.ensure_directories()
        removed = cleanup_orphan_temps(self._storage.temp_dir)
        if removed:
            _logger.info("removed %d orphaned transfer temp file(s)", removed)
        self._session.add_listener(self._on_chat_event)
        self._session.set_transfer_delegate(self.handle_frame)
        self._sweeper = asyncio.create_task(self._sweep_loop())
        _logger.info("transfer manager started — channel=%s", self._session.channel)

    async def close(self) -> None:
        """Cancel active transfers, stop tasks, wipe keys, detach (idempotent)."""

        if self._closing:
            return
        self._closing = True
        for transfer in list(self._transfers.values()):
            if transfer.is_active:
                await self._send_quiet(
                    packets.file_cancel_frame(transfer.transfer_id, reason="session closed")
                )
                self._settle_terminal(transfer, TransferState.CANCELLED, error="session closed")
        self._kick()
        tasks = [task for task in self._pumps.values() if not task.done()]
        if self._sweeper is not None and not self._sweeper.done():
            tasks.append(self._sweeper)
        tasks.extend(task for task in self._background if not task.done())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._session.remove_listener(self._on_chat_event)
        self._session.set_transfer_delegate(None)
        self._wipe_keys()
        self._transfers.clear()
        self._manifests.clear()
        self._bitmaps.clear()
        self._destinations.clear()
        self._offer_attempts.clear()
        _logger.info("transfer manager closed — channel=%s", self._session.channel)

    # ------------------------------------------------------------ public API

    async def send_file(self, path: str | Path) -> TransferSnapshot:
        """Offer a local file to the peer; queues when the airtime cap is hit."""

        source = Path(path).expanduser()
        if not source.is_file():
            raise TransferValidationError(
                f"'{source.name}' is not a regular file.",
                hint="Choose an existing file — directories and devices cannot be sent.",
            )
        if self._session_key() is None:
            raise TransferError(
                "Encryption is not active yet.",
                hint="Wait for 'Encryption: Active' before sending files.",
            )
        safe_name = sanitize_filename(source.name)
        if safe_name is None:
            raise TransferValidationError(
                f"'{source.name}' has no usable filename.",
                hint="Rename the file to a short printable name and retry.",
            )
        transfer_id = generate_transfer_id()
        manifest = await asyncio.to_thread(
            FileManifest.from_file,
            source,
            transfer_id=transfer_id,
            safe_name=safe_name,
            chunk_size=self._limits.chunk_size_bytes,
        )
        if manifest.size_bytes > self._limits.max_file_size_bytes:
            raise TransferLimitError(
                f"'{safe_name}' is {format_bytes(manifest.size_bytes)} — the limit is "
                f"{format_bytes(self._limits.max_file_size_bytes)}.",
                hint="Raise transfer.max_file_size_mb, or send a smaller file.",
            )
        if manifest.total_chunks > TRANSFER_MAX_CHUNKS_PER_FILE:
            raise TransferLimitError(
                f"'{safe_name}' needs more than {TRANSFER_MAX_CHUNKS_PER_FILE} chunks.",
                hint="Increase transfer.chunk_size_kb for very large files.",
            )
        transfer = Transfer(
            transfer_id=transfer_id,
            direction=TransferDirection.SENDING,
            state=TransferState.QUEUED,
            filename=safe_name,
            size_bytes=manifest.size_bytes,
            mime=manifest.mime,
            chunk_size=manifest.chunk_size,
            total_chunks=manifest.total_chunks,
            peer=self._session.peer_name,
            integrity=manifest.sha256,
            source_path=str(source),
            local_path=str(source),
            expires_at=time.time() + self._limits.transfer_expiry_seconds,
        )
        self._transfers[transfer_id] = transfer
        self._manifests[transfer_id] = manifest
        self._bitmaps[transfer_id] = ChunkBitmap(manifest.total_chunks)
        _logger.info(
            "transfer created — %s, %d bytes in %d chunks",
            transfer_id,
            manifest.size_bytes,
            manifest.total_chunks,
        )
        if self._active_count() < self._limits.max_concurrent:
            await self._offer(transfer, announce=True)
        else:
            self._emit(
                TransferEventKind.STATE,
                transfer,
                "Queued — it will start when an active transfer finishes.",
            )
            _logger.info("transfer queued — %s (concurrency cap reached)", transfer_id)
        return transfer.snapshot()

    async def accept(self, transfer_id: str) -> TransferSnapshot:
        """Accept one incoming offer: reserve destination, temp, and reply."""

        transfer = self._require_transfer(transfer_id)
        if transfer.direction is not TransferDirection.RECEIVING:
            raise TransferStateError(
                "Only incoming transfers can be accepted.",
                hint="Use /transfers to list active offers.",
            )
        if transfer.state is not TransferState.OFFERED:
            raise TransferStateError(
                f"Transfer {transfer_id} is {transfer.state.value}, not awaiting approval.",
                hint="Only offered transfers can be accepted.",
            )
        manifest = self._require_manifest(transfer_id)
        self._storage.check_quota(manifest.size_bytes, limit_bytes=self._limits.temp_limit_bytes)
        destination = await asyncio.to_thread(self._storage.destination_for, manifest.filename)
        await asyncio.to_thread(self._storage.open_temp, transfer_id)
        self._destinations[transfer_id] = destination
        transfer.transition_to(TransferState.ACCEPTED)
        self._emit(
            TransferEventKind.STATE,
            transfer,
            "Accepted — waiting for the first chunks…",
        )
        await self._send_quiet(packets.file_accept_frame(transfer_id))
        _logger.info("transfer accepted — %s", transfer_id)
        return transfer.snapshot()

    async def reject(self, transfer_id: str, *, reason: str | None = None) -> TransferSnapshot:
        """Reject one incoming offer."""

        transfer = self._require_transfer(transfer_id)
        if transfer.direction is not TransferDirection.RECEIVING:
            raise TransferStateError(
                "Only incoming offers can be rejected.",
                hint="Use /cancel for transfers you are sending.",
            )
        if transfer.state is not TransferState.OFFERED:
            raise TransferStateError(
                f"Transfer {transfer_id} is {transfer.state.value}, not awaiting approval.",
                hint="Only offered transfers can be rejected.",
            )
        self._settle_terminal(transfer, TransferState.REJECTED, error=reason)
        await self._send_quiet(packets.file_reject_frame(transfer_id, reason=reason))
        self._emit(TransferEventKind.STATE, transfer, "Declined.")
        _logger.info("transfer rejected — %s", transfer_id)
        return transfer.snapshot()

    async def pause(self, transfer_id: str) -> TransferSnapshot:
        """Pause an in-flight transfer (both sides are notified)."""

        transfer = self._require_transfer(transfer_id)
        if transfer.state is not TransferState.TRANSFERRING:
            raise TransferStateError(
                f"Transfer {transfer_id} is {transfer.state.value}; only in-flight "
                "transfers can pause.",
                hint="Wait for the first chunks, or cancel instead.",
            )
        transfer.resume_notified = True  # user-managed: no auto-resume on reconnect
        transfer.transition_to(TransferState.PAUSED)
        self._emit(TransferEventKind.STATE, transfer, "Paused.")
        await self._send_quiet(packets.file_pause_frame(transfer_id))
        self._kick()
        _logger.info("transfer paused — %s", transfer_id)
        return transfer.snapshot()

    async def resume(self, transfer_id: str) -> TransferSnapshot:
        """Resume a paused transfer (both sides are notified)."""

        transfer = self._require_transfer(transfer_id)
        if transfer.state is not TransferState.PAUSED:
            raise TransferStateError(
                f"Transfer {transfer_id} is {transfer.state.value}; only paused "
                "transfers can resume.",
                hint="Use /transfers to inspect current states.",
            )
        transfer.resume_notified = True
        transfer.transition_to(TransferState.TRANSFERRING)
        self._emit(TransferEventKind.STATE, transfer, "Resumed.")
        await self._send_quiet(packets.file_resume_frame(transfer_id))
        if transfer.direction is TransferDirection.RECEIVING:
            await self._share_bitmap(transfer)  # truth sync for the sender
        self._kick()
        _logger.info("transfer resumed — %s", transfer_id)
        return transfer.snapshot()

    async def cancel(self, transfer_id: str, *, reason: str | None = None) -> TransferSnapshot:
        """Cancel any non-terminal transfer, either direction."""

        transfer = self._require_transfer(transfer_id)
        if transfer.is_terminal:
            raise TransferStateError(
                f"Transfer {transfer_id} already ended ({transfer.state.value}).",
                hint="Closed transfers cannot be cancelled.",
            )
        self._settle_terminal(transfer, TransferState.CANCELLED, error=reason)
        await self._send_quiet(packets.file_cancel_frame(transfer_id, reason=reason))
        self._emit(TransferEventKind.STATE, transfer, "Cancelled.")
        self._kick()
        _logger.info("transfer cancelled — %s (%s)", transfer_id, reason or "no reason")
        return transfer.snapshot()

    # ------------------------------------------------------------ inspection

    def get(self, transfer_id: str) -> TransferSnapshot | None:
        transfer = self._transfers.get(transfer_id)
        return transfer.snapshot() if transfer is not None else None

    def list_transfers(self) -> list[TransferSnapshot]:
        """Newest first — the order /transfers displays."""

        return [
            transfer.snapshot()
            for transfer in sorted(
                self._transfers.values(), key=lambda item: item.created_at, reverse=True
            )
        ]

    def pending_incoming(self) -> list[TransferSnapshot]:
        """Incoming offers awaiting a decision, newest first."""

        return [
            snapshot
            for snapshot in self.list_transfers()
            if snapshot.direction is TransferDirection.RECEIVING
            and snapshot.state is TransferState.OFFERED
        ]

    def resolve_id(self, prefix: str) -> str | None:
        """Resolve an id or unique id prefix to a full transfer id."""

        if prefix in self._transfers:
            return prefix
        matches = [tid for tid in self._transfers if tid.startswith(prefix)]
        return matches[0] if len(matches) == 1 else None

    def _require_transfer(self, transfer_id: str) -> Transfer:
        transfer = self._transfers.get(transfer_id)
        if transfer is None:
            raise TransferValidationError(
                f"Unknown transfer '{transfer_id}'.",
                hint="Use /transfers to list known transfers.",
            )
        return transfer

    def _require_manifest(self, transfer_id: str) -> FileManifest:
        manifest = self._manifests.get(transfer_id)
        if manifest is None:
            raise TransferValidationError(
                f"Transfer {transfer_id} has no manifest on record.",
                hint="Re-offer the file to rebuild the manifest.",
            )
        return manifest

    # ------------------------------------------------------- frame dispatch

    async def handle_frame(self, frame: Frame) -> None:
        """Consume one validated FILE_* frame from the chat session."""

        if self._closing:
            return
        handler = {
            FrameType.FILE_OFFER: self._on_offer,
            FrameType.FILE_ACCEPT: self._on_accept,
            FrameType.FILE_REJECT: self._on_reject_frame,
            FrameType.FILE_CHUNK: self._on_chunk,
            FrameType.FILE_ACK: self._on_ack,
            FrameType.FILE_PAUSE: self._on_pause,
            FrameType.FILE_RESUME: self._on_resume,
            FrameType.FILE_CANCEL: self._on_cancel,
            FrameType.FILE_COMPLETE: self._on_complete,
            FrameType.FILE_ERROR: self._on_error_frame,
        }.get(frame.type)
        if handler is not None:
            await handler(frame)

    # ------------------------------------------------------------- inbound

    async def _on_offer(self, frame: Frame) -> None:
        transfer_id = str(frame.data["id"])
        if not is_valid_transfer_id(transfer_id):
            await self._send_quiet(
                packets.file_error_frame(transfer_id, "invalid_transfer", "malformed transfer id")
            )
            return
        existing = self._transfers.get(transfer_id)
        if existing is not None:
            await self._on_reoffer(existing)
            return
        key = self._key_for(transfer_id)
        if key is None:
            _logger.debug("offer for %s before encryption is ready — dropped", transfer_id)
            return
        try:
            manifest_json = open_manifest(bytes(key), transfer_id, self._b64(frame, "ct"))
        except DecryptionError:
            _logger.info("offer %s failed its integrity check — rejected", transfer_id)
            self._drop_key(transfer_id)
            await self._send_quiet(
                packets.file_error_frame(
                    transfer_id, "integrity", "offer failed its integrity check"
                )
            )
            self._emit(
                TransferEventKind.NOTICE,
                detail="An incoming file offer failed integrity verification and was declined.",
            )
            return
        try:
            manifest = FileManifest.from_json(manifest_json)
            manifest.validate(
                max_file_size_bytes=self._limits.max_file_size_bytes,
                max_chunk_size=TRANSFER_MAX_CHUNK_KB * 1024,
                min_chunk_size=TRANSFER_MIN_CHUNK_KB * 1024,
            )
        except TransferValidationError as exc:
            _logger.info("offer %s rejected — %s", transfer_id, exc.message)
            await self._send_quiet(
                packets.file_error_frame(transfer_id, "manifest", exc.message[:120])
            )
            return
        if self._active_count() >= self._limits.max_concurrent:
            await self._send_quiet(
                packets.file_reject_frame(transfer_id, reason="busy — too many active transfers")
            )
            _logger.info("offer %s auto-rejected — concurrency cap reached", transfer_id)
            return
        try:
            self._storage.check_quota(
                manifest.size_bytes + self._reserved_incoming_bytes(),
                limit_bytes=self._limits.temp_limit_bytes,
            )
        except TransferLimitError:
            await self._send_quiet(
                packets.file_reject_frame(transfer_id, reason="temp storage limit reached")
            )
            _logger.info("offer %s auto-rejected — temp quota", transfer_id)
            return
        transfer = Transfer(
            transfer_id=transfer_id,
            direction=TransferDirection.RECEIVING,
            state=TransferState.OFFERED,
            filename=manifest.filename,
            size_bytes=manifest.size_bytes,
            mime=manifest.mime,
            chunk_size=manifest.chunk_size,
            total_chunks=manifest.total_chunks,
            peer=self._session.peer_name,
            integrity=manifest.sha256,
            expires_at=time.time() + self._limits.transfer_expiry_seconds,
        )
        self._transfers[transfer_id] = transfer
        self._manifests[transfer_id] = manifest
        self._bitmaps[transfer_id] = ChunkBitmap(manifest.total_chunks)
        self._emit(TransferEventKind.OFFER, transfer)
        _logger.info(
            "offer received — %s, %d bytes in %d chunks",
            transfer_id,
            manifest.size_bytes,
            manifest.total_chunks,
        )

    async def _on_reoffer(self, transfer: Transfer) -> None:
        """A re-sent offer: dedupe by state (reconnects make senders retry)."""

        if transfer.direction is TransferDirection.SENDING:
            await self._send_quiet(
                packets.file_error_frame(
                    transfer.transfer_id, "id_conflict", "transfer id already in use"
                )
            )
            return
        if transfer.state is TransferState.OFFERED:
            transfer.touch()  # same offer again while the user decides — silent
            return
        if transfer.state is TransferState.COMPLETED:
            digest = self._require_manifest(transfer.transfer_id).sha256
            await self._send_quiet(packets.file_complete_frame(transfer.transfer_id, digest))
            return
        if not transfer.is_terminal:
            await self._share_bitmap(transfer)  # peer needs our accepted truth
            return
        await self._send_quiet(
            packets.file_error_frame(
                transfer.transfer_id, "transfer_closed", f"transfer is {transfer.state.value}"
            )
        )

    async def _on_accept(self, frame: Frame) -> None:
        transfer_id = str(frame.data["id"])
        transfer = self._transfers.get(transfer_id)
        if transfer is None or transfer.direction is not TransferDirection.SENDING:
            return  # never trust ACCEPT for transfers we are not offering
        bitmap_b64 = frame.data.get("bm")
        if isinstance(bitmap_b64, str) and bitmap_b64:
            try:
                peer_bitmap = ChunkBitmap.from_b64(bitmap_b64, total_chunks=transfer.total_chunks)
            except TransferValidationError:
                _logger.info("ACCEPT for %s carried a bad bitmap — failing", transfer_id)
                await self._fail_transfer(transfer, "malformed resume bitmap")
                await self._send_quiet(
                    packets.file_error_frame(transfer_id, "manifest", "malformed resume bitmap")
                )
                return
            self._adopt_bitmap(transfer, peer_bitmap)
        if transfer.state is TransferState.OFFERED:
            transfer.transition_to(TransferState.ACCEPTED)
            self._emit(TransferEventKind.STATE, transfer, "Peer accepted.")
            transfer.transition_to(TransferState.TRANSFERRING)
            self._ensure_pump(transfer)
        elif transfer.state is TransferState.PAUSED and not transfer.resume_notified:
            # The peer answered a link-loss FILE_RESUME with fresh truth.
            transfer.resume_notified = True
            transfer.transition_to(TransferState.TRANSFERRING)
            self._emit(TransferEventKind.STATE, transfer, "Resumed.")
            self._ensure_pump(transfer)
        self._maybe_progress(transfer, force=True)
        self._kick()
        _logger.info(
            "transfer accepted — %s (peer holds %d/%d chunks)",
            transfer_id,
            self._bitmaps[transfer_id].count,
            transfer.total_chunks,
        )

    async def _on_reject_frame(self, frame: Frame) -> None:
        transfer_id = str(frame.data["id"])
        transfer = self._transfers.get(transfer_id)
        if transfer is None or transfer.direction is not TransferDirection.SENDING:
            return
        reason = str(frame.data.get("reason") or "")
        self._settle_terminal(transfer, TransferState.REJECTED, error=reason or None)
        self._emit(
            TransferEventKind.STATE,
            transfer,
            f"Peer declined{': ' + reason if reason else '.'}",
        )
        _logger.info("transfer rejected by peer — %s", transfer_id)

    async def _on_chunk(self, frame: Frame) -> None:
        transfer_id = str(frame.data["id"])
        transfer = self._transfers.get(transfer_id)
        if transfer is None or transfer.direction is not TransferDirection.RECEIVING:
            await self._send_quiet(
                packets.file_error_frame(transfer_id, "unknown_transfer", "unknown transfer")
            )
            return
        if transfer.state is TransferState.OFFERED:
            _logger.debug("chunk for unaccepted offer %s — dropped", transfer_id)
            return
        if transfer.is_terminal or transfer.state is TransferState.PAUSED:
            return
        n = int(frame.data["n"])
        if not 1 <= n <= transfer.total_chunks:
            _logger.info("chunk %d outside 1..%d for %s", n, transfer.total_chunks, transfer_id)
            await self._fail_transfer(transfer, f"chunk number {n} is out of range")
            await self._send_quiet(
                packets.file_error_frame(transfer_id, "bad_chunk", f"chunk {n} out of range")
            )
            return
        sealed = self._b64(frame, "ct")
        expected = self._expected_chunk_length(transfer, n)
        if len(sealed) != expected + 12 + 16:
            _logger.info("chunk %d of %s has a bad length — integrity risk", n, transfer_id)
            await self._integrity_failure(transfer, f"chunk {n} was truncated or padded")
            return
        if self._bitmaps[transfer_id].has(n):
            # Duplicate / replayed chunk: re-ack, never rewrite, never count.
            await self._send_quiet(packets.file_ack_frame(transfer_id, n))
            _logger.debug("duplicate chunk %d of %s re-acked", n, transfer_id)
            return
        key = self._key_for(transfer_id)
        if key is None:
            _logger.debug("chunk %d of %s with no key yet — dropped", n, transfer_id)
            return
        try:
            plaintext = open_chunk(bytes(key), transfer_id, n, sealed)
        except DecryptionError:
            await self._integrity_failure(transfer, f"chunk {n} failed verification")
            return
        if len(plaintext) != expected:
            await self._integrity_failure(transfer, f"chunk {n} has an invalid size")
            return
        try:
            await asyncio.to_thread(
                self._storage.write_chunk, transfer_id, n, transfer.chunk_size, plaintext
            )
        except StorageError as exc:
            await self._fail_transfer(transfer, exc.message)
            await self._send_quiet(
                packets.file_error_frame(transfer_id, "storage", "write failed on the receiver")
            )
            return
        bitmap = self._bitmaps[transfer_id]
        bitmap.mark(n)
        transfer.chunks_done = bitmap.count
        transfer.bytes_done = min(transfer.size_bytes, bitmap.count * transfer.chunk_size)
        transfer.touch()
        if transfer.state is TransferState.ACCEPTED:
            transfer.transition_to(TransferState.TRANSFERRING)
            self._emit(TransferEventKind.STATE, transfer, "Receiving…")
        await self._send_quiet(packets.file_ack_frame(transfer_id, n))
        self._maybe_progress(transfer)
        if bitmap.is_complete:
            await self._finish_receiving(transfer)

    async def _on_ack(self, frame: Frame) -> None:
        transfer_id = str(frame.data["id"])
        transfer = self._transfers.get(transfer_id)
        if transfer is None or transfer.direction is not TransferDirection.SENDING:
            return
        n = int(frame.data["n"])
        if not 1 <= n <= transfer.total_chunks:
            _logger.info(
                "ack %d outside 1..%d for %s — ignored", n, transfer.total_chunks, transfer_id
            )
            return
        bitmap = self._bitmaps[transfer_id]
        if bitmap.mark(n):
            transfer.chunks_done = bitmap.count
            transfer.bytes_done = min(transfer.size_bytes, bitmap.count * transfer.chunk_size)
            transfer.touch()
            self._maybe_progress(transfer)
        self._kick()

    async def _on_pause(self, frame: Frame) -> None:
        transfer = self._transfers.get(str(frame.data["id"]))
        if transfer is None or transfer.state is not TransferState.TRANSFERRING:
            return
        transfer.resume_notified = True
        transfer.transition_to(TransferState.PAUSED)
        self._emit(TransferEventKind.STATE, transfer, "Paused by the peer.")
        self._kick()
        _logger.info("transfer paused by peer — %s", transfer.transfer_id)

    async def _on_resume(self, frame: Frame) -> None:
        transfer = self._transfers.get(str(frame.data["id"]))
        if transfer is None:
            return
        resumed = False
        if transfer.state is TransferState.PAUSED:
            transfer.resume_notified = True
            transfer.transition_to(TransferState.TRANSFERRING)
            resumed = True
            self._emit(TransferEventKind.STATE, transfer, "Resumed.")
        if transfer.direction is TransferDirection.RECEIVING:
            # Truth sync: the sender re-seeds its ACK log from our bitmap.
            if transfer.state is TransferState.COMPLETED:
                digest = self._require_manifest(transfer.transfer_id).sha256
                await self._send_quiet(packets.file_complete_frame(transfer.transfer_id, digest))
            elif transfer.state in (TransferState.ACCEPTED, TransferState.TRANSFERRING):
                await self._share_bitmap(transfer)
        if resumed:
            self._kick()
            _logger.info("transfer resumed — %s", transfer.transfer_id)

    async def _on_cancel(self, frame: Frame) -> None:
        transfer = self._transfers.get(str(frame.data["id"]))
        if transfer is None or transfer.is_terminal:
            return
        reason = str(frame.data.get("reason") or "")
        self._settle_terminal(transfer, TransferState.CANCELLED, error=reason or None)
        self._emit(
            TransferEventKind.STATE,
            transfer,
            f"Cancelled by the peer{': ' + reason if reason else '.'}",
        )
        self._kick()
        _logger.info("transfer cancelled by peer — %s", transfer.transfer_id)

    async def _on_complete(self, frame: Frame) -> None:
        transfer_id = str(frame.data["id"])
        transfer = self._transfers.get(transfer_id)
        if transfer is None or transfer.direction is not TransferDirection.SENDING:
            return
        if transfer.state not in (TransferState.ACCEPTED, TransferState.TRANSFERRING):
            return
        digest = str(frame.data["sha256"])
        if digest != transfer.integrity:
            _logger.info("COMPLETE digest mismatch for %s — failing", transfer_id)
            await self._fail_transfer(transfer, "the peer verified a different file")
            return
        transfer.chunks_done = transfer.total_chunks
        transfer.bytes_done = transfer.size_bytes
        self._maybe_progress(transfer, force=True)  # flush the 100% line
        self._settle_terminal(transfer, TransferState.COMPLETED)
        self._emit(TransferEventKind.STATE, transfer, "Transfer completed ✓")
        self._record_history(transfer)
        self._kick()
        _logger.info("transfer completed — %s (sender)", transfer_id)

    async def _on_error_frame(self, frame: Frame) -> None:
        transfer_id = str(frame.data["id"])
        transfer = self._transfers.get(transfer_id)
        message = str(frame.data.get("message", "peer error"))[:120]
        if transfer is None:
            self._emit(
                TransferEventKind.NOTICE,
                detail=f"The peer reported a transfer problem: {message}",
            )
            return
        if transfer.is_terminal:
            return
        self._settle_terminal(transfer, TransferState.FAILED, error=message)
        self._emit(TransferEventKind.STATE, transfer, f"Failed — {message}")
        self._kick()
        _logger.info("transfer failed by peer notice — %s (%s)", transfer_id, message)

    # ------------------------------------------------------- receiving tail

    async def _finish_receiving(self, transfer: Transfer) -> None:
        """All chunks in: verify the whole file, then publish or purge."""

        transfer_id = transfer.transfer_id
        manifest = self._require_manifest(transfer_id)
        try:
            digest = await asyncio.to_thread(
                hash_temp,
                self._storage.temp_path(transfer_id),
                expected_size=manifest.size_bytes,
            )
        except (DecryptionError, OSError) as exc:
            await self._integrity_failure(
                transfer, str(getattr(exc, "message", "verification failed"))
            )
            return
        if digest != manifest.sha256:
            _logger.info("integrity check failed for %s — purging partial file", transfer_id)
            await self._integrity_failure(transfer, "integrity verification failed")
            return
        destination = self._destinations.get(transfer_id)
        if destination is None:
            destination = await asyncio.to_thread(self._storage.destination_for, manifest.filename)
        try:
            saved = await asyncio.to_thread(self._storage.finalize, transfer_id, destination)
        except (StorageError, TransferValidationError) as exc:
            await self._fail_transfer(transfer, getattr(exc, "message", "finalize failed"))
            await self._send_quiet(
                packets.file_error_frame(transfer_id, "storage", "could not save the file")
            )
            return
        transfer.saved_path = str(saved)
        self._maybe_progress(transfer, force=True)  # flush the 100% line
        self._settle_terminal(transfer, TransferState.COMPLETED)
        self._emit(TransferEventKind.STATE, transfer, "Integrity verification… Verified ✓")
        self._emit(TransferEventKind.SAVED, transfer, str(saved))
        self._record_history(transfer)
        await self._send_quiet(packets.file_complete_frame(transfer_id, digest))
        _logger.info("transfer completed — %s (receiver)", transfer_id)

    async def _integrity_failure(self, transfer: Transfer, message: str) -> None:
        """Tampering/corruption: fail loudly, purge partials, tell the peer."""

        _logger.info("integrity failure — %s (%s)", transfer.transfer_id, message)
        await self._fail_transfer(transfer, message)
        await self._send_quiet(
            packets.file_error_frame(
                transfer.transfer_id, "integrity", "a chunk failed integrity verification"
            )
        )
        self._emit(
            TransferEventKind.NOTICE,
            detail=f"{transfer.filename}: {message} — the partial download was deleted. "
            "The file may have been tampered with in transit.",
        )

    async def _fail_transfer(self, transfer: Transfer, message: str) -> None:
        """Local failure path: terminal FAILED + temp purge + queue promotion."""

        if transfer.is_terminal:
            return
        self._settle_terminal(transfer, TransferState.FAILED, error=message)
        self._emit(TransferEventKind.STATE, transfer, f"Failed — {message}")
        self._kick()

    # ------------------------------------------------------------- sending

    async def _offer(self, transfer: Transfer, *, announce: bool = False) -> None:
        """Seal the manifest and send FILE_OFFER for a queued send."""

        key = self._key_for(transfer.transfer_id)
        if key is None:
            raise TransferError(
                "Encryption is not active yet.",
                hint="Wait for 'Encryption: Active' before sending files.",
            )
        manifest = self._require_manifest(transfer.transfer_id)
        sealed = seal_manifest(bytes(key), transfer.transfer_id, manifest.to_json())
        if transfer.state is TransferState.QUEUED:
            transfer.transition_to(TransferState.OFFERED)
        transfer.touch()
        await self._send_quiet(packets.file_offer_frame(transfer.transfer_id, sealed))
        if announce:
            self._emit(
                TransferEventKind.STATE,
                transfer,
                "Encrypted transfer ready. Waiting for peer approval…",
            )
        _logger.info("transfer offered — %s", transfer.transfer_id)

    def _adopt_bitmap(self, transfer: Transfer, peer_bitmap: ChunkBitmap) -> None:
        """The receiver's bitmap is authoritative — merge it into the ACK log."""

        bitmap = self._bitmaps[transfer.transfer_id]
        for n in range(1, transfer.total_chunks + 1):
            if peer_bitmap.has(n):
                bitmap.mark(n)
        transfer.chunks_done = bitmap.count
        transfer.bytes_done = min(transfer.size_bytes, bitmap.count * transfer.chunk_size)

    def _ensure_pump(self, transfer: Transfer) -> None:
        task = self._pumps.get(transfer.transfer_id)
        if task is None or task.done():
            self._pumps[transfer.transfer_id] = asyncio.create_task(
                self._send_pump(transfer.transfer_id)
            )
        self._kick()

    async def _send_pump(self, transfer_id: str) -> None:
        """Sliding-window sender: seal → frame → wait ACK → retry, bounded."""

        reader: ChunkReader | None = None
        in_flight: dict[int, float] = {}
        attempts: dict[int, int] = {}
        probes = 0
        all_acked_at: float | None = None
        try:
            while not self._closing:
                transfer = self._transfers.get(transfer_id)
                if transfer is None or transfer.is_terminal:
                    return
                bitmap = self._bitmaps[transfer_id]
                if transfer.state is not TransferState.TRANSFERRING or not self._link_ready():
                    await self._wait_wake(_PUMP_PACE_SECONDS)
                    continue
                key = self._key_for(transfer_id)
                if key is None:
                    await self._wait_wake(_PUMP_PACE_SECONDS)
                    continue
                if reader is None:
                    try:
                        assert transfer.source_path is not None
                        reader = ChunkReader(
                            Path(transfer.source_path),
                            chunk_size=transfer.chunk_size,
                            total_chunks=transfer.total_chunks,
                        )
                    except (OSError, TransferValidationError) as exc:
                        message = getattr(exc, "strerror", None) or str(exc)
                        await self._fail_transfer(
                            transfer, f"could not open the source file ({message})"
                        )
                        await self._send_quiet(
                            packets.file_error_frame(
                                transfer_id, "source", "source file unreadable"
                            )
                        )
                        return
                now = time.monotonic()
                window_free = TRANSFER_WINDOW_CHUNKS - len(in_flight)
                for n in bitmap.missing():
                    if window_free <= 0:
                        break
                    if n in in_flight:
                        continue
                    try:
                        sealed = seal_chunk(bytes(key), transfer_id, n, reader.read(n))
                    except (OSError, TransferValidationError) as exc:
                        message = getattr(exc, "message", None) or str(exc)
                        await self._fail_transfer(transfer, str(message))
                        await self._send_quiet(
                            packets.file_error_frame(transfer_id, "source", str(message)[:120])
                        )
                        return
                    await self._send_quiet(packets.file_chunk_frame(transfer_id, n, sealed))
                    in_flight[n] = now
                    attempts[n] = attempts.get(n, 0) + 1
                    window_free -= 1
                for n in list(in_flight):
                    if bitmap.has(n):
                        del in_flight[n]
                now = time.monotonic()
                for n, sent_at in list(in_flight.items()):
                    if now - sent_at < self._limits.ack_timeout_seconds:
                        continue
                    if attempts.get(n, 0) >= self._limits.retry_limit:
                        await self._fail_transfer(
                            transfer, f"chunk {n} was not acknowledged after retries"
                        )
                        await self._send_quiet(
                            packets.file_error_frame(
                                transfer_id, "timeout", "chunk acknowledgements timed out"
                            )
                        )
                        return
                    _logger.info(
                        "chunk retry — %s chunk %d (attempt %d)",
                        transfer_id,
                        n,
                        attempts[n] + 1,
                    )
                    try:
                        sealed = seal_chunk(bytes(key), transfer_id, n, reader.read(n))
                    except (OSError, TransferValidationError) as exc:
                        message = getattr(exc, "message", None) or str(exc)
                        await self._fail_transfer(transfer, str(message))
                        return
                    await self._send_quiet(packets.file_chunk_frame(transfer_id, n, sealed))
                    in_flight[n] = now
                    attempts[n] = attempts.get(n, 0) + 1
                if not bitmap.is_complete:
                    if in_flight:
                        next_deadline = min(
                            (sent_at + self._limits.ack_timeout_seconds) - now
                            for sent_at in in_flight.values()
                        )
                        await self._wait_wake(max(0.02, min(_PUMP_PACE_SECONDS, next_deadline)))
                    else:
                        await self._wait_wake(_PUMP_PACE_SECONDS)
                    continue
                # Fully acked: wait for FILE_COMPLETE, probing periodically.
                if all_acked_at is None:
                    all_acked_at = now
                if now - all_acked_at >= (
                    _COMPLETE_PROBE_MULTIPLIER * self._limits.ack_timeout_seconds
                ):
                    if probes >= self._limits.retry_limit:
                        await self._fail_transfer(
                            transfer, "the peer went silent before confirming completion"
                        )
                        return
                    probes += 1
                    all_acked_at = now
                    _logger.info("completion probe — %s (probe %d)", transfer_id, probes)
                    await self._send_quiet(packets.file_resume_frame(transfer_id))
                await self._wait_wake(self._limits.ack_timeout_seconds)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # a broken pump must surface, not vanish
            _logger.info("send pump failed — %s: %s", transfer_id, exc)
            transfer = self._transfers.get(transfer_id)
            if transfer is not None and not transfer.is_terminal:
                await self._fail_transfer(transfer, "internal send error")
        finally:
            if reader is not None:
                reader.close()
            self._pumps.pop(transfer_id, None)

    # ----------------------------------------------------- chat integration

    def _on_chat_event(self, event: ChatEvent) -> None:
        """Auto-pause on link loss; auto-resume after the session re-keys."""

        if self._closing:
            return
        if event.kind is ChatEventKind.CONNECTION and (
            "lost" in event.detail
            or "Disconnected" in event.detail
            or "closed" in event.detail.lower()
        ):
            self._auto_pause("Connection lost")
        elif event.kind is ChatEventKind.PEER and "left" in event.detail:
            self._auto_pause("Peer left")
        elif event.kind is ChatEventKind.SESSION and "Active" in event.detail:
            self._wipe_keys()  # session re-keyed: per-transfer keys are stale
            self._spawn(self._recover_transfers())
        self._kick()

    def _auto_pause(self, reason: str) -> None:
        for transfer in self._transfers.values():
            if transfer.state is TransferState.TRANSFERRING:
                transfer.resume_notified = False  # auto-resume after recovery
                transfer.transition_to(TransferState.PAUSED)
                self._emit(
                    TransferEventKind.STATE,
                    transfer,
                    f"{reason} — transfer paused; it will resume automatically.",
                )
                _logger.info("auto-paused %s (%s)", transfer.transfer_id, reason)

    async def _recover_transfers(self) -> None:
        """After (re)keying: re-offer pending sends, resume paused ones."""

        if self._closing or not self._link_ready():
            return
        for transfer in list(self._transfers.values()):
            if transfer.direction is not TransferDirection.SENDING:
                continue
            if transfer.state is TransferState.OFFERED:
                try:
                    await self._offer(transfer)
                except TransferError as exc:
                    _logger.info("re-offer of %s failed — %s", transfer.transfer_id, exc.message)
            elif transfer.state is TransferState.PAUSED and not transfer.resume_notified:
                transfer.resume_notified = True
                transfer.transition_to(TransferState.TRANSFERRING)
                self._emit(TransferEventKind.STATE, transfer, "Connection restored — resuming…")
                await self._send_quiet(packets.file_resume_frame(transfer.transfer_id))
                self._ensure_pump(transfer)
        self._kick()

    # -------------------------------------------------------------- sweeping

    async def _sweep_loop(self) -> None:
        while not self._closing:
            await asyncio.sleep(_SWEEP_TICK_SECONDS)
            try:
                await self._sweep()
            except Exception as exc:
                _logger.debug("transfer sweep failed: %s", exc)

    async def _sweep(self) -> None:
        """Expiry, offer re-sends, and bounded memory for terminal transfers."""

        now = time.time()
        for transfer in list(self._transfers.values()):
            tid = transfer.transfer_id
            if transfer.is_terminal:
                if now - transfer.last_activity_at > self._limits.terminal_ttl_seconds:
                    self._purge(transfer)
                continue
            offer_retry_after = _OFFER_RESEND_MULTIPLIER * self._limits.ack_timeout_seconds
            if transfer.state is TransferState.OFFERED:
                if now - transfer.created_at > self._limits.offer_timeout_seconds:
                    _logger.info("offer expired — %s", tid)
                    self._settle_terminal(transfer, TransferState.EXPIRED, error="offer expired")
                    await self._send_quiet(
                        packets.file_cancel_frame(tid, reason="the offer expired")
                    )
                    self._emit(
                        TransferEventKind.STATE,
                        transfer,
                        "Offer expired without an answer."
                        if transfer.direction is TransferDirection.RECEIVING
                        else "The peer did not answer — offer expired.",
                    )
                elif (
                    transfer.direction is TransferDirection.SENDING
                    and self._link_ready()
                    and now - transfer.last_activity_at > offer_retry_after
                ):
                    sent = self._offer_attempts.get(tid, 1)
                    if sent < self._limits.retry_limit:
                        self._offer_attempts[tid] = sent + 1
                        _logger.info("offer retry — %s (attempt %d)", tid, sent + 1)
                        self._spawn(self._offer(transfer))
            elif now - transfer.created_at > self._limits.transfer_expiry_seconds:
                _logger.info("transfer expired — %s", tid)
                self._settle_terminal(transfer, TransferState.EXPIRED, error="transfer expired")
                await self._send_quiet(
                    packets.file_cancel_frame(tid, reason="the transfer expired")
                )
                self._emit(
                    TransferEventKind.STATE,
                    transfer,
                    f"Expired after {format_bytes(transfer.bytes_done)} moved — partials deleted.",
                )
        self._kick()

    def _purge(self, transfer: Transfer) -> None:
        """Forget one terminal transfer entirely (bounded memory)."""

        tid = transfer.transfer_id
        self._transfers.pop(tid, None)
        self._manifests.pop(tid, None)
        self._bitmaps.pop(tid, None)
        self._destinations.pop(tid, None)
        self._offer_attempts.pop(tid, None)
        self._last_progress.pop(tid, None)
        self._drop_key(tid)
        _logger.debug("transfer purged from memory — %s", tid)

    # --------------------------------------------------------------- helpers

    def _active_count(self) -> int:
        return sum(1 for t in self._transfers.values() if t.state in ACTIVE_STATES)

    def _reserved_incoming_bytes(self) -> int:
        """Bytes pledged to active incoming transfers (not all on disk yet)."""

        return sum(
            t.size_bytes
            for t in self._transfers.values()
            if t.direction is TransferDirection.RECEIVING and t.state in ACTIVE_STATES
        )

    def _settle_terminal(
        self,
        transfer: Transfer,
        state: TransferState,
        *,
        error: str | None = None,
    ) -> None:
        """Terminal transition + temp purge + queue promotion (never silent)."""

        try:
            transfer.transition_to(state, error=error)
        except TransferStateError:
            # Terminal states are idempotent: a duplicate settle is harmless.
            if transfer.state is state:
                return
            raise
        self._drop_key(transfer.transfer_id)
        receiving = transfer.direction is TransferDirection.RECEIVING
        if receiving and state is not TransferState.COMPLETED:
            try:
                self._storage.delete_temp(transfer.transfer_id)
            except StorageError as exc:
                _logger.info("temp purge failed — %s", exc.message)
        self._promote_queued()

    def _promote_queued(self) -> None:
        if self._closing:
            return
        queued = [
            t
            for t in sorted(self._transfers.values(), key=lambda item: item.created_at)
            if t.direction is TransferDirection.SENDING and t.state is TransferState.QUEUED
        ]
        for transfer in queued:
            if self._active_count() >= self._limits.max_concurrent:
                break
            self._spawn(self._offer(transfer))

    def _record_history(self, transfer: Transfer) -> None:
        if self._history is None:
            return
        self._history.record_transfer(
            transfer_id=transfer.transfer_id,
            conversation_id=self._session.conversation_id or "",
            direction=_DIRECTION_TO_HISTORY[transfer.direction],
            peer=transfer.peer,
            summary=f"{transfer.filename} ({format_bytes(transfer.size_bytes)})",
            status=transfer.state.value,
        )

    async def _share_bitmap(self, transfer: Transfer) -> None:
        bitmap = self._bitmaps.get(transfer.transfer_id)
        if bitmap is None:
            return
        bitmap_b64 = bitmap.to_b64() if bitmap.count else None
        await self._send_quiet(
            packets.file_accept_frame(transfer.transfer_id, bitmap_b64=bitmap_b64)
        )

    def _expected_chunk_length(self, transfer: Transfer, n: int) -> int:
        if n == transfer.total_chunks:
            tail = transfer.size_bytes - (transfer.total_chunks - 1) * transfer.chunk_size
            return max(1, tail)
        return transfer.chunk_size

    def _link_ready(self) -> bool:
        return self._session.is_ready and self._session.peer_present

    def _session_key(self) -> bytes | None:
        return self._session.session_key_copy()

    def _key_for(self, transfer_id: str) -> bytearray | None:
        key = self._keys.get(transfer_id)
        if key is not None:
            return key
        session_key = self._session_key()
        if session_key is None:
            return None
        derived = bytearray(derive_transfer_key(session_key, transfer_id))
        self._keys[transfer_id] = derived
        return derived

    def _drop_key(self, transfer_id: str) -> None:
        key = self._keys.pop(transfer_id, None)
        if key is not None:
            for index in range(len(key)):
                key[index] = 0

    def _wipe_keys(self) -> None:
        for transfer_id in list(self._keys):
            self._drop_key(transfer_id)

    @staticmethod
    def _b64(frame: Frame, field_name: str) -> bytes:
        """Decode a validated base64 field (validation already ran in frames)."""

        value = str(frame.data[field_name])
        try:
            return base64.b64decode(value.encode("ascii"), validate=True)
        except (binascii.Error, UnicodeEncodeError) as exc:  # pragma: no cover
            raise TransferValidationError(
                f"Frame {frame.type.value} carried invalid base64 in '{field_name}'.",
                hint="The secure-channel codec should have rejected this frame.",
            ) from exc

    async def _send_quiet(self, frame: Frame) -> None:
        """Best-effort frame send — failures are logged, never raised."""

        try:
            await self._session.send_channel_frame(frame)
        except (TransportError, GhostLinkError) as exc:
            _logger.debug("frame %s was not sent: %s", frame.type.value, exc)

    def _kick(self) -> None:
        self._wake.set()

    async def _wait_wake(self, timeout: float) -> None:
        self._wake.clear()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), timeout=timeout)

    def _spawn(self, coroutine: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(coroutine)
        self._background.add(task)
        task.add_done_callback(self._background.discard)
        task.add_done_callback(self._observe_task_failure)

    @staticmethod
    def _observe_task_failure(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        failure = task.exception()
        if failure is not None:
            _logger.debug("background transfer task failed: %s", failure)
