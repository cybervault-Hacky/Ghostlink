"""Packaging integrity: version sync, packaged assets, and metadata."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

import ghostlink
from ghostlink.assets.branding import load_default_config_text
from ghostlink.constants.app import APP_VERSION

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class TestVersionSync:
    def test_pyproject_matches_package_version(self) -> None:
        pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert pyproject["project"]["version"] == ghostlink.__version__
        assert ghostlink.__version__ == APP_VERSION

    def test_version_info_matches(self) -> None:
        assert ghostlink.version_info == tuple(
            int(part) for part in ghostlink.__version__.split(".")
        )


class TestPackagedAssets:
    def test_default_config_resource_is_available(self) -> None:
        text = load_default_config_text()
        assert "[ui]" in text
        assert "[diagnostics]" in text

    def test_default_config_parses_as_toml(self) -> None:
        parsed = tomllib.loads(load_default_config_text())
        assert parsed["ui"]["theme"] == "phantom"
        assert parsed["diagnostics"]["debug"] is False

    def test_default_config_carries_meta_version(self) -> None:
        parsed = tomllib.loads(load_default_config_text())
        assert parsed["meta"]["config_version"] == 1


class TestPythonCompatibility:
    """The declared minimum is Python 3.11 (verified green on 3.11.2)."""

    def test_requires_python_is_3_11(self) -> None:
        pyproject = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
        assert pyproject["project"]["requires-python"] == ">=3.11"

    def test_min_python_matches(self) -> None:
        from ghostlink.constants.app import MIN_PYTHON

        assert MIN_PYTHON == (3, 11)


class TestSecretScan:
    """The release secret scan is clean — run as part of the suite."""

    def test_secret_scan_is_clean(self) -> None:
        import subprocess

        result = subprocess.run(
            [sys.executable, str(PROJECT_ROOT / "scripts" / "scan_secrets.py")],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, f"secret scan failed:\n{result.stdout}\n{result.stderr}"


class TestBannedClaims:
    def test_no_banned_security_claims_in_docs(self) -> None:
        import re

        # Strong affirmative overclaims the project must never make. Negated
        # phrases ("not invisible", "not ... perfect security") are honest
        # disclaimers and are intentionally NOT matched.
        banned = re.compile(
            r"100% secure|impossible to hack|impossible to break|"
            r"mathematically unhackable|immune to compromise",
            re.IGNORECASE,
        )
        for doc in PROJECT_ROOT.joinpath("docs").glob("*.md"):
            text = doc.read_text(encoding="utf-8")
            for line in text.splitlines():
                if banned.search(line):
                    raise AssertionError(f"banned security claim in {doc.name}: {line.strip()}")
