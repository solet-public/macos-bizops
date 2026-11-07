"""Own short battery scratch and stop registered sessions before executor wait.

The 40-byte resolved-root budget reserves 64 bytes of macOS socket space.
Uncertain allocation/removal is advisory; only old, demonstrably dead owners
are swept. SIGKILL recovery occurs on a later start, never synchronously.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path
from types import FrameType, TracebackType
from typing import Protocol, cast

from contention_signal import holder_pid_is_alive


class Supervisor(Protocol):
    """The runner retains process registration and its existing start fuse."""

    def cancel_snapshot(self) -> tuple[tuple[int, ...], tuple[subprocess.Popen[str], ...]]: ...
    def terminate(self, process: subprocess.Popen[str]) -> None: ...


_active: BatteryRuntime | None = None
_SIGNALS = (signal.SIGINT, signal.SIGTERM)


def _parent() -> Path:
    return (Path.home() / ".ananta" / "s").resolve()


def _identity(path: Path) -> tuple[int, int]:
    info = path.lstat()
    return info.st_dev, info.st_ino


@contextmanager
def defer_signals() -> Generator[None]:
    """Publish ownership before delivering a main-thread interrupt, without masks."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    pending: list[int] = []
    saved = {sig: signal.getsignal(sig) for sig in _SIGNALS}
    try:
        for sig in _SIGNALS:
            signal.signal(sig, lambda number, _frame: pending.append(number))
        yield
    finally:
        for sig, handler in saved.items():
            signal.signal(sig, handler)
        if pending:
            os.kill(os.getpid(), pending[0])


def supervisor[T: Supervisor](factory: Callable[[], T]) -> T:
    return cast(T, _active.supervisor) if _active is not None else factory()


def environment() -> dict[str, str]:
    root = _active.root if _active is not None else None
    return dict.fromkeys(("TMPDIR", "TEMP", "TMP"), str(root)) if root else {}


@contextmanager
def abort_on_error() -> Generator[None]:
    """Abort inside the executor block, before its implicit wait on exceptions."""
    try:
        yield
    except BaseException:
        if _active is not None:
            _active.abort()
        raise


class BatteryRuntime:
    """One disposable owner record and supervisor shared across all battery phases."""

    environment = staticmethod(environment)
    select_supervisor = staticmethod(supervisor)
    defer_signals = staticmethod(defer_signals)
    abort_on_error = staticmethod(abort_on_error)

    def __init__(self, owner: Supervisor, terminate_group: Callable[[int], None]) -> None:
        self.supervisor = owner
        self._terminate_group = terminate_group
        self.root: Path | None = None
        self._previous: BatteryRuntime | None = None
        self._handlers: dict[int, signal.Handlers | Callable[[int, FrameType | None], object] | None] = {}
        self._warned = False
        self._aborting = False

    def advisory(self, error: object) -> None:
        if not self._warned:
            print(f"ADVISORY run_smokes scratch unavailable or retained: {error}", file=sys.stderr)
            self._warned = True

    def abort(self) -> None:
        """Mark cancellation atomically, then signal all remembered groups and reap."""
        if self._aborting:
            return
        self._aborting = True
        groups, processes = self.supervisor.cancel_snapshot()
        for group in groups:
            self._terminate_group(group)
        for process in processes:
            process.wait()

    def _signal(self, number: int, _frame: FrameType | None) -> None:
        if self._aborting:
            return
        self.abort()
        raise SystemExit(128 + number)

    def _remove(self, root: Path) -> None:
        try:
            shutil.rmtree(root)
        except OSError as exc:
            self.advisory(exc)

    def _stale_owner(self, root: Path) -> tuple[tuple[int, int], tuple[int, int]] | None:
        """Validate immutable owner metadata independently of the directory name."""
        directory, owner = root.lstat(), (root / ".owner.json").lstat()
        if not stat.S_ISDIR(directory.st_mode) or not stat.S_ISREG(owner.st_mode):
            return None
        if directory.st_uid != os.getuid() or owner.st_uid != os.getuid() or directory.st_mode & 0o077:
            return None
        if not self._dead_record(root / ".owner.json"):
            return None
        return ((directory.st_dev, directory.st_ino), (owner.st_dev, owner.st_ino))

    def _dead_record(self, owner: Path) -> bool:
        record = json.loads(owner.read_text(encoding="utf-8"))
        if not isinstance(record, dict) or record.get("schema") != 1 or record.get("uid") != os.getuid():
            return False
        pid, stamp = record.get("pid"), record.get("start_time_ns")
        if not isinstance(pid, int) or not isinstance(stamp, int) or min(pid, stamp) <= 0:
            return False
        return time.time_ns() - stamp >= 86400 * 10**9 and not holder_pid_is_alive(pid)

    def _sweep_one(self, root: Path) -> None:
        try:
            identity = self._stale_owner(root)
            if identity is not None and identity == (_identity(root), _identity(root / ".owner.json")):
                if not shutil.rmtree.avoids_symlink_attacks:
                    self.advisory("symlink-resistant removal unavailable")
                    return
                self._remove(root)
        except (OSError, ValueError, TypeError) as exc:
            self.advisory(exc)

    def _allocate(self) -> None:
        try:
            parent = _parent()
            if len(os.fsencode(parent / "b-abcdefgh")) > 40 or parent.is_relative_to("/private/tmp") or parent.is_relative_to("/tmp"):
                self.advisory(f"resolved parent exceeds scratch budget: {parent}")
                return
            parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            for candidate in parent.iterdir():
                if re.fullmatch(r"b-[a-z0-9_]{8}", candidate.name):
                    self._sweep_one(candidate)
            self.root = Path(tempfile.mkdtemp(prefix="b-", dir=parent)).resolve()
            if len(os.fsencode(self.root)) > 40:
                raise OSError(f"resolved root exceeds 40 bytes: {self.root}")
            record = {"schema": 1, "pid": os.getpid(), "uid": os.getuid(), "start_time_ns": time.time_ns()}
            with (self.root / ".owner.json").open("x", encoding="utf-8") as handle:
                json.dump(record, handle)
        except (OSError, RuntimeError) as exc:
            self.advisory(exc)
            self.close_root()

    def close_root(self) -> None:
        if self.root is not None:
            self._remove(self.root)
            self.root = None

    def __enter__(self) -> BatteryRuntime:
        global _active
        self._previous = _active
        self._handlers = {sig: signal.getsignal(sig) for sig in _SIGNALS}
        try:
            for sig in _SIGNALS:
                signal.signal(sig, self._signal)
            with defer_signals():
                _active = self
                self._allocate()
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None, traceback: TracebackType | None) -> None:
        global _active
        try:
            with defer_signals():
                self.abort()
                self.close_root()
                _active = self._previous
        finally:
            _active = self._previous
            for sig, handler in self._handlers.items():
                signal.signal(sig, handler)
