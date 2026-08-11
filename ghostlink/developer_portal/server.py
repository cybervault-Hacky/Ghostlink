"""Developer environment and process orchestration.

Manages the local Developer Portal development servers (WSGI backend and Vite
frontend) with automatic prerequisite checks, available port discovery, health
check verification, state persistence, and clean process lifecycle management.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ghostlink.developer_portal.store import STORE_DIR_NAME, PortalStore

DEFAULT_BACKEND_PORT = 8788
DEFAULT_FRONTEND_PORT = 5173
DEFAULT_BACKEND_PORTAL_URL = f"http://127.0.0.1:{DEFAULT_BACKEND_PORT}"
DEV_SERVERS_STATE_FILE = "dev_servers.json"


@dataclass(frozen=True, slots=True)
class PrerequisiteStatus:
    python_ok: bool
    python_version: str
    node_ok: bool
    node_version: str
    npm_ok: bool
    npm_version: str
    frontend_deps_ok: bool
    backend_code_ok: bool


@dataclass(frozen=True, slots=True)
class ServiceEndpoint:
    name: str
    host: str
    port: int
    url: str
    pid: int | None = None
    ready: bool = False


@dataclass(frozen=True, slots=True)
class DevServicesState:
    backend: ServiceEndpoint
    frontend: ServiceEndpoint
    started_at: str


def find_repo_root() -> Path:
    """Find the root directory of the GhostLink repository."""
    # Try relative to this file
    current = Path(__file__).resolve()
    for parent in [current, *current.parents]:
        if (parent / "pyproject.toml").exists() and (parent / "portal").exists():
            return parent
    return Path.cwd()


def is_port_listening(port: int, host: str = "127.0.0.1") -> bool:
    """Check if a TCP port is actively accepting connections."""
    try:
        with socket.create_connection((host, port), timeout=0.5):
            return True
    except (OSError, TimeoutError):
        return False


def find_available_port(preferred: int, host: str = "127.0.0.1") -> int:
    """Find the preferred port or next available localhost port."""
    port = preferred
    while port < preferred + 100:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind((host, port))
                return port
        except OSError:
            port += 1
    raise RuntimeError(f"No available localhost port found in range {preferred}-{preferred + 100}.")


def check_prerequisites(repo_root: Path | None = None) -> PrerequisiteStatus:
    """Check all prerequisites for local developer portal execution."""
    root = repo_root or find_repo_root()
    py_ok = sys.version_info >= (3, 11)
    py_ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"

    node_bin = shutil.which("node")
    node_ok = node_bin is not None
    node_ver = "not found"
    if node_ok:
        try:
            res = subprocess.run(
                ["node", "--version"],
                capture_output=True,
                text=True,
                check=True,
                timeout=3,
            )
            node_ver = res.stdout.strip()
        except Exception:
            node_ver = "unknown"

    npm_bin = shutil.which("npm")
    npm_ok = npm_bin is not None
    npm_ver = "not found"
    if npm_ok:
        try:
            res = subprocess.run(
                ["npm", "--version"],
                capture_output=True,
                text=True,
                check=True,
                timeout=3,
            )
            npm_ver = res.stdout.strip()
        except Exception:
            npm_ver = "unknown"

    web_dir = root / "portal" / "web"
    frontend_deps_ok = (web_dir / "node_modules").is_dir()
    backend_code_ok = (root / "portal" / "backend" / "portal_server").is_dir()

    return PrerequisiteStatus(
        python_ok=py_ok,
        python_version=py_ver,
        node_ok=node_ok,
        node_version=node_ver,
        npm_ok=npm_ok,
        npm_version=npm_ver,
        frontend_deps_ok=frontend_deps_ok,
        backend_code_ok=backend_code_ok,
    )


def ensure_frontend_dependencies(repo_root: Path | None = None) -> bool:
    """Ensure frontend npm packages are installed; installs only when needed."""
    root = repo_root or find_repo_root()
    web_dir = root / "portal" / "web"
    if (web_dir / "node_modules").is_dir():
        return True

    if not shutil.which("npm"):
        return False

    try:
        subprocess.run(
            ["npm", "install", "--no-audit", "--no-fund"],
            cwd=str(web_dir),
            check=True,
            capture_output=True,
            timeout=120,
        )
        return (web_dir / "node_modules").is_dir()
    except Exception:
        return False


def _state_file_path(data_dir: Path) -> Path:
    return data_dir / STORE_DIR_NAME / DEV_SERVERS_STATE_FILE


def load_dev_servers_state(data_dir: Path) -> dict[str, Any] | None:
    """Load the state of running developer servers."""
    path = _state_file_path(data_dir)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
            return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_dev_servers_state(data_dir: Path, state: dict[str, Any]) -> None:
    """Save developer servers process state atomically."""
    path = _state_file_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp_path, path)
    with contextlib.suppress(OSError):
        os.chmod(path, 0o600)


def clear_dev_servers_state(data_dir: Path) -> None:
    """Remove developer servers process state."""
    path = _state_file_path(data_dir)
    if path.exists():
        with contextlib.suppress(OSError):
            path.unlink()


def is_process_running(pid: int) -> bool:
    """Check whether a process PID is currently running."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def check_backend_health(url: str, timeout: float = 1.0) -> bool:
    """Probe the Developer Portal backend health endpoint."""
    health_url = f"{url.rstrip('/')}/api/v1/developer/health"
    try:
        req = urllib.request.Request(health_url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return bool(resp.status == 200)
    except Exception:
        return False


def check_frontend_health(url: str, timeout: float = 1.0) -> bool:
    """Probe the Developer Portal frontend dev server."""
    try:
        req = urllib.request.Request(url.rstrip("/"))
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return bool(resp.status in (200, 304))
    except Exception:
        return False


def start_developer_services(
    *,
    data_dir: Path,
    repo_root: Path | None = None,
    backend_port: int | None = None,
    frontend_port: int | None = None,
    host: str = "127.0.0.1",
    poll_timeout: float = 10.0,
) -> dict[str, Any]:
    """Start local Developer Portal backend and frontend dev servers with health checks."""
    root = repo_root or find_repo_root()
    backend_dir = root / "portal" / "backend"
    web_dir = root / "portal" / "web"

    # Check for existing running servers
    existing = load_dev_servers_state(data_dir)
    if existing:
        b_pid = existing.get("backend_pid")
        f_pid = existing.get("frontend_pid")
        b_url = existing.get("backend_url", f"http://{host}:{DEFAULT_BACKEND_PORT}")
        f_url = existing.get("frontend_url", f"http://{host}:{DEFAULT_FRONTEND_PORT}")

        backend_alive = isinstance(b_pid, int) and is_process_running(b_pid)
        frontend_alive = isinstance(f_pid, int) and is_process_running(f_pid)

        if (
            backend_alive
            and check_backend_health(b_url)
            and frontend_alive
            and check_frontend_health(f_url)
        ):
            return {
                "backend_url": b_url,
                "frontend_url": f_url,
                "backend_pid": b_pid,
                "frontend_pid": f_pid,
                "already_running": True,
                "ready": True,
            }

    # Verify prerequisites
    prereqs = check_prerequisites(root)
    if not prereqs.python_ok:
        raise RuntimeError("Python 3.11+ is required to run the Developer Portal backend.")

    # 1. Backend port & startup
    b_port = backend_port or DEFAULT_BACKEND_PORT
    if not is_port_listening(b_port, host):
        b_port = find_available_port(b_port, host)
        # Launch backend WSGI process
        env = dict(os.environ)
        env["PYTHONPATH"] = str(backend_dir)
        env["APP_ENV"] = "development"
        env["PORTAL_HOST"] = host
        env["PORTAL_PORT"] = str(b_port)
        env["DATABASE_URL"] = "portal.db"
        env["SESSION_SECRET"] = "development-secret"
        env["EMAIL_PROVIDER"] = "dev"

        backend_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "portal_server",
                "--host",
                host,
                "--port",
                str(b_port),
            ],
            cwd=str(backend_dir),
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        b_pid = backend_proc.pid
    else:
        b_pid = None

    backend_url = f"http://{host}:{b_port}"

    # Poll backend health
    deadline = time.monotonic() + poll_timeout
    backend_ready = False
    while time.monotonic() < deadline:
        if check_backend_health(backend_url, timeout=0.5):
            backend_ready = True
            break
        time.sleep(0.2)

    if not backend_ready:
        if b_pid and is_process_running(b_pid):
            with contextlib.suppress(Exception):
                os.kill(b_pid, signal.SIGTERM)
        raise RuntimeError(f"Backend failed to start or become ready on {backend_url}.")

    # 2. Frontend port & startup
    f_port = frontend_port or DEFAULT_FRONTEND_PORT
    f_pid = None
    frontend_url = f"http://{host}:{f_port}"

    if prereqs.node_ok and prereqs.npm_ok:
        if not prereqs.frontend_deps_ok:
            ensure_frontend_dependencies(root)

        if not is_port_listening(f_port, host):
            f_port = find_available_port(f_port, host)
            f_env = dict(os.environ)
            f_env["PORTAL_API_URL"] = backend_url

            frontend_proc = subprocess.Popen(
                [
                    "npm",
                    "run",
                    "dev",
                    "--",
                    "--host",
                    host,
                    "--port",
                    str(f_port),
                ],
                cwd=str(web_dir),
                env=f_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            f_pid = frontend_proc.pid

        frontend_url = f"http://{host}:{f_port}"

        # Poll frontend health
        f_deadline = time.monotonic() + poll_timeout
        while time.monotonic() < f_deadline:
            if check_frontend_health(frontend_url, timeout=0.5):
                break
            time.sleep(0.2)
    else:
        # Frontend cannot be started without Node/npm; fallback to backend URL
        frontend_url = backend_url

    # Save running state
    now_iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    state_payload = {
        "backend_pid": b_pid,
        "backend_port": b_port,
        "backend_url": backend_url,
        "frontend_pid": f_pid,
        "frontend_port": f_port,
        "frontend_url": frontend_url,
        "started_at": now_iso,
    }
    save_dev_servers_state(data_dir, state_payload)

    return {
        "backend_url": backend_url,
        "frontend_url": frontend_url,
        "backend_pid": b_pid,
        "frontend_pid": f_pid,
        "ready": True,
    }


def stop_developer_services(data_dir: Path) -> dict[str, Any]:
    """Stop running developer processes started by GhostLink."""
    state = load_dev_servers_state(data_dir)
    stopped_pids: list[int] = []

    if state:
        for key in ("backend_pid", "frontend_pid"):
            pid = state.get(key)
            if isinstance(pid, int) and is_process_running(pid):
                try:
                    os.kill(pid, signal.SIGTERM)
                    stopped_pids.append(pid)
                except OSError:
                    pass

        # Give processes a moment to terminate gracefully
        time.sleep(0.5)

        for pid in stopped_pids:
            if is_process_running(pid):
                with contextlib.suppress(OSError):
                    os.kill(pid, signal.SIGKILL)

    clear_dev_servers_state(data_dir)
    return {"stopped_pids": stopped_pids, "stopped": True}


def get_developer_status(
    data_dir: Path, portal_url: str = DEFAULT_BACKEND_PORTAL_URL
) -> dict[str, Any]:
    """Retrieve the real-time status of the developer environment."""
    state = load_dev_servers_state(data_dir)
    store = PortalStore(data_dir)

    b_url = state.get("backend_url", portal_url) if state else portal_url
    f_url = (
        state.get("frontend_url", f"http://127.0.0.1:{DEFAULT_FRONTEND_PORT}")
        if state
        else f"http://127.0.0.1:{DEFAULT_FRONTEND_PORT}"
    )

    backend_ready = check_backend_health(b_url)
    frontend_ready = check_frontend_health(f_url)

    auth_data = store.load()
    is_authenticated = bool(auth_data.get("access_token"))

    return {
        "backend_url": b_url,
        "backend_ready": backend_ready,
        "frontend_url": f_url,
        "frontend_ready": frontend_ready,
        "authenticated": is_authenticated,
        "developer_id": auth_data.get("developer_id", ""),
        "scopes": auth_data.get("scopes", []),
        "servers_running": bool(state and (backend_ready or frontend_ready)),
    }
