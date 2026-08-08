#!/usr/bin/env python3
"""GhostLink launcher.

Run GhostLink directly from a source checkout — no installation required:

    python3 ghostlink.py            # interactive menu
    python3 ghostlink.py --doctor   # environment diagnostics
    python3 ghostlink.py --help     # all options

For an installed entrypoint, use `pip install .` and call `ghostlink`.
"""

from __future__ import annotations

import sys
from pathlib import Path

if __package__ is None or __package__ == "":
    # Allow running this file directly (e.g. `python ghostlink.py`) from
    # anywhere: make the project root importable before loading the package.
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from ghostlink.cli.entrypoint import main

if __name__ == "__main__":
    raise SystemExit(main())
