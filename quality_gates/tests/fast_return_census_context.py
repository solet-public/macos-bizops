"""Real, disposable platform services and structural preparer discovery.

Only the registered provisioner supplies PostgreSQL. Each plugin owns a new
physical schema and home; configuration, setup writes and lifecycle receipts
remain report evidence. No application startup, profile or host vault runs.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import itertools
import json
import os
import pkgutil
import sys
import threading
import time
from collections import Counter
from collections.abc import Generator, Mapping
from functools import wraps
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from fast_return_census_fixtures import BoundaryAnswer, UnsafeAttempt, audited_setup, isolated_boundaries, owned_timezone, private_pg_environment, runtime_environment, temporary_app_home
from fast_return_census_vault import CensusVault as CensusVault

from quality_gates.private_test_pg import load_fixture

_SCHEMAS = itertools.count(1)
_WRITE_PREFIXES = ("write", "update", "delete", "create", "install", "drop", "set", "clear", "execute", "upsert", "insert", "increment")
_GUARDED_STARTS = frozenset({"actr_memory_plugin", "agent_messaging_plugin", "claude_code_cloud_session_source_plugin", "codex_cloud_session_source_plugin", "comfyui_image_generation_plugin", "default_scheduling_plugin", "marketo_plugin", "platform_health_plugin"})


def measurement_options(context: Any) -> dict[str, Any]:
    """Use the same exact thread/listener grants during setup and dispatch."""
    if context is None:
        return {}
    return context.guard_options()


def measurement_metadata(context: Any) -> dict[str, Any]:
    """Label standalone synthetic engine fixtures separately from real contexts."""
    preparers = [module.PREPARER_NAME for module in context.modules] if context is not None else []
    return {"state_calls": [], "thread_cpu_ms": 0.0, "job_readback": None, "preparer": preparers, "context": "private-pg-v1" if context is not None else "synthetic-engine-only"}


def excluded_reason(context: Any, key: tuple[str, str], exclusions: Mapping[tuple[str, str], str]) -> str | None:
    """An unsafe default requires the owning preparer's explicit per-verb opt-in."""
    modules = context.modules if context is not None else []
    opted_in = any(key in getattr(module, "MEASURE_EXCLUDED", frozenset()) for module in modules)
    return None if opted_in else exclusions.get(key)


def native_instance(context: Any, plugin: str, plugin_class: type[Any] | None) -> Any:
    if context is not None and plugin in {"postgres_state_management_plugin", "pgvector_service_plugin"}:
        return context.native_instance(plugin_class)
    return None


def prepare_parameters(context: Any, plugin: str, verb: str, params: dict[str, Any]) -> dict[str, Any]:
    """Rebuild ordered prerequisites, then transform inputs before either clock.

    Hooks receive the current context, never a retained global service handle.
    Their files and state calls belong to setup; the real caller clears those
    observations before entering timed_handler.
    """
    if context is not None:
        for module in context.modules:
            before_verb = getattr(module, "before_verb", None)
            if before_verb is not None:
                before_verb(plugin, verb, context)
        for module in context.modules:
            parameters = getattr(module, "parameters", None)
            if parameters is not None:
                params = parameters(plugin, verb, params, context.seeded, context.app_home)
    return params


def prepare_handler_state(context: Any, verb: str) -> dict[str, Any]:
    """Let an owning preparer build isolated caller state before timing."""
    state: dict[str, Any] = {"session_id": "census-session", "flow_id": "census-flow"}
    if context is not None:
        for module in context.modules:
            builder = getattr(module, "handler_state", None)
            if builder is not None:
                state = builder(context.plugin, verb, state, context.seeded)
    return state


async def _await_result(value: Any) -> Any:
    return await value


def timed_handler(method: Any, row: dict[str, Any], call: Any, guard: Any, context: Any) -> None:
    """Retain both clocks and state evidence on every unwind path.

    Database waits consume wall time but not calling-thread CPU. Coroutine
    dispatch includes the await in the same measurement. Readback is separate.
    """
    state = prepare_handler_state(context, row["verb"])
    cpu_started = time.thread_time()
    call.started = time.perf_counter()
    try:
        call.result = method(row["parameters"], state)
        if inspect.isawaitable(call.result):
            call.result = asyncio.run(_await_result(call.result))
    finally:
        call.finished = time.perf_counter()
        call.thread_cpu_ms = (time.thread_time() - cpu_started) * 1000
        call.files = list(guard.files)
        row["listener_ports"] = list(guard.listener_ports)
        if context is not None:
            row["state_calls"] = list(context.state.calls)


def job_readback(context: Any, row: dict[str, Any], result: dict[str, Any], plugin: str, verb: str) -> None:
    """Bind the returned handle to a persisted job and its observed request.

    Generic connector job actions may name the verb in request_data. The
    returned id must select that exact enqueue, rather than a prior setup job.
    """
    payload = result.get("data", result)
    row["job_readback"] = context.jobs.get_job(payload["job_id"])
    stored = row["job_readback"].get("data", {}).get("job", {})
    matching = [job for job in context.jobs.jobs if job.get("job_id") == payload["job_id"] and job.get("plugin_name") == plugin]
    row["worker_reachable"] = any(_job_matches(job, stored, plugin, verb) for job in matching)


def _job_matches(job: dict[str, Any], stored: dict[str, Any], plugin: str, verb: str) -> bool:
    names_verb = job.get("action_name") == verb or job.get("request_data", {}).get("verb") == verb
    return names_verb and stored.get("provider_name") == f"{plugin}.{job.get('action_name')}"


def finish_context_report(report: dict[str, Any]) -> None:
    """Preserve contextual evidence even when no handler reached a fast class."""
    report["context_runs"] = [plugin["context_run"] for plugin in report["plugins"] if "context_run" in plugin]
    observed: list[dict[str, Any]] = []
    inline = []
    for plugin in report["plugins"]:
        for row in plugin["verbs"]:
            observed.extend({"plugin": plugin["plugin"], "verb": row["verb"], **call} for call in row.get("state_calls", []))
            calls = _caller_state_calls(row)
            if calls:
                inline.append({"plugin": plugin["plugin"], "verb": row["verb"], "calls": calls})
    report["state_writes"] = [call for call in observed if call["write"]]
    report["inline_state_calls"] = inline
    report["residual"] = _residual_counts(report)


def _caller_state_calls(row: dict[str, Any]) -> list[dict[str, Any]]:
    return [call for call in row.get("state_calls", []) if call["thread_id"] == row.get("calling_thread")]


def _residual_counts(report: dict[str, Any]) -> dict[str, Any]:
    rows = [row for plugin in report["plugins"] for row in plugin["verbs"] if row["class"] == "RESIDUAL"]
    return {"by_reason": dict(Counter(row["residual_reason"] for row in rows)), "by_proof": dict(Counter(row["proof"]["result"] for row in rows))}


def discover_preparers() -> list[ModuleType]:
    """Consume independently shipped modules in deterministic discovery order."""
    import fast_return_census_preparers

    modules = [importlib.import_module(f"{fast_return_census_preparers.__name__}.{item.name}") for item in sorted(pkgutil.iter_modules(fast_return_census_preparers.__path__), key=lambda item: item.name)]
    for module in modules:
        if not isinstance(getattr(module, "PREPARER_NAME", None), str):
            raise ValueError(f"preparer has no PREPARER_NAME: {module.__name__}")
    return modules


def preparers_for(plugin: str, modules: list[ModuleType]) -> list[ModuleType]:
    return [module for module in modules if plugin in getattr(module, "PLUGINS", frozenset())]


class RecordingState:
    """Instrument real instances without changing their type or protocol surface.

    Record the outermost operation once per calling thread, so a public
    operation calling another public operation cannot double its elapsed time.
    Transactions yield their actual native handle with the same instrumentation.
    """

    def __init__(self, target: Any, schema: str, calls: list[dict[str, Any]] | None = None) -> None:
        self.target = target
        self.schema = schema
        self.calls = calls if calls is not None else []
        self.local = threading.local()
        self.instrument(target)

    def instrument(self, target: Any) -> None:
        target.calls = self.calls
        for name, operation in inspect.getmembers(target, inspect.ismethod):
            if not name.startswith("_"):
                setattr(target, name, self.transactional(operation) if name == "transactional" else self.record(name, operation))

    def record(self, name: str, operation: Any) -> Any:
        signature = inspect.signature(operation)

        @wraps(operation)
        def invoke(*args: Any, **kwargs: Any) -> Any:
            if getattr(self.local, "active", False):
                return operation(*args, **kwargs)
            bound = signature.bind_partial(*args, **kwargs).arguments
            data = bound.get("data", bound.get("query", {}))
            table = bound.get("table", data.get("table") if isinstance(data, dict) else None)
            record = {"method": name, "namespace": bound.get("namespace"), "table": table, "schema": self.schema, "write": name.startswith(_WRITE_PREFIXES), "thread_id": threading.get_ident()}
            started = time.perf_counter()
            self.local.active = True
            try:
                return operation(*args, **kwargs)
            finally:
                self.local.active = False
                record["elapsed_ms"] = (time.perf_counter() - started) * 1000
                self.calls.append(record)

        return invoke

    def transactional(self, operation: Any) -> Any:
        @contextlib.contextmanager
        @wraps(operation)
        def transaction_context(*args: Any, **kwargs: Any) -> Generator[Any]:
            with operation(*args, **kwargs) as transaction:
                self.instrument(transaction)
                yield transaction

        return transaction_context


def _platform_registry(context: Any) -> dict[str, object]:
    """Build health's registry through the production scanner and bindings."""
    from ananta.core.orchestration.service_bindings import ServiceBindings
    from ananta.core.process_registry.builder import build_process_registry

    bindings_file = context.app_home / "config" / "service_bindings.json"
    bindings_file.write_text(
        json.dumps(
            {
                "state_service": "postgres_state_management_plugin",
                "plugin_schema_service": "postgres_state_management_plugin",
                "blob_storage_service": "default_blob_storage_plugin",
                "address_book_service": "default_address_book_plugin",
                "scheduling_service": "default_scheduling_plugin",
                "memory_service": "actr_memory_plugin",
                "embedding_service": "openai_embeddings_plugin",
                "vector_service": "pgvector_service_plugin",
            }
        )
    )
    bindings = ServiceBindings(context.app_home)
    bindings.load()
    context.registry.set_orchestrator_ref(SimpleNamespace(APP_HOME=str(context.app_home), config_manager=context.manager, plugin_manager=context.registry, get_service=context.get_service, service_bindings=bindings, **context.services))
    return build_process_registry(context.registry)


def _control_threads(plugin_class: type[Any]) -> frozenset[str]:
    """Grant exact declared F5 executor names, while job targets stay parked."""
    names = set()
    for module_name in (plugin_class.__module__, "actr_memory_plugin.plugin", "default_scheduling_plugin.plugin"):
        executor = getattr(sys.modules.get(module_name), "_F5_EXECUTOR", None)
        prefix = getattr(executor, "_thread_name_prefix", None)
        workers = getattr(executor, "_max_workers", 0)
        if isinstance(prefix, str) and isinstance(workers, int):
            names.update(f"{prefix}_{index}" for index in range(workers))
    return frozenset(names)


def _lifecycle_result(instance: Any, operation: str) -> Any:
    result = getattr(instance, operation)()
    return asyncio.run(_await_result(result)) if inspect.isawaitable(result) else result


def _lifecycle_ok(result: Any) -> bool:
    """A void hook returned normally; retain None rather than inventing a reply."""
    return result is None or isinstance(result, dict) and result.get("action_status") == "completed"


class RecordingJobs:
    """Observe real create results without relaxing the manager's validation."""

    def __init__(self, manager: Any) -> None:
        self.manager = manager
        self.jobs: list[dict[str, Any]] = []

    def create_job(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        bound = inspect.signature(self.manager.create_job).bind(*args, **kwargs).arguments
        first = len(self.manager.state_service.calls)
        try:
            result = self.manager.create_job(*args, **kwargs)
        finally:
            for call in self.manager.state_service.calls[first:]:
                call["sanctioned_job_persistence"] = True
        data = result.get("data", {})
        if result.get("action_status") == "completed" and data.get("job_id"):
            self.jobs.append(dict(bound) | {"job_id": data["job_id"]})
        return result

    def __getattr__(self, name: str) -> Any:
        return getattr(self.manager, name)


class CensusContext:
    """One plugin's real service graph; create/read/stop, no persistent mutation."""

    def __init__(self, root: Path, scratch: Path, plugin: str, plugin_class: type[Any], probe: Any, modules: list[ModuleType]) -> None:
        from ananta.core.plugins.plugin_manager import PluginManager

        self.root, self.scratch, self.plugin = root, scratch, plugin
        self.plugin_class, self.probe = plugin_class, probe
        self.modules = preparers_for(plugin, modules)
        self.config = load_fixture("fast_return_census")
        self.schema = f"census_{next(_SCHEMAS)}"
        self.services: dict[str, Any] = {}
        self.registry: Any = PluginManager()
        self.started_plugins: list[Any] = []
        self.seeded: dict[str, str] = {}
        self.receipt: dict[str, Any] = {"plugin": plugin, "schema": self.schema, "database": self.config.database, "port": self.config.port, "started": False, "stopped": False, "setup_file_touches": [], "setup_state_calls": [], "prerequisites": {}, "advisories": [], "lifecycle": []}
        self.identity = f"census_{self.config.nonce[:12]}"
        self.vault = CensusVault(self.identity)
        self.app_home = scratch
        self.export_root = scratch
        self.configuration: dict[str, Any] = {}
        if len(self.modules) > 1:
            self.receipt["advisories"].append("multiple preparers applied in module-name order")

    def get_service(self, name: str) -> Any:
        return self.services.get(name)

    @property
    def state(self) -> Any:
        return self.get_service("state_service")

    @property
    def jobs(self) -> Any:
        return self.get_service("async_job_manager")

    def store_blob(self, namespace: str, filename: str, content: bytes, mime_type: str) -> str:
        # The provider derives extension-qualified external_id from original_name.
        result = self.services["blob_storage_service"].store_blob(namespace, content, {"filename": filename, "original_name": filename, "mime_type": mime_type})
        if result.get("action_status") != "completed":
            raise RuntimeError(f"real blob seed failed: {result}")
        return str(result["data"]["blob_id"])

    def _configure(self) -> None:
        from ananta.core.config.config_manager import initialize_config

        self.manager = initialize_config(str(self.app_home))
        for module in self.modules:
            configure = getattr(module, "configure", None)
            if configure is not None:
                self.configuration.update(configure(self.plugin, self.app_home, self.export_root))
        self.configuration.setdefault("export_allowed_roots", [str(self.export_root)])
        self.manager.save_plugin_config(self.plugin, self.configuration)
        self.manager.save_plugin_config("postgres_state_management_plugin", dict(self.config.connection_config(self.schema)))
        self.manager.save_plugin_config("pgvector_service_plugin", {"pool_size": 2})

    def wire(self, instance: Any) -> None:
        """Use production service setters and the orchestrator service registry."""
        instance.orchestrator_ref = SimpleNamespace(APP_HOME=str(self.app_home), config_manager=self.manager, plugin_manager=self.registry, get_service=self.get_service, get_process_registry=lambda: self.services["process_registry"], **self.services)
        for service in ("state_service", "blob_storage_service", "embedding_service", "memory_service", "async_job_manager", "action_factory", "action_manager", "vault_service"):
            setter = getattr(instance, f"set_{service}", None)
            value = self.vault if service == "vault_service" else self.services.get(service)
            if callable(setter) and value is not None:
                setter(value)
        if hasattr(instance, "_async_job_manager") and not callable(getattr(instance, "set_async_job_manager", None)):
            instance._async_job_manager = self.jobs
        if hasattr(instance, "_deferred"):
            instance._deferred.manager = self.jobs

    def ready(self, instance: Any) -> None:
        self.wire(instance)
        readiness = getattr(instance, "prepare_for_readiness", None)
        if callable(readiness):
            readiness()
        is_ready = getattr(instance, "is_ready", None)
        if callable(is_ready) and not is_ready():
            # Matches the startup lifecycle after a successful readiness hook.
            instance.set_ready()
        self.initialize(instance)

    def initialize(self, instance: Any) -> None:
        """Run guarded starts and the actual post-start config dispatcher.

        The production dispatcher owns YAML defaults and initialize signatures.
        A false result remains an advisory in the receipt, without a substitute
        initializer. Unsafe release/router starts remain explicit prerequisites.
        """
        from ananta.core.plugins.plugin_initializer import PluginInitializer

        if instance.name in _GUARDED_STARTS:
            self.start_instance(instance)
        result = PluginInitializer({instance.name: instance}, self.registry.get_validation_registry()).initialize_all_plugins(self.manager)
        self.receipt["lifecycle"].append({"plugin": instance.name, "operation": "initialize", "result": result})
        if not result.get(instance.name):
            self.receipt["advisories"].append(f"production initialize dispatcher returned false: {instance.name}")

    def start_instance(self, instance: Any) -> None:
        if instance not in self.started_plugins:
            self.started_plugins.append(instance)
            result = _lifecycle_result(instance, "start_services")
            self.receipt["lifecycle"].append({"plugin": instance.name, "operation": "start_services", "result": result})
            if not _lifecycle_ok(result):
                raise RuntimeError(f"private fixture service start failed: {instance.name}: {result}")

    def _start_state(self) -> None:
        from ananta.core.state.async_job_manager import AsyncJobManager
        from ananta.services.schema_manager import SchemaManager
        from ananta.services.state_service import StateService
        from postgres_state_management_plugin.plugin import PostgresStatePlugin

        plugin = PostgresStatePlugin()
        self.registry.plugins[plugin.name] = plugin
        plugin.set_vault_service(cast(Any, self.vault))
        self.started_plugins.append(plugin)
        plugin.prepare_for_readiness()
        result = asyncio.run(plugin.start_services())
        if result.get("action_status") != "completed":
            raise RuntimeError(f"private state start failed: {result}")
        state = StateService(self.registry, app_home=str(self.app_home), state_plugin_name=plugin.name)
        RecordingState(state, self.schema)
        self.schemas = SchemaManager(state, plugin)
        state.set_schema_manager(self.schemas)
        self.services["state_service"] = state
        self.services["async_job_manager"] = RecordingJobs(AsyncJobManager(state))
        self.services["vault_service"] = self.vault

    def _providers(self) -> None:
        from actr_memory_plugin.plugin import ACTRMemoryPlugin
        from default_address_book_plugin.plugin import DefaultAddressBookPlugin
        from default_scheduling_plugin.plugin import SchedulingPlugin
        from openai_embeddings_plugin.plugin import OpenAIEmbeddingsPlugin
        from pgvector_service_plugin.plugin import PGVectorServicePlugin

        from default_blob_storage_plugin.plugin import DefaultBlobStoragePlugin

        for cls in (DefaultBlobStoragePlugin, DefaultAddressBookPlugin, SchedulingPlugin, OpenAIEmbeddingsPlugin, PGVectorServicePlugin, ACTRMemoryPlugin):
            provider = cls()
            if provider.name not in self.registry.plugins:
                self.registry.plugins[provider.name] = provider
        if self.plugin in {"default_address_book_plugin", "platform_health_plugin"}:
            from agent_messaging_plugin.plugin import AgentMessagingPlugin

            messaging = AgentMessagingPlugin()
            self.registry.plugins[messaging.name] = messaging
        if self.plugin not in self.registry.plugins:
            self.registry.plugins[self.plugin] = self.plugin_class()

    def _install_schemas(self) -> None:
        from ananta.config.core_schemas import CoreSchemaDefinitions

        declarations = CoreSchemaDefinitions.get_all_core_schemas()
        instances: list[Any] = list(self.registry.plugins.values())
        seen: set[tuple[str, tuple[str, ...]]] = set()
        for instance in instances:
            getter = getattr(instance, "get_schema_definitions", None)
            if callable(getter):
                declarations.extend(cast(list[Any], getter()))
        unique = []
        for definition in declarations:
            key = definition.namespace, tuple(sorted(definition.tables))
            if key not in seen:
                seen.add(key)
                unique.append(definition)
        self.schemas.initialize_schemas(unique)

    def _provider_ready(self, name: str) -> None:
        provider = self.registry.plugins[name]
        try:
            self.ready(provider)
        except (Exception, BoundaryAnswer, UnsafeAttempt) as error:
            self.receipt["prerequisites"][name] = f"{type(error).__name__}: {error}"

    def _build_services(self) -> None:
        from ananta.core.actions.action_factory import ActionFactory
        from ananta.services.blob_storage_service import BlobStorageService
        from ananta.services.embedding_service import EmbeddingService
        from ananta.services.memory_service import MemoryService
        from ananta.services.scheduling_service import SchedulingService
        from ananta.services.session_ledger_service import SessionLedgerService
        from ananta.services.vector_service import VectorService

        self._provider_ready("default_blob_storage_plugin")
        self.services["blob_storage_service"] = BlobStorageService(self.registry, "default_blob_storage_plugin", str(self.app_home))
        self._provider_ready("default_address_book_plugin")
        self.services["address_book_service"] = self.registry.plugins["default_address_book_plugin"]
        process_registry: dict[str, object] = _platform_registry(self) if self.plugin == "platform_health_plugin" else {"processes": {}}
        self.services["process_registry"] = process_registry
        self.services["action_factory"] = ActionFactory(process_registry=self.services["process_registry"], state_service=self.state, app_home=str(self.app_home))
        self.services["embedding_service"] = EmbeddingService(self.registry, "openai_embeddings_plugin")
        self.services["vector_service"] = VectorService(self.registry, "pgvector_service_plugin", self.state)
        self.services["scheduling_service"] = SchedulingService(self.registry, app_home=str(self.app_home))
        self._seed_addresses()
        self._provider_ready("openai_embeddings_plugin")
        self.wire(self.registry.plugins["pgvector_service_plugin"])
        self.registry.plugins["pgvector_service_plugin"].prepare_for_readiness()
        self._provider_ready("actr_memory_plugin")
        memory = self.registry.plugins["actr_memory_plugin"]
        if memory.is_ready():
            self.services["memory_service"] = MemoryService(self.registry, memory.name)
        self._provider_ready("default_scheduling_plugin")
        self.services["session_ledger_service"] = SessionLedgerService(
            state_service=self.state,
            blob_storage_service=self.services["blob_storage_service"],
            plugin_manager=self.registry,
            embedding_service=cast(Any, self.services["embedding_service"]),
            vector_service=cast(Any, self.services["vector_service"]),
            scheduling_service=self.services["scheduling_service"],
            ledger_allowed_roots=[str(self.app_home)],
        )

    def _seed_addresses(self) -> None:
        address = self.services["address_book_service"]
        entries: dict[str, Mapping[str, object]] = {
            "pgvector_service_db": {"host": self.config.host, "port": self.config.port, "user": self.config.user, "database": self.config.database, "db_schema": self.schema},
            "openai_embeddings": {"base_url": "https://census.invalid/v1", "model": "census-fixture", "api_key": "census-synthetic-value"},
        }
        for name, values in entries.items():
            result = address.register(name=name, address_type="service", description="synthetic census fixture", entries=[{"field_type": key if name == "openai_embeddings" else "text", "description": key, "value": str(value)} for key, value in values.items()])
            if result.get("action_status") != "completed":
                raise RuntimeError(f"census address seed failed: {result}")

    def start(self) -> None:
        self._configure()
        self._start_state()
        with isolated_boundaries(self.root, self.scratch, self.probe, **self.guard_options()) as audit:
            self._providers()
            self._install_schemas()
            self._build_services()
            self.receipt["setup_file_touches"].extend(audit.files)
        vector = self.registry.plugins["pgvector_service_plugin"]
        self.started_plugins.append(vector)
        result = asyncio.run(vector.start_services())
        if result.get("action_status") != "completed":
            raise RuntimeError(f"private vector start failed: {result}")
        with isolated_boundaries(self.root, self.scratch, self.probe, **self.guard_options()) as audit:
            for module in self.modules:
                seed = getattr(module, "seed", None)
                if seed is not None:
                    self.seeded.update(seed(self.plugin, self))
            self.receipt["setup_file_touches"].extend(audit.files)
        self.receipt["setup_state_calls"] = list(self.state.calls)
        self.receipt["setup_thread_starts"] = list(self.probe.starts)
        self.receipt["started"] = True

    def guard_options(self) -> dict[str, Any]:
        return {
            "postgres": self.config,
            "run_threads": _control_threads(self.plugin_class) | frozenset(name for module in self.modules for name in getattr(module, "RUN_THREADS", frozenset())),
            "loopback_listener": any(getattr(module, "LOOPBACK_LISTENER", False) for module in self.modules),
            "devnull_write": any(self.plugin in getattr(module, "DEVNULL_WRITE_PLUGINS", frozenset()) for module in self.modules),
        }

    def stop(self) -> None:
        results = []
        for plugin in reversed(self.started_plugins):
            for interface in getattr(plugin, "get_supported_interfaces", lambda: [])():
                plugin.unset_as_active_provider(interface)
            result = _lifecycle_result(plugin, "stop_services")
            results.append({"plugin": plugin.name, "result": result})
        self.receipt.update(stopped=all(_lifecycle_ok(item["result"]) for item in results), stop_results=results, vault_keys=self.vault.keys)

    def native_instance(self, plugin_class: type[Any]) -> Any:
        """Open a fresh explicitly configured provider before parking new workers."""
        instance = plugin_class()
        self.wire(instance)
        self.started_plugins.append(instance)
        instance.prepare_for_readiness()
        result = asyncio.run(instance.start_services())
        if result.get("action_status") != "completed":
            raise RuntimeError(f"private measured provider start failed: {result}")
        return instance


@contextlib.contextmanager
def plugin_context(root: Path, scratch: Path, plugin: str, plugin_class: type[Any], probe: Any, modules: list[ModuleType]) -> Generator[CensusContext]:
    """Keep the real state pool alive across this plugin's deterministic verbs."""
    with temporary_app_home(scratch) as home:
        context = CensusContext(root, home, plugin, plugin_class, probe, modules)
        context.app_home, context.export_root = home / "application" / "profile", home / "export"
        context.app_home.mkdir(parents=True)
        context.export_root.mkdir()
        environment = {"SOLET_NAME": context.identity, "APP_HOME": str(context.app_home), "HOME": str(home), "TMPDIR": str(home), "PATH": os.environ.get("PATH", ""), **runtime_environment(home), **private_pg_environment(context.config)}
        before = set(threading.enumerate())
        with patch.dict(os.environ, environment, clear=True), audited_setup(root, home) as audit, owned_timezone(home):
            try:
                context.start()
                yield context
            finally:
                context.stop()
                context.receipt["setup_file_touches"].extend(audit.files)
                context.receipt["threads_after_stop"] = [thread.name for thread in threading.enumerate() if thread not in before]
    context.receipt["fixture_removed"] = not home.exists()
