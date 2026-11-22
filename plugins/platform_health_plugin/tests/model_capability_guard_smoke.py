#!/usr/bin/env python3
"""RED/GREEN smoke for model_capability_guard.py (no network, no real clock,
no real peer message, no live catalog).

The refresh under test is the REAL ``refresh_model_capability_catalog`` verb
body running against the repo's real-shape in-memory state fake, with every
fetch answered by an ``httpx.MockTransport`` handler (real-shaped fixtures from
the agent_messaging refresh smoke) and the harness model lists read from that
smoke's hermetic fixture (a temporary HOME with a Claude cache and a stub Codex
binary), so no result depends on the host it runs on. A recording double stands in for
``peer_send_by_name`` so the smoke can inspect the operator notices. Age scenarios use a fake clock and cells seeded
directly, so no test depends on how long it takes to run.

The cron fire path runs through the REAL platform classes (PluginProcessScanner
plus the KB overlay merge, ActionExecutor, ActionFactory), whole-tree gate C5.1
is run on the real module with a retargeted control, and the
starting-action entry is submitted through the real
``EventOrchestrator._submit_starting_actions``.

Run:
    PYTHONPATH=<linked worktree> .venv/bin/python3 plugins/platform_health_plugin/tests/model_capability_guard_smoke.py
"""

from __future__ import annotations

import ast
import dataclasses
import logging
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import httpx

REPO_ROOT = Path(__file__).resolve().parents[3]
_TESTS = Path(__file__).resolve().parent
for _entry in (
    REPO_ROOT,
    REPO_ROOT / "ananta" / "src",
    REPO_ROOT / "plugins" / "platform_health_plugin" / "src",
    REPO_ROOT / "plugins" / "default_scheduling_plugin" / "src",
    REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src",
    REPO_ROOT / "plugins" / "agent_messaging_plugin" / "tests",
    _TESTS,
):
    if str(_entry) not in sys.path:
        sys.path.insert(0, str(_entry))

import guard_test_support as fixtures  # noqa: E402 -- shared real-platform fixtures
import model_capability_refresh_smoke as refresh_fixtures  # noqa: E402  # pyright: ignore[reportMissingImports] -- dynamic sibling fixture path
from _real_state_fake import RealShapeState  # noqa: E402  # pyright: ignore[reportMissingImports] -- dynamic sibling fixture path
from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.core.config.environment_config import EnvironmentConfig  # noqa: E402
from ananta.core.domain.enums import ProcessorPolicyCategory  # noqa: E402
from ananta.core.event_orchestrator import EventOrchestrator  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from catalog_guard_test_support import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    _BASE,
    _RESULT_KEYS,
    _CatalogMessagingPlugin,
    _Clock,
    _detail_outage_handler,
    _failing_handler,
    _failure_envelope,
    _guard_logs,
    _healthy_handler,
    _LogCapture,
    _no_renewal_handler,
    _seed_cell,
    _status_marker,
    _success,
)
from default_scheduling_plugin.factories.schedule_factory import ScheduleFactory  # noqa: E402
from default_scheduling_plugin.validation import validate_cron_action_def  # noqa: E402
from platform_health_plugin import model_capability_guard as guard  # noqa: E402
from platform_health_plugin.constants import PLUGIN_NAME, SWEEP_PROCESS_NAME  # noqa: E402
from platform_health_plugin.guard_support import STATE_NAMESPACE  # noqa: E402
from platform_health_plugin.model_capability_guard import (  # noqa: E402
    CHECK_CRON_PROCESS_KEY,
    CHECK_PROCESS_KEY,
    ModelCapabilityGuardConfig,
    RefreshOutcome,
    assess,
    ensure_model_capability_guard_schedule,
    read_accepted_catalog,
    run_model_capability_guard,
)
from platform_health_plugin.plugin import PlatformHealthPlugin  # noqa: E402

from agent_messaging_plugin import model_capability_verbs as verbs  # noqa: E402
from agent_messaging_plugin import schema as owner_schema  # noqa: E402
from agent_messaging_plugin.model_capability_fetch import SOURCE_LEADERBOARD_HTML  # noqa: E402
from agent_messaging_plugin.model_capability_store import CATALOG_PAGE_SIZE, CATALOG_READ_CEILING  # noqa: E402
from quality_gates import whole_tree_integration_gate as whole_tree_gate  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


# ─── Fixtures ────────────────────────────────────────────────────────────────


class _Orchestrator:
    def __init__(self, messaging: object | None, state: RealShapeState) -> None:
        plugins: dict[str, object] = {} if messaging is None else {"agent_messaging_plugin": messaging}
        self.plugin_manager = fixtures._FixturePluginManager(plugins)  # noqa: SLF001
        self._state = state

    def get_service(self, name: str) -> object | None:
        return self._state if name == "state_service" else None


def _world(*, handler: Any = _healthy_handler) -> tuple[_Orchestrator, _CatalogMessagingPlugin, RealShapeState]:
    state = RealShapeState()
    messaging = _CatalogMessagingPlugin(state, handler=handler)
    return _Orchestrator(messaging, state), messaging, state


def _run(orch: _Orchestrator, clock: _Clock, *, state: dict[str, Any] | None = None) -> dict[str, Any]:
    return run_model_capability_guard(orch, config=ModelCapabilityGuardConfig(), state=state if state is not None else {}, clock=clock)


def _seeded_world(*, old_age: timedelta, window_hours: int = 72) -> tuple[_Orchestrator, _CatalogMessagingPlugin, _Clock]:
    """Two accepted cells: ``m-old`` at ``old_age`` and ``m-fresh`` renewed to
    the clock's now by every canned refresh, so the OLDEST cell alone drives
    the age condition while the run itself renews something."""
    orch, messaging, state = _world()
    _seed_cell(state, "m-old", age=old_age, window_hours=window_hours)
    _seed_cell(state, "m-fresh", age=timedelta(hours=1), window_hours=window_hours)
    clock = _Clock()
    messaging.touch_models = ["m-fresh"]
    messaging.touch_clock = clock
    return orch, messaging, clock


# ─── The cron-fired verb runs the refresh ───────────────────────────────────


def test_guard_invokes_the_refresh_with_the_cron_trigger() -> None:
    orch, messaging, _ = _world()
    fire_state = {"flow_id": "flow-model-capability-guard"}
    result = _run(orch, _Clock(real=True), state=fire_state)
    _check(len(messaging.refresh_calls) == 1, "one guard run invokes the catalog refresh exactly once")
    _check(messaging.refresh_calls == [{"trigger": "cron"}], "the refresh is invoked with trigger='cron'")
    _check(messaging.refresh_states == [fire_state], "the refresh receives the calling verb's own state unchanged")
    _check("cron" in verbs._VALID_TRIGGERS, "the trigger the guard sends is one the refresh accepts")  # noqa: SLF001
    _check(result["status"] == "ok" and result["renewed"] is True, "a healthy first run is ok and renewed a cell")
    _check(result["cells_accepted"] == 3 and result["accepted_cell_count"] == 3, "the result carries the real run counts (one cell on two sources, two on the leaderboard alone)")
    _check(result["transitioned"] is False and messaging.sends == [], "an ok first tick is not a transition (marker baseline is ok)")


def _expect_fields(result: dict[str, Any], label: str, **expected: object) -> None:
    actual = {key: result.get(key) for key in expected}
    _check(actual == expected, f"{label}: {actual!r}")


def _expect_text(text: str, label: str, *fragments: str) -> None:
    missing = [fragment for fragment in fragments if fragment not in text]
    _check(not missing, f"{label} (missing {missing!r})")


# ─── A severity change is recorded and logged ───────────────────────────────


def test_a_severity_change_is_recorded_once() -> None:
    orch, messaging, clock = _seeded_world(old_age=timedelta(hours=1))
    state = messaging.state
    markers: list[str | None] = []
    with _guard_logs() as capture:

        def run(label: str, expected_status: str, expected_transition: bool) -> None:
            before = len(capture.transitions())
            result = _run(orch, clock)
            _expect_fields(result, label, status=expected_status, transitioned=expected_transition)
            _check(set(result) == _RESULT_KEYS | {"advisories", "historical_unrenewed_cell_count"}, "the result carries the status fields and advisories")
            markers.append(_status_marker(state))
            _check(len(capture.transitions()) - before == int(expected_transition), f"{label}: transition log count")

        run("a healthy first run", "ok", False)
        messaging.canned_envelope = _failure_envelope("refresh_fetch_failed", "model capability refresh could not fetch: boom")
        run("a refresh failure", "critical", True)
        _expect_text(capture.transitions()[0][1], "failure envelope code reaches the transition log", "refresh_fetch_failed")
        run("the same failure again", "critical", False)
        messaging.canned_envelope = None
        clock.offset = timedelta(hours=48)  # m-old is now 49h old; the fresh renewal makes the current run healthy
        run("a successful partial renewal recovery", "ok", True)
        messaging.touch_models = ["m-old", "m-fresh"]
        clock.offset += timedelta(minutes=1)
        run("a healthy refresh", "ok", False)
        clock.offset += timedelta(minutes=1)  # the next daily refresh measures strictly later than the last
        run("a quiet ok", "ok", False)
    levels = [level for level, _ in capture.transitions()]
    _check(levels == [logging.ERROR, logging.INFO], "the transitions log at error (critical) and info (ok recovery), in order")
    _check(markers == [None, "critical", "critical", "ok", "ok", "ok"], "the marker follows every status after the first change")
    _check(len(messaging.sends) == 1, "the zero-reading failure sends one notice across the severity sequence")
    _check({field.name for field in dataclasses.fields(ModelCapabilityGuardConfig)} == {"warning_age"}, "the guard config holds only warning_age: it has no target to message")


# ─── (a) failures ────────────────────────────────────────────────────────────


def test_fetch_failure_is_critical_logged_once_and_a_repeat_logs_nothing() -> None:
    orch, messaging, state = _world()
    clock = _Clock(real=True)
    _run(orch, clock)  # healthy baseline: one accepted cell
    messaging.handler = _failing_handler
    day = clock().date()
    with _guard_logs() as capture, patch.object(EnvironmentConfig, "solet_name", return_value="catalog-smoke"):
        first = _run(orch, clock)
        runs = state.query_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": owner_schema.TABLE_MODEL_CAPABILITY_REFRESH_RUN})["data"]["records"]
        failed_run_id = runs[-1]["id"]
        second = _run(orch, clock)
    _expect_fields(first, "a fetch failure changes status", status="critical", transitioned=True)
    _expect_fields(second, "another failure keeps status", status="critical", transitioned=False)
    _check(len(first["sources_failed"]) == 1, "only the leaderboard source failed")
    _check(_status_marker(state) == "critical", "the marker records critical")
    _check([level for level, _ in capture.transitions()] == [logging.ERROR], "the repeated failure logs one transition at ERROR")
    details = (f"{SOURCE_LEADERBOARD_HTML}: http_error", "readings_gathered=0", "cells_accepted=0", "Oldest accepted cell", "h old")
    _expect_text(capture.transitions()[0][1], "transition details", *details, "CRITICAL", "selector still works from the last accepted readings")
    _check([send["name"] for send in messaging.sends] == ["Catalog-Smoke-Main"], "one notice went to the solet-local Main role")
    _expect_text(messaging.sends[0]["content"], "notice details", *details, f"{day} UTC", f"refresh run {failed_run_id}")


def test_zero_row_refresh_preserves_cells_and_notifies_again_the_next_day() -> None:
    def empty_page(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<table><tr><th>Model</th></tr></table>")

    orch, messaging, state = _world(handler=empty_page)
    clock = _Clock()
    _seed_cell(state, "claude-sonnet-5", age=timedelta(hours=21))
    query = {"table": owner_schema.TABLE_MODEL_CAPABILITY_CELL, "filters": {"model": "claude-sonnet-5"}}
    prior = state.query_state(AGENT_ROLE_BINDING_NAMESPACE, query)["data"]["records"][0]
    first = _run(orch, clock)
    # Reconstruct the guard and sender against the same durable state.
    replacement = _CatalogMessagingPlugin(state, handler=empty_page)
    orch = _Orchestrator(replacement, state)
    repeat = _run(orch, clock)
    _check(len(messaging.sends) == 1 and replacement.sends == [], "the same UTC day is suppressed across guard instances")
    clock.offset += timedelta(days=1)
    following = _run(orch, clock)
    _check(len(replacement.sends) == 1, "the following day sends one notice")
    _expect_text(replacement.sends[0]["content"], "next day's notice is dated", f"{clock().date()} UTC")
    _expect_text(messaging.sends[0]["content"], "first notice explains retained cells", "thin_page: 0 rows parsed", "21.0h old")
    _check([result["transitioned"] for result in (first, repeat, following)] == [True, False, False], "new days can notify at the same severity")
    for result in (first, repeat, following):
        _expect_fields(result, "the empty leaderboard renews nothing", readings_gathered=0, cells_accepted=0, renewed=False)
        _check(result["sources_failed"][0].startswith(f"{SOURCE_LEADERBOARD_HTML}: thin_page: 0 rows parsed"), "the guard returns the typed zero-row cause")
    row = state.query_state(AGENT_ROLE_BINDING_NAMESPACE, query)["data"]["records"][0]
    _check(all(row[key] == prior[key] for key in ("capability_score", "cost_per_task_usd", "measured_at")), "the accepted cell retains its score, cost and timestamp through repeated empty refreshes")


def test_notice_send_failure_is_visible_and_can_recover() -> None:
    orch, messaging, _ = _world(handler=_failing_handler)
    clock = _Clock()
    refusal = _failure_envelope("peer_role_vacant", "no local Main binding")
    with _guard_logs() as capture, patch.object(messaging, "peer_send_by_name", autospec=True, return_value=refusal):
        failure = _run(orch, clock)
    _expect_fields(failure, "a completed assessment reports the underlying failure", status="critical")
    _expect_text(failure["advisories"][0], "result names the send refusal", "peer_role_vacant")
    _check(any(level == logging.WARNING and "peer_role_vacant" in message for level, message in capture.records), "the send failure is also visible in the log")
    _expect_fields(_run(orch, clock), "retry stays at the same severity", transitioned=False, advisories=[])
    _check(len(messaging.sends) == 1, "the next check can notify after a failed send")


def test_many_failed_sources_are_capped_in_the_transition_text() -> None:
    orch, messaging, _ = _world(handler=_detail_outage_handler)
    with _guard_logs() as capture:
        first = _run(orch, _Clock(real=True))
    _check(first["status"] == "critical" and len(first["sources_failed"]) == 4, "a detail outage fails one source per discovered cell")
    text = capture.transitions()[0][1]
    _check("artificial_analysis_model_detail_html" in text and "(+1 more)" in text, "the line names failed sources, capped with a remainder count")
    _check(first["renewed"] and first["cells_accepted"] > 0 and messaging.sends == [], "a detail outage leaves leaderboard renewals working and sends no daily notice")


def test_a_refresh_that_raises_is_critical_logged_and_not_swallowed() -> None:
    orch, messaging, state = _world()
    clock = _Clock(real=True)
    _run(orch, clock)
    messaging.raise_on_refresh = RuntimeError("scheduler wedged")
    with _guard_logs() as capture:
        result = _run(orch, clock)
    _check(result["status"] == "critical" and result["transitioned"] is True, "a raising refresh is critical and a transition")
    _check(result["refresh_failure"] is not None and "scheduler wedged" in result["refresh_failure"], "the result names the raised failure")
    _check(_status_marker(state) == "critical", "the marker records critical")
    _check(any("scheduler wedged" in message for _, message in capture.transitions()), "the transition line carries the failure")
    _check(any(level == logging.ERROR and "raised" in message for level, message in capture.records), "the raise is logged at ERROR")
    _check(len(messaging.sends) == 1, "a raised refresh sends one failure notice")


def test_an_unreadable_refresh_result_is_critical_not_ok() -> None:
    for label, envelope in (
        ("no counts", _success({"sources_failed": []})),
        ("non-list sources_failed", _success({"readings_gathered": 1, "cells_accepted": 1, "cells_conflicted": 0, "cells_unchanged": 0, "sources_failed": "none"})),
        ("not a dict", cast("Any", ["completed"])),
    ):
        outcome = guard.parse_refresh_result(envelope)
        _check(outcome.failure is not None, f"unreadable refresh result ({label}) is a failure, not zero-counts success")


def test_messaging_plugin_unavailable_raises() -> None:
    state = RealShapeState()
    orch = _Orchestrator(None, state)
    try:
        _run(orch, _Clock())
    except RuntimeError as exc:
        _check("agent_messaging_plugin" in str(exc), "an unresolvable messaging plugin raises instead of reporting ok")
    else:
        _check(False, "an unresolvable messaging plugin raises instead of reporting ok")


# ─── (b) gathered readings, renewed nothing ─────────────────────────────────


def test_readings_gathered_but_nothing_renewed_warns_once() -> None:
    orch, messaging, state = _world()
    clock = _Clock(real=True)
    _run(orch, clock)  # baseline: one accepted cell
    messaging.handler = _no_renewal_handler
    with _guard_logs() as capture:
        first = _run(orch, clock)
        second = _run(orch, clock)
    _check(first["readings_gathered"] > 0, "precondition: readings were gathered")
    _check(first["cells_unchanged"] > 0, "precondition: cells_unchanged is non-zero on a run that renewed nothing (so 'accepted + unchanged == 0' cannot detect it)")
    _expect_fields(first, "a run that renewed nothing warns", cells_accepted=0, sources_failed=[], status="warning", renewed=False, transitioned=True)
    _expect_fields(second, "the same warning keeps its status", status="warning", transitioned=False)
    _check([level for level, _ in capture.transitions()] == [logging.WARNING], "one transition logs at WARNING")
    _expect_text(capture.transitions()[0][1], "warning names the cause", "WARNING", "renewed no accepted cell")
    _check(_status_marker(state) == "warning", "the marker records warning")
    _check(not messaging.sends, "nonzero readings with no failed source send no notice")


# ─── (c) age against the window read from the catalog ───────────────────────


def test_historical_cell_age_is_advisory_during_successful_renewal() -> None:
    orch, messaging, clock = _seeded_world(old_age=timedelta(hours=49))
    messaging.state.set_key_value(STATE_NAMESPACE, "model_capability_guard:last_status", "critical")
    messaging.touch_models = ["m-fresh"]
    with _guard_logs() as capture:
        first = _run(orch, clock)
        _expect_fields(first, "a cell at 49h is advisory during renewal", status="ok", renewed=True, oldest_cell_age_hours=49.0, transitioned=True)
        clock.offset += timedelta(minutes=1)
        again = _run(orch, clock)
        _expect_fields(again, "a repeat keeps the same severity", transitioned=False)
        _check(len(capture.transitions()) == 1, "a healthy partial renewal is quiet")
        clock.offset = timedelta(hours=24, minutes=1)  # m-old is now 73h old; the canned refresh renews m-fresh at the new now
        critical = _run(orch, clock)
        _expect_fields(critical, "a cell at 73h remains advisory during renewal", status="ok", transitioned=False)
        _check(len(capture.transitions()) == 1, "historical age does not add a transition while renewal succeeds")
        messaging.touch_models = ["m-old", "m-fresh"]
        recovered = _run(orch, clock)
        _check(recovered["status"] == "ok" and recovered["transitioned"] is False and len(capture.transitions()) == 1, "a later full refresh remains quiet")
        clock.offset += timedelta(minutes=1)  # the next daily refresh measures strictly later than the last
        quiet = _run(orch, clock)
        _check(quiet["status"] == "ok" and quiet["transitioned"] is False and len(capture.transitions()) == 1, "ok after renewal is quiet again")
    _check(messaging.sends == [], "nothing was sent")


def test_successful_partial_renewal_ignores_historical_unrenewable_age() -> None:
    orch, messaging, clock = _seeded_world(old_age=timedelta(hours=73))
    state = messaging.state
    state.set_key_value(STATE_NAMESPACE, "model_capability_guard:last_status", "critical")
    query = {"table": owner_schema.TABLE_MODEL_CAPABILITY_CELL, "filters": {"model": "m-old"}}
    prior = state.query_state(AGENT_ROLE_BINDING_NAMESPACE, query)["data"]["records"][0]
    with _guard_logs() as capture:
        result = _run(orch, clock)
    current = state.query_state(AGENT_ROLE_BINDING_NAMESPACE, query)["data"]["records"][0]
    _check(
        result.get("status") == "ok" and result.get("renewed") is True,
        "a successful partial renewal is ok despite a historical cell past the window",
    )
    _check(
        result.get("historical_unrenewed_cell_count") == 1,
        "the result counts the historical cell that was not renewed",
    )
    reasons = result.get("reasons", [])
    _check(
        any("1 historical accepted cell" in str(reason) and "stale" in str(reason) for reason in reasons),
        "the result keeps a stale historical-cell advisory",
    )
    _check(current["measured_at"] == prior["measured_at"], "the historical cell measured_at is unchanged")
    _check(any("1 historical accepted cell" in message for _, message in capture.transitions()), "the recovery log names the stale historical-cell count")


def test_all_historical_cells_old_without_renewal_stays_critical() -> None:
    orch, messaging, clock = _seeded_world(old_age=timedelta(hours=73))
    messaging.touch_models = []
    result = _run(orch, clock)
    _check(result["status"] == "critical" and result["renewed"] is False, "all old cells without renewal remain critical")
    _check(result["historical_unrenewed_cell_count"] == 2, "all historical cells are counted when none renews")
    _expect_text(result["reasons"][0], "the no-renewal critical reason keeps the age failure", "past the 72.0h staleness window")


def test_the_window_is_read_from_the_catalog_not_hard_coded() -> None:
    orch, messaging, clock = _seeded_world(old_age=timedelta(hours=100), window_hours=120)
    messaging.canned_envelope = _success({"readings_gathered": 2, "cells_accepted": 0, "cells_conflicted": 0, "cells_unchanged": 2, "sources_failed": []})
    wide = _run(orch, clock)
    _check(wide["status"] == "warning" and wide["staleness_window_hours"] == 120.0, "100h old inside a 120h window is warning, not critical")
    orch2, messaging2, clock2 = _seeded_world(old_age=timedelta(hours=25), window_hours=24)
    messaging2.canned_envelope = _success({"readings_gathered": 2, "cells_accepted": 0, "cells_conflicted": 0, "cells_unchanged": 2, "sources_failed": []})
    narrow = _run(orch2, clock2)
    _check(narrow["status"] == "critical" and narrow["staleness_window_hours"] == 24.0, "25h old past a 24h window is critical, not warning")
    _check(wide["transitioned"] is True and narrow["transitioned"] is True and messaging.sends == [] and messaging2.sends == [], "each scenario recorded one change and sent nothing")


def test_assess_boundaries_are_strict() -> None:
    def at(age: timedelta, window: timedelta = timedelta(hours=72)) -> str:
        critical, warning = guard._age_finding(age, window, timedelta(hours=48))  # noqa: SLF001 -- boundary test for the named classifier
        return "critical" if critical else "warning" if warning else "ok"

    _check(at(timedelta(hours=48)) == "ok", "exactly 48h is ok (warning is a strict >)")
    _check(at(timedelta(hours=48, seconds=1)) == "warning", "48h + 1s is warning")
    _check(at(timedelta(hours=72)) == "warning", "exactly the window is warning (critical is a strict >)")
    _check(at(timedelta(hours=72, seconds=1)) == "critical", "window + 1s is critical")
    _check(at(timedelta(hours=30), window=timedelta(hours=24)) == "critical", "a window tighter than 48h makes its own edge the warning threshold")
    critical, _ = guard._age_finding(None, None, timedelta(hours=48))  # noqa: SLF001 -- boundary test for the named classifier
    _check(critical == "no accepted cell in the catalog", "no accepted cell at all is critical")


def test_renewal_is_a_newer_measurement_not_a_count() -> None:
    cell = ("claude_code", "m", "high")
    stamp = _BASE - timedelta(hours=2)
    before = guard.CatalogReading({cell: stamp}, timedelta(hours=72))
    claims_accepted = RefreshOutcome(failure=None, readings_gathered=2, cells_accepted=1)
    same = assess(claims_accepted, before, guard.CatalogReading({cell: stamp}, timedelta(hours=72)), now=_BASE, warning_age=timedelta(hours=48))
    _check(same.renewed is False and same.status == "warning" and same.historical_unrenewed_cells == 1, "a run claiming cells_accepted>0 whose measured_at did not advance is not renewal")
    newer = assess(claims_accepted, before, guard.CatalogReading({cell: stamp + timedelta(hours=1)}, timedelta(hours=72)), now=_BASE, warning_age=timedelta(hours=48))
    _check(newer.renewed is True and newer.status == "ok", "a newer measured_at after the run is renewal")


def test_state_service_failure_propagates() -> None:
    orch, _, state = _world()
    state.fail_next("query")
    try:
        _run(orch, _Clock())
    except Exception as exc:  # noqa: BLE001 -- asserting the exact loud failure below
        _check("query" in str(exc).lower() or "failed" in str(exc).lower(), "a state-service failure while reading the catalog raises")
    else:
        _check(False, "a state-service failure while reading the catalog raises")


def test_catalog_read_pages_past_the_old_wall_and_truncates_at_the_ceiling() -> None:
    orch, _, state = _world()
    for index in range(guard.CATALOG_PAGE_SIZE + 30):
        _seed_cell(state, f"m-{index:04d}", age=timedelta(hours=1))
    full = read_accepted_catalog(orch)
    _check(len(full.measured_at) == guard.CATALOG_PAGE_SIZE + 30 and full.truncated_at is None, "a catalog past the old 100-row wall is read in full")
    orch_big, _, state_big = _world()
    for index in range(guard.CATALOG_READ_CEILING + 5):
        _seed_cell(state_big, f"m-{index:04d}", age=timedelta(hours=1))
    capped = read_accepted_catalog(orch_big)
    _check(len(capped.measured_at) == guard.CATALOG_READ_CEILING, "a catalog past the read ceiling keeps exactly the ceiling's rows")
    _check(capped.truncated_at == ("claude_code", "m-1000", "high"), "the truncation names the first cell not considered")
    outcome = RefreshOutcome(failure=None, readings_gathered=2, cells_accepted=1)
    verdict = assess(outcome, capped, capped, now=_BASE, warning_age=timedelta(hours=48))
    _check(verdict.status == "warning" and any("truncated" in reason and "m-1000" in reason for reason in verdict.reasons), "the assessment warns loudly and names the truncation, without raising")


def test_catalog_read_refuses_malformed_rows() -> None:
    orch2, _, state2 = _world()
    _seed_cell(state2, "m-bad", age=timedelta(hours=1))
    state2.update_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": guard.TABLE_MODEL_CAPABILITY_CELL, "filters": {"model": "m-bad"}}, {"measured_at": "2026-10-09T10:00:00"})
    try:
        read_accepted_catalog(orch2)
    except ValueError as exc:
        _check("timezone" in str(exc), "a measured_at without a timezone raises")
    else:
        _check(False, "a measured_at without a timezone raises")


def test_null_windows_are_unknown_and_malformed_values_still_raise() -> None:
    orch, _, state = _world()
    _seed_cell(state, "m-unknown", age=timedelta(hours=1))
    row = state.rows(guard.AGENT_ROLE_BINDING_NAMESPACE, guard.TABLE_MODEL_CAPABILITY_CELL)[0]
    row["staleness_window_hours"] = None
    unknown = read_accepted_catalog(orch)
    _check(len(unknown.measured_at) == 1 and unknown.window is None, "all-null windows retain measurements and give no cadence")
    finding = guard._age_finding(timedelta(hours=1), unknown.window, timedelta(hours=48))
    _check(finding[0] is None and "unknown" in str(finding[1]), "unknown cadence is advisory, never a false no-accepted-cell critical")
    _seed_cell(state, "m-known", age=timedelta(hours=2), window_hours=24)
    _check(read_accepted_catalog(orch).window == timedelta(hours=24), "a null window is skipped beside a known window")
    for value in (True, "24", 0, -1, 0.5):
        row["staleness_window_hours"] = value
        try:
            read_accepted_catalog(orch)
        except ValueError:
            _check(True, f"malformed non-null window {value!r} still raises")
        else:
            _check(False, f"malformed non-null window {value!r} still raises")


def test_constants_match_the_catalog_owner() -> None:
    _check(guard.TABLE_MODEL_CAPABILITY_CELL == owner_schema.TABLE_MODEL_CAPABILITY_CELL, "cell table name matches agent_messaging_plugin.schema")
    _check(guard.CELL_ACCEPTANCE_ACCEPTED == owner_schema.CELL_ACCEPTANCE_ACCEPTED, "accepted marker matches agent_messaging_plugin.schema")
    _check(guard.CATALOG_PAGE_SIZE == CATALOG_PAGE_SIZE and guard.CATALOG_READ_CEILING == CATALOG_READ_CEILING, "page size and read ceiling match the owner's constants")
    _check(guard.SOURCE_LEADERBOARD_HTML == SOURCE_LEADERBOARD_HTML, "the failure source matches the leaderboard owner's identifier")
    _check(STATE_NAMESPACE == "platform_health_plugin", "the marker lives in this plugin's own key-value namespace")


# ─── ensure_model_capability_guard_schedule ──────────────────────────────────


def test_schedule_installer_is_idempotent_and_tagged() -> None:
    scheduling = fixtures._FixtureSchedulingService()  # noqa: SLF001
    first = ensure_model_capability_guard_schedule(scheduling)
    second = ensure_model_capability_guard_schedule(scheduling)
    _check(first["status"] == "created" and first["tag"] == "model_capability_guard", "the first run creates the cron under its tag")
    _check(second["status"] == "already_present", "the second run finds it")
    _check(len(scheduling.create_calls) == 1, "a second run adds nothing")
    call = scheduling.create_calls[0]
    _check(call["tags"] == ["model_capability_guard"] and call["cron_expression"] == "30 5 * * *", "tagged, and daily")
    _check(call["actions"] == [{"process_key": CHECK_CRON_PROCESS_KEY, "arguments": {}}], "the literal action targets the EDGE_SINK sibling (pinned to the constant)")
    _check(call["state"] == {"flow_id": "flow-model-capability-guard", "session_id": "sess-model-capability-guard"}, "the schedule carries its own system-owned flow/session ids")


def test_schedule_installer_needs_no_configuration() -> None:
    scheduling = fixtures._FixtureSchedulingService()  # noqa: SLF001
    plugin, orch = _plugin(_CatalogMessagingPlugin(RealShapeState()))
    fixtures._run_init_service_manager(orch, scheduling)  # noqa: SLF001
    result = plugin.ensure_model_capability_guard_schedule({}, {})
    _check(result["data"]["status"] == "created" and len(scheduling.create_calls) == 1, "with an empty plugin config the cron is still installed")


def test_schedule_installer_lookup_failure_raises() -> None:
    scheduling = fixtures._FixtureSchedulingService(fail_lookup=True)  # noqa: SLF001
    try:
        ensure_model_capability_guard_schedule(scheduling)
    except RuntimeError as exc:
        _check("get_schedules_by_tag" in str(exc), "a scheduler error raises instead of reading as 'no existing schedule'")
    else:
        _check(False, "a scheduler error raises instead of reading as 'no existing schedule'")
    _check(scheduling.create_calls == [], "nothing was created after the failed lookup")


# ─── The cron's fire path, through the REAL platform classes ────────────────


def _installed_cron() -> tuple[list[Any], dict[str, Any]]:
    scheduling = fixtures._FixtureSchedulingService()  # noqa: SLF001
    ensure_model_capability_guard_schedule(scheduling)
    call = scheduling.create_calls[0]
    actions, _, _ = ScheduleFactory.parse_actions_from_params({"actions": call["actions"]})
    return actions, call["state"]


def test_registry_declares_the_three_verbs_with_the_right_shape() -> None:
    processes = cast("dict[str, Any]", fixtures._real_registry()["processes"])  # noqa: SLF001
    plugin_processes = {key for key in processes if key.startswith("plugin::platform_health_plugin::")}
    expected_processes = {
        f"plugin::{PLUGIN_NAME}::{SWEEP_PROCESS_NAME}",
        "plugin::platform_health_plugin::check_model_capability_catalog",
        "plugin::platform_health_plugin::check_model_capability_catalog_cron",
        "plugin::platform_health_plugin::ensure_model_capability_guard_schedule",
    }
    _check(plugin_processes == expected_processes, f"registry exposes exactly the retained platform-health verbs: {plugin_processes!r}")
    cron = processes[CHECK_CRON_PROCESS_KEY]
    _check(cron["processor_policy_category"] == ProcessorPolicyCategory.EDGE_SINK and cron["is_discoverable"] is False, "cron target: EDGE_SINK, not discoverable")
    _check("error_processor_customizations" not in cron and "result_processor_customizations" not in cron, "cron target declares no result or error processor")
    direct = processes[CHECK_PROCESS_KEY]
    _check(direct["processor_policy_category"] == ProcessorPolicyCategory.EDGE and direct["is_discoverable"] is False, "direct verb: EDGE, not discoverable (it reaches the network)")
    ensure = processes["plugin::platform_health_plugin::ensure_model_capability_guard_schedule"]
    _check(ensure["processor_policy_category"] == ProcessorPolicyCategory.EDGE and ensure["is_discoverable"] is False, "installer: EDGE, not discoverable")


def test_cron_entry_is_terminal_and_the_fire_stamps_the_flow_id() -> None:
    actions, schedule_state = _installed_cron()
    _check(len(actions) == 1 and actions[0].result_processor_kind is None and actions[0].result_processor is None, "the scheduling plugin's parser keeps the cron entry terminal")
    try:
        validate_cron_action_def(actions[0])
        _check(True, "the scheduling plugin's cron-action validator accepts the entry")
    except ValueError as exc:
        _check(False, f"the scheduling plugin's cron-action validator accepts the entry ({exc})")
    built, recorder, outcome, template_engine = fixtures._fire(fixtures._real_registry(), actions[0], schedule_state)  # noqa: SLF001
    _check(built.get("flow_id") == "flow-model-capability-guard", "the fire stamps the schedule's flow_id")
    _check(outcome == (True, None) and len(recorder.stored) == 1, "the real ActionFactory accepts the fired definition")
    stored = recorder.stored[0] if recorder.stored else {}
    _check(stored.get("process_key") == CHECK_CRON_PROCESS_KEY, "the stored runtime action targets the cron sibling")
    _check("error_processor" not in stored and "result_processor" not in stored, "no processor attached: the fire stays terminal")
    _check(template_engine.calls == 0, "no template resolution ran")
    _, recorder2, outcome2, _ = fixtures._fire(fixtures._real_registry(), actions[0], {})  # noqa: SLF001
    _check(outcome2[0] is False and not recorder2.stored, "a flow_id-less fire is refused by the real factory")


def test_whole_tree_gate_c51_sees_and_exempts_the_literal() -> None:
    plugins = {surface.name: surface for surface in whole_tree_gate._discover_plugin_surfaces()}  # noqa: SLF001
    source_path = REPO_ROOT / "plugins" / "platform_health_plugin" / "src" / "platform_health_plugin" / "model_capability_guard.py"
    source = source_path.read_text(encoding="utf-8")
    findings = whole_tree_gate._check_scheduling_in_module(ast.parse(source), source_path, {}, plugins)  # noqa: SLF001
    _check(findings == [], "C5.1: no finding on model_capability_guard.py (EDGE_SINK target exempted)")
    control_source = source.replace(f'"{CHECK_CRON_PROCESS_KEY}"', f'"{CHECK_PROCESS_KEY}"')
    _check(control_source != source, "control: the cron literal is present and was retargeted")
    control = whole_tree_gate._check_scheduling_in_module(ast.parse(control_source), source_path, {}, plugins)  # noqa: SLF001
    _check(len(control) == 1 and control[0].check_id == "C5.1", "control: the same literal aimed at the EDGE verb is flagged by C5.1")


# The entry shape profile/config/starting_action_definitions.json carries for
# this verb (name, process_key, description; no arguments, no
# result_processor_kind). Held as a literal here because a retained shipped
# smoke may not read repo-local profile files (smoke_contract_lint_gate).
_STARTING_ACTION: dict[str, str] = {
    "name": "ensure_model_capability_guard_schedule",
    "process_key": "plugin::platform_health_plugin::ensure_model_capability_guard_schedule",
    "description": "Install the daily model capability catalog guard cron.",
}


def test_the_starting_action_installs_the_cron_after_startup() -> None:
    entry = _STARTING_ACTION
    _check(entry["process_key"] == "plugin::platform_health_plugin::ensure_model_capability_guard_schedule", "the entry's process_key is the installer verb")
    _check("result_processor_kind" not in entry, "it declares no result_processor_kind: a successful install is terminal")
    scheduling = fixtures._FixtureSchedulingService()  # noqa: SLF001
    recorder = fixtures._RecordingActionEventRecorder()  # noqa: SLF001
    plugin, orch = _plugin(_CatalogMessagingPlugin(RealShapeState()))
    orch.action_factory = ActionFactory(  # type: ignore[assignment]
        template_engine=fixtures._RecordingTemplateEngine(),  # type: ignore[arg-type]  # noqa: SLF001
        state_service=fixtures._SuffixStateService(),  # type: ignore[arg-type]  # noqa: SLF001
        action_event_recorder=recorder,
    )
    orch._process_registry = fixtures._real_registry()  # noqa: SLF001
    orch.current_flow_id = "flow-startup-smoke"
    orch.current_session_id = "sess-startup-smoke"
    orch._submit_starting_actions([dict(entry)], {})  # noqa: SLF001 -- the real starting-action path
    stored = recorder.stored[0] if recorder.stored else {}
    _check(stored.get("process_key") == entry["process_key"] and stored.get("flow_id") == "flow-startup-smoke", "the starting action is submitted on the startup flow")
    fixtures._run_init_service_manager(orch, scheduling)  # noqa: SLF001
    parameters = stored.get("parameters")
    result = plugin.ensure_model_capability_guard_schedule(parameters if isinstance(parameters, dict) else {}, {})
    _check(result["data"]["status"] == "created", "dispatching the stored action installs the cron")


# ─── The plugin's cron verb ──────────────────────────────────────────────────


def _plugin(messaging: _CatalogMessagingPlugin) -> tuple[PlatformHealthPlugin, EventOrchestrator]:
    orch = EventOrchestrator.__new__(EventOrchestrator)
    orch.plugin_manager = fixtures._FixturePluginManager({"agent_messaging_plugin": messaging})  # type: ignore[assignment]  # noqa: SLF001
    orch.state_service = messaging.state  # type: ignore[assignment]
    plugin = PlatformHealthPlugin()
    plugin.orchestrator_ref = orch
    plugin.prepare_for_readiness()
    plugin.start_services()
    return plugin, orch


def _wait_for_check(plugin: PlatformHealthPlugin) -> bool:
    deadline = time.monotonic() + 10
    while plugin._catalog_check_lock.locked() and time.monotonic() < deadline:  # noqa: SLF001
        time.sleep(0.02)
    return not plugin._catalog_check_lock.locked()  # noqa: SLF001


def test_cron_verb_runs_the_refresh_in_the_background_one_at_a_time() -> None:
    release = threading.Event()
    catalog_state = RealShapeState()
    messaging = _CatalogMessagingPlugin(catalog_state, handler=_failing_handler)
    real_refresh = messaging.refresh_model_capability_catalog

    def _blocking_refresh(params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        release.wait(10)
        return real_refresh(params, state)

    messaging.refresh_model_capability_catalog = _blocking_refresh  # type: ignore[method-assign]
    plugin, _ = _plugin(messaging)
    fire_state = {"flow_id": "flow-model-capability-guard", "session_id": "sess-model-capability-guard"}
    first = plugin.check_model_capability_catalog_cron({}, fire_state)
    second = plugin.check_model_capability_catalog_cron({}, fire_state)
    _check(first["data"]["dispatch"] == "started", "a fire returns 'started' at once, before the refresh has finished")
    _check(second["data"]["dispatch"] == "already_running", "an overlapping fire is a no-op, not a second refresh")
    _check(messaging.sends == [], "nothing was sent while the background refresh was still blocked")
    release.set()
    _check(_wait_for_check(plugin), "the background check finishes and releases its slot")
    _check(len(messaging.refresh_calls) == 1 and messaging.refresh_calls[0] == {"trigger": "cron"}, "the cron fire ran exactly one cron-triggered refresh")
    _check(messaging.refresh_states == [fire_state], "the background check ran with the fire's own state")
    _check(_status_marker(catalog_state) == "critical", "the background check recorded the failing catalog's status")
    _check(len(messaging.sends) == 1, "the failing cron fire sends one operator notice")
    plugin.set_active(False)
    _check(plugin.check_model_capability_catalog_cron({}, fire_state)["data"]["dispatch"] == "inactive", "the inactive colour's fires are no-ops")


def test_cron_background_failure_is_logged_at_error() -> None:
    state = RealShapeState()
    messaging = _CatalogMessagingPlugin(state)
    plugin, orch = _plugin(messaging)
    orch.plugin_manager = fixtures._FixturePluginManager({})  # type: ignore[assignment]  # noqa: SLF001 -- messaging plugin unresolvable
    capture = _LogCapture()
    plugin.logger.addHandler(capture)
    try:
        fire = plugin.check_model_capability_catalog_cron({}, {})
        finished = _wait_for_check(plugin)
    finally:
        plugin.logger.removeHandler(capture)
    _check(fire["data"]["dispatch"] == "started" and finished, "a failing check does not raise into the scheduler")
    _check(any("catalog check failed" in message for message in capture.messages), "the failure is logged at ERROR")


def test_direct_verb_returns_the_status_and_advisories() -> None:
    for failing in (False, True):
        state = RealShapeState()
        messaging = _CatalogMessagingPlugin(state)
        if failing:
            messaging.raise_on_refresh = RuntimeError("controlled failure")
        plugin, _ = _plugin(messaging)
        original = messaging.refresh_model_capability_catalog_sync
        job = refresh_fixtures.prove_queued_operation(plugin, "check_model_capability_catalog", {}, messaging, "refresh_model_capability_catalog_sync", original, None)
        data = job["result"]["data"]
        _check(data["status"] == ("critical" if failing else "ok"), "the queued guard preserves the final refresh decision")
        _check(data["transitioned"] is failing, "the result says whether the severity changed on this call")
        _check(set(data) == _RESULT_KEYS | {"advisories", "historical_unrenewed_cell_count"}, "the result carries the status fields and advisories")
        _check(failing or data["cells_accepted"] == 3, "the queued guard preserves final run counts")
        _check(messaging.refresh_states[0]["session_id"] == "smoke-session", "the worker receives the dispatch context")
        _check(len(messaging.sends) == int(failing), "the direct verb sends a notice only for the failing case")


def main() -> int:
    print("=== platform_health_plugin model_capability_guard_smoke ===")
    test_guard_invokes_the_refresh_with_the_cron_trigger()
    test_a_severity_change_is_recorded_once()
    test_fetch_failure_is_critical_logged_once_and_a_repeat_logs_nothing()
    test_zero_row_refresh_preserves_cells_and_notifies_again_the_next_day()
    test_notice_send_failure_is_visible_and_can_recover()
    test_many_failed_sources_are_capped_in_the_transition_text()
    test_a_refresh_that_raises_is_critical_logged_and_not_swallowed()
    test_an_unreadable_refresh_result_is_critical_not_ok()
    test_messaging_plugin_unavailable_raises()
    test_readings_gathered_but_nothing_renewed_warns_once()
    test_historical_cell_age_is_advisory_during_successful_renewal()
    test_successful_partial_renewal_ignores_historical_unrenewable_age()
    test_all_historical_cells_old_without_renewal_stays_critical()
    test_the_window_is_read_from_the_catalog_not_hard_coded()
    test_assess_boundaries_are_strict()
    test_renewal_is_a_newer_measurement_not_a_count()
    test_state_service_failure_propagates()
    test_catalog_read_pages_past_the_old_wall_and_truncates_at_the_ceiling()
    test_catalog_read_refuses_malformed_rows()
    test_null_windows_are_unknown_and_malformed_values_still_raise()
    test_constants_match_the_catalog_owner()
    test_schedule_installer_is_idempotent_and_tagged()
    test_schedule_installer_needs_no_configuration()
    test_schedule_installer_lookup_failure_raises()
    test_registry_declares_the_three_verbs_with_the_right_shape()
    test_cron_entry_is_terminal_and_the_fire_stamps_the_flow_id()
    test_whole_tree_gate_c51_sees_and_exempts_the_literal()
    test_the_starting_action_installs_the_cron_after_startup()
    test_cron_verb_runs_the_refresh_in_the_background_one_at_a_time()
    test_cron_background_failure_is_logged_at_error()
    test_direct_verb_returns_the_status_and_advisories()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
