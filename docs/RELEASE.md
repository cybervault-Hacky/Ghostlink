# Release (Phase 14)

## Status

**IMPLEMENTED & DOCUMENTED.** This file describes the release process. Releases
are produced and verified locally by the repository gates; **no release has
been published** to PyPI, and **no public deployment has been performed**.

## Versioning

Phase 14 → `0.16.0`. The version is kept in sync across:

- `ghostlink/__init__.py` (`__version__`)
- `pyproject.toml` (`project.version`)
- `ghostlink/constants/app.py` (`RELEASE_PHASE`, `RELEASE_CODENAME`)
- `portal/web/package.json` (+ `package-lock.json`)
- `README.md`, `docs/ROADMAP.md`, `CHANGELOG.md`, release metadata
- `deployment/env/*.example` (`DEPLOYMENT_VERSION`)

`tests/test_packaging.py` enforces package/CLI version consistency.

## Release checklist

Before a release:

1. Full test suite (pytest) green.
2. `ruff check` + `ruff format --check` green.
3. `mypy --strict` green (main + portal backend).
4. `compileall` green.
5. Frontend `npm run test` + `npm run build` green.
6. `scripts/security_check.sh` green (deterministic audit + secret scan).
7. `scripts/release_check.sh` green (clean tree, package build, wheel
   inspection, CLI smoke, admin CLI smoke).
8. Migration verification (`ghostlink db status|verify`).
9. Backup verification (`ghostlink backup create|verify`).
10. Owner invariant (`TestSingleOwnerInvariant`) green.
11. Secret scan green; no `.env`, keys, dumps, or build artifacts committed.
12. Clean working tree.

## Build

```
python -m build --wheel --sdist
python scripts/inspect_wheel.py dist/*.whl
```

## CI/CD

GitHub Actions workflows live in `.github/workflows/`. In this build they could
**not** be pushed to the remote because the connected GitHub App token lacks
the `workflows` permission (GitHub refuses to create/update workflow files
without it). See `docs/CI_CD.md` for the exact blocker and manual push steps.

---

## Phase 15 — release process & manifest

### Commands

```
ghostlink release check     # run deterministic release gates (fail on issues)
ghostlink release verify    # same, returns non-zero on failure
ghostlink release manifest  # emit the machine-readable release manifest
```

The release gate **fails closed** when: version mismatch, unexpectedly dirty
working tree, secrets present, security audit fails, tests fail, frontend build
fails, package build fails, or migration checksum is invalid. **Creating a
release never deploys anything** — deployment requires an explicit, separately
approved step.

### Release manifest

Contains: version, phase, commit SHA, build timestamp, Python version, package
version, frontend version, migration version, dependency-lock state,
security-check result, test count, and build status.
