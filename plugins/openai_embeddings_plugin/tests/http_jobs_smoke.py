"""Offline HTTP job coverage through the production action executor.

Run: .venv/bin/python plugins/openai_embeddings_plugin/tests/http_jobs_smoke.py
The real providers and AsyncJobManager use transactional in-memory state. Only
HTTP transport and vector storage are synthetic; sockets are forbidden.
"""

from __future__ import annotations

import importlib
import json
import socket
import sys
import threading
import time
import unittest
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
for source in (
    ROOT,
    ROOT / "ananta/src",
    ROOT / "ananta/tests/core/actions",
    ROOT / "quality_gates/tests",
    ROOT / "plugins/openai_embeddings_plugin/src",
    ROOT / "plugins/actr_memory_plugin/src",
    ROOT / "plugins/default_address_book_plugin/src",
    ROOT / "plugins/jira_plugin/src",
    ROOT / "plugins/schwab_market_data_plugin/src",
    ROOT / "plugins/s3_blob_storage_plugin/src",
    ROOT / "plugins/titanv2_embeddings_plugin/src",
    ROOT / "plugins/openai_embeddings_plugin/tests",
):
    sys.path.insert(0, str(source))

import httpx  # noqa: E402
from actr_memory_plugin.backend import ACTRMemoryBackend  # noqa: E402
from actr_memory_plugin.plugin import ACTRMemoryPlugin  # noqa: E402
from ananta.core.actions.action_processor import ActionProcessor  # noqa: E402
from ananta.core.state.async_job_manager import AsyncJobManager  # noqa: E402
from ananta.core.state.flow_runtime_graph import FlowRuntimeGraph, TokenOwnerType  # noqa: E402
from ananta.services.embedding_service import EmbeddingService  # noqa: E402
from ananta.services.job_service.service import JobService  # noqa: E402
from connector_jobs_support import CONNECTOR_CASES, assert_result, configure  # noqa: E402
from default_address_book_plugin.plugin import DefaultAddressBookPlugin  # noqa: E402
from openai_embeddings_plugin.plugin import OpenAIEmbeddingsPlugin  # noqa: E402

from quality_gates.tests.fast_return_census_context import CensusContext  # noqa: E402

JobState = importlib.import_module("async_job_fixture_support").JobState

CONTEXT = {"session_id": "http-fixture-session", "flow_id": "http-fixture-flow"}
ADDRESS: dict[str, Any] = {
    "name": "fixture-service",
    "address_type": "service",
    "description": "synthetic service",
    "entries": [{"field_type": "url", "description": "endpoint", "value": "https://jobs.invalid"}],
}
CASES: tuple[tuple[str, str, dict[str, Any]], ...] = (
    ("memory", "remember_action", {"content": "synthetic memory", "tags": ["fixture"]}),
    ("memory", "recall_action", {"query": "synthetic memory"}),
    ("embeddings", "generate_embeddings_action", {"inputs": ["synthetic text"]}),
    ("embeddings", "get_embedding_dimension_action", {}),
    ("embeddings", "list_models_action", {}),
    ("address", "register", ADDRESS),
)


class Vectors:
    """Storage boundary; embedding generation still uses the real HTTP client."""

    def __init__(self) -> None:
        self.stored: list[dict[str, Any]] = []

    def store_vectors(self, _namespace: str, vectors: list[dict[str, Any]]) -> dict[str, Any]:
        self.stored.extend(vectors)
        return {"action_status": "completed", "data": {"result": {"stored": len(vectors)}}}

    def search_similar(self, **_kwargs: Any) -> dict[str, Any]:
        return {"action_status": "completed", "data": {"result": {"results": []}}}


class State(JobState):
    """Materialize the PostgreSQL defaults at this in-memory storage boundary."""

    def write_state(self, namespace: str, data: dict[str, Any], calling_service: str | None = None, calling_namespace: str | None = None) -> dict[str, Any]:
        timestamp = datetime.now(UTC).isoformat()
        data = {**data, "record": {"created_at": timestamp, "updated_at": timestamp, "is_deleted": 0, **data["record"]}}
        return cast(dict[str, Any], super().write_state(namespace, data, calling_service, calling_namespace))


class Fixture(ExitStack):
    """Real handlers, library callers, backend and job persistence, with no host IO."""

    def __init__(self, *, failed: bool = False) -> None:
        super().__init__()
        self.state = State()
        self.state.rows["flows"] = [{"id": CONTEXT["flow_id"], "status": "active", "trigger_type": "bridge_process_call"}]
        self.manager = AsyncJobManager(cast(Any, self.state), FlowRuntimeGraph(cast(Any, self.state)))
        self.service = JobService(cast(Any, self.state), self.manager)
        self.entered = threading.Event()
        self.release = threading.Event()
        self.touches: list[int] = []
        self.requests: list[httpx.Request] = []
        self.failed = failed
        self.vectors = Vectors()
        self.embeddings = OpenAIEmbeddingsPlugin()
        self.embeddings._initialized = True
        self.embeddings._base_url = "https://jobs.invalid/v1"
        self.embeddings._default_model = "fixture-embedder"
        self.embeddings.qualification_status = {"state": "ready"}
        registry = SimpleNamespace(get_plugin=lambda _name: self.embeddings)
        self.embedding_service = EmbeddingService(cast(Any, registry), self.embeddings.name)
        self.memory = ACTRMemoryPlugin()
        self.memory._backend = ACTRMemoryBackend(self.state, self.vectors, self.embedding_service)
        self.address = DefaultAddressBookPlugin()
        self.address.state_service = cast(Any, self.state)
        self.address._memory_service = self.memory
        for plugin in (self.embeddings, self.memory, self.address):
            plugin.orchestrator_ref = cast(Any, SimpleNamespace(async_job_manager=self.manager, APP_HOME="/synthetic.invalid/profile"))
        original = httpx.Client
        transport = httpx.MockTransport(self.respond)
        self.enter_context(patch.object(httpx, "Client", side_effect=lambda **kwargs: original(transport=transport, **kwargs)))
        for method in ("connect", "connect_ex"):
            self.enter_context(patch.object(socket.socket, method, side_effect=AssertionError("real network forbidden")))
        self.callback(self.release.set)

    def respond(self, request: httpx.Request) -> httpx.Response:
        assert request.url.host == "jobs.invalid", request.url
        assert "authorization" not in request.headers, "No key is needed by this fixture"
        self.touches.append(threading.get_ident())
        self.requests.append(request)
        self.entered.set()
        self.release.wait(0.3)
        if self.failed:
            raise httpx.ConnectError("synthetic connection failure", request=request)
        if request.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": "fixture-embedder", "type": "embedding"}]})
        inputs = json.loads(request.content)["input"]
        return httpx.Response(200, json={"model": "fixture-embedder", "data": [{"index": index, "embedding": [0.25, 0.75]} for index, _text in enumerate(inputs)]})

    def terminal(self, job_id: str) -> dict[str, Any]:
        deadline = time.monotonic() + 4
        pause = threading.Event()
        while time.monotonic() < deadline:
            row = self.service.get_job(job_id)["data"]["result"]["job"]
            if row["status"] in {"completed", "error", "cancelled"}:
                return cast(dict[str, Any], row)
            pause.wait(0.002)
        raise AssertionError(f"job did not finish: {job_id}")


def execute(plugin: Any, verb: str, parameters: dict[str, Any]) -> dict[str, Any]:
    """Production plugin dispatch and result validation, with routing state supplied."""
    plugin.get_available_actions()
    processor = ActionProcessor.__new__(ActionProcessor)
    processor.plugin_manager = cast(Any, SimpleNamespace(get_plugin=lambda _name: plugin))
    action_id = "http-fixture-action"
    process_key = f"plugin::{plugin.name}::{verb}"
    graph = plugin.orchestrator_ref.async_job_manager._flow_runtime_graph
    flow_token_id = graph.create_token(CONTEXT["flow_id"], TokenOwnerType.PROCESS, action_id, process_key)
    action = SimpleNamespace(id=action_id, process_key=process_key, context_id=None, flow_token_id=flow_token_id, **CONTEXT)
    with ExitStack() as stack:
        stack.enter_context(patch.object(processor, "_prepare_arguments", return_value=parameters))
        stack.enter_context(patch.object(processor, "_resolve_placeholders", side_effect=lambda arguments, _action: arguments))
        for method in ("_store_result_in_context", "_setup_plugin_context", "_lift_inference_vertex_identity", "_build_call_context"):
            stack.enter_context(patch.object(processor, method, return_value=None))
        return cast(dict[str, Any], processor.execute_action(cast(Any, action)))


class HTTPJobs(unittest.TestCase):
    def test_prompt_receipts(self) -> None:
        for provider, verb, parameters in CASES:
            with self.subTest(verb=verb), Fixture() as fixture:
                caller = threading.get_ident()
                started = time.perf_counter()
                receipt = execute(getattr(fixture, provider), verb, parameters)
                milliseconds = (time.perf_counter() - started) * 1000
                self.assertLess(milliseconds, 100, f"{verb} ran HTTP inline: {milliseconds:.1f}ms; touches={fixture.touches}")
                self.assertEqual(receipt["action_status"], "completed", receipt)
                self.assertEqual(receipt["data"]["status"], "queued", receipt)
                job_id = receipt["data"]["job_id"]
                self.assertTrue(fixture.entered.wait(1), "worker never reached HTTP")
                self.assertNotIn(caller, fixture.touches)
                self.assertEqual(fixture.service.get_job(job_id)["data"]["result"]["job"]["status"], "processing")
                fixture.release.set()
                row = fixture.terminal(job_id)
                self.assertEqual(row["status"], "completed", row)
                self.assertEqual(row["result"]["action_status"], "completed", row)
                self.assertEqual(len(fixture.state.rows["job"]), 1)
                self.assertEqual(len(fixture.requests), 1)
                self.assert_result(verb, row["result"]["data"])
                if verb == "remember_action":
                    self.assertEqual(fixture.state.rows["memory"][0]["session_id"], CONTEXT["session_id"])
                print(f"PASS {verb}: {milliseconds:.3f}ms; job={job_id}; worker_http=1; caller_http=0; terminal_readback=true")

    def assert_result(self, verb: str, data: dict[str, Any]) -> None:
        if verb == "remember_action":
            self.assertTrue(data["memory_id"])
        elif verb == "recall_action":
            self.assertEqual(data, {"memories": [], "count": 0})
        elif verb == "generate_embeddings_action":
            self.assertEqual(data["result"]["embeddings"], [[0.25, 0.75]])
        elif verb == "get_embedding_dimension_action":
            self.assertEqual(data["result"]["dimension"], 2)
        elif verb == "list_models_action":
            self.assertEqual(data["result"]["models"][0]["name"], "fixture-embedder")
        else:
            self.assertTrue(data["address_id"])
            self.assertTrue(data["memory_id"])

    def test_http_errors_are_terminal_and_preserved(self) -> None:
        for provider, verb, parameters in CASES:
            with self.subTest(verb=verb), Fixture(failed=True) as fixture:
                receipt = execute(getattr(fixture, provider), verb, parameters)
                fixture.release.set()
                row = fixture.terminal(receipt["data"]["job_id"])
                if verb == "register":
                    # Existing address-book policy keeps registration when optional ingest fails.
                    self.assertEqual(row["status"], "completed", row)
                    self.assertIsNone(row["result"]["data"]["memory_id"])
                else:
                    self.assertEqual(row["status"], "error", row)
                    error = row["result"]["error"]
                    self.assertEqual(row["error"], error if isinstance(error, dict) else row["result"])
                    self.assertIn("synthetic connection failure", str(row["error"]))
                self.assertEqual(len(fixture.requests), 1)

    def test_library_callers_keep_synchronous_results(self) -> None:
        with Fixture() as fixture:
            fixture.release.set()
            generated = cast(dict[str, Any], fixture.embedding_service.generate_embeddings(["library"]))
            dimension = cast(dict[str, Any], fixture.embedding_service.get_embedding_dimension())
            models = cast(dict[str, Any], fixture.embedding_service.list_models())
            self.assertEqual(generated["data"]["result"]["embeddings"], [[0.25, 0.75]])
            self.assertEqual(dimension["data"]["result"]["dimension"], 2)
            self.assertEqual(models["data"]["result"]["models"][0]["name"], "fixture-embedder")
            self.assertTrue(fixture.memory.remember("library")["memory_id"])
            self.assertEqual(fixture.memory.recall("library")["count"], 0)
            registered = cast(dict[str, Any], fixture.address.register(**ADDRESS))
            self.assertTrue(registered["data"]["memory_id"])
            self.assertEqual(fixture.touches, [threading.get_ident()] * 6)
            self.assertFalse(fixture.state.rows.get("job"))

    def test_async_metadata(self) -> None:
        with Fixture() as fixture:
            for provider, verb, _parameters in CASES:
                metadata = next(action for action in getattr(fixture, provider).get_available_actions() if action.name == verb)
                self.assertTrue(metadata.is_async, verb)
                self.assertEqual(set(metadata.return_value_schema.properties), {"job_id", "status"})

    def test_census_setup_uses_library(self) -> None:
        seeded: list[dict[str, Any]] = []

        def register(**parameters: Any) -> dict[str, Any]:
            seeded.append(parameters)
            return {"action_status": "completed"}

        def handler(*_args: Any) -> None:
            raise AssertionError("census setup must use synchronous register library, never register_action")

        context = CensusContext.__new__(CensusContext)
        context.services = {"address_book_service": SimpleNamespace(register=register, register_action=handler)}
        context.config = cast(Any, SimpleNamespace(host="postgres.invalid", port=1234, user="fixture", database="fixture"))
        context.schema = "fixture"
        context._seed_addresses()
        self.assertEqual([entry["name"] for entry in seeded], ["pgvector_service_db", "openai_embeddings"])

    def test_serial_cancel_and_restart(self) -> None:
        for provider in ("memory", "embeddings", "address"):
            with self.subTest(provider=provider):
                self.assert_serial_worker(provider)

    def assert_serial_worker(self, provider: str) -> None:
        with Fixture() as fixture:
            plugin = getattr(fixture, provider)
            entered, release = threading.Event(), threading.Event()
            fixture.callback(release.set)
            calls: list[int] = []

            def operation(parameters: dict[str, Any]) -> dict[str, Any]:
                calls.append(parameters["number"])
                if parameters["number"] == 1:
                    entered.set()
                    assert release.wait(2), "serial test must release its first job"
                return {"action_status": "completed", "data": parameters}

            def submit(number: int) -> dict[str, Any]:
                return cast(dict[str, Any], plugin._jobs.submit(plugin, "fixture", {"number": number}, CONTEXT, operation))

            first = submit(1)
            self.assertTrue(entered.wait(1))
            second = submit(2)
            cancelled = submit(3)
            fixture.manager.update_job(cancelled["data"]["job_id"], {"status": "cancelled"})
            self.assertEqual(calls, [1])
            release.set()
            fixture.terminal(first["data"]["job_id"])
            fixture.terminal(second["data"]["job_id"])
            worker = fixture.manager._owners[plugin.name] if provider == "memory" else plugin._jobs
            deadline = time.monotonic() + 2
            while worker._thread is not None and time.monotonic() < deadline:
                threading.Event().wait(0.002)
            self.assertIsNone(worker._thread)
            self.assertEqual(calls, [1, 2])
            fixture.terminal(submit(4)["data"]["job_id"])
            self.assertEqual(calls, [1, 2, 4])


class ConnectorJobs(unittest.TestCase):
    def test_prompt_receipts_and_original_results(self) -> None:
        for name, verb, parameters in CONNECTOR_CASES:
            if not (ROOT / "plugins" / name / "src").is_dir():
                print(f"SKIP {name}: plugin absent from this profile")
                continue
            with self.subTest(plugin=name, verb=verb), Fixture() as fixture:
                plugin, parameters = configure(fixture, name, verb, parameters)
                metadata = next(action for action in plugin.get_available_actions() if action.name == verb)
                self.assertTrue(metadata.is_async, verb)
                self.assertEqual(set(metadata.return_value_schema.properties), {"job_id", "status"})
                caller = threading.get_ident()
                started = time.perf_counter()
                receipt = execute(plugin, verb, parameters)
                milliseconds = (time.perf_counter() - started) * 1000
                self.assertLess(milliseconds, 100, f"{name}.{verb} blocked dispatch: {milliseconds:.1f}ms; touches={fixture.touches}; receipt={receipt}")
                self.assertEqual(receipt["action_status"], "completed", receipt)
                self.assertEqual(receipt["data"]["status"], "queued", receipt)
                job_id = receipt["data"]["job_id"]
                request = cast(dict[str, Any], fixture.manager.get_job_payload(job_id, "request"))
                json.dumps(request["data"]["payload"])
                self.assertTrue(fixture.entered.wait(3), "worker never reached its external boundary")
                self.assertNotIn(caller, fixture.touches)
                self.assertEqual(fixture.service.get_job(job_id)["data"]["result"]["job"]["status"], "processing")
                fixture.release.set()
                row = fixture.terminal(job_id)
                self.assertEqual(row["status"], "completed", row)
                self.assertEqual(row["result"]["action_status"], "completed", row)
                assert_result(name, verb, row["result"]["data"])
                self.assertEqual(len(fixture.state.rows["job"]), 1)
                print(f"PASS {name}.{verb}: {milliseconds:.3f}ms; caller_io=0; original_job_result=true")

    def test_errors_live_in_job_error(self) -> None:
        for name, verb, parameters in CONNECTOR_CASES:
            if not (ROOT / "plugins" / name / "src").is_dir():
                print(f"SKIP {name}: plugin absent from this profile")
                continue
            with self.subTest(plugin=name, verb=verb), Fixture(failed=True) as fixture:
                plugin, parameters = configure(fixture, name, verb, parameters)
                receipt = execute(plugin, verb, parameters)
                fixture.release.set()
                self.assertEqual(receipt["action_status"], "completed", receipt)
                row = fixture.terminal(receipt["data"]["job_id"])
                self.assertEqual(row["status"], "error", row)
                self.assertTrue(row["error"], row)
                error = row["result"]["error"]
                self.assertEqual(row["error"], error if isinstance(error, dict) else row["result"])


if __name__ == "__main__":
    unittest.main()
