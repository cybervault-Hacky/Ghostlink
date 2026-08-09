# GhostLink Developer Portal — Deployment (Phase 10B)

This document describes the production deployment architecture. Phase 10B is
a **local-first foundation**; a live internet deployment requires the
configuration below. GhostLink does **not** claim an actual production
deployment was performed — it documents how one would be set up.

## Architecture

```
Browser (React SPA, built with Vite)
        │  same-origin HTTPS
        ▼
Reverse proxy (nginx / Caddy)
        │  adds HSTS, terminates TLS, serves /static from the SPA build
        ▼
WSGI app (gunicorn / waitress)  ──  portal_server.app:create_wsgi_app
        │
        ▼
Database (SQLite dev / PostgreSQL prod)
```

## Build & run

```bash
# Frontend
cd portal/web && npm install && npm run build     # -> dist/

# Backend (dev)
cd portal/backend && python -m portal_server

# Backend (production, example with gunicorn)
cd portal/backend
pip install gunicorn
PORTAL_SECURE_COOKIES=true gunicorn -w 4 -b 127.0.0.1:8788 \
  'portal_server.app:create_wsgi_app'   # needs a factory-friendly wrapper
```

## Environment variables

See `portal/.env.example` and [docs/PRODUCTION_CONFIG.md](PRODUCTION_CONFIG.md).
The portal is configured entirely via environment; production **fails
closed** on missing or development values.

| Variable | Notes |
| --- | --- |
| `APP_ENV` | `development` / `staging` / `production` |
| `DATABASE_URL` | `postgresql://…` in production |
| `SESSION_SECRET` | High-entropy; required and non-default in production |
| `PORTAL_SECURE_COOKIES` | `true` over HTTPS (enforced in production) |
| `EMAIL_PROVIDER` | `dev` (records) or `smtp` (production); `smtp` requires `EMAIL_SMTP_*` |
| `WEBAUTHN_RP_ID` / `WEBAUTHN_ORIGIN` | Real RP ID and origin for passkeys (required in production) |
| `PORTAL_TRUSTED_PROXY` | `true` when behind a reverse proxy |

## Database migrations

Migrations are versioned, ordered, and transactional (see
[docs/DATABASE.md](DATABASE.md)). On startup the backend applies pending
migrations and records the version in `schema_meta`; a future schema fails
closed and is never modified. Never rely on destructive auto-recreation.

## Security hardening

* HTTPS everywhere; reverse proxy adds `Strict-Transport-Security`.
* The backend already sets CSP, `X-Content-Type-Options`, `Referrer-Policy`,
  `X-Frame-Options`, and `Permissions-Policy`.
* Cookies are `HttpOnly`, `SameSite=Lax`, `Secure` (production).
* Keep the DB, `.env`, and session secret private and `0600`-permissioned.

## WebAuthn in production

The built-in verifier supports ES256 / P-256 credentials. For full
attestation/format coverage, plug in a maintained WebAuthn library at the
`portal_server.webauthn` boundary. Do **not** collect or store biometric
data; only cryptographic assertions are handled.

## Backup strategy

Back up the database and (separately, carefully) the session secret. Do not
back up `.env` secrets in plaintext archives. See
[docs/BACKUP.md](BACKUP.md) for GhostLink-wide guidance.

## Logging & monitoring hooks

The portal logs metadata-only security events to the database; application
logs are emitted via Python logging (no secrets). Add structured logging and
uptime/error-rate monitoring at the reverse proxy / WSGI layer as your
infrastructure requires.

---

## Phase 13 — production deployment artifacts

**IMPLEMENTED & DOCUMENTED** (not publicly deployed). Phase 13 adds concrete,
security-hardened deployment artifacts in `deployment/`:

- `deployment/docker/` — production + dev `Dockerfile`s, `docker-compose.yml`
  and `docker-compose.prod.yml` (non-root, read-only FS, dropped capabilities,
  no secrets baked in, explicit port, healthcheck, graceful SIGTERM).
- `deployment/nginx/ghostlink.conf` — HTTPS/TLS (placeholder certs only),
  HTTP→HTTPS redirect, HSTS + security headers, request-size/timeout/connection
  limits, trusted-proxy headers, static-asset caching.
- `deployment/systemd/ghostlink.service` — runs gunicorn as a non-root user
  with systemd hardening and graceful shutdown.
- `deployment/postgres/README.md` — PostgreSQL provisioning/operations.

Recommended architecture: `Internet → HTTPS nginx → gunicorn → WSGI app →
PostgreSQL`. **No public deployment has been performed.**
