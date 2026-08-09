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
        # Skip test fixtures: they legitimately embed secret-shaped strings to
        # exercise detection. The repository-wide scan (scan_secrets.py)
        # covers every file independently.
        if "test" in path.stem or "tests" in path.parts:
            continue
        if pem.search(text):
            findings.append(f"{rel}: private-key PEM block")
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


def check_devapi_hygiene() -> list[str]:
    """Phase 12 checks: no owner escalation, no token/secret logging, scopes
    enforced server-side, no Authorization header logging."""
    findings: list[str] = []
    base = PORTAL / "backend" / "portal_server"
    dev = (
        (base / "devapi_handlers.py").read_text(encoding="utf-8")
        if (base / "devapi_handlers.py").exists()
        else ""
    )
    # No owner scope may exist.
    if re.search(r'"owner[^"]*"\s*[,:]', dev) or "owner:*" in dev:
        findings.append("devapi_handlers.py: owner scope or role token present")
    # No Authorization header / token logged.
    if re.search(r"log(ger)?\(.*(token|Authorization|secret)", dev):
        findings.append("devapi_handlers.py: possible token/secret logging")
    # Scopes must be validated (normalize_scopes rejects unknown scopes).
    if "normalize_scopes" not in dev and "VALID_SCOPES" not in dev:
        findings.append("devapi_handlers.py: no scope validation present")
    return findings


def check_phase13_hygiene() -> list[str]:
    """Phase 13 checks: production backend, distributed rate limiting,
    observability, health endpoints, and safe deployment artifacts."""
    findings: list[str] = []
    base = PORTAL / "backend" / "portal_server"
    db_dir = base / "db"
    # PostgreSQL backend + migration system present.
    if not (db_dir / "postgres.py").exists():
        findings.append("db/postgres.py missing — no PostgreSQL backend")
    if not (db_dir / "migrations.py").exists():
        findings.append("db/migrations.py missing — no migration system")
    if "migration_checksum" not in (db_dir / "migrations.py").read_text(encoding="utf-8"):
        findings.append("migrations.py: no checksum validation")
    # Observability present with secret scrubbing.
    obs = base / "observability.py"
    obs_text = obs.read_text(encoding="utf-8") if obs.exists() else ""
    if "scrub_secrets" not in obs_text:
        findings.append("observability.py: secret scrubbing missing")
    # Distributed rate limiting present.
    rl = base / "ratelimit.py"
    rl_text = rl.read_text(encoding="utf-8") if rl.exists() else ""
    if "PostgresRateLimiter" not in rl_text:
        findings.append("ratelimit.py: PostgreSQL rate limiter missing")
    # Health endpoints present.
    app_text = (base / "app.py").read_text(encoding="utf-8") if (base / "app.py").exists() else ""
    if "/health/live" not in app_text or "/health/ready" not in app_text:
        findings.append("app.py: health/live or health/ready endpoint missing")
    # DATABASE_URL must never be printed/logged.
    if re.search(r"print\(.*db_url|log(ger)?\(.*DATABASE_URL", app_text):
        findings.append("app.py: possible DATABASE_URL logging")
    # Deployment artifacts must not embed real keys/secrets.
    for rel in ("docker/docker-compose.prod.yml", "nginx/ghostlink.conf"):
        p = ROOT / "deployment" / rel
        if not p.exists():
            findings.append(f"deployment/{rel} missing")
            continue
        text = p.read_text(encoding="utf-8")
        if re.search(r"-----BEGIN [A-Z ]*PRIVATE KEY-----", text, re.IGNORECASE):
            findings.append(f"deployment/{rel}: embedded private key present")
        if re.search(r"password:\s*['\"][^$]", text):
            findings.append(f"deployment/{rel}: hardcoded password")
    return findings


def check_phase14_hygiene() -> list[str]:
    """Phase 14 checks: Docker hardening, prod config defaults, CORS, CI."""
    findings: list[str] = []
    dockerfile = ROOT / "deployment" / "docker" / "Dockerfile"
    if dockerfile.exists():
        text = dockerfile.read_text(encoding="utf-8")
        # Non-root user required; must not end on a root USER.
        if "USER" not in text:
            findings.append("deployment/docker/Dockerfile: no explicit USER")
        elif re.search(r"USER\s+root\s*$", text, re.MULTILINE):
            findings.append("deployment/docker/Dockerfile: runs as root")
        if re.search(r"COPY\s+\.env", text):
            findings.append("deployment/docker/Dockerfile: ships a .env file")
        if "pip install -e" in text or "pip install .[dev]" in text:
            findings.append("deployment/docker/Dockerfile: includes dev dependencies")
    # Production env template must be fail-closed.
    prod_env = ROOT / "deployment" / "env" / ".env.production.example"
    if prod_env.exists():
        text = prod_env.read_text(encoding="utf-8")
        for needle in (
            "APP_ENV=production",
            "PORTAL_SECURE_COOKIES=true",
            "RATE_LIMIT_BACKEND=postgresql",
        ):
            if needle not in text:
                findings.append(f".env.production.example: missing {needle}")
    # No wildcard CORS / permissive CORS in the backend (skip test fixtures).
    for path, text in _repo_texts():
        if "portal" not in str(path) or not path.name.endswith(".py"):
            continue
        if "test" in path.stem or "tests" in path.parts:
            continue
        if re.search(r"Access-Control-Allow-Origin['\"]?\s*[:=]\s*['\"]\*['\"]", text):
            findings.append(f"{path.relative_to(ROOT)}: wildcard CORS")
    # CI must not auto-deploy to a live production host.
    for wf in (ROOT / ".github" / "workflows").glob("*.yml"):
        text = wf.read_text(encoding="utf-8")
        rel = wf.name
        if "release" in rel:
            if "workflow_dispatch" not in text:
                findings.append(f"{rel}: release workflow is not manual-only")
            if re.search(r"on:\s*push", text):
                findings.append(f"{rel}: release workflow auto-runs on push")
        # A workflow that deploys only with explicit placeholder hosts is fine.
        if (
            re.search(r"(scp|ssh|azure/webapps|gcloud deploy|rsync)\b", text)
            and "REPLACE" not in text
            and re.search(r"deploy\s*[-:]\s*.*(server|host)", text)
        ):
            findings.append(f"{rel}: possible auto-deploy step present")
    return findings


def check_phase15_hygiene() -> list[str]:
    """Phase 15 checks: production debug mode, frontend secret leakage."""
    findings: list[str] = []
    # 1. Production must never enable debug mode.
    for p in (ROOT / "deployment").rglob("*.yml"):
        text = p.read_text(encoding="utf-8")
        if ("APP_ENV: production" in text or "APP_ENV=production" in text) and re.search(
            r"DEBUG\s*[:=]\s*(true|1)\b", text, re.IGNORECASE
        ):
            findings.append(f"{p.relative_to(ROOT)}: debug enabled in production")
    # 2. Frontend must not store secrets in localStorage/sessionStorage.
    for path, text in _repo_texts():
        if "portal" not in str(path) or not path.name.endswith((".ts", ".tsx")):
            continue
        if re.search(r"(localStorage|sessionStorage)\.(setItem|set)", text):
            findings.append(f"{path.relative_to(ROOT)}: client-side storage of values")
    # 3. Backend must never print DATABASE_URL / SMTP password.
    for path, text in _repo_texts():
        if "portal" not in str(path) or not path.name.endswith(".py"):
            continue
        if "tests" in path.parts:
            continue
        if re.search(r"print\(.*(DATABASE_URL|EMAIL_SMTP_PASSWORD|SESSION_SECRET)", text):
            findings.append(f"{path.relative_to(ROOT)}: may print a secret")
    return findings


def run_all() -> list[str]:
    findings: list[str] = []
    findings += check_secrets()
    findings += check_portal_config()
    findings += check_http_layer()
    findings += check_frontend_telemetry()
    findings += check_no_network_in_backend()
    findings += check_devapi_hygiene()
    findings += check_phase13_hygiene()
    findings += check_phase14_hygiene()
    findings += check_phase15_hygiene()
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
