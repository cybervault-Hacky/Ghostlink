#!/usr/bin/env python3
"""GhostLink — repository secret-leak scan (Phase 9).

Searches the tracked source tree for accidental hard-coded secrets and for
the *shape* of runtime secrets that must never appear in committed files:
private-key PEM blocks, invite tokens, join links, and high-entropy hex
blobs that look like key material in source/config context.

This is a tripwire, not a proof: it complements (never replaces) the runtime
log-redaction backstop and the code discipline that already avoids logging
secrets. It is deterministic, offline, and safe to run on every CI/release.

Exit code 0 = clean; 1 = a finding was reported.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# PEM private-key blocks must never be committed.
_PEM_KEY_BLOCK = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")

# One-time invite tokens (gli_ + 20 chars) and join links.
_TOKEN = re.compile(r"\bgli_[A-Za-z0-9]{20}\b")
_JOIN_LINK = re.compile(r"gl://join/[A-Za-z0-9]{20}")
_JOIN_LINK_EXAMPLE = re.compile(r"gl://join/[<…A-Za-z0-9]{0,20}[…>]?\b")

# A bare 64-hex string that looks like an Ed25519/X25519 private key. This is
# noisy, so only flag it in non-test source under key-ish key names.
_HEX64 = re.compile(r"\b[0-9a-fA-F]{64}\b")
_KEYISH_NAME = re.compile(r"(private_key|secret_key|session_key|sender_key|signing_key)")

# Files / directories we never scan (binary, third-party, git metadata).
_SKIP_DIRS = {
    ".git",
    "__pycache__",
    ".venv",
    "build",
    "dist",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "*.egg-info",
}
_ALWAYS_SKIP = {".pyc", ".whl", ".png", ".jpg", ".svg", ".ico", ".lock", ".egg"}


def tracked_files() -> list[Path]:
    """The set of files git tracks (so generated/untracked junk is ignored)."""
    try:
        out = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files"],
            capture_output=True,
            text=True,
            check=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        # No git available — scan the whole tree, honoring _SKIP_DIRS.
        return [p for p in ROOT.rglob("*") if p.is_file()]
    files: list[Path] = []
    for line in out.stdout.splitlines():
        path = ROOT / line
        if path.is_file():
            files.append(path)
    return files


def _interesting(path: Path) -> bool:
    if any(part in _SKIP_DIRS for part in path.parts):
        return False
    if path.suffix in _ALWAYS_SKIP:
        return False
    # Only text-ish files we can meaningfully scan.
    return path.suffix in {
        ".py",
        ".toml",
        ".md",
        ".sh",
        ".txt",
        ".cfg",
        ".json",
        ".yaml",
        ".yml",
        ".rst",
        ".ini",
    }


def _strip_py_surface(text: str) -> str:
    """Remove docstrings and comments so format specs don't false-positive.

    Not a real parser — good enough for a tripwire. A secret inside an actual
    runtime string survives this (docstrings/comments only are removed).
    """
    import io
    import tokenize

    try:
        tokens = list(tokenize.generate_tokens(io.StringIO(text).readline))
    except (tokenize.TokenError, IndentationError):
        return text  # can't tokenize; scan raw
    keep: list[str] = []
    for tok in tokens:
        if tok.type in (tokenize.COMMENT, tokenize.STRING) and (
            tok.type is tokenize.COMMENT
            or tok.string.startswith(("'''", '"""'))
        ):
            keep.append(" " * len(tok.string))
        else:
            keep.append(tok.string)
    return "".join(keep)


def _is_test(path: Path) -> bool:
    return "tests" in path.parts or path.stem.startswith("test_")


def scan() -> int:
    findings: list[str] = []
    for path in tracked_files():
        if not _interesting(path):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = path.relative_to(ROOT)
        if _PEM_KEY_BLOCK.search(text):
            findings.append(f"{rel}: embedded private-key PEM block")
        # Token / join-link literals: only flag in non-test source with
        # docstrings/comments stripped, so documented formats and test
        # fixtures don't false-positive.
        if not _is_test(path):
            surface = _strip_py_surface(text) if path.suffix == ".py" else text
            if _TOKEN.search(surface):
                findings.append(f"{rel}: invite token literal (gli_…)")
            if _JOIN_LINK.search(surface):
                findings.append(f"{rel}: join link literal (gl://join/…)")
        # 64-hex under a key-ish variable name, outside tests (tests may
        # legitimately construct such constants for fixtures).
        if ".py" in path.suffix and not _is_test(path):
            for line_no, line in enumerate(text.splitlines(), 1):
                if _HEX64.search(line) and _KEYISH_NAME.search(line):
                    findings.append(f"{rel}:{line_no}: 64-hex value under a key-ish name")

    if findings:
        print("Secret scan findings:")
        for finding in findings:
            print(f"  ✗ {finding}")
        print(f"\n{len(findings)} finding(s) — see docs/ROADMAP.md Phase 9.")
        return 1
    print("Secret scan clean.")
    return 0


if __name__ == "__main__":
    sys.exit(scan())
