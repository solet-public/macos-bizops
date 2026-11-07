"""Read-only initial-attempt uptake from causal, assignment-bound receipts.

Receipt observation times are neither first model tokens nor an atomic snapshot.
No current-attempt projection is used to repair historical initial outcomes.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import StateOperationError, require_records

from .managed_dispatch_errors import DispatchError
from .managed_dispatch_unit import require_assignment
from .schema import TABLE_MANAGED_DISPATCH, TABLE_MANAGED_DISPATCH_EVENT

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

_PAGE_SIZE = 100
_MAX_PAGES = 100
_MAX_SAMPLE = 500
_RUNTIME_VENDORS = {"codex": "openai", "claude_code": "anthropic"}


def _time(value: object) -> datetime | None:
    if not isinstance(value, (datetime, str)):
        return None
    try:
        parsed = value if isinstance(value, datetime) else datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.astimezone(UTC) if parsed.tzinfo is not None else None


def _input_time(value: object, name: str) -> datetime:
    """Validate caller syntax separately from advisory missing source telemetry."""
    parsed = _time(value)
    if parsed is None:
        raise DispatchError("uptake_time_invalid", f"{name} must be an aware ISO timestamp.")
    return parsed


def _integer(value: object, name: str, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        raise DispatchError("uptake_integer_invalid", f"{name} must be an integer.")
    try:
        parsed = int(value)
    except ValueError as exc:
        raise DispatchError("uptake_integer_invalid", f"{name} must be an integer.") from exc
    if not 1 <= parsed <= maximum:
        raise DispatchError("uptake_integer_invalid", f"{name} must be from 1 through {maximum}.")
    return parsed


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _page_rows(
    state: StateManagementInterface,
    query: dict[str, Any],
) -> list[dict[str, Any]]:
    result = state.query_ordered(AGENT_ROLE_BINDING_NAMESPACE, query)
    rows = require_records(result)
    data = result.get("data")
    if not isinstance(data, Mapping) or data.get("records") != rows:
        raise StateOperationError("Ordered read returned malformed records; completeness is unknown.")
    return [{field: value.isoformat() if isinstance(value, datetime) else value for field, value in row.items()} for row in rows]


def _read_pages(
    state: StateManagementInterface,
    table: str,
    first: str,
    filters: dict[str, object],
) -> tuple[list[dict[str, Any]], bool, list[str]]:
    """Exhaust tie-safe pages, exposing any budget or provider interruption."""
    query: dict[str, Any] = {
        "table": table,
        "filters": filters,
        "order_by": [[first, "asc"], ["id", "asc"]],
        "limit": _PAGE_SIZE,
    }
    rows: list[dict[str, Any]] = []
    for _ in range(_MAX_PAGES):
        try:
            page = _page_rows(state, query)
        except StateOperationError as exc:
            return rows, False, [f"{table}: {exc}"]
        rows.extend(page)
        if len(page) < _PAGE_SIZE:
            return rows, True, []
        last = page[-1]
        cursor = [last.get(first), last.get("id")]
        if None in cursor or "" in cursor or cursor == query.get("after"):
            return rows, False, [f"{table}: cursor missing or did not advance"]
        query["after"] = cursor
    return rows, False, [f"{table}: bounded read budget exhausted ({_MAX_PAGES} pages)"]


def _observed_events(
    events: list[dict[str, Any]],
    as_of: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Retain malformed evidence separately; future receipts never enter attribution."""
    observed: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    future: list[str] = []
    for event in events:
        at = _time(event.get("event_at"))
        if at is None:
            malformed.append(event)
        elif at <= as_of:
            observed.append(event)
        else:
            future.append(_text(event.get("id")))
    observed.sort(key=lambda event: (_input_time(event.get("event_at"), "event_at"), _text(event.get("id"))))
    return observed, malformed, future


def _initial_event(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Identify attempt one by its durable key, never by earliest successful retry."""
    initial = [event for event in events if event.get("event_kind") == "first_turn" and event.get("event_id") == "first-turn:1" and event.get("accepted") is True]
    return initial[0] if len(initial) == 1 else None


def _historical_labels(initial: Mapping[str, Any] | None) -> dict[str, Any]:
    """Use only initial-attempt labels; current projections can name replacements."""
    payload = initial.get("payload") if initial is not None else None
    evidence = payload if isinstance(payload, Mapping) else {}
    runtime = _text(evidence.get("agent_runtime")) or "unknown"
    return {
        "transport": _text(evidence.get("host")) or "unknown",
        "runtime": runtime,
        "vendor": _RUNTIME_VENDORS.get(runtime, "unknown"),
        "label_evidence_id": initial.get("id") if initial is not None else None,
        "transport_basis": "initial first_turn.payload.host",
        "runtime_basis": "initial first_turn.payload.agent_runtime",
        "vendor_basis": "runtime product mapping: codex=openai, claude_code=anthropic; otherwise unknown",
    }


def _eligibility(labels: Mapping[str, Any], runtime: str, transport: str) -> str:
    """Unknown eligibility stays in the cohort; known mismatches are disclosed."""
    pairs = ((runtime, labels["runtime"]), (transport, labels["transport"]))
    if any(wanted and actual != "unknown" and wanted != actual for wanted, actual in pairs):
        return "excluded"
    return "unknown" if any(wanted and actual == "unknown" for wanted, actual in pairs) else "eligible"


def _row_evidence(
    contract: Mapping[str, Any],
    events: list[dict[str, Any]],
    as_of: datetime,
) -> dict[str, Any]:
    observed, malformed, future = _observed_events(events, as_of)
    initial = _initial_event(observed)
    return {
        "dispatch_id": contract.get("dispatch_id"),
        "contract_id": contract.get("id"),
        "created_at": contract.get("created_at"),
        "assignment_sha256": contract.get("assignment_sha256"),
        **_historical_labels(initial),
        "initial_event": initial,
        "initial_receipt": initial,
        "observed_events": observed,
        "malformed_events": malformed,
        "source_event_ids": [event.get("event_id") for event in observed],
        "source_row_ids": [event.get("id") for event in observed],
        "malformed_evidence": malformed,
        "excluded_future_row_ids": future,
        "submitted_at": None,
        "delivered_at": None,
        "first_uptake_event_at": None,
        "uptake_event_id": None,
        "uptake_row_id": None,
        "latency_seconds": None,
        "converted": None,
        "mature": None,
        "status": "unknown",
        "reason": "initial_submission_missing",
        "advisories": [],
    }


def _submission_problem(contract: Mapping[str, Any], row: Mapping[str, Any]) -> str:
    """Validate partial evidence before indexing any attribution fields."""
    initial = row["initial_event"]
    if initial is None:
        return "initial_submission_missing"
    if not _text(contract.get("dispatch_id")) or not _text(contract.get("id")):
        return "contract_identity_missing"
    if not _text(initial.get("id")):
        return "initial_receipt_identity_missing"
    if not isinstance(initial.get("payload"), Mapping):
        return "initial_payload_malformed"
    if not _text(initial.get("attempt_agent_instance_id")):
        return "initial_attempt_identity_missing"
    return _submission_timing_problem(contract, initial)


def _submission_timing_problem(contract: Mapping[str, Any], initial: Mapping[str, Any]) -> str:
    """Observation time cannot precede its own immutable dispatch contract."""
    created = _time(contract.get("created_at"))
    submitted = _time(initial.get("event_at"))
    if created is None or submitted is None or submitted < created:
        return "initial_timing_malformed"
    return "" if isinstance(initial["payload"].get("delivered"), bool) else "delivery_evidence_missing"


def _ack_problem(
    event: Mapping[str, Any],
    contract: Mapping[str, Any],
    initial: Mapping[str, Any],
) -> str:
    """Accepted worker authority and exact attempt identity must both survive readback."""
    if event.get("dispatch_id") != contract.get("dispatch_id"):
        return "wrong_dispatch"
    if event.get("accepted") is not True:
        return "rejected_ack"
    if not _text(event.get("event_id")) or not _text(event.get("id")):
        return "ack_receipt_identity_missing"
    attempt = initial.get("attempt_agent_instance_id")
    if event.get("attempt_agent_instance_id") != attempt:
        return "wrong_attempt"
    if event.get("actor_instance_id") != attempt or event.get("actor_role") != contract.get("role_name"):
        return "not_worker_authored"
    return _ack_assignment_problem(event, contract)


def _ack_assignment_problem(event: Mapping[str, Any], contract: Mapping[str, Any]) -> str:
    payload = event.get("payload")
    if not isinstance(payload, Mapping):
        return "ack_payload_malformed"
    try:
        assignment = require_assignment(contract)
    except DispatchError:
        return "assignment_evidence_malformed"
    if payload.get("assignment_sha256") != assignment.sha256:
        return "ack_assignment_mismatch"
    if payload.get("revision_id") != assignment.revision.revision_id:
        return "ack_revision_mismatch"
    if payload.get("role_binding") != contract.get("role_name"):
        return "ack_role_mismatch"
    required = ("scope_readback_sha256", "plan_sha256")
    return "ack_readback_missing" if any(not _text(payload.get(name)) for name in required) else ""


def _first_ack(
    contract: Mapping[str, Any],
    row: dict[str, Any],
    submitted: datetime,
) -> dict[str, Any] | None:
    """Select the earliest valid ACK after submission in receipt/id order."""
    found: dict[str, Any] | None = None
    for event in row["observed_events"]:
        if event.get("event_kind") != "ack":
            continue
        problem = _ack_problem(event, contract, row["initial_event"])
        at = _time(event.get("event_at"))
        if not problem and (at is None or at < submitted):
            problem = "ack_predates_submission"
        if problem:
            row["advisories"].append({"event_id": event.get("event_id"), "reason": problem})
        elif found is None:
            found = event
    return found


def _attribute(contract: Mapping[str, Any], row: dict[str, Any], as_of: datetime, window: int) -> None:
    """Attribute only initial submission and ACK; failures and unknowns remain rows."""
    problem = _submission_problem(contract, row)
    if problem:
        row["reason"] = problem
        return
    initial = row["initial_event"]
    submitted = _input_time(initial["event_at"], "event_at")
    row.update({"attempt_agent_instance_id": initial["attempt_agent_instance_id"], "submitted_at": submitted.isoformat(), "mature": as_of >= submitted + timedelta(seconds=window)})
    if initial["payload"]["delivered"] is False:
        row.update({"converted": False, "status": "failed", "reason": "initial_delivery_failed"})
        _apply_uncertainty(row)
        return
    row["delivered_at"] = submitted.isoformat()
    ack = _first_ack(contract, row, submitted)
    row.update({"converted": False, "status": "failed", "reason": "no_valid_initial_ack"})
    if ack is not None:
        at = _input_time(ack["event_at"], "event_at")
        latency = (at - submitted).total_seconds()
        timely = 0 <= latency <= window
        row.update({"first_uptake_event_at": at.isoformat(), "uptake_event_id": ack.get("event_id"), "uptake_row_id": ack.get("id"), "latency_seconds": latency, "converted": timely, "status": "success" if timely else "failed", "reason": "timely_ack" if timely else "late_ack"})
    _apply_uncertainty(row)


def _apply_uncertainty(row: dict[str, Any]) -> None:
    if row["malformed_events"]:
        row.update({"converted": None, "status": "unknown", "reason": "malformed_event_timing"})
    elif row["filter_eligibility"] == "unknown":
        row.update({"status": "unknown", "reason": "filter_eligibility_unknown"})
    elif any(note["reason"] in {"ack_payload_malformed", "assignment_evidence_malformed", "ack_readback_missing", "ack_receipt_identity_missing"} for note in row["advisories"]):
        row.update({"converted": None, "status": "unknown", "reason": "ack_evidence_malformed"})
    elif row["mature"] is False:
        row["status"] = "pending"


def _cohort(
    state: StateManagementInterface,
    contracts: list[dict[str, Any]],
    *,
    since: datetime,
    as_of: datetime,
    sample: int,
    runtime: str,
    transport: str,
    window: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, list[str]]:
    """Select consecutive eligible contracts before inspecting any ACK outcomes."""
    selected: list[dict[str, Any]] = []
    exclusions: list[dict[str, Any]] = []
    complete = True
    advisories: list[str] = []
    for contract in contracts:
        if len(selected) >= sample:
            break
        created = _time(contract.get("created_at"))
        if created is not None and not since <= created <= as_of:
            continue
        row, exhausted, notes = _contract_evidence(state, contract, as_of)
        complete = complete and exhausted
        advisories.extend(notes)
        eligible = _eligibility(row, runtime, transport)
        row["filter_eligibility"] = eligible
        if eligible == "excluded":
            exclusions.append({"dispatch_id": contract.get("dispatch_id"), "reason": "historical_filter_mismatch", "transport": row["transport"], "runtime": row["runtime"], "label_evidence_id": row["label_evidence_id"]})
            continue
        _attribute(contract, row, as_of, window)
        selected.append(row)
    return selected, exclusions, complete, advisories


def _contract_evidence(
    state: StateManagementInterface,
    contract: Mapping[str, Any],
    as_of: datetime,
) -> tuple[dict[str, Any], bool, list[str]]:
    dispatch_id = _text(contract.get("dispatch_id"))
    if dispatch_id:
        events, complete, notes = _read_pages(state, TABLE_MANAGED_DISPATCH_EVENT, "event_at", {"dispatch_id": dispatch_id})
    else:
        events, complete, notes = [], False, ["Contract has no dispatch_id; event attribution unavailable."]
    return _row_evidence(contract, events, as_of), complete, notes


def _counts(rows: list[dict[str, Any]]) -> dict[str, int | float | None]:
    """The denominator is every selected contract, independent of its outcome."""
    successes = sum(row["converted"] is True for row in rows)
    return {"selected_count": len(rows), "successes": successes, "failed": sum(row["status"] == "failed" for row in rows), "pending": sum(row["mature"] is False for row in rows), "unknown": sum(row["status"] == "unknown" for row in rows), "conversion_rate": successes / len(rows) if rows else None}


def _breakdowns(rows: list[dict[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in ("transport", "runtime", "vendor"):
        buckets: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            buckets.setdefault(row[field], []).append(row)
        result[field] = {label: _counts(members) for label, members in sorted(buckets.items())}
    return result


def _qualified_members(rows: list[dict[str, Any]]) -> bool:
    """Require mature, measurable original eligibility for every denominator member."""
    return len(rows) == 50 and all(row["mature"] is True and row["status"] != "unknown" and row["filter_eligibility"] == "eligible" for row in rows)


def _slo(
    rows: list[dict[str, Any]],
    *,
    complete: bool,
    sample: int,
    window: int,
    runtime: str,
    transport: str,
) -> str:
    """Only the explicitly configured 50-member tmux Codex cohort can qualify."""
    if not rows:
        return "UNMEASURABLE"
    configured = (sample, window, runtime, transport) == (50, 300, "codex", "tmux")
    if not configured or not complete or not _qualified_members(rows):
        return "PARTIAL"
    return "PASS" if sum(row["converted"] is True for row in rows) >= 48 else "FAIL"


def _public_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    internal = {"initial_event", "observed_events", "malformed_events"}
    return [{key: value for key, value in row.items() if key not in internal} for row in rows]


def managed_dispatch_uptake_rate(
    state: StateManagementInterface,
    *,
    since: str,
    as_of: str | None = None,
    window_seconds: int | str = 300,
    sample_size: int | str = 50,
    runtime: str = "",
    transport: str = "",
) -> dict[str, Any]:
    """Select consecutive contracts before outcomes and report initial ACK evidence.

    Missing evidence is advisory. Only caller syntax errors raise DispatchError.
    Reads are capped at 100 rows/page and 100 pages/table or dispatch; exhaustion
    failures remain visible and cannot qualify an SLO. No source writes occur.
    """
    start = _input_time(since, "since")
    cutoff = datetime.now(UTC) if as_of is None else _input_time(as_of, "as_of")
    if start > cutoff:
        raise DispatchError("uptake_range_invalid", "since must not be after as_of.")
    window = _integer(window_seconds, "window_seconds", 86400)
    sample = _integer(sample_size, "sample_size", _MAX_SAMPLE)
    contracts, exhausted, notes = _read_pages(state, TABLE_MANAGED_DISPATCH, "created_at", {})
    rows, exclusions, complete, advisories = _cohort(
        state,
        contracts,
        since=start,
        as_of=cutoff,
        sample=sample,
        runtime=runtime,
        transport=transport,
        window=window,
    )
    reads_complete = exhausted and complete
    status = _slo(rows, complete=reads_complete, sample=sample, window=window, runtime=runtime, transport=transport)
    return {
        "input": {"since": start.isoformat(), "as_of": cutoff.isoformat(), "window_seconds": window, "sample_size": sample, "runtime": runtime, "transport": transport},
        "selected_ids": [row["dispatch_id"] for row in rows],
        "selected_contract_ids": [row["contract_id"] for row in rows],
        "dispatches": _public_rows(rows),
        **_counts(rows),
        "breakdowns": _breakdowns(rows),
        "exclusions": exclusions,
        "reads_complete": reads_complete,
        "slo_status": status,
        "advisories": notes + advisories + (["Cohort is incomplete, immature or not the configured tmux Codex SLO."] if status in {"PARTIAL", "UNMEASURABLE"} else []),
        "provenance": "Receipt observation times; accepted assignment-bound worker ACK proxy, not first model token. Frozen IDs and cutoff describe read evidence, not an atomic database snapshot.",
    }
