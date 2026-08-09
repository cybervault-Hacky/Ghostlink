# GhostLink Developer Portal (Phase 10B)

The GhostLink Developer Portal is a secure web application for GhostLink
developers: remote developer accounts, developer credentials with a secure
lifecycle, projects, sessions, security activity, and account settings.

> **Status:** Phase 10B foundation — a real, tested, security-sensitive
> portal. It is **local-first**: the backend is a dependency-light Python
> WSGI app (stdlib + `cryptography`), SQLite by default (PostgreSQL-ready
> schema), and the frontend is React + TypeScript + Vite. Production email,
> WebAuthn attestation breadth, and real HTTPS deployment are documented
> but not shipped as live services.

## Layout

```
portal/
  backend/portal_server/   Python WSGI backend (stdlib + cryptography)
  backend/tests/           Backend security tests (pytest)
  web/                     React + TypeScript + Vite frontend
  .env.example             Environment template (never commit real .env)
```

## Run locally

Backend:

```bash
cd portal/backend
pip install -r ../../requirements.txt   # cryptography (already a GhostLink dep)
python -m portal_server                  # http://127.0.0.1:8788
```

Frontend:

```bash
cd portal/web
npm install
npm run dev                              # http://127.0.0.1:5173 (proxies /api)
```

Tests:

```bash
pytest portal/backend/tests              # backend security tests
cd portal/web && npm test                # frontend tests
cd portal/web && npm run build           # production build
```

## Documentation

- Architecture, authentication, credentials, API: `docs/DEVELOPER_PORTAL.md`
- API reference: `docs/API.md`
- Security model: `docs/SECURITY.md`
- Deployment: `docs/DEPLOYMENT.md`
