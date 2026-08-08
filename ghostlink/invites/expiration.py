"""Invite expiry helpers (Phase 5).

Two clocks with two jobs:

* **enforcement** — the relay authority uses its own monotonic clock, which
  cannot jump backwards, so a peer's (or your own) system-clock drift can
  neither extend nor shorten an invite. The authority is always the relay.
* **display** — ``expires_at`` is an aware UTC wall-clock instant so both
  terminals can render the same deadline; countdowns tick on the local
  monotonic clock.

Duration text accepts ``900``, ``15s``, ``5m``, ``1h`` forms.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

from ghostlink.exceptions.invites import InviteValidationError

_DURATION_PATTERN: re.Pattern[str] = re.compile(r"^(\d+)(s|m|h)?$", re.IGNORECASE)

_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600}


def parse_duration_seconds(text: str) -> int:
    """Parse ``"2s"`` / ``"5m"`` / ``"1h"`` / ``"900"`` into seconds."""

    cleaned = text.strip().lower()
    match = _DURATION_PATTERN.fullmatch(cleaned)
    if match is None:
        raise InviteValidationError(
            f"Cannot parse '{text}' as a duration.",
            hint="Use seconds with an optional unit: 30s, 5m, 1h.",
        )
    amount = int(match.group(1))
    unit = match.group(2) or "s"
    return amount * _UNIT_SECONDS[unit]


def format_duration_words(seconds: float) -> str:
    """``2 seconds`` / ``1 minute`` / ``3 hours`` for invite panels."""

    whole = int(seconds)
    if whole < 60:
        return f"{whole} second" + ("s" if whole != 1 else "")
    if whole < 3600 and whole % 60 == 0:
        minutes = whole // 60
        return f"{minutes} minute" + ("s" if minutes != 1 else "")
    if whole % 3600 == 0:
        hours = whole // 3600
        return f"{hours} hour" + ("s" if hours != 1 else "")
    minutes = whole // 60
    remainder = whole % 60
    return f"{minutes}m {remainder}s"


def format_countdown(seconds: float) -> str:
    """``MM:SS`` countdown display, clamped at zero and one hour."""

    whole = max(0, int(seconds))
    minutes, secs = divmod(whole, 60)
    if minutes >= 60:
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def expiry_from_now(seconds: float, *, now: datetime | None = None) -> datetime:
    """Aware-UTC deadline ``seconds`` in the future from ``now``."""

    if seconds <= 0:
        raise InviteValidationError(
            "An invite's lifetime must be positive.",
            hint="Choose at least one second, e.g. --expires 30s.",
        )
    moment = now if now is not None else datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return moment + timedelta(seconds=seconds)


def monotonic_deadline(seconds: float, *, now: float | None = None) -> float:
    """A monotonic deadline for local countdown/disable enforcement."""

    import time

    moment = now if now is not None else time.monotonic()
    return moment + seconds
