# Dependencies & supply-chain (Phase 15H)

## Status

**DOCUMENTED & VERIFIED** (dependency state inspected). Vulnerability audits run
in CI (`pip-audit`, `npm audit`). We do **not** claim a vulnerability-free
status; findings are surfaced and assessed honestly.

## Direct dependencies

### Python (runtime, portal + CLI)

- `cryptography>=42,<47` — ChaCha20-Poly1305, HKDF-SHA256, X25519, Ed25519. The
  most security-sensitive dependency.
- `rich` — terminal rendering (UI only).
- `simple-term-menu` — terminal menus (UI only).
- `psycopg[binary]` / `psycopg_pool` — PostgreSQL driver + pool (portal).
- `gunicorn` — production WSGI server (portal).

### Frontend (portal/web)

- `react`, `react-dom` — UI runtime.
- `react-router-dom` — client-side routing (SPA).
- dev: `vite`, `vitest`, `@vitejs/plugin-react`, `@testing-library/react`.

## Security-sensitive dependencies

- `cryptography` (crypto primitives) and `react-router-dom` (browser routing).

## Known audit findings (honest, not hidden)

`npm audit` reports two advisory groups:

1. **`react-router` / `react-router-dom` `6.0.0 – 7.17.0`**
   (`GHSA-337j-9hxr-rhxg`, `GHSA-wrjc-x8rr-h8h6`). The `deserializeErrors`
   advisory applies to **SSR hydration**; GhostLink's portal is a **client-side
   SPA with no server rendering**, so that path is not exercised. The open
   redirect advisory concerns `<Link>`/`useNavigate` with user-controlled
   hrefs; the portal only uses static routes. The fix requires a breaking major
   upgrade to `react-router@7`; that upgrade is **not applied** to avoid
   destabilising the routing layer. **ENVIRONMENT-GATED / DEFERRED**: upgrade to
   react-router 7 in a dedicated frontend phase.

2. **`vite <= 6.4.2` (dev dependency)** — a build-time/dev-server tool; not
   shipped in the runtime bundle. The fix requires `vite@8` (breaking). Not
   applied to avoid destabilising the build toolchain; documented.

## Update strategy

- Patch/minor updates are applied via `pip` / `npm` lockfiles and validated by
  the full quality gate before commit.
- Breaking upgrades (e.g. react-router 7, vite 8) are scheduled as dedicated,
  fully-tested changes — never mixed into an operational phase.

## Emergency dependency replacement strategy

The crypto primitives are isolated behind the `ghostlink.developer` key
abstraction and the portal's crypto helpers, so a problematic `cryptography`
release can be pinned to a known-good version without changing call sites. The
database driver is isolated behind the `portal_server.db` backend abstraction
(`DatabaseBackend`), so `psycopg` could be swapped for `pg8000` with a new
backend module. No single dependency is load-bearing across the whole system.

## Audit commands

```
pip-audit -r <(pip freeze)          # Python dependencies
npm audit --audit-level=high        # frontend dependencies (portal/web)
python scripts/scan_secrets.py      # repository secret scan
python scripts/security_check.py    # deterministic security audit
```
