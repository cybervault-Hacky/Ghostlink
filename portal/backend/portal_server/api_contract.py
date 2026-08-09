"""Machine-readable API contract (Phase 15L).

A compact, machine-readable description of the stable public surface of the
Developer API v1. Route handlers must keep these endpoint paths, HTTP methods,
response envelope shapes, and error codes stable so existing Phase 12+ CLI
clients continue to work. ``get_contract()`` returns the contract; a test
asserts the live router still exposes every endpoint.
"""

from __future__ import annotations

from typing import Any

# Stable public endpoints (Phase 12 onward). `path_params` lists the URL
# placeholders; `scopes` lists the required developer scope (or null for
# auth-token / health endpoints).
PUBLIC_ENDPOINTS: list[dict[str, Any]] = [
    {"path": "/api/v1/developer/auth/pair-begin", "method": "POST", "scopes": None},
    {"path": "/api/v1/developer/auth/pair-approve", "method": "POST", "scopes": None},
    {"path": "/api/v1/developer/auth/token", "method": "POST", "scopes": None},
    {"path": "/api/v1/developer/auth/refresh", "method": "POST", "scopes": None},
    {"path": "/api/v1/developer/devices", "method": "GET", "scopes": ["device:read"]},
    {
        "path": "/api/v1/developer/devices/{device_id}/revoke",
        "method": "POST",
        "scopes": ["device:write"],
    },
    {"path": "/api/v1/developer/projects", "method": "GET", "scopes": ["project:read"]},
    {"path": "/api/v1/developer/credentials", "method": "GET", "scopes": ["credential:read"]},
    {
        "path": "/api/v1/developer/credentials/{credential_id}/rotate",
        "method": "POST",
        "scopes": ["credential:rotate"],
    },
    {
        "path": "/api/v1/developer/credentials/{credential_id}/revoke",
        "method": "POST",
        "scopes": ["credential:read"],
    },
    {"path": "/api/v1/developer/security/activity", "method": "GET", "scopes": ["security:read"]},
    {"path": "/api/v1/developer/health", "method": "GET", "scopes": None},
]

# Stable error-code vocabulary (clients key off these).
ERROR_CODES = (
    "invalid_credentials",
    "rate_limited",
    "unauthorized",
    "forbidden",
    "not_found",
    "invalid_token",
    "revoked",
    "expired",
    "payload_too_large",
    "rate_limit_store_unavailable",
    "internal",
)


def get_contract() -> dict[str, Any]:
    return {
        "name": "ghostlink-developer-api",
        "version": "v1",
        "auth": "Authorization: Bearer <access_token>",
        "envelope": {"ok": "…", "error": {"code": "…", "message": "…"}},
        "error_codes": list(ERROR_CODES),
        "endpoints": PUBLIC_ENDPOINTS,
    }


__all__ = ["ERROR_CODES", "PUBLIC_ENDPOINTS", "get_contract"]
