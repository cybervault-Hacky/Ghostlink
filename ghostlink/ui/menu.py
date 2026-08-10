"""Interactive selection menu.

On a real terminal the menu is keyboard-driven (arrow keys / vim keys via the
optional ``simple-term-menu`` dependency). When input is piped or the
dependency is unavailable, it degrades to a numbered prompt with identical
semantics — scripts and CI can drive the application end-to-end either way.
"""

from __future__ import annotations

import importlib.util
import sys
import threading
from dataclasses import dataclass

from rich.table import Table
from rich.text import Text

from ghostlink.core.logging import get_logger
from ghostlink.ui.console import ConsoleManager

_KEYBOARD_CURSOR_HINT = "↑/↓ or j/k to move · Enter to select · q to quit"
_NUMBERED_CURSOR_HINT = "Type a number and press Enter"


@dataclass(frozen=True, slots=True)
class MenuEntry:
    """One selectable menu option."""

    key: str
    label: str
    description: str
    icon: str


class InteractiveMenu:
    """Prompts the user to choose one of several :class:`MenuEntry` items."""

    def __init__(self, console: ConsoleManager) -> None:
        self._console = console
        self._logger = get_logger("ui.menu")

    @property
    def keyboard_driven(self) -> bool:
        """True when arrow-key navigation can be used in this session."""

        return (
            sys.stdin.isatty()
            and sys.stdout.isatty()
            and importlib.util.find_spec("simple_term_menu") is not None
        )

    @property
    def interaction_hint(self) -> str:
        return _KEYBOARD_CURSOR_HINT if self.keyboard_driven else _NUMBERED_CURSOR_HINT

    # ------------------------------------------------------------------ prompt

    def prompt(
        self,
        entries: tuple[MenuEntry, ...] | list[MenuEntry],
        *,
        default_key: str,
    ) -> str:
        """Return the key of the chosen entry, or ``default_key`` on quit."""

        if not entries:
            raise ValueError("prompt() requires at least one menu entry")
        if self.keyboard_driven:
            return self._prompt_keyboard(tuple(entries), default_key=default_key)
        return self._prompt_numbered(tuple(entries), default_key=default_key)

    # ---------------------------------------------------------------- keyboard

    def _prompt_keyboard(
        self,
        entries: tuple[MenuEntry, ...],
        *,
        default_key: str,
    ) -> str:
        # simple-term-menu calls signal.signal(SIGWINCH, ...) during ``show``.
        # CPython only allows signal handlers to be registered from the main
        # thread of the main interpreter; doing so from a worker thread raises
        # ``ValueError: signal only works in main thread of the main
        # interpreter`` (the crash reported on Termux/Python 3.11 when the menu
        # was driven through ``asyncio.to_thread``). Fail fast with a clear
        # developer-facing error instead of letting the third-party library
        # surface the cryptic signal error at runtime.
        if threading.current_thread() is not threading.main_thread():
            raise RuntimeError(
                "InteractiveMenu keyboard prompt must run on the main thread; "
                "simple-term-menu installs signal handlers and cannot run in a "
                "worker thread. Call prompt() synchronously instead of wrapping "
                "it in asyncio.to_thread/run_in_executor."
            )

        from simple_term_menu import TerminalMenu

        titles = [f"{entry.icon}   {entry.label}" for entry in entries]
        menu = TerminalMenu(
            titles,
            menu_cursor="❯ ",
            menu_cursor_style=("fg_cyan", "bold"),
            menu_highlight_style=("fg_cyan", "bold"),
            cycle_cursor=True,
            clear_screen=False,
            clear_menu_on_exit=False,
            show_search_hint=False,
        )
        chosen = menu.show()
        if chosen is None or not isinstance(chosen, int):
            self._logger.debug("menu dismissed; selecting default '%s'", default_key)
            return default_key
        return entries[chosen].key

    # ---------------------------------------------------------------- numbered

    def render_legend(self, entries: tuple[MenuEntry, ...]) -> None:
        """Render the entries table (also useful for embedding in layouts)."""
        table = Table(box=None, show_header=False, show_edge=False, pad_edge=False)
        table.add_column(no_wrap=True, style="gl.accent", justify="right")
        table.add_column(no_wrap=True)
        table.add_column(no_wrap=True, style="gl.title")
        table.add_column(style="gl.muted")
        for index, entry in enumerate(entries, start=1):
            table.add_row(str(index), entry.icon, entry.label, entry.description)
        self._console.print(table)

    def _prompt_numbered(
        self,
        entries: tuple[MenuEntry, ...],
        *,
        default_key: str,
    ) -> str:
        self.render_legend(entries)
        self._console.newline()
        while True:
            try:
                raw = self._console.console.input(Text.assemble(("  ❯ ", "gl.highlight"))).strip()
            except EOFError:
                self._logger.debug("stdin closed; selecting default '%s'", default_key)
                return default_key
            if raw.lower() in {"q", "quit", "exit"}:
                return default_key
            if raw.isdigit():
                index = int(raw)
                if 1 <= index <= len(entries):
                    return entries[index - 1].key
            self._console.print(
                Text(f"  Enter a number between 1 and {len(entries)}.", style="gl.warning")
            )
