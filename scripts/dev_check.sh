#!/usr/bin/env bash
# GhostLink — developer quality gate.
#
# Runs the same checks CI does: byte-compilation, lint, format verification,
# and the test suite. Install dev extras first: pip install -e ".[dev]"
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"
cd "${PROJECT_ROOT}"

PYTHON_BIN="${PYTHON:-python3}"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$1"; }

step "Byte-compilation"
"${PYTHON_BIN}" -m compileall -q ghostlink tests ghostlink.py

step "Ruff lint"
if command -v ruff >/dev/null 2>&1; then
    ruff check ghostlink tests ghostlink.py
    step "Ruff format (check only)"
    ruff format --check ghostlink tests ghostlink.py
else
    echo "ruff not installed — skipping (pip install ruff)"
fi

step "Mypy (strict)"
if "${PYTHON_BIN}" -c "import mypy" >/dev/null 2>&1; then
    "${PYTHON_BIN}" -m mypy --config-file pyproject.toml
else
    echo "mypy not installed — skipping (pip install mypy)"
fi

step "Tests"
"${PYTHON_BIN}" -m pytest tests/

step "Smoke: --version and --doctor"
"${PYTHON_BIN}" ghostlink.py --version
"${PYTHON_BIN}" ghostlink.py --doctor || true  # exit 3 simply reports findings

echo
echo "All developer checks passed."
