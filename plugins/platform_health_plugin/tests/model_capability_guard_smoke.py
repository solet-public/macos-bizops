#!/usr/bin/env python3
"""RED/GREEN smoke for model_capability_guard.py (no network, no real clock,
no real peer message, no live catalog).

The refresh under test is the REAL ``refresh_model_capability_catalog`` verb
body running against the repo's real-shape in-memory state fake, with every
fetch answered by an ``httpx.MockTransport`` handler (real-shaped fixtures from
the agent_messaging refresh smoke) and the harness model lists read from that
smoke's hermetic fixture (a temporary HOME with a Claude cache and a stub Codex
binary), so no result depends on the host it runs on. Delivery is a recording double standing in
for ``peer_send_by_name``. Age scenarios use a fake clock and cells seeded
directly, so no test depends on how long it takes to run.

The cron fire path runs through the REAL platform classes (PluginProcessScanner
plus the KB overlay merge, ActionExecutor, ActionFactory), whole-tree gate C5.1
is run on the real module with a retargeted control, and the
starting-action entry is submitted through the real
``EventOrchestrator._submit_starting_actions``.

Run:
    PYTHONPATH=<lane worktree> .venv/bin/python3 plugins/platform_health_plugin/tests/model_capability_guard_smoke.py
"""

from __future__ import annotations

import ast
import logging
import sys
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any, cast

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

import host_disk_guard_smoke as host  # noqa: E402 -- shares the real-platform fixtures (registry, fire path, scheduling double)
import model_capability_refresh_smoke as refresh_fixtures  # noqa: E402  # pyright: ignore[reportMissingImports] -- dynamic sibling fixture path
import platform_health_plugin.plugin as plugin_module  # noqa: E402
from _real_state_fake import RealShapeState  # noqa: E402  # pyright: ignore[reportMissingImports] -- dynamic sibling fixture path
from ananta.core.domain.enums import ProcessorPolicyCategory  # noqa: E402
from ananta.core.event_orchestrator import EventOrchestrator  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from catalog_guard_test_support import (  # noqa: E402  # pyright: ignore[reportMissingImports]
    _BASE,
    ROLE,
    _CatalogMessagingPlugin,
    _Clock,
    _detail_outage_handler,
    _failing_handler,
    _failure_envelope,
    _healthy_handler,
    _no_renewal_handler,
    _success,
)
from default_scheduling_plugin.factories.schedule_factory import ScheduleFactory  # noqa: E402
from default_scheduling_plugin.validation import validate_cron_action_def  # noqa: E402
from platform_health_plugin import model_capability_guard as guard  # noqa: E402
from platform_health_plugin.host_disk_guard import STATE_NAMESPACE  # noqa: E402
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
        self.plugin_manager = host._FixturePluginManager(plugins)  # noqa: SLF001
        self._state = state

    def get_service(self, name: str) -> object | None:
        return self._state if name == "state_service" else None


def _world(*, handler: Any = _healthy_handler, delivery: str = "queued_wake") -> tuple[_Orchestrator, _CatalogMessagingPlugin, RealShapeState]:
    state = RealShapeState()
    messaging = _CatalogMessagingPlugin(state, handler=handler, delivery=delivery)
    return _Orchestrator(messaging, state), messaging, state


def _run(orch: _Orchestrator, clock: _Clock, *, role: str | None = ROLE, state: dict[str, Any] | None = None) -> dict[str, Any]:
    return run_model_capability_guard(orch, config=ModelCapabilityGuardConfig(alert_role=role), state=state if state is not None else {}, clock=clock)


def _seed_cell(state: RealShapeState, model: str, *, age: timedelta, window_hours: int = 72) -> None:
    measured = (_BASE - age).isoformat()
    record = {
        "provider": "anthropic",
        "runtime": "claude_code",
        "model": model,
        "effort": "high",
        "capability_score": 50.0,
        "cost_per_task_usd": 1.0,
        "relative_cost_multiplier": 1.0,
        "currency": "USD",
        "effort_supported": True,
        "measured_at": measured,
        "accepted_at": measured,
        "acceptance": owner_schema.CELL_ACCEPTANCE_ACCEPTED,
        "agreeing_observation_ids": [],
        "disagreement_note": None,
        "staleness_window_hours": window_hours,
        "last_refresh_run_id": "run-seed",
    }
    state.write_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": guard.TABLE_MODEL_CAPABILITY_CELL, "record": record})


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


class _LogCapture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.ERROR)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


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
    _check(result["alerted"] is False and messaging.sends == [], "an ok first tick is quiet (marker baseline is ok)")


# ─── (a) failures ────────────────────────────────────────────────────────────


def test_fetch_failure_raises_critical_once_and_a_repeat_sends_nothing() -> None:
    orch, messaging, _ = _world()
    clock = _Clock(real=True)
    _run(orch, clock)  # healthy baseline: one accepted cell
    messaging.handler = _failing_handler
    first = _run(orch, clock)
    _check(first["status"] == "critical" and first["transitioned"] is True and first["alerted"] is True, "a fetch failure is critical and alerts")
    _check(len(first["sources_failed"]) == 1, "the result lists the failed source (with no leaderboard no cell is discovered, so no per-cell fetch fails)")
    _check(len(messaging.sends) == 1 and messaging.sends[0]["name"] == ROLE, "exactly one alert, to the configured role")
    text = messaging.sends[0]["content"]
    _check("CRITICAL" in text and "readings_gathered=0" in text and "cells_accepted=0" in text, "the alert names the run counts")
    _check("artificial_analysis_leaderboard_html" in text, "the alert names the failed source")
    _check("Oldest accepted cell" in text and "h old" in text, "the alert names the oldest accepted cell's age")
    _check("selector still works from the last accepted readings" in text, "the alert says the selector keeps serving the last accepted readings")
    second = _run(orch, clock)
    _check(second["status"] == "critical" and second["transitioned"] is False and second["alerted"] is False, "a second identical failure is not a transition")
    _check(len(messaging.sends) == 1, "a second identical failure sends nothing")


def test_many_failed_sources_are_capped_in_the_alert() -> None:
    orch, messaging, _ = _world(handler=_detail_outage_handler)
    first = _run(orch, _Clock(real=True))
    _check(first["status"] == "critical" and len(first["sources_failed"]) == 4, "a detail outage fails one source per discovered cell")
    text = messaging.sends[0]["content"]
    _check("artificial_analysis_model_detail_html" in text and "(+1 more)" in text, "the alert names failed sources, capped with a remainder count")


def test_a_refresh_that_raises_is_critical_logged_and_not_swallowed() -> None:
    orch, messaging, _ = _world()
    clock = _Clock(real=True)
    _run(orch, clock)
    messaging.raise_on_refresh = RuntimeError("scheduler wedged")
    capture = _LogCapture()
    logger = logging.getLogger("platform_health_plugin.model_capability_guard")
    logger.addHandler(capture)
    try:
        result = _run(orch, clock)
    finally:
        logger.removeHandler(capture)
    _check(result["status"] == "critical" and result["alerted"] is True, "a raising refresh is critical and alerts")
    _check(result["refresh_failure"] is not None and "scheduler wedged" in result["refresh_failure"], "the result names the raised failure")
    _check("scheduler wedged" in messaging.sends[-1]["content"], "the alert text carries the failure")
    _check(any("raised" in message for message in capture.messages), "the raise is logged at ERROR")


def test_a_failure_envelope_is_critical() -> None:
    orch, messaging, _ = _world()
    clock = _Clock(real=True)
    _run(orch, clock)
    messaging.canned_envelope = _failure_envelope("refresh_fetch_failed", "model capability refresh could not fetch: boom")
    result = _run(orch, clock)
    _check(result["status"] == "critical" and "refresh_fetch_failed" in str(result["refresh_failure"]), "a failure envelope is critical and names its code")


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
    orch, messaging, _ = _world()
    clock = _Clock(real=True)
    _run(orch, clock)  # baseline: one accepted cell
    messaging.handler = _no_renewal_handler
    first = _run(orch, clock)
    _check(first["readings_gathered"] > 0 and first["cells_accepted"] == 0, "precondition: readings were gathered and no cell was accepted")
    _check(first["cells_unchanged"] > 0, "precondition: cells_unchanged is non-zero on a run that renewed nothing (so 'accepted + unchanged == 0' cannot detect it)")
    _check(first["sources_failed"] == [], "precondition: no source failed")
    _check(first["status"] == "warning" and first["renewed"] is False, "a run that gathered readings but renewed nothing is warning")
    _check(first["alerted"] is True and len(messaging.sends) == 1 and "WARNING" in messaging.sends[0]["content"], "warning alerts once")
    _check("renewed no accepted cell" in messaging.sends[0]["content"], "the alert says why")
    second = _run(orch, clock)
    _check(second["status"] == "warning" and second["alerted"] is False and len(messaging.sends) == 1, "an identical second run sends nothing")


# ─── (c) age against the window read from the catalog ───────────────────────


def test_cell_age_warns_at_49h_critical_at_73h_and_a_healthy_refresh_recovers_once() -> None:
    orch, messaging, clock = _seeded_world(old_age=timedelta(hours=49))
    first = _run(orch, clock)
    _check(first["status"] == "warning" and first["renewed"] is True, "a cell at 49h is warning though the run renewed something")
    _check(abs(first["oldest_cell_age_hours"] - 49.0) < 1e-9, "the result reports the oldest accepted cell's age")
    _check(first["alerted"] is True and len(messaging.sends) == 1, "warning alerts once")
    again = _run(orch, clock)
    _check(again["alerted"] is False and len(messaging.sends) == 1, "a second tick at the same severity sends nothing")
    clock.offset = timedelta(hours=24)  # m-old is now 73h old; the canned refresh renews m-fresh at the new now
    critical = _run(orch, clock)
    _check(critical["status"] == "critical" and critical["alerted"] is True and len(messaging.sends) == 2, "a cell at 73h is critical and escalates once")
    _check("past the 72.0h staleness window" in messaging.sends[1]["content"], "the critical alert names the window it was read from")
    messaging.touch_models = ["m-old", "m-fresh"]
    recovered = _run(orch, clock)
    _check(recovered["status"] == "ok" and recovered["alerted"] is True and len(messaging.sends) == 3, "a later healthy refresh sends one recovery")
    _check("OK (recovered)" in messaging.sends[2]["content"], "the recovery alert says so")
    clock.offset += timedelta(minutes=1)  # the next daily refresh measures strictly later than the last
    quiet = _run(orch, clock)
    _check(quiet["status"] == "ok" and quiet["alerted"] is False and len(messaging.sends) == 3, "ok after recovery is quiet again")


def test_the_window_is_read_from_the_catalog_not_hard_coded() -> None:
    orch, messaging, clock = _seeded_world(old_age=timedelta(hours=100), window_hours=120)
    wide = _run(orch, clock)
    _check(wide["status"] == "warning" and wide["staleness_window_hours"] == 120.0, "100h old inside a 120h window is warning, not critical")
    orch2, messaging2, clock2 = _seeded_world(old_age=timedelta(hours=25), window_hours=24)
    narrow = _run(orch2, clock2)
    _check(narrow["status"] == "critical" and narrow["staleness_window_hours"] == 24.0, "25h old past a 24h window is critical, not warning")
    _check(len(messaging.sends) == 1 and len(messaging2.sends) == 1, "each scenario alerted once")


def test_assess_boundaries_are_strict() -> None:
    renewing = RefreshOutcome(failure=None, readings_gathered=2, cells_accepted=1)
    old = guard.CatalogReading({("claude_code", "m", "high"): _BASE - timedelta(hours=200)}, timedelta(hours=72))

    def at(age: timedelta, window: timedelta = timedelta(hours=72)) -> str:
        cells = guard.CatalogReading({("claude_code", "m", "high"): _BASE - age}, window)
        return assess(renewing, old, cells, now=_BASE, warning_age=timedelta(hours=48)).status

    _check(at(timedelta(hours=48)) == "ok", "exactly 48h is ok (warning is a strict >)")
    _check(at(timedelta(hours=48, seconds=1)) == "warning", "48h + 1s is warning")
    _check(at(timedelta(hours=72)) == "warning", "exactly the window is warning (critical is a strict >)")
    _check(at(timedelta(hours=72, seconds=1)) == "critical", "window + 1s is critical")
    _check(at(timedelta(hours=30), window=timedelta(hours=24)) == "critical", "a window tighter than 48h makes its own edge the warning threshold")
    empty = guard.CatalogReading({}, None)
    _check(assess(renewing, empty, empty, now=_BASE, warning_age=timedelta(hours=48)).status == "critical", "no accepted cell at all is critical")


def test_renewal_is_a_newer_measurement_not_a_count() -> None:
    cell = ("claude_code", "m", "high")
    stamp = _BASE - timedelta(hours=2)
    before = guard.CatalogReading({cell: stamp}, timedelta(hours=72))
    claims_accepted = RefreshOutcome(failure=None, readings_gathered=2, cells_accepted=1)
    same = assess(claims_accepted, before, guard.CatalogReading({cell: stamp}, timedelta(hours=72)), now=_BASE, warning_age=timedelta(hours=48))
    _check(same.renewed is False and same.status == "warning", "a run claiming cells_accepted>0 whose measured_at did not advance is not renewal")
    newer = assess(claims_accepted, before, guard.CatalogReading({cell: stamp + timedelta(hours=1)}, timedelta(hours=72)), now=_BASE, warning_age=timedelta(hours=48))
    _check(newer.renewed is True and newer.status == "ok", "a newer measured_at after the run is renewal")


# ─── Delivery ────────────────────────────────────────────────────────────────


def test_no_alert_role_still_refreshes_and_reports_why() -> None:
    orch, messaging, _ = _world()
    clock = _Clock(real=True)
    _run(orch, clock, role=None)
    messaging.handler = _failing_handler
    capture = _LogCapture()
    logger = logging.getLogger("platform_health_plugin.model_capability_guard")
    logger.addHandler(capture)
    try:
        first = _run(orch, clock, role=None)
        second = _run(orch, clock, role=None)
    finally:
        logger.removeHandler(capture)
    _check(len(messaging.refresh_calls) == 3, "with no alert_role the refresh still runs on every tick")
    _check(first["status"] == "critical" and first["alerted"] is False and first["alert_reason"] == "alert_target_unconfigured", "no role -> alerted=False with alert_reason")
    _check(messaging.sends == [], "nothing was sent")
    _check(any("no alert_role configured" in message for message in capture.messages), "the unconfigured transition is logged at ERROR")
    _check(second["transitioned"] is True and second["alert_reason"] == "alert_target_unconfigured", "the marker did not advance, so the transition is retried (and re-logged) next tick")


def test_an_undelivered_alert_is_retried_next_tick() -> None:
    orch, messaging, _ = _world(delivery="queued_for_replay")
    clock = _Clock(real=True)
    _run(orch, clock)
    messaging.handler = _failing_handler
    first = _run(orch, clock)
    _check(first["alerted"] is False and "queued_for_replay" in str(first["alert_reason"]), "queued_for_replay is not a live delivery: alerted=False with the reason")
    messaging.delivery = "queued_wake"
    second = _run(orch, clock)
    _check(second["alerted"] is True and len(messaging.sends) == 2, "the next tick retries the undelivered transition and it lands")
    third = _run(orch, clock)
    _check(third["alerted"] is False and len(messaging.sends) == 2, "after a confirmed delivery the transition is not repeated")


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
    _check(STATE_NAMESPACE == "platform_health_plugin", "the marker lives in this plugin's own key-value namespace")


# ─── ensure_model_capability_guard_schedule ──────────────────────────────────


def test_schedule_installer_is_idempotent_and_tagged() -> None:
    scheduling = host._FixtureSchedulingService()  # noqa: SLF001
    first = ensure_model_capability_guard_schedule(scheduling)
    second = ensure_model_capability_guard_schedule(scheduling)
    _check(first["status"] == "created" and first["tag"] == "model_capability_guard", "the first run creates the cron under its tag")
    _check(second["status"] == "already_present", "the second run finds it")
    _check(len(scheduling.create_calls) == 1, "a second run adds nothing")
    call = scheduling.create_calls[0]
    _check(call["tags"] == ["model_capability_guard"] and call["cron_expression"] == "30 5 * * *", "tagged, and daily")
    _check(call["actions"] == [{"process_key": CHECK_CRON_PROCESS_KEY, "arguments": {}}], "the literal action targets the EDGE_SINK sibling (pinned to the constant)")
    _check(call["state"] == {"flow_id": "flow-model-capability-guard", "session_id": "sess-model-capability-guard"}, "the schedule carries its own system-owned flow/session ids")


def test_schedule_installer_does_not_need_an_alert_role() -> None:
    scheduling = host._FixtureSchedulingService()  # noqa: SLF001
    plugin_module.get_config = lambda: host._FakeConfigManager({})  # type: ignore[assignment]  # noqa: SLF001
    plugin, orch = _plugin({}, _CatalogMessagingPlugin(RealShapeState()))
    host._run_init_service_manager(orch, scheduling)  # noqa: SLF001
    result = plugin.ensure_model_capability_guard_schedule({}, {})
    _check(result["data"]["status"] == "created" and len(scheduling.create_calls) == 1, "with no alert_role the cron is still installed (the refresh is the point)")


def test_schedule_installer_lookup_failure_raises() -> None:
    scheduling = host._FixtureSchedulingService(fail_lookup=True)  # noqa: SLF001
    try:
        ensure_model_capability_guard_schedule(scheduling)
    except RuntimeError as exc:
        _check("get_schedules_by_tag" in str(exc), "a scheduler error raises instead of reading as 'no existing schedule'")
    else:
        _check(False, "a scheduler error raises instead of reading as 'no existing schedule'")
    _check(scheduling.create_calls == [], "nothing was created after the failed lookup")


# ─── The cron's fire path, through the REAL platform classes ────────────────


def _installed_cron() -> tuple[list[Any], dict[str, Any]]:
    scheduling = host._FixtureSchedulingService()  # noqa: SLF001
    ensure_model_capability_guard_schedule(scheduling)
    call = scheduling.create_calls[0]
    actions, _, _ = ScheduleFactory.parse_actions_from_params({"actions": call["actions"]})
    return actions, call["state"]


def test_registry_declares_the_three_verbs_with_the_right_shape() -> None:
    processes = cast("dict[str, Any]", host._real_registry()["processes"])  # noqa: SLF001
    cron = processes[CHECK_CRON_PROCESS_KEY]
    _check(cron["processor_policy_category"] == ProcessorPolicyCategory.EDGE_SINK and cron["is_discoverable"] is False, "cron target: EDGE_SINK, not discoverable")
    _check("error_processor_customizations" not in cron and "result_processor_customizations" not in cron, "cron target declares no result or error processor")
    direct = processes[CHECK_PROCESS_KEY]
    _check(direct["processor_policy_category"] == ProcessorPolicyCategory.EDGE and direct["is_discoverable"] is False, "direct verb: EDGE, not discoverable (it reaches the network)")
    ensure = processes["plugin::platform_health_plugin::ensure_model_capability_guard_schedule"]
    _check(ensure["processor_policy_category"] == ProcessorPolicyCategory.EDGE and ensure["is_discoverable"] is False, "installer: EDGE, not discoverable")
    heartbeat = processes["plugin::platform_health_plugin::check_main_seat_heartbeat_cron"]
    _check(heartbeat["processor_policy_category"] == ProcessorPolicyCategory.EDGE_SINK, "the seat heartbeat remains terminal beside the catalog cron")
    _check("result_processor_customizations" not in heartbeat, "seat heartbeat cannot start an inference continuation")


def test_cron_entry_is_terminal_and_the_fire_stamps_the_flow_id() -> None:
    actions, schedule_state = _installed_cron()
    _check(len(actions) == 1 and actions[0].result_processor_kind is None and actions[0].result_processor is None, "the scheduling plugin's parser keeps the cron entry terminal")
    try:
        validate_cron_action_def(actions[0])
        _check(True, "the scheduling plugin's cron-action validator accepts the entry")
    except ValueError as exc:
        _check(False, f"the scheduling plugin's cron-action validator accepts the entry ({exc})")
    built, recorder, outcome, template_engine = host._fire(host._real_registry(), actions[0], schedule_state)  # noqa: SLF001
    _check(built.get("flow_id") == "flow-model-capability-guard", "the fire stamps the schedule's flow_id")
    _check(outcome == (True, None) and len(recorder.stored) == 1, "the real ActionFactory accepts the fired definition")
    stored = recorder.stored[0] if recorder.stored else {}
    _check(stored.get("process_key") == CHECK_CRON_PROCESS_KEY, "the stored runtime action targets the cron sibling")
    _check("error_processor" not in stored and "result_processor" not in stored, "no processor attached: the fire stays terminal")
    _check(template_engine.calls == 0, "no template resolution ran")
    _, recorder2, outcome2, _ = host._fire(host._real_registry(), actions[0], {})  # noqa: SLF001
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
    scheduling = host._FixtureSchedulingService()  # noqa: SLF001
    recorder = host._RecordingActionEventRecorder()  # noqa: SLF001
    plugin, orch = _plugin({}, _CatalogMessagingPlugin(RealShapeState()))
    orch.action_factory = host.ActionFactory(  # type: ignore[assignment]
        template_engine=host._RecordingTemplateEngine(),  # type: ignore[arg-type]  # noqa: SLF001
        state_service=host._SuffixStateService(),  # noqa: SLF001
        action_event_recorder=recorder,
    )
    orch._process_registry = host._real_registry()  # noqa: SLF001
    orch.current_flow_id = "flow-startup-smoke"
    orch.current_session_id = "sess-startup-smoke"
    orch._submit_starting_actions([dict(entry)], {})  # noqa: SLF001 -- the real starting-action path
    stored = recorder.stored[0] if recorder.stored else {}
    _check(stored.get("process_key") == entry["process_key"] and stored.get("flow_id") == "flow-startup-smoke", "the starting action is submitted on the startup flow")
    host._run_init_service_manager(orch, scheduling)  # noqa: SLF001
    parameters = stored.get("parameters")
    result = plugin.ensure_model_capability_guard_schedule(parameters if isinstance(parameters, dict) else {}, {})
    _check(result["data"]["status"] == "created", "dispatching the stored action installs the cron")


# ─── The plugin's cron verb ──────────────────────────────────────────────────


def _plugin(config: dict[str, object], messaging: _CatalogMessagingPlugin) -> tuple[PlatformHealthPlugin, EventOrchestrator]:
    plugin_module.get_config = lambda: host._FakeConfigManager(config)  # type: ignore[assignment]  # noqa: SLF001
    orch = EventOrchestrator.__new__(EventOrchestrator)
    orch.plugin_manager = host._FixturePluginManager({"agent_messaging_plugin": messaging})  # type: ignore[assignment]  # noqa: SLF001
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
    plugin, _ = _plugin({"alert_role": ROLE}, messaging)
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
    _check(len(messaging.sends) == 1 and messaging.sends[0]["name"] == ROLE, "the configured alert_role (plugin config) is the alert target")
    plugin.set_active(False)
    _check(plugin.check_model_capability_catalog_cron({}, fire_state)["data"]["dispatch"] == "inactive", "the inactive colour's fires are no-ops")


def test_cron_background_failure_is_logged_at_error() -> None:
    state = RealShapeState()
    messaging = _CatalogMessagingPlugin(state)
    plugin, orch = _plugin({"alert_role": ROLE}, messaging)
    orch.plugin_manager = host._FixturePluginManager({})  # type: ignore[assignment]  # noqa: SLF001 -- messaging plugin unresolvable
    capture = _LogCapture()
    plugin.logger.addHandler(capture)
    try:
        fire = plugin.check_model_capability_catalog_cron({}, {})
        finished = _wait_for_check(plugin)
    finally:
        plugin.logger.removeHandler(capture)
    _check(fire["data"]["dispatch"] == "started" and finished, "a failing check does not raise into the scheduler")
    _check(any("catalog check failed" in message for message in capture.messages), "the failure is logged at ERROR")


def test_direct_verb_returns_the_result_and_hands_state_to_the_alert() -> None:
    for failing in (False, True):
        state = RealShapeState()
        messaging = _CatalogMessagingPlugin(state)
        if failing:
            messaging.raise_on_refresh = RuntimeError("controlled failure")
        plugin, _ = _plugin({"alert_role": ROLE}, messaging)
        original = messaging.refresh_model_capability_catalog_sync
        job = refresh_fixtures.prove_queued_operation(plugin, "check_model_capability_catalog", {}, messaging, "refresh_model_capability_catalog_sync", original, None)
        data = job["result"]["data"]
        _check(data["status"] == ("critical" if failing else "ok"), "the queued guard preserves the final refresh decision")
        _check(failing or data["cells_accepted"] == 3, "the queued guard preserves final run counts")
        _check(messaging.refresh_states[0]["session_id"] == "smoke-session", "the worker receives the dispatch context")


def main() -> int:
    print("=== platform_health_plugin model_capability_guard_smoke ===")
    original_get_config = plugin_module.get_config
    try:
        test_guard_invokes_the_refresh_with_the_cron_trigger()
        test_fetch_failure_raises_critical_once_and_a_repeat_sends_nothing()
        test_many_failed_sources_are_capped_in_the_alert()
        test_a_refresh_that_raises_is_critical_logged_and_not_swallowed()
        test_a_failure_envelope_is_critical()
        test_an_unreadable_refresh_result_is_critical_not_ok()
        test_messaging_plugin_unavailable_raises()
        test_readings_gathered_but_nothing_renewed_warns_once()
        test_cell_age_warns_at_49h_critical_at_73h_and_a_healthy_refresh_recovers_once()
        test_the_window_is_read_from_the_catalog_not_hard_coded()
        test_assess_boundaries_are_strict()
        test_renewal_is_a_newer_measurement_not_a_count()
        test_no_alert_role_still_refreshes_and_reports_why()
        test_an_undelivered_alert_is_retried_next_tick()
        test_state_service_failure_propagates()
        test_catalog_read_pages_past_the_old_wall_and_truncates_at_the_ceiling()
        test_catalog_read_refuses_malformed_rows()
        test_null_windows_are_unknown_and_malformed_values_still_raise()
        test_constants_match_the_catalog_owner()
        test_schedule_installer_is_idempotent_and_tagged()
        test_schedule_installer_does_not_need_an_alert_role()
        test_schedule_installer_lookup_failure_raises()
        test_registry_declares_the_three_verbs_with_the_right_shape()
        test_cron_entry_is_terminal_and_the_fire_stamps_the_flow_id()
        test_whole_tree_gate_c51_sees_and_exempts_the_literal()
        test_the_starting_action_installs_the_cron_after_startup()
        test_cron_verb_runs_the_refresh_in_the_background_one_at_a_time()
        test_cron_background_failure_is_logged_at_error()
        test_direct_verb_returns_the_result_and_hands_state_to_the_alert()
    finally:
        plugin_module.get_config = original_get_config
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
