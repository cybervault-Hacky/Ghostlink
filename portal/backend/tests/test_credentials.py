"""Developer credential lifecycle + authorization tests (Phase 10B)."""

from __future__ import annotations

from conftest import make_active_user, signin_and_set_csrf


def _create_key(portal_app, name: str = "prod-key") -> dict:
    status, body = portal_app.client.post("/api/v1/developer-keys", json={"name": name})
    assert status == 201, body
    return body["credential"]


class TestCredentialLifecycle:
    def test_create_shows_secret_once(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        cred = _create_key(portal_app)
        assert cred["secret"].startswith("gl_dev_dk_")
        # secret never persisted in plaintext
        rows = portal_app.db.query("SELECT * FROM credentials WHERE key_id=?", (cred["keyId"],))
        assert rows and "gl_dev_" not in rows[0]["secret_hash"]
        # listing never returns the secret
        status, body = portal_app.client.get("/api/v1/developer-keys")
        assert status == 200
        assert all("secret" not in c for c in body["credentials"])

    def test_create_unique_key_ids(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        ids = {_create_key(portal_app, f"k{i}")["keyId"] for i in range(5)}
        assert len(ids) == 5

    def test_verify_valid_credential(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        cred = _create_key(portal_app)
        status, body = portal_app.client.post(
            "/api/v1/developer-keys/verify", json={"credential": cred["secret"]}
        )
        assert status == 200 and body.get("authenticated") is True

    def test_verify_wrong_credential(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        cred = _create_key(portal_app)
        mutated = cred["secret"][:-1] + ("A" if cred["secret"][-1] != "A" else "B")
        status, _ = portal_app.client.post(
            "/api/v1/developer-keys/verify", json={"credential": mutated}
        )
        assert status == 401

    def test_revoked_credential_cannot_authenticate(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        cred = _create_key(portal_app)
        status, _ = portal_app.client.post(f"/api/v1/developer-keys/{cred['keyId']}/revoke")
        assert status == 200
        status, _ = portal_app.client.post(
            "/api/v1/developer-keys/verify", json={"credential": cred["secret"]}
        )
        assert status == 401

    def test_rotate_invalidates_old(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        old = _create_key(portal_app, "old")
        status, body = portal_app.client.post(f"/api/v1/developer-keys/{old['keyId']}/rotate")
        assert status == 200
        new_secret = body["credential"]["secret"]
        new_key = body["credential"]["keyId"]
        assert new_key != old["keyId"]
        # new works, old revoked
        assert (
            portal_app.client.post(
                "/api/v1/developer-keys/verify", json={"credential": new_secret}
            )[0]
            == 200
        )
        assert (
            portal_app.client.post(
                "/api/v1/developer-keys/verify", json={"credential": old["secret"]}
            )[0]
            == 401
        )


class TestAuthorization:
    def test_cannot_access_another_users_credential(self, portal_app, tmp_path) -> None:
        make_active_user(portal_app, email="alice@x.com")
        signin_and_set_csrf(portal_app, email="alice@x.com")
        cred = _create_key(portal_app)
        portal_app.client.post("/api/v1/auth/signout")
        portal_app.client.cookies.clear()
        portal_app.client.csrf_token = None
        # Bob signs in.
        make_active_user(portal_app, email="bob@x.com")
        signin_and_set_csrf(portal_app, email="bob@x.com")
        status, _ = portal_app.client.post(f"/api/v1/developer-keys/{cred['keyId']}/revoke")
        assert status == 404  # not found (owner-scoped)

    def test_verify_unknown_key_generic_401(self, portal_app) -> None:
        # A well-formed but never-issued credential returns a generic 401,
        # never 404, so the public verifier does not reveal key existence.
        _k, unknown = __import__(
            "ghostlink.developer.keys", fromlist=["issue_credential"]
        ).issue_credential()
        status, body = portal_app.client.post(
            "/api/v1/developer-keys/verify", json={"credential": unknown}
        )
        assert status == 401
        assert body["error"]["code"] == "unauthorized"
