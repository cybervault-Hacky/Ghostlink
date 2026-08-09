# GhostLink — Compatibility Matrix

This document is the authoritative compatibility statement for GhostLink.
Every claim here is backed by the CI/test configuration and by the code
itself; nothing is asserted that has not been exercised.

## Supported platforms

| Platform | Status | Notes |
| --- | --- | --- |
| Termux (Android) | ✅ Supported | First-class target; see [README](../README.md) |
| Desktop Linux | ✅ Supported | POSIX, no root required |
| macOS / Windows / BSD | ⚠ Not targeted | No code blocks it deliberately, but they are not CI-tested |

GhostLink is **terminal-only**. There is no APK, Android Studio project,
Flutter, browser client, web app, GUI, or Electron build — and none is
planned.

## Supported Python versions

GhostLink targets **Python 3.11+**. The minimum is set by what the code
actually uses, not by aspiration:

* `pyproject.toml` declares `requires-python = ">=3.11"`.
* The ruff config keeps `UP042`/`UP047` disabled explicitly so the source
  also runs under the 3.11 interpreter (e.g. the Termux CI lint job).
* The full test suite runs green on Python 3.11.2 (see below).

| Python | Minimum | CI / tests | Status |
| --- | --- | --- | --- |
| 3.11 | required | Full suite runs green on 3.11.2 | ✅ Supported (tested) |
| 3.12 | supported | Canonical fixture target (3.12.4) | ✅ Supported |
| 3.13 | expected | Not separately CI-tested here | ⚠ Expected, unverified |
| 3.14 | expected | Depends on `cryptography`/`rich`/`simple-term-menu` wheels | ⚠ Expected, unverified |

The syntax/API features used are all 3.11-era or older: `tomllib`
(3.11+), `datetime.UTC` (3.11+), `ExceptionGroup` (3.11+), `typing.Self`
(3.11+), `StrEnum` (3.11+). No PEP 695 type-alias syntax is used, so the
source parses cleanly on 3.11.

## Runtime dependencies

| Dependency | Range (pyproject) | Python floor | Notes |
| --- | --- | --- | --- |
| `rich` | `>=13.7,<16` | 3.8+ | Terminal rendering engine |
| `simple-term-menu` | `>=1.6.6,<2.0` | 3.6+ | Arrow-key menus; degrades to numbered prompts |
| `cryptography` | `>=42,<47` | 3.7+ | X25519 / Ed25519 / HKDF-SHA256 / ChaCha20-Poly1305 |

All three are pure-Python (or ship manylinux/wheel builds) and install on
Termux without a C toolchain.

## Protocol compatibility

| Version | Meaning |
| --- | --- |
| `PROTOCOL_VERSION = 4` | Relay wire protocol (Phase 6B groups onward) |
| `SUPPORTED_PROTOCOL_VERSIONS = {1,2,3,4}` | Client/relay negotiation set |
| `GROUP_PROTOCOL_VERSION = 4` | Minimum for group operations |
| `INVITE_PROTOCOL_VERSION = 3` | Minimum for invite operations |

Protocol downgrade attempts and unsupported versions are rejected
deterministically (see `docs/GROUPS.md` §37 and the protocol tests); the
client never silently falls back to an insecure suite.

## Crypto suites

| Suite | Status | Notes |
| --- | --- | --- |
| `mesh-v1` | ✅ Default | Phase 6C pairwise-mesh encryption (backward compatible) |
| `senderkey-v1` | ✅ Available | Phase 7 O(1) sender-key group encryption |

Unknown suites fail closed; an established group never silently changes
suite (see `docs/GROUPS.md` §36-§37).

## How this is verified

* `scripts/dev_check.sh` runs the full quality gate.
* `scripts/release_check.sh` (Phase 9) additionally builds the package,
  inspects its contents, scans for secrets and banned security claims, and
  smoke-tests the CLI from a clean install.
* Deterministic fuzz/property tests cover the parsers (see
  `tests/test_fuzz_properties.py`).
