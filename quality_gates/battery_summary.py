"""Typed full-battery observations and bounded historical diagnostics.

Receipt consumer protocol:

* ``summary.exit_code`` is the return value from this invocation's final
  skip/failure policy, not a recomputation by the consumer. An interrupted
  invocation has no normal return value and records null.
* ``summary.result`` repeats the receipt result: ``passed``,
  ``passed_with_retries``, ``failed`` or ``interrupted``. A retry-assisted
  pass means an observed first failure was followed by an observed pass.
* ``total`` counts registered entries represented by the final rows.
  ``passed``, ``skipped``, ``failed``, ``missing`` and ``not_run`` are
  mutually exclusive counts of those rows and sum to ``total``.
* ``not_run`` includes work for which no final observation was recorded;
  interruption must not turn these entries into passes or failures.
* ``retried_smokes`` lists final rows marked retried, in register order;
  ``retried_count`` is its length. It includes retries that stayed red.
* ``flaky_smokes`` is the retried subset whose final observation passed;
  ``flaky_count`` is its length. These are observations from this run,
  never an assertion that a test is a known flake on other candidates.
* ``failing_smokes`` lists final failed or missing rows in register order.
  A policy-blocking skip remains a skip in the counts and raw evidence,
  even though ``exit_code`` is 1 and ``result`` is ``failed``.
* ``smoke_results[].exit_code`` and campaign attempts carry the actual child
  return code (negative for a signal), including cancellation and harness exits.
  Null means no child completed: missing, not started, or start-rate refusal.
  The runner has no execution deadline. A final row records the latest attempt.
* Consumers retain ``smoke_results`` and ``retries`` for the measured
  per-attempt detail; the aggregate does not replace those fields.

The historical advisory has a separate validity state from the run result:

* ``repeat_flaky.state`` is ``available`` only when the immediate prior
  receipt has usable digest/campaign provenance and every per-smoke row
  has a unique nonempty name, a recognized verdict and a boolean retry
  marker. Partial malformed rows invalidate attribution for that receipt.
* ``scope`` is always ``current_and_immediate_prior_receipts``. Nested
  ``reauthorized_from`` history is retained but never consulted.
* ``prior_candidate_digest`` and ``prior_campaign`` identify the receipt
  actually consulted; unavailable provenance is represented by null.
* ``smokes`` is the current register-ordered intersection of observed
  flaky names; ``count`` is its length. An available empty intersection
  is different from unavailable evidence, even though both have count 0.
* ``reason`` is null for available evidence and explains unavailable
  detail otherwise. Consumers must check ``state`` before interpreting
  an empty list as evidence that no observed flaky names repeated.

Both fields are created at the same completion path as the receipt result.
Reauthorization creates fresh aggregates and retains the prior object under
``reauthorized_from`` without correcting or deleting historical evidence.
There is no summary-only update, history backfill, receipt-directory scan,
register write or change to the full-battery admission/exit policies.
Focused registers keep their existing no-full-receipt behavior.

"""

from __future__ import annotations

from collections import Counter
from typing import Literal, TypedDict, cast

ReceiptVerdict = Literal["passed", "skipped", "failed", "missing", "not_run"]


class SmokeResult(TypedDict):
    exit_code: int | None
    smoke: str
    verdict: ReceiptVerdict
    seconds: float | None
    retried: bool
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
    retried_smokes: list[str]
    retried_count: int
    flaky_smokes: list[str]
    flaky_count: int
    failing_smokes: list[str]


class RepeatFlaky(TypedDict):
    state: Literal["available", "unavailable"]
    scope: str
    prior_candidate_digest: str | None
    prior_campaign: str | None
    smokes: list[str]
    count: int
    reason: str | None


def build_summary(results: dict[str, SmokeResult], result: str, exit_code: int | None) -> BatterySummary:
    """Aggregate measured final rows; retries never erase persistent red."""
    rows = list(results.values())
    counts = Counter(row["verdict"] for row in rows)
    retried = [row["smoke"] for row in rows if row["retried"]]
    flaky = [row["smoke"] for row in rows if row["retried"] and row["verdict"] == "passed"]
    return BatterySummary(
        exit_code=exit_code,
        result=result,
        total=len(rows),
        passed=counts["passed"],
        skipped=counts["skipped"],
        failed=counts["failed"],
        missing=counts["missing"],
        not_run=counts["not_run"],
        retried_smokes=retried,
        retried_count=len(retried),
        flaky_smokes=flaky,
        flaky_count=len(flaky),
        failing_smokes=[row["smoke"] for row in rows if row["verdict"] in ("failed", "missing")],
    )


def _prior_smoke_observation(item: object) -> tuple[str, bool] | None:
    """Decode one historical row without assuming required fields exist."""
    if not isinstance(item, dict):
        return None
    row = cast(dict[str, object], item)
    name = row.get("smoke")
    verdict = row.get("verdict")
    retried = row.get("retried")
    if not isinstance(name, str) or not name:
        return None
    if verdict not in ("passed", "failed", "skipped", "missing", "not_run") or not isinstance(retried, bool):
        return None
    return name, retried and verdict == "passed"


def _observed_prior_flaky(rows: object) -> list[str] | None:
    """Validate all prior rows before attributing any observed flaky name."""
    if not isinstance(rows, list):
        return None
    names: set[str] = set()
    flaky: list[str] = []
    for item in cast(list[object], rows):
        observed = _prior_smoke_observation(item)
        if observed is None:
            return None
        name, is_flaky = observed
        if name in names:
            return None
        names.add(name)
        if is_flaky:
            flaky.append(name)
    return flaky


def _prior_provenance(prior: dict[str, object], key: str) -> str | None:
    """Retain usable provenance and disclose missing/invalid values as null."""
    value = prior.get(key)
    return value if isinstance(value, str) and value else None


def repeat_flaky_advisory(prior: object, current: list[str]) -> RepeatFlaky:
    """Use only the in-memory immediate prior receipt, without modifying it."""
    advisory = RepeatFlaky(
        state="unavailable",
        scope="current_and_immediate_prior_receipts",
        prior_candidate_digest=None,
        prior_campaign=None,
        smokes=[],
        count=0,
        reason="immediate prior receipt unavailable",
    )
    if not isinstance(prior, dict):
        return advisory
    previous = cast(dict[str, object], prior)
    digest = _prior_provenance(previous, "candidate_digest")
    campaign = _prior_provenance(previous, "campaign")
    advisory["prior_candidate_digest"] = digest
    advisory["prior_campaign"] = campaign
    names = _observed_prior_flaky(previous.get("smoke_results"))
    if names is None or digest is None or campaign is None:
        advisory["reason"] = "immediate prior per-smoke detail or provenance invalid/unavailable"
        return advisory
    repeated = [name for name in current if name in names]
    advisory.update(state="available", smokes=repeated, count=len(repeated), reason=None)
    return advisory
