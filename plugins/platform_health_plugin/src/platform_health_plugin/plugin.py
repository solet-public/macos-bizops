"""platform_health_plugin entry point.

Exposes a single ``@platform_process`` verb:
``plugin::platform_health_plugin::execute_registry_sweep``.

The verb iterates the live process registry and invokes every registered
``@platform_process`` and ``@service_interface_process`` with sentinel
arguments. Read-shape calls (``list_*``, ``get_*``) execute live; write-shape
calls are skipped unless the caller passes ``write_enabled=True``.

Per Architect Q1 ruling (2026-05-30) the verb is **NOT** a startup-blocking
gate. The plugin sits idle until an operator or CI explicitly calls the
verb via ``process_call``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

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


class PlatformHealthPlugin(PluginBase, EdgeProcessProvider):
    """Operator/CI diagnostic gate over the live process registry."""

    name: str = PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.logger = logging.getLogger(__name__)

    def prepare_for_readiness(self) -> None:
        if self.orchestrator_ref is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref not injected before prepare_for_readiness",
            )
        self.set_ready()

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
        }

    @platform_process(
        name=SWEEP_PROCESS_NAME,
        context_handling=ContextHandling.NONE,
        parameters={
            "write_enabled": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                required=False,
                description=(
                    "When True, write-shape verbs (anything not starting with "
                    "list_/get_) are invoked too. Use against a wipe-on-tear-down "
                    "test schema; do not run against production state."
                ),
            ),
            "include_pattern": ParameterMetadata(
                type=ParameterType.STRING,
                required=False,
                description=(
                    "Substring filter on process_key; only matching processes "
                    "are swept. Useful for narrowing to one namespace, e.g. "
                    "'session_ledger_service' or 'scheduling_service'."
                ),
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
                    description="Per-process rows: process_key, shape, status, error_class, error_message.",
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
        include_pattern = params.get("include_pattern")
        if include_pattern is not None and not isinstance(include_pattern, str):
            raise ValueError("include_pattern must be a string when supplied")
        report = sweep.run_sweep(
            self.orchestrator_ref,
            write_enabled=write_enabled,
            include_pattern=include_pattern,
        )
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": report,
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }
