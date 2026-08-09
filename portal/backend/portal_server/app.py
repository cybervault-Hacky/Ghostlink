"""Developer Portal — WSGI application, routing, and domain handlers (Phase 10B).

Single cohesive module: the WSGI entry point, request routing, session/CSRF
resolution, and the API handlers for authentication, account, developer
credentials, projects, sessions, security activity, settings, MFA (TOTP)
and WebAuthn.

All responses are strict JSON with typed error envelopes; all mutations are
authenticated, CSRF-protected, rate-limited, and audited. Secrets are never
returned after creation.
"""

from __future__ import annotations

import contextlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ghostlink.developer import keys as dev_keys
from portal_server import auth, devapi_handlers, webauthn
from portal_server.config import PortalConfig
from portal_server.db import DatabaseBackend
from portal_server.emailing import DevEmailProvider, EmailProvider
from portal_server.http import (
    COOKIE_NAME,
    Request,
    Response,
    app_response,
    created,
    error_response,
    json_response,
    ok,
)
from portal_server.mfa import generate_totp_secret, verify_totp
from portal_server.observability import StructuredLogger, validate_request_id
from portal_server.ratelimit import (
    InMemoryRateLimiter,
    RateLimitBackendError,
    build_rate_limiter,
)

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

SESSION_TTL_SECONDS = 7 * 24 * 3600  # 7 days

# Phase 13: request-body size limit (13U) — reject oversized bodies early.
MAX_BODY_BYTES = 1024 * 1024  # 1 MiB


# Backward-compatible alias: tests and the dev runner construct ``EmailSender``
# which is the in-memory dev provider.
EmailSender = DevEmailProvider


class Portal:
    """Holds app config and shared state; handed to handlers."""

    def __init__(
        self,
        db: DatabaseBackend,
        *,
        secure_cookies: bool = False,
        emails: EmailProvider | None = None,
        clock: Callable[[], float] = time.monotonic,
        config: PortalConfig | None = None,
    ) -> None:
        self.db = db
        self.config = config
        self.secure_cookies = secure_cookies or (config is not None and config.secure_cookies)
        # Default email is disabled so the portal never claims real delivery
        # unless a provider is explicitly enabled (dev or SMTP).
        self.emails = emails if emails is not None else DevEmailProvider(enabled=False)
        # Phase 13: rate limiter is selected by configuration; a PostgreSQL
        # deployment uses the distributed backend (fail closed on misuse).
        if config is not None and config.rate_limit_backend == "postgresql":
            self.limiter = build_rate_limiter(
                "postgresql", db, overrides=config.rate_limit_overrides or None
            )
        else:
            self.limiter = InMemoryRateLimiter(
                monotonic=clock,
                limits=(
                    config.rate_limit_overrides if config and config.rate_limit_overrides else None
                ),
            )
        self.clock = clock
        self.logger = StructuredLogger(
            level=config.log_level if config else "info",
            log_format=config.log_format if config else "json",
            environment=config.app_env if config else "development",
            deployment_version=config.deployment_version if config else "",
        )
        self.request_id_header = config.request_id_header if config else "X-Request-ID"
        self.webauthn_challenges: dict[str, tuple[str, float]] = {}  # key -> (value, expiry_mono)
        self.preauth_tickets: dict[str, int] = {}  # ticket -> user_id (short-lived)


# ------------------------------------------------------------------ helpers


def _hash_verifier_from_secret(credential: str) -> tuple[str, str, str]:
    """Hash the *secret portion* of a credential (never the full string)."""
    _kid, secret_part = dev_keys.parse_credential(credential)
    verifier, salt, digest = dev_keys.make_verifier(secret_part)
    return verifier, salt, digest


def _credential_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "name": row["name"],
        "keyId": row["key_id"],
        "status": row["status"],
        "createdAt": row["created_at"],
        "lastUsedAt": row["last_used_at"],
        "rotatedAt": row["rotated_at"],
        "revokedAt": row["revoked_at"],
    }


def _user_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "email": row["email"],
        "status": row["status"],
        "emailVerified": bool(row["email_verified"]),
        "developerId": row["developer_id"],
        "createdAt": row["created_at"],
    }


# ------------------------------------------------------------------- auth


def _authenticate(
    request: Request, portal: Portal
) -> tuple[dict[str, Any] | None, Response | None]:
    """Resolve the current user from the session cookie; also CSRF-checks
    unsafe methods. Returns (user_row, error_response)."""
    token = request.cookies.get(COOKIE_NAME)
    if not token:
        return None, error_response(401, "unauthorized", "Not signed in.")
    row = portal.db.query_one(
        "SELECT * FROM sessions WHERE token_hash=?",
        (auth.hash_secret(token),),
    )
    if row is None:
        return None, error_response(401, "unauthorized", "Session not found.")
    # Absolute session lifetime.
    if row["revoked_at"] or row["expires_at"] < auth.now_iso():
        return None, error_response(401, "unauthorized", "Session expired or revoked.")
    # Idle timeout (Phase 11 session hardening).
    idle_seconds = portal.config.idle_timeout_seconds if portal.config else 30 * 60
    idle_ok = _within_last(row["last_active_at"], idle_seconds)
    if not idle_ok:
        portal.db.execute(
            "UPDATE sessions SET revoked_at=? WHERE id=?", (auth.now_iso(), row["id"])
        )
        return None, error_response(401, "unauthorized", "Session idle timeout exceeded.")
    user = portal.db.query_one("SELECT * FROM users WHERE id=?", (row["user_id"],))
    if user is None or user["status"] != "active":
        return None, error_response(403, "forbidden", "Account is not active.")
    # CSRF check on unsafe methods.
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        provided = request.header("X-CSRF-Token")
        if not auth.constant_time_equal(provided, row["csrf_token"]):
            return None, error_response(403, "csrf", "CSRF validation failed.")
    portal.db.execute(
        "UPDATE sessions SET last_active_at=? WHERE id=?",
        (auth.now_iso(), row["id"]),
    )
    return user, None


def _within_last(iso_value: str | None, seconds: int) -> bool:
    """True if ``iso_value`` (ISO-8601 UTC) is within ``seconds`` of now."""
    from datetime import UTC, datetime, timedelta

    if not iso_value:
        return True  # never set — treat as fresh (defensive)
    try:
        moment = datetime.fromisoformat(iso_value)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        now = datetime.now(UTC)
        return (now - moment) < timedelta(seconds=seconds)
    except ValueError:
        return False  # malformed timestamp fails closed


def _record_event(
    portal: Portal, user_id: int, action: str, actor: str, metadata: dict[str, Any] | None = None
) -> None:
    portal.db.execute(
        "INSERT INTO security_events(user_id, action, actor, metadata, created_at) "
        "VALUES(?,?,?,?,?)",
        (user_id, action, actor, __import__("json").dumps(metadata or {}), auth.now_iso()),
    )


def _session_payload(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "createdAt": row["created_at"],
        "lastActiveAt": row["last_active_at"],
        "expiresAt": row["expires_at"],
        "revokedAt": row["revoked_at"],
        "device": row["device"],
        "browser": row["browser"],
        "os": row["os"],
    }


# -------------------------------------------------------------- handlers


def handle_signup(portal: Portal, request: Request) -> Response:
    if not portal.limiter.allow("sign_up", request.client_ip()):
        return error_response(429, "rate_limited", "Too many sign-up attempts. Try again later.")
    body = request.body_json or {}
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    if not EMAIL_RE.fullmatch(email):
        return error_response(400, "invalid_email", "A valid email is required.")
    if len(password) < 12 or len(password) > 128:
        return error_response(400, "weak_password", "Password must be 12-128 characters.")
    if portal.db.query_one("SELECT id FROM users WHERE email=?", (email,)):
        return error_response(409, "email_taken", "An account with that email already exists.")
    developer_id = dev_keys.generate_developer_id()
    user_id = portal.db.execute_many(
        [
            (
                "INSERT INTO users(email, password_hash, status, developer_id, "
                "created_at, updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    email,
                    auth.hash_password(password),
                    "pending",
                    developer_id,
                    auth.now_iso(),
                    auth.now_iso(),
                ),
            )
        ],
        lastrow=True,
    )
    if user_id is None:
        return error_response(500, "internal", "Could not create account.")
    token = auth.make_token()
    portal.db.execute(
        "INSERT INTO tokens(user_id, kind, token_hash, expires_at, created_at) VALUES(?,?,?,?,?)",
        (user_id, "email_verify", auth.hash_secret(token), _future_iso(24 * 3600), auth.now_iso()),
    )
    if portal.emails.enabled:
        portal.emails.send_verification(email, token)
    _record_event(portal, user_id, "signup", "email", {"email": email})
    return created(
        {
            "user": {
                "id": user_id,
                "email": email,
                "status": "pending",
                "emailVerified": False,
                "developerId": developer_id,
            }
        }
    )


def handle_email_verify(portal: Portal, request: Request) -> Response:
    body = request.body_json or {}
    token = str(body.get("token", ""))
    row = portal.db.query_one(
        "SELECT * FROM tokens WHERE kind='email_verify' AND token_hash=?",
        (auth.hash_secret(token),),
    )
    if row is None or row["used_at"] or row["expires_at"] < auth.now_iso():
        return error_response(400, "invalid_token", "This verification link is invalid or expired.")
    portal.db.execute_many(
        [
            ("UPDATE tokens SET used_at=? WHERE id=?", (auth.now_iso(), row["id"])),
            (
                "UPDATE users SET status='active', email_verified=1, updated_at=? WHERE id=?",
                (auth.now_iso(), row["user_id"]),
            ),
        ]
    )
    _record_event(portal, row["user_id"], "email_verified", "email")
    return ok({"verified": True})


def handle_signin(portal: Portal, request: Request) -> Response:
    if not portal.limiter.allow("sign_in", request.client_ip()):
        return error_response(429, "rate_limited", "Too many sign-in attempts. Try again later.")
    body = request.body_json or {}
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    user = portal.db.query_one("SELECT * FROM users WHERE email=?", (email,))
    if user is None or not auth.verify_password(password, user["password_hash"]):
        if user is not None:
            portal.db.execute(
                "UPDATE users SET failed_logins=failed_logins+1 WHERE id=?",
                (user["id"],),
            )
            _record_event(portal, user["id"], "failed_signin", "unknown")
        return error_response(401, "invalid_credentials", "Incorrect email or password.")
    if user["status"] != "active":
        return error_response(403, "not_verified", "Email not verified yet.")
    if user["locked_until"] and user["locked_until"] > auth.now_iso():
        return error_response(403, "locked", "Account temporarily locked.")
    # MFA gate: if enabled, require a pending MFA ticket.
    mfa = portal.db.query_one("SELECT * FROM mfa_totp WHERE user_id=? AND enabled=1", (user["id"],))
    if mfa:
        return json_response(
            200, {"mfaRequired": True, "preauth": _issue_preauth(portal, user["id"])}
        )
    return _complete_signin(portal, request, user)


def _issue_preauth(portal: Portal, user_id: int) -> str:
    # A short-lived in-memory ticket that only permits MFA completion.
    ticket = auth.make_token()
    portal.preauth_tickets[ticket] = user_id
    return ticket


def handle_mfa_verify(portal: Portal, request: Request) -> Response:
    body = request.body_json or {}
    preauth = str(body.get("preauth", ""))
    code = str(body.get("code", ""))
    user_id = portal.preauth_tickets.pop(preauth, None)
    if user_id is None:
        return error_response(400, "invalid_ticket", "MFA session expired.")
    mfa = portal.db.query_one("SELECT * FROM mfa_totp WHERE user_id=? AND enabled=1", (user_id,))
    if mfa is None or not verify_totp(mfa["secret"], code):
        _record_event(portal, user_id, "mfa_failed", "mfa")
        return error_response(401, "invalid_code", "Incorrect authentication code.")
    user = portal.db.query_one("SELECT * FROM users WHERE id=?", (user_id,))
    if user is None:
        return error_response(500, "internal", "Account not found.")
    return _complete_signin(portal, request, user)


def _future_iso(seconds: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def _session_expiry(portal: Portal) -> str:
    ttl = portal.config.session_ttl_seconds if portal.config else SESSION_TTL_SECONDS
    return _future_iso(ttl)


def _complete_signin(portal: Portal, request: Request, user: dict[str, Any]) -> Response:
    token = auth.new_session_token()
    csrf = auth.new_csrf_token()
    now = auth.now_iso()
    with portal.db.transaction():
        portal.db.execute(
            "UPDATE users SET failed_logins=0, locked_until=NULL WHERE id=?",
            (user["id"],),
        )
        portal.db.execute(
            "INSERT INTO sessions(user_id, token_hash, created_at, expires_at, last_active_at, "
            "device, browser, os, ip, csrf_token) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                user["id"],
                auth.hash_secret(token),
                now,
                _session_expiry(portal),
                now,
                str(request.body_json.get("device", "") or "")[:80]
                if isinstance(request.body_json, dict)
                else "",
                str(request.header("User-Agent"))[:120],
                "linux",
                request.client_ip(),
                csrf,
            ),
        )
    _record_event(portal, user["id"], "signin", "session")
    resp = ok({"user": _user_payload(user), "csrfToken": csrf})
    resp.set_cookie(
        COOKIE_NAME,
        token,
        secure=portal.secure_cookies,
        HttpOnly=True,
        SameSite="Lax",
        MaxAge=SESSION_TTL_SECONDS,
    )
    return resp


def handle_signout(portal: Portal, request: Request) -> Response:
    token = request.cookies.get(COOKIE_NAME)
    if token:
        portal.db.execute(
            "UPDATE sessions SET revoked_at=? WHERE token_hash=?",
            (auth.now_iso(), auth.hash_secret(token)),
        )
    resp = ok({"signedOut": True})
    resp.set_cookie(
        COOKIE_NAME, "", secure=portal.secure_cookies, HttpOnly=True, SameSite="Lax", MaxAge=0
    )
    return resp


def handle_password_reset_request(portal: Portal, request: Request) -> Response:
    if not portal.limiter.allow("password_reset_request", request.client_ip()):
        return error_response(429, "rate_limited", "Too many reset requests.")
    body = request.body_json or {}
    email = str(body.get("email", "")).strip().lower()
    user = portal.db.query_one("SELECT id FROM users WHERE email=?", (email,))
    # Generic response regardless of whether the email exists.
    if user is not None:
        token = auth.make_token()
        portal.db.execute(
            "INSERT INTO tokens(user_id, kind, token_hash, expires_at, created_at) "
            "VALUES(?,?,?,?,?)",
            (
                user["id"],
                "password_reset",
                auth.hash_secret(token),
                _future_iso(3600),
                auth.now_iso(),
            ),
        )
        if portal.emails.enabled:
            portal.emails.send_password_reset(email, token)
    return ok({"sent": True})


def handle_password_reset(portal: Portal, request: Request) -> Response:
    body = request.body_json or {}
    token = str(body.get("token", ""))
    password = str(body.get("password", ""))
    if len(password) < 12 or len(password) > 128:
        return error_response(400, "weak_password", "Password must be 12-128 characters.")
    row = portal.db.query_one(
        "SELECT * FROM tokens WHERE kind='password_reset' AND token_hash=?",
        (auth.hash_secret(token),),
    )
    if row is None or row["used_at"] or row["expires_at"] < auth.now_iso():
        return error_response(400, "invalid_token", "This reset link is invalid or expired.")
    portal.db.execute_many(
        [
            ("UPDATE tokens SET used_at=? WHERE id=?", (auth.now_iso(), row["id"])),
            (
                "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
                (auth.hash_password(password), auth.now_iso(), row["user_id"]),
            ),
        ]
    )
    _record_event(portal, row["user_id"], "password_changed", "reset")
    return ok({"reset": True})


# --------------------------------------------------------------- dashboard


def handle_dashboard(portal: Portal, request: Request, user: dict[str, Any]) -> Response:
    resp = _dashboard_base(portal, user)
    # Phase 12: developer-API platform counts.
    devices = portal.db.query_one(
        "SELECT COUNT(*) AS n FROM developer_devices WHERE user_id=? AND status='active'",
        (user["id"],),
    )
    api_creds = portal.db.query_one(
        "SELECT COUNT(*) AS n FROM api_credentials WHERE user_id=? AND status='active'",
        (user["id"],),
    )
    resp["devApi"] = {
        "devices": int(devices["n"]) if devices else 0,
        "credentials": int(api_creds["n"]) if api_creds else 0,
    }
    return ok(resp)


def _dashboard_base(portal: Portal, user: dict[str, Any]) -> dict[str, Any]:
    creds = portal.db.query(
        "SELECT * FROM credentials WHERE user_id=? ORDER BY created_at DESC", (user["id"],)
    )
    projects = portal.db.query(
        "SELECT * FROM projects WHERE user_id=? ORDER BY created_at DESC", (user["id"],)
    )
    sessions = portal.db.query(
        "SELECT * FROM sessions WHERE user_id=? ORDER BY created_at DESC", (user["id"],)
    )
    mfa = portal.db.query_one("SELECT enabled FROM mfa_totp WHERE user_id=?", (user["id"],))
    return {
        "user": _user_payload(user),
        "counts": {
            "credentials": len([c for c in creds if c["status"] == "active"]),
            "projects": len([p for p in projects if p["status"] == "active"]),
            "sessions": len([s for s in sessions if not s["revoked_at"]]),
            "mfaEnabled": bool(mfa and mfa["enabled"]),
        },
    }


# ------------------------------------------------- developer API (web, 12N)


def handle_devapi_devices(portal: Portal, request: Request, user: dict[str, Any]) -> Response:
    """List the developer's Termux/API devices (session-authenticated web)."""
    rows = portal.db.query(
        "SELECT device_id, name, platform, client_version, status, created_at, last_seen_at "
        "FROM developer_devices WHERE user_id=? ORDER BY created_at DESC",
        (user["id"],),
    )
    return ok({"devices": rows})


def handle_devapi_device_revoke(
    portal: Portal, request: Request, user: dict[str, Any], device_id: str
) -> Response:
    """Revoke a developer device (persistent, fail-closed)."""
    row = portal.db.query_one(
        "SELECT id FROM developer_devices WHERE device_id=? AND user_id=?",
        (device_id, user["id"]),
    )
    if row is None:
        return error_response(404, "not_found", "Device not found.")
    with portal.db.transaction():
        portal.db.execute("UPDATE developer_devices SET status='revoked' WHERE id=?", (row["id"],))
        portal.db.execute(
            "UPDATE api_tokens SET revoked_at=? WHERE device_id=?", (auth.now_iso(), row["id"])
        )
    _record_event(portal, user["id"], "device_revoked", "web", {"deviceId": device_id})
    return ok({"revoked": True})


def handle_devapi_credentials(portal: Portal, request: Request, user: dict[str, Any]) -> Response:
    """List scoped developer-API credentials (metadata only, never the secret)."""
    rows = portal.db.query(
        "SELECT credential_id, name, scopes, status, created_at, last_used_at, rotated_at, "
        "revoked_at FROM api_credentials WHERE user_id=? ORDER BY created_at DESC",
        (user["id"],),
    )
    return ok(
        {
            "credentials": [
                {
                    "credential_id": r["credential_id"],
                    "name": r["name"],
                    "scopes": r["scopes"].split(),
                    "status": r["status"],
                    "created_at": r["created_at"],
                    "last_used_at": r["last_used_at"],
                    "rotated_at": r["rotated_at"],
                    "revoked_at": r["revoked_at"],
                }
                for r in rows
            ]
        }
    )


def handle_devapi_credential_revoke(
    portal: Portal, request: Request, user: dict[str, Any], credential_id: str
) -> Response:
    row = portal.db.query_one(
        "SELECT id FROM api_credentials WHERE credential_id=? AND user_id=?",
        (credential_id, user["id"]),
    )
    if row is None:
        return error_response(404, "not_found", "Credential not found.")
    with portal.db.transaction():
        portal.db.execute(
            "UPDATE api_credentials SET status='revoked', revoked_at=? WHERE id=?",
            (auth.now_iso(), row["id"]),
        )
        portal.db.execute(
            "UPDATE api_tokens SET revoked_at=? WHERE credential_id=?", (auth.now_iso(), row["id"])
        )
    _record_event(portal, user["id"], "api_key_revoked", "web", {"credentialId": credential_id})
    return ok({"revoked": True})


def handle_devapi_activity(portal: Portal, request: Request, user: dict[str, Any]) -> Response:
    """Metadata-only API activity log (no tokens, no bodies, no secrets)."""
    rows = portal.db.query(
        "SELECT endpoint, category, result, created_at FROM api_activity "
        "WHERE user_id=? ORDER BY created_at DESC LIMIT 100",
        (user["id"],),
    )
    return ok({"events": rows})


def handle_devapi_pairing(portal: Portal, request: Request, user: dict[str, Any]) -> Response:
    """Active pairing codes for the developer's account (metadata only)."""
    rows = portal.db.query(
        "SELECT nonce, device_id, status, created_at, expires_at FROM pairing_codes "
        "WHERE user_id=? AND status='pending' ORDER BY created_at DESC LIMIT 50",
        (user["id"],),
    )
    return ok({"pairing": rows})


# ------------------------------------------------------------- credentials


def handle_credentials_list(portal: Portal, request: Request, user: dict[str, Any]) -> Response:
    rows = portal.db.query(
        "SELECT * FROM credentials WHERE user_id=? ORDER BY created_at DESC", (user["id"],)
    )
    return ok({"credentials": [_credential_payload(r) for r in rows]})


def handle_credential_create(portal: Portal, request: Request, user: dict[str, Any]) -> Response:
    if not portal.limiter.allow("credential_create", f"u{user['id']}"):
        return error_response(429, "rate_limited", "Too many credential creations.")
    body = request.body_json or {}
    name = str(body.get("name", "")).strip()
    if not (0 < len(name) <= 64):
        return error_response(400, "invalid_name", "Name must be 1-64 characters.")
    key_id, secret = dev_keys.issue_credential()
    verifier, salt, digest = _hash_verifier_from_secret(secret)
    row_id = portal.db.execute_many(
        [
            (
                "INSERT INTO credentials(user_id, name, key_id, secret_hash, secret_salt, "
                "verifier, "
                "status, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (user["id"], name, key_id, digest, salt, verifier, "active", auth.now_iso()),
            )
        ],
        lastrow=True,
    )
    _record_event(portal, user["id"], "key_created", "web", {"keyId": key_id})
    return created({"credential": {"keyId": key_id, "name": name, "secret": secret, "id": row_id}})


def handle_credential_rotate(
    portal: Portal, request: Request, user: dict[str, Any], key_id: str
) -> Response:
    if not portal.limiter.allow("credential_rotate", f"u{user['id']}"):
        return error_response(429, "rate_limited", "Too many rotations.")
    row = portal.db.query_one(
        "SELECT * FROM credentials WHERE user_id=? AND key_id=?", (user["id"], key_id)
    )
    if row is None:
        return error_response(404, "not_found", "Credential not found.")
    if row["status"] == "revoked":
        return error_response(400, "revoked", "This credential is already revoked.")
    new_key_id, secret = dev_keys.issue_credential()
    verifier, salt, digest = _hash_verifier_from_secret(secret)
    portal.db.execute_many(
        [
            (
                "UPDATE credentials SET status='revoked', revoked_at=?, rotated_at=? WHERE id=?",
                (auth.now_iso(), auth.now_iso(), row["id"]),
            ),
            (
                "INSERT INTO credentials(user_id, name, key_id, secret_hash, secret_salt, "
                "verifier, "
                "status, created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    user["id"],
                    row["name"] + " (rotated)",
                    new_key_id,
                    digest,
                    salt,
                    verifier,
                    "active",
                    auth.now_iso(),
                ),
            ),
        ]
    )
    _record_event(portal, user["id"], "key_rotated", "web", {"keyId": row["key_id"]})
    return ok({"credential": {"keyId": new_key_id, "secret": secret}})


def handle_credential_revoke(
    portal: Portal, request: Request, user: dict[str, Any], key_id: str
) -> Response:
    if not portal.limiter.allow("credential_revoke", f"u{user['id']}"):
        return error_response(429, "rate_limited", "Too many revocations.")
    row = portal.db.query_one(
        "SELECT * FROM credentials WHERE user_id=? AND key_id=?", (user["id"], key_id)
    )
    if row is None:
        return error_response(404, "not_found", "Credential not found.")
    portal.db.execute(
        "UPDATE credentials SET status='revoked', revoked_at=? WHERE id=?",
        (auth.now_iso(), row["id"]),
    )
    _record_event(portal, user["id"], "key_revoked", "web", {"keyId": key_id})
    return ok({"revoked": True})


def handle_credential_verify(portal: Portal, request: Request) -> Response:
    # Public verification endpoint: checks a presented credential without
    # exposing anything. Returns a generic result (no key existence leak).
    body = request.body_json or {}
    credential = str(body.get("credential", ""))
    try:
        key_id, secret = dev_keys.parse_credential(credential)
    except Exception:
        return error_response(400, "invalid_credential", "Malformed credential.")
    row = portal.db.query_one("SELECT * FROM credentials WHERE key_id=?", (key_id,))
    if row is None or row["status"] != "active":
        return error_response(401, "unauthorized", "Credential not valid.")
    try:
        dev_keys.verify_secret(secret, row["verifier"], row["secret_salt"], row["secret_hash"])
    except Exception:
        return error_response(401, "unauthorized", "Credential not valid.")
    portal.db.execute(
        "UPDATE credentials SET last_used_at=? WHERE id=?", (auth.now_iso(), row["id"])
    )
    _record_event(portal, row["user_id"], "key_verified", "api", {"keyId": key_id})
    return ok({"authenticated": True})


# ---------------------------------------------------------------- projects


def handle_projects_list(portal: Portal, request: Request, user: dict[str, Any]) -> Response:
    rows = portal.db.query(
        "SELECT * FROM projects WHERE user_id=? ORDER BY created_at DESC", (user["id"],)
    )
    return ok(
        {
            "projects": [
                {
                    "id": r["id"],
                    "name": r["name"],
                    "slug": r["slug"],
                    "status": r["status"],
                    "createdAt": r["created_at"],
                }
                for r in rows
            ]
        }
    )


def handle_project_create(portal: Portal, request: Request, user: dict[str, Any]) -> Response:
    body = request.body_json or {}
    name = str(body.get("name", "")).strip()
    if not (0 < len(name) <= 80):
        return error_response(400, "invalid_name", "Project name must be 1-80 characters.")
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "project"
    row_id = portal.db.execute_many(
        [
            (
                "INSERT INTO projects(user_id, name, slug, status, created_at, updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (user["id"], name, slug, "active", auth.now_iso(), auth.now_iso()),
            )
        ],
        lastrow=True,
    )
    _record_event(portal, user["id"], "project_created", "web", {"name": name})
    return created({"project": {"id": row_id, "name": name, "slug": slug, "status": "active"}})


def handle_project_action(
    portal: Portal, request: Request, user: dict[str, Any], project_id: str
) -> Response:
    row = portal.db.query_one(
        "SELECT * FROM projects WHERE id=? AND user_id=?", (project_id, user["id"])
    )
    if row is None:
        return error_response(404, "not_found", "Project not found.")
    body = request.body_json or {}
    if request.method == "PATCH":
        name = str(body.get("name", "")).strip()
        if not (0 < len(name) <= 80):
            return error_response(400, "invalid_name", "Project name must be 1-80 characters.")
        portal.db.execute(
            "UPDATE projects SET name=?, updated_at=? WHERE id=?", (name, auth.now_iso(), row["id"])
        )
        _record_event(portal, user["id"], "project_renamed", "web", {"id": project_id})
    elif request.method == "POST":
        action = str(body.get("action", ""))
        if action == "archive":
            portal.db.execute(
                "UPDATE projects SET status='archived', updated_at=? WHERE id=?",
                (auth.now_iso(), row["id"]),
            )
            _record_event(portal, user["id"], "project_archived", "web", {"id": project_id})
        else:
            return error_response(400, "invalid_action", "Unknown project action.")
    else:
        return error_response(405, "method_not_allowed", "Method not allowed.")
    return ok({"ok": True})


# --------------------------------------------------------------- sessions


def handle_sessions_list(portal: Portal, request: Request, user: dict[str, Any]) -> Response:
    token = request.cookies.get(COOKIE_NAME)
    current_hash = auth.hash_secret(token) if token else ""
    rows = portal.db.query(
        "SELECT * FROM sessions WHERE user_id=? ORDER BY created_at DESC", (user["id"],)
    )
    payload = []
    for r in rows:
        item = _session_payload(r)
        item["current"] = r["token_hash"] == current_hash
        payload.append(item)
    return ok({"sessions": payload})


def handle_session_revoke(
    portal: Portal, request: Request, user: dict[str, Any], session_id: str
) -> Response:
    if not portal.limiter.allow("session_action", f"u{user['id']}"):
        return error_response(429, "rate_limited", "Too many session actions.")
    row = portal.db.query_one(
        "SELECT * FROM sessions WHERE id=? AND user_id=?", (session_id, user["id"])
    )
    if row is None:
        return error_response(404, "not_found", "Session not found.")
    portal.db.execute("UPDATE sessions SET revoked_at=? WHERE id=?", (auth.now_iso(), row["id"]))
    _record_event(portal, user["id"], "session_revoked", "web", {"id": session_id})
    return ok({"revoked": True})


def handle_signout_others(portal: Portal, request: Request, user: dict[str, Any]) -> Response:
    token = request.cookies.get(COOKIE_NAME)
    current_hash = auth.hash_secret(token) if token else ""
    portal.db.execute(
        "UPDATE sessions SET revoked_at=? WHERE user_id=? AND token_hash<>?",
        (auth.now_iso(), user["id"], current_hash),
    )
    _record_event(portal, user["id"], "sessions_revoked_others", "web")
    return ok({"revoked": True})


# ---------------------------------------------------------------- activity


def handle_activity(portal: Portal, request: Request, user: dict[str, Any]) -> Response:
    rows = portal.db.query(
        "SELECT action, actor, metadata, created_at FROM security_events WHERE user_id=? "
        "ORDER BY created_at DESC LIMIT 100",
        (user["id"],),
    )
    return ok({"events": rows})


# ---------------------------------------------------------------- settings


def handle_change_password(portal: Portal, request: Request, user: dict[str, Any]) -> Response:
    body = request.body_json or {}
    current = str(body.get("currentPassword", ""))
    new_password = str(body.get("newPassword", ""))
    if not auth.verify_password(current, user["password_hash"]):
        return error_response(403, "wrong_password", "Current password is incorrect.")
    if len(new_password) < 12 or len(new_password) > 128:
        return error_response(400, "weak_password", "Password must be 12-128 characters.")
    with portal.db.transaction():
        portal.db.execute(
            "UPDATE users SET password_hash=?, updated_at=? WHERE id=?",
            (auth.hash_password(new_password), auth.now_iso(), user["id"]),
        )
        # Phase 11: revoke all sessions (including this one) on password change
        # so an attacker with a stolen session cannot persist past the reset.
        portal.db.execute(
            "UPDATE sessions SET revoked_at=? WHERE user_id=?",
            (auth.now_iso(), user["id"]),
        )
    _record_event(portal, user["id"], "password_changed", "web")
    return ok({"changed": True})


# ---------------------------------------------------------- MFA / WebAuthn


def handle_mfa_setup(portal: Portal, request: Request, user: dict[str, Any]) -> Response:
    body = request.body_json or {}
    existing = portal.db.query_one("SELECT * FROM mfa_totp WHERE user_id=?", (user["id"],))
    if body.get("confirm"):
        code = str(body.get("code", ""))
        if existing is None or not verify_totp(existing["secret"], code):
            return error_response(401, "invalid_code", "Incorrect authentication code.")
        codes = [auth.generate_recovery_code() for _ in range(8)]
        with portal.db.transaction():
            portal.db.execute(
                "UPDATE mfa_totp SET enabled=1, verified=1 WHERE user_id=?", (user["id"],)
            )
            for c in codes:
                portal.db.execute(
                    "INSERT INTO mfa_recovery_codes(user_id, code_hash, created_at) VALUES(?,?,?)",
                    (user["id"], auth.hash_secret(c), auth.now_iso()),
                )
        _record_event(portal, user["id"], "mfa_enabled", "web")
        return created({"recoveryCodes": codes})  # shown once
    secret = existing["secret"] if existing else generate_totp_secret()
    if existing is None:
        portal.db.execute(
            "INSERT INTO mfa_totp(user_id, secret, enabled, verified, created_at) "
            "VALUES(?,?,0,0,?)",
            (user["id"], secret, auth.now_iso()),
        )
    return ok(
        {
            "secret": secret,
            "otpauth": f"otpauth://totp/GhostLink:{user['email']}?secret={secret}&issuer=GhostLink",
        }
    )


def handle_mfa_disable(portal: Portal, request: Request, user: dict[str, Any]) -> Response:
    portal.db.execute_many(
        [
            ("DELETE FROM mfa_recovery_codes WHERE user_id=?", (user["id"],)),
            ("DELETE FROM mfa_totp WHERE user_id=?", (user["id"],)),
        ]
    )
    _record_event(portal, user["id"], "mfa_disabled", "web")
    return ok({"disabled": True})


def handle_webauthn_begin(portal: Portal, request: Request, user: dict[str, Any]) -> Response:
    challenge = webauthn.new_challenge()
    expiry = portal.clock() + 300.0  # 5-minute challenge lifetime
    portal.webauthn_challenges[f"wa:{user['id']}:{challenge}"] = (challenge, expiry)
    rp_id = portal.config.webauthn_rp_id if portal.config else "localhost"
    return ok(
        {
            "challenge": challenge,
            "rpId": rp_id,
            "user": {"id": str(user["id"]), "name": user["email"]},
        }
    )


def handle_webauthn_register(portal: Portal, request: Request, user: dict[str, Any]) -> Response:
    body = request.body_json or {}
    challenge = str(body.get("challenge", ""))
    credential_id = str(body.get("credentialId", ""))
    cose = webauthn.b64url_decode(str(body.get("publicKeyCose", "")))
    stored = portal.webauthn_challenges.pop(f"wa:{user['id']}:{challenge}", None)
    # Single-use + expiry check.
    if stored is None or stored[0] != challenge or portal.clock() > stored[1]:
        return error_response(400, "invalid_challenge", "Challenge invalid or expired.")
    # Origin validation when the client supplies clientData (production).
    client_data_b64 = str(body.get("clientData", "") or "")
    if client_data_b64:
        try:
            client_data_json = webauthn.b64url_decode(client_data_b64)
        except Exception:
            return error_response(400, "invalid_client_data", "Malformed client data.")
        allowed = (
            (portal.config.webauthn_origin,)
            if portal.config and portal.config.webauthn_origin
            else ("http://localhost:5173", "http://127.0.0.1:5173")
        )
        if not webauthn.valid_origin(client_data_json, allowed):
            return error_response(400, "invalid_origin", "WebAuthn origin mismatch.")
    try:
        raw_point = webauthn.parse_cose_ec2_public_key(cose)
        webauthn.build_public_key(raw_point)  # validate
    except Exception:
        return error_response(400, "invalid_credential", "Unsupported or malformed passkey.")
    portal.db.execute(
        "INSERT OR REPLACE INTO webauthn_credentials(user_id, credential_id, public_key, "
        "algorithm, created_at) "
        "VALUES(?,?,?,?,?)",
        (user["id"], credential_id, webauthn.b64url_encode(raw_point), "ES256", auth.now_iso()),
    )
    _record_event(portal, user["id"], "passkey_registered", "web")
    return ok({"registered": True})


# ------------------------------------------------------------------- router


@dataclass
class Route:
    method: str
    pattern: str
    handler: Callable[..., Response]
    auth_required: bool = True
    # auth mode for non-session routes: "dev" = bearer access token,
    # "dev-open" = no auth (pairing/token/health), "dev-session" = session.
    auth: str = "session"


ROUTES: list[Route] = [
    Route("POST", "/api/v1/auth/signup", lambda p, r: handle_signup(p, r), auth_required=False),
    Route(
        "POST", "/api/v1/auth/verify", lambda p, r: handle_email_verify(p, r), auth_required=False
    ),
    Route("POST", "/api/v1/auth/signin", lambda p, r: handle_signin(p, r), auth_required=False),
    Route("POST", "/api/v1/auth/mfa", lambda p, r: handle_mfa_verify(p, r), auth_required=False),
    Route("POST", "/api/v1/auth/signout", handle_signout, auth_required=False),
    Route(
        "POST",
        "/api/v1/auth/password-reset/request",
        lambda p, r: handle_password_reset_request(p, r),
        auth_required=False,
    ),
    Route(
        "POST",
        "/api/v1/auth/password-reset",
        lambda p, r: handle_password_reset(p, r),
        auth_required=False,
    ),
    Route("GET", "/api/v1/dashboard", handle_dashboard, auth_required=True),
    Route("GET", "/api/v1/developer-keys", handle_credentials_list, auth_required=True),
    Route("POST", "/api/v1/developer-keys", handle_credential_create, auth_required=True),
    Route(
        "POST",
        "/api/v1/developer-keys/verify",
        lambda p, r: handle_credential_verify(p, r),
        auth_required=False,
    ),
    Route(
        "POST",
        "/api/v1/developer-keys/{key_id}/rotate",
        handle_credential_rotate,
        auth_required=True,
    ),
    Route(
        "POST",
        "/api/v1/developer-keys/{key_id}/revoke",
        handle_credential_revoke,
        auth_required=True,
    ),
    Route("GET", "/api/v1/projects", handle_projects_list, auth_required=True),
    Route("POST", "/api/v1/projects", handle_project_create, auth_required=True),
    Route("PATCH", "/api/v1/projects/{project_id}", handle_project_action, auth_required=True),
    Route("POST", "/api/v1/projects/{project_id}", handle_project_action, auth_required=True),
    Route("GET", "/api/v1/sessions", handle_sessions_list, auth_required=True),
    Route(
        "POST", "/api/v1/sessions/{session_id}/revoke", handle_session_revoke, auth_required=True
    ),
    Route("POST", "/api/v1/sessions/revoke-others", handle_signout_others, auth_required=True),
    Route("GET", "/api/v1/activity", handle_activity, auth_required=True),
    # Phase 12N developer-API platform (web, session-authenticated).
    Route("GET", "/api/v1/devapi/devices", handle_devapi_devices, auth_required=True),
    Route(
        "POST",
        "/api/v1/devapi/devices/{device_id}/revoke",
        handle_devapi_device_revoke,
        auth_required=True,
    ),
    Route("GET", "/api/v1/devapi/credentials", handle_devapi_credentials, auth_required=True),
    Route(
        "POST",
        "/api/v1/devapi/credentials/{credential_id}/revoke",
        handle_devapi_credential_revoke,
        auth_required=True,
    ),
    Route("GET", "/api/v1/devapi/activity", handle_devapi_activity, auth_required=True),
    Route("GET", "/api/v1/devapi/pairing", handle_devapi_pairing, auth_required=True),
    Route("POST", "/api/v1/security/password", handle_change_password, auth_required=True),
    Route("POST", "/api/v1/security/mfa/setup", handle_mfa_setup, auth_required=True),
    Route("POST", "/api/v1/security/mfa/disable", handle_mfa_disable, auth_required=True),
    Route("POST", "/api/v1/security/webauthn/begin", handle_webauthn_begin, auth_required=True),
    Route(
        "POST", "/api/v1/security/webauthn/register", handle_webauthn_register, auth_required=True
    ),
    # ---------------- Developer API platform (Phase 12) ----------------
    Route(
        "POST",
        "/api/v1/developer/auth/pair-begin",
        devapi_handlers.handle_pair_begin,
        auth_required=False,
        auth="dev-open",
    ),
    Route(
        "POST",
        "/api/v1/developer/auth/pair-approve",
        devapi_handlers.handle_pair_approve,
        auth_required=False,
        auth="dev-session",
    ),
    Route(
        "POST",
        "/api/v1/developer/auth/token",
        devapi_handlers.handle_token,
        auth_required=False,
        auth="dev-open",
    ),
    Route(
        "POST",
        "/api/v1/developer/auth/refresh",
        devapi_handlers.handle_refresh,
        auth_required=False,
        auth="dev-open",
    ),
    Route(
        "GET",
        "/api/v1/developer/devices",
        devapi_handlers.handle_devices_list,
        auth_required=False,
        auth="dev",
    ),
    Route(
        "POST",
        "/api/v1/developer/devices/{device_id}/revoke",
        devapi_handlers.handle_device_revoke,
        auth_required=False,
        auth="dev",
    ),
    Route(
        "GET",
        "/api/v1/developer/projects",
        devapi_handlers.handle_projects_list,
        auth_required=False,
        auth="dev",
    ),
    Route(
        "GET", "/api/v1/developer/credentials", devapi_handlers.handle_credentials_list, auth="dev"
    ),
    Route(
        "POST",
        "/api/v1/developer/credentials/{credential_id}/rotate",
        devapi_handlers.handle_credential_rotate,
        auth_required=False,
        auth="dev",
    ),
    Route(
        "POST",
        "/api/v1/developer/credentials/{credential_id}/revoke",
        devapi_handlers.handle_credential_revoke,
        auth_required=False,
        auth="dev",
    ),
    Route(
        "GET",
        "/api/v1/developer/security/activity",
        devapi_handlers.handle_activity,
        auth_required=False,
        auth="dev",
    ),
    Route("GET", "/api/v1/developer/health", devapi_handlers.handle_health, auth="dev-open"),
]


def _match(pattern: str, path: str) -> dict[str, str] | None:
    # Build a regex, treating "{name}" as a capture and everything else as
    # literal (escaped). Avoids the re.escape() pitfall that would also
    # escape underscores inside the placeholder.
    parts: list[str] = []
    names: list[str] = []
    cursor = 0
    for placeholder in re.finditer(r"\{([^}]+)\}", pattern):
        literal = pattern[cursor : placeholder.start()]
        parts.append(re.escape(literal))
        names.append(placeholder.group(1))
        parts.append("([^/]+)")
        cursor = placeholder.end()
    parts.append(re.escape(pattern[cursor:]))
    regex = "".join(parts)
    match = re.fullmatch(regex, path)
    if not match:
        return None
    return dict(zip(names, match.groups(), strict=False))


def create_wsgi_app(
    db: DatabaseBackend,
    *,
    secure_cookies: bool = False,
    emails: EmailProvider | None = None,
    clock: Callable[[], float] = time.monotonic,
    debug: bool = False,
    config: PortalConfig | None = None,
) -> Callable[[dict[str, Any], Any], list[bytes]]:
    resolved_emails = emails
    if resolved_emails is None and config is not None:
        from portal_server.emailing import get_email_provider

        resolved_emails = get_email_provider(config)
    portal = Portal(
        db,
        secure_cookies=secure_cookies,
        emails=resolved_emails,
        clock=clock,
        config=config,
    )
    apply_hsts = config is not None and config.is_production

    def wsgi_app(environ: dict[str, Any], start_response: Any) -> list[bytes]:
        request = Request(environ)
        if apply_hsts:
            # Inject HSTS for every response in production.
            environ["ghostlink.hsts"] = "1"
        # Only honour X-Forwarded-* when a trusted reverse proxy is configured.
        if config is not None and config.trusted_proxy:
            environ["ghostlink.trusted_proxy"] = "1"
        return _dispatch(portal, request, environ, start_response, debug=debug)

    return wsgi_app


def _log_request(
    portal: Portal, request: Request, status: int, started: float, request_id: str
) -> None:
    """Operational request log — structured, metadata-only, secret-safe."""
    duration_ms = (time.monotonic() - started) * 1000.0
    portal.logger.info(
        "request",
        event="request",
        request_id=request_id,
        method=request.method,
        route=request.path,
        status=status,
        duration_ms=round(duration_ms, 2),
    )


def _host_allowed(portal: Portal, request: Request) -> bool:
    """Reject requests whose ``Host`` header is not allow-listed (13H)."""
    hosts = portal.config.allowed_hosts if portal.config else ()
    if not hosts:
        return True  # not configured → not enforced (dev)
    host = str(request.header("Host") or "").strip()
    if not host:
        return False
    # Strip an optional port before comparison.
    bare = host.rsplit(":", 1)[0] if ":" in host and host.rsplit(":", 1)[1].isdigit() else host
    return bare in hosts


def _dispatch(
    portal: Portal,
    request: Request,
    environ: dict[str, Any],
    start_response: Any,
    *,
    debug: bool,
) -> list[bytes]:
    """Route a request, applying HSTS when the caller flagged production."""
    started = time.monotonic()
    request_id = validate_request_id(request.header(portal.request_id_header))
    if not _host_allowed(portal, request):
        resp = error_response(403, "forbidden", "Unknown host.")
        resp.headers[portal.request_id_header] = request_id
        with contextlib.suppress(Exception):
            _log_request(portal, request, resp.status, started, request_id)
        return app_response(resp, start_response)
    try:
        length = int(request.environ.get("CONTENT_LENGTH") or 0)
        if length > MAX_BODY_BYTES:
            resp = error_response(413, "payload_too_large", "Request body is too large.")
            resp.headers[portal.request_id_header] = request_id
            with contextlib.suppress(Exception):
                _log_request(portal, request, resp.status, started, request_id)
            return app_response(resp, start_response)
        resp = _route_request(portal, request)
    except RateLimitBackendError:
        resp = error_response(503, "rate_limit_store_unavailable", "Rate limit store unavailable.")
    except Exception as exc:
        if debug:
            raise
        resp = error_response(500, "internal", "An unexpected error occurred.")
        portal.logger.error(
            "unhandled_exception",
            event="request_error",
            request_id=request_id,
            route=request.path,
            error_class=exc.__class__.__name__,
        )
    if environ.get("ghostlink.hsts"):
        resp.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
    resp.headers[portal.request_id_header] = request_id
    with contextlib.suppress(Exception):
        _log_request(portal, request, resp.status, started, request_id)
    return app_response(resp, start_response)


def _ready_state(portal: Portal) -> tuple[bool, list[str]]:
    """Validate DB connectivity + migrations + required production config."""
    problems: list[str] = []
    try:
        row = portal.db.query_one("SELECT 1 AS ok")
        if row is None:
            problems.append("database unreachable")
    except Exception:
        problems.append("database unreachable")
    try:
        if portal.db.schema_version < 3:
            problems.append("migrations not applied")
    except Exception:
        problems.append("cannot read schema version")
    if portal.config is not None and portal.config.is_production:
        if not portal.config.allowed_hosts:
            problems.append("ALLOWED_HOSTS not configured")
        if portal.config.session_secret == "development-secret":
            problems.append("SESSION_SECRET is the insecure default")
        if not portal.config.secure_cookies:
            problems.append("PORTAL_SECURE_COOKIES is not true")
        if portal.config.email_provider in ("dev", ""):
            problems.append("EMAIL_PROVIDER is not production-ready")
    return (not problems, problems)


def handle_health_live(portal: Portal) -> Response:
    """Liveness: process is alive; no database dependency."""
    return ok({"status": "alive"})


def handle_health_ready(portal: Portal) -> Response:
    """Readiness: DB connectivity, migrations and required config all valid."""
    ready, problems = _ready_state(portal)
    if ready:
        return ok({"status": "ready", "checks": []})
    return json_response(503, {"status": "not_ready", "checks": problems})


def handle_health(portal: Portal) -> Response:
    """Safe operational summary — never exposes URLs, secrets or internals."""
    ready, problems = _ready_state(portal)
    payload: dict[str, Any] = {
        "status": "ready" if ready else "degraded",
        "deployment_version": portal.config.deployment_version if portal.config else "",
        "database_backend": portal.db.backend_name,
        "schema_version": portal.db.schema_version,
    }
    if not ready:
        payload["checks"] = problems
    return ok(payload)


def _route_request(portal: Portal, request: Request) -> Response:
    if request.path == "/health/live" and request.method == "GET":
        return handle_health_live(portal)
    if request.path == "/health/ready" and request.method == "GET":
        return handle_health_ready(portal)
    if request.path == "/health" and request.method == "GET":
        return handle_health(portal)
    if not request.path.startswith("/api/"):
        return error_response(404, "not_found", "Not found.")
    for route in ROUTES:
        if route.method != request.method:
            continue
        params = _match(route.pattern, request.path)
        if params is None:
            continue
        user: dict[str, Any] | None = None
        if route.auth_required:
            user, err = _authenticate(request, portal)
            if err is not None:
                return err
            assert user is not None
        if route.auth in ("dev", "dev-open"):
            # Bearer/open dev routes authenticate inside the handler; convert
            # a raised ScopedCredentialError (e.g. rate limit) into its
            # proper Response instead of a 500.
            try:
                return route.handler(portal, request, **params)
            except Exception as exc:
                from portal_server.devapi import ScopedCredentialError, api_error_response

                if isinstance(exc, ScopedCredentialError):
                    return api_error_response(exc)
                raise
        if route.auth == "dev-session":
            user, err = _authenticate(request, portal)
            if err is not None:
                return err
            assert user is not None
            return route.handler(portal, request, user, **params)
        if user is not None:
            resp = route.handler(portal, request, user, **params)
        else:
            resp = route.handler(portal, request)
        return resp
    return error_response(404, "not_found", "Endpoint not found.")


__all__ = ["EmailSender", "Portal", "create_wsgi_app"]
