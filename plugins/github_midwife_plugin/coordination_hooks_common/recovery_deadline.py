"""Bound the complete standalone hook, including store locks and file reads.

Tool adapters invoke handle in the main thread of a fresh hook process. Git
children retain their own group watchdog; this guard also interrupts a held
publication/journal lock. A small publication reserve lets capture deadlines
publish the bytes already held before the final hook deadline expires.
"""

from __future__ import annotations

import signal
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from types import FrameType
from typing import NoReturn

from recovery_types import RecoveryError


class HookDeadline(BaseException):
    """Escape capture's recoverable read errors to finish the whole hook."""


def _expired(_signum: int, _frame: FrameType | None) -> NoReturn:
    raise HookDeadline("deadline: shared recovery hook budget exhausted")


@contextmanager
def hook_deadline(deadline: float) -> Generator[None]:
    """Preserve caller signal state; the actual adapter main-thread path passes.

    Off-thread embedding cannot safely install a process signal handler. It
    reports a failed save through handle, while the user's command proceeds.
    The registered tool adapters always use a standalone main-thread process;
    the public handle/CLI and held-root-lock smokes exercise that pass path.
    """
    if threading.current_thread() is not threading.main_thread():
        raise RecoveryError("hook_requires_main_thread")
    previous = signal.getsignal(signal.SIGALRM)
    if previous is None:
        raise RecoveryError("hook_alarm_handler_unavailable")
    started = time.monotonic()
    remaining, interval = signal.getitimer(signal.ITIMER_REAL)
    if (remaining or interval) and previous is not _expired:
        raise RecoveryError("hook_alarm_in_use")
    signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, max(0.000001, deadline - started))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0.0)
        signal.signal(signal.SIGALRM, previous)
        if remaining:
            signal.setitimer(signal.ITIMER_REAL, max(0.000001, remaining - (time.monotonic() - started)), interval)
