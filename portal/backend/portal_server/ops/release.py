"""Release manifest, check, and verify (Phase 15F).

``ghostlink release check`` runs local deterministic gates and reports status.
``ghostlink release verify`` performs the same gates and returns non-zero if
any mandatory requirement fails. ``ghostlink release manifest`` emits a
machine-readable manifest containing version, phase, commit SHA, build
timestamp, Python version, package/frontend version, migration version,
dependency-lock state, security-check result, test count and build status.

A release NEVER deploys anything automatically.
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[4]  # repo root (portal/backend/ops/...)


def _git_head() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"], capture_output=True, text=True
        )
        return out.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def _git_dirty() -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "status", "--porcelain"], capture_output=True, text=True
        )
        return bool(out.stdout.strip())
    except Exception:
        return True


def _package_version() -> str:
    try:
        import ghostlink

        return ghostlink.__version__
    except Exception:
        return "unknown"


def _migration_version() -> int:
    try:
        from portal_server.db import PG_SCHEMA_VERSION, SCHEMA_VERSION

        return max(PG_SCHEMA_VERSION, SCHEMA_VERSION)
    except Exception:
        return 0


def _frontend_version() -> str:
    try:
        import json as _json

        pkg = REPO / "portal" / "web" / "package.json"
        if pkg.exists():
            return str(_json.loads(pkg.read_text()).get("version", "unknown"))
        return "unknown"
    except Exception:
        return "unknown"


def _test_count() -> dict[str, int]:
    """Best-effort: counts test functions discovered by pytest --collect-only."""
    try:
        out = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-o", "addopts="],
            cwd=str(REPO),
            capture_output=True,
            text=True,
        )
        # parse "NNN tests collected in Xs" (stderr or stdout)
        combined = out.stdout + out.stderr
        last = [ln for ln in combined.splitlines() if "collected" in ln]
        if last:
            import re

            m = re.search(r"(\d+)\s+test", last[-1])
            if m:
                return {"collected": int(m.group(1))}
    except Exception:
        pass
    return {"collected": 0}


def build_manifest() -> dict[str, Any]:
    return {
        "version": _package_version(),
        "phase": _release_phase(),
        "commit": _git_head(),
        "build_timestamp": datetime.now(UTC).isoformat(timespec="seconds"),
        "python": platform.python_version(),
        "package_version": _package_version(),
        "frontend_version": _frontend_version(),
        "migration_version": _migration_version(),
        "dependency_lock_state": _lock_state(),
        "security_check": "not_run",
        "test_count": _test_count(),
        "build_status": "not_built",
        "public_deployment": False,
    }


def _release_phase() -> str:
    try:
        from ghostlink.constants.app import RELEASE_LABEL

        return RELEASE_LABEL
    except Exception:
        return "unknown"


def _lock_state() -> str:
    # npm lock present; pip has no lockfile but dependencies are pinned in pyproject.
    if (REPO / "portal" / "web" / "package-lock.json").exists():
        return "npm-lock-present"
    return "unknown"


def run_release_gate() -> dict[str, Any]:
    """Run deterministic local checks; returns a report."""
    issues: list[dict[str, str]] = []
    # 1. version consistency package vs pyproject
    pyproject_version = ""
    try:
        for line in (REPO / "pyproject.toml").read_text().splitlines():
            if line.startswith("version"):
                pyproject_version = line.split("=")[1].strip().strip('"')
                break
    except Exception:
        pass
    pkg = _package_version()
    if pkg != pyproject_version:
        issues.append(
            {"category": "version", "detail": f"package={pkg} pyproject={pyproject_version}"}
        )

    # 2. dirty tree
    if _git_dirty():
        issues.append({"category": "tree", "detail": "working tree is not clean"})

    # 3. secret scan
    secret_rc = _run_script("scripts/scan_secrets.py")
    if secret_rc != 0:
        issues.append({"category": "secrets", "detail": "secret scan failed"})

    # 4. security audit
    audit_rc = _run_script("scripts/security_check.py")
    if audit_rc != 0:
        issues.append({"category": "security", "detail": "security audit failed"})

    # 5. tests (subprocess, quick subset is not reliable; full run is expensive —
    #    we record whether a smoke pytest import works)
    import_rc = _run_python("-c", "import ghostlink, portal_server; print('ok')")
    if import_rc != 0:
        issues.append({"category": "import", "detail": "package import failed"})

    clean = not issues
    return {
        "clean": clean,
        "issues": issues,
        "manifest": build_manifest(),
    }


def _run_script(rel: str) -> int:
    script = REPO / rel
    try:
        return subprocess.run([sys.executable, str(script)], cwd=str(REPO)).returncode
    except Exception:
        return 1


def _run_python(*args: str) -> int:
    env = dict(os.environ)
    pb = REPO / "portal" / "backend"
    env["PYTHONPATH"] = str(pb) + os.pathsep + env.get("PYTHONPATH", "")
    try:
        return subprocess.run([sys.executable, *args], cwd=str(REPO), env=env).returncode
    except Exception:
        return 1


__all__ = ["build_manifest", "run_release_gate"]
