"""Headless pseudo-TTY smoke test for the interactive GhostLink menu.

Spawns ``ghostlink`` under a PTY (so simple-term-menu takes its real
keyboard-driven code path), waits for the menu to render, sends ``q`` to quit
cleanly, and verifies the process did not crash. Exits non-zero if the menu
fails to appear or the process dies from an unhandled exception.
"""

from __future__ import annotations

import os
import pty
import select
import signal
import sys
import time

WAIT_SECONDS = 10.0
MENU_MARKERS = ("Host a Room", "Join a Room", "Settings", "About")


def main() -> int:
    env = dict(os.environ)
    env.setdefault("TERM", "xterm-256color")
    env.setdefault("HOME", os.path.expanduser("~"))

    pid, fd = pty.fork()
    if pid == 0:
        # Child: exec ghostlink with no command -> interactive menu.
        os.execvpe("ghostlink", ["ghostlink"], env)

    output = bytearray()
    deadline = time.monotonic() + WAIT_SECONDS
    menu_seen = False
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            if not chunk:
                break
            output.extend(chunk)
            text = output.decode("utf-8", errors="ignore")
            if all(marker in text for marker in MENU_MARKERS):
                menu_seen = True
                break

    if not menu_seen:
        sys.stderr.write("Menu markers never appeared; captured output:\n")
        sys.stderr.write(output.decode("utf-8", errors="ignore")[-4000:])
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)
        return 1

    # The menu is open and stable. Send 'q' to quit (bound to exit).
    os.write(fd, b"q")

    exit_code = 1
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        ready, _, _ = select.select([fd], [], [], 0.2)
        if ready:
            try:
                chunk = os.read(fd, 65536)
            except OSError:
                chunk = b""
            if chunk:
                output.extend(chunk)
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
        except ChildProcessError:
            break
        if wpid == pid:
            exit_code = os.WEXITSTATUS(status) if os.WIFEXITED(status) else -os.WTERMSIG(status)
            break
    else:
        os.kill(pid, signal.SIGKILL)
        os.waitpid(pid, 0)

    final = output.decode("utf-8", errors="ignore")
    if "ValueError" in final and "signal only works" in final:
        sys.stderr.write("FAIL: original SIGWINCH ValueError reproduced\n")
        sys.stderr.write(final[-4000:])
        return 2
    if "Traceback" in final and "RuntimeError" in final:
        sys.stderr.write("FAIL: menu thread guard tripped at runtime\n")
        sys.stderr.write(final[-4000:])
        return 3

    print("MENU_OK" if exit_code == 0 else f"EXIT_{exit_code}")
    return 0 if exit_code == 0 else exit_code


if __name__ == "__main__":
    raise SystemExit(main())
