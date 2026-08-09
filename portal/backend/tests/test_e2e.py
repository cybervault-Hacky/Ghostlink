"""End-to-end lifecycle test (Phase 11P).

Drives the real WSGI application through the full user journey with real
application boundaries — no mocks of security-critical behavior.
"""

from __future__ import annotations

from portal_server.mfa import current_totp


class TestFullLifecycle:
    def test_full_developer_journey(self, portal_app) -> None:
        c = portal_app.client

        # 1. Signup (pending) + verification token recorded by dev adapter.
        status, body = c.post(
            "/api/v1/auth/signup",
            json={"email": "e2e@example.com", "password": "super-secure-pass-123"},
        )
        assert status == 201 and body["user"]["status"] == "pending"

        # 2. Verify email.
        token = portal_app.emails.sent[-1]["token"]
        status, _ = c.post("/api/v1/auth/verify", json={"token": token})
        assert status == 200

        # 3. Sign in, set CSRF.
        status, body = c.signin("e2e@example.com", "super-secure-pass-123")
        assert status == 200

        # 4. Dashboard.
        status, body = c.get("/api/v1/dashboard")
        assert status == 200 and body["user"]["email"] == "e2e@example.com"

        # 5. Create developer credential -> one-time secret reveal.
        status, body = c.post("/api/v1/developer-keys", json={"name": "prod-key"})
        assert status == 201
        secret = body["credential"]["secret"]
        key_id = body["credential"]["keyId"]
        assert secret.startswith("gl_dev_dk_")

        # 6. Verify the credential.
        status, body = c.post("/api/v1/developer-keys/verify", json={"credential": secret})
        assert status == 200 and body.get("authenticated") is True

        # 7. Enable MFA + verify TOTP.
        status, body = c.post("/api/v1/security/mfa/setup")
        assert status == 200
        status, body = c.post(
            "/api/v1/security/mfa/setup",
            json={"confirm": True, "code": current_totp(body["secret"])},
        )
        assert status == 201 and len(body["recoveryCodes"]) == 8

        # 8. Create a project.
        status, body = c.post("/api/v1/projects", json={"name": "My App"})
        assert status == 201

        # 9. Sign out; MFA is now required on the next sign-in.
        c.post("/api/v1/auth/signout")
        c.cookies.clear()
        c.csrf_token = None
        status, body = c.post(
            "/api/v1/auth/signin",
            json={"email": "e2e@example.com", "password": "super-secure-pass-123"},
        )
        assert status == 200 and body.get("mfaRequired") is True
        preauth = body["preauth"]
        status, body = c.post(
            "/api/v1/auth/mfa",
            json={"preauth": preauth, "code": current_totp(secret2_secret(portal_app))},
        )
        assert status == 200
        # MFA completion issues a fresh session + CSRF token — capture it.
        c.csrf_token = body["csrfToken"]

        # 10. Rotate credential -> old invalid, new valid.
        status, body = c.post(f"/api/v1/developer-keys/{key_id}/rotate")
        assert status == 200
        new_secret = body["credential"]["secret"]
        assert c.post("/api/v1/developer-keys/verify", json={"credential": new_secret})[0] == 200
        assert c.post("/api/v1/developer-keys/verify", json={"credential": secret})[0] == 401

        # 11. Revoke the new credential -> invalid.
        new_key = body["credential"]["keyId"]
        assert c.post(f"/api/v1/developer-keys/{new_key}/revoke")[0] == 200
        assert c.post("/api/v1/developer-keys/verify", json={"credential": new_secret})[0] == 401

        # 12. Security events are recorded (metadata only, no secret).
        status, body = c.get("/api/v1/activity")
        assert status == 200
        actions = {e["action"] for e in body["events"]}
        for expected in ("signup", "key_created", "key_rotated", "key_revoked", "mfa_enabled"):
            assert expected in actions


def secret2_secret(portal_app):
    row = portal_app.db.query_one("SELECT secret FROM mfa_totp WHERE enabled=1")
    return row["secret"]
