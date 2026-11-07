"""Pacing and cancellation for the startup knowledge-base install (prb_2e7713d4).

A first install of a large knowledge base (the workbench KB has 2,561 files)
embeds chunks back to back and keeps about one core busy for most of an hour.
Deploys restart the platform more often than that, so the install never
finished and its work was lost at every start.

:class:`InstallControl` is the checkpoint the indexing loop calls before each
file and each chunk. It does two things:

* it raises :class:`InstallCancelledError` once the platform is shutting down, so
  the install stops between chunks instead of submitting work to an embedding
  executor that has already shut down;
* it sleeps after each unit of work so the install uses at most its duty share
  of wall time, through the session ledger's :class:`DutyCycle` helper. The
  install has no idle signal, so the busy and idle duties are the same value.

The duty comes from ``SOLET_KB_INSTALL_DUTY`` (a fraction in (0, 1], default
0.05). Startup's first pass with no install records defaults to 1.0 so a small
KB becomes searchable promptly; an explicit setting still wins. An invalid
value uses 0.05 and logs a warning, never a refusal (rul_becb4bed).
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass

from ananta.llm.session_ledger.event_embedding_pacing import DutyCycle, PacingSettings

from .constants import PLUGIN_NAME

logger = logging.getLogger(__name__)

KB_INSTALL_DUTY_ENV = "SOLET_KB_INSTALL_DUTY"
DEFAULT_KB_INSTALL_DUTY = 0.05

# The longest single sleep between two shutdown checks.
_SLEEP_SLICE_SECONDS = 0.5


class InstallCancelledError(Exception):
    """The platform began shutting down; the install stopped between chunks."""


def kb_install_duty(*, first_install: bool = False) -> float:
    """Configured duty, or full speed for a first install when no setting exists."""
    raw = os.environ.get(KB_INSTALL_DUTY_ENV)
    if raw is None:
        return 1.0 if first_install else DEFAULT_KB_INSTALL_DUTY
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if math.isfinite(value) and 0 < value <= 1:
        return value
    logger.warning(
        "%s: %s=%r is not a fraction in (0, 1]; using the default duty %s",
        PLUGIN_NAME,
        KB_INSTALL_DUTY_ENV,
        raw,
        DEFAULT_KB_INSTALL_DUTY,
    )
    return DEFAULT_KB_INSTALL_DUTY


def _no_arrival() -> None:
    """The install has no arrival signal; a ``None`` arrival selects the (equal) idle duty."""
    return None


@dataclass(frozen=True, slots=True)
class InstallControl:
    """The pause and the shutdown check the indexing loop runs between units of work."""

    pace: Callable[[], float]
    should_stop: Callable[[], bool]

    def checkpoint(self) -> None:
        """Stop if the platform is shutting down; otherwise pause for the work done since the last checkpoint."""
        if self.should_stop():
            raise InstallCancelledError
        self.pace()
        if self.should_stop():
            raise InstallCancelledError


def paced_install_control(
    should_stop: Callable[[], bool],
    *,
    duty: float | None = None,
    first_install: bool = False,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> InstallControl:
    """An :class:`InstallControl` at ``duty`` (default: the configured duty) whose pauses end early on shutdown."""

    def pause(seconds: float) -> None:
        deadline = clock() + seconds
        while not should_stop():
            remaining = deadline - clock()
            if remaining <= 0:
                return
            sleep(min(remaining, _SLEEP_SLICE_SECONDS))

    share = kb_install_duty(first_install=first_install) if duty is None else duty
    cycle = DutyCycle(
        PacingSettings(busy_duty=share, idle_duty=share),
        newest_arrival=_no_arrival,
        clock=clock,
        sleep=pause,
    )
    return InstallControl(pace=cycle.pace, should_stop=should_stop)


__all__ = [
    "DEFAULT_KB_INSTALL_DUTY",
    "KB_INSTALL_DUTY_ENV",
    "InstallCancelledError",
    "InstallControl",
    "kb_install_duty",
    "paced_install_control",
]
