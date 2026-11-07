"""Detached SIGTERM watchdog for ``stop_self``.

Slice 4.5 of
``workbench/2026-06-05_bridge_port_routing_and_session_lifecycle_design.md``
introduces the ``stop_self`` verb on the
:class:`SelfDeploymentServiceInterface` base. The verb runs INSIDE the
the solet process it's being asked to kill; a synchronous SIGTERM would
prevent the verb's response from flushing back to the caller (the
network write races the signal handler). The fix is a detached
subprocess: ``stop_self`` spawns a tiny Python subprocess that sleeps
briefly, sends SIGTERM, waits a bounded window, escalates to SIGKILL
if the target is still alive — all from outside the solet process, so
the verb's response has time to fully serialize and ship before any
signal arrives.

``start_new_session=True`` puts the watchdog in its own POSIX session,
so the SIGTERM it later sends to the solet doesn't cascade back to the
watchdog itself.

**Owned-deploy custody.** ``stop_self`` holds the solet's owned-deploy
admission lock and hands that descriptor to this watchdog through
``pass_fds`` (:meth:`OwnedDeployLease.transfer`). The watchdog keeps it
open through its whole bounded signal sequence and closes it on exit, so no
deploy, rollback or second stop can start in the delayed-signal window.

**Identity before every signal.** The watchdog receives the target's
start-time token (:func:`process_identity.start_token`) and re-probes it
before SIGTERM and again before SIGKILL; a missing or different token (the
target is gone, or its pid was reused) ends the sequence without
signalling.

Lives as a sibling module to :mod:`drain_sentinel` so it's easy to
test (the plugin injects an alternate spawner via
``set_watchdog_spawner_for_smoke``; :func:`run_signal_sequence` takes
injected probe/kill/sleep seams).
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Final

from macos_self_deployment_plugin import process_identity
from macos_self_deployment_plugin.constants import (
    DEFAULT_STOP_SELF_PRE_SIGTERM_DELAY_SECONDS,
    DEFAULT_STOP_SELF_SIGKILL_ESCALATION_SECONDS,
    PLUGIN_NAME,
)

WATCHDOG_MODULE: Final[str] = "macos_self_deployment_plugin.stop_self_watchdog"

OUTCOME_SIGNALLED: Final[str] = "signalled"
OUTCOME_TARGET_GONE: Final[str] = "target_gone"
OUTCOME_IDENTITY_UNPROVED: Final[str] = "identity_unproved"


@dataclass(frozen=True, slots=True)
class WatchdogRequest:
    """What one watchdog needs: its target, the target's identity, the inherited lock."""

    target_pid: int
    target_start_token: str
    lock_fd: int


# A spawner launches the watchdog for a request (passing ``lock_fd`` to the
# child) and returns the watchdog's pid (used purely for audit).
WatchdogSpawner = Callable[[WatchdogRequest], int]

_logger = logging.getLogger(PLUGIN_NAME)


def spawn(request: WatchdogRequest) -> int:
    """Production watchdog spawner: detach this module as a subprocess.

    ``pass_fds`` hands exactly the owned-deploy lock descriptor to the
    child; every other descriptor is closed. ``start_new_session=True`` is
    the load-bearing detachment (POSIX session leader new); without it, the
    SIGTERM the watchdog later sends could cascade back if our process tree
    got re-parented.
    """
    proc = subprocess.Popen(  # noqa: S603 — interpreter + this module, no shell
        [
            sys.executable,
            "-m",
            WATCHDOG_MODULE,
            str(request.target_pid),
            request.target_start_token,
            str(request.lock_fd),
        ],
        pass_fds=(request.lock_fd,),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    _logger.info(
        "%s: stop_self watchdog spawned pid=%d targeting the solet pid=%d (SIGTERM in %.1fs, SIGKILL escalation in %.1fs; holds owned-deploy fd %d)",
        PLUGIN_NAME,
        proc.pid,
        request.target_pid,
        DEFAULT_STOP_SELF_PRE_SIGTERM_DELAY_SECONDS,
        DEFAULT_STOP_SELF_PRE_SIGTERM_DELAY_SECONDS + DEFAULT_STOP_SELF_SIGKILL_ESCALATION_SECONDS,
        request.lock_fd,
    )
    return proc.pid


def run_signal_sequence(
    *,
    target_pid: int,
    start_token: str,
    identity_probe: Callable[[int], str | None] = process_identity.start_token,
    kill: Callable[[int, int], None] = os.kill,
    sleep: Callable[[float], None] = time.sleep,
) -> str:
    """The existing SIGTERM → bounded wait → SIGKILL sequence, identity-checked.

    Returns one of the ``OUTCOME_*`` tokens. No signal is sent unless the
    target's live start token still equals ``start_token``.
    """
    steps = (
        (DEFAULT_STOP_SELF_PRE_SIGTERM_DELAY_SECONDS, signal.SIGTERM),
        (DEFAULT_STOP_SELF_SIGKILL_ESCALATION_SECONDS, signal.SIGKILL),
    )
    for delay, signum in steps:
        sleep(delay)
        if identity_probe(target_pid) != start_token:
            return OUTCOME_IDENTITY_UNPROVED
        try:
            kill(target_pid, signum)
        except ProcessLookupError:
            return OUTCOME_TARGET_GONE
    return OUTCOME_SIGNALLED


def main(argv: list[str] | None = None) -> int:
    """Watchdog process entry: hold the inherited lock until the sequence ends."""
    target_pid, start_token, lock_fd = (argv if argv is not None else sys.argv[1:])[:3]
    try:
        run_signal_sequence(target_pid=int(target_pid), start_token=start_token)
    finally:
        os.close(int(lock_fd))
    return 0


if __name__ == "__main__":
    sys.exit(main())
