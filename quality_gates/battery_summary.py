"""Typed full-battery observations from one run of each registered smoke."""

from __future__ import annotations

from collections import Counter
from typing import Literal, TypedDict

ReceiptVerdict = Literal["passed", "skipped", "failed", "missing", "not_run"]


class SmokeResult(TypedDict):
    exit_code: int | None
    smoke: str
    verdict: ReceiptVerdict
    seconds: float | None
    output_tail: str
    output_truncated: bool
    output_ref: str


class BatterySummary(TypedDict):
    exit_code: int | None
    result: str
    total: int
    passed: int
    skipped: int
    failed: int
    missing: int
    not_run: int
    failing_smokes: list[str]


def build_summary(results: dict[str, SmokeResult], result: str, exit_code: int | None) -> BatterySummary:
    """Aggregate the single observed verdict for each registered smoke."""
    rows = list(results.values())
    counts = Counter(row["verdict"] for row in rows)
    return BatterySummary(
        exit_code=exit_code,
        result=result,
        total=len(rows),
        passed=counts["passed"],
        skipped=counts["skipped"],
        failed=counts["failed"],
        missing=counts["missing"],
        not_run=counts["not_run"],
        failing_smokes=[row["smoke"] for row in rows if row["verdict"] in ("failed", "missing")],
    )
