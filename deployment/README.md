# Deployment (Phase 13)

This directory contains the **reference** production deployment for the
GhostLink Developer Portal. It is intended to be adapted to your own
infrastructure. **GhostLink is not publicly deployed anywhere; no public
deployment has occurred.**

## Architecture

```
Internet
   │
   ▼
HTTPS reverse proxy (Nginx)      ← terminates TLS, sets trusted headers
   │
   ▼
Gunicorn (WSGI)                  ← 2 workers, JSON logs
   │
   ▼
portal_server WSGI app
   │
   ├── PostgreSQL                 ← primary persistence + distributed rate limits
   └── backups/                   ← encrypted logical backups (`.glbak`)
```

## Contents

| Path | Purpose |
|------|---------|
| `docker/Dockerfile` | Production image (non-root, read-only FS, no secrets) |
| `docker/Dockerfile.dev` | Development image (source-mounted, SQLite) |
| `docker/docker-compose.yml` | Local dev stack (PostgreSQL + portal) |
| `docker/docker-compose.prod.yml` | Production compose overlay (env-driven secrets) |
| `nginx/ghostlink.conf` | Reverse proxy + TLS + security headers + limits |
| `systemd/ghostlink.service` | systemd unit running gunicorn as non-root |
| `postgres/README.md` | PostgreSQL provisioning and operations |

## Environment variables (production)

`APP_ENV=production` and all of the following are **required** (the portal
fails closed if any is missing or insecure):

- `DATABASE_URL` (PostgreSQL)
- `SESSION_SECRET`
- `PORTAL_SECURE_COOKIES=true`
- `EMAIL_PROVIDER` + SMTP settings
- `WEBAUTHN_RP_ID`, `WEBAUTHN_ORIGIN`
- `ALLOWED_HOSTS`
- `RATE_LIMIT_BACKEND=postgresql` (mandatory for a PostgreSQL deployment)

Optional but recommended: `TRUSTED_PROXIES`, `LOG_LEVEL`, `LOG_FORMAT`,
`BACKUP_DIR`, `BACKUP_ENCRYPT`, `BACKUP_PASSPHRASE`, retention variables.

## SECURITY ASSUMPTIONS (each must hold for the reference config to be safe)

1. Secrets are provided via environment/secret manager only; never committed
   to git or baked into images.
2. Only the reverse proxy terminates TLS and is exposed publicly; the portal
   binds `127.0.0.1` and is never directly reachable from the internet.
3. `TRUSTED_PROXIES` / `ALLOWED_HOSTS` match the real proxy; otherwise
   `X-Forwarded-For`/`Host` are ignored or rejected.
4. Real TLS certificates are provisioned (the nginx config uses placeholders
   and ships **no** keys).
5. HSTS is enabled only after HTTPS is verified working.

## Verification

```bash
docker compose -f deployment/docker/docker-compose.prod.yml config   # validate
python -m portal_server.manage system health
python -m portal_server.manage system readiness
curl -s https://example.com/health/live
curl -s https://example.com/health/ready
```
