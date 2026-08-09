"""``ghostlink security-status`` — safe offline security & recovery summary.

Renders metadata only — crypto suites, identity fingerprint, connection
config, recovery posture, and a local group summary (epoch, membership,
suite). It never connects to a relay, never writes state, and never
displays keys, tokens, plaintext, or any secret material.
"""

from __future__ import annotations

from ghostlink.cli.arguments import CLIOptions
from ghostlink.cli.commands.base import CommandRuntime, build_runtime, resolve_relay_url
from ghostlink.constants.app import APP_VERSION
from ghostlink.constants.net import DEFAULT_CRYPTO_SUITE, GROUP_CRYPTO_SUITES
from ghostlink.developer import DeveloperManager
from ghostlink.exceptions.base import ExitCode
from ghostlink.groups.models import LocalGroupState
from ghostlink.identity.fingerprint import identity_fingerprint
from ghostlink.ui.components.panels import section_panel
from ghostlink.ui.components.tables import info_table


def _append_developer_rows(runtime: CommandRuntime, rows: list[tuple[str, str]]) -> None:
    """Append developer-account metadata rows (public info only, no secret)."""
    from ghostlink.developer.storage import DEV_DIR_NAME

    developer_dir = runtime.config.data_dir / DEV_DIR_NAME
    manager = DeveloperManager(developer_dir)
    account = manager.status()
    if account is None:
        rows.append(("Developer account", "not configured"))
        return
    active = [c for c in account.credentials.values() if c.is_active]
    revoked = [c for c in account.credentials.values() if not c.is_active]
    rows.append(("Developer account", account.status))
    rows.append(("Developer id", account.developer_id))
    rows.append(("Developer credentials", f"{len(active)} active / {len(revoked)} revoked"))
    for cred in account.credentials.values():
        last = cred.last_used_at.date().isoformat() if cred.last_used_at else "never"
        created = cred.created_at.date().isoformat()
        rows.append(
            (
                "  · credential",
                f"{cred.key_id} — {cred.status}, created {created}, last used {last}",
            )
        )


def run_security_status(options: CLIOptions) -> int:
    """Render the read-only security & recovery summary."""

    runtime = build_runtime(options)
    console = runtime.console

    identity = runtime.identities.ensure()
    fp = identity_fingerprint(bytes.fromhex(identity.public_key_hex))

    relay_url = resolve_relay_url(runtime.settings, options)
    relay_row = relay_url or "(not configured)"

    rows: list[tuple[str, str]] = [
        ("Version", APP_VERSION),
        ("Identity fingerprint", fp),
        ("Relay", relay_row),
        ("Default crypto suite", DEFAULT_CRYPTO_SUITE),
        ("Available suites", ", ".join(sorted(GROUP_CRYPTO_SUITES))),
        ("Reconnect attempts", str(runtime.settings.relay.reconnect_attempts)),
    ]

    # Local group summary (metadata only; never any key material).
    records = runtime.groups.list_all()
    active = [r for r in records if r.state is LocalGroupState.ACTIVE]
    rows.append(("Groups", f"{len(active)} active / {len(records)} total"))
    for record in active[:8]:  # bounded display
        rows.append(
            (
                "  · group",
                f"{record.name} — epoch {record.epoch}, {record.member_count()}/8 members, "
                f"suite {record.crypto_suite}",
            )
        )

    # Developer account metadata (never any secret).
    _append_developer_rows(runtime, rows)

    console.newline()
    console.print(
        section_panel(
            "Security Status",
            info_table(rows),
            subtitle="read-only summary — no secrets, keys, or tokens shown",
        )
    )
    console.newline()
    return int(ExitCode.OK)


__all__ = ["run_security_status"]
