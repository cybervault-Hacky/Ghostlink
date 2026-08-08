"""Application bootstrap.

``build_application`` wires the whole runtime together in dependency order:

    environment → configuration → logging → theme → console → storage →
    session service → application

Every constructed service is registered in the :class:`ServiceContainer`, so
Phase 2 features resolve their dependencies from one place instead of
reaching across modules.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ghostlink.config.manager import ConfigOverrides, ConfigurationManager
from ghostlink.constants.app import APP_VERSION, DEVELOPER
from ghostlink.core.application import Application
from ghostlink.core.environment import EnvironmentDetector
from ghostlink.core.logging import get_logger, setup_logging
from ghostlink.models.environment import ColorSupport, EnvironmentInfo
from ghostlink.models.settings import AppSettings
from ghostlink.services.container import ServiceContainer
from ghostlink.services.rooms import RoomService
from ghostlink.services.session import SessionService
from ghostlink.storage.manager import StorageManager
from ghostlink.ui.components.notifications import NotificationCenter, NotificationLevel
from ghostlink.ui.console import ConsoleManager
from ghostlink.ui.screens.base import ScreenContext
from ghostlink.ui.themes import ThemeEngine

if TYPE_CHECKING:
    from ghostlink.cli.arguments import CLIOptions


def build_application(cli_options: CLIOptions) -> Application:
    """Construct a fully-wired :class:`Application` from CLI options."""

    environment = EnvironmentDetector.detect()

    config = ConfigurationManager(
        config_path=cli_options.config_path,
        overrides=ConfigOverrides(
            theme=cli_options.theme,
            data_dir=cli_options.data_dir,
            debug=cli_options.debug,
        ),
    )
    settings = config.load()
    config.ensure_directories()

    logging_report = setup_logging(
        log_dir=config.logs_dir,
        debug=settings.diagnostics.debug,
    )
    logger = get_logger("core.bootstrap")
    logger.info(
        "launch v%s by %s — log file: %s",
        APP_VERSION,
        DEVELOPER,
        logging_report.file_path or "disabled",
    )

    theme = ThemeEngine().get(settings.ui.theme)
    no_color = bool(cli_options.no_color) or environment.color_support is ColorSupport.NONE
    console = ConsoleManager(theme, no_color=no_color)

    notifications = NotificationCenter(console, enabled=settings.notifications.enabled)

    storage = StorageManager(config.state_dir)
    session = SessionService(storage, environment)
    rooms = RoomService(storage, settings)

    container = ServiceContainer()
    container.register(EnvironmentInfo, environment)
    container.register(AppSettings, settings)
    container.register(ConfigurationManager, config)
    container.register(ConsoleManager, console)
    container.register(NotificationCenter, notifications)
    container.register(StorageManager, storage)
    container.register(SessionService, session)
    container.register(RoomService, rooms)

    context = ScreenContext(
        console=console,
        settings=settings,
        environment=environment,
        notifications=notifications,
        config_path=config.config_path,
        data_dir=config.data_dir,
        rooms=rooms,
    )

    if config.created_default:
        context.deferred.append(
            (
                NotificationLevel.SUCCESS,
                f"Configuration initialized at {config.config_path}",
            )
        )
    if not environment.is_supported:
        context.deferred.append(
            (
                NotificationLevel.WARNING,
                f"Platform '{environment.platform_label}' is not officially "
                "supported — GhostLink targets Termux and Linux.",
            )
        )
    if not environment.supported_python:
        context.deferred.append(
            (
                NotificationLevel.WARNING,
                f"Python {environment.python_version} detected — GhostLink "
                "targets Python 3.12+; upgrade for full support.",
            )
        )
    if logging_report.warning:
        context.deferred.append((NotificationLevel.WARNING, logging_report.warning))

    return Application(context, session)
