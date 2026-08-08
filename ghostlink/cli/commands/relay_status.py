"""``ghostlink relay-status`` — live relay probe with a full dashboard."""

from __future__ import annotations

import asyncio

from ghostlink.cli.arguments import CLIOptions
from ghostlink.cli.commands.base import (
    build_runtime,
    relay_config_from,
    resolve_relay_url,
)
from ghostlink.constants.app import APP_NAME, APP_VERSION
from ghostlink.core.logging import get_logger
from ghostlink.exceptions.base import ExitCode
from ghostlink.exceptions.transport import RelayError
from ghostlink.transport.relay.client import probe_relay
from ghostlink.ui.components.progress import StepProgress
from ghostlink.ui.dashboards import relay_unconfigured_panel, render_relay_dashboard

MAX_PINGS = 20


def run_relay_status(options: CLIOptions) -> int:
    """Probe the relay: connect, handshake, measure RTT, report, disconnect."""

    runtime = build_runtime(options)
    logger = get_logger("cli.relay-status")
    console = runtime.console

    url = resolve_relay_url(runtime.settings, options)
    if url is None:
        relay_unconfigured_panel(console)
        console.newline()
        return int(ExitCode.OK)

    pings = options.pings if options.pings is not None else 4
    if not (1 <= pings <= MAX_PINGS):
        raise RelayError(
            f"--pings must be between 1 and {MAX_PINGS}, got {pings}.",
            hint="Choose a small sample count for interactive diagnostics.",
        )

    with StepProgress(console, title=f"Probing {url}…") as progress:
        with progress.step("Connecting and handshaking"):
            pass
        with progress.step(f"Measuring {pings} round trip(s)"):
            report = asyncio.run(
                probe_relay(
                    url,
                    client_name=f"{APP_NAME}/{APP_VERSION}",
                    config=relay_config_from(runtime.settings, options),
                    pings=pings,
                )
            )
        with progress.step("Closing gracefully"):
            pass

    logger.info(
        "relay probe complete — %s rtt_samples=%s",
        url,
        [f"{sample:.1f}" for sample in report.rtt_samples_ms],
    )
    render_relay_dashboard(console, report)
    console.newline()
    return int(ExitCode.OK)
