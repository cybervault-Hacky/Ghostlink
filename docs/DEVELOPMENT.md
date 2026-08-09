# GhostLink — Developer Guide

## Environment setup

```bash
git clone https://github.com/cybervault-Hacky/Ghostlink.git
cd Ghostlink

python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

On Termux use `pkg install python git` first, then the same steps (skip the
venv if you prefer Termux's system site-packages).

## Daily commands

| Task | Command |
| --- | --- |
| Run the app | `python ghostlink.py` (or `ghostlink` when installed) |
| Diagnose environment | `python ghostlink.py --doctor` |
| Security & recovery summary | `python ghostlink.py security-status` |
| Full quality gate | `scripts/dev_check.sh` |
| Release-candidate gate | `scripts/release_check.sh` |
| Secret scan | `python scripts/scan_secrets.py` |
| Tests only | `pytest tests/` |
| Lint / format | `ruff check .` / `ruff format .` |
| Regenerate doc images | `python scripts/generate_screenshots.py` |

`dev_check.sh` runs byte-compilation, Ruff lint, Ruff format verification,
the test suite, and `--version` / `--doctor` smoke checks — the same gate CI
should run.

## Quality standards (enforced)

- **Type hints everywhere.** `mypy --strict` configuration ships in
  `pyproject.toml`; public functions are fully annotated.
- **No global mutable state.** Environment, paths, and consoles are injected.
- **Errors carry context.** Raise `GhostLinkError` subclasses with a message
  *and* an actionable hint; never let bare `OSError`/`KeyError` reach the UI.
- **Formatting is mechanical.** Ruff (line length 100) decides, not taste.
- **`from __future__ import annotations`** at the top of every module.

## Writing a new screen

```python
class ProfileScreen(Screen):
    async def show(self) -> None:
        context = self.context
        context.console.clear()
        context.console.print(section_panel("Profile", info_table(self._rows())))
        await self.pause()
```

- Take dependencies from `ScreenContext`; add new ones in
  `core/bootstrap.py` and register them in the `ServiceContainer`.
- Use `ui.components` primitives — never hand-assemble panel chrome.
- Blocking input goes through `asyncio.to_thread` and, ideally,
  `self.pause()` / `InteractiveMenu`.

## Adding a setting

1. Add the key to `ALLOWED_SCHEMA` in `ghostlink/config/loader.py`.
2. Add the field (with validation) to the right dataclass in
   `ghostlink/models/settings.py`.
3. Document it in `ghostlink/assets/default_config.toml` *and*
   `docs/CONFIGURATION.md`.
4. Surface it on the Settings screen if user-facing.

## Adding a storage namespace

```python
store = storage_manager.store("rooms")  # lowercase, [a-z0-9_-]
store["last_room"] = "gl-invite-…"
```

Namespaces map to `<data_dir>/state/<namespace>.json` with atomic writes and
`0600` permissions. Keep values JSON-compatible.

## Test conventions

- Mirrors the package layout: `test_config_manager.py` tests
  `config/manager.py`, and so on.
- The `isolated_home` fixture redirects `HOME`/XDG paths — always use it for
  anything touching the filesystem.
- Drive the UI headlessly by monkeypatching `InteractiveMenu.prompt` and
  `Screen.pause` (see `tests/test_application.py`).
- Assert on rendered output through `ConsoleManager(record=True)` +
  `export_text()`.
- Networking tests run **real loopback sockets**, never mocks: the
  `running_relay` async context manager starts a `RelayServer` on an
  ephemeral port inside the test loop; `threaded_relay` does the same from a
  background thread for synchronous CLI tests; `run(coro)` drives coroutines
  from sync test bodies (see `tests/conftest.py`).
- Phase 8 adds deterministic fuzz/property tests (`test_fuzz_properties.py`,
  fixed seeds, fail-closed only), recovery-coordinator tests
  (`test_recovery.py`), log-hygiene tests (`test_log_hygiene.py`),
  crash-consistency tests (`test_crash_consistency.py`), and adversarial
  loopback tests (`test_groups_adversarial_e2e.py`, `test_relay_abuse.py`).
- `filterwarnings = ["error"]` is part of the contract: transports, tasks,
  and sockets must be torn down deterministically — a leaked socket or
  pending task fails the suite.

## Releasing

`scripts/release_check.sh` is the release-candidate gate. It requires a
clean git tree and then verifies, in order: version synchronization,
byte-compilation, Ruff lint, Ruff format, `mypy --strict`, the full pytest
suite, a wheel+sdist build, archive inspection (no junk/secrets), a
clean-install CLI smoke, a repository secret scan, and a banned security-
claim scan. It is fully local and offline.

`schema migration` — config and state documents carry an explicit schema
version (`ghostlink/core/migration.py`). When you change the on-disk shape
of any persisted document, bump the relevant `*_SCHEMA_VERSION`, write a
forward-migration path, and add tests. GhostLink fails closed (rejects,
never reinterprets) on documents from a newer version.
