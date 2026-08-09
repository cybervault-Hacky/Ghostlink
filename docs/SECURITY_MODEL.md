# GhostLink Developer Portal — Security Model (Phase 11)

This document consolidates the portal's security model. Full design,
deployment, and recovery detail live in
[docs/DEVELOPER_PORTAL.md](DEVELOPER_PORTAL.md),
[docs/PRODUCTION_CONFIG.md](PRODUCTION_CONFIG.md),
[docs/DEPLOYMENT.md](DEPLOYMENT.md), and
[docs/DISASTER_RECOVERY.md](DISASTER_RECOVERY.md).

## Authentication

* Passwords hashed with PBKDF2-HMAC-SHA256 (600k iterations, per-user salt);
  never stored or returned in plaintext.
* Email verification and password reset use single-use, short-lived,
  CSPRNG tokens stored hashed at rest.
* Sessions: HttpOnly, SameSite=Lax, Secure in production; absolute lifetime
  + idle timeout; session rotation on MFA-complete sign-in; password change
  revokes all sessions.
* Account lockout + IP/account rate limiting. Password-reset responses are
  generic (no email-existence enumeration).

## Sessions

* Cookie-only; never in localStorage.
* CSRF token per session, required on all unsafe methods.
* Individual and bulk revocation; `revoke-others` keeps the current session.

## MFA & WebAuthn

* TOTP (RFC 6238) with hashed, once-shown recovery codes.
* WebAuthn/passkeys: single-use, expiring challenges; origin validation;
  ES256 signature verification. **No biometric data is ever collected,
  transmitted, or stored** — the platform authenticator handles biometrics
  and the server receives only cryptographic assertions.

## Developer credentials

* Generated with the Phase 10A CSPRNG; secret shown once, stored only as
  salted HKDF verification material.
* Create / rotate / revoke are authenticated, confirmed, rate-limited, and
  audited. Rotation and revocation are persistent and irreversible.

## Web / transport

* Strict configuration (fail-closed in production), HSTS, CSP, and the
  standard security headers (see `security.py`).
* Errors are intentionally boring: no stack traces, DB errors, paths, or
  secret material.

## Logging & secrets

* Metadata-only audit/operational logs; a redaction backstop and a
  deterministic security audit (`scripts/security_check.sh`) enforce that no
  secret reaches logs, exceptions, URLs, or frontend persistent state.

## Honest limitations

* Rate limits are per-process, not distributed/global.
* SQLite is the dev database; Postgres is production-ready by schema but a
  Postgres backend must be wired at deployment.
* Email delivery requires a real provider in production; the dev adapter only
  records messages.
* WebAuthn covers ES256/P-256; full attestation breadth is a documented
  extension point.
* No anonymity claim; no claim of protection against a fully compromised
  host.
