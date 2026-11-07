"""Duty-cycle pacing for the event-embedding drain (prb_f6964132).

CPU inference runs back to back on one core for every embedded event, so an
unpaced drain pins a core for as long as it has work.  After each unit of work
the drain sleeps ``worked * (1 - duty) / duty``: at the default busy duty of
0.1 it averages about a tenth of one core.  The idle duty (default 1.0, full
speed) applies only when the ledger has had no new in-scope arrival for
``idle_after`` seconds, which is how the drain tells that the fleet is not
working.  Settings come from the environment at the start of each fire; an
invalid setting or a failed idle read uses the slow busy duty and logs an
advisory, never a refusal (rul_becb4bed).
"""

from __future__ import annotations

import logging
import math
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

BUSY_DUTY_ENV = "SOLET_EVENT_EMBED_BUSY_DUTY"
IDLE_DUTY_ENV = "SOLET_EVENT_EMBED_IDLE_DUTY"
IDLE_AFTER_ENV = "SOLET_EVENT_EMBED_IDLE_AFTER_SECONDS"
DEFAULT_BUSY_DUTY = 0.1
DEFAULT_IDLE_DUTY = 1.0
DEFAULT_IDLE_AFTER_SECONDS = 900.0

# The idle read is one indexed row; re-read it at most this often.
_IDLE_READ_INTERVAL_SECONDS = 60.0


def _setting(name: str, default: float, *, upper: float | None) -> float:
    """A positive finite setting at most ``upper``; anything else is the default, loudly."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        value = math.nan
    if math.isfinite(value) and value > 0 and (upper is None or value <= upper):
        return value
    logger.warning("event embedding pacing: %s=%r is not a positive finite value%s; using %s", name, raw, f" <= {upper}" if upper else "", default)
    return default


@dataclass(frozen=True, slots=True)
class PacingSettings:
    """Fractions of wall time the drain may work, busy and idle, and the arrival age that means idle."""

    busy_duty: float = DEFAULT_BUSY_DUTY
    idle_duty: float = DEFAULT_IDLE_DUTY
    idle_after_seconds: float = DEFAULT_IDLE_AFTER_SECONDS

    @classmethod
    def from_environment(cls) -> PacingSettings:
        return cls(
            busy_duty=_setting(BUSY_DUTY_ENV, DEFAULT_BUSY_DUTY, upper=1.0),
            idle_duty=_setting(IDLE_DUTY_ENV, DEFAULT_IDLE_DUTY, upper=1.0),
            idle_after_seconds=_setting(IDLE_AFTER_ENV, DEFAULT_IDLE_AFTER_SECONDS, upper=None),
        )


def _arrival_time(raw: object) -> datetime | None:
    """A stored ``imported_at`` as naive UTC (the column's convention); ``None`` when unreadable."""
    if isinstance(raw, datetime):
        return raw.astimezone(UTC).replace(tzinfo=None) if raw.tzinfo else raw
    if isinstance(raw, str):
        try:
            return _arrival_time(datetime.fromisoformat(raw))
        except ValueError:
            return None
    return None


class DutyCycle:
    """Sleeps after each unit of drain work so the drain uses at most its duty share of wall time."""

    __slots__ = ("_settings", "_newest_arrival", "_clock", "_sleep", "_now", "_mark", "_duty", "_duty_read_at", "paused_seconds")

    def __init__(
        self,
        settings: PacingSettings,
        *,
        newest_arrival: Callable[[], object],
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] = lambda: datetime.now(UTC).replace(tzinfo=None),
    ) -> None:
        self._settings = settings
        self._newest_arrival = newest_arrival
        self._clock = clock
        self._sleep = sleep
        self._now = now
        self._mark = clock()
        self._duty = settings.busy_duty
        self._duty_read_at: float | None = None
        self.paused_seconds = 0.0

    def pace(self) -> float:
        """Sleep in proportion to the work done since the last pace; return the pause in seconds."""
        worked = max(0.0, self._clock() - self._mark)
        duty = self.duty()
        pause = worked * (1.0 - duty) / duty
        if pause > 0:
            self._sleep(pause)
            self.paused_seconds += pause
        self._mark = self._clock()
        return pause

    def duty(self) -> float:
        """Busy duty unless the newest in-scope arrival is older than the idle threshold; read at most once a minute."""
        now = self._clock()
        if self._duty_read_at is not None and now - self._duty_read_at < _IDLE_READ_INTERVAL_SECONDS:
            return self._duty
        self._duty_read_at = now
        self._duty = self._measured_duty()
        return self._duty

    def _measured_duty(self) -> float:
        try:
            raw = self._newest_arrival()
        except Exception:  # noqa: BLE001 - an unknown idle state is advisory: pace at the safe busy duty
            logger.warning("event embedding pacing: newest-arrival read failed; using busy duty %s", self._settings.busy_duty, exc_info=True)
            return self._settings.busy_duty
        if raw is None:
            return self._settings.idle_duty
        arrived = _arrival_time(raw)
        if arrived is None:
            logger.warning("event embedding pacing: newest arrival %r unreadable; using busy duty %s", raw, self._settings.busy_duty)
            return self._settings.busy_duty
        idle = (self._now() - arrived).total_seconds() >= self._settings.idle_after_seconds
        return self._settings.idle_duty if idle else self._settings.busy_duty


__all__ = [
    "BUSY_DUTY_ENV",
    "DEFAULT_BUSY_DUTY",
    "DEFAULT_IDLE_AFTER_SECONDS",
    "DEFAULT_IDLE_DUTY",
    "IDLE_AFTER_ENV",
    "IDLE_DUTY_ENV",
    "DutyCycle",
    "PacingSettings",
]
