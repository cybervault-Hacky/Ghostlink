#!/usr/bin/env bash
# GhostLink launcher — works on Termux and desktop Linux, no install required.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"

PYTHON_BIN="$(command -v python3 || command -v python)"
if [[ -z "${PYTHON_BIN}" ]]; then
    echo "GhostLink requires Python 3.11+ but no interpreter was found." >&2
    echo "On Termux run: pkg install python" >&2
    exit 1
fi

exec "${PYTHON_BIN}" "${PROJECT_ROOT}/ghostlink.py" "$@"
