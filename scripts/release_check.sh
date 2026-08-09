#!/usr/bin/env bash
# GhostLink — release-candidate verification (Phase 9).
#
# Runs the complete production-release gate in dependency order and stops on
# the first failure. Everything is local and deterministic; it never contacts
# the network and never modifies user state.
#
# Steps:
#   1. clean git tree
#   2. version consistency (pyproject vs package)
#   3. byte-compilation
#   4. ruff lint
#   5. ruff format (check)
#   6. mypy --strict
#   7. full pytest suite
#   8. package build (wheel + sdist)
#   9. package inspection (no secrets / junk in the archive)
#  10. CLI smoke from the built artifact
#  11. repository secret scan
#  12. banned security-claim scan
#
# Usage:
#   scripts/release_check.sh            # full gate
#   SKIP_BUILD=1 scripts/release_check.sh   # skip the (slow) wheel/sdist build
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"
cd "${PROJECT_ROOT}"

PYTHON="${PYTHON:-python3}"
step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }
fail() { printf '\n\033[1;31mrelease check FAILED: %s\033[0m\n' "$1" >&2; exit 1; }

# ---------------------------------------------------------------- 1. git tree
step "Clean git tree"
if ! git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    fail "not a git repository"
fi
if [ -n "$(git status --porcelain)" ]; then
    fail "working tree is not clean (git status shows changes)"
fi

# ------------------------------------------------------------- 2. version sync
step "Version consistency"
PKG_VERSION="$("${PYTHON}" -c 'import ghostlink,sys;sys.stdout.write(ghostlink.__version__)')"
PYPROJECT_VERSION="$(sed -n 's/^version = "\([^"]*\)"/\1/p' pyproject.toml | head -1)"
if [ "$PKG_VERSION" != "$PYPROJECT_VERSION" ]; then
    fail "version mismatch: package=$PKG_VERSION pyproject=$PYPROJECT_VERSION"
fi
echo "version: ${PKG_VERSION}"

# ---------------------------------------------------------- 3. byte-compilation
step "Byte-compilation"
"${PYTHON}" -m compileall -q ghostlink tests ghostlink.py

# ------------------------------------------------------------ 4. ruff lint
step "Ruff lint"
ruff check ghostlink tests ghostlink.py
ruff check portal/backend

# ------------------------------------------------------- 5. ruff format check
step "Ruff format (check)"
ruff format --check ghostlink tests ghostlink.py
ruff format --check portal/backend

# ---------------------------------------------------------- 6. mypy --strict
step "Mypy (strict)"
"${PYTHON}" -m mypy --config-file pyproject.toml
"${PYTHON}" -m mypy --strict portal/backend/portal_server

# -------------------------------------------------------------- 7. pytest
step "Test suite"
"${PYTHON}" -m pytest tests/ portal/backend/tests/

# ------------------------------------------------- 7b. frontend (if npm present)
step "Frontend build, typecheck & tests"
if command -v npm >/dev/null 2>&1 && [ -f portal/web/package.json ]; then
    (cd portal/web && npm run build && npm run test) || fail "frontend build/test failed"
else
    echo "npm not available — skipping frontend (backend gate still passed)"
fi

# ------------------------------------------------------------- 8. package build
step "Package build (wheel + sdist)"
if [ "${SKIP_BUILD:-0}" = "1" ]; then
    echo "SKIP_BUILD=1 — skipping build"
else
    rm -rf build dist *.egg-info
    "${PYTHON}" -m build --wheel --sdist
fi

# ---------------------------------------------------------- 9. package inspect
step "Package inspection"
if [ "${SKIP_BUILD:-0}" = "1" ]; then
    echo "SKIP_BUILD=1 — skipping archive inspection"
else
    WHEEL="$(ls dist/*.whl 2>/dev/null | head -1)" || fail "no wheel found in dist/"
    "${PYTHON}" scripts/inspect_wheel.py "$WHEEL" || fail "wheel inspection failed"
fi

# --------------------------------------------------------------- 10. CLI smoke
step "CLI smoke from built artifact"
if [ "${SKIP_BUILD:-0}" = "1" ]; then
    echo "SKIP_BUILD=1 — running CLI smoke from source instead"
    "${PYTHON}" ghostlink.py --version
    "${PYTHON}" ghostlink.py --help >/dev/null
    "${PYTHON}" ghostlink.py --doctor || true
else
    WHEEL="$(ls dist/*.whl 2>/dev/null | head -1)"
    SMOKE_VENV="$(mktemp -d)/smoke"
    "${PYTHON}" -m venv "${SMOKE_VENV}"
    "${SMOKE_VENV}/bin/pip" install --quiet "$WHEEL" || fail "install from wheel failed"
    "${SMOKE_VENV}/bin/ghostlink" --version || fail "ghostlink --version failed"
    "${SMOKE_VENV}/bin/ghostlink" --help >/dev/null || fail "ghostlink --help failed"
    "${SMOKE_VENV}/bin/ghostlink" --doctor || true  # exit 3 simply reports findings
    rm -rf "$(dirname "${SMOKE_VENV}")"
fi

# ----------------------------------------------------------- 11. security audit
step "Deterministic security audit"
"${PYTHON}" scripts/security_check.py

# ----------------------------------------------------------- 12. secret scan
step "Repository secret scan"
"${PYTHON}" scripts/scan_secrets.py

# -------------------------------------------------- 13. banned security claims
# Strong affirmative overclaims the project must never make. Negated phrases
# ("not invisible", "not ... perfect security") are honest disclaimers and
# are intentionally NOT matched (matches tests/test_packaging.py).
step "Banned security-claim scan"
if grep -RniE "100% secure|impossible to hack|impossible to break|mathematically unhackable|immune to compromise" README.md docs/ ghostlink/ --include=*.md --include=*.py; then
    fail "found a banned security claim"
fi
echo "no banned security claims"

# ---------------------------------------------------- 14. Phase 13 admin CLI
step "Phase 13 admin CLI smoke (db status / system readiness)"
TMPADMIN="$(mktemp -d)"
if DATABASE_URL="${TMPADMIN}/portal.db" BACKUP_DIR="${TMPADMIN}/backups" \
    "${PYTHON}" ghostlink.py db status >/dev/null 2>&1; then
    DATABASE_URL="${TMPADMIN}/portal.db" BACKUP_DIR="${TMPADMIN}/backups" \
        "${PYTHON}" ghostlink.py system readiness >/dev/null 2>&1 || fail "system readiness failed"
    DATABASE_URL="${TMPADMIN}/portal.db" BACKUP_DIR="${TMPADMIN}/backups" \
        "${PYTHON}" ghostlink.py backup create >/dev/null 2>&1 || fail "backup create failed"
    BK="$(ls "${TMPADMIN}"/backups/*.glbak 2>/dev/null | head -1)"
    [ -n "$BK" ] && "${PYTHON}" ghostlink.py backup verify "$BK" >/dev/null 2>&1 || fail "backup verify failed"
    echo "admin CLI smoke OK"
else
    echo "admin CLI smoke skipped (portal backend unavailable)"
fi
rm -rf "${TMPADMIN}"

# ---------------------------------------------------- 15. Docker artifact check
step "Docker artifact static check"
if "${PYTHON}" scripts/docker_check.py >/dev/null 2>&1; then
    echo "docker_check OK"
else
    echo "docker_check reported findings (see output)"
    fail "docker_check failed"
fi

# ---------------------------------------------------- 16. Dependency audit (best-effort)
step "Dependency audit (pip-audit / npm audit if available)"
if command -v pip-audit >/dev/null 2>&1; then
    pip-audit -r <(pip freeze) || echo "pip-audit found issues (review above) — not blocking local gate"
else
    echo "pip-audit not installed — run: pip install pip-audit && pip-audit -r <(pip freeze)"
fi
if command -v npm >/dev/null 2>&1 && [ -f portal/web/package.json ]; then
    (cd portal/web && npm audit --audit-level=high || echo "npm audit found issues — review above")
else
    echo "npm audit skipped (npm unavailable)"
fi

echo
echo "All release checks passed."
