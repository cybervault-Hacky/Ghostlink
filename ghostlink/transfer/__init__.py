"""Secure encrypted file transfer (Phase 4: Secure File Transfer).

Files move over the existing end-to-end encrypted chat channel — the relay
still only forwards sealed frames:

* :mod:`ghostlink.transfer.models` — the transfer lifecycle state machine
* :mod:`ghostlink.transfer.manifest` — sealed, receiver-validated file metadata
* :mod:`ghostlink.transfer.chunking` — streaming chunk I/O + resume bitmaps
* :mod:`ghostlink.transfer.integrity` — per-transfer keys (HKDF) + AEAD chunks
* :mod:`ghostlink.transfer.storage` — traversal-proof downloads and temp hygiene
* :mod:`ghostlink.transfer.packets` — the ten FILE_* secure-channel frames
* :mod:`ghostlink.transfer.manager` — offer/pump/resume/expiry orchestration
* :mod:`ghostlink.transfer.progress` — bars, speeds, ETAs for the terminal UI
"""

from ghostlink.transfer.chunking import ChunkBitmap, ChunkReader
from ghostlink.transfer.integrity import derive_transfer_key, hash_file
from ghostlink.transfer.manager import (
    TransferEvent,
    TransferEventKind,
    TransferLimits,
    TransferManager,
)
from ghostlink.transfer.manifest import FileManifest
from ghostlink.transfer.models import (
    ACTIVE_STATES,
    TERMINAL_STATES,
    TRANSFER_PROTOCOL,
    Transfer,
    TransferDirection,
    TransferSnapshot,
    TransferState,
    generate_transfer_id,
    is_valid_transfer_id,
)
from ghostlink.transfer.progress import SpeedMeter, progress_line, render_bar, transfers_table
from ghostlink.transfer.storage import TransferStorage, sanitize_filename
from ghostlink.utils.text import format_bytes

__all__ = [
    "ACTIVE_STATES",
    "TERMINAL_STATES",
    "TRANSFER_PROTOCOL",
    "ChunkBitmap",
    "ChunkReader",
    "FileManifest",
    "SpeedMeter",
    "Transfer",
    "TransferDirection",
    "TransferEvent",
    "TransferEventKind",
    "TransferLimits",
    "TransferManager",
    "TransferSnapshot",
    "TransferState",
    "TransferStorage",
    "derive_transfer_key",
    "format_bytes",
    "generate_transfer_id",
    "hash_file",
    "is_valid_transfer_id",
    "progress_line",
    "render_bar",
    "sanitize_filename",
    "transfers_table",
]
