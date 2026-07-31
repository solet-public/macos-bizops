"""F2 smoke — ``_wait_for_router_socket`` bounded-wait behavior.

Regression guard for the birth-time race that FATAL'd fresh seed births:
``macos_self_deployment_plugin.prepare_for_readiness`` checked the router socket
ONE-SHOT and raised immediately if the router (a SEPARATE KeepAlive agent that
comes up independently) had not yet created it. At a fresh birth both agents
load ~simultaneously (RunAtLoad), so the main boot could reach the check a beat
too early -> FATAL first boot + LaunchAgent-restart cycle. The fix bounded-waits
for the socket, only failing loudly past the window. Cases:

  1. present_immediately -> returns fast, no raise (the common, socket-up path).
  2. appears_mid_poll    -> returns once the socket appears (the race path the
     fix targets; the old one-shot check would have raised here).
  3. absent_raises       -> raises RuntimeError naming the install path once the
     bounded window elapses (a genuinely absent router is a real error).

Sandbox discipline (per ``[[sandbox_mutating_smokes]]``): the "socket" is a plain
file in a TemporaryDirectory (the helper only ``.exists()``-checks the path),
under a distinct name, never ``~/.ananta/runtime/``. The wait window is
monkeypatched sub-second so case 3 is fast.
"""

from __future__ import annotations

import tempfile
import threading
import time
from pathlib import Path

from macos_self_deployment_plugin import plugin as _plugin
from macos_self_deployment_plugin.plugin import _wait_for_router_socket


def _stamp(name: str, ok: bool) -> None:
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}")


def _case_present_immediately(tmp: Path) -> bool:
    sock = tmp / "present.router.sock"
    sock.touch()
    start = time.monotonic()
    _wait_for_router_socket(sock)  # must not raise, must be fast
    return (time.monotonic() - start) < 0.5


def _case_appears_mid_poll(tmp: Path) -> bool:
    sock = tmp / "delayed.router.sock"

    def _create_after_delay() -> None:
        time.sleep(0.2)
        sock.touch()

    creator = threading.Thread(target=_create_after_delay)
    creator.start()
    try:
        _wait_for_router_socket(sock)  # must return once the socket appears
        return sock.exists()
    finally:
        creator.join()


def _case_absent_raises(tmp: Path) -> bool:
    sock = tmp / "never.router.sock"  # never created
    orig_wait = _plugin.DEFAULT_ROUTER_SOCKET_WAIT_SECONDS
    orig_poll = _plugin.DEFAULT_ROUTER_SOCKET_POLL_INTERVAL_SECONDS
    _plugin.DEFAULT_ROUTER_SOCKET_WAIT_SECONDS = 0.3
    _plugin.DEFAULT_ROUTER_SOCKET_POLL_INTERVAL_SECONDS = 0.05
    try:
        _wait_for_router_socket(sock)
        return False  # should have raised
    except RuntimeError as exc:
        return "router socket not found" in str(exc)
    finally:
        _plugin.DEFAULT_ROUTER_SOCKET_WAIT_SECONDS = orig_wait
        _plugin.DEFAULT_ROUTER_SOCKET_POLL_INTERVAL_SECONDS = orig_poll


def main() -> int:
    results: list[tuple[str, bool]] = []
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        results.append(("present_immediately", _case_present_immediately(tmp)))
        results.append(("appears_mid_poll", _case_appears_mid_poll(tmp)))
        results.append(("absent_raises", _case_absent_raises(tmp)))

    print("\nsummary")
    passed = sum(1 for _, ok in results if ok)
    for name, ok in results:
        _stamp(name, ok)
    print(f"\n{passed}/{len(results)} cases passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
