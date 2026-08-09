"""Projects, sessions, activity, settings, MFA, and WebAuthn tests (Phase 10B)."""

from __future__ import annotations

from conftest import make_active_user, signin_and_set_csrf


class TestDashboard:
    def test_dashboard_returns_counts(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        status, body = portal_app.client.get("/api/v1/dashboard")
        assert status == 200
        assert "counts" in body and body["user"]["email"] == "dev@example.com"


class TestProjects:
    def test_create_list_rename_archive(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        status, body = portal_app.client.post("/api/v1/projects", json={"name": "My App"})
        assert status == 201
        pid = body["project"]["id"]
        status, body = portal_app.client.get("/api/v1/projects")
        assert status == 200 and len(body["projects"]) == 1
        status, _ = portal_app.client.patch(f"/api/v1/projects/{pid}", json={"name": "Renamed App"})
        assert status == 200
        status, _ = portal_app.client.post(f"/api/v1/projects/{pid}", json={"action": "archive"})
        assert status == 200

    def test_cannot_access_others_project(self, portal_app) -> None:
        make_active_user(portal_app, email="p1@x.com")
        signin_and_set_csrf(portal_app, email="p1@x.com")
        _, body = portal_app.client.post("/api/v1/projects", json={"name": "Secret"})
        pid = body["project"]["id"]
        portal_app.client.post("/api/v1/auth/signout")
        portal_app.client.cookies.clear()
        portal_app.client.csrf_token = None
        make_active_user(portal_app, email="p2@x.com")
        signin_and_set_csrf(portal_app, email="p2@x.com")
        status, _ = portal_app.client.post(f"/api/v1/projects/{pid}", json={"action": "archive"})
        assert status == 404


class TestSessions:
    def test_list_and_revoke_session(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        status, body = portal_app.client.get("/api/v1/sessions")
        assert status == 200
        assert len(body["sessions"]) == 1
        current = body["sessions"][0]
        assert current["current"] is True
        status, _ = portal_app.client.post(f"/api/v1/sessions/{current['id']}/revoke")
        assert status == 200
        # current session now invalid
        status, _ = portal_app.client.get("/api/v1/dashboard")
        assert status == 401

    def test_revoke_others(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        # Create a second session directly.
        portal_app.db.execute(
            "INSERT INTO sessions(user_id, token_hash, created_at, expires_at, "
            "last_active_at, csrf_token) "
            "VALUES(1,'h','t','t','t','c')"
        )
        status, _ = portal_app.client.post("/api/v1/sessions/revoke-others")
        assert status == 200
        # The other session is revoked; the current session stays active.
        current_hash = portal_app.db.query_one(
            "SELECT token_hash FROM sessions WHERE user_id=1 AND revoked_at IS NOT NULL"
        )
        assert current_hash is not None
        active = portal_app.db.query(
            "SELECT * FROM sessions WHERE user_id=1 AND revoked_at IS NULL"
        )
        assert len(active) == 1


class TestActivity:
    def test_activity_records_metadata_only(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        _create(portal_app)
        status, body = portal_app.client.get("/api/v1/activity")
        assert status == 200
        actions = [e["action"] for e in body["events"]]
        assert "signup" in actions and "key_created" in actions
        # No secret in any activity metadata.
        blob = str(body["events"])
        assert "gl_dev_" not in blob


def _create(portal_app) -> None:
    portal_app.client.post("/api/v1/developer-keys", json={"name": "k"})


class TestSettings:
    def test_change_password(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        status, _ = portal_app.client.post(
            "/api/v1/security/password",
            json={"currentPassword": "super-secure-pass-123", "newPassword": "new-secure-pass-456"},
        )
        assert status == 200
        # old session's password now changed; old password rejected
        status, _ = portal_app.client.post(
            "/api/v1/auth/signin",
            json={"email": "dev@example.com", "password": "super-secure-pass-123"},
        )
        assert status == 401


class TestMFA:
    def test_totp_enable_and_verify(self, portal_app) -> None:
        from portal_server.mfa import current_totp

        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        status, body = portal_app.client.post("/api/v1/security/mfa/setup")
        assert status == 200
        secret = body["secret"]
        code = current_totp(secret)
        status, body = portal_app.client.post(
            "/api/v1/security/mfa/setup", json={"confirm": True, "code": code}
        )
        assert status == 201
        assert len(body["recoveryCodes"]) == 8
        # sign-in now requires MFA
        portal_app.client.post("/api/v1/auth/signout")
        portal_app.client.cookies.clear()
        portal_app.client.csrf_token = None
        status, body = portal_app.client.post(
            "/api/v1/auth/signin",
            json={"email": "dev@example.com", "password": "super-secure-pass-123"},
        )
        assert status == 200 and body.get("mfaRequired") is True
        preauth = body["preauth"]
        status, _ = portal_app.client.post(
            "/api/v1/auth/mfa", json={"preauth": preauth, "code": current_totp(secret)}
        )
        assert status == 200

    def test_totp_wrong_code_rejected(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        portal_app.client.post("/api/v1/security/mfa/setup")
        status, _ = portal_app.client.post(
            "/api/v1/security/mfa/setup", json={"confirm": True, "code": "000000"}
        )
        assert status == 401


class TestWebAuthn:
    def test_register_es256_credential(self, portal_app) -> None:

        from cryptography.hazmat.primitives.asymmetric import ec

        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        status, body = portal_app.client.post("/api/v1/security/webauthn/begin")
        assert status == 200
        challenge = body["challenge"]
        # Build a real ES256 public key and COSE encoding.
        private_key = ec.generate_private_key(ec.SECP256R1())
        point = private_key.public_key().public_bytes(
            encoding=__import__(
                "cryptography.hazmat.primitives.serialization", fromlist=["Encoding"]
            ).Encoding.X962,
            format=__import__(
                "cryptography.hazmat.primitives.serialization", fromlist=["PublicFormat"]
            ).PublicFormat.UncompressedPoint,
        )
        # COSE_Key map {1:2, 3:1, -1:1, -2:x, -3:y}
        x = point[1:33]
        y = point[33:]
        cose = _cbor_cose_key(x, y)
        cred_id = "test-credential-id"
        status, body = portal_app.client.post(
            "/api/v1/security/webauthn/register",
            json={"challenge": challenge, "credentialId": cred_id, "publicKeyCose": _b64(cose)},
        )
        assert status == 200 and body.get("registered") is True
        row = portal_app.db.query_one(
            "SELECT * FROM webauthn_credentials WHERE credential_id=?", (cred_id,)
        )
        assert row is not None

    def test_register_rejects_unknown_challenge(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        status, _ = portal_app.client.post(
            "/api/v1/security/webauthn/register",
            json={"challenge": "forged", "credentialId": "x", "publicKeyCose": _b64(b"\xa0")},
        )
        assert status == 400


def _b64(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _cbor_cose_key(x: bytes, y: bytes) -> bytes:
    """Minimal CBOR encoding of a COSE_Key EC2 P-256 map."""
    pairs = [(1, 2), (3, 1), (-1, 1), (-2, x), (-3, y)]
    inner = b"".join(_cbor(k) + _cbor(v) for k, v in pairs)
    return _cbor_head(5, len(pairs)) + inner


def _cbor_head(major: int, value: int) -> bytes:
    if value < 24:
        return bytes([(major << 5) | value])
    if value < 256:
        return bytes([(major << 5) | 24, value])
    if value < 65536:
        return bytes([(major << 5) | 25]) + value.to_bytes(2, "big")
    return bytes([(major << 5) | 26]) + value.to_bytes(4, "big")


def _cbor(value: object) -> bytes:
    if isinstance(value, int):
        if value >= 0:
            return _cbor_head(0, value)
        return _cbor_head(1, -1 - value)
    if isinstance(value, bytes):
        return _cbor_head(2, len(value)) + value
    raise TypeError("unsupported cbor value")
