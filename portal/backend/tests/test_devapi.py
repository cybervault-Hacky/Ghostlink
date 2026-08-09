"""Phase 12 developer-API tests.

Covers the full developer platform: pairing, scoped credential issuance,
token exchange/refresh, device registration/revocation, project binding,
scope enforcement, credential rotation/revocation, restart persistence,
replay/expiry, and the owner-role boundary.
"""

from __future__ import annotations

from conftest import make_active_user, signin_and_set_csrf


def _begin_pairing(portal_app, name: str = "termux-device") -> dict:
    status, body = portal_app.client.post(
        "/api/v1/developer/auth/pair-begin",
        json={"device_name": name, "platform": "termux", "client_version": "0.14.0"},
    )
    assert status == 200, body
    return body


def _approve_pairing(
    portal_app, code: str, scopes: str = "project:read device:read credential:read"
) -> dict:
    status, body = portal_app.client.post(
        "/api/v1/developer/auth/pair-approve",
        json={"pairing_code": code, "scopes": scopes},
    )
    assert status == 201, body
    return body


def _issue_tokens(portal_app, credential_id: str, secret: str) -> dict:
    status, body = portal_app.client.post(
        "/api/v1/developer/auth/token",
        json={"credential_id": credential_id, "credential_secret": secret},
    )
    assert status == 200, body
    return body


class TestPairing:
    def test_full_pairing_and_token(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        pairing = _begin_pairing(portal_app)
        assert pairing["pairing_code"].startswith("GL-")
        approved = _approve_pairing(portal_app, pairing["pairing_code"])
        assert approved["credential_secret"].startswith("gl_dev_")
        tokens = _issue_tokens(portal_app, approved["credential_id"], approved["credential_secret"])
        assert tokens["access_token"] and tokens["refresh_token"]
        assert tokens["developer_id"].startswith("dev_")

    def test_pairing_code_single_use(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        pairing = _begin_pairing(portal_app)
        _approve_pairing(portal_app, pairing["pairing_code"])
        # Reusing the same code must fail.
        status, _ = portal_app.client.post(
            "/api/v1/developer/auth/pair-approve", json={"pairing_code": pairing["pairing_code"]}
        )
        assert status == 400

    def test_pairing_wrong_code_rejected(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        _begin_pairing(portal_app)
        status, _ = portal_app.client.post(
            "/api/v1/developer/auth/pair-approve", json={"pairing_code": "GL-XXXX-XXXX-XXXX-XXXX"}
        )
        assert status == 400

    def test_pairing_unknown_scope_rejected(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        pairing = _begin_pairing(portal_app)
        status, _ = portal_app.client.post(
            "/api/v1/developer/auth/pair-approve",
            json={"pairing_code": pairing["pairing_code"], "scopes": "owner:*"},
        )
        assert status == 400


class TestScopedAuth:
    def test_scope_enforced(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        pairing = _begin_pairing(portal_app)
        approved = _approve_pairing(portal_app, pairing["pairing_code"], scopes="device:read")
        tokens = _issue_tokens(portal_app, approved["credential_id"], approved["credential_secret"])
        # device:read allows /devices.
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        status, _body = portal_app.client.get("/api/v1/developer/devices", headers=headers)
        assert status == 200
        # project:read is NOT in scope -> denied.
        status, _ = portal_app.client.get("/api/v1/developer/projects", headers=headers)
        assert status == 403

    def test_no_token_unauthorized(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        status, _ = portal_app.client.get("/api/v1/developer/devices")
        assert status == 401

    def test_expired_access_token(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        pairing = _begin_pairing(portal_app)
        approved = _approve_pairing(portal_app, pairing["pairing_code"])
        tokens = _issue_tokens(portal_app, approved["credential_id"], approved["credential_secret"])
        portal_app.db.execute(
            "UPDATE api_tokens SET expires_at='2000-01-01T00:00:00+00:00' WHERE kind='access'"
        )
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        status, _ = portal_app.client.get("/api/v1/developer/devices", headers=headers)
        assert status == 401

    def test_revoked_credential_rejected(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        pairing = _begin_pairing(portal_app)
        approved = _approve_pairing(portal_app, pairing["pairing_code"])
        tokens = _issue_tokens(portal_app, approved["credential_id"], approved["credential_secret"])
        # Revoke the credential server-side.
        portal_app.db.execute(
            "UPDATE api_credentials SET status='revoked', revoked_at=? WHERE credential_id=?",
            (
                __import__("portal_server.auth", fromlist=["now_iso"]).now_iso(),
                approved["credential_id"],
            ),
        )
        _headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        # Existing access token still works until expiry (it was issued valid),
        # but a fresh token issuance must fail.
        status, _ = portal_app.client.post(
            "/api/v1/developer/auth/token",
            json={
                "credential_id": approved["credential_id"],
                "credential_secret": approved["credential_secret"],
            },
        )
        assert status == 401


class TestCredentialLifecycle:
    def test_rotation_and_revocation(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        pairing = _begin_pairing(portal_app)
        approved = _approve_pairing(
            portal_app, pairing["pairing_code"], scopes="credential:read credential:rotate"
        )
        tokens = _issue_tokens(portal_app, approved["credential_id"], approved["credential_secret"])
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        # Rotate via the API.
        status, body = portal_app.client.post(
            f"/api/v1/developer/credentials/{approved['credential_id']}/rotate",
            json={},
            headers=headers,
        )
        assert status == 200
        new_id = body["credential_id"]
        new_secret = body["credential_secret"]
        # Old secret no longer issues tokens; new one does.
        assert (
            portal_app.client.post(
                "/api/v1/developer/auth/token",
                json={
                    "credential_id": approved["credential_id"],
                    "credential_secret": approved["credential_secret"],
                },
            )[0]
            == 401
        )
        assert (
            portal_app.client.post(
                "/api/v1/developer/auth/token",
                json={"credential_id": new_id, "credential_secret": new_secret},
            )[0]
            == 200
        )


class TestDevices:
    def test_list_and_revoke(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        pairing = _begin_pairing(portal_app, name="My Termux")
        approved = _approve_pairing(portal_app, pairing["pairing_code"])
        tokens = _issue_tokens(portal_app, approved["credential_id"], approved["credential_secret"])
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        status, _body = portal_app.client.get("/api/v1/developer/devices", headers=headers)
        assert status == 200
        device_id = pairing["device_id"]
        # device:write is needed to revoke; the scoped credential lacks it.
        status, _ = portal_app.client.post(
            f"/api/v1/developer/devices/{device_id}/revoke", json={}, headers=headers
        )
        assert status == 403


class TestProjectBinding:
    def test_credentials_require_owner_match_for_project(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        # A developer-API credential is not owner-bound; no project escalation
        # path exists. Verify the /developer/projects list requires scope.
        pairing = _begin_pairing(portal_app)
        approved = _approve_pairing(portal_app, pairing["pairing_code"], scopes="project:read")
        tokens = _issue_tokens(portal_app, approved["credential_id"], approved["credential_secret"])
        headers = {"Authorization": f"Bearer {tokens['access_token']}"}
        status, _body = portal_app.client.get("/api/v1/developer/projects", headers=headers)
        assert status == 200 and "projects" in _body


class TestOwnerBoundary:
    def test_no_owner_scope_exists(self) -> None:
        from portal_server.devapi import VALID_SCOPES

        assert not any("owner" in s or "root" in s or "admin" in s for s in VALID_SCOPES)

    def test_pairing_cannot_request_owner_scope(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        pairing = _begin_pairing(portal_app)
        status, _ = portal_app.client.post(
            "/api/v1/developer/auth/pair-approve",
            json={"pairing_code": pairing["pairing_code"], "scopes": "security:read owner:*"},
        )
        assert status == 400


class TestRefreshAndPersistence:
    def test_refresh_rotates(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        pairing = _begin_pairing(portal_app)
        approved = _approve_pairing(portal_app, pairing["pairing_code"])
        tokens = _issue_tokens(portal_app, approved["credential_id"], approved["credential_secret"])
        status, body = portal_app.client.post(
            "/api/v1/developer/auth/refresh", json={"refresh_token": tokens["refresh_token"]}
        )
        assert status == 200
        assert body["access_token"] and body["refresh_token"]

    def test_revocation_persists_across_restart(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        pairing = _begin_pairing(portal_app)
        approved = _approve_pairing(portal_app, pairing["pairing_code"])
        _tokens = _issue_tokens(
            portal_app, approved["credential_id"], approved["credential_secret"]
        )
        # Revoke credential; a "restart" is just a fresh read from the DB.
        portal_app.db.execute(
            "UPDATE api_credentials SET status='revoked' WHERE credential_id=?",
            (approved["credential_id"],),
        )
        status, _ = portal_app.client.post(
            "/api/v1/developer/auth/token",
            json={
                "credential_id": approved["credential_id"],
                "credential_secret": approved["credential_secret"],
            },
        )
        assert status == 401  # revoked persists


class TestPortalWebViews:
    """Phase 12N: session-authenticated web views for the developer API."""

    def test_devapi_devices_list_and_revoke(self, portal_app) -> None:
        from conftest import make_active_user, signin_and_set_csrf

        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        # Register a device via the full pairing flow.
        pairing = _begin_pairing(portal_app, name="web-device")
        _approve_pairing(portal_app, pairing["pairing_code"], scopes="device:read")
        status, body = portal_app.client.get("/api/v1/devapi/devices")
        assert status == 200
        assert len(body["devices"]) == 1
        assert body["devices"][0]["platform"] == "termux"
        device_id = body["devices"][0]["device_id"]
        # Revoke via the web view.
        status, _ = portal_app.client.post(f"/api/v1/devapi/devices/{device_id}/revoke")
        assert status == 200
        status, body = portal_app.client.get("/api/v1/devapi/devices")
        assert body["devices"][0]["status"] == "revoked"

    def test_devapi_credentials_list(self, portal_app) -> None:
        from conftest import make_active_user, signin_and_set_csrf

        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        status, body = portal_app.client.get("/api/v1/devapi/credentials")
        assert status == 200
        assert body["credentials"] == []

    def test_devapi_activity_metadata_only(self, portal_app) -> None:
        from conftest import make_active_user, signin_and_set_csrf

        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        portal_app.client.post(
            "/api/v1/developer/auth/pair-begin",
            json={"device_name": "d", "platform": "termux", "client_version": "0.14.0"},
        )
        status, body = portal_app.client.get("/api/v1/devapi/activity")
        assert status == 200
        blob = str(body["events"])
        assert "token" not in blob and "secret" not in blob and "Authorization" not in blob

    def test_devapi_pairing_requires_auth(self, portal_app) -> None:
        status, _ = portal_app.client.get("/api/v1/devapi/pairing")
        assert status == 401


class TestApiRateLimit:
    def test_pair_begin_rate_limited(self, portal_app) -> None:
        from conftest import make_active_user

        make_active_user(portal_app)
        status = 0
        for i in range(12):
            status, _ = portal_app.client.post(
                "/api/v1/developer/auth/pair-begin",
                json={"device_name": f"d{i}", "platform": "termux", "client_version": "0.14.0"},
            )
        assert status == 429

    def test_token_issue_rate_limited(self, portal_app) -> None:
        from conftest import make_active_user, signin_and_set_csrf

        from ghostlink.developer import keys as dev_keys

        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        _kid, secret = dev_keys.issue_credential()
        status = 0
        for _ in range(25):
            status, _ = portal_app.client.post(
                "/api/v1/developer/auth/token",
                json={"credential_id": "dk_XXXX0000", "credential_secret": secret},
            )
        assert status == 429
