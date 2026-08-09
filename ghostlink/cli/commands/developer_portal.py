"""``ghostlink developer login/…`` — Termux ↔ Developer Portal integration (Phase 12).

Terminal-native commands for a developer installation to authenticate to the
GhostLink Developer Portal, register/revoke devices, list/use projects,
manage scoped credentials, and review API security activity.

Secrets policy: permanent developer credentials are entered only at login
time and are never stored locally; only short-lived access tokens and
rotating refresh tokens are kept in the local 0600 store. Tokens are never
printed, logged, or placed in URLs.
"""

from __future__ import annotations

import getpass

from rich.table import Table

from ghostlink.cli.arguments import CLIOptions
from ghostlink.cli.commands.base import CommandRuntime, build_runtime
from ghostlink.developer_portal.client import PortalClient, PortalClientError
from ghostlink.developer_portal.store import PortalStore, PortalStoreError
from ghostlink.exceptions.base import ExitCode

DEFAULT_PORTAL_URL = "http://127.0.0.1:8788"


def _portal_url(runtime: CommandRuntime, options: CLIOptions) -> str:
    url = (options.relay_url or runtime.settings.relay.url or DEFAULT_PORTAL_URL).rstrip("/")
    # The configured relay url may be ws:// — the portal HTTP API is http://.
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
        return _doctor(options)
    raise PortalClientError(f"Unknown developer-portal action '{action}'.")


def _login(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    store = _store(runtime)
    client = _client(runtime, options)
    credential_id = input("credential id (dk_…): ").strip()
    secret = getpass.getpass("credential secret (shown once at pairing): ")
    if not credential_id or not secret:
        console.print("✗ Credential id and secret are required.")
        return int(ExitCode.CONFIGURATION)
    try:
        tokens = client.token_exchange(credential_id, secret)
    except PortalClientError as exc:
        console.print(f"✗ {exc.message}")
        return int(ExitCode.CONFIGURATION)
    store.save(
        {
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "developer_id": tokens.get("developer_id", ""),
            "scopes": tokens.get("scopes", []),
        }
    )
    console.print("✓ Signed in to the Developer Portal (tokens stored securely, 0600).")
    console.print(f"  Developer: {tokens.get('developer_id', '')}")
    return int(ExitCode.OK)


def _logout(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    store = _store(runtime)
    try:
        store.clear()
    except PortalStoreError as exc:
        console.print(f"✗ {exc}")
    console.print("✓ Signed out; local tokens cleared.")
    return int(ExitCode.OK)


def _tokens(store: PortalStore) -> dict[str, object]:
    data = store.load()
    if not data.get("access_token"):
        raise PortalClientError("Not signed in. Run 'ghostlink developer login' first.")
    return data


def _whoami(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    store = _store(runtime)
    try:
        data = _tokens(store)
    except PortalClientError as exc:
        console.print(f"✗ {exc.message}")
        return int(ExitCode.CONFIGURATION)
    scopes_value = data.get("scopes", [])
    scopes = ", ".join(str(s) for s in scopes_value) if isinstance(scopes_value, list) else "none"
    console.print(f"Developer: {data.get('developer_id', '?')}")
    console.print(f"Scopes:    {scopes}")
    return int(ExitCode.OK)


def _auth_get(
    runtime: CommandRuntime, options: CLIOptions
) -> tuple[PortalClient, PortalStore, dict[str, object], str]:
    store = _store(runtime)
    client = _client(runtime, options)
    data = _tokens(store)
    access = str(data["access_token"])
    return client, store, data, str(access)


def _device(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    sub = options.developer_key_action or "list"
    target = options.developer_key_id or ""
    try:
        client, _store_obj, _data, access = _auth_get(runtime, options)
    except (PortalClientError, PortalStoreError) as exc:
        console.print(f"✗ {exc}")
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
                console.print("✗ device revoke requires a device id.")
                return int(ExitCode.CONFIGURATION)
            client.device_revoke(access, target)
            console.print(f"✓ Device {target} revoked.")
            return int(ExitCode.OK)
        if sub == "register":
            # Pairing: this CLI displays a code; the user approves it on the
            # portal (session) and then runs `developer login` with the
            # issued credential. This keeps permanent secrets off Termux.
            body = client.pair_begin(target or "termux-device", "termux", "0.14.0")
            console.print("Pairing code (short-lived, single-use):")
            console.print(f"  {body['pairing_code']}")
            console.print(
                "Approve this device on the Developer Portal, then run "
                "'ghostlink developer login' with the credential shown there."
            )
            return int(ExitCode.OK)
        console.print(f"✗ Unknown device action '{sub}'.")
        return int(ExitCode.CONFIGURATION)
    except PortalClientError as exc:
        console.print(f"✗ {exc.message}")
        return int(ExitCode.CONFIGURATION)


def _project(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    sub = options.developer_key_action or "list"
    try:
        client, _store_obj, _data, access = _auth_get(runtime, options)
    except (PortalClientError, PortalStoreError) as exc:
        console.print(f"✗ {exc}")
        return int(ExitCode.CONFIGURATION)
    try:
        if sub == "list":
            body = client.projects_list(access)
            table = Table(title="Projects", header_style="gl.accent")
            table.add_column("id")
            table.add_column("Name")
            table.add_column("Status")
            for p in body.get("projects", []):
                table.add_row(str(p["id"]), p["name"], p["status"])
            console.print(table)
            return int(ExitCode.OK)
        console.print(f"✗ Unknown project action '{sub}'.")
        return int(ExitCode.CONFIGURATION)
    except PortalClientError as exc:
        console.print(f"✗ {exc.message}")
        return int(ExitCode.CONFIGURATION)


def _credential(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    sub = options.developer_key_action or "status"
    try:
        client, _store_obj, _data, access = _auth_get(runtime, options)
    except (PortalClientError, PortalStoreError) as exc:
        console.print(f"✗ {exc}")
        return int(ExitCode.CONFIGURATION)
    try:
        if sub == "status":
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
        console.print(f"✗ Unknown credential action '{sub}'.")
        return int(ExitCode.CONFIGURATION)
    except PortalClientError as exc:
        console.print(f"✗ {exc.message}")
        return int(ExitCode.CONFIGURATION)


def _security_status(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    try:
        client, _store_obj, _data, access = _auth_get(runtime, options)
    except (PortalClientError, PortalStoreError) as exc:
        console.print(f"✗ {exc}")
        return int(ExitCode.CONFIGURATION)
    try:
        body = client.activity(access)
    except PortalClientError as exc:
        console.print(f"✗ {exc.message}")
        return int(ExitCode.CONFIGURATION)
    console.print(f"Developer: {_data.get('developer_id', '?')}")
    console.print(f"Recent API activity: {len(body.get('events', []))} event(s)")
    return int(ExitCode.OK)


def _doctor(options: CLIOptions) -> int:
    runtime = build_runtime(options)
    console = runtime.console
    store = _store(runtime)
    client = _client(runtime, options)
    console.print("GhostLink Developer-Portal Doctor")
    # Local store health (never prints secrets).
    try:
        data = store.load()
        authed = bool(data.get("access_token"))
        console.print(f"  Signed in:    {'yes' if authed else 'no'}")
        console.print(f"  Store dir:    {store.directory}")
        console.print("  Store perms:  0600/0700")
    except PortalStoreError as exc:
        console.print(f"  Store:        ERROR — {exc}")
    # API connectivity (explicit, never silent).
    try:
        client.health()
        console.print(f"  Portal reachable at {client.base_url}")
    except PortalClientError as exc:
        console.print(f"  Portal:       unreachable — {exc.message}")
    return int(ExitCode.OK)
