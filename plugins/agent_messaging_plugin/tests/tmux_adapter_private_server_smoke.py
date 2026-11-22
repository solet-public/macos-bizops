"""Private tmux server fixture and default-server isolation check for the real tier."""

from __future__ import annotations

import contextlib
import os
import shlex
import shutil
import subprocess
import tempfile
import time
from collections.abc import Callable, Generator
from pathlib import Path


def _private_tmux(tmp_dir: Path) -> str:
    real_tmux = shutil.which("tmux")
    assert real_tmux is not None
    wrapper = tmp_dir / "private-tmux"
    wrapper.write_text(f'#!/bin/sh\nexec {shlex.quote(real_tmux)} -S {shlex.quote(str(tmp_dir / "tmux.sock"))} "$@"\n')
    wrapper.chmod(0o755)
    return str(wrapper)


def session_on_default_server(host_ref: str) -> bool:
    real_tmux = shutil.which("tmux")
    assert real_tmux is not None
    return subprocess.run([real_tmux, "has-session", "-t", host_ref], capture_output=True, text=True, check=False).returncode == 0


def _default_server_sessions(tmux_bin: str, *, socket_name: str | None = None) -> list[str]:
    command = [tmux_bin]
    if socket_name is not None:
        command.extend(["-L", socket_name])
    command.extend(["list-sessions", "-F", "#{session_name}"])
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode == 1 and ("no server running" in result.stderr or (result.stderr.startswith("error connecting to ") and "(No such file or directory)" in result.stderr)):
        return []
    result.check_returncode()
    return result.stdout.splitlines()


@contextlib.contextmanager
def _private_tmux_server(tmp_dir: Path) -> Generator[str]:
    wrapper = _private_tmux(tmp_dir)
    try:
        yield wrapper
    finally:
        subprocess.run([wrapper, "kill-server"], capture_output=True, text=True, check=False)


def test_real_tier_leaves_the_default_server_alone(check: Callable[[bool, str], None]) -> None:
    tmux_bin = shutil.which("tmux")
    if tmux_bin is None:
        return
    absent_socket = f"smoke-absent-{os.getpid()}-{time.monotonic_ns() % 10**8}"
    check(_default_server_sessions(tmux_bin, socket_name=absent_socket) == [], "absent tmux server should have no sessions")
    before = _default_server_sessions(tmux_bin)
    with tempfile.TemporaryDirectory() as tmp, _private_tmux_server(Path(tmp)) as wrapper:
        session = f"fleet-realtmux-smoke-{os.getpid()}-{time.monotonic_ns() % 10**8}"
        subprocess.run([wrapper, "new-session", "-d", "-s", session, "sleep 30"], capture_output=True, text=True, check=True)
        after = _default_server_sessions(tmux_bin)
        gained = set(after) - set(before)
        check(not any(name.startswith("fleet-") for name in gained), f"real tier added default-server sessions: {sorted(gained)!r}")
