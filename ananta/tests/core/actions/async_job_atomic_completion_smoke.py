#!/usr/bin/env python3
"""Atomic job persistence, public service adapters, and completion race proof."""

from __future__ import annotations

import json
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "ananta" / "src"))
sys.path.insert(0, str(ROOT))
for plugin_name in ("postgres_state_management_plugin", "agent_messaging_plugin"):
    sys.path.insert(0, str(ROOT / "plugins" / plugin_name / "src"))

from ananta.core.actions.action_metadata import ParameterMetadata, ParameterType, ReturnValueSchema  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.actions.action_queue_poller import ActionQueuePoller, QueuedAction  # noqa: E402
from ananta.core.orchestration.managers.flow_manager import FlowManager  # noqa: E402
from ananta.core.plugins.plugin_manager import PluginManager  # noqa: E402
from ananta.core.process_registry.invocation_schema_generator import InvocationSchemaGenerator  # noqa: E402
from ananta.core.process_registry.service_interface_scanner import ServiceInterfaceScanner  # noqa: E402
from ananta.core.services.service_interface_decorator import service_interface_process  # noqa: E402
from ananta.core.state.async_job_manager import AsyncJobManager, registered_action_context  # noqa: E402
from ananta.core.state.async_job_persistence import create_job_and_request, update_job_and_payloads  # noqa: E402
from ananta.core.state.execution_token_context import action_execution_context, get_current_flow_token_id  # noqa: E402
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph, TokenOwnerType, TokenState  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402
from async_job_fixture_support import JobState  # noqa: E402


def joined_job_routes() -> None:
    """A sibling terminal write cannot resolve a token while routing is parked."""
    for first_status in ("completed", "error"):
        state, manager, token_id, completed = manager_fixture()
        unrelated = create(manager, token_id)
        with registered_action_context("action", "flow", "s"):
            first_id, second_id = create(manager, token_id), create(manager, token_id)
        assert_registered_lineage(state, first_id, second_id)
        routing, release = threading.Event(), threading.Event()

        def route(job_id: str, _status: str, _result: object, _error: object, *, target_job: str = first_id, reached: threading.Event = routing, resume: threading.Event = release) -> bool:
            if job_id == target_job:
                reached.set()
                assert resume.wait(5)
            return False

        manager._route_completion_to_role = route
        with ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(manager.update_job, first_id, {"status": first_status})
            try:
                assert routing.wait(5)
                assert manager.update_job(second_id, {"status": "completed"})["action_status"] == "completed"
                assert completed == [], "sibling persistence cannot overtake original completion routing"
            finally:
                release.set()
            assert future.result(timeout=5)["action_status"] == "completed"
        assert completed == ["flow"]
        assert state.rows["flow_tokens"][0]["state"] == ("completed" if first_status == "completed" else "failed")
        assert next(job for job in state.rows["job"] if job["id"] == unrelated)["status"] == "queued", "unrelated queued work must not hold this owner's token"
    print("PASS linked_job_join, sibling_terminal_during_routing, retained_failure, one_token_resolution")


def assert_registered_lineage(state: JobState, root_id: str, child_id: str) -> None:
    root = next(job for job in state.rows["job"] if job["id"] == root_id)
    child = next(job for job in state.rows["job"] if job["id"] == child_id)
    assert root["group_id"] == child["group_id"] == "registered:action"
    assert json.loads(root["metadata"])["registered_action_id"] == "action"
    assert json.loads(child["metadata"])["registered_root_job_id"] == root_id
    assert "registered_action_id" not in json.loads(child["metadata"]), "a child cannot duplicate the original action route"


@dataclass
class FixtureAction(QueuedAction):
    source_plugin: str | None = None


class Service:
    def __init__(self, manager: AsyncJobManager) -> None:
        self.manager = manager
        self.captured: dict[str, Any] = {}

    def rebuild(self) -> dict[str, object]:
        return {"count": 23}

    @service_interface_process(
        name="rebuild",
        provider="fixture_service",
        is_async=True,
        parameters={"state": ParameterMetadata(type=ParameterType.OBJECT, required=False)},
        return_value_schema=ReturnValueSchema(type=ParameterType.OBJECT, description="job receipt"),
    )
    def submit_rebuild(self, state: dict[str, Any]) -> dict[str, object]:
        assert state.get("session_id") and state.get("flow_id"), "server context required"
        self.captured = state
        assert get_current_flow_token_id() == "token-fixture", "service adapter captures execution token"
        return self.manager.create_job("fixture", "rebuild", {"notes": "fixture"}, job_metadata=state)


def service_adapter() -> None:
    state = JobState()
    service = Service(AsyncJobManager(cast(Any, state)))
    metadata = cast(Any, Service.submit_rebuild)._service_interface_metadata
    generator = InvocationSchemaGenerator()
    entry = ServiceInterfaceScanner(generator)._build_base_process_entry("service_interface::fixture_service::rebuild", metadata, metadata.to_process_dict()["parameters"])  # wint:negative-fixture
    assert entry["is_async"] is True
    assert generator.generate_action_blueprint_from_metadata("service_interface::fixture_service::rebuild", metadata)["metadata"]["is_async"] is True  # type: ignore[index]  # wint:negative-fixture
    process = metadata.to_process_dict()
    queued = FixtureAction("a", "service_interface::fixture_service::rebuild", "{}", "fixture", "now", session_id="s", flow_id="f", flow_token_id="token-fixture")  # wint:negative-fixture
    processor = ActionProcessor(PluginManager())
    receipt = processor._execute_standard_service_method(service, "fixture_service", "rebuild", process, {"state": {"flow_id": "spoof"}}, queued)
    assert receipt["action_status"] == "completed" and service.rebuild() == {"count": 23}
    assert service.captured["flow_id"] == "f" and state.rows["job"][0]["flow_token_id"] == "token-fixture"
    assert get_current_flow_token_id() is None
    for target in (None, "missing_method", "captured"):
        try:
            processor._execute_standard_service_method(service, "fixture_service", "rebuild", {**process, "function_name": target}, {}, queued)
        except FrameworkError:
            pass
        else:
            raise AssertionError("missing/noncallable registered target must fail")
    print("PASS public_adapter_target, direct_python_result, server_context_and_token, async_metadata")


def manager_fixture() -> tuple[JobState, AsyncJobManager, str, list[str]]:
    state = JobState()
    state.rows["flows"] = [{"id": "flow", "status": "active", "trigger_type": "bridge_process_call"}]
    graph = FlowRuntimeGraph(cast(Any, state))
    completed: list[str] = []
    graph.register_completion_callback(completed.append)
    token = graph.create_token("flow", TokenOwnerType.PROCESS, "action")
    return state, AsyncJobManager(cast(Any, state), graph), token, completed


def create(manager: AsyncJobManager, token: str) -> str:
    with action_execution_context(token):
        receipt: Any = manager.create_job("fixture", "operation", {"notes": "atomic fixture"}, job_id="external-not-canonical", job_metadata={"flow_id": "flow", "session_id": "s"})
    return str(receipt["data"]["job_id"])


def failures() -> None:
    for failed_write in ("job", "job_payload", "commit"):
        state, manager, token, completed = manager_fixture()
        state.fail_write = failed_write
        state.fail_commit = failed_write == "commit"
        receipt = manager.create_job("fixture", "operation", {"notes": "failure"})
        assert receipt["action_status"] == "error" and not state.rows.get("job") and not state.rows.get("job_payload")
        state.fail_write = ""
        state.fail_commit = False
        job_id = create(manager, token)
        assert job_id != "external-not-canonical"
        state.fail_write = "job_payload" if failed_write == "job_payload" else ""
        state.fail_update = "job" if failed_write == "job" else ""
        state.fail_commit = failed_write == "commit"
        error = manager.update_job(job_id, {"status": "completed", "result": {"count": 9}})
        assert error["action_status"] == "error" and state.rows["job"][0]["status"] == "queued"
        assert len(state.rows["job_payload"]) == 1 and completed == []
    print("PASS atomic_request_result_update_commit_failures, canonical_job_id")


def races() -> None:
    for worker_first in (True, False):
        state, manager, token, completed = manager_fixture()
        graph = manager._flow_runtime_graph
        assert graph is not None
        job_id = create(manager, token)
        if not worker_first:
            graph.update_token_state(token, TokenState.WAITING_JOB)
        outcome = manager.update_job(job_id, {"status": "error", "error": {"stage": "host"}})
        assert outcome["action_status"] == "completed"
        graph.update_token_state(token, TokenState.WAITING_JOB)
        graph.complete_token(token, success=True)
        assert state.rows["flow_tokens"][0]["state"] == "failed" and completed == ["flow"]
        second = manager.update_job(job_id, {"status": "completed", "result": {"wrong": True}})
        assert second["action_status"] == "error" and len(state.rows["job_payload"]) == 2
    print("PASS worker_before_poller, poller_before_worker, one_terminal_outcome_and_resolution")


def commit_before_resolution() -> None:
    for status in ("completed", "error", "cancelled"):
        state, manager, token, completed = manager_fixture()
        job_id = create(manager, token)
        poller: Any = ActionQueuePoller.__new__(ActionQueuePoller)
        poller.state_service = state
        poller._flow_runtime_graph = manager._flow_runtime_graph
        poller._retrieve_action_details = lambda _id, **_kwargs: ("plugin::fixture::call", "notes", None, None, "s", "flow", None, {}, None, None, None)  # wint:negative-fixture
        for name in ("_update_action_status_to_completed", "_store_action_result", "_record_work_products_after_success"):
            setattr(poller, name, lambda *_args: None)
        poller._extract_and_store_attachments = lambda *_args: []
        poller._submit_returned_actions = lambda *_args: True
        poller._maybe_dispatch_result_processing = lambda **_kwargs: None

        def before_routing(*_args: object, poller: Any = poller, token: str = token, completed: list[str] = completed, state: JobState = state) -> bool:
            poller._mark_action_completed("action", {"action_status": "completed"}, token)
            assert completed == [], "poller must not resolve during worker completion routing"
            assert state.rows["flow_tokens"][0]["state"] == "waiting_job"
            return False

        manager._route_completion_to_role = before_routing
        assert manager.update_job(job_id, {"status": status})["action_status"] == "completed"
        assert completed == ["flow"]
        assert state.rows["flow_tokens"][0]["state"] == ("completed" if status == "completed" else "failed")
        poller._mark_action_completed("action", {"action_status": "completed"}, token)
        assert completed == ["flow"]
    print("PASS terminal_commit_before_token_resolution, real_poller_preserves_worker_outcome")


def delivery_order() -> None:
    state, manager, token, completed = manager_fixture()
    job_id = create(manager, token)
    metadata = {"flow_id": "flow", "session_id": "s", "completion_handlers": {"result": {"process_key": "plugin::fixture::consume", "template": {}}}}  # wint:negative-fixture
    state.rows["job"][0]["metadata"] = json.dumps(metadata)
    calls: list[str] = []

    class Factory:
        def submit_action_definition(self, _definition: object, **_kwargs: object) -> None:
            assert state.rows["job"][0]["status"] == "completed"
            assert json.loads(state.rows["job_payload"][-1]["payload_data"]) == {"count": 9}
            calls.append("committed before continuation")
            raise RuntimeError("injected delivery failure")

    manager.set_action_factory(cast(Any, Factory()))
    assert calls == []
    result: Any = manager.update_job(job_id, {"status": "completed", "result": {"count": 9}})
    assert result["action_status"] == "error" and result["data"]["outcome_committed"] is True
    assert calls == ["committed before continuation"] and completed == ["flow"]
    assert json.loads(state.rows["job"][0]["metadata"])["completion_reach"] == "bridge_dispatch_no_return_path"
    assert manager.get_job_payload(job_id, "result")["action_status"] == "completed"
    print("PASS deterministic_consumer_after_commit, delivery_failure_drainable_and_token_resolved")


def sequences() -> None:
    manager = FlowManager(cast(Any, JobState()))
    with ThreadPoolExecutor(max_workers=12) as executor:
        values = list(executor.map(lambda _: manager.get_next_sequence_in_flow("flow"), range(1200)))
    assert sorted(values) == list(range(1, 1201))
    print("PASS concurrent_same_flow_sequence_allocation")


def _assert_standard_record(record: dict[str, Any]) -> None:
    assert record["namespace"] == "core"
    assert record["name"] == record["id"]
    assert record["created_by"] == "ananta.services.state_service"
    assert record["updated_by"] == "ananta.services.state_service"


def record_preparation() -> None:
    state = JobState()
    supplied: dict[str, object] = {
        "id": "job-explicit",
        "name": "named",
        "created_by": "creator",
        "updated_by": "editor",
    }
    prepared = state.prepare_record("core", "job", supplied)
    assert prepared == {**supplied, "namespace": "core"}
    assert "namespace" not in supplied and prepared is not supplied
    for field in ("namespace", "name", "created_by", "updated_by"):
        missing = {k: v for k, v in prepared.items() if k != field}
        try:
            with state.transactional() as transaction:
                transaction.write_state("core", {"table": "job", "record": missing})
        except AssertionError as error:
            assert field in str(error)
        else:
            raise AssertionError(f"missing prepared field {field} must fail")
    assert not state.rows
    print("PASS full_record_preparation_and_preservation, missing_standard_fields_refused")


def transactional_ids() -> None:
    """A real transaction requires ids before insert; fakes must not mint them."""
    state = JobState()
    record: dict[str, object] = {
        "status": "queued",
        "external_id": "caller-id",
        "provider_type": "plugin",
        "provider_name": "fixture",
        "description": "standard-field fixture",
        "notes": "required business columns",
    }
    job_id = create_job_and_request(cast(Any, state), record, {"input": 1})
    assert job_id.startswith("job-") and len(job_id) == 17
    assert "id" not in record and "namespace" not in record, "do not mutate caller data"
    _assert_standard_record(state.rows["job"][0])
    assert state.rows["job"][0]["external_id"] == "caller-id"
    request = state.rows["job_payload"][0]
    assert request["id"].startswith("jpl-") and len(request["id"]) == 17
    assert request["job_id"] == job_id and request["sequence"] == 1
    _assert_standard_record(request)
    update_job_and_payloads(cast(Any, state), job_id, {"status": "completed"}, {"result": 2}, None)
    outcome = state.rows["job_payload"][1]
    assert outcome["id"].startswith("jpl-") and outcome["id"] != request["id"]
    assert outcome["job_id"] == job_id and outcome["sequence"] == 2
    _assert_standard_record(outcome)
    assert json.loads(outcome["payload_data"]) == {"result": 2}
    print("PASS explicit_canonical_job_request_and_outcome_ids, caller_record_unchanged")


def main() -> None:
    joined_job_routes()
    record_preparation()
    transactional_ids()
    service_adapter()
    failures()
    races()
    commit_before_resolution()
    delivery_order()
    sequences()


if __name__ == "__main__":
    main()
