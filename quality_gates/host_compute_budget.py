"""Independent client for the neutral host compute lease directory."""

from __future__ import annotations

import fcntl
import json
import math
import os
import resource
import subprocess
import tempfile
import time
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, cast

RESOURCES = ("cpu_slots", "pg_segments")
DEFAULT_CAPACITY = {"cpu_slots": 24, "pg_segments": 28}
DEFAULT_DEMAND = {"landing_battery": 8, "full_battery": 8, "deploy": 16}
DIRECTORY_ENV = "HOST_HEAVY_LEASE_DIR"


@dataclass(frozen=True, slots=True)
class LeaseOutcome:
    admitted: bool
    reason: str
    advisories: tuple[str, ...]
    token: str
    directory: Path
    pid: int
    owner_start: str
    job_kind: str
    workers: int | None
    cpu_start: float
    wall_start: float
    wait_seconds: float = 0.0
    wait_reason: str | None = None


def with_wait(lease: LeaseOutcome, seconds: float, reason: str | None) -> LeaseOutcome:
    """Attach admission facts before release; the use record remains append-only.

    The lease owner supplies the completed admission episode. The optional
    reason can be cleared; correction after release requires a new invocation.
    """
    if not math.isfinite(seconds) or seconds < 0:
        raise ValueError("invalid_wait_seconds")
    return replace(lease, wait_seconds=seconds, wait_reason=reason[:240] if reason is not None else None)


def directory() -> Path:
    return Path(os.environ.get(DIRECTORY_ENV, str(Path.home() / ".ananta/locks/host_heavy_admission")))


def process_birth(pid: int) -> str | None:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "stat=,lstart="],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
        env={**os.environ, "LC_ALL": "C"},
    )
    if result.returncode == 1 and not result.stdout.strip() and not result.stderr:
        return None
    if result.returncode or result.stderr:
        raise OSError("host_lease_process_identity_unavailable")
    fields = result.stdout.strip().split(maxsplit=1)
    if not fields or fields[0].startswith("Z"):
        return None
    if len(fields) != 2:
        raise OSError("host_lease_process_identity_unavailable")
    return fields[1]


def _prepare(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for path in (root, root / "leases", root / "use"):
        path.mkdir(exist_ok=True, mode=0o700)
        if path.stat().st_uid != os.getuid():
            raise OSError(f"host_lease_foreign_directory:{path}")
        path.chmod(0o700)


@contextmanager
def _locked(root: Path) -> Generator[None]:
    _prepare(root)
    descriptor = os.open(root / "settings.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        if os.fstat(stream.fileno()).st_uid != os.getuid():
            raise OSError("host_lease_foreign_lock")
        os.fchmod(stream.fileno(), 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(stream, fcntl.LOCK_UN)


def _atomic(path: Path, row: dict[str, Any]) -> None:
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".lease-", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write((json.dumps(row, sort_keys=True) + "\n").encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _settings(root: Path, advisories: list[str]) -> dict[str, Any] | None:
    try:
        parsed: object = json.loads((root / "settings.json").read_text())
    except (OSError, ValueError) as error:
        advisories.append(f"settings_unavailable:{error}")
        return None
    if not isinstance(parsed, dict):
        advisories.append("settings_schema_unknown")
        return None
    snapshot = cast(dict[str, Any], parsed)
    version = snapshot.get("host_format_version")
    if type(version) is not int or version != 1:
        advisories.append("settings_schema_unknown")
        return None
    return snapshot


def _figure(settings: dict[str, Any] | None, group: str, kind: str, resource_name: str) -> int | None:
    if settings is None:
        return None
    try:
        source = settings[group] if group == "capacities" else settings[group][kind]
        value = source[resource_name]["value"]
    except (KeyError, TypeError):
        return None
    if type(value) is not int or value < (1 if resource_name == "cpu_slots" or group == "capacities" else 0):
        return None
    return value


def _lease_amounts(path: Path, demand: dict[str, Any], advisories: list[str]) -> tuple[dict[str, int], set[str]]:
    totals: dict[str, int] = dict.fromkeys(RESOURCES, 0)
    unknown: set[str] = set()
    for resource_name in RESOURCES:
        amount = demand.get(resource_name)
        if type(amount) is not int or amount < 0:
            unknown.add(resource_name)
            advisories.append(f"lease_demand_unknown:{path.name}:{resource_name}")
        else:
            totals[resource_name] = amount
    return totals, unknown


def _validated_lease(path: Path) -> dict[str, Any] | None:
    parsed: object = json.loads(path.read_text())
    if not isinstance(parsed, dict) or parsed.get("schema") != 1 or parsed.get("state") not in ("running", "waiting"):
        raise ValueError("invalid lease")
    row = cast(dict[str, Any], parsed)
    pid, birth = row.get("pid"), row.get("owner_start")
    if type(pid) is not int or pid <= 0 or not isinstance(birth, str) or not birth:
        raise ValueError("invalid owner identity")
    live = process_birth(pid)
    if live is None or live != birth:
        path.unlink(missing_ok=True)
        return None
    return row


def _running_row(path: Path, advisories: list[str]) -> tuple[dict[str, int], set[str]]:
    empty: dict[str, int] = dict.fromkeys(RESOURCES, 0)
    try:
        row = _validated_lease(path)
        if row is None or row["state"] == "waiting":
            return empty, set()
        demand = row.get("demand")
        if not isinstance(demand, dict):
            raise ValueError("invalid demand")
        return _lease_amounts(path, cast(dict[str, Any], demand), advisories)
    except (OSError, ValueError, TypeError) as error:
        advisories.append(f"lease_unreadable:{path.name}:{error}")
        return empty, set(RESOURCES)


def _running(root: Path, own_token: str, advisories: list[str]) -> tuple[dict[str, int], set[str]]:
    totals: dict[str, int] = dict.fromkeys(RESOURCES, 0)
    unknown: set[str] = set()
    for path in sorted((root / "leases").glob("*.json")):
        if path.name == f"{own_token}.json":
            continue
        row_totals, row_unknown = _running_row(path, advisories)
        unknown.update(row_unknown)
        for resource_name in RESOURCES:
            totals[resource_name] += row_totals[resource_name]
    return totals, unknown


def _cpu_seconds() -> float:
    parent = resource.getrusage(resource.RUSAGE_SELF)
    children = resource.getrusage(resource.RUSAGE_CHILDREN)
    return parent.ru_utime + parent.ru_stime + children.ru_utime + children.ru_stime


def _validate_request(job_kind: str, cpu_slots: int | None, workers: int | None, reference_workers: int | None, token: str) -> None:
    if job_kind not in DEFAULT_DEMAND:
        raise ValueError(f"unknown_job_kind:{job_kind}")
    if workers is not None and (workers < 1 or reference_workers is None or reference_workers < 1):
        raise ValueError("invalid_worker_mapping")
    if cpu_slots is not None and cpu_slots < 1:
        raise ValueError("invalid_cpu_demand")
    if not token.replace("-", "").replace("_", "").isalnum() or len(token) > 128:
        raise ValueError("invalid_token")


def _check_own(path: Path, pid: int, birth: str) -> None:
    if not path.exists():
        return
    existing: object = json.loads(path.read_text())
    if not isinstance(existing, dict) or existing.get("pid") != pid or existing.get("owner_start") != birth:
        raise ValueError("token_owner_mismatch")
    if existing.get("state") == "running":
        raise ValueError("token_already_running")


def _requested_demand(
    settings: dict[str, Any] | None,
    job_kind: str,
    cpu_slots: int | None,
    workers: int | None,
    reference_workers: int | None,
    advisories: list[str],
) -> dict[str, int | None]:
    base = DEFAULT_DEMAND[job_kind] if settings is None else _figure(settings, "demands", job_kind, "cpu_slots")
    if base is None:
        advisories.append("demand_unknown:cpu_slots")
    cpu = cpu_slots if cpu_slots is not None else base
    if cpu_slots is None and base is not None and workers is not None and reference_workers is not None:
        cpu = max(1, math.ceil(base * workers / reference_workers))
    return {"cpu_slots": cpu, "pg_segments": 0}


def _capacity_reasons(
    settings: dict[str, Any] | None,
    job_kind: str,
    running: dict[str, int],
    unknown: set[str],
    demand: dict[str, int | None],
    advisories: list[str],
) -> list[str]:
    reasons: list[str] = []
    for resource_name in RESOURCES:
        capacity = _figure(settings, "capacities", job_kind, resource_name)
        candidate = demand[resource_name]
        if capacity is None or resource_name in unknown or candidate is None:
            advisories.append(f"capacity_unknown:{resource_name}")
            continue
        if running[resource_name] + candidate > capacity:
            reasons.append(f"{resource_name} running={running[resource_name]} candidate={candidate} capacity={capacity} pass_condition={running[resource_name]}+{candidate}<={capacity}; remeasure on next poll")
    return reasons


def try_acquire(
    job_kind: str,
    cpu_slots: int | None = None,
    *,
    workers: int | None = None,
    reference_workers: int | None = None,
    token: str | None = None,
) -> LeaseOutcome:
    """Check known resources and write one waiting or running row atomically."""
    selected_token = token or uuid.uuid4().hex
    _validate_request(job_kind, cpu_slots, workers, reference_workers, selected_token)
    root = directory()
    pid = os.getpid()
    birth = process_birth(pid)
    if birth is None:
        raise OSError("host_lease_owner_identity_unavailable")
    advisories: list[str] = []
    with _locked(root):
        own_path = root / "leases" / f"{selected_token}.json"
        _check_own(own_path, pid, birth)
        settings = _settings(root, advisories)
        running, unknown = _running(root, selected_token, advisories)
        demand = _requested_demand(settings, job_kind, cpu_slots, workers, reference_workers, advisories)
        reasons = _capacity_reasons(settings, job_kind, running, unknown, demand, advisories)
        reason = "; ".join(reasons)
        _atomic(
            root / "leases" / f"{selected_token}.json",
            {
                "schema": 1,
                "token": selected_token,
                "job_id": selected_token,
                "pid": pid,
                "owner_start": birth,
                "started_at": time.time(),
                "launcher": "ananta",
                "job_kind": job_kind,
                "workers": workers,
                "demand": demand,
                "state": "waiting" if reasons else "running",
                "reason": reason or "admitted",
            },
        )
    return LeaseOutcome(not reasons, reason, tuple(advisories), selected_token, root, pid, birth, job_kind, workers, _cpu_seconds(), time.monotonic())


def release(lease: LeaseOutcome) -> None:
    """Write one measured use record and remove the token under the host lock."""
    with _locked(lease.directory):
        path = lease.directory / "leases" / f"{lease.token}.json"
        if not path.exists():
            return
        row: dict[str, Any] = json.loads(path.read_text())
        if row.get("pid") != lease.pid or row.get("owner_start") != lease.owner_start:
            raise ValueError("token_owner_mismatch")
        if row.get("state") == "running":
            cpu_seconds = max(0.0, _cpu_seconds() - lease.cpu_start)
            wall_seconds = max(0.0, time.monotonic() - lease.wall_start)
            record = {
                "schema": 1,
                "token": lease.token,
                "job_id": lease.token,
                "job_kind": lease.job_kind,
                "workers": lease.workers,
                "cpu_seconds": float(cpu_seconds),
                "wall_seconds": float(wall_seconds),
                "average_cores": cpu_seconds / wall_seconds if wall_seconds > 0 else None,
                "peak_pg_segments": 0,
                "wait_seconds": lease.wait_seconds,
                "wait_reason": lease.wait_reason,
            }
            output = lease.directory / "use" / f"{lease.token}.json"
            descriptor = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(json.dumps(record, sort_keys=True) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
        path.unlink()
