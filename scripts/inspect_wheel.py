#!/usr/bin/env python3
"""GhostLink — built-wheel inspection (Phase 9).

Verifies that a built wheel is a valid zip and contains no junk (cache
files, virtualenvs, git metadata, build/dist artifacts, or secrets). Used by
``scripts/release_check.sh`` and the release gate.

Exit 0 = clean; 1 = a problem was reported.
"""

from __future__ import annotations

import sys
import zipfile

BAD_MARKERS = ("__pycache__", ".venv", ".git/", "dist/", "build/", "secrets")
BAD_SUFFIXES = (".pyc",)


def inspect(wheel_path: str) -> int:
    if not zipfile.is_zipfile(wheel_path):
        print(f"  not a zip archive: {wheel_path}")
        return 1
    with zipfile.ZipFile(wheel_path) as archive:
        names = archive.namelist()
        for name in names:
            if any(marker in name for marker in BAD_MARKERS) or name.endswith(BAD_SUFFIXES):
                print(f"  junk in archive: {name}")
                return 1
    print(f"  wheel ok: {len(names)} entries, no junk")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: inspect_wheel.py <path.whl>")
        raise SystemExit(2)
    raise SystemExit(inspect(sys.argv[1]))
