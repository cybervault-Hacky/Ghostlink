"""Phase 15D — secret-scanner detection tests with dynamic fixtures.

Uses dynamically generated secret-shaped strings so the repository's own
secret scan never sees a literal fake credential. Verifies the scanner flags
real-looking secrets in committed source while ignoring placeholders, example
files, and test fixtures.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"


@pytest.fixture(scope="module")
def scanmod():
    spec = importlib.util.spec_from_file_location("gl_scan", SCRIPTS / "scan_secrets.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scanner_flags_pairing_code(scanmod, tmp_path: Path) -> None:
    code = "GL-" + "A" * 20
    p = tmp_path / "src.py"
    p.write_text(f"code = {code!r}\n")
    assert scanmod._PAIRING_CODE.search(f"code = {code!r}")


def test_scanner_flags_developer_credential(scanmod) -> None:
    cred = "dk_" + "abc" + ":" + "x" * 24
    assert scanmod._DEV_CRED.search(f"token={cred!r}")


def test_scanner_flags_bearer_and_jwt(scanmod) -> None:
    assert scanmod._BEARER.search("Authorization: Bearer " + "a" * 50)
    jwt = (
        "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0"
        ".dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    )
    assert scanmod._JWT.search(f"t = {jwt!r}")


def test_scanner_flags_assigned_secret_and_db_password(scanmod) -> None:
    assert scanmod._ASSIGNED_SECRET.search("SESSION_SECRET = 'somethinglong'")
    assert scanmod._DBURL_PASSWORD.search("url = 'postgresql://user:supersecret@db:5432/ghostlink'")


def test_scanner_ignores_placeholders(scanmod) -> None:
    assert scanmod._looks_secretish("SESSION_SECRET = 'CHANGE_ME'") is False
    assert scanmod._looks_secretish("EMAIL_SMTP_PASSWORD = 'CHANGE_ME'") is False
    assert scanmod._looks_secretish("url = 'postgresql://u:CHANGE_ME@host/db'") is False


def test_is_example_detection(scanmod, tmp_path: Path) -> None:
    assert scanmod._is_example(Path(".env.production.example"))
    assert scanmod._is_example(Path("foo.example"))
    assert not scanmod._is_example(Path("app.py"))


def test_repo_is_clean(scanmod) -> None:
    # The real repository scan must be clean (no committed secrets).
    rc = scanmod.scan()
    assert rc == 0
