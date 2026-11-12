"""Offline async action, base-result and real executor coverage for agent_messaging_plugin.

Corpus assertions use RealShapeState. They do not establish real-PG inbox
completeness above 100 held roles: predecessor RED iss_67da3915 is retained
separately, because that fake does not enforce the provider's read cap.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Generator
from contextlib import ExitStack, contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from functools import wraps
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "ananta/src", ROOT / "plugins/agent_messaging_plugin/src", ROOT / "plugins/agent_messaging_plugin/tests"):
    sys.path.insert(0, str(source))

import managed_dispatch_smoke as dispatch_fixture  # noqa: E402
import model_cell_enablement_smoke as cells_fixture  # noqa: E402
import peer_inbox_horizon_both_sections_smoke as inbox_fixture  # noqa: E402
from _real_state_fake import RealShapeState  # noqa: E402
from _register_unit_double import fixture_assignment  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller, QueuedAction  # noqa: E402
from ananta.core.state.execution_token_context import action_execution_context  # noqa: E402
from session_lifecycle_transport_smoke import packet_lane  # noqa: E402
from session_lifecycle_verbs_smoke import _FakeDriverWithChannel  # noqa: E402

from agent_messaging_plugin import (  # noqa: E402
    managed_dispatch,  # noqa: E402
    session_hosts,  # noqa: E402
)
from agent_messaging_plugin import plugin as plugin_module  # noqa: E402
from agent_messaging_plugin import session_lifecycle_verbs as lifecycle_bodies  # noqa: E402
from agent_messaging_plugin.operation_jobs import RegisteredOperations  # noqa: E402
from agent_messaging_plugin.session_lifecycle_store import ManagedSessionSpec, insert_managed_session, transition_lifecycle_state  # noqa: E402
from plugins.agent_messaging_plugin.tests.small_plugin_job_support import Ledger, check_wrapper, execute, measure_case, terminal  # noqa: E402

CASES: tuple[tuple[str, dict[str, Any]], ...] = (("reconcile_ledger_tmux", {}),)
REGISTERED_CASES = (
    "deliver_result",
    "deliver_error",
    "deliver_job_completion",
    "managed_dispatch_status",
    "report_managed_dispatch",
    "set_model_cells_enabled",
    "peer_inbox",
    "retire_role",
    "send_peer_message",
    "set_autonomic_slot",
    "reconcile_operator_session_liveness",
    "clear_session",
    "compact_session",
    "preserve_lane_evidence",
    "read_lane_evidence",
    "drive_session",
    "retire_session",
    "start_interface",
)
USES_U1_ADAPTER_DOUBLE = not hasattr(ActionProcessor, "_admit_registered_owner")


@contextmanager
def _u1_adapter_contract() -> Generator[None]:
    """Owned U1 double on the old base only; merged U1 must use its real adapter.

    Main-seat ruling agm-_088b969d87c6429995e6c7e79418d0ba permits this exact
    boundary double. It supplies only owner-hook selection and trusted state;
    the real poller, ActionProcessor, owner worker and operation leaves run.
    The U1 owner-admission seam disables the double, so the merged battery
    fails if real registered plugin dispatch does not reach the owner hook.
    """
    if not USES_U1_ADAPTER_DOUBLE:
        yield
        return
    original = ActionProcessor._execute_plugin_method

    def selected(processor: Any, plugin: Any, method_name: str, arguments: dict[str, Any], action: Any) -> dict[str, Any]:
        state = {"session_id": action.session_id, "flow_id": action.flow_id, "call_context": processor._build_call_context(action)}
        processor._lift_inference_vertex_identity(state, action)
        with action_execution_context(action.flow_token_id):
            result = plugin.admit_registered_operation(method_name, arguments, state)
        outcome = original(processor, plugin, method_name, arguments, action) if result is None else result
        assert isinstance(outcome, dict), "plugin_action_result_is_an_envelope"
        return outcome

    with patch.object(ActionProcessor, "_execute_plugin_method", new=selected):
        yield


@contextmanager
def fixture(module: ModuleType, _verb: str, *, delay: float = 0.0, failed: bool = False, failure_message: str = "fixture transport failure") -> Generator[tuple[Any, Ledger, list[int]]]:
    ledger, touches = Ledger(), []

    def transport(result: Any) -> Any:
        touches.append(threading.get_ident())
        time.sleep(delay)
        if failed:
            raise RuntimeError(failure_message)
        return result

    plugin = module.AgentMessagingPlugin()
    plugin.logger = logging.getLogger("async-action-fixture")
    state = RealShapeState()
    plugin.orchestrator_ref = SimpleNamespace(async_job_manager=ledger, get_service=lambda _name: state)
    with ExitStack() as stack:
        stack.enter_context(patch.dict("os.environ", {"SOLET_NAME": "fixture"}))
        stack.enter_context(patch("subprocess.run", side_effect=lambda *_args, **_kwargs: transport(subprocess.CompletedProcess([], 0, b""))))
        yield plugin, ledger, touches


def test_execute_paths() -> None:
    goldens = json.loads(Path(__file__).with_name("async_actions_base.json").read_text())
    for verb, params in CASES:
        for failed in (False, True):
            measure_case(plugin_module, verb, params, failed, goldens, fixture)


def test_wrapper_selection() -> None:
    for verb, params in CASES:
        with fixture(plugin_module, verb) as (plugin, ledger, _touches):
            check_wrapper(plugin, ledger, verb, params)


def test_empty_exception_message() -> None:
    with fixture(plugin_module, "reconcile_ledger_tmux", failed=True, failure_message="") as (plugin, ledger, _touches):
        plugin._start_choreography_worker()
        try:
            receipt = execute(plugin, "reconcile_ledger_tmux", {})
            row = terminal(plugin, ledger, receipt["data"]["job_id"])
            expected = {"action_status": "error", "error": ""}
            assert row["status"] == "error" and row["result"] == expected and row["error"] == expected, row
        finally:
            plugin._stop_choreography_worker()


async def _poll_registered(plugin: Any, verb: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Real poller dispatch and ActionProcessor, replacing only storage/context ports."""
    results: list[dict[str, Any]] = []
    failures: list[str] = []
    poller = ActionQueuePoller.__new__(ActionQueuePoller)
    poller.action_processor = cast("Any", SimpleNamespace(execute_action=lambda _action: execute(plugin, verb, parameters)))

    def completed(_action_id: str, result: dict[str, Any], _token: str | None, *, on_status_written: Any) -> None:
        results.append(result)
        on_status_written()

    action = QueuedAction(id=f"fixture-{verb}", process_key=f"plugin::{plugin.name}::{verb}", parameters=json.dumps(parameters), notes="fixture", created_at="", session_id="fixture-session", flow_id="fixture-flow")
    with ExitStack() as stack:
        stack.enter_context(_u1_adapter_contract())
        stack.enter_context(patch.object(poller, "_is_terminated_flow_sibling", return_value=False))
        stack.enter_context(patch.object(poller, "_prepare_action_for_execution", return_value=True))
        stack.enter_context(patch.object(poller, "_resolve_io_context", return_value=None))
        stack.enter_context(patch.object(poller, "_mark_action_completed", side_effect=completed))
        stack.enter_context(patch.object(poller, "_mark_action_failed", side_effect=lambda *_args, **_kwargs: failures.append(str(_args))))
        await poller._process_action(action)
    assert not failures and len(results) == 1, (verb, failures, results)
    return results[0]


def test_registered_driver_admission() -> None:
    """A real channel send cannot hold the serial poller before its receipt."""
    plugin, ledger, state = plugin_module.AgentMessagingPlugin(), Ledger(), RealShapeState()
    plugin.logger = logging.getLogger("registered-messaging-fixture")
    plugin.orchestrator_ref = SimpleNamespace(async_job_manager=ledger, get_service=lambda _name: state)
    insert_managed_session(cast("Any", state), ManagedSessionSpec(agent_instance_id="agi-compact", lane_id="compact", assignment=fixture_assignment(), work_class="read_only", budget_line="fixture", host="registered-fixture"))
    transition_lifecycle_state(cast("Any", state), agent_instance_id="agi-compact", from_state="spawning", to_state="live", directed_by="fixture")
    driver = _FakeDriverWithChannel()
    entered, release = threading.Event(), threading.Event()

    def send(text: str) -> None:
        entered.set()
        assert release.wait(5), "channel_leaf_released"
        driver.channel.sent.append(text)

    async def scenario() -> None:
        async def serial_dispatch() -> None:
            receipt = await _poll_registered(plugin, "compact_session", {"agent_instance_id": "agi-compact"})
            assert receipt.get("data", {}).get("status") == "queued", "registered_entry_returns_persisted_admission"
            job_id = receipt["data"]["job_id"]
            assert ledger.rows[job_id]["provider_name"] == "agent_messaging_plugin.compact_session", "receipt_identifies_persisted_operation"
            ordinary = await _poll_registered(plugin, "check_choreography_job_status", {"job_id": job_id})
            assert ordinary["success"] and not release.is_set(), "ordinary_dispatch_while_driver_parked"

        dispatch = asyncio.create_task(serial_dispatch())
        try:
            assert await asyncio.to_thread(entered.wait, 2), "actual_driver_leaf_reached"
            await asyncio.wait_for(asyncio.shield(dispatch), 0.5)
        finally:
            release.set()
            await dispatch

    with patch.dict(session_hosts._REGISTRY, {"registered-fixture": driver}), patch.object(driver.channel, "send", side_effect=send):
        asyncio.run(scenario())
    job_id = next(iter(ledger.rows))
    assert terminal(plugin, ledger, job_id)["result"]["data"]["lifecycle_state"] == "live"
    assert driver.channel.sent == ["/compact"], "driver_runs_exactly_once"
    print("PASS compact_session: actual poller, persisted admission, parked driver, ordinary serial dispatch")


def _assert_admission(receipt: dict[str, Any], ledger: Ledger, verb: str) -> str:
    assert receipt["success"] and receipt["action_status"] == "completed" and receipt["error"] is None, receipt
    assert receipt["timestamp"] and receipt["actions"] == [] and receipt["data"]["status"] == "queued", receipt
    job_id = receipt["data"]["job_id"]
    assert ledger.rows[job_id]["provider_name"] == f"agent_messaging_plugin.{verb}", ledger.rows
    return job_id


def test_registered_operation_matrix() -> None:
    """Dispatch controls: every selected key, complete envelopes and synchronous callers."""
    for verb in REGISTERED_CASES:
        _registered_operation_case(verb)


def _registered_operation_case(verb: str) -> None:
    with fixture(plugin_module, verb) as (plugin, ledger, _touches):
        # Completed population cannot displace this operation's exact id.
        for index in range(200):
            stale = ledger.create_job(plugin_name=plugin.name, action_name=verb, request_data={})["data"]["job_id"]
            ledger.update_job(stale, {"status": "completed", "result": {"old": index}})
        entered, release = threading.Event(), threading.Event()
        body_name = "peer_inbox_action" if verb == "peer_inbox" else verb
        body = getattr(plugin, body_name)
        expected = {"action_status": "completed", "data": {"exact": verb, "history": list(range(500))}, "actions": [], "error": None, "timestamp": "fixture"}

        @wraps(body)
        def operation(params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
            assert params == {"event_kind": "completion", "value": verb} and state["session_id"] == "fixture-session", "request_and_server_context_retained"
            entered.set()
            assert release.wait(5), "matrix_body_released"
            return expected

        with patch.object(plugin, body_name, new=operation):
            try:
                receipt = asyncio.run(_poll_registered(plugin, verb, {"event_kind": "completion", "value": verb}))
                job_id = _assert_admission(receipt, ledger, verb)
                assert len(ledger.rows) == 201 and entered.wait(2), "exactly_one_new_owned_job"
                ordinary = asyncio.run(_poll_registered(plugin, "check_choreography_job_status", {"job_id": job_id}))
                assert ordinary["success"] and not release.is_set(), "ordinary_dispatch_before_body_finishes"
            finally:
                release.set()
            assert terminal(plugin, ledger, job_id)["result"] == expected, "complete_exact_id_envelope"
            assert operation({"event_kind": "completion", "value": verb}, {"session_id": "fixture-session"}) == expected and len(ledger.rows) == 201, "direct_body_has_no_recursive_admission"
        print(f"PASS registered dispatch control {verb}: exact id, one job, complete history, synchronous body")


def test_registered_ack_and_completion() -> None:
    """Native ACK/milestone are immediate; only the real artifact hash is queued."""
    with tempfile.TemporaryDirectory() as raw:
        state = dispatch_fixture._state()
        prepared = dispatch_fixture._prepare(state, Path(raw))
        dispatch_fixture._first_turn(state, delivered=True)
        plugin, ledger = plugin_module.AgentMessagingPlugin(), Ledger()
        plugin.orchestrator_ref = SimpleNamespace(async_job_manager=ledger, get_service=lambda _name: state)
        base = {"dispatch_id": "mdp-fixture", "attempt_agent_instance_id": "agi-worker-1"}
        identity = dispatch_fixture._assignment_identity(managed_dispatch.read_managed_dispatch(state, "mdp-fixture"))
        with patch.object(plugin, "_dispatch_actor_from_state", return_value=dispatch_fixture._worker_actor()):
            ack = asyncio.run(_poll_registered(plugin, "report_managed_dispatch", {**base, "event_id": "outer-ack", "event_kind": "ack", "prior_version": 1, "payload": {**identity, "role_binding": "Codex-Fixture-Builder", "scope_readback_sha256": "a" * 64, "plan_sha256": "b" * 64}}))
            assert ack["data"]["state"] == "active" and not ledger.rows, "native_ack_immediately_accepted_without_job"
            milestone = asyncio.run(
                _poll_registered(
                    plugin,
                    "report_managed_dispatch",
                    {**base, "event_id": "outer-milestone", "event_kind": "milestone", "prior_version": 2, "payload": {"completed_work": ["fixture"], "current_evidence": {"fixture": "pass"}, "next_action": "completion", "next_report_deadline": (datetime.now(UTC) + timedelta(minutes=10)).isoformat()}},
                )
            )
            assert milestone["data"]["version"] == 3 and not ledger.rows, "native_milestone_immediately_readable"
            _completion_hash_case(plugin, ledger, state, prepared, base, identity)


def _completion_hash_case(plugin: Any, ledger: Ledger, state: Any, prepared: dict[str, Any], base: dict[str, Any], identity: dict[str, Any]) -> None:
    artifact = Path(prepared["expected_path"])
    artifact.write_text("complete fixture\n")
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    entered, release = threading.Event(), threading.Event()
    original = managed_dispatch._file_sha256

    def hashed(path: Path) -> str:
        assert len(ledger.rows) == 1, "job_persisted_before_actual_hash"
        entered.set()
        assert release.wait(5), "hash_leaf_released"
        return original(path)

    parameters = {
        **base,
        "event_id": "outer-completion",
        "event_kind": "completion",
        "prior_version": 3,
        "payload": {**identity, "artifact_path": str(artifact), "artifact_sha256": digest, "completion_contract_sha256": prepared["completion_contract_sha256"], "verdict": "READY-FOR-REVIEW", "evidence": {"focused": {"status": "pass", "detail": "fixture"}, "registered": {"status": "pass", "detail": "fixture"}}},
    }
    with patch.object(managed_dispatch, "_file_sha256", side_effect=hashed):
        try:
            receipt = asyncio.run(_poll_registered(plugin, "report_managed_dispatch", parameters))
            job_id = _assert_admission(receipt, ledger, "report_managed_dispatch")
            assert entered.wait(2) and managed_dispatch.read_managed_dispatch(state, "mdp-fixture")["state"] == "active", "receipt_is_not_completion_acceptance"
            ordinary = asyncio.run(_poll_registered(plugin, "check_choreography_job_status", {"job_id": job_id}))
            assert ordinary["success"] and not release.is_set(), "ordinary_dispatch_during_actual_hash"
        finally:
            release.set()
        outcome = terminal(plugin, ledger, job_id)["result"]
    assert outcome["data"]["state"] == "completion_reported" and outcome["data"]["version"] == 4, outcome
    stale = asyncio.run(_poll_registered(plugin, "report_managed_dispatch", {**parameters, "event_id": "outer-stale"}))
    rejected = terminal(plugin, ledger, stale["data"]["job_id"])
    assert rejected["status"] == "error" and rejected["result"]["error"]["code"] == "stale_dispatch_version", "stale_cas_refusal_retained_in_terminal_job"
    assert managed_dispatch.read_managed_dispatch(state, "mdp-fixture")["version"] == 4, "stale_completion_does_not_advance_state"
    print("PASS native ACK/milestone, real parked completion hash, exact result, stale CAS refusal")


def test_unselected_operations() -> None:
    """Unselected work and carved-out receipt consumers retain their direct path."""
    with fixture(plugin_module, "compact_session") as (plugin, ledger, _touches):
        assert isinstance(plugin, RegisteredOperations), "registered_owner_mixin_is_bound"
        assert plugin.admit_registered_operation("report_alive", {}, {}) is None and not ledger.rows, "unselected_has_no_admission_or_effect"
        for verb in ("peer_ack_role_read_page", "peer_role_read_receipts"):
            assert plugin.admit_registered_operation(verb, {}, {}) is None and not ledger.rows, "synchronous_receipt_consumer_carveout"
        for kind in ("ack", "milestone", "blocked", "invalid"):
            assert plugin.admit_registered_operation("report_managed_dispatch", {"event_kind": kind}, {}) is None and not ledger.rows, "noncompletion_reporting_keeps_native_path"
    print("PASS unselected and carved-out operations have no admission or effects")


def test_uncertain_operations() -> None:
    """Uncertain outcomes never authorize replay."""
    with fixture(plugin_module, "compact_session") as (plugin, ledger, _touches):
        calls: list[str] = []
        marker: ContextVar[str] = ContextVar("messaging-owner-context", default="missing")

        def effect(_params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
            calls.append(marker.get())
            assert state["flow_id"] == "fixture-flow"
            return {"action_status": "error", "error": {"code": "fixture.effect_error"}, "data": {"evidence": list(range(100))}}

        original_update = ledger.update_job

        def reject_terminal(job_id: str, updates: dict[str, Any]) -> dict[str, Any]:
            if updates.get("status") in {"completed", "error"}:
                return {"action_status": "failed", "error": {"code": "fixture.terminal_write"}}
            return original_update(job_id, updates)

        with patch.object(plugin, "compact_session", new=effect), patch.object(ledger, "update_job", side_effect=reject_terminal):
            marker.set("captured")
            receipt = plugin.admit_registered_operation("compact_session", {}, {"session_id": "fixture-session", "flow_id": "fixture-flow"})
            assert receipt is not None
            worker = plugin._operation_jobs._thread
            if worker is not None:
                worker.join(3)
            assert ledger.rows[receipt["data"]["job_id"]]["status"] == "processing" and calls == ["captured"], "uncertain_terminal_stays_owned"
            plugin._operation_jobs._drain()
            assert calls == ["captured"], "no_replay_after_terminal_write_failure"
        print("PASS copied context and terminal uncertainty without replay")


def _parked_result(plugin: Any, ledger: Ledger, verb: str, parameters: dict[str, Any], module: Any, leaf: str) -> dict[str, Any]:
    """Park an actual operation leaf; retain the complete original result."""
    entered, release = threading.Event(), threading.Event()
    original = getattr(module, leaf)

    def parked(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5), f"{leaf}_released"
        return original(*args, **kwargs)

    with patch.object(module, leaf, side_effect=parked):
        try:
            receipt = asyncio.run(_poll_registered(plugin, verb, parameters))
            job_id = _assert_admission(receipt, ledger, verb)
            assert entered.wait(2), f"actual_{leaf}_reached"
            ordinary = asyncio.run(_poll_registered(plugin, "check_choreography_job_status", {"job_id": job_id}))
            assert ordinary["success"] and not release.is_set(), "ordinary_dispatch_while_actual_leaf_parked"
        finally:
            release.set()
        return terminal(plugin, ledger, job_id)["result"]


def test_complete_dispatch_event_population() -> None:
    with tempfile.TemporaryDirectory() as raw:
        state = cast("Any", dispatch_fixture._state())
        dispatch_fixture._prepare(state, Path(raw))
        dispatch_fixture._first_turn(state, delivered=True)
        dispatch_fixture._ack(state)
        # Derive the declared namespace/table from the same schema as the reader.
        from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE

        from agent_messaging_plugin.schema import TABLE_MANAGED_DISPATCH_EVENT

        namespace, table = AGENT_ROLE_BINDING_NAMESPACE, TABLE_MANAGED_DISPATCH_EVENT
        seed = {key: value for key, value in state.rows(namespace, table)[0].items() if key not in {"id", "external_id"}}
        for index in range(300):
            state.write_state(namespace, {"table": table, "record": {**seed, "event_id": f"population-{index}", "event_kind": "fixture-rejected", "accepted": False}})
        plugin, ledger = plugin_module.AgentMessagingPlugin(), Ledger()
        plugin.orchestrator_ref = SimpleNamespace(async_job_manager=ledger, get_service=lambda _name: state)
        from agent_messaging_plugin import managed_dispatch_bridge

        outcome = _parked_result(plugin, ledger, "managed_dispatch_status", {"dispatch_id": "mdp-fixture"}, managed_dispatch_bridge, "all_managed_dispatch_events")
        assert outcome["data"]["event_count"] == len(state.rows(namespace, table)) and outcome["data"]["rejected_event_count"] == 300, "complete_event_population_not_sliced"
        print("PASS native status: 300 rejected events plus full original receipt history")


def test_complete_cell_population_and_stale_policy() -> None:
    state, plugin, ledger = cells_fixture.fixture(), plugin_module.AgentMessagingPlugin(), Ledger()
    for index in range(300):
        state.write_state(cells_fixture.NS, {"table": cells_fixture.CELLS, "record": cells_fixture.cell(f"fixture-{index}")})
    plugin.orchestrator_ref = SimpleNamespace(async_job_manager=ledger, get_service=lambda _name: state)
    actor = managed_dispatch.DispatchActor("agi-operator", "ases-operator", "live_peer_binding")
    parameters = {"selector": {}, "enabled": False, "reason": "fixture policy", "expected_updated_at": None}
    with patch.object(plugin, "_dispatch_actor_from_state", return_value=actor):
        outcome = _parked_result(plugin, ledger, "set_model_cells_enabled", parameters, cells_fixture.policy, "read_cells_page")
        assert len(outcome["data"]["cells"]) == 300 and not any(row["enabled"] for row in outcome["data"]["cells"]), "complete_cell_readback_and_atomic_policy"
        assert outcome["data"]["history"], "policy_history_retained"
        stale = asyncio.run(_poll_registered(plugin, "set_model_cells_enabled", parameters))
        rejected = terminal(plugin, ledger, stale["data"]["job_id"])
        assert rejected["result"]["error"]["code"] == "stale_policy_state", "stale_policy_premise_refuses_without_changes"
    print("PASS native cell policy: 300 complete cells, history and stale-write refusal")


def test_complete_role_population_and_receipts() -> None:
    state, plugin = inbox_fixture._world()
    ledger = Ledger()
    plugin.orchestrator_ref = SimpleNamespace(async_job_manager=ledger, get_service=lambda _name: state)
    from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE, TABLE_ROLE_BINDING

    template = {key: value for key, value in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_ROLE_BINDING)[0].items() if key not in {"id", "external_id"}}
    for index in range(150):
        role = f"population-role-{index}"
        state.write_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_ROLE_BINDING, "record": {**template, "external_id": f"role:{role}", "role": role}})
    for index in range(5, 155):
        inbox_fixture._write_role(state, plugin, index, second=index)
    parameters = {"agent_session_id": inbox_fixture._SESSION_ID, "limit": 5}
    outcome = _parked_result(plugin, ledger, "peer_inbox", parameters, plugin_module, "read_peer_inbox_for_session")
    assert len(outcome["data"]["role_read_coverage"]) == 151 and outcome["data"]["role_section_status"] == "ok", "all_held_roles_read_with_honest_page_boundary"
    token = outcome["data"]["role_read_page_token"]
    assert token and outcome["data"]["role_entries"], "exact_page_receipt_has_real_messages"
    ack = asyncio.run(_poll_registered(plugin, "peer_ack_role_read_page", {"agent_session_id": inbox_fixture._SESSION_ID, "page_token": token}))
    assert ack["data"]["status"] == "acked" and ack["data"]["receipt_count"] == len(outcome["data"]["role_entries"]), "receipt_acknowledges_exact_page_only"
    lookup = asyncio.run(_poll_registered(plugin, "peer_role_read_receipts", {"agent_session_id": inbox_fixture._SESSION_ID, "candidates": []}))
    assert lookup["data"]["agent_instance_id"] == inbox_fixture._INSTANCE_ID and lookup["data"]["results"] == [] and len(ledger.rows) == 1, "carved_out_receipt_verbs_keep_immediate_final_data"
    print("PASS in-memory inbox: 151 held roles, 155 role messages, exact page acknowledgement; real-PG >100 remains RED")


def test_real_custody_leaves() -> None:
    with tempfile.TemporaryDirectory() as raw:
        lane = packet_lane(Path(raw), "registered-custody")
        plugin_module.AgentMessagingPlugin.__init__(lane.plugin)
        ledger = Ledger()
        lane.plugin.orchestrator_ref = SimpleNamespace(async_job_manager=ledger, get_service=lambda _name: lane.state)
        with patch.dict("os.environ", lane.environment, clear=True):
            snapshot = _parked_result(lane.plugin, ledger, "read_lane_evidence", lane.identity, lifecycle_bodies, "snapshot_lane_custody")
            parameters = {**lane.identity, "expected_fingerprint": snapshot["data"]["fingerprint"], "disposition": "preserved", "rationale": "registered custody fixture"}
            saved = _parked_result(lane.plugin, ledger, "preserve_lane_evidence", parameters, lifecycle_bodies, "capture_lane_custody")
            assert saved["data"]["complete"] and saved["data"]["custody_verified"], "actual_custody_packet_read_verified"
            assert saved["data"]["binding"]["agent_instance_id"] == lane.identity["agent_instance_id"] and lane.lane.path.exists(), "exact_bound_custody_without_retirement"
        print("PASS real custody snapshot and capture: persisted jobs precede filesystem leaves")


if __name__ == "__main__":
    print(f"ADAPTER EVIDENCE: U1 owner-hook double={USES_U1_ADAPTER_DOUBLE}; real merged route required at landing")
    test_registered_driver_admission()
    test_registered_operation_matrix()
    test_registered_ack_and_completion()
    test_unselected_operations()
    test_uncertain_operations()
    test_complete_dispatch_event_population()
    test_complete_cell_population_and_stale_policy()
    test_complete_role_population_and_receipts()
    test_real_custody_leaves()
    test_execute_paths()
    test_wrapper_selection()
    test_empty_exception_message()
    print("PASS agent_messaging_plugin async action coverage")
