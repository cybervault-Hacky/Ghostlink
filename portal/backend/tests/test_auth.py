"""Authentication, sessions, tokens, and rate-limit tests (Phase 10B)."""

from __future__ import annotations

from conftest import make_active_user, signin_and_set_csrf
from portal_server import auth


class TestPasswordHashing:
    def test_hash_is_salted_and_versioned(self) -> None:
        v = auth.hash_password("correct horse battery staple")
        assert v.startswith("pbkdf2_sha256$")
        assert auth.verify_password("correct horse battery staple", v)
        assert not auth.verify_password("wrong password", v)
        # Same password twice produces different verifiers (unique salt).
        assert auth.hash_password("pw") != auth.hash_password("pw")

    def test_malformed_verifier_fails(self) -> None:
        assert not auth.verify_password("x", "not-a-verifier")
        assert not auth.verify_password("x", "pbkdf2_sha256$bad")


class TestSignupAndVerify:
    def test_signup_creates_pending_account(self, portal_app) -> None:
        status, body = portal_app.client.post(
            "/api/v1/auth/signup", json={"email": "a@b.com", "password": "super-secure-pass-123"}
        )
        assert status == 201
        assert body["user"]["status"] == "pending"
        assert body["user"]["emailVerified"] is False
        # plaintext password never persisted
        raw = portal_app.db.query("SELECT password_hash FROM users WHERE email='a@b.com'")[0][
            "password_hash"
        ]
        assert "super-secure-pass-123" not in raw

    def test_verify_email_activates(self, portal_app) -> None:
        make_active_user(portal_app, email="c@d.com")
        user = portal_app.db.query_one(
            "SELECT status, email_verified FROM users WHERE email='c@d.com'"
        )
        assert user["status"] == "active" and user["email_verified"] == 1

    def test_verification_token_single_use(self, portal_app) -> None:
        portal_app.client.post(
            "/api/v1/auth/signup", json={"email": "e@f.com", "password": "super-secure-pass-123"}
        )
        token = portal_app.emails.sent[-1]["token"]
        status, _ = portal_app.client.post("/api/v1/auth/verify", json={"token": token})
        assert status == 200
        status, _ = portal_app.client.post("/api/v1/auth/verify", json={"token": token})
        assert status == 400  # replay rejected

    def test_weak_password_rejected(self, portal_app) -> None:
        status, _ = portal_app.client.post(
            "/api/v1/auth/signup", json={"email": "g@h.com", "password": "short"}
        )
        assert status == 400

    def test_invalid_email_rejected(self, portal_app) -> None:
        status, _ = portal_app.client.post(
            "/api/v1/auth/signup", json={"email": "nope", "password": "super-secure-pass-123"}
        )
        assert status == 400

    def test_duplicate_email_rejected(self, portal_app) -> None:
        for _ in range(2):
            status, _ = portal_app.client.post(
                "/api/v1/auth/signup",
                json={"email": "dup@x.com", "password": "super-secure-pass-123"},
            )
        assert status == 409


class TestSignInAndSession:
    def test_signin_sets_cookie_and_csrf(self, portal_app) -> None:
        make_active_user(portal_app)
        status, body = portal_app.client.signin()
        assert status == 200
        assert "gl_portal_session" in portal_app.client.cookies
        assert body["csrfToken"]

    def test_unverified_cannot_signin(self, portal_app) -> None:
        portal_app.client.post(
            "/api/v1/auth/signup", json={"email": "uv@x.com", "password": "super-secure-pass-123"}
        )
        status, _ = portal_app.client.post(
            "/api/v1/auth/signin", json={"email": "uv@x.com", "password": "super-secure-pass-123"}
        )
        assert status == 403

    def test_wrong_password_rejected(self, portal_app) -> None:
        make_active_user(portal_app)
        status, _ = portal_app.client.post(
            "/api/v1/auth/signin", json={"email": "dev@example.com", "password": "wrong-password-1"}
        )
        assert status == 401

    def test_protected_route_requires_auth(self, portal_app) -> None:
        status, _ = portal_app.client.get("/api/v1/dashboard")
        assert status == 401

    def test_csrf_required_for_mutations(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        portal_app.client.csrf_token = "wrong-token"
        status, _ = portal_app.client.post("/api/v1/developer-keys", json={"name": "key"})
        assert status == 403  # CSRF failure

    def test_session_revoked_on_signout(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        portal_app.client.post("/api/v1/auth/signout")
        portal_app.client.cookies.pop("gl_portal_session", None)
        status, _ = portal_app.client.get("/api/v1/dashboard")
        assert status == 401


class TestPasswordReset:
    def test_reset_flow(self, portal_app) -> None:
        make_active_user(portal_app, email="reset@x.com")
        status, body = portal_app.client.post(
            "/api/v1/auth/password-reset/request", json={"email": "reset@x.com"}
        )
        assert status == 200 and body.get("sent") is True
        token = portal_app.emails.sent[-1]["token"]
        status, _ = portal_app.client.post(
            "/api/v1/auth/password-reset", json={"token": token, "password": "brand-new-pass-456"}
        )
        assert status == 200
        # old password no longer works
        status, _ = portal_app.client.post(
            "/api/v1/auth/signin",
            json={"email": "reset@x.com", "password": "super-secure-pass-123"},
        )
        assert status == 401
        # new password works
        status, _ = portal_app.client.post(
            "/api/v1/auth/signin", json={"email": "reset@x.com", "password": "brand-new-pass-456"}
        )
        assert status == 200

    def test_reset_token_single_use(self, portal_app) -> None:
        make_active_user(portal_app, email="reuse@x.com")
        portal_app.client.post("/api/v1/auth/password-reset/request", json={"email": "reuse@x.com"})
        token = portal_app.emails.sent[-1]["token"]
        portal_app.client.post(
            "/api/v1/auth/password-reset", json={"token": token, "password": "brand-new-pass-456"}
        )
        status, _ = portal_app.client.post(
            "/api/v1/auth/password-reset", json={"token": token, "password": "another-pass-789"}
        )
        assert status == 400  # replay rejected

    def test_reset_does_not_leak_email_existence(self, portal_app) -> None:
        status, body = portal_app.client.post(
            "/api/v1/auth/password-reset/request", json={"email": "missing@x.com"}
        )
        assert status == 200 and body.get("sent") is True  # generic response


class TestRateLimiting:
    def test_signin_rate_limit(self, portal_app) -> None:
        make_active_user(portal_app)
        for _ in range(12):
            status, _ = portal_app.client.post(
                "/api/v1/auth/signin", json={"email": "dev@example.com", "password": "wrong-pass"}
            )
        assert status == 429

    def test_credential_create_rate_limit(self, portal_app) -> None:
        make_active_user(portal_app)
        signin_and_set_csrf(portal_app)
        status = 0
        for _ in range(15):
            status, _ = portal_app.client.post("/api/v1/developer-keys", json={"name": f"key{_}"})
        assert status == 429
