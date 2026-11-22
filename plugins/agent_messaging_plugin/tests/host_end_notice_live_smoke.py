#!/usr/bin/env python3
"""Exercise session-closed notices on one disposable tmux server."""

from __future__ import annotations

import contextlib
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Callable, Generator
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from composer_tmux_fixture import FixtureSession  # noqa: E402

from agent_messaging_plugin.host_end_notice import arm_session_closed_notice, observe_tmux_session  # noqa: E402


def run(argv: list[str]) -> subprocess.CompletedProcess[str]:
    outcome = subprocess.run(argv, capture_output=True, text=True, timeout=5, check=False)
    if outcome.returncode:
        raise RuntimeError(f"{argv!r}: {outcome.returncode}: {outcome.stderr}")
    return outcome


@contextlib.contextmanager
def private_tmux_server() -> Generator[Callable[..., subprocess.CompletedProcess[str]]]:
    """A run_fn bound to a throwaway tmux server, for a test that spawns managed sessions.

    Spawn arms a server-global session-closed hook, so such a test must never share the
    operator's default server: leaving the block fails if that server gained one."""
    private = FixtureSession()

    def hooks() -> int:
        shown = subprocess.run([private.tmux, "show-hooks", "-g"], capture_output=True, text=True, timeout=5, check=False)
        return shown.stdout.count("session-closed[")

    before = hooks()
    try:
        yield private.run
    finally:
        private.run([private.tmux, "kill-server"])  # a spawned pane can outlive the fixture session
        private.close()  # removes the socket, so only after the server is gone
    if hooks() != before:
        raise AssertionError("the test armed a session-closed hook on the default tmux server")


def wait_notice(log: Path, name: str) -> float:
    expected = f'live-test call --no-wait plugin::agent_messaging_plugin::session_host_closed {{"host": "tmux", "host_ref": "{name}"}}'
    start = time.monotonic()
    while time.monotonic() - start < 10:
        if log.exists() and expected in log.read_text():
            return time.monotonic() - start
        time.sleep(0.1)
    raise AssertionError(f"notice for {name} absent after 10 seconds; log={log.read_text() if log.exists() else '<absent>'}")


def require_armed(prefix: list[str], bridge: Path) -> None:
    if not arm_session_closed_notice(tmux=prefix, run_fn=subprocess.run, solet_name="live-test", solet_bin=str(bridge)):
        raise AssertionError("arming failed")


def main() -> int:
    tmux = shutil.which("tmux")
    if tmux is None:
        raise RuntimeError("tmux missing: live end-notice proof failed")
    prefix = [tmux, "-L", "engine6-end-" + uuid4().hex[:12]]
    socket_path: Path | None = None
    folder: Path | None = None
    passed = 0
    try:
        with tempfile.TemporaryDirectory(prefix="engine6-end-") as raw:
            folder = Path(raw)
            bridge = folder / "solet-bridge"
            log = folder / "calls.log"
            bridge.write_text(f'#!/bin/sh\nprintf \'%s %s\\n\' "$SOLET_NAME" "$*" >> {shlex.quote(str(log))}\n')
            bridge.chmod(0o755)
            run([*prefix, "new-session", "-d", "-s", "keeper", "sleep 300"])
            socket_path = Path(run([*prefix, "display-message", "-p", "#{socket_path}"]).stdout.strip())
            run([*prefix, "new-session", "-d", "-s", "fleet-live-aaaa1111", "sleep 300"])
            require_armed(prefix, bridge)
            run([*prefix, "kill-session", "-t", "=fleet-live-aaaa1111"])
            print(f"L1 notice in {wait_notice(log, 'fleet-live-aaaa1111'):.3f}s")
            passed += 1

            run([*prefix, "new-session", "-d", "-s", "fleet-live-bbbb2222", "sleep 1"])
            print(f"L2 notice in {wait_notice(log, 'fleet-live-bbbb2222'):.3f}s")
            passed += 1

            require_armed(prefix, bridge)
            hooks = run([*prefix, "show-hooks", "-g", "session-closed"]).stdout
            if sum("SOLET_NAME=live-test" in line for line in hooks.splitlines()) != 1:
                raise AssertionError(f"expected one hook, got: {hooks}")
            print("L3 one hook after repeat arm")
            passed += 1

            observation = observe_tmux_session("fleet-live-aaaa1111", tmux=prefix)
            if observation.state != "gone":
                raise AssertionError(f"closed session read {observation}")
            print("L4 closed session is gone")
            passed += 1

            run([*prefix, "kill-session", "-t", "=keeper"])
            print(f"L5 last-session notice in {wait_notice(log, 'keeper'):.3f}s")
            passed += 1
    finally:
        subprocess.run([*prefix, "kill-server"], capture_output=True, text=True, timeout=5, check=False)
        if socket_path is not None:
            socket_path.unlink(missing_ok=True)
    if socket_path is None or socket_path.exists() or folder.exists():
        raise AssertionError(f"L6 cleanup failed: socket={socket_path}, folder={folder}")
    print("L6 private socket and temporary folder removed")
    passed += 1
    print(f"{passed} passed, 0 failed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
