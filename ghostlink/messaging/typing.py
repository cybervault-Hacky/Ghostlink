"""Typing indicators (Phase 3).

Two independent concerns live here:

* **Outgoing throttling** — the UI reports keystrokes constantly; the
  indicator decides when a TYPING_START frame is actually due (at most one
  per interval) and when the peer should be told we stopped (idle timeout).
* **Incoming state** — the peer's TYPING_START/STOp frames become a small
  expiring state machine the UI can poll; a TYPING_START older than the
  expiry (whose STOP got lost) simply ages out instead of sticking forever.

No message content is involved at any point — typing frames are empty.
"""

from __future__ import annotations

import time

from ghostlink.exceptions.messaging import MessageValidationError

DEFAULT_START_INTERVAL_SECONDS: float = 3.0
DEFAULT_IDLE_SECONDS: float = 4.0
DEFAULT_PEER_EXPIRY_SECONDS: float = 6.0


class TypingIndicator:
    """Throttled outgoing and expiring incoming typing state."""

    def __init__(
        self,
        *,
        start_interval_seconds: float = DEFAULT_START_INTERVAL_SECONDS,
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        peer_expiry_seconds: float = DEFAULT_PEER_EXPIRY_SECONDS,
    ) -> None:
        for name, value in (
            ("start_interval_seconds", start_interval_seconds),
            ("idle_seconds", idle_seconds),
            ("peer_expiry_seconds", peer_expiry_seconds),
        ):
            if value <= 0:
                raise MessageValidationError(
                    f"Typing timing '{name}' must be positive, got {value}.",
                    hint="Use small positive second values, e.g. 3.0.",
                )
        self._start_interval = start_interval_seconds
        self._idle = idle_seconds
        self._peer_expiry = peer_expiry_seconds

        self._local_active = False
        self._last_start_sent = 0.0
        self._last_keystroke = 0.0
        self._peer_typing_since: float | None = None

    # --------------------------------------------------------------- outgoing

    def note_keystroke(self, *, at: float | None = None) -> bool:
        """A keystroke happened → True when a TYPING_START frame is due."""

        now = time.monotonic() if at is None else at
        self._last_keystroke = now
        if not self._local_active or (now - self._last_start_sent) >= self._start_interval:
            self._local_active = True
            self._last_start_sent = now
            return True
        return False

    def note_sent_or_stopped(self) -> bool:
        """Message sent / input cleared → True when TYPING_STOP is due."""

        if self._local_active:
            self._local_active = False
            return True
        return False

    def idle_elapsed(self, *, at: float | None = None) -> bool:
        """True when we are 'typing' but no keystroke arrived within idle."""

        if not self._local_active:
            return False
        now = time.monotonic() if at is None else at
        return (now - self._last_keystroke) >= self._idle

    @property
    def idle_seconds(self) -> float:
        return self._idle

    # --------------------------------------------------------------- incoming

    def peer_started(self, *, at: float | None = None) -> None:
        self._peer_typing_since = time.monotonic() if at is None else at

    def peer_stopped(self) -> None:
        self._peer_typing_since = None

    def peer_typing(self, *, at: float | None = None) -> bool:
        """True while a peer TYPING_START is fresh (STOP never required)."""

        if self._peer_typing_since is None:
            return False
        now = time.monotonic() if at is None else at
        if (now - self._peer_typing_since) >= self._peer_expiry:
            self._peer_typing_since = None
            return False
        return True

    # ----------------------------------------------------------------- cleanup

    def reset(self) -> None:
        self._local_active = False
        self._last_keystroke = 0.0
        self._last_start_sent = 0.0
        self._peer_typing_since = None
