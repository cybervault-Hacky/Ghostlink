"""Argument parsing for the ``ghostlink`` command.

Global flags configure the run; subcommands perform one task and exit.
Precedence is strict: flags beat the configuration file, which beats
platform defaults.
"""

from __future__ import annotations

import argparse
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ghostlink.constants.app import (
    APP_DESCRIPTION,
    APP_NAME,
    APP_VERSION,
    BUILD_DATE,
    RELEASE_LABEL,
)
from ghostlink.ui.themes import ThemeEngine


@dataclass(frozen=True, slots=True)
class CLIOptions:
    """Normalized command-line options consumed by bootstrap and commands."""

    config_path: Path | None
    data_dir: Path | None
    theme: str | None
    debug: bool | None
    no_color: bool
    doctor: bool
    command: str | None = None
    name: str | None = None
    room_id: str | None = None
    lifetime: int | None = None
    invite_lifetime: int | None = None
    multi_use: bool = False
    relay_url: str | None = None
    pings: int | None = None
    timeout: float | None = None
    chat_name: str | None = None


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the ``ghostlink`` command."""

    themes = ", ".join(ThemeEngine().names)
    parser = argparse.ArgumentParser(
        prog="ghostlink",
        description=APP_DESCRIPTION,
        epilog=(
            "Examples:\n"
            "  ghostlink                          Launch the interactive menu\n"
            "  ghostlink host --relay ws://127.0.0.1:8787\n"
            "                                   Create a room and host the chat\n"
            "  ghostlink join gl-room-XXXX-… --relay ws://127.0.0.1:8787\n"
            "                                   Join the room's encrypted chat\n"
            "  ghostlink relay-status --relay ws://127.0.0.1:8787\n"
            "  ghostlink doctor                   Diagnose the environment\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{APP_NAME} {APP_VERSION} ({RELEASE_LABEL}, {BUILD_DATE})",
        help="print the version banner and exit",
    )
    parser.add_argument(
        "--config",
        metavar="FILE",
        type=Path,
        default=None,
        help="use FILE instead of the default configuration file",
    )
    parser.add_argument(
        "--data-dir",
        metavar="DIR",
        type=Path,
        default=None,
        help="store state and logs in DIR (overrides the configuration)",
    )
    parser.add_argument(
        "--theme",
        metavar="NAME",
        choices=sorted(ThemeEngine().names),
        default=None,
        help=f"color theme for this run (choices: {themes})",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        default=None,
        help="enable Debug Mode for this run (verbose logging, tracebacks)",
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        default=False,
        help="disable all color and gradient output",
    )
    parser.add_argument(
        "--doctor",
        action="store_true",
        default=False,
        help="run environment diagnostics and exit (same as the doctor command)",
    )

    subparsers = parser.add_subparsers(dest="command", metavar="COMMAND")

    host = subparsers.add_parser("host", help="create a room and a first invite, then display both")
    host.add_argument("--name", default=None, help="display name for the room")
    host.add_argument(
        "--lifetime",
        type=int,
        default=None,
        metavar="MINUTES",
        help="room lifetime (0 = never expires; default from configuration)",
    )
    host.add_argument(
        "--invite-lifetime",
        type=int,
        default=None,
        metavar="MINUTES",
        help="invite lifetime in minutes (default from configuration)",
    )
    host.add_argument(
        "--multi-use",
        action="store_true",
        default=False,
        help="issue a reusable invite instead of the default one-time invite",
    )
    host.add_argument(
        "--relay",
        dest="relay_url",
        default=None,
        metavar="URL",
        help="relay endpoint for the live chat (falls back to configuration)",
    )
    host.add_argument(
        "--as",
        dest="chat_name",
        default=None,
        metavar="NAME",
        help="display name shown to your peer in the chat",
    )

    join = subparsers.add_parser("join", help="join a room and start the encrypted chat")
    join.add_argument(
        "room_id",
        metavar="gl-room-XXXX-XXXX-XXXX",
        help="the room identifier shared by the host",
    )
    join.add_argument(
        "--relay",
        dest="relay_url",
        default=None,
        metavar="URL",
        help="relay endpoint for the live chat (falls back to configuration)",
    )
    join.add_argument(
        "--as",
        dest="chat_name",
        default=None,
        metavar="NAME",
        help="display name shown to your peer in the chat",
    )

    relay_status = subparsers.add_parser(
        "relay-status", help="probe a relay and render the live status dashboard"
    )
    relay_status.add_argument(
        "--relay",
        dest="relay_url",
        default=None,
        metavar="URL",
        help="relay endpoint (falls back to the configured [relay] url)",
    )
    relay_status.add_argument(
        "--pings",
        type=int,
        default=None,
        metavar="N",
        help="round-trip measurements to take (1-20, default 4)",
    )
    relay_status.add_argument(
        "--timeout",
        type=float,
        default=None,
        metavar="SECONDS",
        help="connect deadline (default from configuration)",
    )

    subparsers.add_parser("session", help="show the session dashboard (history, rooms, invites)")
    subparsers.add_parser("doctor", help="run environment diagnostics and exit")
    return parser


def parse_args(argv: Sequence[str] | None = None) -> CLIOptions:
    """Parse ``argv`` (default: ``sys.argv[1:]``) into :class:`CLIOptions`."""

    parser = build_parser()
    namespace = parser.parse_args(argv)
    config_path = namespace.config.expanduser() if namespace.config else None
    data_dir = namespace.data_dir.expanduser() if namespace.data_dir else None

    if config_path is not None and not config_path.is_file():
        parser.error(f"the configuration file '{config_path}' does not exist")
    if getattr(namespace, "lifetime", None) is not None and namespace.lifetime < 0:
        parser.error("--lifetime must be ≥ 0 minutes")
    if getattr(namespace, "invite_lifetime", None) is not None and namespace.invite_lifetime < 1:
        parser.error("--invite-lifetime must be ≥ 1 minute")
    if getattr(namespace, "timeout", None) is not None and namespace.timeout <= 0:
        parser.error("--timeout must be a positive number of seconds")

    return CLIOptions(
        config_path=config_path,
        data_dir=data_dir,
        theme=namespace.theme,
        debug=namespace.debug,
        no_color=namespace.no_color,
        doctor=namespace.doctor,
        command=namespace.command,
        name=getattr(namespace, "name", None),
        room_id=getattr(namespace, "room_id", None),
        lifetime=getattr(namespace, "lifetime", None),
        invite_lifetime=getattr(namespace, "invite_lifetime", None),
        multi_use=getattr(namespace, "multi_use", False),
        relay_url=getattr(namespace, "relay_url", None),
        pings=getattr(namespace, "pings", None),
        timeout=getattr(namespace, "timeout", None),
        chat_name=getattr(namespace, "chat_name", None),
    )
