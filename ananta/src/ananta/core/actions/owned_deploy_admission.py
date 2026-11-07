"""Per-solet owned-deploy admission: one live deploy or recovery mutation at a time.

Every operation that rewrites what a solet runs — the core ``apply_manifest``
commit, a local blue/green restart or durable rollback, and ``stop_self`` —
enters through :meth:`OwnedDeployAdmission.admit` before its first side
effect. Admission is a nonblocking exclusive ``flock`` on a per-solet lock
file under the private runtime root. The file is created once and never
unlinked, so every participant locks the same inode.

**The live descriptor lock is the only authority.** Ownership is never a
clock, a TTL or a JSON row. When the owning process exits, crashes or
``exec``s, the kernel closes the descriptor and the lock is free; a live but
hung owner stays truthfully busy and is never expired by age. A contender is
refused at once with :class:`OwnedDeployBusyError` and never waits on the
caller's thread.

**Nesting joins, it never re-locks.** The core apply commit delegates to the
bound deployment plugin on the same thread, and the plugin's restart admits
again. That inner admission joins the outer attempt (same lock path, same
owning thread) and only the outermost scope releases. Another thread of the
same process, or an inherited task context, is never the same owner: it opens
its own descriptor and is refused like any other process.

**Custody can move to exactly one child.** ``stop_self`` hands the held
descriptor to its existing watchdog (:meth:`OwnedDeployLease.transfer`): the
child inherits it through ``pass_fds`` and the parent closes its own copy
without ``LOCK_UN``, so exclusion spans the watchdog's delayed signals and
ends when the watchdog exits.

**Active context is advisory metadata.** The holder records an immutable
attempt id, the operation, its pid and start token, a phase, an optional
detail and a sequence number in ``<solet>.owned_deploy.active.json``
(private, atomic sibling write), and appends every transition to
``<solet>.owned_deploy.history.jsonl``. Each mutation names the attempt and
sequence it expects, so a stale owner cannot overwrite or clear a
successor. The next acquisition retires whatever active record it finds as
``orphaned``, because a record left behind once the lock is free belongs to
an owner that is gone. A missing or corrupt record is logged loudly and
never fences anything.

Failure to establish exclusion at all (the runtime root is unwritable, the
lock file cannot be opened, ``flock`` errors, or the inode was replaced)
raises :class:`OwnedDeployExclusionError` before any side effect. That is a
data-integrity boundary, not a busy owner.

Scope limit: this fences participants that admit through this module.
External managers that stop or signal a solet without it are outside this
guard.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
import threading
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final, cast

from ananta.core.config.environment_config import EnvironmentConfig
from ananta.core.runtime.port_manager import get_runtime_dir

logger = logging.getLogger(__name__)

OWNED_DEPLOY_IN_PROGRESS: Final[str] = "owned_deploy_in_progress"
OWNED_DEPLOY_EXCLUSION_UNAVAILABLE: Final[str] = "owned_deploy_exclusion_unavailable"

LOCK_SUFFIX: Final[str] = ".owned_deploy.lock"
ACTIVE_SUFFIX: Final[str] = ".owned_deploy.active.json"
HISTORY_SUFFIX: Final[str] = ".owned_deploy.history.jsonl"
_TMP_SUFFIX: Final[str] = ".tmp"
_PRIVATE_MODE: Final[int] = 0o600

PHASE_ADMITTED: Final[str] = "admitted"
PHASE_TRANSFERRED: Final[str] = "transferred"

EVENT_ADMITTED: Final[str] = "admitted"
EVENT_JOINED: Final[str] = "joined"
EVENT_SET: Final[str] = "set"
EVENT_CORRECTED: Final[str] = "corrected"
EVENT_CLEARED: Final[str] = "cleared"
EVENT_TRANSFERRED: Final[str] = "transferred"
EVENT_RELEASED: Final[str] = "released"
EVENT_FAILED: Final[str] = "failed"
EVENT_ORPHANED: Final[str] = "orphaned"
EVENT_STALE_CLEAR_REFUSED: Final[str] = "stale_clear_refused"

IdentityProbe = Callable[[int], str | None]
DescriptorSpawner = Callable[[int], int]


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


@dataclass(frozen=True, slots=True)
class ContextVersion:
    """The attempt and sequence a mutation expects to find on disk."""

    attempt_id: str
    sequence: int


_FieldSpec = dict[str, tuple[type, ...]]
_CONTEXT_FIELDS: Final[_FieldSpec] = {
    "attempt_id": (str,),
    "solet_name": (str,),
    "operation": (str,),
    "owner_pid": (int,),
    "owner_start_token": (str, type(None)),
    "phase": (str,),
    "detail": (str, type(None)),
    "sequence": (int,),
    "started_at": (str,),
    "updated_at": (str,),
}
_HISTORY_FIELDS: Final[_FieldSpec] = {
    "attempt_id": (str,),
    "sequence": (int,),
    "event": (str,),
    "operation": (str,),
    "phase": (str,),
    "detail": (str, type(None)),
    "pid": (int,),
    "at": (str,),
}


def _typed_fields(data: object, spec: _FieldSpec) -> dict[str, object]:
    """Return ``data``'s fields named by ``spec``; ``ValueError`` on any mismatch."""
    if not isinstance(data, dict):
        msg = f"not an object: {data!r}"
        raise ValueError(msg)
    record = cast("dict[str, object]", data)
    bad = [name for name, types in spec.items() if type(record.get(name)) not in types]
    if bad:
        msg = f"malformed fields {bad}: {record!r}"
        raise ValueError(msg)
    return {name: record[name] for name in spec}


@dataclass(frozen=True, slots=True)
class ActiveContext:
    """The current owner's advisory record. ``attempt_id`` never changes."""

    attempt_id: str
    solet_name: str
    operation: str
    owner_pid: int
    owner_start_token: str | None
    phase: str
    detail: str | None
    sequence: int
    started_at: str
    updated_at: str

    @property
    def version(self) -> ContextVersion:
        return ContextVersion(self.attempt_id, self.sequence)

    def describe(self) -> str:
        return f"operation={self.operation} attempt={self.attempt_id} owner_pid={self.owner_pid} phase={self.phase} sequence={self.sequence} started_at={self.started_at}"

    @classmethod
    def from_json(cls, data: object) -> ActiveContext:
        """Parse a stored record; ``ValueError`` when any field is malformed."""
        return cls(**cast("dict[str, Any]", _typed_fields(data, _CONTEXT_FIELDS)))


@dataclass(frozen=True, slots=True)
class ContextRead:
    """A readback of the active record: the record, or why there is none."""

    context: ActiveContext | None
    advisory: str | None = None

    def describe(self) -> str:
        if self.context is not None:
            return self.context.describe()
        return f"owner context unreadable ({self.advisory})" if self.advisory else "owner context not recorded"


@dataclass(frozen=True, slots=True)
class HistoryEntry:
    """One immutable transition of an attempt."""

    attempt_id: str
    sequence: int
    event: str
    operation: str
    phase: str
    detail: str | None
    pid: int
    at: str


class OwnedDeployRefusalError(Exception):
    """Base for a typed refusal raised before any side effect."""

    def __init__(self, reason_code: str, detail: str) -> None:
        super().__init__(f"{reason_code}: {detail}")
        self.reason_code = reason_code
        self.detail = detail

    @property
    def reason_text(self) -> str:
        return f"{self.reason_code}: {self.detail}"


class OwnedDeployBusyError(OwnedDeployRefusalError):
    """Another live owner holds the lock (concurrent-mutation boundary)."""

    def __init__(self, owner: ContextRead) -> None:
        super().__init__(OWNED_DEPLOY_IN_PROGRESS, owner.describe())
        self.owner = owner


class OwnedDeployExclusionError(OwnedDeployRefusalError):
    """Mutual exclusion could not be established (data-integrity boundary)."""

    def __init__(self, detail: str) -> None:
        super().__init__(OWNED_DEPLOY_EXCLUSION_UNAVAILABLE, detail)


class StaleOwnedDeployContextError(Exception):
    """A mutation named an attempt or sequence that is no longer current."""


@dataclass(frozen=True, slots=True)
class _Paths:
    lock: Path
    active: Path
    history: Path

    @classmethod
    def for_solet(cls, runtime_dir: Path, solet_name: str) -> _Paths:
        return cls(
            lock=runtime_dir / f"{solet_name}{LOCK_SUFFIX}",
            active=runtime_dir / f"{solet_name}{ACTIVE_SUFFIX}",
            history=runtime_dir / f"{solet_name}{HISTORY_SUFFIX}",
        )


class _ContextStore:
    """Private-mode atomic active record plus append-only history."""

    def __init__(self, paths: _Paths) -> None:
        self._paths = paths

    def read(self) -> ContextRead:
        try:
            raw = self._paths.active.read_text(encoding="utf-8")
        except FileNotFoundError:
            return ContextRead(None)
        except OSError as exc:
            return ContextRead(None, f"unreadable: {exc}")
        try:
            return ContextRead(ActiveContext.from_json(json.loads(raw)))
        except ValueError as exc:
            return ContextRead(None, f"corrupt: {exc}")

    def require_current(self, expected: ContextVersion) -> None:
        """Refuse when the on-disk record names a different attempt or sequence."""
        current = self.read()
        if current.advisory is not None:
            logger.warning("owned-deploy active context %s; writing from the holder's record", current.advisory)
        if current.context is not None and current.context.version != expected:
            msg = f"expected {expected} but the active record is {current.context.version}"
            raise StaleOwnedDeployContextError(msg)

    def write(self, context: ActiveContext) -> None:
        payload = json.dumps(asdict(context), sort_keys=True).encode("utf-8")
        tmp = self._paths.active.with_name(self._paths.active.name + _TMP_SUFFIX)
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _PRIVATE_MODE)
            try:
                os.write(fd, payload)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(tmp, self._paths.active)
            _fsync_dir(self._paths.active.parent)
        except OSError as exc:
            logger.warning("owned-deploy active context write failed (advisory; the lock still governs): %s", exc)

    def clear(self, expected: ContextVersion) -> None:
        self.require_current(expected)
        try:
            self._paths.active.unlink(missing_ok=True)
            _fsync_dir(self._paths.active.parent)
        except OSError as exc:
            logger.warning("owned-deploy active context clear failed (advisory): %s", exc)

    def append(self, context: ActiveContext, event: str) -> None:
        entry = HistoryEntry(
            attempt_id=context.attempt_id,
            sequence=context.sequence,
            event=event,
            operation=context.operation,
            phase=context.phase,
            detail=context.detail,
            pid=os.getpid(),
            at=_utc_now(),
        )
        line = (json.dumps(asdict(entry), sort_keys=True) + "\n").encode("utf-8")
        try:
            fd = os.open(self._paths.history, os.O_WRONLY | os.O_CREAT | os.O_APPEND, _PRIVATE_MODE)
            try:
                os.write(fd, line)
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as exc:
            logger.warning("owned-deploy history append failed (advisory): %s", exc)

    def history(self) -> tuple[list[HistoryEntry], list[str]]:
        try:
            lines = self._paths.history.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return [], []
        except OSError as exc:
            return [], [f"history unreadable: {exc}"]
        entries: list[HistoryEntry] = []
        advisories: list[str] = []
        for number, line in enumerate(lines, start=1):
            try:
                fields = _typed_fields(json.loads(line), _HISTORY_FIELDS)
                entries.append(HistoryEntry(**cast("dict[str, Any]", fields)))
            except ValueError as exc:
                advisories.append(f"history line {number} unreadable: {exc}")
        return entries, advisories


def _fsync_dir(directory: Path) -> None:
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


class _Attempt:
    """One admitted attempt: its live descriptor and its advisory record."""

    def __init__(self, *, store: _ContextStore, fd: int, context: ActiveContext, identity_probe: IdentityProbe | None) -> None:
        self.store = store
        self.fd = fd
        self.context = context
        self.identity_probe = identity_probe
        self.transferred = False

    def commit(self, event: str, expected: ContextVersion, updated: ActiveContext) -> ActiveContext:
        """Advance the record from ``expected``; refuse a stale premise."""
        if self.transferred:
            msg = f"owned-deploy attempt {self.context.attempt_id} was transferred; this lease no longer owns it"
            raise RuntimeError(msg)
        if expected != self.context.version:
            msg = f"expected {expected} but this lease is at {self.context.version}"
            raise StaleOwnedDeployContextError(msg)
        self.store.require_current(expected)
        updated = replace(updated, sequence=self.context.sequence + 1, updated_at=_utc_now())
        self.store.write(updated)
        self.store.append(updated, event)
        self.context = updated
        return updated

    def commit_advisory(self, event: str, updated: ActiveContext) -> None:
        """Record a transition whose metadata must never undo the lock state."""
        try:
            self.commit(event, self.context.version, updated)
        except StaleOwnedDeployContextError as stale:
            logger.error("owned-deploy %s record skipped for attempt %s: %s", event, self.context.attempt_id, stale)

    def close(self, event: str) -> None:
        """Terminal history, then clear the active record if it is still ours."""
        closed = replace(self.context, sequence=self.context.sequence + 1, updated_at=_utc_now())
        try:
            self.store.clear(self.context.version)
        except StaleOwnedDeployContextError as stale:
            logger.error("owned-deploy attempt %s left a successor's record in place: %s", self.context.attempt_id, stale)
            self.store.append(closed, EVENT_STALE_CLEAR_REFUSED)
            return
        self.store.append(closed, event)

    def release(self) -> None:
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)

    def hand_off(self) -> None:
        """Drop this process's copy WITHOUT ``LOCK_UN``; the child's copy keeps the lock."""
        self.transferred = True
        os.close(self.fd)


@dataclass(slots=True)
class _Holding:
    thread_id: int
    attempt: _Attempt
    depth: int = 1


_REGISTRY_LOCK = threading.Lock()
_HELD: dict[str, _Holding] = {}


def _forget_holder(key: str) -> None:
    with _REGISTRY_LOCK:
        _HELD.pop(key, None)


def _require_outermost_owner(key: str, attempt: _Attempt) -> None:
    with _REGISTRY_LOCK:
        holding = _HELD.get(key)
    if holding is None or holding.attempt is not attempt or holding.thread_id != threading.get_ident() or holding.depth != 1:
        msg = "only the outermost owning scope on its own thread may transfer the lock"
        raise RuntimeError(msg)


class OwnedDeployLease:
    """The holder's handle on one attempt; only an admitted scope receives one."""

    def __init__(self, attempt: _Attempt, lock_key: str) -> None:
        self._attempt = attempt
        self._lock_key = lock_key

    @property
    def context(self) -> ActiveContext:
        return self._attempt.context

    @property
    def version(self) -> ContextVersion:
        return self._attempt.context.version

    @property
    def transferred(self) -> bool:
        return self._attempt.transferred

    def set_phase(self, phase: str, *, expected: ContextVersion) -> ActiveContext:
        return self._attempt.commit(EVENT_SET, expected, replace(self.context, phase=phase))

    def correct_phase(self, phase: str, *, expected: ContextVersion) -> ActiveContext:
        return self._attempt.commit(EVENT_CORRECTED, expected, replace(self.context, phase=phase))

    def set_detail(self, detail: str, *, expected: ContextVersion) -> ActiveContext:
        event = EVENT_SET if self.context.detail is None else EVENT_CORRECTED
        return self._attempt.commit(event, expected, replace(self.context, detail=detail))

    def clear_detail(self, *, expected: ContextVersion) -> ActiveContext:
        return self._attempt.commit(EVENT_CLEARED, expected, replace(self.context, detail=None))

    def transfer(self, spawn: DescriptorSpawner) -> int:
        """Hand the live lock descriptor to one child; return the child's pid.

        ``spawn`` receives the descriptor number and must pass it to the child
        with ``pass_fds``. Only after it returns does this process close its
        own copy, WITHOUT ``LOCK_UN`` (that would release the child's shared
        open file description too). If ``spawn`` raises, nothing moved and
        the enclosing scope releases normally.
        """
        attempt = self._attempt
        _require_outermost_owner(self._lock_key, attempt)
        child_pid = spawn(attempt.fd)
        try:
            token = attempt.identity_probe(child_pid) if attempt.identity_probe is not None else None
            moved = replace(attempt.context, phase=PHASE_TRANSFERRED, owner_pid=child_pid, owner_start_token=token)
            attempt.commit_advisory(EVENT_TRANSFERRED, moved)
        finally:
            _forget_holder(self._lock_key)
            attempt.hand_off()
        return child_pid


class OwnedDeployAdmission:
    """Admission to owned deploy/recovery work for one solet's runtime root."""

    def __init__(self, *, runtime_dir: Path, solet_name: str, identity_probe: IdentityProbe | None = None) -> None:
        if not solet_name:
            msg = "owned-deploy admission needs a solet name"
            raise ValueError(msg)
        self._solet_name = solet_name
        self._paths = _Paths.for_solet(runtime_dir, solet_name)
        self._key = str(self._paths.lock)
        self._store = _ContextStore(self._paths)
        self._identity_probe = identity_probe

    @classmethod
    def for_solet(cls, solet_name: str, *, identity_probe: IdentityProbe | None = None) -> OwnedDeployAdmission:
        """Admission under the solet's private runtime root (``get_runtime_dir``)."""
        try:
            runtime_dir = get_runtime_dir(solet_name)
        except OSError as exc:
            raise OwnedDeployExclusionError(f"runtime root unavailable: {exc}") from exc
        return cls(runtime_dir=runtime_dir, solet_name=solet_name, identity_probe=identity_probe)

    @classmethod
    def for_current_solet(cls) -> OwnedDeployAdmission:
        """Admission for this process's solet (``SOLET_NAME``); an unresolvable identity cannot exclude."""
        try:
            solet_name = EnvironmentConfig.solet_name()
        except RuntimeError as exc:
            raise OwnedDeployExclusionError(f"solet identity unavailable: {exc}") from exc
        return cls.for_solet(solet_name)

    @property
    def lock_path(self) -> Path:
        return self._paths.lock

    def read_active(self) -> ContextRead:
        """Current readback of the active record (advisory; the lock governs)."""
        return self._store.read()

    def read_history(self) -> tuple[list[HistoryEntry], list[str]]:
        """Historical readback: every recorded transition plus unreadable-line advisories."""
        return self._store.history()

    @contextmanager
    def admit(self, operation: str) -> Generator[OwnedDeployLease]:
        """Hold owned-deploy admission for the body, or raise a typed refusal first."""
        holding, joined = self._enter(operation)
        event = EVENT_FAILED
        try:
            yield OwnedDeployLease(holding.attempt, self._key)
            event = EVENT_RELEASED
        finally:
            self._exit(holding, joined=joined, event=event)

    def _enter(self, operation: str) -> tuple[_Holding, bool]:
        with _REGISTRY_LOCK:
            holding = _HELD.get(self._key)
            if holding is not None and holding.thread_id == threading.get_ident():
                holding.depth += 1
            elif holding is not None:
                raise OwnedDeployBusyError(ContextRead(holding.attempt.context))
        if holding is not None:
            attempt = holding.attempt
            attempt.commit_advisory(EVENT_JOINED, replace(attempt.context, phase=f"joined:{operation}"))
            return holding, True
        fd = self._acquire()
        try:
            attempt = self._start_attempt(fd, operation)
        except BaseException:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
            raise
        holding = _Holding(thread_id=threading.get_ident(), attempt=attempt)
        with _REGISTRY_LOCK:
            _HELD[self._key] = holding
        return holding, False

    def _exit(self, holding: _Holding, *, joined: bool, event: str) -> None:
        if joined:
            with _REGISTRY_LOCK:
                holding.depth -= 1
            return
        attempt = holding.attempt
        if attempt.transferred:
            return
        try:
            attempt.close(event)
        finally:
            _forget_holder(self._key)
            attempt.release()

    def _acquire(self) -> int:
        """Open the stable lock inode and take it exclusively without blocking."""
        try:
            self._paths.lock.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            fd = os.open(self._paths.lock, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, _PRIVATE_MODE)
        except OSError as exc:
            raise OwnedDeployExclusionError(f"cannot open lock {self._paths.lock}: {exc}") from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise OwnedDeployBusyError(self._store.read()) from None
        except OSError as exc:
            os.close(fd)
            raise OwnedDeployExclusionError(f"flock failed on {self._paths.lock}: {exc}") from exc
        if not _same_inode(fd, self._paths.lock):
            os.close(fd)
            raise OwnedDeployExclusionError(f"lock file {self._paths.lock} was replaced while acquiring")
        return fd

    def _start_attempt(self, fd: int, operation: str) -> _Attempt:
        """Retire any record a gone owner left, then record the new attempt."""
        leftover = self._store.read()
        if leftover.context is not None:
            orphan = replace(leftover.context, sequence=leftover.context.sequence + 1, updated_at=_utc_now())
            self._store.append(orphan, EVENT_ORPHANED)
            logger.warning("owned-deploy retired an orphaned attempt: %s", leftover.context.describe())
        elif leftover.advisory is not None:
            logger.warning("owned-deploy found an unreadable leftover record (%s); replacing it", leftover.advisory)
        pid = os.getpid()
        now = _utc_now()
        context = ActiveContext(
            attempt_id=uuid.uuid4().hex,
            solet_name=self._solet_name,
            operation=operation,
            owner_pid=pid,
            owner_start_token=self._identity_probe(pid) if self._identity_probe is not None else None,
            phase=PHASE_ADMITTED,
            detail=None,
            sequence=0,
            started_at=now,
            updated_at=now,
        )
        self._store.write(context)
        self._store.append(context, EVENT_ADMITTED)
        return _Attempt(store=self._store, fd=fd, context=context, identity_probe=self._identity_probe)


def run_admitted[T](
    operation: str,
    body: Callable[[OwnedDeployLease], T],
    *,
    refused: Callable[[OwnedDeployRefusalError], T],
    admission: Callable[[], OwnedDeployAdmission] = OwnedDeployAdmission.for_current_solet,
) -> T:
    """Run ``body`` under owned-deploy admission, or return ``refused(refusal)`` with nothing done.

    Resolving ``admission`` (by default this process's solet) happens inside the
    refusal boundary, so an unresolvable identity or runtime root is refused
    the same way as a held lock.
    """
    try:
        with admission().admit(operation) as lease:
            return body(lease)
    except OwnedDeployRefusalError as refusal:
        return refused(refusal)


def _same_inode(fd: int, path: Path) -> bool:
    try:
        on_path = os.stat(path)
    except OSError:
        return False
    held = os.fstat(fd)
    return (held.st_dev, held.st_ino) == (on_path.st_dev, on_path.st_ino)


__all__ = [
    "OWNED_DEPLOY_EXCLUSION_UNAVAILABLE",
    "OWNED_DEPLOY_IN_PROGRESS",
    "ActiveContext",
    "ContextRead",
    "ContextVersion",
    "DescriptorSpawner",
    "HistoryEntry",
    "IdentityProbe",
    "OwnedDeployAdmission",
    "OwnedDeployBusyError",
    "OwnedDeployExclusionError",
    "OwnedDeployLease",
    "OwnedDeployRefusalError",
    "StaleOwnedDeployContextError",
    "run_admitted",
]
