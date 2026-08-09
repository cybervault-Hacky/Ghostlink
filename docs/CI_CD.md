# CI/CD & supply-chain (Phase 13S/T)

## Status

**IMPLEMENTED & DOCUMENTED** as GitHub Actions workflows. They run in the
repository's CI when GitHub Actions is enabled; they have **not** been
executed in this offline build environment (network to GitHub Actions is not
available here).

## Workflows (`.github/workflows/`)

- **`test.yml`** — pytest, ruff lint + format, mypy --strict, compileall,
  frontend tests + build on Python 3.11/3.12, plus a **`postgres-integration`**
  job that runs the portal backend suite against a real PostgreSQL service.
- **`security.yml`** — deterministic security audit, repository secret scan,
  banned security-claim scan, and dependency vulnerability audits
  (`pip-audit`, `npm audit`). Findings are surfaced; we do **not** claim a
  vulnerability-free status — audit-tool limitations are documented.
- **`build.yml`** — package build (wheel + sdist), wheel inspection, Docker
  image build (prod + dev), compose config validation.
- **`release.yml`** — manual (`workflow_dispatch`) full release gate, verifies
  the tag matches the package version, and **never deploys automatically** to
  any server. The deployment step is a manual `environment: production`
  approval gate that only reports readiness; a human operator performs the
  deploy.

## Supply-chain security (13T)

- Dependency lock verification (`package-lock.json`) and deterministic pip
  installs in CI.
- Secret scanning (`scripts/scan_secrets.py`).
- Dependency vulnerability auditing where tooling is available (`pip-audit`,
  `npm audit`) — results are surfaced, not used to claim a clean bill.
- Docker image scanning and SBOM generation are **NOT IMPLEMENTED** (no
  tooling available in the offline environment); documented as a known
  limitation rather than claimed.
