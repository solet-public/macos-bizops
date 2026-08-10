"""Quality Service Public API.

``@service_interface_process``-decorated surface for the three verbs of
:class:`QualityServiceInterface`. The bound provider
(``platform_dev_surface_plugin``) inherits the plain contract ABC and is
reachable through ``service_interface::quality_service::*`` keys. The matching
KB JSONs live at ``ananta/knowledge_base/processes/quality_service/*.json``
(dual-write per the D1 mandate).

Every verb is EDGE (returns structured data) and carries BOTH processor-
customization blocks on the decorator (structural half) — the companion JSON
supplies the prose half. ``requires_call_context=True`` logs the server-built
principal per gate run / repo read. ``run_gate`` / ``run_test`` are
``is_long_running`` — the full aggregate gate and smoke suite run for minutes.

Gate/smoke output is the platform's OWN toolchain output (ruff / radon / smoke
verdicts), not user data — every field is rated public (0.0) for exposure
scoring. The security boundary is the server-side NAME allowlist + repo-root
confinement, not per-field redaction.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from ananta.core.actions.action_metadata import (
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.services.service_interface_decorator import service_interface_process

if TYPE_CHECKING:
    from ananta.core.services.call_context import CallContext

PROVIDER = "quality_service"

_GATE_PARAM = ParameterMetadata(
    description=(
        "Allowlisted gate name from the server-side registry (see list_gates). "
        "Directly-runnable: 'code_quality' (the whole-tree aggregate), "
        "'whole_tree_integration', 'service_interface_ast', 'sql_access', "
        "'wint2_driver_import', 'wint2_vault_key_declaration'. Unknown or "
        "coherence-only names (god_class/radon_cc/radon_mi — run via "
        "'code_quality') are rejected with a typed error."
    ),
    required=True,
    type=ParameterType.STRING,
)
_SMOKE_PARAM = ParameterMetadata(
    description=(
        "Optional repo-relative path of ONE smoke to run; it must be present in "
        "the gate register (quality_gates/gate_smokes.txt) or the verb rejects "
        "it. Omit to run the full gate-eligible smoke suite."
    ),
    required=False,
    type=ParameterType.STRING,
)

# Every returned field is the platform's own toolchain output — public (0.0).


def _list_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Enumerated gate registry + smoke register.",
        properties={
            "gates": ParameterMetadata(
                type=ParameterType.LIST,
                description="Per-gate rows: name, kind, description, timeout_seconds, directly_runnable, run_via.",
            ),
            "smokes": ParameterMetadata(
                type=ParameterType.LIST,
                description="Repo-relative smoke paths from the tracked register.",
            ),
            "smoke_count": ParameterMetadata(
                type=ParameterType.INTEGER, description="Number of registered smokes."
            ),
        },
    )


def _run_return_schema(target_field: str, target_desc: str) -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Execution verdict with bounded output.",
        properties={
            target_field: ParameterMetadata(
                type=ParameterType.STRING, description=target_desc
            ),
            "passed": ParameterMetadata(
                type=ParameterType.BOOLEAN, description="True iff exit_code == 0."
            ),
            "skipped": ParameterMetadata(
                type=ParameterType.BOOLEAN,
                description=(
                    "True iff exit_code == 77 (the reserved SKIP convention: a "
                    "disclosed, non-blocking dependency gap, distinct from a "
                    "genuine failure). Always False for a static gate (run_gate) "
                    "or the whole suite (run_test with no smoke), which never "
                    "themselves exit 77 -- meaningful for run_test against a "
                    "single named smoke."
                ),
            ),
            "exit_code": ParameterMetadata(
                type=ParameterType.INTEGER,
                description="Process exit code (77 = disclosed skip, 124 = timeout).",
            ),
            "timed_out": ParameterMetadata(
                type=ParameterType.BOOLEAN, description="True iff the hard timeout fired."
            ),
            "summary": ParameterMetadata(
                type=ParameterType.STRING, description="Last meaningful line of output."
            ),
            "output": ParameterMetadata(
                type=ParameterType.STRING, description="Captured stdout+stderr (tail, bounded)."
            ),
            "truncated": ParameterMetadata(
                type=ParameterType.BOOLEAN, description="True iff output was size-capped."
            ),
            "output_chars_total": ParameterMetadata(
                type=ParameterType.INTEGER, description="True total output length before capping."
            ),
        },
    )


class QualityServicePublicAPI(ABC):
    """AI-discoverable quality-gate + smoke execution surface.

    Access via: ``service_interface::quality_service::{verb}``
    """

    @service_interface_process(
        name="list_gates",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={},
        return_value_schema=_list_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="quality_gate_registry",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
        requires_call_context=True,
    )
    @abstractmethod
    def list_gates(
        self, *, call_context: CallContext | None = None
    ) -> dict[str, Any]:
        """Read-only enumeration of the server-side gate registry + smoke register."""

    @service_interface_process(
        name="run_gate",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={"gate": _GATE_PARAM},
        return_value_schema=_run_return_schema("gate", "The gate that was run."),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=True,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="quality_gate_run_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        requires_call_context=True,
    )
    @abstractmethod
    def run_gate(
        self, gate: str, *, call_context: CallContext | None = None
    ) -> dict[str, Any]:
        """Run ONE gate by allowlisted name; report pass/fail + bounded output."""

    @service_interface_process(
        name="run_test",
        provider=PROVIDER,
        is_discoverable=True,
        parameters={"smoke": _SMOKE_PARAM},
        return_value_schema=_run_return_schema("target", "'suite' or the single smoke path run."),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=True,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="quality_test_run_result",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
        requires_call_context=True,
    )
    @abstractmethod
    def run_test(
        self, smoke: str | None = None, *, call_context: CallContext | None = None
    ) -> dict[str, Any]:
        """Run the gate-eligible smoke suite, or one registered smoke by path."""
