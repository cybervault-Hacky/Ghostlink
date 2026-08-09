# Changelog

All notable GhostLink changes by release. GhostLink follows a phased,
production-gated development model — each phase is fully tested before the next
begins.

## [0.17.0] — Phase 15 (Production Operations & Platform Maturity)

- **Production readiness** — `ghostlink production check` (PASS/WARN/FAIL,
  non-zero on mandatory failure, never prints secrets).
- **Release management** — `ghostlink release check|verify|manifest`
  (fail-closed gates; machine-readable manifest with version, commit, build
  timestamp, python, package/frontend/migration versions, lock state, test
  count, build status). A release never deploys automatically.
- **Security severity model** — INFO/NOTICE/WARNING/HIGH/CRITICAL
  classification; security event emission in structured logs.
- **Secret scanning hardened** — detects pairing codes, `dk_…` credentials,
  Bearer/JWT tokens, assigned secrets, and database-URL passwords while
  ignoring placeholders/examples/test fixtures.
- **Deterministic DR drill** — encrypted backup, restore into isolated DB,
  revoked state stays revoked, no Owner created; corruption/wrong-key/
  incomplete/schema tests.
- **API contract** — machine-readable developer-API contract with stability
  tests locking the endpoint set and error envelope.
- **Gated PostgreSQL integration suite** (12 tests) — runs against a real
  server in CI; environment-gated locally (reported honestly).
- **Docker static verification** — `scripts/docker_check.py`.
- **Concurrency & failure tests** — rate limits, rotation, backup, replay, DB
  locked/duplicate/rollback.
- **Extended Owner invariant** — `TestSingleOwnerInvariant` covers project/
  device/credential/CLI/DB/restore escalation paths.

**Status:** no public deployment has been performed. PostgreSQL runtime
integration and Docker image builds are environment-gated in this build.

## [0.16.0] — Phase 14 (Public Production Launch & Reliability)

- **Production environment model** — explicit `development` / `staging` /
  `production` with fail-closed validation. Production rejects SQLite, memory
  rate limiting, insecure cookies, wildcard `ALLOWED_HOSTS`, dev email, the dev
  session secret, and non-HTTPS `PUBLIC_BASE_URL`. Added
  `deployment/env/.env{,.development,.staging,.production}.example` templates.
- **PostgreSQL production path** — `DB_POOL_*`, `DB_CONNECT_TIMEOUT`,
  `DB_STATEMENT_TIMEOUT`, `DB_IDLE_TIMEOUT`; `BACKUP_ENCRYPTION_KEY`;
  `EMAIL_SMTP_USERNAME`. Runtime integration remains environment-gated (no
  PostgreSQL server available in the build environment); adapter + gated tests
  in place.
- **Observability** — added `event`, `environment`, `error_class` to structured
  logs; startup/shutdown lifecycle events.
- **Termux reliability** — the portal client now fails closed on a 2xx with
  malformed JSON (`invalid_response`) and surfaces clear errors for HTTP
  429/500/503/401; tests added.
- **Security audit tooling** — extended `scripts/security_check.py` for Docker
  hardening, production env defaults, wildcard CORS, and auto-deploy
  workflows; added tests for the checker.
- **Owner invariant** — dedicated `TestSingleOwnerInvariant` regression suite.
- **Frontend quality** — shared `Loading` (role=status) and `ErrorState`
  (role=alert) components with loading/error/empty states and reduced-motion
  support.
- **Docs & release** — `CHANGELOG.md`, `docs/RELEASE.md`,
  `docs/PRODUCTION_RUNBOOK.md`, `docs/INCIDENT_RESPONSE.md`, `docs/TERMUX.md`.

**Status:** no public deployment has been performed. PostgreSQL runtime
integration and Docker image builds are environment-gated in this build.

## [0.15.0] — Phase 13 (Production Infrastructure, Database & Deployment Hardening)

- PostgreSQL backend abstraction (`psycopg3` + `psycopg_pool`), numbered +
  checksummed migrations, transaction safety, distributed rate limiting,
  health/readiness/liveness, structured JSON observability, encrypted backups,
  retention, gunicorn/reverse-proxy/systemd/Docker deployment, CI/CD workflows
  (push of workflow files was blocked by GitHub App `workflows` permission).

## [0.14.0] — Phase 12 (Developer API Platform & Termux Integration)

- Developer API v1, Termux developer CLI, device identity, secure pairing,
  scoped credentials, access/refresh token rotation, project binding, owner/
  developer role separation, MFA, WebAuthn, glassmorphism developer portal.

## [0.13.0] — Phase 11 (Production Portal Hardening & Launch Readiness)

## [0.10.0–0.12.0] — Phases 10A/10B

- Developer account & credential infrastructure; the GhostLink Developer Portal.
