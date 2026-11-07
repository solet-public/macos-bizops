"""Sample overall host load, including work without a battery receipt."""

from __future__ import annotations

import dataclasses
import math
import os
import threading
from types import TracebackType


@dataclasses.dataclass(frozen=True, slots=True)
class HostLoad:
    start_1m: float
    finish_1m: float
    max_sampled_1m: float
    sample_count: int
    logical_cpu_count: int


class HostLoadMonitor:
    """Sample once per second and at both boundaries; never change a verdict."""

    def __init__(self) -> None:
        self._values: list[float] = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)
        self._error: OSError | ValueError | None = None
        cpu_count = os.cpu_count()
        if cpu_count is None or cpu_count < 1:
            raise ValueError("overall host load needs a measured logical CPU count")
        self._cpu_count = cpu_count

    def _sample(self) -> None:
        value = os.getloadavg()[0]
        if not math.isfinite(value) or value < 0:
            raise ValueError("overall host load must be finite and nonnegative")
        self._values.append(value)

    def _watch(self) -> None:
        try:
            while not self._stop.wait(1.0):
                self._sample()
        except (OSError, ValueError) as exc:
            self._error = exc

    def __enter__(self) -> HostLoadMonitor:
        self._sample()
        self._thread.start()
        return self

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _error: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self._stop.set()
        self._thread.join()
        if self._error is not None:
            raise self._error
        self._sample()

    def snapshot(self) -> HostLoad:
        if self._thread.is_alive() or len(self._values) < 2:
            raise RuntimeError("host-load snapshot requires a completed measurement")
        return HostLoad(
            self._values[0],
            self._values[-1],
            max(self._values),
            len(self._values),
            self._cpu_count,
        )
