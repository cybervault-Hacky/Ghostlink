"""Shared fixtures for the GhostLink test suite."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import AsyncIterator, Coroutine
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, TypeVar

import pytest

from ghostlink.models.environment import (
    ColorSupport,
    EnvironmentInfo,
    PlatformKind,
)
from ghostlink.models.settings import AppSettings
from ghostlink.models.theme import ThemeSpec
from ghostlink.transport.relay.server import RelayServer
from ghostlink.ui.console import ConsoleManager
from ghostlink.ui.themes import ThemeEngine

T = TypeVar("T")


@pytest.fixture()
def theme() -> ThemeSpec:
    return ThemeEngine().get("phantom")


@pytest.fixture()
def console_manager(theme: ThemeSpec) -> ConsoleManager:
    """A recording console with fixed geometry for deterministic assertions."""

    return ConsoleManager(theme, record=True, width=100)


@pytest.fixture()
def environment_info() -> EnvironmentInfo:
    """A canonical supported-Linux environment snapshot."""

    return EnvironmentInfo(
        platform=PlatformKind.LINUX,
        system="Linux",
        release="6.8.0-test",
        machine="x86_64",
        python_version="3.12.4",
        python_version_info=(3, 12, 4),
        python_implementation="CPython",
        terminal_columns=100,
        terminal_rows=30,
        stdin_is_tty=False,
        stdout_is_tty=False,
        color_support=ColorSupport.TRUECOLOR,
    )


@pytest.fixture()
def app_settings() -> AppSettings:
    return AppSettings.defaults()


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect HOME and XDG dirs into a temporary location."""

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


# --------------------------------------------------------------------- async


def run(coroutine: Coroutine[Any, Any, T]) -> T:
    """Drive a coroutine to completion on a fresh loop (sync test bodies)."""

    return asyncio.run(coroutine)


@asynccontextmanager
async def running_relay(
    **server_kwargs: Any,
) -> AsyncIterator[RelayServer]:
    """Start a :class:`RelayServer` in the current loop on an ephemeral port."""

    server = RelayServer(host="127.0.0.1", port=0, **server_kwargs)
    await server.start()
    try:
        yield server
    finally:
        await server.aclose()


class ThreadedRelay:
    """A relay running in a dedicated thread — usable from sync CLI tests."""

    def __init__(self, **server_kwargs: Any) -> None:
        self._kwargs = server_kwargs
        self._ready = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: RelayServer | None = None
        self._thread: threading.Thread | None = None
        self.url = ""

    def __enter__(self) -> ThreadedRelay:
        self._thread = threading.Thread(target=self._drive, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            raise TimeoutError("threaded relay did not start in time")
        return self

    def __exit__(self, *exc_info: object) -> None:
        loop = self._loop
        server = self._server
        if loop is not None and server is not None:
            future = asyncio.run_coroutine_threadsafe(server.aclose(), loop)
            future.result(timeout=10)
            loop.call_soon_threadsafe(loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=10)
        if loop is not None:
            loop.close()

    def _drive(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        server = RelayServer(host="127.0.0.1", port=0, **self._kwargs)

        async def _start() -> None:
            await server.start()
            self.url = server.url

        loop.run_until_complete(_start())
        self._loop = loop
        self._server = server
        self._ready.set()
        loop.run_forever()


@pytest.fixture()
def threaded_relay() -> Any:
    """Yield factory producing :class:`ThreadedRelay` context managers."""

    def _factory(**kwargs: Any) -> ThreadedRelay:
        return ThreadedRelay(**kwargs)

    return _factory
