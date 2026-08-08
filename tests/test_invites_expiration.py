"""Expiry helpers: duration parsing, countdown formatting, clock handling."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ghostlink.exceptions.invites import InviteValidationError
from ghostlink.invites.expiration import (
    expiry_from_now,
    format_countdown,
    format_duration_words,
    monotonic_deadline,
    parse_duration_seconds,
)


class TestDurationParsing:
    @pytest.mark.parametrize(
        ("text", "seconds"),
        [
            ("2s", 2),
            ("5s", 5),
            ("30s", 30),
            ("1m", 60),
            ("5m", 300),
            ("15m", 900),
            ("30m", 1800),
            ("1h", 3600),
            ("900", 900),
            ("45", 45),
            (" 10m ", 600),
        ],
    )
    def test_valid_durations(self, text: str, seconds: int) -> None:
        assert parse_duration_seconds(text) == seconds

    @pytest.mark.parametrize("text", ["", "x", "1d", "-5s", "1.5m", "s", "ms30", "1 m"])
    def test_invalid_durations(self, text: str) -> None:
        with pytest.raises(InviteValidationError):
            parse_duration_seconds(text)


class TestFormatting:
    def test_words(self) -> None:
        assert format_duration_words(2) == "2 seconds"
        assert format_duration_words(1) == "1 second"
        assert format_duration_words(60) == "1 minute"
        assert format_duration_words(300) == "5 minutes"
        assert format_duration_words(3600) == "1 hour"
        assert format_duration_words(7200) == "2 hours"
        assert format_duration_words(95) == "1m 35s"

    def test_countdown(self) -> None:
        assert format_countdown(4) == "00:04"
        assert format_countdown(59) == "00:59"
        assert format_countdown(61) == "01:01"
        assert format_countdown(3599) == "59:59"
        assert format_countdown(3600) == "01:00:00"
        assert format_countdown(0) == "00:00"
        assert format_countdown(-3) == "00:00"


class TestClocks:
    def test_expiry_is_aware_utc(self) -> None:
        now = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
        expiry = expiry_from_now(2, now=now)
        assert expiry.tzinfo is UTC
        assert (expiry - now).total_seconds() == 2

    def test_expiry_requires_positive_lifetime(self) -> None:
        with pytest.raises(InviteValidationError):
            expiry_from_now(0)
        with pytest.raises(InviteValidationError):
            expiry_from_now(-1)

    def test_monotonic_deadline_moves_forward(self) -> None:
        import time

        start = time.monotonic()
        deadline = monotonic_deadline(5, now=start)
        assert deadline == start + 5
        assert monotonic_deadline(0.05) > start
