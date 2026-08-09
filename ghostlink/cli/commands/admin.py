"""Phase 13 production administration commands.

``ghostlink db status|migrate|verify``, ``ghostlink backup …``,
``ghostlink system health|readiness`` and ``ghostlink security-audit``
delegate to the Developer Portal backend's operations layer
(``portal_server.ops.manage``), which owns the database. Because the portal
backend is a separate deployable (``portal/backend``), these commands work when
the repository checkout is available on the same host; otherwise they report
that the production-only functionality is unavailable with a deterministic
exit code. No secrets are ever printed.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ghostlink.cli.arguments import CLIOptions

_PORTAL_BACKEND_REL = Path("portal") / "backend"


def _run_admin(argv: list[str]) -> int:
    # Prefer importing the portal ops layer so the DB abstraction is used
    # directly (works from a repository checkout with the backend present).
    try:
        import portal_server.ops.manage
    except Exception:
        portal_backend = Path(__file__).resolve().parents[3] / _PORTAL_BACKEND_REL
        if portal_backend.is_dir():
            sys.path.insert(0, str(portal_backend))
            try:
                import portal_server.ops.manage  # type: ignore[import-not-found]
            except Exception as exc:  # pragma: no cover - defensive
                return _unavailable(f"portal backend could not be loaded: {exc}")
        else:
            return _unavailable(
                "the GhostLink Developer Portal backend is not installed on this host."
            )
    return int(portal_server.ops.manage.main(argv))


def _unavailable(reason: str) -> int:
    print(
        f"error: {reason} "
        "This production-only command requires the portal backend "
        "(portal/backend) and a configured DATABASE_URL.",
        file=sys.stderr,
    )
    return 3


def run_admin(options: CLIOptions) -> int:
    """Route a Phase 13 admin command to the portal operations layer."""
    command = options.command or ""
    if command == "security-audit":
        argv = ["security-audit"]
    else:
        argv = [command, *(options.admin_argv or [])]
    return _run_admin(argv)


__all__ = ["run_admin"]
