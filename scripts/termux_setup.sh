#!/usr/bin/env bash
# GhostLink — one-shot Termux bootstrap.
#
# Installs the toolchain GhostLink needs on a fresh Termux session and leaves
# the application ready to launch. Safe to re-run; every step is idempotent.
set -euo pipefail

info() { printf '\033[36m▸\033[0m %s\n' "$1"; }

info "Updating package index…"
pkg update -y

info "Installing Python and Git…"
pkg install -y python git

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "${SCRIPT_DIR}")"

info "Installing GhostLink runtime dependencies…"
python -m pip install --upgrade pip
python -m pip install -r "${PROJECT_ROOT}/requirements.txt"

# Optional but recommended: external keyboard keys and better touch input.
if ! grep -q "extra-keys" "${HOME}/.termux/termux.properties" 2>/dev/null; then
    info "Tip: enable the extra-keys row for arrow navigation (optional):"
    echo "    mkdir -p ~/.termux && echo \"extra-keys = [['ESC','TAB','UP','DOWN','ENTER']]\" >> ~/.termux/termux.properties"
fi

info "Done. Launch GhostLink with:"
echo "    cd ${PROJECT_ROOT} && python ghostlink.py"
