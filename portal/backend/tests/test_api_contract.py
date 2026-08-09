"""Phase 15L — API contract stability tests.

Verifies the live router still exposes every stable public developer endpoint
(with the correct method and auth mode) so Phase 12+ CLI clients continue to
work, and that the machine-readable contract is consistent.
"""

from __future__ import annotations

from portal_server.api_contract import PUBLIC_ENDPOINTS, get_contract
from portal_server.app import ROUTES


def _route_for(path: str, method: str):
    # Normalize: replace {param} with any capture and match against router.
    import re

    for route in ROUTES:
        if route.method != method:
            continue
        parts: list[str] = []
        cursor = 0
        for ph in re.finditer(r"\{([^}]+)\}", route.pattern):
            literal = route.pattern[cursor : ph.start()]
            parts.append(re.escape(literal))
            parts.append("[^/]+")
            cursor = ph.end()
        parts.append(re.escape(route.pattern[cursor:]))
        if re.fullmatch("".join(parts), path):
            return route
    return None


def test_all_public_endpoints_are_registered() -> None:
    for ep in PUBLIC_ENDPOINTS:
        route = _route_for(ep["path"], ep["method"])
        assert route is not None, f"missing route for {ep['method']} {ep['path']}"


def test_public_endpoints_use_dev_auth_modes() -> None:
    for ep in PUBLIC_ENDPOINTS:
        route = _route_for(ep["path"], ep["method"])
        assert route is not None
        # Scoped endpoints must use the dev bearer auth mode; token/health
        # endpoints use dev-open; pairing-approve uses dev-session.
        if ep["scopes"] is not None:
            assert route.auth in ("dev", "dev-session"), ep["path"]
        else:
            assert route.auth in ("dev-open", "dev-session"), ep["path"]


def test_contract_envelope_and_error_codes() -> None:
    contract = get_contract()
    assert contract["name"] == "ghostlink-developer-api"
    assert contract["version"] == "v1"
    assert "error" in contract["envelope"]
    for code in (
        "invalid_credentials",
        "rate_limited",
        "unauthorized",
        "forbidden",
        "not_found",
        "internal",
        "payload_too_large",
    ):
        assert code in contract["error_codes"]


def test_error_envelope_shape_is_stable() -> None:
    # A real error response must carry the {"error":{"code","message"}} shape.
    from portal_server.http import error_response

    resp = error_response(401, "unauthorized", "Not signed in.")
    body = resp.body
    assert set(body.keys()) == {"error"}
    assert set(body["error"].keys()) == {"code", "message"}
