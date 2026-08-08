"""Transfer cleanup: orphan temp removal and expiry sweeping (Phase 4).

Temp files must never outlive the transfers they belong to. On startup,
any ``.part`` artifact without a live transfer is deleted (a crashed run
never completes a download — partials are only bytes, never presented as
files). While running, the manager's sweeper expires stale offers and
abandoned transfers and removes their temp state.
"""

from __future__ import annotations

from pathlib import Path

from ghostlink.core.logging import get_logger
from ghostlink.transfer.storage import TEMP_SUFFIX

_logger = get_logger("transfer.cleanup")


def cleanup_orphan_temps(temp_dir: Path, *, active_ids: frozenset[str] | None = None) -> int:
    """Delete every ``.part`` file not tied to a live transfer id.

    ``active_ids`` defaults to empty: on first start there are no live
    transfers, so every leftover temp file is orphaned. Returns the number
    of files removed. Only exact ``tf_*.part``-shaped names are touched."""

    active = active_ids or frozenset()
    if not temp_dir.is_dir():
        return 0
    removed = 0
    for entry in temp_dir.iterdir():
        if not (entry.is_file() and entry.suffix == TEMP_SUFFIX):
            continue
        transfer_id = entry.name[: -len(TEMP_SUFFIX)]
        if not transfer_id.startswith("tf_") or transfer_id in active:
            continue
        try:
            entry.unlink()
            removed += 1
            _logger.info("removed orphaned temp file %s", entry.name)
        except OSError:
            _logger.warning("could not remove orphaned temp file %s", entry.name)
    return removed
