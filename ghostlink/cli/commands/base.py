"""Shared runtime constructions for CLI subcommands."""

from __future__ import annotations

from dataclasses import dataclass

from ghostlink.cli.arguments import CLIOptions
from ghostlink.config.manager import ConfigOverrides, ConfigurationManager
from ghostlink.core.environment import EnvironmentDetector
from ghostlink.core.logging import get_logger, setup_logging
from ghostlink.groups.lifecycle import LocalGroupManager
from ghostlink.groups.registry import LocalGroupRegistry
from ghostlink.identity.lifecycle import IdentityManager
from ghostlink.identity.storage import IdentityStore
from ghostlink.invites.lifecycle import SecureInviteManager
from ghostlink.invites.registry import LocalInviteRegistry
from ghostlink.models.environment import ColorSupport, EnvironmentInfo
from ghostlink.models.settings import AppSettings
from ghostlink.services.rooms import RoomService
from ghostlink.storage.manager import StorageManager
from ghostlink.transport.relay.client import RelayClientConfig
from ghostlink.ui.console import ConsoleManager
from ghostlink.ui.themes import ThemeEngine


@dataclass(slots=True)
class CommandRuntime:
    """Everything a subcommand needs, built once per invocation."""

    console: ConsoleManager
    settings: AppSettings
    config: ConfigurationManager
    environment: EnvironmentInfo
    rooms: RoomService
    identities: IdentityManager
    invites: SecureInviteManager
    groups: LocalGroupManager


def build_runtime(options: CLIOptions) -> CommandRuntime:
    """Bootstrap configuration, logging, theme, and services for a command."""

    environment = EnvironmentDetector.detect()
    config = ConfigurationManager(
        config_path=options.config_path,
        overrides=ConfigOverrides(
            theme=options.theme, data_dir=options.data_dir, debug=options.debug
        ),
    )
    settings = config.load()
    config.ensure_directories()
    setup_logging(log_dir=config.logs_dir, debug=settings.diagnostics.debug)

    theme = ThemeEngine().get(settings.ui.theme)
    console = ConsoleManager(
        theme,
        no_color=options.no_color or environment.color_support is ColorSupport.NONE,
    )
    storage = StorageManager(config.state_dir)
    rooms = RoomService(storage, settings)
    identities = IdentityManager(IdentityStore(storage))
    invites = SecureInviteManager(LocalInviteRegistry(storage))
    groups = LocalGroupManager(LocalGroupRegistry(storage), identities)
    get_logger("cli.commands").debug("command runtime built")
    return CommandRuntime(
        console=console,
        settings=settings,
        config=config,
        environment=environment,
        rooms=rooms,
        identities=identities,
        invites=invites,
        groups=groups,
    )


def relay_config_from(settings: AppSettings, options: CLIOptions) -> RelayClientConfig:
    """Project settings + CLI overrides onto the relay client config."""

    relay = settings.relay
    return RelayClientConfig(
        connect_timeout_seconds=(
            options.timeout if options.timeout is not None else relay.connect_timeout_seconds
        ),
        handshake_timeout_seconds=relay.handshake_timeout_seconds,
        heartbeat_interval_seconds=relay.heartbeat_interval_seconds,
        heartbeat_timeout_seconds=relay.heartbeat_timeout_seconds,
        reconnect_attempts=relay.reconnect_attempts,
        reconnect_base_delay_seconds=relay.reconnect_base_delay_seconds,
    )


def resolve_relay_url(settings: AppSettings, options: CLIOptions) -> str | None:
    """The relay URL for a command: explicit flag, then configured value."""

    if options.relay_url:
        return options.relay_url
    configured = settings.relay.url.strip()
    return configured or None
