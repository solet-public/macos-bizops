"""Real registered delivery admission, bounded requests and terminal routing.

All storage, recipient and bridge identities belong to this fixture. The real
scanner, action factory, processor, poller, job manager and delivery bodies run.
Peer listing intentionally keeps its synchronous complete result for hook callers.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for source in (ROOT, ROOT / "ananta/src", ROOT / "ananta/tests/core/actions", ROOT / "plugins/agent_messaging_plugin/src", Path(__file__).resolve().parent):
    sys.path.insert(0, str(source))

from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller  # noqa: E402
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator  # noqa: E402
from ananta.core.process_registry.plugin_process_scanner import PluginProcessScanner  # noqa: E402
from ananta.core.process_registry.plugin_registration_validator import PluginRegistrationValidator  # noqa: E402
from ananta.core.process_registry.service_interface_metadata_generator import ServiceInterfaceMetadataGenerator  # noqa: E402
from ananta.core.state.async_job_manager import AsyncJobManager  # noqa: E402
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph  # noqa: E402
from ananta.platform.new_template_engine import NewTemplateEngine  # noqa: E402
from peer_inbox_horizon_both_sections_smoke import _ROLE, _world  # noqa: E402
from peer_list_process_smoke import _binding  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from quality_gates.private_test_pg import MissingFixtureError, ledger_fixture  # noqa: E402

DELIVERIES = ("deliver_result", "deliver_error", "deliver_job_completion")
atomic_fixture = importlib.import_module("async_job_atomic_completion_smoke")
owner_fixture = importlib.import_module("async_job_manager_completion_handler_smoke")


class Harness:
    """Real job transactions plus owned, in-memory peer and bridge providers."""

    def __init__(self) -> None:
        self.state, self.manager, self.token, self.completed = atomic_fixture.manager_fixture()
        self.peer_state, self.plugin = _world()
        # The existing role fixture models query_state. Delivery also reads
        # the original job through the equivalent bounded read_state port.
        self.peer_state.read_state = self.peer_state.query_state
        self.peer_state.rows("core", "job").append({"id": "original-owned-job", "metadata": json.dumps({"completion_reach": "bridge_dispatch_no_return_path"})})
        self.plugin.orchestrator_ref = SimpleNamespace(async_job_manager=self.manager, get_service=lambda _name: self.peer_state)
        self.bridge_manager = BridgeSessionManager(session_id_factory=lambda _name: "s", idle_timeout_s=60, max_pending_events=20, long_poll_timeout_s=1)
        self.bridge = self.bridge_manager.open("owned-messaging-fixture")
        self.plugin._bridge_manager = self.bridge_manager
        plugins = PluginManager()
        plugins.plugins[self.plugin.name] = self.plugin
        self.registry: dict[str, Any] = {"processes": {}}
        scanner = PluginProcessScanner(plugins, PluginRegistrationValidator(), ServiceInterfaceMetadataGenerator(), InvocationSchemaGenerator())
        scanner._process_individual_plugin(self.plugin.name, self.plugin, self.registry)
        patterns = json.loads((ROOT / "ananta/src/ananta/platform/metadata/schemas/template_patterns.json").read_text())
        templates = NewTemplateEngine(cast(Any, SimpleNamespace(get_template_patterns=lambda: patterns)))
        self.factory = ActionFactory(process_registry=self.registry, template_engine=templates, state_service=self.state._id_service)
        self.processor = owner_fixture.ordinary_plugin_processor(plugins, self.state, "plugin::agent_messaging_plugin::deliver_result")
        self.processor.process_registry = self.registry
        self.processor.discovery_service.processes = self.registry["processes"]
        self.processor.orchestrator = cast(Any, self.plugin.orchestrator_ref)
        self.callbacks: list[tuple[Any, ...]] = []
        self.manager.set_registered_completion_handler(lambda *values: self.callbacks.append(values))
        self.submitted: list[dict[str, Any]] = []
        self.manager.set_action_factory(cast(Any, SimpleNamespace(submit_action_definition=lambda definition: self.submitted.append(definition))))
        self.state.rows["flows"][0]["trigger_data"] = json.dumps({"completion_route_role": "owned-fixture-recipient"})

    def action(self, verb: str, parameters: dict[str, Any]) -> Any:
        key = f"plugin::agent_messaging_plugin::{verb}"
        runtime = self.factory.create_action({"name": "owned delivery", "process_key": key, "arguments": parameters, "notes": "owned registered admission", "flow_id": "flow", "session_id": "s", "result_processor": None, "error_processor": None})
        return atomic_fixture.FixtureAction(f"action-{verb}", key, json.dumps(runtime["parameters"]), "owned delivery", "now", session_id="s", flow_id="flow", flow_token_id=self.token, source_plugin="agent_messaging_plugin")

    async def poll(self, action: Any) -> dict[str, Any]:
        """Replace storage/context ports, retaining the actual poller dispatch."""
        poller: Any = ActionQueuePoller.__new__(ActionQueuePoller)
        poller.action_processor = self.processor
        results: list[dict[str, Any]] = []
        failures: list[tuple[Any, ...]] = []

        def recorded(_action_id: str, result: dict[str, Any], _token: str | None, *, on_status_written: Any) -> None:
            results.append(result)
            on_status_written()

        with ExitStack() as stack:
            for name, value in (("_is_terminated_flow_sibling", False), ("_prepare_action_for_execution", True), ("_resolve_io_context", None)):
                stack.enter_context(patch.object(poller, name, return_value=value))
            stack.enter_context(patch.object(poller, "_mark_action_completed", side_effect=recorded))
            stack.enter_context(patch.object(poller, "_mark_action_failed", side_effect=lambda *args, **_kwargs: failures.append(args)))
            await poller._process_action(action)
        assert not failures and len(results) == 1, (failures, results)
        return results[0]

    def finish(self, job_id: str) -> dict[str, Any]:
        worker = self.plugin._operation_jobs._thread
        if worker is not None:
            worker.join(5)
            assert not worker.is_alive(), "owned worker drains"
        job = self.manager.get_job(job_id)["data"]["job"]
        job["result"] = self.manager.get_job_payload(job_id, "result")["data"]["payload"]
        return job


def delivery_parameters(harness: Harness, verb: str) -> dict[str, Any]:
    payload = {"content": "owned-opaque-payload-" * 50000, "tail": [1, 2, 3]}
    if verb == "deliver_job_completion":
        return {"name": _ROLE, "job_id": "original-owned-job", "provider_name": "owned.operation", "status": "completed", "payload": payload}
    return {"bridge_id": harness.bridge.bridge_id, "source_process_key": "plugin::agent_messaging_plugin::peer_list", "result_payload" if verb == "deliver_result" else "error_payload": payload}


def parked_delivery(verb: str) -> None:
    harness = Harness()
    parameters = delivery_parameters(harness, verb)
    action = harness.action(verb, parameters)
    entered, release = threading.Event(), threading.Event()
    original = json.dumps
    touches: list[int] = []

    def parked(*args: Any, **kwargs: Any) -> Any:
        value = args[0] if args else None
        payload = value.get("payload", value) if isinstance(value, dict) else None
        if isinstance(payload, dict) and payload.get("tail") == [1, 2, 3] and "content" in payload:
            touches.append(threading.get_ident())
            entered.set()
            assert release.wait(5), "test releases the actual payload JSON encoder"
        return original(*args, **kwargs)

    with patch.object(json, "dumps", side_effect=parked), ThreadPoolExecutor(max_workers=1) as executor:
        started = time.perf_counter()
        pending = executor.submit(asyncio.run, harness.poll(action))
        try:
            assert entered.wait(3), "valid registered dispatch reached the real full-payload JSON encoder"
            try:
                receipt = pending.result(timeout=0.2)
            except TimeoutError:
                raise AssertionError(f"{verb}: receipt must precede full-payload JSON encoding; real encoder reached, registered caller still blocked") from None
            elapsed = (time.perf_counter() - started) * 1000
            job_id = assert_parked_receipt(harness, receipt, elapsed, release)
            worker = harness.plugin._operation_jobs._thread
            assert worker is not None and touches == [worker.ident], "only the serial owner enters the opaque payload encoder"
        finally:
            release.set()
            pending.result(timeout=5)
    job = assert_terminal_lineage(harness, job_id, action.id)
    assert_terminal_payload(harness, job, verb, parameters)
    assert len(touches) == 1
    print(f"PASS {verb}: registered scanner/factory/poller, {elapsed:.3f}ms receipt, bounded request, one terminal route")


def assert_parked_receipt(harness: Harness, receipt: dict[str, Any], elapsed: float, release: threading.Event) -> str:
    assert receipt["success"] and receipt["data"]["status"] == "queued", receipt
    assert elapsed < 50, elapsed
    job_id = str(receipt["data"]["job_id"])
    assert len(harness.state.rows["job"]) == 1, "exactly one atomic job"
    request = harness.manager.get_job_payload(job_id, "request")["data"]["payload"]
    assert len(json.dumps(request)) < 512, "only a bounded public descriptor is persisted"
    ordinary = asyncio.run(harness.poll(harness.action("peer_list", {})))
    assert ordinary["success"] and "instances" in ordinary["data"] and not release.is_set(), "ordinary dispatch completes during the parked effect"
    assert not harness.callbacks and not harness.bridge.pending_events, "no completion or append before formatting"
    return job_id


def assert_terminal_lineage(harness: Harness, job_id: str, action_id: str) -> dict[str, Any]:
    job = harness.finish(job_id)
    assert job["status"] == "completed" and job["flow_token_id"] == harness.token
    assert json.loads(job["metadata"])["registered_action_id"] == action_id
    assert len(harness.callbacks) == 1 and not harness.submitted, "one original completion, no recursive delivery job"
    assert harness.manager.update_job(job_id, {"status": "completed"})["action_status"] == "error", "stale terminal write refuses"
    return job


def assert_terminal_payload(harness: Harness, job: dict[str, Any], verb: str, parameters: dict[str, Any]) -> None:
    if verb != "deliver_job_completion":
        events = harness.bridge.pending_events
        assert len(events) == 1 and json.loads(events[0].content)["payload"] == parameters["result_payload" if verb == "deliver_result" else "error_payload"], "complete opaque payload delivered once"
        assert job["result"]["data"]["status"] == "queued"
    else:
        assert job["result"]["data"]["delivery"] == "queued_for_replay", "full completion operation keeps durable offline-role delivery"
        assert job["result"]["data"]["reach_stamped"] is True, "successful hand-off stamps the original job"
        original = harness.peer_state.rows("core", "job")[0]
        assert json.loads(original["metadata"])["completion_reach"] == "role_inbox_delivered"


def terminal_bridge_outcomes() -> None:
    for gone in (False, True):
        harness = Harness()
        if gone:
            harness.bridge_manager.close(harness.bridge.bridge_id)
        else:
            harness.bridge_manager._max_pending_events = 0
        receipt = asyncio.run(harness.poll(harness.action("deliver_error", delivery_parameters(harness, "deliver_error"))))
        job = harness.finish(receipt["data"]["job_id"])
        assert job["status"] == ("completed" if gone else "error"), job
        if gone:
            assert job["result"]["data"]["status"] == "dropped_bridge_gone"
        else:
            error = harness.manager.get_job_payload(job["id"], "error")["data"]["payload"]
            assert error["code"] == "bridge.queue_full", error
        assert len(harness.callbacks) == 1 and not harness.submitted, "result/error routing cannot spawn recursive delivery"
    print("PASS vanished bridge terminal drop and typed queue-full error")


def synchronous_list_completeness() -> None:
    harness = Harness()
    for index in range(250):
        harness.plugin._peer_registry.register(_binding(bridge_id=f"owned-{index}", agent_id="owned", agent_instance_id=f"owned-instance-{index}", session_label=f"owned-label-{index}"))
    result = asyncio.run(harness.poll(harness.action("peer_list", {})))
    assert len(result["data"]["instances"]["owned"]) == 250 and not harness.state.rows.get("job"), "hook carve-out returns all bindings synchronously"
    direct = harness.plugin.deliver_result(delivery_parameters(harness, "deliver_result"), {"flow_id": "flow", "session_id": "s"})
    assert direct["data"]["status"] == "queued" and not harness.state.rows.get("job"), "direct internal body returns final data with no job"
    print("PASS synchronous peer_list complete250 and internal delivery full results")


def worker_loss_and_terminal_uncertainty() -> None:
    for terminal_failure in (False, True):
        harness = Harness()
        parameters = delivery_parameters(harness, "deliver_result")
        updates = harness.manager.update_job
        append = harness.bridge_manager.append_event
        touches: list[str] = []

        def lost(*args: Any, terminal_failure: bool = terminal_failure, touches: list[str] = touches, append: Any = append, **kwargs: Any) -> Any:
            touches.append("entered")
            if not terminal_failure:
                raise SystemExit("owned worker loss")
            return append(*args, **kwargs)

        def persisted(job_id: str, values: dict[str, Any], *, terminal_failure: bool = terminal_failure, updates: Any = updates) -> dict[str, Any]:
            if terminal_failure and values.get("status") in {"completed", "error"}:
                return {"action_status": "error", "error": {"code": "owned.terminal_write"}}
            return cast(dict[str, Any], updates(job_id, values))

        with patch.object(harness.bridge_manager, "append_event", side_effect=lost), patch.object(harness.manager, "update_job", side_effect=persisted):
            receipt = asyncio.run(harness.poll(harness.action("deliver_result", parameters)))
            job_id = receipt["data"]["job_id"]
            worker = harness.plugin._operation_jobs._thread
            if worker is not None:
                worker.join(5)
                assert not worker.is_alive()
            harness.plugin._operation_jobs._drain()
        job = harness.manager.get_job(job_id)["data"]["job"]
        assert touches == ["entered"] and len(harness.state.rows["job"]) == 1, "uncertainty cannot replay an entered effect"
        if terminal_failure:
            assert job["status"] == "processing" and len(harness.bridge.pending_events) == 1 and not harness.callbacks
        else:
            error = harness.manager.get_job_payload(job_id, "error")["data"]["payload"]
            assert job["status"] == "error" and error["code"] == "messaging_owner.worker_lost" and error["details"]["uncertain"]
        assert not harness.submitted
    print("PASS worker loss and terminal-write uncertainty retain ownership without replay")


def cancelled_successor_keeps_fifo() -> None:
    harness = Harness()
    entered, release = threading.Event(), threading.Event()
    append = harness.bridge_manager.append_event

    def parked(*args: Any, **kwargs: Any) -> Any:
        entered.set()
        assert release.wait(5)
        return append(*args, **kwargs)

    with patch.object(harness.bridge_manager, "append_event", side_effect=parked):
        try:
            first = asyncio.run(harness.poll(harness.action("deliver_result", delivery_parameters(harness, "deliver_result"))))
            assert entered.wait(3)
            second = asyncio.run(harness.poll(harness.action("deliver_error", delivery_parameters(harness, "deliver_error"))))
            second_id = second["data"]["job_id"]
            assert harness.manager.update_job(second_id, {"status": "cancelled"})["action_status"] == "completed"
            assert not harness.bridge.pending_events, "later delivery cannot overtake the parked first"
        finally:
            release.set()
        harness.finish(first["data"]["job_id"])
    assert len(harness.bridge.pending_events) == 1 and harness.manager.get_job(second_id)["data"]["job"]["status"] == "cancelled"
    assert len(harness.callbacks) == 1 and not harness.submitted, "cancelled queued body never executes"
    print("PASS serial append ordering and cancellation before worker entry")


def private_job_persistence() -> None:
    """Supplementary real-PG job/request/outcome proof; native runs disclose absence."""
    from ananta.config.core_schemas import CoreSchemaDefinitions
    from ananta.services.schema_manager import SchemaManager

    try:
        with ledger_fixture("messaging_write") as fixture:
            schemas = SchemaManager(fixture.state, fixture.plugin)
            schemas.initialize_schemas([CoreSchemaDefinitions.get_job_schema(), CoreSchemaDefinitions.get_job_payload_schema()])
            fixture.state.set_schema_manager(schemas)
            harness = Harness()
            harness.manager = AsyncJobManager(fixture.state, FlowRuntimeGraph(fixture.state))
            harness.plugin.orchestrator_ref.async_job_manager = harness.manager
            receipt = harness.plugin.admit_registered_operation("deliver_result", delivery_parameters(harness, "deliver_result"), {"flow_id": "owned-pg-flow", "session_id": "owned-pg-session"})
            assert receipt is not None and receipt["data"]["status"] == "queued", receipt
            job_id = receipt["data"]["job_id"]
            job = harness.finish(job_id)
            request_envelope: Any = harness.manager.get_job_payload(job_id, "request")
            request = request_envelope["data"]["payload"]
            assert len(json.dumps(request)) < 512 and job["status"] == "completed", "real PG keeps bounded request and complete terminal result"
            assert job["result"]["data"]["status"] == "queued" and len(harness.bridge.pending_events) == 1
            assert harness.manager.update_job(job_id, {"status": "error"})["action_status"] == "error", "real PG refuses a stale terminal rewrite"
            print("PRIVATE_PG_BODY messaging registered admission: atomic bounded request, complete result, stale terminal refusal")
    except MissingFixtureError:
        print("SKIP supplementary private PostgreSQL absent; provision_test_pg.py --exec supplies the owned fixture")


def main() -> None:
    for verb in DELIVERIES:
        parked_delivery(verb)
    terminal_bridge_outcomes()
    synchronous_list_completeness()
    worker_loss_and_terminal_uncertainty()
    cancelled_successor_keeps_fifo()
    private_job_persistence()
    print("PASS messaging registered admission")


if __name__ == "__main__":
    main()
