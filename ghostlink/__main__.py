"""Allow running GhostLink as ``python -m ghostlink``."""

from __future__ import annotations

from ghostlink.cli.entrypoint import main

if __name__ == "__main__":
    raise SystemExit(main())
