#!/usr/bin/env python3
"""Measured-budget validation and host-load sampling preserve finite failures."""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from collections.abc import Callable
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "quality_gates"))
from host_load import HostLoadMonitor  # noqa: E402
from smoke_budgets import load_budgets  # noqa: E402


def _require(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)
    print(f"PASS {label}")


def _refuses(action: Callable[[], object], label: str) -> None:
    try:
        action()
    except (ValueError, OSError):
        print(f"PASS {label}")
    else:
        raise AssertionError(label)


def _row(**changes: object) -> dict[str, object]:
    return {
        "entry": "tests/slow_smoke.py",
        "base_seconds": 70.0,
        "observed_seconds": 90.1,
        "base_commit_sha": "a" * 40,
        "base_receipt_sha256": "b" * 64,
        "observation_sha256": "c" * 64,
        **changes,
    }


def _document(rows: list[object], **changes: object) -> dict[str, object]:
    return {
        "schema_version": 1,
        "multiplier": 3,
        "round_up_seconds": 15,
        "maximum_seconds": 900,
        "measurements": rows,
        **changes,
    }


def _load(document: dict[str, object]) -> dict[str, int]:
    with tempfile.TemporaryDirectory() as temporary:
        path = Path(temporary) / "budgets.json"
        path.write_text(json.dumps(document), encoding="utf-8")
        return load_budgets(path)


def _derived_budgets() -> None:
    result = _load(_document([_row()]))
    _require(bool(result), "calibration result is nonempty")
    _require(result == {"tests/slow_smoke.py": 285}, "slower-observation multiplier and upward rounding")
    _require("tests/slow_smoke.py.neighbor" not in result, "exact entry does not broaden to neighbors")
    result = _load(_document([_row(base_seconds=100.0, observed_seconds=80.0)]))
    _require(result["tests/slow_smoke.py"] == 300, "slower serial-base timing controls")
    result = _load(_document([_row(base_seconds=1.0, observed_seconds=2.0)]))
    _require(result["tests/slow_smoke.py"] == 120, "small calibration retains bounded default floor")
    result = _load(_document([_row(base_seconds=300.0)]))
    _require(result["tests/slow_smoke.py"] == 900, "finite ceiling boundary accepted")
    _refuses(lambda: _load(_document([_row(base_seconds=300.1)])), "over-ceiling work requires splitting")


def _invalid_evidence() -> None:
    for value in (0, -1, True, "90", float("nan"), float("inf")):
        _refuses(lambda value=value: _load(_document([_row(base_seconds=value)])), f"invalid timing {value!r}")
    _refuses(lambda: _load(_document([_row(), _row()])), "duplicate entry refuses")
    _refuses(lambda: _load(_document([])), "empty evidence refuses")
    _refuses(lambda: _load(_document([_row()], multiplier=4)), "fixed multiplier cannot drift silently")
    _refuses(lambda: _load(_document([_row(base_commit_sha="unknown")])), "missing base identity refuses")
    _refuses(lambda: _load(_document([_row(observation_sha256="unknown")])), "missing observation evidence refuses")
    for entry in ("../outside.py", "/absolute.py", "tests//slow.py", "tests\\slow.py"):
        _refuses(lambda entry=entry: _load(_document([_row(entry=entry)])), f"unsafe entry {entry!r}")


def _overall_load_without_battery_receipts() -> None:
    interior_seen = threading.Event()
    values = [2.0, 24.0, 3.0]

    def observed_load() -> tuple[float, float, float]:
        value = values.pop(0) if len(values) > 1 else values[0]
        if value == 24.0:
            interior_seen.set()
        return value, value, value

    with (
        patch("host_load.os.getloadavg", side_effect=observed_load),
        patch("host_load.os.cpu_count", return_value=8),
        HostLoadMonitor() as monitor,
    ):
        _require(interior_seen.wait(5.0), "periodic sample observes interior host work")
    measured = monitor.snapshot()
    _require(measured.start_1m == 2.0 and measured.finish_1m == 3.0, "host-load boundaries retained")
    _require(measured.max_sampled_1m == 24.0, "interior host load retained without battery receipt")
    _require(measured.sample_count >= 3 and measured.logical_cpu_count == 8, "load sample count and CPU denominator retained")


def _unavailable_load_refuses() -> None:
    def measure() -> None:
        with HostLoadMonitor():
            pass

    with patch("host_load.os.getloadavg", side_effect=OSError("measurement unavailable")):
        _refuses(measure, "unavailable host load is not fabricated as zero")
    with patch("host_load.os.cpu_count", return_value=None):
        _refuses(HostLoadMonitor, "unknown CPU count is not guessed")


def main() -> None:
    _derived_budgets()
    _invalid_evidence()
    _overall_load_without_battery_receipts()
    _unavailable_load_refuses()
    print("Smoke runtime policy: all checks passed")


if __name__ == "__main__":
    main()
