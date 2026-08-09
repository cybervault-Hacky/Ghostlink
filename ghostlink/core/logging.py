"""Professional logging infrastructure.

A single ``ghostlink`` root logger feeds two sinks:

* a rotating log file inside the data directory (always on, capped in size)
* a Rich console handler attached to stderr — only in Debug Mode, so the
  interface stays clean during normal operation

File logging degrades gracefully: if the log directory is not writable, the
application continues without a file sink and reports the reason through the
returned :class:`LoggingReport`.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.logging import RichHandler

from ghostlink.constants.files import LOG_FILE_NAME

ROOT_LOGGER_NAME = "ghostlink"
LOG_MAX_BYTES = 512 * 1024
LOG_BACKUP_COUNT = 3
_FILE_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class SecretRedactor:
    """Defense-in-depth log scrubbing (Phase 8).

    GhostLink already never *intentionally* logs secrets; this is a
    backstop so that even a slip in some code path cannot leak key material,
    invite tokens, or other registered secrets into logs or debug output.

    Two scrubbing layers:

    * exact values registered at runtime via :meth:`register_secret` (e.g.
      the invite token at mint time) are replaced everywhere they appear;
    * well-known secret *shapes* — one-time invite tokens and join links —
      are scrubbed by pattern so unregistered tokens are still protected.
    """

    _TOKEN_PATTERN = re.compile(r"\bgli_[A-Za-z0-9]{20}\b")

    def __init__(self) -> None:
        self._exact: set[str] = set()

    def register_secret(self, value: str) -> None:
        if isinstance(value, str) and value:
            self._exact.add(value)

    def redact(self, text: str) -> str:
        for value in self._exact:
            text = text.replace(value, "<redacted>")
        return self._TOKEN_PATTERN.sub("<redacted-token>", text)


_secret_redactor = SecretRedactor()


def register_secret(value: str) -> None:
    """Register a runtime secret (e.g. an invite token) for log scrubbing."""
    _secret_redactor.register_secret(value)


def redact_secrets(text: str) -> str:
    """Public scrubbing helper (also usable in exception formatting)."""
    return _secret_redactor.redact(text)


class RedactingFilter(logging.Filter):
    """A logging filter that scrubs secrets from every record it passes."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        redacted = _secret_redactor.redact(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


@dataclass(frozen=True, slots=True)
class LoggingReport:
    """The outcome of logging initialisation."""

    file_path: Path | None
    file_logging: bool
    debug: bool
    warning: str | None = None


def get_logger(name: str) -> logging.Logger:
    """Return a child of the ``ghostlink`` root logger."""

    return logging.getLogger(f"{ROOT_LOGGER_NAME}.{name}")


def setup_logging(*, log_dir: Path, debug: bool, echo_debug: bool = True) -> LoggingReport:
    """Configure the ``ghostlink`` logger hierarchy and return a report.

    The file sink records INFO by default and DEBUG when Debug Mode is on.
    The console sink (stderr, Rich-formatted) is only attached in Debug Mode
    when ``echo_debug`` is set, keeping the interactive UI pristine.
    """

    root = logging.getLogger(ROOT_LOGGER_NAME)
    root.setLevel(logging.DEBUG)
    root.propagate = False
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    file_level = logging.DEBUG if debug else logging.INFO
    file_path: Path | None = None
    file_logging = True
    warning: str | None = None

    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_path = log_dir / LOG_FILE_NAME
        file_handler: logging.Handler = RotatingFileHandler(
            file_path,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(logging.Formatter(_FILE_FORMAT, datefmt=_DATE_FORMAT))
    except OSError as exc:
        file_logging = False
        file_path = None
        warning = f"File logging unavailable ({exc.strerror or exc})."
        file_handler = logging.NullHandler()

    file_handler.setLevel(file_level)
    # Phase 8 backstop: scrub registered secrets + token shapes from all logs.
    root.addFilter(RedactingFilter())
    root.addHandler(file_handler)

    if debug and echo_debug:
        console_handler = RichHandler(
            level=logging.DEBUG,
            markup=False,
            rich_tracebacks=True,
            show_path=True,
            show_time=True,
        )
        console_handler.setLevel(logging.DEBUG)
        root.addHandler(console_handler)

    logging.captureWarnings(True)
    return LoggingReport(
        file_path=file_path,
        file_logging=file_logging,
        debug=debug,
        warning=warning,
    )
