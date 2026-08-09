"""GhostLink Developer Portal — backend (Phase 10B).

A self-contained, security-focused web application for GhostLink
developers: remote developer accounts, secure authentication (password,
sessions, MFA/TOTP, WebAuthn architecture), developer credentials with a
secure lifecycle (create / rotate / revoke / verify), projects, sessions,
security activity, and account settings.

Design principles
-----------------
* **Minimal, auditable dependencies.** The HTTP layer is a small WSGI
  application on the standard library (runs under ``wsgiref`` for dev and
  tests; gunicorn/waitress for production). SQLite is the development
  database with a PostgreSQL-ready schema (no SQLAlchemy dependency).
* **Secrets are never stored in plaintext.** Passwords are hashed with
  PBKDF2-HMAC-SHA256 (per-user salt, 600k iterations). Developer secrets,
  recovery codes, and session/token values are stored only as salted
  hashes/verifiers. Plaintext is shown exactly once, if ever.
* **Fail closed.** Everything is validated, rate-limited, authorized, and
  audited; malformed or unauthorized requests never leak sensitive state.
* **No telemetry, no tracking, no analytics.** Security activity is
  application functionality, not advertising.
"""

from __future__ import annotations

__version__ = "0.12.0"
