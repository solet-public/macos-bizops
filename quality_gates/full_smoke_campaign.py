"""Durable full-battery requests, linked attempts and filing outbox history.

This store shares the proof journal's owner/CAS/locking implementation, in a
distinct namespace. A running record is never a PASS. Only a worker holding
the run exclusion may append its interruption and start a linked attempt.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
from collections.abc import Generator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from quality_gates.smoke_budgets import load_budgets
from quality_gates.smoke_identity import digest
from quality_gates.smoke_pass_store import SmokePassStore, default_store_root

_CALIBRATION_ROWS = Path(__file__).with_name("smoke_budgets.json")


def utc_now() -> str:
    return datetime.now(UTC).isoformat()


class CampaignStore:
    """Append queued/running/closed plus pending/submitted/confirmed outbox."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root or default_store_root().parent / "full-campaigns-v1"
        self.journal = SmokePassStore(self.root)

    def get(self, key: str) -> dict[str, Any] | None:
        history = self.journal.history(key)
        return {**history[-1]["payload"], "version": history[-1]["version"]} if history else None

    def all(self) -> list[dict[str, Any]]:
        return [value for path in sorted(self.root.glob("*")) if len(path.name) == 64 and (value := self.get(path.name)) is not None]

    def write(self, key: str, state: Mapping[str, Any], *, expected_version: int, reason: str) -> dict[str, Any]:
        self.journal._append(key, "campaign", state, expected_version=expected_version, principal=f"local-uid:{os.getuid()}", reason=reason)
        return self.get(key) or {}

    def enqueue(self, request: Mapping[str, Any]) -> dict[str, Any]:
        key = digest(request)
        existing = self.get(key)
        if existing:
            return existing
        return self.write(key, {"campaign_id": key, "request": dict(request), "state": "queued", "queued_at": utc_now(), "next_due_at": utc_now(), "attempts": [], "filing": {"status": "not_needed"}}, expected_version=0, reason="accepted full battery request")

    @contextmanager
    def exclusion(self) -> Generator[bool]:
        """Host-external single worker; never inspect or terminate processes."""
        self.journal._authorize(f"local-uid:{os.getuid()}")
        descriptor = os.open(self.root / "worker.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
            else:
                try:
                    yield True
                finally:
                    fcntl.flock(handle, fcntl.LOCK_UN)


def battery_busy(directory: Path) -> bool:
    """A held canonical receipt lock is positive running-battery evidence."""
    for path in directory.glob("*.lock"):
        with path.open("a+") as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                return True
            fcntl.flock(handle, fcntl.LOCK_UN)
    return False


def campaign_budget(directory: Path, *, registered_count: int, per_smoke_seconds: int = 120, entries: Sequence[str] = ()) -> dict[str, Any]:
    """Use physical whole-battery timings, or disclose a conservative bound.

    Three times a measured maximum matches the existing smoke-budget policy.
    An uncalibrated initial run sums per-entry historical estimates: an
    entry's retained calibration row in smoke_budgets.json, else the runner's
    retained historical row, else ``per_smoke_seconds``. Those rows are
    evidence for this advisory estimate only; the runner applies no execution
    deadline. The bound is not mislabeled a measured duration or limited by
    the obsolete 900s suite cap.
    """
    measurements: list[tuple[float, str]] = []
    for path in sorted(directory.glob("*.json"))[-100:]:
        try:
            row = json.loads(path.read_bytes())
            start, end = datetime.fromisoformat(row["started_at"]), datetime.fromisoformat(row["finished_at"])
            seconds = (end - start).total_seconds()
            if seconds > 0 and row.get("result") == "passed" and row.get("summary", {}).get("total") == registered_count:
                measurements.append((seconds, str(path)))
        except (OSError, ValueError, KeyError, TypeError):
            continue
    if measurements:
        seconds, path = max(measurements)
        return {"seconds": math.ceil(seconds * 3), "basis": "3x measured full battery wall", "evidence": path, "advisory": None}
    from quality_gates.run_smokes import _HISTORICAL_ENTRY_BUDGETS_SECONDS

    estimates = {**_HISTORICAL_ENTRY_BUDGETS_SECONDS, **load_budgets(_CALIBRATION_ROWS)}
    bound = sum(max(per_smoke_seconds, estimates.get(entry, per_smoke_seconds)) for entry in entries) if entries else max(1, registered_count) * max([per_smoke_seconds, *estimates.values()])
    return {"seconds": bound * 3, "basis": "conservative serial per-entry bounds plus setup headroom", "evidence": None, "advisory": "whole-battery timing UNKNOWN; calibrate after first physical campaign"}
