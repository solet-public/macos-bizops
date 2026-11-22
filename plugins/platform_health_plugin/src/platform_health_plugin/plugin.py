"""platform_health_plugin entry point.

Verbs:

* ``plugin::platform_health_plugin::execute_registry_sweep`` — the registry
  sweep. WARNING: a diagnostic name does not make this verb diagnose-safe.
  Read-shape calls can reach external systems. The default is
  ``dry_run=True``, which only classifies; live dispatch requires
  ``dry_run=False`` and is restricted to in-process ``service_interface``
  providers unless named external plugin namespaces and an
  operator-confirmation citation are supplied. Per Architect Q1 ruling
  (2026-05-30) it is **NOT** a startup-blocking gate: it runs only when an
  operator or CI calls it via ``process_call``.
* ``plugin::platform_health_plugin::check_model_capability_catalog`` — runs the
  model capability catalog refresh once, classifies the catalog, and records and
  logs a severity change; it sends nothing to any session
  (``is_discoverable=False``: it reaches the network).
* ``plugin::platform_health_plugin::check_model_capability_catalog_cron`` — its
  daily EDGE_SINK cron target, dispatched on a background thread.
* ``plugin::platform_health_plugin::ensure_model_capability_guard_schedule`` — the
  idempotent install primitive for that cron (``is_discoverable=False``). It needs
  no configuration, so the cron installs on any deployment.

``scheduling_service`` is resolved at call time, never at readiness: it lives
on the orchestrator's ``service_manager``, which startup creates only at its
``init_service_manager`` step, AFTER every plugin's ``prepare_for_readiness``
and ``start_services`` have run. So this plugin comes up ready without it,
and nothing installs the cron from a lifecycle method. The install runs as a
starting action (``ensure_model_capability_guard_schedule``, the ``session_ledger``
``ensure_periodic_poll_schedule`` precedent) in the deployment's own runtime
``config/starting_action_definitions.json``, which the orchestrator submits
after startup completes. No shipped profile template carries it; a deployment
adds the starting action itself. The cron fire honours ``set_active``: the inactive colour
of a blue-green pair does nothing on a fire.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, cast

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ActionStatus, ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)

from platform_health_plugin.constants import (
    PLUGIN_NAME,
    SWEEP_PROCESS_NAME,
)
from platform_health_plugin.model_capability_guard import (
    CHECK_PROCESS_NAME as MODEL_CAPABILITY_CHECK_PROCESS_NAME,
)
from platform_health_plugin.model_capability_guard import (
    DEFAULT_SCHEDULE_CRON as MODEL_CAPABILITY_DEFAULT_SCHEDULE_CRON,
)
from platform_health_plugin.model_capability_guard import (
    DEFAULT_SCHEDULE_TAG as MODEL_CAPABILITY_DEFAULT_SCHEDULE_TAG,
)
from platform_health_plugin.model_capability_guard import (
    ENSURE_SCHEDULE_PROCESS_NAME as MODEL_CAPABILITY_ENSURE_SCHEDULE_PROCESS_NAME,
)
from platform_health_plugin.model_capability_guard import (
    ModelCapabilityGuardConfig,
)
from platform_health_plugin.model_capability_guard import (
    ensure_model_capability_guard_schedule as _ensure_model_capability_guard_schedule,
)
from platform_health_plugin.model_capability_guard import (
    run_model_capability_guard as _run_model_capability_guard,
)

from .operation_jobs import JOB_RETURN_SCHEMA, AsyncJobs


class PlatformHealthPlugin(PluginBase, EdgeProcessProvider):
    """Operator/CI diagnostic gate over the live process registry."""

    name: str = PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self._operation_jobs = AsyncJobs()
        self.logger = logging.getLogger(__name__)
        self._running = False
        self._active = True
        # Single slot for the cron-fired check: a fire that lands while the
        # previous check is still running is a no-op, never a second check.
        self._catalog_check_lock = threading.Lock()

    def prepare_for_readiness(self) -> None:
        if self.orchestrator_ref is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref not injected before prepare_for_readiness",
            )
        # scheduling_service is deliberately NOT acquired here: it does not
        # exist yet at this startup step (see the module docstring).
        self.set_ready()

    def start_services(self) -> None:
        """Mark the plugin running. Installs nothing: ``scheduling_service``
        does not exist yet at this startup step (see the module docstring)."""
        self._running = True

    def stop_services(self) -> None:
        self._running = False

    def is_running(self) -> bool:
        return self._running

    def set_active(self, active: bool) -> None:
        self._active = active

    def _dispatch_catalog_check_in_background(self, state: dict[str, Any]) -> str:
        """Hand one catalog refresh + check to a background thread and return
        at once: the refresh's network fetches and state reads/writes run off
        the action-queue loop. A fire landing while the
        previous one is still running is a no-op."""
        if not self._active:
            return "inactive"
        if not self._catalog_check_lock.acquire(blocking=False):
            return "already_running"

        def body() -> None:
            try:
                self._run_catalog_check(state)
            except Exception:  # noqa: BLE001 — logged at ERROR with the traceback, never swallowed silently
                self.logger.exception("%s model-capability catalog check failed", self.name)
            finally:
                self._catalog_check_lock.release()

        threading.Thread(target=body, name="model-capability-catalog-check", daemon=True).start()
        return "started"

    def _run_catalog_check(self, state: dict[str, Any]) -> dict[str, Any]:
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{self.name}: orchestrator_ref unavailable; cannot refresh the catalog")
        config = ModelCapabilityGuardConfig()
        return _run_model_capability_guard(self.orchestrator_ref, config=config, state=state)

    def _require_scheduling_service(self) -> Any:
        """``scheduling_service`` resolved NOW, at call time. Raises loudly
        when it is absent: the orchestrator is not injected, or the call ran
        before startup's ``init_service_manager`` step."""
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{self.name}: orchestrator_ref unavailable; cannot reach the scheduler")
        service = self.orchestrator_ref.get_service("scheduling_service")
        if service is None:
            raise RuntimeError(
                f"{self.name}: scheduling_service is not available yet -- it exists only after startup's init_service_manager step; run ensure_model_capability_guard_schedule as a starting action or later, never from a plugin lifecycle method",
            )
        return service

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            SWEEP_PROCESS_NAME: EdgeProcessDefinition(
                name=SWEEP_PROCESS_NAME,
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type="platform_health_sweep_result",
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            MODEL_CAPABILITY_CHECK_PROCESS_NAME: EdgeProcessDefinition(
                name=MODEL_CAPABILITY_CHECK_PROCESS_NAME,
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type="model_capability_guard_result",
                ),
                # A retry would run a second live refresh for the same intent.
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            MODEL_CAPABILITY_ENSURE_SCHEDULE_PROCESS_NAME: EdgeProcessDefinition(
                name=MODEL_CAPABILITY_ENSURE_SCHEDULE_PROCESS_NAME,
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type="model_capability_guard_schedule_ensure_result",
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
        }

    @platform_process(
        name=SWEEP_PROCESS_NAME,
        context_handling=ContextHandling.NONE,
        parameters={
            "write_enabled": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=("When True, write-shape verbs (anything not starting with list_/get_) are invoked too. Use against a wipe-on-tear-down test schema; do not run against production state."),
            ),
            "dry_run": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=("Defaults to true. When true, returns every classification row and never dispatches a process. Set false only for an explicitly scoped live sweep."),
            ),
            "external_namespaces": ParameterMetadata(
                type=ParameterType.LIST,
                required=False,
                description=("Explicit list of declared outward-facing plugin provider namespaces to include in a live sweep. This is a list, not a broad enable flag, and requires operator_confirmation."),
            ),
            "operator_confirmation": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description=("Authorizing operator turn or message id, required whenever external_namespaces is non-empty. Echoed in those result rows."),
            ),
            "include_pattern": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description=("Substring filter on process_key; only matching processes are swept. Useful for narrowing to one namespace, e.g. 'session_ledger_service' or 'scheduling_service'."),
            ),
        },
        output_type="object",
        output_description="Per-process sweep report.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Sweep summary + per-process rows.",
            properties={
                "total": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Processes considered after include_pattern filter.",
                ),
                "ok": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Processes that completed without raising.",
                ),
                "failed": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Processes that raised; see results[].error_message.",
                ),
                "skipped": ParameterMetadata(
                    type=ParameterType.INTEGER,
                    description="Skipped (write-shape, self, unresolved).",
                ),
                "results": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=("Per-process rows: process_key, shape, status, would_dispatch, error_class, error_message, and operator_confirmation."),
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Platform health registry sweep",
            result_type="platform_health_sweep_result",
            result_description="Per-process pass/fail report from the registry sweep.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def execute_registry_sweep(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002 — required by @platform_process signature
    ) -> dict[str, Any]:
        from platform_health_plugin import sweep  # late import: keeps sweep module reloadable  # noqa: PLC0415

        if self.orchestrator_ref is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref unavailable; cannot read registry",
            )
        write_enabled = bool(params.get("write_enabled", False))
        dry_run = params.get("dry_run", True)
        if not isinstance(dry_run, bool):
            raise ValueError("dry_run must be a boolean when supplied")
        include_pattern = params.get("include_pattern")
        if include_pattern is not None and not isinstance(include_pattern, str):
            raise ValueError("include_pattern must be a string when supplied")
        external_namespaces = _parse_external_namespaces(params.get("external_namespaces", []))
        operator_confirmation = params.get("operator_confirmation")
        if operator_confirmation is not None and not isinstance(operator_confirmation, str):
            raise ValueError("operator_confirmation must be a string when supplied")
        report = sweep.run_sweep(
            self.orchestrator_ref,
            write_enabled=write_enabled,
            include_pattern=include_pattern,
            dry_run=dry_run,
            external_namespaces=external_namespaces,
            operator_confirmation=operator_confirmation,
        )
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": report,
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    @platform_process(
        is_async=True,
        name="check_model_capability_catalog",
        is_discoverable=False,
        context_handling=ContextHandling.NONE,
        parameters={},
        output_type="object",
        output_description="Model capability catalog refresh outcome and severity.",
        return_value_schema=JOB_RETURN_SCHEMA,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Model capability catalog check",
            result_type="model_capability_guard_result",
            result_description="Catalog refresh outcome and renewal health, and whether the severity changed on this call.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def check_model_capability_catalog(
        self,
        params: dict[str, Any],  # noqa: ARG002 — required by @platform_process signature
        state: dict[str, Any],
    ) -> dict[str, Any]:
        return self._operation_jobs.submit(self, "check_model_capability_catalog", dict(params), state, lambda _values: _envelope(self._run_catalog_check(state)))

    # Literal ``name=``: the whole-tree gate resolves a cron target's
    # processor_policy_category from the literal decorator name.
    @platform_process(
        name="check_model_capability_catalog_cron",
        is_discoverable=False,
        context_handling=ContextHandling.NONE,
        parameters={},
        output_type="object",
        output_description="Whether this fire started a background model capability catalog check.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Cron fire outcome.",
            properties={
                "dispatch": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="started | already_running | inactive",
                ),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
    )
    def check_model_capability_catalog_cron(
        self,
        params: dict[str, Any],  # noqa: ARG002 — required by @platform_process signature
        state: dict[str, Any],
    ) -> dict[str, Any]:
        outcome = self._dispatch_catalog_check_in_background(state)
        self.logger.info("%s model-capability cron fire: %s", self.name, outcome)
        return _envelope({"dispatch": outcome})

    @platform_process(
        is_async=True,
        name="ensure_model_capability_guard_schedule",
        context_handling=ContextHandling.NONE,
        is_discoverable=False,
        parameters={
            "cron_expression": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description=f"Cron expression (UTC). Default '{MODEL_CAPABILITY_DEFAULT_SCHEDULE_CRON}' (daily).",
            ),
            "tag": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description=f"Schedule tag, used for idempotent lookup. Default '{MODEL_CAPABILITY_DEFAULT_SCHEDULE_TAG}'.",
            ),
        },
        output_type="object",
        output_description="Queued guard installation job; read its full ensure outcome in job.result.",
        return_value_schema=JOB_RETURN_SCHEMA,
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            action_label="Ensure model capability guard schedule",
            result_type="model_capability_guard_schedule_ensure_result",
            result_description="Whether the model capability guard cron was just created or was already present.",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    def ensure_model_capability_guard_schedule_action(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return cast(dict[str, Any], self.admit_registered_operation("ensure_model_capability_guard_schedule", params, state))

    def admit_registered_operation(self, action_name: str, parameters: dict[str, Any], state: dict[str, Any]) -> dict[str, Any] | None:
        """Admit the whole guard installer; its scheduler dependency stays synchronous."""
        if action_name != "ensure_model_capability_guard_schedule":
            return None
        operation = self.ensure_model_capability_guard_schedule
        context = dict(state)
        return self.submit_scheduler_operation(self, action_name, dict(parameters), context, lambda values: operation(values, context))

    def submit_scheduler_operation(
        self,
        owner: PluginBase,
        action_name: str,
        parameters: dict[str, Any],
        state: dict[str, Any],
        operation: Callable[[dict[str, Any]], dict[str, Any]],
    ) -> dict[str, Any]:
        """Single scheduler-family queue, shared with this plugin's existing jobs.

        Serialize ensure's lookup/create with all admitted registry walks. The
        operation calls synchronous dependencies directly, never its own queue.
        """
        return self._operation_jobs.submit(owner, action_name, parameters, state, operation)

    def ensure_model_capability_guard_schedule(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002 — required by @platform_process signature
    ) -> dict[str, Any]:
        report = _ensure_model_capability_guard_schedule(
            self._require_scheduling_service(),
            cron_expression=str(params.get("cron_expression") or MODEL_CAPABILITY_DEFAULT_SCHEDULE_CRON),
            tag=str(params.get("tag") or MODEL_CAPABILITY_DEFAULT_SCHEDULE_TAG),
        )
        return _envelope(report)


def _envelope(data: dict[str, Any]) -> dict[str, Any]:
    return {
        "action_status": ActionStatus.COMPLETED.value,
        "data": data,
        "actions": [],
        "error": None,
        "timestamp": datetime.now(UTC).isoformat(),
    }


def _parse_external_namespaces(value: object) -> tuple[str, ...]:
    """Validate the explicit external-provider enumeration without coercion."""
    if not isinstance(value, list) or not all(isinstance(name, str) and name for name in value):
        raise ValueError("external_namespaces must be a list of non-empty strings")
    if len(set(value)) != len(value):
        raise ValueError("external_namespaces must not contain duplicates")
    return tuple(value)
