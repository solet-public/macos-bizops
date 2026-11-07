#!/usr/bin/env python3
"""Offline smoke for managed-session usage joins and coverage."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import patch

from session_usage_verbs_smoke import (
    NOW,
    _check,
    _context,
    _mapping,
    _module,
    _spawn,
    _state,
    _total,
    _unit,
    _usage,
    run_assertions,
)


def _assert_observed_from_evidence() -> None:
    state, raw = _state()
    _spawn(state, raw, "agi-observed", model="dispatch-model", effort="low")
    _mapping(state, "agi-observed", "runtime-observed")
    row = _total(
        "runtime-observed",
        model="unknown",
        usage=_usage(input_tokens=13, output_tokens=29, reasoning_output_tokens=19),
    )
    answer = _unit(state, {"agent_instance_id": "agi-observed"}, [row])
    session = answer["sessions"][0]
    _check(
        session["dispatch_model"] == "dispatch-model" and session["observed"][0]["model"] == "unknown" and session["observed"][0]["effort"] == "unknown" and session["executor_mismatch"] is False,
        "OBSERVED_FROM_EVIDENCE_NOT_DISPATCH_COLUMNS",
    )


def _assert_mismatch() -> None:
    state, raw = _state()
    _spawn(state, raw, "agi-mismatch", model="dispatch-model", effort="low")
    _mapping(state, "agi-mismatch", "runtime-mismatch")
    answer = _unit(
        state,
        {"agent_instance_id": "agi-mismatch"},
        [_total("runtime-mismatch", model="claude-opus-5-5")],
    )
    _check(answer["sessions"][0]["executor_mismatch"] is True, "MISMATCH_FLAGGED_WHEN_OBSERVED_DIFFERS")


def _assert_none_without_mapping() -> None:
    state, raw = _state()
    _spawn(state, raw, "agi-no-map")
    answer = _unit(state, {"agent_instance_id": "agi-no-map"}, [])
    _check(
        answer["coverage"] == "none" and answer["notes"]["workers_without_runtime_ids"] == 1 and answer["sessions"][0]["runtime_session_ids"] == [],
        "COVERAGE_NONE_WITHOUT_MAPPING_NEVER_REFUSES",
    )


def _assert_ledger_unavailable() -> None:
    state, raw = _state()
    _spawn(state, raw, "agi-no-ledger")
    _mapping(state, "agi-no-ledger", "runtime-no-ledger")
    answer = _unit(state, {"agent_instance_id": "agi-no-ledger"}, [], unavailable=True)
    module = _module()
    window = module.read_session_usage_window({}, totals_reader=None, catalog=None, now=NOW)
    _check(
        answer["coverage"] == "none" and "ledger_service_unavailable" in answer["notes"]["advisories"] and window == {"status": "unknown", "sessions": [], "advisories": ["ledger_service_unavailable"]},
        "COVERAGE_NONE_WHEN_LEDGER_SERVICE_UNAVAILABLE_NEVER_REFUSES",
    )


def _assert_partial_unmapped() -> None:
    state, raw = _state()
    _spawn(state, raw, "agi-partial-mapped")
    _spawn(state, raw, "agi-partial-unmapped")
    _mapping(state, "agi-partial-mapped", "runtime-partial")
    answer = _unit(
        state,
        {"unit_id": "unt-usage"},
        [_total("runtime-partial")],
    )
    _check(
        answer["coverage"] == "partial" and answer["notes"]["workers_without_runtime_ids"] == 1 and answer["notes"]["runtime_session_ids_resolved"] == 1,
        "COVERAGE_PARTIAL_WHEN_ONE_SESSION_UNMAPPED",
    )

    state, raw = _state()
    _spawn(state, raw, "agi-mapping-read-failed")
    _context(state, "agi-mapping-read-failed", "runtime-mapping-read-failed")
    from agent_messaging_plugin import session_usage_helpers

    answer: dict[str, Any] | None = None
    try:
        with patch.object(
            session_usage_helpers,
            "list_session_claude_mappings",
            side_effect=RuntimeError("fixture mapping read failure"),
        ):
            answer = _unit(
                state,
                {"agent_instance_id": "agi-mapping-read-failed"},
                [_total("runtime-mapping-read-failed")],
            )
    except TypeError:
        pass
    session = answer["sessions"][0] if answer is not None else {}
    _check(
        answer is not None and answer["coverage"] == "partial" and session["runtime_session_ids"] == ["runtime-mapping-read-failed"] and len(session["observed"]) == 1 and "session_claude_mapping_unavailable:RuntimeError:agi-mapping-read-failed" in answer["notes"]["advisories"],
        "COVERAGE_PARTIAL_WHEN_ONE_SESSION_UNMAPPED",
    )


def _assert_coverage_full_zero() -> None:
    state, raw = _state()
    _spawn(state, raw, "agi-full-usage")
    _spawn(state, raw, "agi-full-zero")
    _mapping(state, "agi-full-usage", "runtime-full-usage")
    _mapping(state, "agi-full-zero", "runtime-full-zero")
    zero_row = _total("runtime-full-zero", usage_event_count=0)
    zero_row["by_model"] = {}
    answer = _unit(
        state,
        {"unit_id": "unt-usage"},
        [_total("runtime-full-usage"), zero_row],
    )
    _check(answer["coverage"] == "full", "COVERAGE_FULL_WHEN_EVERY_SESSION_COVERED_OR_MEASURED_ZERO")


def _assert_same_worker_gap() -> None:
    state, raw = _state()
    _spawn(state, raw, "agi-mixed-worker")
    _mapping(state, "agi-mixed-worker", "runtime-mixed-good")
    _mapping(state, "agi-mixed-worker", "runtime-mixed-missing")
    answer = _unit(
        state,
        {"agent_instance_id": "agi-mixed-worker"},
        [_total("runtime-mixed-good")],
    )
    _check(
        answer["coverage"] == "partial"
        and answer["notes"]["runtime_session_ids_not_found"] == 1
        and answer["notes"]["runtime_session_ids_missing_values"] == ["runtime-mixed-missing"]
        and answer["notes"]["runtime_session_id_causes"]["runtime-mixed-missing"] == ["not_found"]
        and answer["notes"]["runtime_session_ids_resolved"] == 1,
        "SAME_WORKER_MIXED_RESOLVED_NOT_FOUND_IS_PARTIAL",
    )


def _assert_partial_bounded() -> None:
    state, raw = _state()
    _spawn(state, raw, "agi-bounded")
    _mapping(state, "agi-bounded", "runtime-bounded")
    partial = _total(
        "runtime-bounded",
        status="partial",
        advisories=["usage_pages_truncated:100"],
    )
    answer = _unit(state, {"agent_instance_id": "agi-bounded"}, [partial])
    _check(
        answer["coverage"] == "partial" and answer["notes"]["runtime_session_ids_incomplete"] == 1 and "usage_incomplete:runtime-bounded:partial" in answer["notes"]["advisories"],
        "S1_PARTIAL_BOUNDED_TRUNCATED_PROPAGATES",
    )


def _assert_multi_session_sum() -> None:
    state, raw = _state()
    _spawn(state, raw, "agi-cleared", model="claude-opus-5-5", effort="high")
    _mapping(state, "agi-cleared", "runtime-clear-old")
    _mapping(state, "agi-cleared", "runtime-clear-new")
    rows = [
        _total("runtime-clear-old", usage=_usage(input_tokens=10, output_tokens=4)),
        _total("runtime-clear-new", usage=_usage(input_tokens=20, output_tokens=6)),
    ]
    answer = _unit(state, {"agent_instance_id": "agi-cleared"}, rows)
    observed = answer["sessions"][0]["observed"]
    _check(
        answer["coverage"] == "full" and len(observed) == 1 and observed[0]["effort"] == "unknown" and observed[0]["usage"]["input_tokens"] == 30 and observed[0]["usage"]["output_tokens"] == 10,
        "MULTI_SESSION_ACROSS_CLEAR_SUMMED_PER_TUPLE",
    )


def _multi_lane_contributions_match(
    sessions: dict[str, Any],
    expected_usage: dict[str, tuple[str, int, int]],
) -> bool:
    for instance, (runtime_id, input_tokens, output_tokens) in expected_usage.items():
        session = sessions.get(instance)
        if session is None or session.get("runtime_session_ids") != [runtime_id]:
            return False
        observed = session.get("observed", [])
        if not isinstance(observed, list) or len(observed) != 1:
            return False
        usage = observed[0].get("usage", {})
        if usage.get("input_tokens") != input_tokens or usage.get("output_tokens") != output_tokens:
            return False
    return True


def _assert_multi_lane() -> None:
    state, raw = _state()
    instances = ["agi-lane-author", "agi-lane-reviewer", "agi-lane-planner"]
    expected_usage: dict[str, tuple[str, int, int]] = {}
    totals = []
    for index, instance in enumerate(instances):
        runtime_id = f"runtime-{index}"
        input_tokens = (index + 1) * 10
        output_tokens = (index + 1) * 5
        _spawn(state, raw, instance)
        _mapping(state, instance, runtime_id)
        expected_usage[instance] = (runtime_id, input_tokens, output_tokens)
        totals.append(
            _total(
                runtime_id,
                usage=_usage(input_tokens=input_tokens, output_tokens=output_tokens),
            )
        )
    answer = _unit(
        state,
        {"unit_id": "unt-usage"},
        totals,
    )
    sessions = {row["agent_instance_id"]: row for row in answer["sessions"]}
    every_session_joined = answer["coverage"] == "full" and set(sessions) == set(instances) and _multi_lane_contributions_match(sessions, expected_usage)
    _check(
        every_session_joined,
        "MULTI_LANE_UNIT_EVERY_MANAGED_SESSION_JOINED",
    )


def _assert_codex_context_id() -> None:
    state, raw = _state()
    _spawn(state, raw, "agi-codex", runtime="codex", model="gpt-6-sol", effort="high")
    _context(
        state,
        "agi-codex",
        "rollout-codex-runtime-id",
        runtime="codex",
        provider="openai",
        effort="high",
    )
    answer = _unit(
        state,
        {"agent_instance_id": "agi-codex"},
        [_total("rollout-codex-runtime-id", model="gpt-6-sol", adapter="codex")],
    )
    session = answer["sessions"][0]
    _check(
        session["runtime_session_ids"] == ["rollout-codex-runtime-id"] and session["provider"] == "openai" and session["runtime"] == "codex",
        "CODEX_RUNTIME_SESSION_ID_FROM_CONTEXT_STATUS",
    )


def _assert_effort_unknown() -> None:
    state, raw = _state()
    _spawn(state, raw, "agi-effort-unknown", model="claude-opus-5-5", effort="xhigh")
    _mapping(state, "agi-effort-unknown", "runtime-effort-unknown")
    answer = _unit(
        state,
        {"agent_instance_id": "agi-effort-unknown"},
        [_total("runtime-effort-unknown")],
    )
    _check(
        answer["sessions"][0]["observed"][0]["effort"] == "unknown" and answer["notes"]["effort_unknown_sessions"] == 1 and answer["sessions"][0]["executor_mismatch"] is False,
        "EFFORT_UNKNOWN_KEPT_AND_COUNTED",
    )


def _assert_effort_chronology() -> None:
    state, raw = _state()
    _spawn(state, raw, "agi-effort-chronology", model="claude-opus-5-5", effort="high")
    _mapping(state, "agi-effort-chronology", "runtime-effort-old")
    _mapping(state, "agi-effort-chronology", "runtime-effort-current")
    _context(state, "agi-effort-chronology", "runtime-effort-current", effort="high")
    rows = [
        _total("runtime-effort-old", model="claude-opus-5-5"),
        _total("runtime-effort-current", model="claude-opus-5-5"),
    ]
    answer = _unit(state, {"agent_instance_id": "agi-effort-chronology"}, rows)
    observed = answer["sessions"][0]["observed"]
    by_effort = {row["effort"]: row for row in observed}
    _check(
        set(by_effort) == {"unknown", "high"} and answer["notes"]["historical_effort_unavailable"] == 1 and answer["sessions"][0]["executor_mismatch"] is False,
        "EFFORT_TWO_ID_CHRONOLOGY_MATCHES_CURRENT_ONLY",
    )


def _assert_as_of_lag() -> None:
    state, raw = _state()
    _spawn(state, raw, "agi-as-of")
    _mapping(state, "agi-as-of", "runtime-as-of")
    answer = _unit(
        state,
        {"agent_instance_id": "agi-as-of"},
        [
            _total(
                "runtime-as-of",
                last_event_at="2026-10-17T10:00:00+00:00",
                last_poll_at="2026-10-17T11:00:00+00:00",
            ),
        ],
    )
    _check(
        answer["as_of"] == "2026-10-17T10:00:00+00:00" and answer["lag_seconds"] == 7200.0,
        "AS_OF_AND_LAG_PRESENT",
    )


def _assert_totals_chunked() -> None:
    state, raw = _state()
    for index in range(201):
        instance = f"agi-chunk-{index}"
        _spawn(state, raw, instance)
        _mapping(state, instance, f"runtime-chunk-{index}")
    chunks: list[list[str]] = []

    def reader(*, runtime_session_ids: list[str]) -> dict[str, Any]:
        chunks.append(runtime_session_ids)
        return {"status": "complete", "sessions": [], "not_found": runtime_session_ids, "advisories": []}

    answer = _module().read_unit_usage(state, {"unit_id": "unt-usage"}, totals_reader=reader, now=NOW)
    _check(
        answer["notes"]["runtime_session_id_count"] == 201 and [len(chunk) for chunk in chunks] == [200, 1] and max(map(len, chunks)) <= 200,
        "TOTALS_CHUNKED_AT_200_IDS",
    )


ASSERTIONS: tuple[tuple[str, Callable[[], None]], ...] = (
    ("OBSERVED_FROM_EVIDENCE_NOT_DISPATCH_COLUMNS", _assert_observed_from_evidence),
    ("MISMATCH_FLAGGED_WHEN_OBSERVED_DIFFERS", _assert_mismatch),
    ("COVERAGE_NONE_WITHOUT_MAPPING_NEVER_REFUSES", _assert_none_without_mapping),
    ("COVERAGE_NONE_WHEN_LEDGER_SERVICE_UNAVAILABLE_NEVER_REFUSES", _assert_ledger_unavailable),
    ("COVERAGE_PARTIAL_WHEN_ONE_SESSION_UNMAPPED", _assert_partial_unmapped),
    ("COVERAGE_FULL_WHEN_EVERY_SESSION_COVERED_OR_MEASURED_ZERO", _assert_coverage_full_zero),
    ("SAME_WORKER_MIXED_RESOLVED_NOT_FOUND_IS_PARTIAL", _assert_same_worker_gap),
    ("S1_PARTIAL_BOUNDED_TRUNCATED_PROPAGATES", _assert_partial_bounded),
    ("MULTI_SESSION_ACROSS_CLEAR_SUMMED_PER_TUPLE", _assert_multi_session_sum),
    ("MULTI_LANE_UNIT_EVERY_MANAGED_SESSION_JOINED", _assert_multi_lane),
    ("CODEX_RUNTIME_SESSION_ID_FROM_CONTEXT_STATUS", _assert_codex_context_id),
    ("EFFORT_UNKNOWN_KEPT_AND_COUNTED", _assert_effort_unknown),
    ("EFFORT_TWO_ID_CHRONOLOGY_MATCHES_CURRENT_ONLY", _assert_effort_chronology),
    ("AS_OF_AND_LAG_PRESENT", _assert_as_of_lag),
    ("TOTALS_CHUNKED_AT_200_IDS", _assert_totals_chunked),
)


def main() -> int:
    return run_assertions(ASSERTIONS)


if __name__ == "__main__":
    raise SystemExit(main())
