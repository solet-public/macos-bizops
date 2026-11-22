"""Ordered smoke-battery tickets backed by neutral numeric host leases.

Tickets are operational snapshots, shared by every lane of the same user.
A short flock protects queue updates; it is NEVER held while a battery runs.
Priority orders waiting tickets; admitted batteries run concurrently. The
head checks running demand from both clients under the shared host lock.
Unknown figures advise while known figures still decide.

Lifecycle: create waiting, read/refresh reason, transition to running, then
remove on exit/interruption; the next invocation sweeps dead PID owners.
Requests are immutable and never reopened: a retry creates a new ticket.
Updates/removal take the queue lock; atomic replacement makes external reads
whole. Stderr retains waiting/admission history for the caller's run receipt.
Nested fixture runners inherit a live admission rather than queue behind
their own parent. This is independent of the existing serial-phase flock.
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import math
import os
import sys
import time
import uuid
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Literal

sys.path.insert(0, str(Path(__file__).resolve().parent))
if TYPE_CHECKING:
    from quality_gates import host_compute_budget

PRIORITIES = {"normal": 0, "critical": 1}
MAX_WAIT_SECONDS = 20 * 60
REPORT_SECONDS = 30
POLL_SECONDS = 1
DIRECTORY_ENV = "RUN_SMOKES_ADMISSION_DIR"
INHERITED_ENV = "RUN_SMOKES_ADMISSION_TICKET"
PRIORITY_ENV = "RUN_SMOKES_PRIORITY"


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def priority_value(value: str | None = None) -> str:
    """CLI wins over environment; an unknown environment value is advisory."""
    selected = value if value is not None else os.environ.get(PRIORITY_ENV, "normal")
    if selected not in PRIORITIES:
        _stderr(f"ADVISORY run_smokes admission: unknown priority {selected!r}; using normal")
        return "normal"
    return selected


def ticket_directory() -> Path:
    """Use one per-user directory across all linked checkouts."""
    return Path(os.environ.get(DIRECTORY_ENV, str(Path.home() / ".ananta" / "locks" / "run_smokes_admission")))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        pass
    return True


@dataclass(frozen=True, slots=True)
class Ticket:
    name: str
    pid: int
    demand: int
    priority: str
    requested_at: float
    waiting_since: str
    state: Literal["waiting", "running"] = "waiting"
    reason: str = "requested"
    kind: Literal["battery", "serial"] = "battery"
    resource: str = ""


@contextmanager
def _locked(directory: Path) -> Generator[None]:
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (directory / "queue.lock").open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _publish(directory: Path, ticket: Ticket) -> None:
    path = directory / f"{ticket.name}.json"
    temporary = path.with_suffix(".tmp")
    try:
        temporary.write_text(json.dumps(asdict(ticket), sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_ticket(ticket: Ticket, path: Path) -> None:
    if ticket.name != path.stem or ticket.pid < 1 or ticket.demand < 1:
        raise ValueError("invalid ticket identity/demand")
    if ticket.priority not in PRIORITIES or ticket.state not in {"waiting", "running"} or not math.isfinite(ticket.requested_at):
        raise ValueError("invalid ticket priority/state/time")
    if ticket.kind not in {"battery", "serial"}:
        raise ValueError("invalid ticket kind")


def _read_tickets(directory: Path, alive: Callable[[int], bool], report: Callable[[str], None]) -> list[Ticket]:
    tickets: list[Ticket] = []
    for path in sorted(directory.glob("*.json")):
        try:
            ticket = Ticket(**json.loads(path.read_text(encoding="utf-8")))
            _validate_ticket(ticket, path)
            if alive(ticket.pid):
                tickets.append(ticket)
            else:
                path.unlink()
                report(f"run_smokes admission: swept dead pid {ticket.pid}; ticket {ticket.name}")
        except (OSError, ValueError, TypeError) as exc:
            report(f"ADVISORY run_smokes admission: unreadable ticket {path}: {exc}; ignoring")
    return tickets


def _host_load_advisory() -> str:
    try:
        cpu_count = os.cpu_count()
        load = os.getloadavg()[0]
        if cpu_count and load > cpu_count:
            return f"; ADVISORY host load {load:.1f}>{cpu_count} without lease is not capacity demand"
    except OSError as error:
        return f"; ADVISORY host load unavailable: {error}"
    return ""


def _admission_decision(
    ticket: Ticket,
    tickets: list[Ticket],
    job_kind: str,
    reference_workers: int | None,
    lease_box: list[host_compute_budget.LeaseOutcome],
) -> tuple[bool, str]:
    import host_compute_budget  # noqa: PLC0415

    waiting = [row for row in tickets if row.state == "waiting"]
    head = min(waiting, key=lambda row: (-PRIORITIES[row.priority], row.requested_at, row.name))
    if head.name != ticket.name:
        return False, f"earlier/priority ticket {head.name} ({head.priority}) ahead"
    lease = host_compute_budget.try_acquire(
        job_kind,
        None if reference_workers is not None else ticket.demand,
        workers=ticket.demand if reference_workers is not None else None,
        reference_workers=reference_workers,
        token=ticket.name,
    )
    lease_box[:] = [lease]
    reason = lease.reason or "numeric host capacity admitted"
    if lease.advisories:
        reason += "; ADVISORY " + ", ".join(lease.advisories)
    return lease.admitted, reason + _host_load_advisory()


def _wait(
    directory: Path,
    ticket: Ticket,
    job_kind: str,
    reference_workers: int | None,
    lease_box: list[host_compute_budget.LeaseOutcome],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    alive: Callable[[int], bool],
    report: Callable[[str], None],
) -> Ticket:
    last_report = -math.inf
    while True:
        with _locked(directory):
            tickets = [row for row in _read_tickets(directory, alive, report) if row.kind == "battery"]
            now = clock()
            admitted, reason = _admission_decision(ticket, tickets, job_kind, reference_workers, lease_box)
            reason = f"{reason}; priority {ticket.priority}; ticket {ticket.name}; waiting since {ticket.waiting_since}"
            ticket = replace(ticket, state="running" if admitted else "waiting", reason=reason)
            _publish(directory, ticket)
        if admitted:
            report(f"run_smokes admission: admitted; {reason}")
            return ticket
        if now - last_report >= REPORT_SECONDS:
            report(f"run_smokes admission: waiting {reason!r}")
            last_report = now
        sleep(POLL_SECONDS)


def _inherited(directory: Path, tickets: list[Ticket]) -> Ticket | None:
    """An existing running ticket can be shared only in its own directory."""
    raw = os.environ.get(INHERITED_ENV)
    if raw is None:
        return None
    path = Path(raw)
    if path.parent.resolve() != directory.resolve():
        return None
    return next((row for row in tickets if row.name == path.stem and row.state == "running"), None)


def _release(directory: Path, name: str, report: Callable[[str], None]) -> None:
    try:
        with _locked(directory):
            (directory / f"{name}.json").unlink(missing_ok=True)
    except OSError as exc:
        report(f"ADVISORY run_smokes admission: ticket cleanup failed: {exc}; next invocation sweeps dead owners")


def _serial_attempt(directory: Path, ticket: Ticket, attempt: Callable[[], bool], now: float, report: Callable[[str], None]) -> tuple[bool, str]:
    """Choose a waiting serial ticket and try the physical flock atomically."""
    with _locked(directory):
        waiting = [row for row in _read_tickets(directory, _pid_alive, report) if row.kind == "serial" and row.resource == ticket.resource]
        head = min(waiting, key=lambda row: (-PRIORITIES[row.priority], row.requested_at, row.name))
        aged = now - ticket.requested_at >= MAX_WAIT_SECONDS
        reason = f"serial slot: priority {ticket.priority}; ticket {ticket.name}; waiting since {ticket.waiting_since}"
        if aged:
            reason = f"ADVISORY aging limit {MAX_WAIT_SECONDS}s reached; priority bypass; {reason}"
        eligible = head.name == ticket.name or aged
        if eligible and attempt():
            return True, reason
        reason += "; physical host serial-only flock busy" if eligible else f"; earlier/priority ticket {head.name} ({head.priority}) ahead"
        _publish(directory, replace(ticket, reason=reason))
        return False, reason


def _poll_serial(
    attempt: Callable[[], bool],
    ticket: Ticket | None,
    directory: Path,
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    report: Callable[[str], None],
    report_seconds: float,
) -> None:
    last_report = -math.inf
    while True:
        now = clock()
        if ticket is None:
            acquired, reason = attempt(), "serial slot: queue unavailable; waiting for physical host serial-only flock"
        else:
            acquired, reason = _serial_attempt(directory, ticket, attempt, now, report)
        if acquired:
            report(f"run_smokes admission: serial slot acquired; {reason}")
            return
        if now - last_report >= report_seconds:
            report(f"run_smokes admission: waiting {reason!r}")
            last_report = now
        sleep(0.1)


def wait_for_serial_lock(
    attempt: Callable[[], bool],
    *,
    resource: str = "",
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    report: Callable[[str], None] = _stderr,
    report_seconds: float = REPORT_SECONDS,
) -> None:
    """Order each physical serial-flock attempt by priority and FIFO.

    The waiting ticket is consumed when the physical lock is acquired. Aging
    bypasses queue position after twenty minutes, never physical mutual
    exclusion. An unavailable queue advises and polls the physical lock.
    """
    directory = ticket_directory()
    name = f"serial-{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex}"
    try:
        with _locked(directory):
            parent = _inherited(directory, _read_tickets(directory, _pid_alive, report))
            priority = parent.priority if parent is not None else priority_value()
            ticket = Ticket(name, os.getpid(), 1, priority, clock(), dt.datetime.now(dt.UTC).isoformat(), kind="serial", resource=resource)
            _publish(directory, ticket)
        try:
            _poll_serial(attempt, ticket, directory, clock, sleep, report, report_seconds)
        except (OSError, ValueError, TypeError) as exc:
            report(f"ADVISORY run_smokes admission: serial queue unavailable: {exc}; using physical flock")
            _poll_serial(attempt, None, directory, clock, sleep, report, report_seconds)
    except OSError as exc:
        report(f"ADVISORY run_smokes admission: serial ticket unavailable: {exc}; using physical flock")
        _poll_serial(attempt, None, directory, clock, sleep, report, report_seconds)
    finally:
        _release(directory, name, report)


@contextmanager
def admit(
    demand: int,
    priority: str | None = None,
    *,
    job_kind: str = "full_battery",
    reference_workers: int | None = None,
    directory: Path | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    alive: Callable[[int], bool] = _pid_alive,
    report: Callable[[str], None] = _stderr,
) -> Generator[Ticket | None]:
    """Wait before starting a suite; release its ticket on every Python exit.

    Monotonic time, sleep and PID observation are injectable seams for fast
    tests; real runs use the host lease registry.
    """
    directory = ticket_directory() if directory is None else directory
    name = f"{time.time_ns()}-{os.getpid()}-{uuid.uuid4().hex}"
    ticket: Ticket | None = None
    owned = False
    lease_box: list[host_compute_budget.LeaseOutcome] = []
    previous = os.environ.get(INHERITED_ENV)
    reported: set[str] = set()

    def report_once(message: str) -> None:
        if not message.startswith("ADVISORY run_smokes admission: unreadable ticket"):
            report(message)
        elif message not in reported:
            reported.add(message)
            report(message)

    try:
        try:
            with _locked(directory):
                ticket = _inherited(directory, _read_tickets(directory, alive, report_once))
                if ticket is None:
                    ticket = Ticket(name, os.getpid(), max(1, demand), priority_value(priority), clock(), dt.datetime.now(dt.UTC).isoformat())
                    owned = True
                    _publish(directory, ticket)
            if owned:
                ticket = _wait(directory, ticket, job_kind, reference_workers, lease_box, clock, sleep, alive, report_once)
        except (OSError, ValueError, TypeError) as exc:
            report(f"ADVISORY run_smokes admission: capacity/ticket data unavailable: {exc}; proceeding")
            ticket = None
        if ticket is not None:
            os.environ[INHERITED_ENV] = str(directory / f"{ticket.name}.json")
        yield ticket
    finally:
        if previous is None:
            os.environ.pop(INHERITED_ENV, None)
        else:
            os.environ[INHERITED_ENV] = previous
        if owned:
            if lease_box:
                import host_compute_budget  # noqa: PLC0415

                host_compute_budget.release(lease_box[0])
            _release(directory, name, report)
