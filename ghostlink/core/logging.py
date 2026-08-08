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
