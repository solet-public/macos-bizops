"""The harness pass of the model catalog refresh (iss_0e805222, iss_52fdb8e5; rul_473f772f; design 3.3).

The harness lists decide which models exist. A cell whose model a CURRENT, readable
harness list stops listing starts a clock, `harness_unverified_since`; after one
staleness window of continuous absence the refresh withdraws the cell, reversibly and
with history; a model that is listed again clears the clock and, when the refresh
itself withdrew the cell, reinstates it. Nothing here removes a row or a score.

Who may move a cell, and the one marker that tells them apart:

* `harness_unverified_since` non-null = the harness cannot confirm this model right now,
  whether the cell still serves or was withdrawn for that reason by this pass.
* A withdrawn cell with a NULL clock was withdrawn by a person (the withdraw verb clears
  the clock), so this pass never reverses it.
* A withdrawn cell with a SET clock was withdrawn by this pass; only a relisting reverses
  it (or `reinstate_model_capability_cell`, which the next refresh undoes while the model
  is still unlisted and the window has passed).

What a list may prove (the status table in design 1.7):

* `ok` and no older than the cell policy window: starts the clock for a model it lacks, clears it for
  one it has, and, after the window, withdraws.
* anything else -- a Codex cache-file list (`degraded`), an `unreadable` or `empty` list,
  or an `ok` list older than the cell policy window -- starts, advances and clears nothing, and
  withdraws nothing. An unreadable or empty list is a read fault, not a fact. The pass
  says so in an advisory; it never refuses (rul_becb4bed).

A harness-listed model the leaderboard no longer scores keeps its score and is never
touched here: the leaderboard decides score and cost, never existence.

Pure decisions (`decide`, `harness_authority`) are separated from the writes
(`apply_harness_pass`), which go through `StateManagementInterface` only. This module
imports nothing from `model_capability_store`, so the store may import from it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, Literal

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_completed, require_updated

from .model_capability_fetch import DiscoveryRun
from .model_capability_harness import STATUS_OK, adapter_for_runtime
from .model_capability_reconcile import CellKey, key_text
from .schema import (
    CELL_ACCEPTANCE_ACCEPTED,
    CELL_ACCEPTANCE_WITHDRAWN,
    TABLE_MODEL_CAPABILITY_CELL,
    TABLE_MODEL_CAPABILITY_OBSERVATION,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

ACCEPTANCE_CHANGE_METRIC: Final[str] = "acceptance_change"
"""Observation metric for a cell moving between `accepted` and `withdrawn`."""

HARNESS_CLOCK_METRIC: Final[str] = "harness_clock"
"""Observation metric for the harness-absence clock being started, restarted or cleared."""

_MAX_EXCERPT: Final[int] = 512

Action = Literal["start", "restart", "clear", "withdraw", "reinstate"]


class HarnessPassError(RuntimeError):
    """The harness pass met a row it cannot write safely (an invariant, not data it may skip)."""


@dataclass(frozen=True, slots=True)
class ListedHarness:
    """What the pass may conclude from one runtime's current, readable harness list."""

    runtime: str
    source: str
    client_version: str
    model_ids: frozenset[str]
    fetched_at: datetime | None = None

    @property
    def label(self) -> str:
        return f"{self.runtime} harness ({self.source}, client {self.client_version or 'unknown'})"


def _parse_clock(raw: object) -> datetime | None:
    """The clock as a timezone-aware time, or None when it is unset or unreadable."""
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def harness_authority(run: DiscoveryRun, *, now: datetime) -> tuple[dict[str, ListedHarness], list[str]]:
    """Identify readable sources with a known fetch time; each cell supplies its freshness bound."""
    authority: dict[str, ListedHarness] = {}
    advisories: list[str] = []
    for runtime, summary in run.harness.items():
        status = str(summary.get("status", ""))
        fetched = _parse_clock(summary.get("fetched_at"))
        if status != STATUS_OK or fetched is None:
            advisories.append(f"{runtime} harness list is {status} with {'unknown age' if fetched is None else 'known age'} ({summary.get('detail', '')}): no harness clock was moved")
        else:
            authority[runtime] = ListedHarness(runtime, str(summary.get("source", "")), str(summary.get("client_version", "")), frozenset(run.harness_model_ids.get(runtime, ())), fetched)
    return authority, advisories


def _window(row: Mapping[str, Any]) -> timedelta | None:
    value = row.get("staleness_window_hours")
    if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
        return None
    return timedelta(hours=value)


def decide(row: Mapping[str, Any], harness: ListedHarness, *, now: datetime) -> Action | None:
    """What this pass does to one cell of a runtime whose list is authoritative, or None.

    The decision table (design 3.3): a manual withdrawal (withdrawn, clock null) is never
    touched; an automatic one (withdrawn, clock set) is reinstated when the model is listed
    again; a serving cell starts a clock when its model is absent, clears it when the model
    is listed, and is withdrawn once the clock has run one window."""
    window = _window(row)
    if window is None:
        return None
    since_raw = row.get("harness_unverified_since")
    listed = str(row["model"]) in harness.model_ids
    if row.get("acceptance") == CELL_ACCEPTANCE_WITHDRAWN:
        return "reinstate" if since_raw is not None and listed else None
    if listed:
        return "clear" if since_raw is not None else None
    since = _parse_clock(since_raw)
    if since is None:
        return "start" if since_raw is None else "restart"
    return "withdraw" if now - since > window else None


@dataclass(slots=True)
class HarnessPassReport:
    """Which cells the pass moved, as `runtime/model@effort`, and what it wants said loudly."""

    clock_started: list[str] = field(default_factory=lambda: list[str]())
    clock_cleared: list[str] = field(default_factory=lambda: list[str]())
    withdrawn: list[str] = field(default_factory=lambda: list[str]())
    reinstated: list[str] = field(default_factory=lambda: list[str]())
    advisories: list[str] = field(default_factory=lambda: list[str]())

    def result_fields(self) -> dict[str, Any]:
        return {
            "harness_clock_started": list(self.clock_started),
            "harness_clock_cleared": list(self.clock_cleared),
            "auto_withdrawn": list(self.withdrawn),
            "auto_reinstated": list(self.reinstated),
        }

    def note(self) -> str | None:
        parts = [
            f"{len(items)} cell(s) {label}"
            for items, label in (
                (self.clock_started, "newly unverified by the harness"),
                (self.withdrawn, "withdrawn: the harness no longer lists them"),
                (self.reinstated, "reinstated: the harness lists them again"),
                (self.clock_cleared, "confirmed again by the harness"),
            )
            if items
        ]
        return "; ".join(parts) or None


def _cell_key(row: Mapping[str, Any]) -> CellKey:
    return (str(row["runtime"]), str(row["model"]), str(row["effort"]))


def _withdraw_note(row: Mapping[str, Any], harness: ListedHarness, stamp: str) -> str:
    return f"withdrawn {stamp}: {harness.label} has not listed {row['model']} since {row.get('harness_unverified_since')}"


def _updates(action: Action, row: Mapping[str, Any], harness: ListedHarness, *, run_id: str, stamp: str) -> dict[str, Any]:
    """The columns an action rewrites. Score, cost, measured_at and accepted_at are never cleared."""
    updates: dict[str, Any] = {"last_refresh_run_id": run_id}
    if action in ("start", "restart"):
        updates["harness_unverified_since"] = stamp
    elif action == "clear":
        updates["harness_unverified_since"] = None
    elif action == "withdraw":
        updates.update({"acceptance": CELL_ACCEPTANCE_WITHDRAWN, "disagreement_note": _withdraw_note(row, harness, stamp)})
    else:
        updates.update({"acceptance": CELL_ACCEPTANCE_ACCEPTED, "harness_unverified_since": None, "disagreement_note": None, "accepted_at": row.get("accepted_at") or stamp})
    return updates


def _evidence(action: Action, row: Mapping[str, Any], harness: ListedHarness, *, stamp: str) -> tuple[str, str, str]:
    """(metric, value_text, raw_excerpt) of the observation that records this action."""
    key = key_text(_cell_key(row))
    if action == "withdraw":
        return ACCEPTANCE_CHANGE_METRIC, f"{row['acceptance']}->{CELL_ACCEPTANCE_WITHDRAWN}: harness_absent", _withdraw_note(row, harness, stamp)
    if action == "reinstate":
        return ACCEPTANCE_CHANGE_METRIC, f"{CELL_ACCEPTANCE_WITHDRAWN}->{CELL_ACCEPTANCE_ACCEPTED}: harness_relisted", f"{harness.label} lists {key} again; reinstated {stamp}"
    if action == "clear":
        return HARNESS_CLOCK_METRIC, "cleared: harness_relisted", f"{harness.label} lists {key} again; the absence clock set {row.get('harness_unverified_since')} was cleared {stamp}"
    verb = "started" if action == "start" else f"restarted (the stored clock {row.get('harness_unverified_since')!r} was unreadable)"
    return HARNESS_CLOCK_METRIC, f"{verb}: harness_absent", f"{harness.label} does not list {key}; the absence clock {verb} {stamp}"


def _write_evidence(state: StateManagementInterface, run_id: str, action: Action, row: Mapping[str, Any], harness: ListedHarness, *, stamp: str) -> None:
    metric, value_text, excerpt = _evidence(action, row, harness, stamp=stamp)
    record: dict[str, Any] = {
        "refresh_run_id": run_id,
        "source_id": f"harness_{harness.runtime}",
        "fetch_method": f"harness pass over {harness.label}"[:_MAX_EXCERPT],
        "fetched_at": stamp,
        "provider": adapter_for_runtime(harness.runtime).provider,
        "runtime": harness.runtime,
        "model": row["model"],
        "effort": row["effort"],
        "metric": metric,
        "value_number": None,
        "value_text": value_text,
        "raw_excerpt": excerpt[:_MAX_EXCERPT],
        "fetch_status": "ok",
    }
    require_completed(
        state.write_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MODEL_CAPABILITY_OBSERVATION, "record": record}),
        "record model capability harness pass evidence",
    )


def _apply_one(state: StateManagementInterface, run_id: str, action: Action, row: Mapping[str, Any], harness: ListedHarness, *, stamp: str) -> bool:
    """One predicated update on id + acceptance + updated_at, then its evidence row; False when another writer moved the cell first."""
    updated_at = row.get("updated_at")
    if updated_at is None:
        raise HarnessPassError(f"cell {row['id']} has no updated_at to guard the write with.")
    affected = require_updated(
        state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_MODEL_CAPABILITY_CELL, "filters": {"id": row["id"], "acceptance": row["acceptance"], "updated_at": updated_at}},
            _updates(action, row, harness, run_id=run_id, stamp=stamp),
        ),
    )
    if affected == 0:
        return False
    _write_evidence(state, run_id, action, row, harness, stamp=stamp)
    return True


def _models_of(keys: Sequence[str]) -> dict[str, list[str]]:
    """`runtime/model@effort` keys grouped as runtime/model -> efforts."""
    grouped: dict[str, list[str]] = {}
    for key in keys:
        model, _, effort = key.partition("@")
        grouped.setdefault(model, []).append(effort)
    return grouped


def _absence_advisories(report: HarnessPassReport, authority: Mapping[str, ListedHarness]) -> list[str]:
    """One loud line per model the harness no longer lists: its clock started, or it was withdrawn."""
    lines: list[str] = []
    for keys, outcome in ((report.clock_started, "its absence clock started"), (report.withdrawn, "the window has passed, so it was withdrawn (reversible; a relisting reinstates it)")):
        for model, efforts in _models_of(keys).items():
            runtime = model.partition("/")[0]
            lines.append(f"{authority[runtime].label} does not list {model.partition('/')[2]}: {outcome} for {sorted(efforts)}")
    return lines


def _record(report: HarnessPassReport, action: Action, key: CellKey) -> None:
    {"start": report.clock_started, "restart": report.clock_started, "clear": report.clock_cleared, "withdraw": report.withdrawn, "reinstate": report.reinstated}[action].append(key_text(key))


def _current_for_cell(row: Mapping[str, Any], harness: ListedHarness, *, now: datetime) -> bool:
    window = _window(row)
    return window is not None and harness.fetched_at is not None and now - harness.fetched_at <= window


def apply_harness_pass(
    state: StateManagementInterface,
    run_id: str,
    run: DiscoveryRun,
    rows: Sequence[Mapping[str, Any]],
    *,
    now: datetime,
) -> HarnessPassReport:
    """Move the harness-absence clocks of every cell whose runtime has an authoritative list.

    `rows` is the whole cell table (the caller reads it with no ceiling, so history can never hide
    a cell from this pass). A cell another writer changed after it was read is skipped and named in
    an advisory; the next run decides it again. A withdrawn cell with a retained score is
    reinstated with its values; one without a score is left as it is and named, because there is
    nothing to reinstate (record the cell instead)."""
    authority, advisories = harness_authority(run, now=now)
    report = HarnessPassReport(advisories=advisories)
    stamp = now.isoformat()
    for row in rows:
        harness = authority.get(str(row["runtime"]))
        if harness is not None and not _current_for_cell(row, harness, now=now):
            report.advisories.append(f"{key_text(_cell_key(row))}: harness age or policy window is unknown or stale; no clock transition is justified")
            continue
        action = None if harness is None else decide(row, harness, now=now)
        if harness is None or action is None:
            continue
        key = _cell_key(row)
        if action == "reinstate" and row.get("capability_score") is None:
            report.advisories.append(f"{key_text(key)} was withdrawn by the harness pass and is listed again, but it kept no score, so it was left withdrawn; record the cell to serve it")
            continue
        if _apply_one(state, run_id, action, row, harness, stamp=stamp):
            _record(report, action, key)
        else:
            report.advisories.append(f"{key_text(key)} changed after the harness pass read it; it was left for the next run")
    report.advisories.extend(_absence_advisories(report, authority))
    return report


def refresh_outlook(row: Mapping[str, Any], *, now: datetime) -> dict[str, Any]:
    """Whether the next refresh would withdraw a reinstated cell again (design 3.3, 3.5).

    The harness absence clock is null when the harness confirms the model or it was never
    checked; only a set clock past the cell's window makes the refresh withdraw the cell again.
    An unreadable clock is reported as unknown, never as a refusal (rul_becb4bed)."""
    since_raw = row.get("harness_unverified_since")
    if since_raw is None:
        return {"undone_by_next_refresh": False, "detail": "no harness absence clock is set on this cell, so a refresh has no reason to withdraw it again."}
    since = _parse_clock(since_raw)
    if since is None:
        return {"undone_by_next_refresh": None, "detail": f"harness_unverified_since is unreadable ({since_raw!r}); whether a refresh withdraws this cell again is not known."}
    window = _window(row)
    if window is None:
        return {"undone_by_next_refresh": None, "detail": "the cell policy window is unknown; whether a refresh withdraws this cell again is not known."}
    deadline = since + window
    if now > deadline:
        return {
            "undone_by_next_refresh": True,
            "detail": f"the harness has not listed this model since {since.isoformat()} and the window ended {deadline.isoformat()}: the next refresh withdraws this cell again unless the harness lists the model by then.",
        }
    return {
        "undone_by_next_refresh": False,
        "detail": f"the harness has not listed this model since {since.isoformat()}; a refresh after {deadline.isoformat()} withdraws this cell again unless the harness lists the model.",
    }


__all__ = [
    "ACCEPTANCE_CHANGE_METRIC",
    "HARNESS_CLOCK_METRIC",
    "HarnessPassError",
    "HarnessPassReport",
    "ListedHarness",
    "apply_harness_pass",
    "decide",
    "harness_authority",
    "refresh_outlook",
]
