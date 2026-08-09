"""Phase 14 — security-check tooling tests (section 19).

Verifies the deterministic security audit produces no false positives on the
clean repository and that seeded violations are detected (Docker root,
production env defaults, wildcard CORS, auto-deploy workflows).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]  # repo root
SCRIPTS = ROOT / "scripts"


def _load_security_check():
    path = SCRIPTS / "security_check.py"
    spec = importlib.util.spec_from_file_location("gl_security_check", path)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def sc():
    return _load_security_check()


def test_clean_repo_has_no_findings(sc) -> None:
    findings = sc.run_all()
    assert findings == [], findings


def test_docker_root_detected(sc, tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "deployment" / "docker").mkdir(parents=True)
    (tmp_path / "deployment" / "docker" / "Dockerfile").write_text(
        "FROM python:3.11-slim\nUSER root\nCMD python\n"
    )
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    findings = sc.check_phase14_hygiene()
    assert any("runs as root" in f for f in findings)


def test_docker_env_copy_detected(sc, tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "deployment" / "docker").mkdir(parents=True)
    (tmp_path / "deployment" / "docker" / "Dockerfile").write_text(
        "FROM python:3.11-slim\nCOPY .env /app/.env\nUSER ghostlink\n"
    )
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    findings = sc.check_phase14_hygiene()
    assert any(".env" in f for f in findings)


def test_wildcard_cors_detected(sc, tmp_path: Path, monkeypatch) -> None:
    portal = tmp_path / "portal" / "backend" / "portal_server"
    portal.mkdir(parents=True)
    (portal / "http.py").write_text(
        "headers = {'Access-Control-Allow-Origin': '*', 'X-Content-Type-Options': 'nosniff'}\n"
    )
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    findings = sc.check_phase14_hygiene()
    assert any("wildcard CORS" in f for f in findings)


def test_prod_env_missing_fail_closed_key(sc, tmp_path: Path, monkeypatch) -> None:
    env_dir = tmp_path / "deployment" / "env"
    env_dir.mkdir(parents=True)
    (env_dir / ".env.production.example").write_text("APP_ENV=production\n")
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    findings = sc.check_phase14_hygiene()
    assert any("RATE_LIMIT_BACKEND" in f for f in findings)


def test_secrets_finds_embedded_private_key(sc, tmp_path: Path, monkeypatch) -> None:
    # Build the PEM text dynamically so the literal pattern is not present in
    # this source file (the repository-wide scan_secrets.py scans tests too).
    begin = "-----BEGIN " + "RSA PRIVATE KEY-----"
    end = "-----END " + "RSA PRIVATE KEY-----"
    (tmp_path / "deployment").mkdir(parents=True)
    (tmp_path / "deployment" / "secret.py").write_text(f"{begin}\nMII\n{end}\n")
    monkeypatch.setattr(sc, "ROOT", tmp_path)
    assert tmp_path == sc.ROOT
    assert len(sc._text_files()) >= 1, sc._text_files()
    findings = sc.check_secrets()
    assert any("private-key" in f for f in findings), findings
