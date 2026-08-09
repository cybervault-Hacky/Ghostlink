"""Phase 11 hardening tests: WebAuthn negatives, security headers, session
idle-timeout, and password-change session invalidation."""

from __future__ import annotations

from conftest import make_active_user, signin_and_set_csrf


def _cose_from_point(point: bytes) -> bytes:
    x = point[1:33]
    y = point[33:]
    pairs = [(1, 2), (3, 1), (-1, 1), (-2, x), (-3, y)]
    inner = b"".join(_cbor(k) + _cbor(v) for k, v in pairs)
    return _cbor_head(5, len(pairs)) + inner


def _cbor(value: object) -> bytes:
    if isinstance(value, int):
        return _cbor_head(0, value) if value >= 0 else _cbor_head(1, -1 - value)
    if isinstance(value, bytes):
        return _cbor_head(2, len(value)) + value
    raise TypeError("unsupported")


def _cbor_head(major: int, value: int) -> bytes:
    if value < 24:
        return bytes([(major << 5) | value])
    if value < 256:
        return bytes([(major << 5) | 24, value])
    if value < 65536:
        return bytes([(major << 5) | 25]) + value.to_bytes(2, "big")
    return bytes([(major << 5) | 26]) + value.to_bytes(4, "big")


def _b64(data: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _make_point() -> bytes:
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    private_key = ec.generate_private_key(ec.SECP256R1())
    return private_key.public_key().public_bytes(
        encoding=Encoding.X962, format=PublicFormat.UncompressedPoint
    )


def _begin_and_register(portal_app, *, cose: bytes | None = None, client_data: str = ""):
    make_active_user(portal_app)
    signin_and_set_csrf(portal_app)
    status, body = portal_app.client.post("/api/v1/security/webauthn/begin")
    assert status == 200
    challenge = body["challenge"]
    payload = {
        "challenge": challenge,
        "credentialId": "cred-1",
        "publicKeyCose": _b64(cose if cose is not None else _cose_from_point(_make_point())),
    }
    if client_data:
        payload["clientData"] = _b64(client_data.encode("utf-8"))
    return portal_app, challenge, payload


class TestWebAuthnHardening:
    def test_challenge_single_use_replay_rejected(self, portal_app) -> None:
        _, _challenge, payload = _begin_and_register(portal_app)
        status, _ = portal_app.client.post("/api/v1/security/webauthn/register", json=payload)
        assert status == 200
        # Replaying the same challenge must be rejected.
        status, _ = portal_app.client.post("/api/v1/security/webauthn/register", json=payload)
        assert status == 400

    def test_expired_challenge_rejected(self, clocked_portal) -> None:
        _, _challenge, payload = _begin_and_register(clocked_portal)
        clocked_portal.clock.advance(301.0)  # beyond 300s challenge TTL
        status, _ = clocked_portal.client.post("/api/v1/security/webauthn/register", json=payload)
        assert status == 400

    def test_wrong_origin_rejected(self, portal_app) -> None:
        client_data = '{"type":"webauthn.create","challenge":"x","origin":"https://evil.example","crossOrigin":false}'
        _app, challenge, payload = _begin_and_register(portal_app, client_data=client_data)
        # Challenge mismatch also fails, but the origin check is the point:
        # build clientData with the *correct* challenge but a bad origin.
        import json as _json

        cd = _json.dumps(
            {"type": "webauthn.create", "challenge": challenge, "origin": "https://evil.example"}
        )
        payload["clientData"] = _b64(cd.encode("utf-8"))
        status, _ = portal_app.client.post("/api/v1/security/webauthn/register", json=payload)
        assert status == 400

    def test_malformed_cose_rejected(self, portal_app) -> None:
        _app, _challenge, payload = _begin_and_register(portal_app, cose=b"\xa0garbage")
        status, _ = portal_app.client.post("/api/v1/security/webauthn/register", json=payload)
        assert status == 400

    def test_unknown_challenge_rejected(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        status, _ = portal_app.client.post(
            "/api/v1/security/webauthn/register",
            json={
                "challenge": "forged-challenge",
                "credentialId": "x",
                "publicKeyCose": _b64(_cose_from_point(_make_point())),
            },
        )
        assert status == 400


class TestSecurityHeaders:
    def _headers(self, portal_app, path: str = "/health") -> dict:
        portal_app.client.get(path)
        # TestClient doesn't retain headers; inspect via a direct request.
        return (
            portal_app.app(  # type: ignore[call-overload]
                {
                    "REQUEST_METHOD": "GET",
                    "PATH_INFO": path,
                    "QUERY_STRING": "",
                    "REMOTE_ADDR": "127.0.0.1",
                    "wsgi.input": __import__("io").BytesIO(b""),
                    "CONTENT_LENGTH": "0",
                },
                lambda *a, **k: None,
            )
            and {}
        )

    def test_csp_and_headers_present(self, portal_app) -> None:
        captured: dict = {}

        def start_response(status: str, headers: list) -> None:
            captured["headers"] = dict(headers)

        portal_app.app(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/health",
                "QUERY_STRING": "",
                "REMOTE_ADDR": "127.0.0.1",
                "wsgi.input": __import__("io").BytesIO(b""),
                "CONTENT_LENGTH": "0",
            },
            start_response,
        )
        headers = captured.get("headers", {})
        assert "Content-Security-Policy" in headers
        assert "X-Content-Type-Options" in headers
        assert "Referrer-Policy" in headers
        assert "X-Frame-Options" in headers
        assert "Permissions-Policy" in headers

    def test_hsts_only_in_production(self, tmp_path) -> None:
        from portal_server.config import load_config
        from portal_server.db import Database

        cfg = load_config(
            {
                "APP_ENV": "production",
                "DATABASE_URL": "portal.db",
                "SESSION_SECRET": "a-long-random-secret-123",
                "EMAIL_PROVIDER": "smtp",
                "EMAIL_SMTP_HOST": "smtp.example.com",
                "PORTAL_SECURE_COOKIES": "true",
                "WEBAUTHN_RP_ID": "portal.example.com",
                "WEBAUTHN_ORIGIN": "https://portal.example.com",
                "ALLOWED_HOSTS": "portal.example.com",
            }
        )
        app = __import__("portal_server.app", fromlist=["create_wsgi_app"]).create_wsgi_app(
            Database(tmp_path / "p.db"), config=cfg
        )
        captured: dict = {}

        def start_response(status: str, headers: list) -> None:
            captured["headers"] = dict(headers)

        app(
            {
                "REQUEST_METHOD": "GET",
                "PATH_INFO": "/health",
                "QUERY_STRING": "",
                "REMOTE_ADDR": "127.0.0.1",
                "HTTP_HOST": "portal.example.com",
                "wsgi.input": __import__("io").BytesIO(b""),
                "CONTENT_LENGTH": "0",
            },
            start_response,
        )
        assert "Strict-Transport-Security" in captured.get("headers", {})


class TestSessionHardening:
    def test_idle_timeout_revokes_session(self, clocked_portal) -> None:
        make_active_user(clocked_portal)
        signin_and_set_csrf(clocked_portal)
        status, _ = clocked_portal.client.get("/api/v1/dashboard")
        assert status == 200
        # Simulate an idle session by writing an old last_active_at directly.
        from datetime import UTC, datetime, timedelta

        stale = (datetime.now(UTC) - timedelta(minutes=40)).isoformat(timespec="seconds")
        clocked_portal.db.execute("UPDATE sessions SET last_active_at=? WHERE user_id=1", (stale,))
        status, _ = clocked_portal.client.get("/api/v1/dashboard")
        assert status == 401  # idle timeout exceeded

    def test_password_change_revokes_sessions(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        status, _ = portal_app.client.post(
            "/api/v1/security/password",
            json={
                "currentPassword": "super-secure-pass-123",
                "newPassword": "new-secure-pass-456",
            },
        )
        assert status == 200
        # The session that changed the password is now revoked.
        status, _ = portal_app.client.get("/api/v1/dashboard")
        assert status == 401
