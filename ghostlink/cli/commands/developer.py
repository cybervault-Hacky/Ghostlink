"""``ghostlink developer`` — manage the local developer account & credentials.

This is a local-only feature (Phase 10A). It never makes a network request,
never uploads anything, and never displays a developer key more than once
(at creation/rotation). After that, all output is redacted metadata.

The raw key is shown exactly once with an explicit "save it now" warning.
"""

from __future__ import annotations

from pathlib import Path

from rich import box
from rich.console import RenderableType
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ghostlink.cli.arguments import CLIOptions
from ghostlink.cli.commands.base import CommandRuntime, build_runtime
from ghostlink.developer import DeveloperManager
from ghostlink.developer.errors import DeveloperError
from ghostlink.developer.storage import DEV_DIR_NAME
from ghostlink.developer.validation import ensure_writable_account
from ghostlink.exceptions.base import ExitCode

_TITLE: str = "[gl.title]GHOSTLINK DEVELOPER ACCOUNT[/]"


def _panel(runtime: CommandRuntime, body: RenderableType, *, subtitle: str) -> Panel:
    return Panel(
        body,
        title=_TITLE,
        subtitle=f"[gl.muted]{subtitle}[/]",
        box=box.DOUBLE,
        border_style="gl.accent",
        padding=(0, 2),
        width=min(68, max(40, runtime.console.width)),
        expand=False,
    )


def _redact(credential: str) -> str:
    """Redact a credential to a metadata-only preview (never the secret)."""
    prefix = credential[: len("gl_dev_dk_") + 2]
    return f"{prefix}••••••••••••••••"


def _developer_dir(runtime: CommandRuntime) -> Path:
    return runtime.config.data_dir / DEV_DIR_NAME


def _manager(runtime: CommandRuntime) -> DeveloperManager:
    return DeveloperManager(_developer_dir(runtime))


def run_developer(options: CLIOptions) -> int:
    """Dispatch a developer-account action and return the exit code."""
    action = options.developer_action or "status"
    if action == "init":
        return _init(options)
    if action == "status":
        return _status(options)
    if action == "export-info":
        return _export_info(options)
    if action == "key":
        key_action = options.developer_key_action or "list"
        if key_action == "create":
            return _key_create(options)
        if key_action == "list":
            return _key_list(options)
        if key_action == "rotate":
            return _key_rotate(options)
        if key_action == "revoke":
            return _key_revoke(options)
        raise DeveloperError(
            f"Unknown developer key action '{key_action}'.",
            hint="Valid: create, list, rotate, revoke.",
        )
    raise DeveloperError(
        f"Unknown developer action '{action}'.",
        hint="Valid: init, status, key, export-info.",
    )


# -------------------------------------------------------------------- init


def _init(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    manager = _manager(runtime)
    ensure_writable_account(_developer_dir(runtime))
    try:
        account, credential = manager.init_account()
    except DeveloperError as exc:
        console.print(f"✗ {exc.message}")
        if exc.hint:
            console.print(f"  {exc.hint}")
        return int(ExitCode.CONFIGURATION)
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="gl.muted", no_wrap=True)
    grid.add_column(style="gl.text")
    grid.add_row("Developer", Text(account.developer_id, style="gl.accent"))
    grid.add_row("Status", "ACTIVE")
    grid.add_row("Created", account.created_at.date().isoformat())
    console.print(_panel(runtime, grid, subtitle="local developer account"))
    console.print(
        Panel(
            Text.assemble(
                ("Save this key now. It will not be shown again.\n\n", "gl.warning"),
                (credential, "gl.highlight"),
                (
                    "\n\nUse it to authenticate to the future GhostLink developer "
                    "portal (Phase 10B). It is stored only as salted verification "
                    "material — never in plaintext.",
                    "gl.muted",
                ),
            ),
            title="[gl.title]Developer API key — show once[/]",
            box=box.ROUNDED,
            border_style="gl.warning",
            padding=(1, 2),
        )
    )
    console.print(
        Text(
            "The key is never uploaded or sent anywhere by GhostLink. Keep it "
            "private; anyone with it can act as this developer account.",
            style="gl.muted",
        )
    )
    return int(ExitCode.OK)


# ------------------------------------------------------------------ status


def _status(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    manager = _manager(runtime)
    account = manager.status()
    if account is None:
        console.print(
            _panel(
                runtime,
                Text("No developer account configured.", style="gl.muted"),
                subtitle="run: ghostlink developer init",
            )
        )
        return int(ExitCode.OK)
    active = [c for c in account.credentials.values() if c.is_active]
    grid = Table.grid(padding=(0, 2))
    grid.add_column(style="gl.muted", no_wrap=True)
    grid.add_column(style="gl.text")
    grid.add_row("Status", account.status.upper())
    grid.add_row("Developer", Text(account.developer_id, style="gl.accent"))
    grid.add_row("Created", account.created_at.date().isoformat())
    grid.add_row("Active credentials", str(len(active)))
    grid.add_row("Total credentials", str(len(account.credentials)))
    console.print(_panel(runtime, grid, subtitle="local developer account — no secrets shown"))
    return int(ExitCode.OK)


# ---------------------------------------------------------------- key list


def _key_list(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    manager = _manager(runtime)
    try:
        credentials = manager.list_credentials()
    except DeveloperError as exc:
        console.print(f"✗ {exc.message}")
        if exc.hint:
            console.print(f"  {exc.hint}")
        return int(ExitCode.CONFIGURATION)
    table = Table(title="Developer credentials", header_style="gl.accent", expand=False)
    table.add_column("Key id", style="gl.text", no_wrap=True)
    table.add_column("Status", style="gl.text", no_wrap=True)
    table.add_column("Created", style="gl.text", no_wrap=True)
    table.add_column("Last used", style="gl.text", no_wrap=True)
    for cred in credentials:
        last = cred.last_used_at.date().isoformat() if cred.last_used_at else "—"
        table.add_row(cred.key_id, cred.status, cred.created_at.date().isoformat(), last)
    console.print(table)
    return int(ExitCode.OK)


# -------------------------------------------------------------- key create


def _key_create(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    manager = _manager(runtime)
    try:
        _account, credential = manager.create_credential()
    except DeveloperError as exc:
        console.print(f"✗ {exc.message}")
        if exc.hint:
            console.print(f"  {exc.hint}")
        return int(ExitCode.CONFIGURATION)
    console.print(
        Panel(
            Text.assemble(
                ("Save this key now. It will not be shown again.\n\n", "gl.warning"),
                (credential, "gl.highlight"),
            ),
            title="[gl.title]New developer API key — show once[/]",
            box=box.ROUNDED,
            border_style="gl.warning",
            padding=(1, 2),
        )
    )
    return int(ExitCode.OK)


# --------------------------------------------------------------- key rotate


def _key_rotate(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    manager = _manager(runtime)
    try:
        _account, credential = manager.rotate_credential()
    except DeveloperError as exc:
        console.print(f"✗ {exc.message}")
        if exc.hint:
            console.print(f"  {exc.hint}")
        return int(ExitCode.CONFIGURATION)
    console.print(
        Panel(
            Text.assemble(
                (
                    "Old credential(s) revoked. Save this new key now — it will "
                    "not be shown again.\n\n",
                    "gl.warning",
                ),
                (credential, "gl.highlight"),
            ),
            title="[gl.title]Rotated developer API key — show once[/]",
            box=box.ROUNDED,
            border_style="gl.warning",
            padding=(1, 2),
        )
    )
    return int(ExitCode.OK)


# --------------------------------------------------------------- key revoke


def _key_revoke(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    manager = _manager(runtime)
    key_id = (options.developer_key_id or "").strip()
    if not key_id:
        console.print("✗ Revoke requires a key id (dk_…).")
        console.print("  Use 'ghostlink developer key list' to see key ids.")
        return int(ExitCode.CONFIGURATION)
    try:
        manager.revoke_credential(key_id)
    except DeveloperError as exc:
        console.print(f"✗ {exc.message}")
        if exc.hint:
            console.print(f"  {exc.hint}")
        return int(ExitCode.CONFIGURATION)
    console.print(f"✓ Credential {key_id} revoked.")
    return int(ExitCode.OK)


# ------------------------------------------------------------- export-info


def _export_info(options: CLIOptions) -> int:
    """Print the public account metadata as JSON (never the secret).

    This is the future-compatible surface for a Phase 10B registration
    flow: it emits only public identifiers and status — no secret, no
    verification hash. Registration still requires the user to explicitly
    share the credential out-of-band; nothing is uploaded here.
    """
    import json

    runtime = build_runtime(options)
    console = runtime.console
    manager = _manager(runtime)
    account = manager.status()
    if account is None:
        console.print("✗ No developer account configured.")
        console.print("  Run 'ghostlink developer init' first.")
        return int(ExitCode.CONFIGURATION)
    credentials = manager.list_credentials()
    document = {
        "developer_id": account.developer_id,
        "status": account.status,
        "created_at": account.created_at.isoformat(),
        "credentials": [
            {
                "key_id": c.key_id,
                "status": c.status,
                "created_at": c.created_at.isoformat(),
                "last_used_at": c.last_used_at.isoformat() if c.last_used_at else None,
            }
            for c in credentials
        ],
    }
    console.print(json.dumps(document, indent=2, sort_keys=True))
    console.print(
        Text(
            "This metadata is public and contains no secret. The raw gl_dev_… "
            "key is never exported by GhostLink.",
            style="gl.muted",
        )
    )
    return int(ExitCode.OK)


__all__ = ["run_developer"]
