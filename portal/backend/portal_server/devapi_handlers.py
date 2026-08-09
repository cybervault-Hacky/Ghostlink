"""Developer API handlers (Phase 12).

Routes under ``/api/v1/developer/*``. Authentication is via short-lived
bearer access tokens (and a rotating refresh flow); pairing uses short-lived
single-use codes; scopes are enforced server-side; projects are bound
server-side from the credential/token — never from a client parameter.
"""

from __future__ import annotations

import secrets
from typing import Any

from portal_server import auth
from portal_server.devapi import (
    ACCESS_TOKEN_TTL_SECONDS,
    PAIRING_CODE_TTL_SECONDS,
    REFRESH_TOKEN_TTL_SECONDS,
    ScopedCredentialError,
    new_access_token,
    new_credential_id,
    new_device_id,
    new_pairing_code,
    new_refresh_token,
    normalize_scopes,
)
from portal_server.http import Request, Response, created, ok

_PAIR_MAX_ATTEMPTS = 5
_PAIR_WINDOW_SECONDS = 300.0


def _err(message: str, *, code: str = "error", status: int = 400) -> ScopedCredentialError:
    return ScopedCredentialError(message, code=code, status=status)


def _rate_limit(portal: Any, scope: str, key: str) -> None:
    """Enforce a per-(scope, key) in-memory rate limit; raises 429 on tripping.

    Phase 12P: protects pairing, token issuance/refresh, and credential
    operations. Per-process only — documented as not distributed/global.
    """
    if not portal.limiter.allow(scope, key):
        raise _err("Rate limit exceeded. Try again later.", code="rate_limited", status=429)


def _bearer_token(request: Request) -> str:
    header = request.header("Authorization")
    if not header.startswith("Bearer "):
        raise _err("Missing bearer token.", code="unauthorized", status=401)
    return header[len("Bearer ") :].strip()


def _record_api_activity(
    portal: Any,
    *,
    user_id: int,
    device_id: Any = None,
    project_id: Any = None,
    endpoint: str,
    category: str,
    result: str,
) -> None:
    portal.db.execute(
        "INSERT INTO api_activity(user_id, device_id, project_id, endpoint, category, "
        "result, created_at) VALUES(?,?,?,?,?,?,?)",
        (user_id, device_id, project_id, endpoint, category, result, auth.now_iso()),
    )


def _scope_allowed(token: dict[str, Any], required: str) -> bool:
    scopes = frozenset(token.get("scopes", "").split())
    return required in scopes


def _resolve_access(
    portal: Any,
    request: Request,
    *,
    required_scope: str,
    endpoint: str,
    category: str,
) -> dict[str, Any]:
    """Resolve a bearer access token and enforce the required scope."""
    token = _bearer_token(request)
    row = portal.db.query_one(
        "SELECT * FROM api_tokens WHERE kind='access' AND token_hash=?",
        (auth.hash_secret(token),),
    )
    if row is None:
        _record_api_activity(
            portal, user_id=0, endpoint=endpoint, category=category, result="invalid_token"
        )
        raise _err("Invalid access token.", code="unauthorized", status=401)
    if row["revoked_at"] or row["expires_at"] < auth.now_iso():
        _record_api_activity(
            portal,
            user_id=row["user_id"],
            device_id=row["device_id"],
            project_id=row["project_id"],
            endpoint=endpoint,
            category=category,
            result="expired",
        )
        raise _err("Access token expired or revoked.", code="unauthorized", status=401)
    user = portal.db.query_one("SELECT * FROM users WHERE id=?", (row["user_id"],))
    if user is None or user["status"] != "active":
        raise _err("Account is not active.", code="forbidden", status=403)
    # Project binding: enforce server-side ownership of the bound project.
    if row["project_id"]:
        project = portal.db.query_one(
            "SELECT * FROM projects WHERE id=? AND user_id=?", (row["project_id"], user["id"])
        )
        if project is None:
            raise _err("Project not available.", code="forbidden", status=403)
    if not _scope_allowed(row, required_scope):
        _record_api_activity(
            portal,
            user_id=user["id"],
            device_id=row["device_id"],
            project_id=row["project_id"],
            endpoint=endpoint,
            category=category,
            result="scope_denied",
        )
        raise _err("Insufficient scope.", code="insufficient_scope", status=403)
    portal.db.execute(
        "UPDATE api_tokens SET last_used_at=? WHERE id=?", (auth.now_iso(), row["id"])
    )
    if row["device_id"]:
        portal.db.execute(
            "UPDATE developer_devices SET last_seen_at=? WHERE id=?",
            (auth.now_iso(), row["device_id"]),
        )
    _record_api_activity(
        portal,
        user_id=user["id"],
        device_id=row["device_id"],
        project_id=row["project_id"],
        endpoint=endpoint,
        category=category,
        result="ok",
    )
    return {
        "user": user,
        "token": row,
        "device_id": row["device_id"],
        "project_id": row["project_id"],
        "scopes": frozenset(row["scopes"].split()),
    }


# ------------------------------------------------------------- auth / tokens


def handle_pair_begin(portal: Any, request: Request) -> Response:
    _rate_limit(portal, "pair_begin", request.client_ip())
    body = request.body_json or {}
    name = str(body.get("device_name", "")).strip()[:64]
    _platform = str(body.get("platform", "termux"))[:32]
    _client_version = str(body.get("client_version", ""))[:32]
    if not name:
        return _resp(_err("Device name is required.", code="invalid_input"))
    code = new_pairing_code()
    nonce = secrets.token_urlsafe(16)
    device_id = new_device_id()
    portal.db.execute(
        "INSERT INTO pairing_codes(user_id, code_hash, nonce, device_id, status, expires_at, "
        "created_at) VALUES(NULL,?,?,?,?,?,?)",
        (
            auth.hash_secret(code),
            nonce,
            device_id,
            "pending",
            _future_iso(PAIRING_CODE_TTL_SECONDS),
            auth.now_iso(),
        ),
    )
    return ok(
        {
            "pairing_code": code,
            "nonce": nonce,
            "device_id": device_id,
            "expires_in": PAIRING_CODE_TTL_SECONDS,
        }
    )


def _future_iso(seconds: int) -> str:
    from datetime import UTC, datetime, timedelta

    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(timespec="seconds")


def handle_pair_approve(portal: Any, request: Request, user: dict[str, Any]) -> Response:
    """Approve a pending pairing (session-authenticated) and issue a scoped
    credential for a new device. Returns the credential secret **once**."""
    _rate_limit(portal, "pair_approve", f"u{user['id']}")
    body = request.body_json or {}
    code = str(body.get("pairing_code", "")).strip()
    scopes = str(body.get("scopes", "project:read device:read credential:read")).strip()
    try:
        scope_set = normalize_scopes(scopes)
    except ValueError as exc:
        return _resp(_err(str(exc), code="invalid_scope"))
    row = portal.db.query_one(
        "SELECT * FROM pairing_codes WHERE code_hash=? AND status='pending'",
        (auth.hash_secret(code),),
    )
    if row is None or row["expires_at"] < auth.now_iso():
        return _resp(_err("Pairing code invalid or expired.", code="invalid_pairing"))
    # Mark used atomically (single-use).
    portal.db.execute_many(
        [
            (
                "UPDATE pairing_codes SET status='used' WHERE id=? AND status='pending'",
                (row["id"],),
            ),
            (
                "INSERT INTO developer_devices(user_id, device_id, name, platform, client_version, "
                "status, created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    user["id"],
                    row["device_id"],
                    body.get("device_name", "device"),
                    "termux",
                    "",
                    "active",
                    auth.now_iso(),
                ),
            ),
        ]
    )
    # Issue a scoped credential bound to this user + device.
    credential_id = new_credential_id()
    from ghostlink.developer import keys as dev_keys

    _cid, secret = dev_keys.issue_credential()
    verifier, salt, digest = dev_keys.make_verifier(secret)
    portal.db.execute(
        "INSERT INTO api_credentials(user_id, device_id, name, credential_id, secret_hash, "
        "secret_salt, verifier, scopes, status, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (
            user["id"],
            None,
            "pairing",
            credential_id,
            digest,
            salt,
            verifier,
            " ".join(sorted(scope_set)),
            "active",
            auth.now_iso(),
        ),
    )
    _record_api_activity(
        portal,
        user_id=user["id"],
        endpoint="/developer/auth/pair-approve",
        category="pairing",
        result="approved",
    )
    return created(
        {
            "device_id": row["device_id"],
            "credential_id": credential_id,
            "credential_secret": secret,  # shown once
            "scopes": sorted(scope_set),
        }
    )


def handle_token(portal: Any, request: Request) -> Response:
    """Exchange a scoped credential for short-lived access + refresh tokens."""
    _rate_limit(portal, "token_issue", request.client_ip())
    body = request.body_json or {}
    credential_id = str(body.get("credential_id", ""))
    credential_secret = str(body.get("credential_secret", ""))
    if not credential_id or not credential_secret:
        return _resp(
            _err("credential_id and credential_secret are required.", code="invalid_input")
        )
    row = portal.db.query_one(
        "SELECT * FROM api_credentials WHERE credential_id=?", (credential_id,)
    )
    if row is None or row["status"] != "active":
        return _resp(_err("Credential not valid.", code="unauthorized", status=401))
    from ghostlink.developer import keys as dev_keys

    try:
        dev_keys.verify_secret(
            credential_secret, row["verifier"], row["secret_salt"], row["secret_hash"]
        )
    except Exception:
        return _resp(_err("Credential not valid.", code="unauthorized", status=401))
    user = portal.db.query_one("SELECT * FROM users WHERE id=?", (row["user_id"],))
    if user is None or user["status"] != "active":
        return _resp(_err("Account not active.", code="forbidden", status=403))
    access = new_access_token()
    refresh = new_refresh_token()
    now = auth.now_iso()
    portal.db.execute_many(
        [
            (
                "INSERT INTO api_tokens(user_id, device_id, project_id, credential_id, kind, "
                "token_hash, scopes, created_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    user["id"],
                    row["device_id"],
                    row["project_id"],
                    row["id"],
                    "access",
                    auth.hash_secret(access),
                    row["scopes"],
                    now,
                    _future_iso(ACCESS_TOKEN_TTL_SECONDS),
                ),
            ),
            (
                "INSERT INTO api_tokens(user_id, device_id, project_id, credential_id, kind, "
                "token_hash, scopes, created_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    user["id"],
                    row["device_id"],
                    row["project_id"],
                    row["id"],
                    "refresh",
                    auth.hash_secret(refresh),
                    row["scopes"],
                    now,
                    _future_iso(REFRESH_TOKEN_TTL_SECONDS),
                ),
            ),
            ("UPDATE api_credentials SET last_used_at=? WHERE id=?", (now, row["id"])),
        ]
    )
    _record_api_activity(
        portal,
        user_id=user["id"],
        device_id=row["device_id"],
        project_id=row["project_id"],
        endpoint="/developer/auth/token",
        category="auth",
        result="ok",
    )
    return ok(
        {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
            "developer_id": user["developer_id"],
            "scopes": row["scopes"].split(),
        }
    )


def handle_refresh(portal: Any, request: Request) -> Response:
    _rate_limit(portal, "token_refresh", request.client_ip())
    body = request.body_json or {}
    refresh = str(body.get("refresh_token", ""))
    row = portal.db.query_one(
        "SELECT * FROM api_tokens WHERE kind='refresh' AND token_hash=?",
        (auth.hash_secret(refresh),),
    )
    if row is None or row["revoked_at"] or row["expires_at"] < auth.now_iso():
        return _resp(_err("Refresh token invalid or expired.", code="unauthorized", status=401))
    # Rotate: revoke old refresh, issue new access + refresh.
    new_access = new_access_token()
    new_refresh = new_refresh_token()
    now = auth.now_iso()
    portal.db.execute_many(
        [
            ("UPDATE api_tokens SET revoked_at=? WHERE id=?", (now, row["id"])),
            (
                "INSERT INTO api_tokens(user_id, device_id, project_id, credential_id, kind, "
                "token_hash, scopes, created_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    row["user_id"],
                    row["device_id"],
                    row["project_id"],
                    row["credential_id"],
                    "access",
                    auth.hash_secret(new_access),
                    row["scopes"],
                    now,
                    _future_iso(ACCESS_TOKEN_TTL_SECONDS),
                ),
            ),
            (
                "INSERT INTO api_tokens(user_id, device_id, project_id, credential_id, kind, "
                "token_hash, scopes, created_at, expires_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    row["user_id"],
                    row["device_id"],
                    row["project_id"],
                    row["credential_id"],
                    "refresh",
                    auth.hash_secret(new_refresh),
                    row["scopes"],
                    now,
                    _future_iso(REFRESH_TOKEN_TTL_SECONDS),
                ),
            ),
        ]
    )
    return ok(
        {
            "access_token": new_access,
            "refresh_token": new_refresh,
            "expires_in": ACCESS_TOKEN_TTL_SECONDS,
        }
    )


# ----------------------------------------------------------- resources


def handle_devices_list(portal: Any, request: Request) -> Response:
    ctx = _resolve_access(
        portal,
        request,
        required_scope="device:read",
        endpoint="/developer/devices",
        category="device",
    )
    rows = portal.db.query(
        "SELECT * FROM developer_devices WHERE user_id=? ORDER BY created_at DESC",
        (ctx["user"]["id"],),
    )
    return ok(
        {
            "devices": [
                {
                    "device_id": r["device_id"],
                    "name": r["name"],
                    "platform": r["platform"],
                    "status": r["status"],
                    "created_at": r["created_at"],
                    "last_seen_at": r["last_seen_at"],
                }
                for r in rows
            ]
        }
    )


def handle_device_revoke(portal: Any, request: Request, device_id: str) -> Response:
    ctx = _resolve_access(
        portal,
        request,
        required_scope="device:write",
        endpoint=f"/developer/devices/{device_id}/revoke",
        category="device",
    )
    row = portal.db.query_one(
        "SELECT * FROM developer_devices WHERE device_id=? AND user_id=?",
        (device_id, ctx["user"]["id"]),
    )
    if row is None:
        return _resp(_err("Device not found.", code="not_found", status=404))
    with portal.db.transaction():
        portal.db.execute("UPDATE developer_devices SET status='revoked' WHERE id=?", (row["id"],))
        # Revoke all tokens bound to this device.
        portal.db.execute(
            "UPDATE api_tokens SET revoked_at=? WHERE device_id=?", (auth.now_iso(), row["id"])
        )
    _record_api_activity(
        portal,
        user_id=ctx["user"]["id"],
        device_id=row["id"],
        endpoint="/developer/devices/revoke",
        category="device",
        result="revoked",
    )
    return ok({"revoked": True})


def handle_projects_list(portal: Any, request: Request) -> Response:
    ctx = _resolve_access(
        portal,
        request,
        required_scope="project:read",
        endpoint="/developer/projects",
        category="project",
    )
    rows = portal.db.query(
        "SELECT * FROM projects WHERE user_id=? ORDER BY created_at DESC", (ctx["user"]["id"],)
    )
    return ok(
        {
            "projects": [
                {"id": r["id"], "name": r["name"], "slug": r["slug"], "status": r["status"]}
                for r in rows
            ]
        }
    )


def handle_credentials_list(portal: Any, request: Request) -> Response:
    ctx = _resolve_access(
        portal,
        request,
        required_scope="credential:read",
        endpoint="/developer/credentials",
        category="credential",
    )
    rows = portal.db.query(
        "SELECT * FROM api_credentials WHERE user_id=? ORDER BY created_at DESC",
        (ctx["user"]["id"],),
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
                    "revoked_at": r["revoked_at"],
                }
                for r in rows
            ]
        }
    )


def handle_credential_rotate(portal: Any, request: Request, credential_id: str) -> Response:
    ctx = _resolve_access(
        portal,
        request,
        required_scope="credential:rotate",
        endpoint=f"/developer/credentials/{credential_id}/rotate",
        category="credential",
    )
    _rate_limit(portal, "credential_rotate", f"u{ctx['user']['id']}")
    row = portal.db.query_one(
        "SELECT * FROM api_credentials WHERE credential_id=? AND user_id=?",
        (credential_id, ctx["user"]["id"]),
    )
    if row is None:
        return _resp(_err("Credential not found.", code="not_found", status=404))
    if row["status"] == "revoked":
        return _resp(_err("Credential is revoked.", code="revoked", status=400))
    from ghostlink.developer import keys as dev_keys

    _cid, secret = dev_keys.issue_credential()
    verifier, salt, digest = dev_keys.make_verifier(secret)
    new_id = new_credential_id()
    portal.db.execute_many(
        [
            (
                "UPDATE api_credentials SET status='revoked', revoked_at=?, rotated_at=? "
                "WHERE id=?",
                (auth.now_iso(), auth.now_iso(), row["id"]),
            ),
            (
                "INSERT INTO api_credentials(user_id, device_id, project_id, name, credential_id, "
                "secret_hash, secret_salt, verifier, scopes, status, created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    row["user_id"],
                    row["device_id"],
                    row["project_id"],
                    row["name"] + " (rotated)",
                    new_id,
                    digest,
                    salt,
                    verifier,
                    row["scopes"],
                    "active",
                    auth.now_iso(),
                ),
            ),
        ]
    )
    _record_api_activity(
        portal,
        user_id=ctx["user"]["id"],
        device_id=row["device_id"],
        project_id=row["project_id"],
        endpoint="/developer/credentials/rotate",
        category="credential",
        result="rotated",
    )
    return ok({"credential_id": new_id, "credential_secret": secret})


def handle_credential_revoke(portal: Any, request: Request, credential_id: str) -> Response:
    ctx = _resolve_access(
        portal,
        request,
        required_scope="credential:read",
        endpoint=f"/developer/credentials/{credential_id}/revoke",
        category="credential",
    )
    row = portal.db.query_one(
        "SELECT * FROM api_credentials WHERE credential_id=? AND user_id=?",
        (credential_id, ctx["user"]["id"]),
    )
    if row is None:
        return _resp(_err("Credential not found.", code="not_found", status=404))
    with portal.db.transaction():
        portal.db.execute(
            "UPDATE api_credentials SET status='revoked', revoked_at=? WHERE id=?",
            (auth.now_iso(), row["id"]),
        )
        portal.db.execute(
            "UPDATE api_tokens SET revoked_at=? WHERE credential_id=?", (auth.now_iso(), row["id"])
        )
    _record_api_activity(
        portal,
        user_id=ctx["user"]["id"],
        device_id=row["device_id"],
        project_id=row["project_id"],
        endpoint="/developer/credentials/revoke",
        category="credential",
        result="revoked",
    )
    return ok({"revoked": True})


def handle_activity(portal: Any, request: Request) -> Response:
    ctx = _resolve_access(
        portal,
        request,
        required_scope="security:read",
        endpoint="/developer/security/activity",
        category="security",
    )
    rows = portal.db.query(
        "SELECT endpoint, category, result, created_at FROM api_activity WHERE user_id=? "
        "ORDER BY created_at DESC LIMIT 100",
        (ctx["user"]["id"],),
    )
    return ok({"events": rows})


def handle_health(portal: Any, request: Request) -> Response:
    return ok({"status": "ok"})


def _resp(exc: ScopedCredentialError) -> Response:
    from portal_server.devapi import api_error_response

    return api_error_response(exc)
