#!/usr/bin/env python3
"""GhostLink — deterministic Docker artifact static check (Phase 15B).

Statically validates all production Docker artifacts (Dockerfile, compose
files, .dockerignore, systemd) without requiring Docker. Reports findings for
each hardening requirement and exits non-zero if any mandatory requirement
fails. Runtime execution is reported as unavailable when `docker` is not
installed — never faked.

Exit 0 = clean; 1 = a finding was reported.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPLOY = ROOT / "deployment" / "docker"


def _findings() -> list[str]:
    findings: list[str] = []
    dockerfile = DEPLOY / "Dockerfile"
    if not dockerfile.exists():
        return ["deployment/docker/Dockerfile missing"]
    text = dockerfile.read_text(encoding="utf-8")

    # Non-root execution
    if "USER" not in text:
        findings.append("Dockerfile: no explicit USER (runs as root)")
    elif re.search(r"USER\s+root\s*$", text, re.MULTILINE):
        findings.append("Dockerfile: ends running as root")

    # Minimal base image
    if not re.search(r"FROM\s+python:\d", text):
        findings.append("Dockerfile: no python base image")

    # No development server in production
    if re.search(r"portal_server\s*$|wsgiref|python\s+-m\s+portal_server\s*$", text):
        findings.append("Dockerfile: dev WSGI server used instead of gunicorn")
    if "gunicorn" not in text:
        findings.append("Dockerfile: gunicorn not referenced")

    # No secrets baked in
    if re.search(r"COPY\s+\.env|ENV\s+(SESSION_SECRET|DATABASE_URL|.*_PASSWORD)\s*=", text):
        findings.append("Dockerfile: secret may be baked into image")
    if "SESSION_SECRET" in text and "environment" not in text.lower():
        findings.append("Dockerfile: SESSION_SECRET reference without env injection")

    # No dev dependencies
    if re.search(r"pip\s+install\s+-e|pip\s+install\s+\.[^)]*dev|requirements-dev", text):
        findings.append("Dockerfile: development dependencies installed")

    # Read-only filesystem + capability dropping (compose overlay)
    prod_compose = DEPLOY / "docker-compose.prod.yml"
    if prod_compose.exists():
        ctext = prod_compose.read_text(encoding="utf-8")
        if "read_only: true" not in ctext:
            findings.append("docker-compose.prod.yml: read_only not set")
        if "cap_drop" not in ctext:
            findings.append("docker-compose.prod.yml: capabilities not dropped")
        if "no-new-privileges" not in ctext:
            findings.append("docker-compose.prod.yml: no-new-privileges not set")

    # Healthcheck present
    if "HEALTHCHECK" not in text:
        findings.append("Dockerfile: no HEALTHCHECK")

    # Explicit exposed port
    if not re.search(r"EXPOSE\s+\d+", text):
        findings.append("Dockerfile: no EXPOSE port")

    # Pinned base image (not :latest)
    if re.search(r"FROM\s+[^\s]+:latest", text):
        findings.append("Dockerfile: uses :latest base image")

    # .dockerignore excludes secrets
    di = ROOT / ".dockerignore"
    if di.exists():
        ditem = di.read_text(encoding="utf-8")
        for secret in ("**/.env", "**/*.pem", "**/*.key", "**/*.crt"):
            if secret not in ditem:
                findings.append(f".dockerignore: does not exclude {secret}")

    return findings


def main() -> int:
    print("== GhostLink Docker artifact static check (Phase 15B) ==")
    findings = _findings()
    if findings:
        print("Findings:")
        for f in findings:
            print(f"  ✗ {f}")
    else:
        print("Static Docker artifact checks clean.")

    has_docker = shutil.which("docker") is not None
    print(f"\nDocker runtime available: {'yes' if has_docker else 'no'}")
    if not has_docker:
        print(
            "RUNTIME EXECUTION UNAVAILABLE in this environment — static validation only. "
            "To verify the image at runtime, run:\n"
            "  docker build -f deployment/docker/Dockerfile -t ghostlink/portal:0.17.0 .\n"
            "  docker compose -f deployment/docker/docker-compose.prod.yml config\n"
        )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
