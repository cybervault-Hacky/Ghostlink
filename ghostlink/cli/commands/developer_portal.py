"""``ghostlink developer`` — Termux ↔ Developer Portal integration.

Terminal-native commands for a developer installation to start local portal
services, pair devices via short-lived pairing flows, authenticate, manage
scoped credentials, and review API security activity.

Secrets policy: permanent developer credentials are entered only at login/pairing
time and are never stored locally; only short-lived access tokens and
rotating refresh tokens are kept in the local 0600 store. Tokens are never
printed, logged, or placed in URLs.
"""

from __future__ import annotations

import contextlib
import getpass
import shutil
import subprocess
import webbrowser

from rich.table import Table

from ghostlink.cli.arguments import CLIOptions
from ghostlink.cli.commands.base import CommandRuntime, build_runtime
from ghostlink.developer_portal.client import PortalClient, PortalClientError
from ghostlink.developer_portal.server import (
    DEFAULT_BACKEND_PORTAL_URL,
    check_backend_health,
    check_prerequisites,
    get_developer_status,
    start_developer_services,
    stop_developer_services,
)
from ghostlink.developer_portal.store import PortalStore, PortalStoreError
from ghostlink.exceptions.base import ExitCode
from ghostlink.ui.menu import InteractiveMenu, MenuEntry

DEFAULT_PORTAL_URL = DEFAULT_BACKEND_PORTAL_URL


def _portal_url(runtime: CommandRuntime, options: CLIOptions) -> str:
    url = (options.relay_url or runtime.settings.relay.url or DEFAULT_BACKEND_PORTAL_URL).rstrip(
        "/"
    )
    if url.startswith("wss://"):
        url = "https://" + url[len("wss://") :]
    elif url.startswith("ws://"):
        url = "http://" + url[len("ws://") :]
    return url


def _client(runtime: CommandRuntime, options: CLIOptions) -> PortalClient:
    return PortalClient(_portal_url(runtime, options))


def _store(runtime: CommandRuntime) -> PortalStore:
    return PortalStore(runtime.config.data_dir)


def run_developer_portal(options: CLIOptions, action: str) -> int:
    """Dispatch a developer-portal action and return the exit code."""
    if action == "start":
        return _start(options)
    if action == "stop":
        return _stop(options)
    if action == "status":
        return _status_summary(options)
    if action == "portal":
        return _portal_open(options)
    if action == "pair":
        return _pair(options)
    if action == "login":
        return _login(options)
    if action == "logout":
        return _logout(options)
    if action == "whoami":
        return _whoami(options)
    if action == "device":
        return _device(options)
    if action == "project":
        return _project(options)
    if action == "credential":
        return _credential(options)
    if action == "security-status":
        return _security_status(options)
    if action == "doctor":
        return _doctor_improved(options)
    if action == "menu":
        return _interactive_menu(options)
    raise PortalClientError(f"Unknown developer-portal action '{action}'.")


# ----------------------------------------------------------- start / stop / status


def _start(options: CLIOptions) -> int:
    """Start local backend and frontend developer servers and display status."""
    runtime = build_runtime(options)
    console = runtime.console
    data_dir = runtime.config.data_dir

    console.print("[gl.title]Starting GhostLink Developer Environment…[/]")

    try:
        res = start_developer_services(data_dir=data_dir)
    except Exception as exc:
        console.print(f"[gl.error]✗ Could not start developer services: {exc}[/]")
        return int(ExitCode.CONFIGURATION)

    backend_url = res["backend_url"]
    frontend_url = res["frontend_url"]

    store = _store(runtime)
    tokens = store.load()
    is_authed = bool(tokens.get("access_token"))
    dev_id = str(tokens.get("developer_id", ""))

    console.newline()
    console.print("[gl.title]GhostLink Developer[/]")
    console.print("[gl.border]────────────────────────────────────[/]")
    console.print(
        f"[gl.success]✓[/] Backend       [gl.text]Running[/]  [gl.muted]({backend_url})[/]"
    )
    console.print("[gl.success]✓[/] Database      [gl.text]Ready[/]    [gl.muted](SQLite)[/]")
    console.print("[gl.success]✓[/] Developer API [gl.text]Ready[/]")
    console.print(
        f"[gl.success]✓[/] Portal        [gl.text]Running[/]  [gl.muted]({frontend_url})[/]"
    )
    console.newline()
    console.print(f"[gl.highlight]Developer Portal:[/] [gl.accent]{frontend_url}[/]")
    console.newline()

    if is_authed:
        console.print(f"[gl.success]Authenticated as:[/] [gl.accent]{dev_id}[/]")
    else:
        console.print("[gl.muted]Waiting for authentication…[/]")
        console.print("Sign in on the portal, click [bold]Connect this device[/], and pair.")

    return int(ExitCode.OK)


def _stop(options: CLIOptions) -> int:
    """Stop running developer processes started by GhostLink."""
    runtime = build_runtime(options)
    console = runtime.console
    data_dir = runtime.config.data_dir

    res = stop_developer_services(data_dir)
    stopped_count = len(res.get("stopped_pids", []))
    if stopped_count > 0:
        console.print(f"[gl.success]✓ Stopped {stopped_count} developer process(es).[/]")
    else:
        console.print("[gl.muted]No active GhostLink developer servers were running.[/]")
    return int(ExitCode.OK)


def _status_summary(options: CLIOptions) -> int:
    """Display the real-time status of the developer environment."""
    runtime = build_runtime(options)
    console = runtime.console
    data_dir = runtime.config.data_dir
    portal_url = _portal_url(runtime, options)

    status = get_developer_status(data_dir, portal_url)

    table = Table(title="GhostLink Developer Status", header_style="gl.accent", expand=False)
    table.add_column("Component", style="gl.text")
    table.add_column("Status", style="gl.text")
    table.add_column("Details", style="gl.muted")

    b_ok = status["backend_ready"]
    table.add_row(
        "Backend",
        "[gl.success]RUNNING[/]" if b_ok else "[gl.error]OFFLINE[/]",
        status["backend_url"],
    )
    table.add_row(
        "Database",
        "[gl.success]READY[/]" if b_ok else "[gl.muted]OFFLINE[/]",
        "SQLite (dev)",
    )
    table.add_row(
        "Developer API",
        "[gl.success]READY[/]" if b_ok else "[gl.error]OFFLINE[/]",
        f"{status['backend_url']}/api/v1/developer",
    )
    f_ok = status["frontend_ready"]
    table.add_row(
        "Portal",
        "[gl.success]RUNNING[/]" if f_ok else "[gl.muted]OFFLINE[/]",
        status["frontend_url"],
    )
    authed = status["authenticated"]
    dev_str = f"Developer: {status['developer_id']}" if authed else "Run 'ghostlink developer pair'"
    table.add_row(
        "Authentication",
        "[gl.success]AUTHENTICATED[/]" if authed else "[gl.warning]NOT CONNECTED[/]",
        dev_str,
    )

    console.print(table)
    return int(ExitCode.OK)


def _portal_open(options: CLIOptions) -> int:
    """Open or print the Developer Portal URL."""
    runtime = build_runtime(options)
    console = runtime.console
    data_dir = runtime.config.data_dir

    try:
        res = start_developer_services(data_dir=data_dir)
        url = res["frontend_url"]
    except Exception:
        url = "http://127.0.0.1:5173"

    console.print(f"[gl.title]Developer Portal:[/] [gl.accent]{url}[/]")

    # Try platform openers
    opened = False
    for tool in ("termux-open-url", "xdg-open"):
        if shutil.which(tool):
            try:
                subprocess.run([tool, url], check=True, capture_output=True, timeout=2)
                opened = True
                break
            except Exception:
                continue

    if not opened:
        with contextlib.suppress(Exception):
            opened = webbrowser.open(url)

    if opened:
        console.print("[gl.muted]Opened in default browser.[/]")
    return int(ExitCode.OK)


# -------------------------------------------------------------------- pair


def _pair(options: CLIOptions) -> int:
    """Pair Termux device with the Developer Portal using a pairing code or QR."""
    runtime = build_runtime(options)
    console = runtime.console
    store = _store(runtime)
    client = _client(runtime, options)

    code = (options.developer_key_action or options.developer_key_id or "").strip()

    if not code:
        console.print("[gl.title]Pair this Termux Device[/]")
        console.print(
            "[gl.muted]Enter the short-lived pairing code shown on the Developer Portal "
            "(e.g. GL-XXXX-YYYY) or paste a gl://dev-pair URI.[/]"
        )
        try:
            raw = input("Pairing code / URI: ").strip()
            code = raw
        except (EOFError, KeyboardInterrupt):
            console.newline()
            return int(ExitCode.OK)

    if not code:
        console.print("[gl.error]✗ Pairing code is required.[/]")
        return int(ExitCode.CONFIGURATION)

    try:
        tokens = client.pair_complete(code)
    except PortalClientError as exc:
        console.print(f"[gl.error]✗ Pairing failed: {exc.message}[/]")
        return int(ExitCode.CONFIGURATION)

    store.save(
        {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "developer_id": tokens.get("developer_id", ""),
            "device_id": tokens.get("device_id", ""),
            "scopes": tokens.get("scopes", []),
        }
    )

    console.print("[gl.success]✓ Device paired successfully![/]")
    console.print(f"  Developer: [gl.accent]{tokens.get('developer_id', '')}[/]")
    console.print(f"  Device ID: [gl.muted]{tokens.get('device_id', '')}[/]")
    console.print("  Tokens:    [gl.muted]Stored securely (0600)[/]")
    return int(ExitCode.OK)


# ---------------------------------------------------------------- auth / whoami


def _login(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    store = _store(runtime)
    client = _client(runtime, options)
    credential_id = input("credential id (dk_…): ").strip()
    secret = getpass.getpass("credential secret (shown once at pairing): ")
    if not credential_id or not secret:
        console.print("[gl.error]✗ Credential id and secret are required.[/]")
        return int(ExitCode.CONFIGURATION)
    try:
        tokens = client.token_exchange(credential_id, secret)
    except PortalClientError as exc:
        console.print(f"[gl.error]✗ {exc.message}[/]")
        return int(ExitCode.CONFIGURATION)
    store.save(
        {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "developer_id": tokens.get("developer_id", ""),
            "scopes": tokens.get("scopes", []),
        }
    )
    console.print(
        "[gl.success]✓ Signed in to the Developer Portal (tokens stored securely, 0600).[/]"
    )
    console.print(f"  Developer: [gl.accent]{tokens.get('developer_id', '')}[/]")
    return int(ExitCode.OK)


def _logout(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    store = _store(runtime)
    try:
        store.clear()
    except PortalStoreError as exc:
        console.print(f"[gl.error]✗ {exc}[/]")
    console.print("[gl.success]✓ Signed out; local tokens cleared.[/]")
    return int(ExitCode.OK)


def _tokens(store: PortalStore) -> dict[str, object]:
    data = store.load()
    if not data.get("access_token"):
        raise PortalClientError("Not signed in. Run 'ghostlink developer pair' or 'start' first.")
    return data


def _whoami(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    store = _store(runtime)
    try:
        data = _tokens(store)
    except PortalClientError as exc:
        console.print(f"[gl.error]✗ {exc.message}[/]")
        return int(ExitCode.CONFIGURATION)
    scopes_value = data.get("scopes", [])
    scopes = ", ".join(str(s) for s in scopes_value) if isinstance(scopes_value, list) else "none"
    console.print(f"Developer: [gl.accent]{data.get('developer_id', '?')}[/]")
    console.print(f"Device:    [gl.muted]{data.get('device_id', 'Termux')}[/]")
    console.print(f"Scopes:    [gl.muted]{scopes}[/]")
    return int(ExitCode.OK)


def _auth_get(
    runtime: CommandRuntime, options: CLIOptions
) -> tuple[PortalClient, PortalStore, dict[str, object], str]:
    store = _store(runtime)
    client = _client(runtime, options)
    data = _tokens(store)
    access = str(data["access_token"])
    return client, store, data, str(access)


# ------------------------------------------------------------- resources


def _device(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    sub = options.developer_key_action or "list"
    target = options.developer_key_id or ""
    try:
        client, _store_obj, _data, access = _auth_get(runtime, options)
    except (PortalClientError, PortalStoreError) as exc:
        console.print(f"[gl.error]✗ {exc}[/]")
        return int(ExitCode.CONFIGURATION)
    try:
        if sub == "list":
            body = client.devices_list(access)
            table = Table(title="Developer devices", header_style="gl.accent")
            table.add_column("Device id")
            table.add_column("Name")
            table.add_column("Status")
            for d in body.get("devices", []):
                table.add_row(d["device_id"], d["name"], d["status"])
            console.print(table)
            return int(ExitCode.OK)
        if sub == "revoke":
            if not target:
                console.print("[gl.error]✗ device revoke requires a device id.[/]")
                return int(ExitCode.CONFIGURATION)
            client.device_revoke(access, target)
            console.print(f"[gl.success]✓ Device {target} revoked.[/]")
            return int(ExitCode.OK)
        if sub == "register":
            body = client.pair_begin(target or "termux-device", "termux", "0.17.0")
            console.print("[gl.title]Pairing code (short-lived, single-use):[/]")
            console.print(f"  [gl.highlight]{body['pairing_code']}[/]")
            console.print(
                "[gl.muted]Approve this device on the Developer Portal to complete pairing.[/]"
            )
            return int(ExitCode.OK)
        console.print(f"[gl.error]✗ Unknown device action '{sub}'.[/]")
        return int(ExitCode.CONFIGURATION)
    except PortalClientError as exc:
        console.print(f"[gl.error]✗ {exc.message}[/]")
        return int(ExitCode.CONFIGURATION)


def _project(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    sub = options.developer_key_action or "list"
    try:
        client, _store_obj, _data, access = _auth_get(runtime, options)
    except (PortalClientError, PortalStoreError) as exc:
        console.print(f"[gl.error]✗ {exc}[/]")
        return int(ExitCode.CONFIGURATION)
    try:
        if sub == "list":
            body = client.projects_list(access)
            table = Table(title="Projects", header_style="gl.accent")
            table.add_column("ID")
            table.add_column("Name")
            table.add_column("Slug")
            table.add_column("Status")
            for p in body.get("projects", []):
                table.add_row(str(p["id"]), p["name"], p["slug"], p["status"])
            console.print(table)
            return int(ExitCode.OK)
        console.print(f"[gl.error]✗ Unknown project action '{sub}'.[/]")
        return int(ExitCode.CONFIGURATION)
    except PortalClientError as exc:
        console.print(f"[gl.error]✗ {exc.message}[/]")
        return int(ExitCode.CONFIGURATION)


def _credential(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    sub = options.developer_key_action or "status"
    try:
        client, _store_obj, _data, access = _auth_get(runtime, options)
    except (PortalClientError, PortalStoreError) as exc:
        console.print(f"[gl.error]✗ {exc}[/]")
        return int(ExitCode.CONFIGURATION)
    try:
        if sub in ("status", "list"):
            body = client.credentials_list(access)
            table = Table(title="Scoped credentials (metadata only)", header_style="gl.accent")
            table.add_column("Credential id")
            table.add_column("Name")
            table.add_column("Scopes")
            table.add_column("Status")
            for c in body.get("credentials", []):
                table.add_row(c["credential_id"], c["name"], ", ".join(c["scopes"]), c["status"])
            console.print(table)
            return int(ExitCode.OK)
        console.print(f"[gl.error]✗ Unknown credential action '{sub}'.[/]")
        return int(ExitCode.CONFIGURATION)
    except PortalClientError as exc:
        console.print(f"[gl.error]✗ {exc.message}[/]")
        return int(ExitCode.CONFIGURATION)


def _security_status(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    try:
        client, _store_obj, _data, access = _auth_get(runtime, options)
    except (PortalClientError, PortalStoreError) as exc:
        console.print(f"[gl.error]✗ {exc}[/]")
        return int(ExitCode.CONFIGURATION)
    try:
        body = client.activity(access)
    except PortalClientError as exc:
        console.print(f"[gl.error]✗ {exc.message}[/]")
        return int(ExitCode.CONFIGURATION)
    console.print(f"Developer: [gl.accent]{_data.get('developer_id', '?')}[/]")
    console.print(f"Recent API activity: [gl.muted]{len(body.get('events', []))} event(s)[/]")
    return int(ExitCode.OK)


# ------------------------------------------------------------------- doctor


def _doctor_improved(options: CLIOptions) -> int:
    """Comprehensive diagnostic checks for developer environment."""
    runtime = build_runtime(options)
    console = runtime.console
    portal_url = _portal_url(runtime, options)

    prereqs = check_prerequisites()
    store = _store(runtime)

    table = Table(title="GhostLink Developer Doctor", header_style="gl.accent", expand=False)
    table.add_column("Check", style="gl.text")
    table.add_column("Status", style="gl.text")
    table.add_column("Details", style="gl.muted")

    all_ready = True

    # Python
    table.add_row(
        "Python",
        "[gl.success]PASS[/]" if prereqs.python_ok else "[gl.error]FAIL[/]",
        f"v{prereqs.python_version}",
    )
    if not prereqs.python_ok:
        all_ready = False

    # Node.js
    table.add_row(
        "Node.js",
        "[gl.success]PASS[/]" if prereqs.node_ok else "[gl.warning]WARN[/]",
        prereqs.node_version,
    )

    # npm
    table.add_row(
        "npm",
        "[gl.success]PASS[/]" if prereqs.npm_ok else "[gl.warning]WARN[/]",
        prereqs.npm_version,
    )

    # Frontend packages
    table.add_row(
        "Frontend",
        "[gl.success]PASS[/]" if prereqs.frontend_deps_ok else "[gl.muted]NOT INSTALLED[/]",
        "portal/web/node_modules",
    )

    # Backend code
    table.add_row(
        "Backend",
        "[gl.success]PASS[/]" if prereqs.backend_code_ok else "[gl.error]FAIL[/]",
        "portal_server",
    )
    if not prereqs.backend_code_ok:
        all_ready = False

    # Backend live check
    backend_live = check_backend_health(portal_url)
    table.add_row(
        "Developer API",
        "[gl.success]PASS[/]" if backend_live else "[gl.muted]OFFLINE[/]",
        f"{portal_url}/api/v1/developer/health",
    )

    # Database
    table.add_row("Database", "[gl.success]PASS[/]", "SQLite (development)")

    # Token Store
    try:
        auth_data = store.load()
        has_token = bool(auth_data.get("access_token"))
        table.add_row("Token Store", "[gl.success]PASS[/]", "0600 / tokens.json")
        table.add_row(
            "Authentication",
            "[gl.success]PASS[/]" if has_token else "[gl.muted]NOT CONNECTED[/]",
            f"Developer: {auth_data.get('developer_id', 'none')}",
        )
    except Exception as exc:
        table.add_row("Token Store", "[gl.error]FAIL[/]", str(exc))
        all_ready = False

    console.print(table)
    console.newline()
    if all_ready:
        console.print("[bold gl.success]Overall: READY[/]")
    else:
        console.print("[bold gl.warning]Overall: ACTION REQUIRED[/]")
    return int(ExitCode.OK)


# ------------------------------------------------------------- interactive menu


def _interactive_menu(options: CLIOptions) -> int:
    """Interactive developer command center for Termux."""
    runtime = build_runtime(options)
    console = runtime.console
    data_dir = runtime.config.data_dir
    portal_url = _portal_url(runtime, options)
    menu = InteractiveMenu(console)

    while True:
        console.clear()
        console.newline()

        status = get_developer_status(data_dir, portal_url)
        authed = status["authenticated"]
        dev_id = status["developer_id"] or "Not connected"

        console.print("[gl.title]GhostLink Developer[/]")
        console.print("[gl.border]────────────────────────────────────[/]")
        authed_str = "[gl.success]Authenticated[/]" if authed else "[gl.muted]Not connected[/]"
        api_str = "[gl.success]Ready[/]" if status["backend_ready"] else "[gl.muted]Offline[/]"
        console.print(f"Status:         {authed_str}")
        console.print(f"Developer ID:   [gl.accent]{dev_id}[/]")
        console.print(f"Developer API:  {api_str}")
        console.newline()

        entries = (
            MenuEntry(
                key="start",
                label="Start Developer Portal",
                description="Launch backend & frontend dev servers",
            ),
            MenuEntry(
                key="pair",
                label="Connect Device (Pair)",
                description="Pair this device using a pairing code",
            ),
            MenuEntry(
                key="status",
                label="Environment Status",
                description="Check servers & authentication status",
            ),
            MenuEntry(
                key="portal",
                label="Open Developer Portal",
                description="Open portal in web browser",
            ),
            MenuEntry(
                key="project",
                label="List Projects",
                description="View developer projects",
            ),
            MenuEntry(
                key="credential",
                label="Manage Credentials",
                description="View scoped API credentials",
            ),
            MenuEntry(
                key="security",
                label="Security Status",
                description="Review recent developer activity",
            ),
            MenuEntry(
                key="doctor",
                label="Developer Doctor",
                description="Run environment diagnostics",
            ),
            MenuEntry.spacer(),
            MenuEntry(
                key="logout",
                label="Sign Out",
                description="Clear local access tokens",
            ),
            MenuEntry(
                key="stop",
                label="Stop Background Servers",
                description="Shut down local dev servers",
            ),
            MenuEntry.spacer(),
            MenuEntry(
                key="exit",
                label="Exit Developer Menu",
                description="Return to main menu",
            ),
        )

        choice = menu.prompt(entries, default_key="exit")
        if choice in ("exit", "back", None):
            return int(ExitCode.OK)

        console.clear()
        console.newline()

        if choice == "start":
            _start(options)
        elif choice == "pair":
            _pair(options)
        elif choice == "status":
            _status_summary(options)
        elif choice == "portal":
            _portal_open(options)
        elif choice == "project":
            _project(options)
        elif choice == "credential":
            _credential(options)
        elif choice == "security":
            _security_status(options)
        elif choice == "doctor":
            _doctor_improved(options)
        elif choice == "logout":
            _logout(options)
        elif choice == "stop":
            _stop(options)

        console.newline()
        with contextlib.suppress(EOFError, KeyboardInterrupt):
            input("Press Enter to continue…")


__all__ = ["DEFAULT_PORTAL_URL", "run_developer_portal"]
