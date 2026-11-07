"""Exercise F5's real async entry points against slow synchronous backends."""

from __future__ import annotations

import asyncio
import importlib
import inspect
import json
import logging
import os
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Awaitable, Callable
from contextlib import ExitStack, nullcontext
from contextvars import ContextVar
from functools import partial
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock, patch

ROOT = Path(__file__).resolve().parents[3]
CLASSES = {
    "actr_memory_plugin": "ACTRMemoryPlugin",
    "agent_messaging_plugin": "AgentMessagingPlugin",
    "claude_code_cloud_session_source_plugin": "ClaudeCodeCloudSessionSourcePlugin",
    "codex_cloud_session_source_plugin": "CodexCloudSessionSourcePlugin",
    "comfyui_image_generation_plugin": "ComfyUIImageGenerationPlugin",
    "coreai_embeddings_plugin": "CoreAIEmbeddingsPlugin",
    "cosyvoice2_tts_plugin": "CosyVoice2TTSPlugin",
    "default_inference_plugin": "Plugin",
    "default_scheduling_plugin": "SchedulingPlugin",
    "discord_plugin": "DiscordPlugin",
    "g_suite_plugin": "GSuitePlugin",
    "marketo_plugin": "MarketoPlugin",
    "pgvector_service_plugin": "PGVectorServicePlugin",
    "postgres_state_management_plugin": "PostgresStatePlugin",
    "rds_pgvector_service_plugin": "RDSPGVectorServicePlugin",
    "rds_postgres_state_management_plugin": "RdsPostgresStateManagementPlugin",
    "seed_factory_plugin": "SeedFactoryPlugin",
    "session_bridge_plugin": "SessionBridgePlugin",
    "signal_plugin": "SignalPlugin",
}
for _name in CLASSES:
    sys.path.insert(0, str(ROOT / "plugins" / _name / "src"))
sys.path.insert(0, str(ROOT / "ananta" / "src"))


def load(name: str) -> tuple[ModuleType, Any]:
    module = importlib.import_module(name + ".plugin")
    # An editable shared .venv must never turn this into a test of other bytes.
    assert Path(str(module.__file__)).resolve().is_relative_to(ROOT)
    cls = getattr(module, CLASSES[name])
    plugin = cls.__new__(cls)
    plugin.__dict__.update(
        name=name,
        logger=logging.getLogger("f5-test"),
        _services_started=False,
        _service_started_at=None,
        _service_error=None,
    )
    plugin.is_active_interface_provider = Mock(return_value=False)
    plugin.set_ready = Mock()
    return module, plugin


class SlowBackend:
    def __init__(self, failure: bool, value: Any = None) -> None:
        self.failure = failure
        self.value = value
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
        self.thread_ids: list[int] = []
        self.delay = 0.3

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        self.calls.append((args, kwargs))
        self.thread_ids.append(threading.get_ident())
        time.sleep(self.delay)
        if self.failure:
            raise OSError("slow backend failed")
        return self.value


class JoiningThread:
    """A joinable active worker with a deliberately slow final operation."""

    def __init__(self, backend: SlowBackend) -> None:
        self.backend = backend
        self.alive = True

    def is_alive(self) -> bool:
        return self.alive

    def start(self) -> None:
        self.backend()
        self.alive = True

    def join(self, timeout: float | None = None) -> None:
        self.backend(timeout=timeout)
        self.alive = False


LIFECYCLE_NAMES = tuple(name for name in CLASSES if name not in {"actr_memory_plugin", "default_inference_plugin"})


def configure_agent(plugin: Any, backend: SlowBackend, stack: ExitStack) -> None:
    plugin._launch_jobs = SimpleNamespace(start=backend, stop=Mock())
    plugin._start_choreography_worker = Mock()
    plugin._stop_choreography_worker = Mock()
    hosts = importlib.import_module(plugin.name + ".session_hosts")
    stack.enter_context(patch.object(hosts, "shutdown_all_drivers", backend))


def configure_cloud(plugin: Any, backend: SlowBackend, stack: ExitStack) -> None:
    plugin._backfill = SimpleNamespace(start=backend, stop=backend)
    backend.value = True


def configure_coreai(plugin: Any, backend: SlowBackend, stack: ExitStack) -> None:
    plugin._transition = nullcontext
    plugin._close = backend
    plugin.set_error = Mock()


def configure_cosyvoice(plugin: Any, backend: SlowBackend, stack: ExitStack) -> None:
    plugin._load_config = backend
    backend.value = {"enabled": False}


def configure_scheduler(plugin: Any, backend: SlowBackend, stack: ExitStack) -> None:
    plugin._configure_scheduler = backend
    plugin._register_scheduler_listeners = Mock()
    plugin._start_scheduler_when_ready = Mock()
    plugin._scheduler_manager = SimpleNamespace(stop=backend)


def configure_discord(plugin: Any, backend: SlowBackend, stack: ExitStack) -> None:
    plugin._discord_bot_client = SimpleNamespace(shutdown=backend)


def configure_gsuite(plugin: Any, backend: SlowBackend, stack: ExitStack) -> None:
    plugin._deferred = SimpleNamespace(stop_event=plugin._stop_event, thread=plugin._worker_thread)


def configure_vector(plugin: Any, backend: SlowBackend, stack: ExitStack) -> None:
    folder = Path(stack.enter_context(tempfile.TemporaryDirectory()))
    config = folder / "config" / "plugins" / (plugin.name + ".json")
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({}), encoding="utf-8")
    stack.enter_context(patch.dict(os.environ, {"APP_HOME": str(folder)}))
    provider = plugin._provider

    def initialize(config: dict[str, Any]) -> None:
        backend(config)
        plugin._provider = provider

    plugin.initialize = initialize


def configure_seed(plugin: Any, backend: SlowBackend, stack: ExitStack) -> None:
    plugin._release_worker = SimpleNamespace(start=backend, stop=backend)
    backend.value = True


CONFIGURERS: dict[str, Callable[[Any, SlowBackend, ExitStack], None]] = {
    "agent_messaging_plugin": configure_agent,
    "claude_code_cloud_session_source_plugin": configure_cloud,
    "codex_cloud_session_source_plugin": configure_cloud,
    "coreai_embeddings_plugin": configure_coreai,
    "cosyvoice2_tts_plugin": configure_cosyvoice,
    "default_scheduling_plugin": configure_scheduler,
    "discord_plugin": configure_discord,
    "g_suite_plugin": configure_gsuite,
    "pgvector_service_plugin": configure_vector,
    "rds_pgvector_service_plugin": configure_vector,
    "seed_factory_plugin": configure_seed,
}
IMMEDIATE_STARTS = {"discord_plugin", "signal_plugin"}
JOINING_PLUGINS = {
    "comfyui_image_generation_plugin",
    "cosyvoice2_tts_plugin",
    "g_suite_plugin",
    "marketo_plugin",
    "session_bridge_plugin",
    "signal_plugin",
}
ENVELOPED_FAILURES = {
    ("cosyvoice2_tts_plugin", "start_services"),
    ("cosyvoice2_tts_plugin", "stop_services"),
    ("default_scheduling_plugin", "start_services"),
    ("default_scheduling_plugin", "stop_services"),
    ("pgvector_service_plugin", "start_services"),
    ("rds_pgvector_service_plugin", "start_services"),
    ("postgres_state_management_plugin", "stop_services"),
    ("rds_postgres_state_management_plugin", "stop_services"),
}
# ServicePlugin's existing lifecycle decorator converts uncaught backend
# failures into the existing routing envelope, rather than raising to its caller.
ENVELOPED_FAILURES.update(
    {
        (name, method)
        for name in {
            "claude_code_cloud_session_source_plugin",
            "codex_cloud_session_source_plugin",
            "comfyui_image_generation_plugin",
            "discord_plugin",
            "g_suite_plugin",
            "marketo_plugin",
            "seed_factory_plugin",
            "session_bridge_plugin",
            "signal_plugin",
        }
        for method in ("start_services", "stop_services")
        if not (method == "start_services" and name in IMMEDIATE_STARTS)
    }
)
LOGGED_FAILURES = {("pgvector_service_plugin", "stop_services"), ("rds_pgvector_service_plugin", "stop_services")}


def lifecycle_case(
    name: str,
    method: str,
    backend: SlowBackend,
    stack: ExitStack,
) -> Any:
    module, plugin = load(name)
    plugin._services_started = method == "stop_services"
    plugin._stop_event = threading.Event()
    plugin._worker_thread = JoiningThread(backend)
    plugin._polling_thread = plugin._worker_thread
    plugin._job_thread = plugin._worker_thread
    plugin._worker_loop = Mock()
    plugin._job_processing_loop = Mock()
    plugin._service_action_result = lambda data: {
        "action_status": "completed",
        "data": data,
        "actions": [],
        "error": None,
        "timestamp": "test",
    }
    provider = SimpleNamespace(close=backend, bootstrap_for_lifecycle=backend)
    plugin._provider = provider
    plugin._get_provider = Mock(return_value=provider)
    plugin._backend = provider

    configure = CONFIGURERS.get(name)
    if configure is not None:
        configure(plugin, backend, stack)

    # Starts with no heavyweight body still participate in the same serialized
    # lane as stop. Slow thread creation verifies that boundary too.
    if method == "start_services" and name in {
        "comfyui_image_generation_plugin",
        "g_suite_plugin",
        "marketo_plugin",
        "session_bridge_plugin",
    }:
        stack.enter_context(patch.object(module, "threading", SimpleNamespace(Thread=Mock(return_value=plugin._worker_thread))))
    return plugin


class LoopResponsivenessTest(unittest.IsolatedAsyncioTestCase):
    async def measure(self, operation: Callable[[], Awaitable[Any]]) -> tuple[Any, Exception | None]:
        gaps: list[float] = []
        finished = False

        async def ticker() -> None:
            while not finished:
                before = asyncio.get_running_loop().time()
                await asyncio.sleep(0.005)
                gaps.append(asyncio.get_running_loop().time() - before)

        ticking = asyncio.create_task(ticker())
        await asyncio.sleep(0.01)
        value: Any = None
        error: Exception | None = None
        try:
            value = await operation()
        except Exception as exc:
            error = exc
        finally:
            await asyncio.sleep(0.01)
            finished = True
            await ticking
        maximum = max(gaps)
        print(f"TICKER max_gap={maximum:.6f}s", flush=True)
        self.assertLess(maximum, 0.1, "slow backend blocked its caller's event loop")
        return value, error

    async def test_memory_facades(self) -> None:
        module, plugin = load("actr_memory_plugin")
        cls = getattr(module, CLASSES["actr_memory_plugin"])
        methods = [name for name in vars(cls) if name.endswith("_async")]
        self.assertEqual(len(methods), 31)
        for method in methods:
            arguments = {
                key: {"ids": ["memory-1"], "compacted_event_count": 2, "params": {"parameters": {}}, "state": {"session_id": "session", "flow_id": "flow"}}.get(key, f"argument-{key}") for key, parameter in inspect.signature(getattr(plugin, method)).parameters.items() if parameter.default is inspect.Parameter.empty
            }
            for failure in (False, True):
                with self.subTest(method=method, failure=failure):
                    token = {"action_status": "completed", "data": {"method": method}, "error": None}
                    backend = SlowBackend(failure, token)
                    setattr(plugin, method.removesuffix("_async"), backend)
                    value, error = await self.measure(partial(getattr(plugin, method), **arguments))
                    self.assertEqual(len(backend.calls), 1)
                    self.assertNotEqual(backend.thread_ids, [threading.get_ident()])
                    if failure:
                        self.assertIsInstance(error, OSError)
                    else:
                        self.assertIs(value, token)
                        self.assertIsNone(error)
                    for key, argument in arguments.items():
                        self.assertEqual(backend.calls[0][1][key], argument)

    async def test_inference_facade(self) -> None:
        _, plugin = load("default_inference_plugin")
        for failure in (False, True):
            with self.subTest(failure=failure):
                token = {"action_status": "completed", "data": {"response": "answer"}, "error": None}
                backend = SlowBackend(failure, token)
                plugin._process_inference_request = backend
                params, state = {"prompt": "question"}, {"session_id": "session"}
                value, error = await self.measure(partial(plugin.execute_async, params, state))
                self.assertEqual(backend.calls, [((params, state), {})])
                if failure:
                    self.assertIsInstance(error, OSError)
                else:
                    self.assertIs(value, token)
                    self.assertIsNone(error)

    async def test_lifecycle_entries(self) -> None:
        for name in LIFECYCLE_NAMES:
            methods = ("cleanup",) if name == "coreai_embeddings_plugin" else ("start_services", "stop_services")
            if name in {"postgres_state_management_plugin", "rds_postgres_state_management_plugin"}:
                methods += ("_cleanup",)
            for method in methods:
                for failure in (False, True):
                    with self.subTest(plugin=name, method=method, failure=failure), ExitStack() as stack:
                        backend = SlowBackend(failure)
                        plugin = lifecycle_case(name, method, backend, stack)
                        value, error = await self.measure(getattr(plugin, method))
                        self.check_lifecycle_result(name, method, failure, backend, value, error)
                        if not failure and method == "stop_services":
                            self.assertFalse(plugin._services_started)

    def check_lifecycle_result(self, name: str, method: str, failure: bool, backend: SlowBackend, value: Any, error: Exception | None) -> None:
        immediate = method == "start_services" and name in IMMEDIATE_STARTS
        if not immediate:
            self.assertTrue(backend.calls, "the slow backend path must actually execute")
        raises = failure and not immediate and (name, method) not in ENVELOPED_FAILURES | LOGGED_FAILURES
        if raises:
            self.assertIsInstance(error, OSError)
            return
        self.assertIsNone(error)
        if value is None:
            return
        self.assertEqual(set(value), {"action_status", "data", "actions", "error", "timestamp"})
        status = "error" if failure and (name, method) in ENVELOPED_FAILURES else "completed"
        self.assertEqual(value["action_status"], status)

    async def test_restart_and_idempotency(self) -> None:
        for name in LIFECYCLE_NAMES:
            if name == "coreai_embeddings_plugin":
                continue
            with self.subTest(plugin=name), ExitStack() as stack:
                backend = SlowBackend(False)
                backend.delay = 0.001
                plugin = lifecycle_case(name, "start_services", backend, stack)
                for method in ("start_services", "start_services", "stop_services", "stop_services", "start_services", "stop_services"):
                    result = await getattr(plugin, method)()
                    self.assertEqual(result["action_status"], "completed")
                    self.assertEqual(plugin._services_started, method == "start_services")

    async def test_context_order_and_cancelled_owner(self) -> None:
        identity: ContextVar[str] = ContextVar("f5_test_identity", default="missing")
        for name in CLASSES:
            with self.subTest(plugin=name):
                module, _ = load(name)
                started, release = threading.Event(), threading.Event()
                seen: list[str] = []

                def first(seen: list[str] = seen, started: threading.Event = started, release: threading.Event = release) -> str:
                    seen.append(identity.get())
                    started.set()
                    release.wait(2)
                    return "first"

                identity.set("request-owner")
                owner = asyncio.create_task(module._run_f5_blocking(first))
                try:
                    await self.wait_for_event(started)
                    identity.set("next-owner")
                    queued = asyncio.create_task(module._run_f5_blocking(lambda seen=seen: seen.append(identity.get()) or "second"))
                    owner.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await owner
                    await asyncio.sleep(0.02)
                    self.assertFalse(queued.done(), "cancellation must not release outstanding ownership")
                    self.assertEqual(module._F5_EXECUTOR._work_queue.qsize(), 0)
                    self.assertEqual(seen, ["request-owner"])
                    release.set()
                    self.assertEqual(await queued, "second")
                    self.assertEqual(seen, ["request-owner", "next-owner"])
                finally:
                    release.set()

    async def test_cancelled_backend_failure_is_logged(self) -> None:
        module, _ = load("actr_memory_plugin")
        started, release = threading.Event(), threading.Event()

        def fail() -> None:
            started.set()
            release.wait(2)
            raise OSError("cancelled owner backend failed")

        owner = asyncio.create_task(module._run_f5_blocking(fail))
        try:
            await self.wait_for_event(started)
            owner.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await owner
            with self.assertLogs(module.__name__, level="ERROR") as logs:
                release.set()
                await module._run_f5_blocking(lambda: None)
            self.assertTrue(any("cancelled owner backend failed" in line for line in logs.output))
        finally:
            release.set()

    async def wait_for_event(self, event: threading.Event) -> None:
        for _ in range(1000):
            if event.is_set():
                return
            await asyncio.sleep(0.001)
        self.fail("worker did not start")

    async def test_active_worker_shutdown(self) -> None:
        for name in JOINING_PLUGINS:
            with self.subTest(plugin=name), ExitStack() as stack:
                backend = SlowBackend(False)
                plugin = lifecycle_case(name, "stop_services", backend, stack)
                settled = threading.Event()

                def work(plugin: Any = plugin, settled: threading.Event = settled) -> None:
                    plugin._stop_event.wait()
                    time.sleep(0.3)
                    settled.set()

                worker = threading.Thread(target=work)
                plugin._worker_thread = worker
                plugin._job_thread = worker
                plugin._polling_thread = worker
                if name == "g_suite_plugin":
                    plugin._deferred.thread = worker
                plugin._backend = SimpleNamespace(close=lambda settled=settled: self.assertTrue(settled.is_set()))
                worker.start()
                try:
                    value, error = await self.measure(plugin.stop_services)
                    self.assertIsNone(error)
                    self.assertEqual(value["action_status"], "completed")
                    self.assertTrue(settled.is_set())
                    self.assertFalse(worker.is_alive())
                    self.assertFalse(plugin._services_started)
                finally:
                    plugin._stop_event.set() if plugin._stop_event is not None else None
                    worker.join(1)

    async def test_coreai_active_cleanup_and_reopen(self) -> None:
        module, _ = load("coreai_embeddings_plugin")
        plugin = module.CoreAIEmbeddingsPlugin({"asset_root": "/tmp", "compute_preference": "cpu"})
        runtime = SimpleNamespace(close=Mock(), prepare=Mock())
        plugin._runtime = runtime
        plugin._active_calls = 1

        def finish() -> None:
            time.sleep(0.3)
            with plugin._condition:
                plugin._active_calls = 0
                plugin._condition.notify_all()

        worker = threading.Thread(target=finish)
        worker.start()
        try:
            _, error = await self.measure(plugin.cleanup)
            self.assertIsNone(error)
            runtime.close.assert_called_once()
            self.assertIsNone(plugin._runtime)
            self.assertFalse(plugin.is_ready())
            with patch.object(module, "EmbeddingRuntime", return_value=runtime):
                plugin.prepare_for_readiness()
            self.assertTrue(plugin.is_ready())
            await plugin.cleanup()
            self.assertFalse(plugin.is_ready())
        finally:
            worker.join(1)

    async def test_session_launch_shutdown_remains_nonwaiting(self) -> None:
        _, plugin = load("agent_messaging_plugin")
        jobs_module = importlib.import_module("agent_messaging_plugin.session_launch_jobs")
        jobs = jobs_module.SessionLaunchJobs()
        release = threading.Event()
        future = jobs._executor.submit(release.wait)
        jobs._active_owner = {"operation_id": "outstanding-owner"}
        plugin._launch_jobs = jobs
        plugin._stop_choreography_worker = Mock()
        hosts = importlib.import_module("agent_messaging_plugin.session_hosts")
        try:
            with patch.object(hosts, "shutdown_all_drivers"):
                before = time.monotonic()
                await plugin.stop_services()
                self.assertLess(time.monotonic() - before, 0.1)
            self.assertFalse(future.done())
            self.assertEqual(jobs._active_owner, {"operation_id": "outstanding-owner"})
            self.assertTrue(jobs._stopped.is_set())
        finally:
            release.set()
            future.result(1)

    async def test_discord_memory_order(self) -> None:
        module, plugin = load("discord_plugin")
        plugin._active = True
        plugin._state_service = object()
        plugin._session_manager = object()
        plugin._blob_storage_service = object()
        plugin._extract_channel_context = Mock(return_value=("channel", None, "guild"))
        message = SimpleNamespace(id="message", author=SimpleNamespace(id="user"), attachments=[], content="hello")
        for failure in (False, True):
            with self.subTest(failure=failure), ExitStack() as stack:
                backend = SlowBackend(failure)
                plugin._memory_service = SimpleNamespace(store_interaction=backend)
                plugin._handle_user_message = Mock(side_effect=lambda backend=backend, **_: self.assertEqual(len(backend.calls), 1))
                stack.enter_context(patch.object(module.session_binding, "get_or_create_binding_async", AsyncMock(return_value={"session_id": "session", "id": "binding"})))
                stack.enter_context(patch.object(module.attachment_helpers, "store_message_attachments", AsyncMock(return_value=[])))
                value, error = await self.measure(lambda: plugin._handle_message(message))
                self.assertIsNone(value)
                if failure:
                    self.assertIsInstance(error, OSError)
                    plugin._handle_user_message.assert_not_called()
                else:
                    self.assertIsNone(error)
                    plugin._handle_user_message.assert_called_once()
                self.assertEqual(backend.calls[0][1]["content"], "hello")
                self.assertEqual(backend.calls[0][1]["session_id"], "session")


if __name__ == "__main__":
    unittest.main()
