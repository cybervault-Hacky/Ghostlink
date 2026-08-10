"""Regression tests for the Termux/Python 3.11 interactive-menu crash.

simple-term-menu installs a ``SIGWINCH`` handler inside ``TerminalMenu.show``.
CPython only allows signal handlers to be registered from the main thread of
the main interpreter, so running the prompt through ``asyncio.to_thread`` (or
any executor worker) raised::

    ValueError: signal only works in main thread of the main interpreter

These tests pin the contract: the keyboard menu may only be driven from the
main thread, and :class:`~ghostlink.ui.screens.home.HomeScreen` keeps the
prompt on that thread rather than delegating it to a worker.
"""

from __future__ import annotations

import asyncio
import inspect
import sys
import threading
import types
from pathlib import Path

import pytest

from ghostlink.ui.menu import InteractiveMenu, MenuEntry
from ghostlink.ui.screens import home as home_screen_module
from ghostlink.ui.screens.home import HomeScreen

ENTRIES = (
    MenuEntry(key="one", label="First", description="first option", icon="1"),
    MenuEntry(key="exit", label="Exit", description="leave", icon="x"),
)


def _force_keyboard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pretend we are attached to an interactive terminal with stm installed."""

    monkeypatch.setattr(InteractiveMenu, "keyboard_driven", property(lambda self: True))


class TestKeyboardMenuRunsOnMainThread:
    def test_prompt_keyboard_rejects_worker_thread(
        self,
        console_manager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        _force_keyboard(monkeypatch)
        menu = InteractiveMenu(console_manager)

        result: dict[str, object] = {}

        def _worker() -> None:
            try:
                menu.prompt(ENTRIES, default_key="exit")
            except BaseException as exc:  # pragma: no cover - captured below
                result["exc"] = exc

        thread = threading.Thread(target=_worker, daemon=True)
        thread.start()
        thread.join(timeout=5)
        assert not thread.is_alive(), "worker thread did not finish"

        assert isinstance(result.get("exc"), RuntimeError)
        assert "main thread" in str(result["exc"])

    def test_prompt_keyboard_runs_on_main_thread(
        self,
        console_manager,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """On the main thread the guard passes and control reaches stm.show()."""

        _force_keyboard(monkeypatch)
        calls: dict[str, object] = {}

        class _FakeTerminalMenu:
            def __init__(self, titles: object, **kwargs: object) -> None:
                calls["titles"] = titles
                calls["kwargs"] = kwargs

            def show(self) -> int:
                calls["thread"] = threading.current_thread()
                return 0  # select the first entry

        fake_module = types.ModuleType("simple_term_menu")
        fake_module.TerminalMenu = _FakeTerminalMenu  # type: ignore[attr-defined]
        monkeypatch.setitem(sys.modules, "simple_term_menu", fake_module)

        menu = InteractiveMenu(console_manager)
        assert menu.prompt(ENTRIES, default_key="exit") == "one"
        assert calls["thread"] is threading.main_thread()


class TestHomeScreenKeepsMenuOnMainThread:
    def test_show_does_not_use_to_thread_for_menu(self) -> None:
        """The exact regression: home.py must not wrap the menu in to_thread."""

        source = inspect.getsource(HomeScreen.show)
        assert "to_thread" not in source, (
            "HomeScreen.show must call self._menu.prompt synchronously; "
            "simple-term-menu requires the main thread for signal handling."
        )
        assert "run_in_executor" not in source
        assert "self._menu.prompt(" in source
        assert "await self._menu.prompt" not in source

    def test_home_screen_prompt_invoked_on_main_thread(
        self,
        isolated_home: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Drive HomeScreen.show far enough to prove the prompt thread is main.

        A headless smoke test: we don't need a real TTY — we replace
        ``InteractiveMenu.prompt`` with a probe that records its thread and
        returns "exit" immediately, then confirm the prompt ran on the main
        thread even though ``show`` itself is an ``asyncio.run`` coroutine.
        This is the failure mode from the bug report: previously the prompt was
        scheduled via ``asyncio.to_thread`` and ran on a worker thread.
        """

        observed: dict[str, threading.Thread] = {}
        real_prompt = InteractiveMenu.prompt

        def _recording_prompt(
            self: InteractiveMenu,
            entries: tuple[MenuEntry, ...],
            *,
            default_key: str,
        ) -> str:
            observed["thread"] = threading.current_thread()
            return "exit"

        monkeypatch.setattr(InteractiveMenu, "prompt", _recording_prompt)

        from ghostlink.cli.arguments import parse_args
        from ghostlink.core.bootstrap import build_application

        application = build_application(parse_args([]))

        # Reproduce the production entrypoint: asyncio.run on the main thread.
        asyncio.run(HomeScreen(application._context).show())

        assert observed.get("thread") is threading.main_thread(), (
            f"InteractiveMenu.prompt ran on {observed.get('thread')!r}, "
            "expected the main interpreter thread"
        )
        assert "thread" in observed
        assert real_prompt is not None  # keep reference used

    def test_no_simple_term_menu_off_thread_in_repo(self) -> None:
        """No source file may wrap a simple-term-menu prompt in a worker."""

        root = Path(home_screen_module.__file__).resolve().parent.parent
        offenders: list[str] = []
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            is_menu_file = path.name == "menu.py"
            references_menu = "simple_term_menu" in text or "TerminalMenu" in text
            uses_worker = "to_thread" in text or "run_in_executor" in text
            if not is_menu_file and references_menu and uses_worker:
                # Distinguish unrelated to_thread usage (file hashes, getpass)
                # from an actual menu call dispatched to a worker thread.
                for line in text.splitlines():
                    stripped = line.strip()
                    if "to_thread" in stripped and (
                        "menu" in stripped.lower() or "prompt" in stripped.lower()
                    ):
                        offenders.append(f"{path}: {stripped}")
        assert not offenders, f"menu prompt must not run in a worker thread: {offenders}"
