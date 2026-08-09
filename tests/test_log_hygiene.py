"""Phase 8 log & exception secret-hygiene regression tests.

GhostLink never *intentionally* logs secrets; these tests make the promise
mechanically checkable by injecting secrets through the logging pipeline
and verifying the Phase 8 redaction backstop keeps them out of the emitted
records. They also assert that the sender-key / mesh code never constructs
log messages carrying key material (source-level scan as a tripwire).
"""

from __future__ import annotations

import io
import logging

from ghostlink.core.logging import (
    RedactingFilter,
    get_logger,
    redact_secrets,
    register_secret,
)


class TestRedactionBackstop:
    def test_registered_secret_scrubbed_from_log(self) -> None:
        logger = get_logger("hygiene.test")
        handler = io.StringIO()
        stream = logging.StreamHandler(handler)
        stream.addFilter(RedactingFilter())
        stream.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(stream)
        try:
            secret = "gli_abcdefghijklmnopqrstuvwx1234"
            register_secret(secret)
            logger.warning("leaked %s in the clear", secret)
        finally:
            logger.removeHandler(stream)
        emitted = handler.getvalue()
        assert secret not in emitted
        assert "<redacted>" in emitted

    def test_token_shape_scrubbed_without_registration(self) -> None:
        token = "gli_ABCDEFGHIJKLMNOPQRST"  # 20-char token, not registered
        redacted = redact_secrets(f"failed to redeem {token} link")
        assert token not in redacted
        assert "<redacted-token>" in redacted

    def test_plain_non_secret_text_untouched(self) -> None:
        message = "group gl-group-AAAA-BBBB-CCCC epoch 3 synced"
        assert redact_secrets(message) == message

    def test_filter_does_not_crash_on_exception_record(self) -> None:
        logger = get_logger("hygiene.test2")
        handler = io.StringIO()
        stream = logging.StreamHandler(handler)
        stream.addFilter(RedactingFilter())
        stream.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(stream)
        try:
            try:
                raise RuntimeError("boom")
            except RuntimeError:
                logger.exception("an exception record")
        finally:
            logger.removeHandler(stream)
        assert "boom" in handler.getvalue()


class TestSecretFreeSource:
    """Tripwire: sender-key / mesh / service code must not build log strings
    that interpolate key-shaped variables."""

    def test_senderkeys_module_has_no_logger(self) -> None:
        # senderkeys.py manages the raw secret material; it must not log at all.
        import pathlib

        source = pathlib.Path("ghostlink/groups/senderkeys.py").read_text(encoding="utf-8")
        for forbidden in ("get_logger", "logging.", "print("):
            assert forbidden not in source, f"senderkeys.py must not call {forbidden!r}"

    def test_key_variables_never_inside_log_call(self) -> None:
        import pathlib

        source = pathlib.Path("ghostlink/groups/service.py").read_text(encoding="utf-8")
        # Tripwire: no log statement contains these secret-bearing names.
        for line in source.splitlines():
            if "_logger." in line and any(
                name in line for name in ("chain_root", "_root", "message_key")
            ):
                raise AssertionError(f"log line references key material: {line.strip()}")
