#!/usr/bin/env python3
"""Offline smoke for usage pricing, pools, estimates, and priced windows."""

from __future__ import annotations

import copy
import json
import tempfile
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from ananta.llm.session_ledger.usage_totals import HARNESS_USAGE_ADAPTERS
from session_usage_verbs_smoke import (
    NOW,
    PROFILE_PATH,
    AgentMessagingPlugin,
    FrameworkError,
    _check,
    _context,
    _mapping,
    _module,
    _spawn,
    _state,
    _submit,
    _total,
    _totals,
    _unit,
    _usage,
    run_assertions,
)


def _assert_pricing_and_pools() -> None:
    from agent_messaging_plugin import session_usage_unit as unit_module
    from agent_messaging_plugin.allowance_pool_readings import PoolReading

    state, raw = _state()
    _spawn(state, raw, "agi-priced", model="claude-opus-5-5", effort="high")
    _mapping(state, "agi-priced", "runtime-priced")
    pool = PoolReading(
        profile_id="anthropic-max-20x-2026-08-22",
        pool_id="rolling-five-hour",
        consumed=75.0,
        remaining=25.0,
        as_of=NOW - timedelta(hours=2),
        next_reset_at=NOW - timedelta(minutes=1),
        expires_at=NOW + timedelta(hours=1),
        source="smoke fixture",
        recorded_by="smoke",
        recorded_at=NOW - timedelta(hours=2),
    )
    with patch.object(unit_module, "read_pool_readings", return_value=(pool,)):
        answer = _unit(
            state,
            {"agent_instance_id": "agi-priced"},
            [_total("runtime-priced", model="claude-opus-5-5")],
        )
    observed = answer["sessions"][0]["observed"][0]
    _check(
        observed["pricing_basis"] == "list_equivalent:anthropic-max-20x-2026-08-22" and isinstance(observed["priced_usd"], float) and observed["pool_readings"][0]["expired"] is True and "rolling-five-hour" in observed["pool_ids"],
        "PRICING_AND_POOLS_ATTACHED_END_TO_END",
    )


def _assert_unpriced_notes() -> None:
    state, raw = _state()
    _spawn(state, raw, "agi-unpriced", model="claude-sonnet-5-5")
    _mapping(state, "agi-unpriced", "runtime-unpriced")
    answer = _unit(
        state,
        {"agent_instance_id": "agi-unpriced"},
        [_total("runtime-unpriced", model="claude-sonnet-5-5", usage=_usage(input_tokens=17, output_tokens=8))],
    )
    observed = answer["sessions"][0]["observed"][0]
    _check(
        observed["priced_usd"] is None and observed["pricing_basis"].startswith("unpriced:") and "claude-sonnet-5-5" in answer["notes"]["unpriced_models"] and answer["notes"]["unpriced_token_count"] == 25 and answer["notes"]["unpriced_category_count"] > 0,
        "UNPRICED_COUNTED_IN_NOTES",
    )


def _assert_estimates() -> None:
    from agent_messaging_plugin import session_usage_pricing as pricing_module

    receipt = {
        "selected": {
            "runtime": "claude_code",
            "model": "claude-opus-5-5",
            "effort": "high",
            "cost_per_task_usd": 0.41,
        },
    }
    state, raw = _state()
    _spawn(
        state,
        raw,
        "agi-receipt-estimate",
        model="claude-opus-5-5",
        effort="high",
        receipt=receipt,
        difficulty=45.0,
    )
    _mapping(state, "agi-receipt-estimate", "runtime-receipt-estimate")
    receipt_answer = _unit(
        state,
        {"agent_instance_id": "agi-receipt-estimate"},
        [_total("runtime-receipt-estimate", model="claude-opus-5-5")],
    )
    cell = {
        "runtime": "claude_code",
        "model": "claude-opus-5-5",
        "effort": "high",
        "acceptance": "accepted",
        "cost_per_task_usd": 0.62,
    }
    with patch.object(pricing_module, "read_cells_page", return_value=SimpleNamespace(rows=[cell], first_unconsidered=None)):
        lookup_state, lookup_raw = _state()
        _spawn(lookup_state, lookup_raw, "agi-catalog-estimate", model="claude-opus-5-5", effort="high")
        _mapping(lookup_state, "agi-catalog-estimate", "runtime-catalog-estimate")
        _context(lookup_state, "agi-catalog-estimate", "runtime-catalog-estimate", effort="high")
        lookup_answer = _unit(
            lookup_state,
            {"agent_instance_id": "agi-catalog-estimate"},
            [_total("runtime-catalog-estimate", model="claude-opus-5-5", usage=_usage(output_tokens=100))],
        )
    with patch.object(pricing_module, "read_cells_page", return_value=SimpleNamespace(rows=[], first_unconsidered=None)):
        none_state, none_raw = _state()
        _spawn(none_state, none_raw, "agi-no-estimate", model="claude-opus-5-5", effort="high")
        _mapping(none_state, "agi-no-estimate", "runtime-no-estimate")
        _context(none_state, "agi-no-estimate", "runtime-no-estimate", effort="high")
        none_answer = _unit(
            none_state,
            {"agent_instance_id": "agi-no-estimate"},
            [_total("runtime-no-estimate", model="claude-opus-5-5", usage=_usage(output_tokens=100))],
        )
    _check(
        receipt_answer["estimate"]["basis"] == "receipt" and receipt_answer["estimate"]["usd"] == 0.41 and receipt_answer["difficulty_score"] == 45.0 and lookup_answer["estimate"]["basis"] == "catalog_lookup" and lookup_answer["estimate"]["usd"] == 0.62 and none_answer["estimate"]["basis"] == "none",
        "ESTIMATE_RECEIPT_THEN_CATALOG_LOOKUP_THEN_NONE",
    )


def _assert_bridge_window_arguments() -> None:
    unit_method = AgentMessagingPlugin.read_unit_usage
    window_method = AgentMessagingPlugin.read_session_usage_window
    unit_key = "plugin::agent_messaging_plugin::read_unit_usage"
    window_key = "plugin::agent_messaging_plugin::read_session_usage_window"
    _submit(window_method, window_key, {"since": "2026-10-17T00:00:00Z", "until": "2026-10-18T00:00:00Z", "vendor": "claude_code", "priced": True})
    for _method, key in ((unit_method, unit_key), (window_method, window_key)):
        _check(
            AgentMessagingPlugin.get_edge_process_definitions(object.__new__(AgentMessagingPlugin))[key.rsplit("::", 1)[1]].error_processor_template_customizations.retryable,
            "BRIDGE_ACCEPTS_PRICED_WINDOW_ARGUMENTS",
        )
    try:
        _submit(window_method, window_key, {"runtime_session_ids": ["rt-1"]})
    except FrameworkError as exc:
        _check(exc.error_code == "action.unknown_arguments", "BRIDGE_ACCEPTS_PRICED_WINDOW_ARGUMENTS")
        return
    raise AssertionError("BRIDGE_ACCEPTS_PRICED_WINDOW_ARGUMENTS")


def _window_response() -> dict[str, Any]:
    return {
        "status": "unknown:bounded",
        "sessions": [
            {
                "runtime_session_id": "rt-window",
                "adapter": "claude_code",
                "status": "complete",
                "meter": "supported",
                "by_model": {
                    "claude-opus-5-5": _usage(input_tokens=1000, cached_input_tokens=0, cache_write_input_tokens=0, output_tokens=500, reasoning_output_tokens=100),
                    "unknown": _usage(input_tokens=11, cached_input_tokens=5, cache_write_input_tokens=3, output_tokens=30, reasoning_output_tokens=20, tool_calls=1),
                },
                "usage_event_count": 2,
                "last_event_at": "2026-10-17T11:55:00+00:00",
                "last_poll_at": "2026-10-17T11:59:00+00:00",
                "advisories": ["model_unavailable:1 events; their model is not recorded"],
            },
        ],
        "not_found": [],
        "advisories": ["enumeration_partial"],
        "overflow": True,
        "sessions_at_least": 101,
        "as_of": "2026-10-17T11:55:00+00:00",
    }


def _assert_priced_window_models() -> None:
    module = _module()
    response = _window_response()
    calls: list[dict[str, Any]] = []

    def reader(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return copy.deepcopy(response)

    since = "2026-10-17T00:00:00Z"
    until = "2026-10-18T00:00:00Z"
    answer = module.read_session_usage_window(
        {"since": since, "until": until, "vendor": "claude_code", "priced": True},
        totals_reader=reader,
        catalog=None,
        now=NOW,
    )
    _check(
        len(calls) == 1
        and calls[0] == {"since": since, "until": until, "vendor": "claude_code"}
        and answer["status"] == "unknown:bounded"
        and answer["overflow"] is True
        and answer["pricing_by_model"]["claude-opus-5-5"]["basis"] == "list_equivalent:anthropic-max-20x-2026-08-22"
        and answer["pricing_by_model"]["claude-opus-5-5"]["usd"] is not None
        and answer["pricing_by_model"]["unknown"] == {"usd": None, "basis": "unpriced:model_unknown"}
        and answer["unpriced_token_count"] == 49
        and answer["unpriced_models"] == ["unknown"],
        "PRICED_WINDOW_ONE_PRICED_ONE_UNPRICED_MODEL",
    )


def _assert_raw_window_exact() -> None:
    module = _module()
    response = _window_response()
    calls: list[dict[str, Any]] = []

    def reader(**kwargs: Any) -> dict[str, Any]:
        calls.append(kwargs)
        return copy.deepcopy(response)

    absent = module.read_session_usage_window(
        {"since": "s", "until": "u", "vendor": "claude_code"},
        totals_reader=reader,
        catalog=None,
        now=NOW,
    )
    false = module.read_session_usage_window(
        {"since": "s", "until": "u", "vendor": "claude_code", "priced": False},
        totals_reader=reader,
        catalog=None,
        now=NOW,
    )
    error_message = "parameter_invalid: provide ids or vendor, since and until"
    try:
        module.read_session_usage_window(
            {"vendor": "claude_code"},
            totals_reader=lambda **_kwargs: (_ for _ in ()).throw(ValueError(error_message)),
            catalog=None,
            now=NOW,
        )
    except ValueError as exc:
        caller_error_preserved = str(exc) == error_message
    else:
        caller_error_preserved = False
    _check(
        absent == response and false == response and len(calls) == 2 and all(call == {"since": "s", "until": "u", "vendor": "claude_code"} for call in calls) and caller_error_preserved,
        "PRICED_ABSENT_EXACT_S1_KEYS",
    )


def _assert_fixture_adapter_profile() -> None:
    from ananta.llm.session_ledger.usage_totals import register_usage_adapter, totals_for_events

    import agent_messaging_plugin.session_usage_helpers as helpers_module
    import agent_messaging_plugin.session_usage_verbs as module

    def fixture_adapter(events: Any, _since: Any, _until: Any) -> dict[str, Any]:
        return {"by_model": {"fixture-model": _usage(input_tokens=250, output_tokens=100)}, "usage_event_count": len(events), "advisories": []}

    original = dict(HARNESS_USAGE_ADAPTERS)
    profile_data = json.loads(PROFILE_PATH.read_text(encoding="utf-8"))
    template = next(row for row in profile_data["profiles"] if row.get("kind") == "metered_api")
    fixture_profile = copy.deepcopy(template)
    fixture_profile.update(
        {
            "profile_id": "fixture-runtime-metered-profile",
            "profile_version": "smoke-only",
            "provider": "fixture_provider",
            "runtime": "fixture_harness",
            "model": "fixture-model",
            "effective_at": "2026-01-01T00:00:00+00:00",
            "source_ref": "smoke-only fixture",
            "fetched_at": "2026-01-01T00:00:00+00:00",
            "refresh_status": "current",
        },
    )
    with tempfile.TemporaryDirectory(prefix="usage-profile-") as directory:
        catalog_path = Path(directory) / "usage_economics.v1.json"
        state, raw = _state()
        _spawn(state, raw, "agi-fixture-harness", runtime="fixture_harness", model="fixture-model")
        _mapping(state, "agi-fixture-harness", "runtime-fixture-harness")
        event_rows = [{"event_at": NOW.isoformat()}]
        register_usage_adapter("fixture_harness", fixture_adapter)
        try:
            folded = totals_for_events("fixture_harness", event_rows)

            def reader(**_kwargs: Any) -> dict[str, Any]:
                return _totals(
                    [
                        _total(
                            "runtime-fixture-harness",
                            model="fixture-model",
                            usage=folded["by_model"]["fixture-model"],
                            adapter="fixture_harness",
                        ),
                    ],
                )

            with patch.object(helpers_module, "USAGE_ECONOMICS_PROFILE_PATH", catalog_path):
                catalog_path.write_text(json.dumps(profile_data), encoding="utf-8")
                before = module.read_unit_usage(
                    state,
                    {"agent_instance_id": "agi-fixture-harness"},
                    totals_reader=reader,
                    now=NOW,
                )
                profile_data["profiles"].append(fixture_profile)
                catalog_path.write_text(json.dumps(profile_data), encoding="utf-8")
                after = module.read_unit_usage(
                    state,
                    {"agent_instance_id": "agi-fixture-harness"},
                    totals_reader=reader,
                    now=NOW,
                )
        finally:
            HARNESS_USAGE_ADAPTERS.clear()
            HARNESS_USAGE_ADAPTERS.update(original)
    _check(
        before["sessions"][0]["observed"][0]["pricing_basis"].startswith("unpriced:") and after["sessions"][0]["observed"][0]["pricing_basis"] == "metered:fixture-runtime-metered-profile" and after["sessions"][0]["observed"][0]["priced_usd"] is not None,
        "FIXTURE_HARNESS_UNIT_USAGE_END_TO_END",
    )


ASSERTIONS: tuple[tuple[str, Callable[[], None]], ...] = (
    ("PRICING_AND_POOLS_ATTACHED_END_TO_END", _assert_pricing_and_pools),
    ("UNPRICED_COUNTED_IN_NOTES", _assert_unpriced_notes),
    ("ESTIMATE_RECEIPT_THEN_CATALOG_LOOKUP_THEN_NONE", _assert_estimates),
    ("BRIDGE_ACCEPTS_PRICED_WINDOW_ARGUMENTS", _assert_bridge_window_arguments),
    ("PRICED_WINDOW_ONE_PRICED_ONE_UNPRICED_MODEL", _assert_priced_window_models),
    ("PRICED_ABSENT_EXACT_S1_KEYS", _assert_raw_window_exact),
    ("FIXTURE_HARNESS_UNIT_USAGE_END_TO_END", _assert_fixture_adapter_profile),
)


def main() -> int:
    return run_assertions(ASSERTIONS)


if __name__ == "__main__":
    raise SystemExit(main())
