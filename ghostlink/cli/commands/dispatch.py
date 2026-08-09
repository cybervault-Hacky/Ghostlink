"""Command dispatch: route parsed CLIOptions to subcommand implementations."""

from __future__ import annotations

from ghostlink.cli.arguments import CLIOptions
from ghostlink.cli.commands.admin import run_admin
from ghostlink.cli.commands.developer import run_developer
from ghostlink.cli.commands.groups import run_group
from ghostlink.cli.commands.host import run_host
from ghostlink.cli.commands.identity import run_identity
from ghostlink.cli.commands.invites import run_invite
from ghostlink.cli.commands.join import run_join
from ghostlink.cli.commands.relay_status import run_relay_status
from ghostlink.cli.commands.security_status import run_security_status
from ghostlink.cli.commands.session import run_session
from ghostlink.cli.doctor import run_doctor


def run_command(options: CLIOptions) -> int:
    """Execute the subcommand named in ``options.command``."""

    if options.command == "host":
        return run_host(options)
    if options.command == "join":
        return run_join(options)
    if options.command == "identity":
        return run_identity(options)
    if options.command == "invite":
        return run_invite(options)
    if options.command == "group":
        return run_group(options)
    if options.command == "relay-status":
        return run_relay_status(options)
    if options.command == "session":
        return run_session(options)
    if options.command == "doctor":
        return run_doctor(options)
    if options.command == "security-status":
        return run_security_status(options)
    if options.command == "developer":
        return run_developer(options)
    if options.command in ("db", "backup", "system", "security-audit"):
        return run_admin(options)
    raise ValueError(f"Unknown command {options.command!r} passed argument parsing.")
