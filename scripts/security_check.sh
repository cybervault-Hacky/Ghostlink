#!/usr/bin/env bash
# GhostLink — deterministic security audit (Phase 11K).
#
# Runs the offline, reproducible security checks in scripts/security_check.py
# and then the repository secret scan. Deterministic; no network.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"
cd "${PROJECT_ROOT}"

PYTHON="${PYTHON:-python3}"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "Security audit"
"${PYTHON}" scripts/security_check.py

step "Repository secret scan"
"${PYTHON}" scripts/scan_secrets.py

step "Banned security-claim scan"
if grep -RniE "100% secure|impossible to hack|impossible to break|mathematically unhackable|immune to compromise" README.md docs/ ghostlink/ portal/ --include=*.md --include=*.py --include=*.tsx 2>/dev/null; then
    echo "found a banned security claim" >&2
    exit 1
fi
echo "no banned security claims"

echo
echo "All security checks passed."
