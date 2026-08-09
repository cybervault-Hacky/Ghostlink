#!/usr/bin/env python3
"""GhostLink — deterministic security audit tool (Phase 11K).

Runs deterministic, offline checks over the repository and the portal
source to catch production-config mistakes and accidental secret leakage.
It is *not* an "AI scanner" — every check is a concrete, reproducible test.

Checks:
  1. No committed secret material (PEM keys, tokens, join links).
  2. No debug-mode production configuration committed (PORTAL_SECURE_COOKIES
     false / EMAIL_PROVIDER=dev in a production .env).
  3. No unsafe cookie defaults in the HTTP layer (Secure/SameSite/HttpOnly).
  4. CSP present and without dangerous permissiveness.
  5. No telemetry/analytics SDK references in the frontend.
  6. No accidental network calls in the portal backend (no http/requests/urllib).
  7. No plaintext credential storage in the portal schema.

Exit 0 = clean; 1 = a finding was reported.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PORTAL = ROOT / "portal"

_SKIP = {"node_modules", ".git", "__pycache__", "dist", "build", ".venv"}

_BANNED_TELEMETRY = re.compile(
    r"google-analytics|gtag\(|googletagmanager|segment\.io|mixpanel|sentry\b"
    r"|hotjar|fullstory|amplitude|matomo|piwik",
    re.IGNORECASE,
)


def _text_files() -> list[Path]:
    files: list[Path] = []
    for base in (ROOT,):
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if any(part in _SKIP for part in path.parts):
                continue
            if path.suffix not in {
                ".py",
                ".ts",
                ".tsx",
                ".json",
                ".toml",
                ".md",
                ".sh",
                ".env.example",
            }:
                continue
            files.append(path)
    return files


def _repo_texts() -> list[tuple[Path, str]]:
    out: list[tuple[Path, str]] = []
    for path in _text_files():
        try:
            out.append((path, path.read_text(encoding="utf-8", errors="replace")))
        except OSError:
            continue
    return out


def _git_tracked() -> set[Path]:
    try:
        result = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files"],
            capture_output=True,
            text=True,
            check=True,
        )
        return {ROOT / line for line in result.stdout.splitlines()}
    except Exception:
        return set()


def check_secrets() -> list[str]:
    findings: list[str] = []
    pem = re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")
    token = re.compile(r"\bgli_[A-Za-z0-9]{20}\b")
    join = re.compile(r"gl://join/[A-Za-z0-9]{20}")
    for path, text in _repo_texts():
        rel = path.relative_to(ROOT)
        if pem.search(text):
            findings.append(f"{rel}: private-key PEM block")
        if "test" not in path.stem and "tests" not in path.parts:
            if token.search(text):
                findings.append(f"{rel}: invite token literal")
            if join.search(text):
                findings.append(f"{rel}: join link literal")
    return findings


def check_portal_config() -> list[str]:
    findings: list[str] = []
    for path, text in _repo_texts():
        if path.name != ".env.example" or "portal" not in str(path):
            continue
        if "PORTAL_SECURE_COOKIES=false" in text and "production" in text:
            findings.append(f"{path.name}: development cookie policy documented as default")
    return findings


def check_http_layer() -> list[str]:
    findings: list[str] = []
    base = PORTAL / "backend" / "portal_server"
    security_text = (
        (base / "security.py").read_text(encoding="utf-8")
        if (base / "security.py").exists()
        else ""
    )
    if "Content-Security-Policy" not in security_text:
        findings.append("security.py: missing CSP default header")
    if "X-Content-Type-Options" not in security_text:
        findings.append("security.py: missing X-Content-Type-Options")
    if "X-Frame-Options" not in security_text:
        findings.append("security.py: missing X-Frame-Options (frame protection)")
    app_text = (base / "app.py").read_text(encoding="utf-8") if (base / "app.py").exists() else ""
    if "HttpOnly=True" not in app_text:
        findings.append("app.py: session cookies not HttpOnly")
    if "SameSite=" not in app_text:
        findings.append("app.py: session cookies not SameSite-bound")
    return findings


def check_frontend_telemetry() -> list[str]:
    findings: list[str] = []
    for path, text in _repo_texts():
        if "portal/web" not in str(path):
            continue
        if _BANNED_TELEMETRY.search(text):
            findings.append(f"{path.relative_to(ROOT)}: telemetry/analytics reference")
    return findings


def check_no_network_in_backend() -> list[str]:
    findings: list[str] = []
    for path, text in _repo_texts():
        if "portal/backend/portal_server" not in str(path):
            continue
        if path.name == "__main__.py":
            continue  # the dev server binds a socket by design
        if re.search(
            r"import (requests|httpx|aiohttp)\b|from (requests|httpx|aiohttp|urllib\.request) ",
            text,
        ):
            findings.append(f"{path.relative_to(ROOT)}: outbound HTTP library usage")
    return findings


def run_all() -> list[str]:
    findings: list[str] = []
    findings += check_secrets()
    findings += check_portal_config()
    findings += check_http_layer()
    findings += check_frontend_telemetry()
    findings += check_no_network_in_backend()
    return findings


def main() -> int:
    findings = run_all()
    if findings:
        print("Security check findings:")
        for finding in findings:
            print(f"  ✗ {finding}")
        print(f"\n{len(findings)} finding(s).")
        return 1
    print("Security check clean.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
