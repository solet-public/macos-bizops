"""Real bootstrap-route regression checks for operation-owned probes."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from bootstrap_adapter.routes import execute_adapter_request  # noqa: E402

from solet_manager import (
    operation_executor,  # noqa: E402
    preview_engine,  # noqa: E402
)
from solet_manager.adapters import (  # noqa: E402
    OperationRequest,
    OperationResult,
    PlannedAction,
)
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.doctor_inference_qualification import (  # noqa: E402
    collect_inference_pre_probe_timeout_advisories,
    collect_inference_probe_advisories,
)
from solet_manager.errors import StateConflictError  # noqa: E402
from solet_manager.flow import PlannedOperation, SetupPlan  # noqa: E402
from solet_manager.inference_probe_policy import (  # noqa: E402
    ADVISORY_ERROR_KIND,
    advisory_inference_probe_result,
)
from solet_manager.models import CheckpointStatus, CommandResult, ExitCode, JsonValue  # noqa: E402
from solet_manager.operation_records import operation_probe_request  # noqa: E402
from solet_manager.paths import ManagerPaths  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction, load_transaction  # noqa: E402

_CONTRACTS = Path(__file__).resolve().parents[2] / "plugins/github_midwife_plugin/knowledge_base"
_STAGE_ID = "system_dependencies"
_EXPECTED_PRE_APPLY_PROBES = (
    ("request_homebrew_install", "homebrew_available", "bootstrap::homebrew.probe"),
    ("install_python_runtime", "python_version_valid", "bootstrap::python.probe_version"),
    (
        "build_instance_environment",
        "instance_environment_dependency_closure_valid",
        "bootstrap::environment.probe_dependency_closure",
    ),
    (
        "install_postgresql",
        "postgres_binary_version_valid",
        "bootstrap::postgres.probe_version",
    ),
    (
        "configure_postgresql",
        "postgres_role_policy_valid",
        "bootstrap::postgres.probe_role_policy",
    ),
)


def operation_owned_probe_requests_use_probe_identity() -> None:
    """Drive every bootstrap pre-apply probe through the real closed route."""

    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        transaction = _transaction(bundle, root / "target")
        observed: list[tuple[str, str, str]] = []
        recorded_owner = False
        for operation_id, probe_id, probe_ref in _EXPECTED_PRE_APPLY_PROBES:
            operation = _operation(bundle, operation_id)
            runner, request = operation_probe_request(
                transaction,
                bundle,
                operation,
                probe_id=probe_id,
                purpose="pre_apply",
                attempt=1,
            )
            observed.append((operation_id, request.operation_id, request.operation_ref))
            _require(runner == "bootstrap", f"{operation_id} pre-apply probe uses bootstrap")
            _require(
                request.operation_id == probe_id
                and request.operation_ref == probe_ref
                and request.public_inputs == {},
                f"{operation_id} emits its probe's exact adapter identity and inputs",
            )
            raw_result = execute_adapter_request(
                request.to_dict(),
                runner=_unavailable_runner,
                which=_unavailable_which,
                now=_fixed_now,
            )
            _require(
                raw_result["error_kind"] not in {"adapter_missing", "adapter_protocol_error"},
                f"{operation_id} probe reaches its registered bootstrap route",
            )
            if operation_id == "request_homebrew_install":
                _assert_parent_journal_owner(root, transaction, operation, raw_result, request)
                recorded_owner = True
        _assert_pgvector_blocked_evidence(bundle, transaction, root)
        _require(
            observed == list(_EXPECTED_PRE_APPLY_PROBES),
            "all bootstrap probe triples are exact",
        )
        _require(recorded_owner, "parent-operation journal ownership was exercised")
        _assert_model_qualification_projections(bundle, transaction)


def _assert_model_qualification_projections(
    bundle: ContractBundle,
    transaction: Transaction,
) -> None:
    selections = (
        (
            "configure_lm_studio_embeddings",
            "embedding_model",
            "fixture-embedding-model",
            "embedding_model_qualification",
            "setup::models.qualify_embedding",
        ),
    )
    for operation_id, decision_id, selected, probe_id, probe_ref in selections:
        _assert_model_qualification_projection(
            bundle,
            transaction,
            operation_id=operation_id,
            decision_id=decision_id,
            selected=selected,
            probe_id=probe_id,
            probe_ref=probe_ref,
        )


def _assert_model_qualification_projection(
    bundle: ContractBundle,
    transaction: Transaction,
    *,
    operation_id: str,
    decision_id: str,
    selected: str,
    probe_id: str,
    probe_ref: str,
) -> None:
    operation = _operation(bundle, operation_id)
    resolved = replace(
        transaction,
        answers={
            "decisions": {
                "autostart": "disabled",
                "coding_agents": ["codex"],
                decision_id: selected,
            }
        },
    )
    runner, request = operation_probe_request(
        resolved,
        bundle,
        operation,
        probe_id=probe_id,
        purpose="post_apply",
        attempt=1,
    )
    _require(
        runner == "hydration"
        and request.operation_ref == probe_ref
        and request.public_inputs == {"candidate_id": selected},
        f"{operation_id} postcondition projects only the resolved candidate selection",
    )
    for label, answers in (
        ("missing", {"decisions": {}}),
        ("invalid", {"decisions": {decision_id: [selected]}}),
    ):
        try:
            operation_probe_request(
                replace(resolved, answers=answers),
                bundle,
                operation,
                probe_id=probe_id,
                purpose="post_apply",
                attempt=1,
            )
        except StateConflictError:
            continue
        raise AssertionError(f"{label} {decision_id} selection must fail closed")


def _assert_pgvector_blocked_evidence(
    bundle: ContractBundle,
    transaction: Transaction,
    root: Path,
) -> None:
    operation = _operation(bundle, "install_postgresql")
    _runner, request = operation_probe_request(
        transaction,
        bundle,
        operation,
        probe_id="pgvector_ready",
        purpose="post_apply",
        attempt=1,
    )
    prefix = root / "pgvector-prefix"

    def runner(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        joined = " ".join(command)
        if command[0] == "/fixture/brew":
            if command[1:] == ["--version"]:
                return subprocess.CompletedProcess(command, 0, "Homebrew 4.0\n", "")
            if command[1:] == ["list", "--formula"]:
                return subprocess.CompletedProcess(command, 0, "postgresql@17\npgvector\n", "")
            if command[1:] == ["--prefix", "postgresql@17"]:
                return subprocess.CompletedProcess(command, 0, f"{prefix}\n", "")
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "psql (PostgreSQL) 17.5\n", "")
        if command[0].endswith("pg_isready"):
            return subprocess.CompletedProcess(command, 0, "accepting connections\n", "")
        if "pg_available_extensions" in joined:
            return subprocess.CompletedProcess(command, 0, "1\n", "")
        if "pg_extension" in joined:
            return subprocess.CompletedProcess(command, 0, "0\n", "")
        return subprocess.CompletedProcess(command, 1, "", "unhandled fixture command")

    raw_result = execute_adapter_request(
        request.to_dict(),
        runner=runner,
        which=lambda name: "/fixture/brew" if name == "brew" else name,
        now=_fixed_now,
    )
    evidence = raw_result["evidence"]
    _require(
        raw_result["checkpoint_status"] == "blocked"
        and any(item["observed"] != item["expected"] for item in evidence),
        "blocked pgvector route emits evidence that disagrees with its expected state",
    )


def planned_action_drift_uses_operation_route() -> None:
    """Exercise the route-identity and remediation boundary without owner-mapping fakes."""

    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        _postgres_route_split_regressions(bundle, root)
        _approved_inventory_action_regression(bundle, root)
        _true_drift_regression(bundle, root)
        _contract_route_closure_and_probe_purity(bundle, root)
        _post_apply_block_regression(bundle, root)


class _RouteSplitAdapter:
    """Explicit parent-route versus declared-probe fixture for this drift class."""

    def __init__(
        self,
        operation: PlannedOperation,
        *,
        blocked_precondition: bool = False,
        error_kind: str = "fixture_precondition_blocked",
        pre_apply_revision: str = "approved",
        blocked_postcondition_probe_id: str | None = None,
        preconditions_verified: bool = False,
        inference_pre_probe_timeout: bool = False,
        inference_inventory_error_kind: str | None = None,
        inference_preview_error_kind: str | None = None,
    ) -> None:
        self.operation = operation
        self.blocked_precondition = blocked_precondition
        self.error_kind = error_kind
        self.pre_apply_revision = pre_apply_revision
        self.blocked_postcondition_probe_id = blocked_postcondition_probe_id
        self.preconditions_verified = preconditions_verified
        self.inference_pre_probe_timeout = inference_pre_probe_timeout
        self.inference_inventory_error_kind = inference_inventory_error_kind
        self.inference_preview_error_kind = inference_preview_error_kind
        self.requests: list[tuple[str, OperationRequest]] = []
        self.mutated_request_ids: list[str] = []
        self.apply_calls = 0
        self.parent_pre_apply_calls = 0

    def __call__(
        self,
        _registry: object,
        *,
        runner: str,
        request: OperationRequest,
    ) -> OperationResult:
        self.requests.append((runner, request))
        if request.phase == "apply":
            self.mutated_request_ids.append(request.request_id)
            self.apply_calls += 1
            return _result(request, CheckpointStatus.APPLIED)
        if request.probe_purpose == "post_apply":
            if request.operation_id == self.blocked_postcondition_probe_id:
                return _result(
                    request,
                    CheckpointStatus.BLOCKED,
                    error_kind=self.error_kind,
                )
            return _result(request, CheckpointStatus.VERIFIED)
        if request.operation_id == self.operation.operation_id:
            self.parent_pre_apply_calls += 1
            if (
                request.probe_purpose == "preview"
                and self.inference_preview_error_kind is not None
            ):
                return _result(
                    request,
                    CheckpointStatus.FAILED,
                    error_kind=self.inference_preview_error_kind,
                    timed_out=self.inference_preview_error_kind == "adapter_timeout",
                    retry_safe=False,
                    exit_code=None,
                )
            if self.inference_pre_probe_timeout and self.parent_pre_apply_calls == 1:
                return _result(
                    request,
                    CheckpointStatus.FAILED,
                    error_kind="adapter_timeout",
                    timed_out=True,
                    retry_safe=False,
                    exit_code=None,
                )
            if (
                self.inference_inventory_error_kind is not None
                and self.parent_pre_apply_calls == 2
            ):
                return _result(
                    request,
                    CheckpointStatus.FAILED,
                    actions=(_planned_action(self.operation, self.pre_apply_revision),),
                    error_kind=self.inference_inventory_error_kind,
                    timed_out=self.inference_inventory_error_kind == "adapter_timeout",
                    retry_safe=False,
                    exit_code=None,
                )
            return _result(
                request,
                CheckpointStatus.PENDING,
                actions=(_planned_action(self.operation, self.pre_apply_revision),),
            )
        if request.operation_id in self.operation.precondition_probe_ids:
            if self.preconditions_verified:
                return _result(request, CheckpointStatus.VERIFIED)
            if self.blocked_precondition:
                return _result(
                    request,
                    CheckpointStatus.BLOCKED,
                    error_kind=self.error_kind,
                )
            return _result(request, CheckpointStatus.PENDING)
        return _result(request, CheckpointStatus.VERIFIED)


class _FixtureRegistry:
    def refresh_base_python(self) -> None:
        pass


def _postgres_route_split_regressions(bundle: ContractBundle, root: Path) -> None:
    _install_postgresql_route_split_regression(bundle, root)
    _configure_postgresql_route_split_regression(bundle, root)
    _no_drift_controls(bundle, root)


def _install_postgresql_route_split_regression(
    bundle: ContractBundle,
    root: Path,
) -> None:
    install = _operation(bundle, "install_postgresql")
    blocked_probe = "postgres_binary_version_valid"
    original_remediation = bundle.probes[blocked_probe]["remediation_operation_refs"]
    bundle.probes[blocked_probe]["remediation_operation_refs"] = []
    try:
        terminal_adapter = _RouteSplitAdapter(
            install,
            blocked_precondition=True,
            error_kind="postgres_probe_not_verified",
        )
        terminal = _run_pending_case(
            bundle,
            root / "postgres-terminal",
            install,
            terminal_adapter,
        )
    finally:
        bundle.probes[blocked_probe]["remediation_operation_refs"] = original_remediation
    _require(
        terminal.terminal_result is not None
        and terminal.terminal_result.error_kind == "postgres_probe_not_verified"
        and terminal.transaction.result_kind != "probe_drift",
        "install_postgresql preserves its declared blocked error when remediation does not apply",
    )
    _require(
        not any(request.phase == "apply" for _runner, request in terminal_adapter.requests),
        "terminal install_postgresql precondition does not apply",
    )

    remediable_adapter = _RouteSplitAdapter(
        install,
        blocked_precondition=True,
        error_kind="postgres_probe_not_verified",
    )
    remediable = _run_pending_case(
        bundle,
        root / "postgres-remediable",
        install,
        remediable_adapter,
    )
    _require(
        remediable.terminal_result is None,
        "install_postgresql blocked precondition does not become probe_drift",
    )
    _require(
        any(request.phase == "apply" for _runner, request in remediable_adapter.requests),
        "install_postgresql remediation named by its probe reaches apply",
    )
    _require(
        all(
            attempt["operation_id"] == install.operation_id
            for attempt in remediable.transaction.operation_attempts
        ),
        "declared probe and operation-route inventory preserve parent journal attribution",
    )


def _configure_postgresql_route_split_regression(
    bundle: ContractBundle,
    root: Path,
) -> None:
    configure = _operation(bundle, "configure_postgresql")
    configure_adapter = _RouteSplitAdapter(
        configure,
        blocked_precondition=True,
        error_kind="postgres_role_policy_valid",
    )
    configured = _run_pending_case(
        bundle,
        root / "postgres-configure",
        configure,
        configure_adapter,
    )
    _require(
        configured.terminal_result is None
        and any(request.phase == "apply" for _runner, request in configure_adapter.requests),
        "configure_postgresql uses its parent route and remediates its declared policy probe",
    )
    _require(
        configure_adapter.error_kind
        in [attempt["error_kind"] for attempt in configured.transaction.operation_attempts],
        "configure_postgresql journals its declared policy error under the parent operation",
    )


def _no_drift_controls(bundle: ContractBundle, root: Path) -> None:
    environment = _operation(bundle, "build_instance_environment")
    environment_adapter = _RouteSplitAdapter(environment)
    environment_result = _run_pending_case(
        bundle,
        root / "dependency-closure",
        environment,
        environment_adapter,
    )
    _require(
        environment_result.terminal_result is None
        and any(request.phase == "apply" for _runner, request in environment_adapter.requests),
        "build_instance_environment remains a no-drift apply control",
    )

    connector = _operation(bundle, "configure_salesforce")
    connector_adapter = _RouteSplitAdapter(connector)
    connector_result = _run_pending_case(
        bundle,
        root / "connector",
        connector,
        connector_adapter,
    )
    _require(
        connector_result.terminal_result is None
        and any(request.phase == "apply" for _runner, request in connector_adapter.requests),
        "connector operation compares its parent inventory rather than its actionless probe",
    )
    _inference_probe_policy_regression(bundle, root)


def _inference_probe_policy_regression(
    bundle: ContractBundle,
    root: Path,
) -> None:
    """Every inference-probe consumer records timeout and empty output as advisory."""

    inference = _operation(bundle, "configure_lm_studio_inference")
    inference = replace(inference, public_inputs={"model": "fixture-inference"})
    adapter = _RouteSplitAdapter(inference, inference_pre_probe_timeout=True)
    outcome = _run_pending_case(bundle, root / "inference-pre-probe-timeout", inference, adapter)
    timeout_attempt = outcome.transaction.operation_attempts[0]
    advisories = collect_inference_pre_probe_timeout_advisories(outcome.transaction)
    _require(
        outcome.terminal_result is None
        and adapter.apply_calls == 1
        and outcome.transaction.operation_statuses[inference.operation_id]
        is CheckpointStatus.VERIFIED,
        "inference pre-probe timeout keeps the served-model configuration on its apply path",
    )
    _require(
        timeout_attempt["phase"] == "pre_probe"
        and timeout_attempt["checkpoint_status"] == CheckpointStatus.VERIFIED.value
        and timeout_attempt["error_kind"] == ADVISORY_ERROR_KIND
        and timeout_attempt["timed_out"] is True,
        "inference pre-probe timeout remains durably recorded as an advisory",
    )
    _require(
        len(advisories) == 1
        and advisories[0]["check_id"] == "doctor::inference_configuration_pre_probe"
        and advisories[0]["status"] == "warn"
        and advisories[0]["blocking"] is False
        and advisories[0]["observed"] == {
            "checkpoint_status": CheckpointStatus.VERIFIED.value,
            "error_kind": ADVISORY_ERROR_KIND,
            "timed_out": True,
            "duration_ms": 0,
        },
        "doctor renders the retained inference pre-probe outcome as a non-blocking advisory",
    )
    _all_inference_probe_kinds_are_advisory(bundle, root)
    _preview_and_inventory_inference_probe_regressions(bundle, root)


def _preview_and_inventory_inference_probe_regressions(
    bundle: ContractBundle,
    root: Path,
) -> None:
    inference = replace(
        _operation(bundle, "configure_lm_studio_inference"),
        public_inputs={"model": "fixture-inference"},
    )
    transaction = _transaction(bundle, root / "preview-target")
    preview = SetupPlan(transaction.answers, (inference,), (), ())
    for error_kind in ("adapter_timeout", "adapter_empty_content"):
        preview_adapter = _RouteSplitAdapter(
            inference,
            inference_preview_error_kind=error_kind,
        )
        with patch.object(preview_engine, "invoke_adapter", preview_adapter):
            preview_results, preview_failures = preview_engine._probe_operations(
                transaction,
                bundle,
                preview,
                _FixtureRegistry(),
            )
        preview_result = preview_results[inference.operation_id]
        _require(
            preview_result.checkpoint_status is CheckpointStatus.VERIFIED
            and preview_result.error_kind == ADVISORY_ERROR_KIND
            and not preview_failures,
            f"preview {error_kind} is advisory for the selected served inference model",
        )
        inventory_adapter = _RouteSplitAdapter(
            inference,
            inference_pre_probe_timeout=True,
            inference_inventory_error_kind=error_kind,
        )
        outcome = _run_pending_case(
            bundle,
            root / f"inventory-{error_kind}",
            inference,
            inventory_adapter,
        )
        _require(
            outcome.terminal_result is None
            and inventory_adapter.apply_calls == 1
            and outcome.transaction.operation_statuses[inference.operation_id]
            is CheckpointStatus.VERIFIED,
            f"repeat inventory {error_kind} cannot block inference configuration",
        )


def _all_inference_probe_kinds_are_advisory(bundle: ContractBundle, root: Path) -> None:
    transaction = _transaction(bundle, root / "policy-target")
    answers = transaction.answers
    controls = (
        ("pre_probe", "setup::models.configure_lm_studio_inference", "pre_apply"),
        ("post_apply", "setup::models.configure_lm_studio_inference", "post_apply"),
        ("qualification", "setup::models.qualify_structured_actions", "decision_qualification"),
        ("representative", "setup::models.qualify_representative_inference", "completion"),
        ("identity_completion", "service_interface::inference_service.qualify", "completion"),
        ("identity_stage_entry", "service_interface::inference_service.qualify", "stage_entry"),
        ("identity_stage_exit", "service_interface::inference_service.qualify", "stage_exit"),
    )
    for label, operation_ref, purpose in controls:
        request = OperationRequest(
            request_id="00000000-0000-4000-8000-000000000007",
            operation_id=f"inference.{label}",
            operation_ref=operation_ref,
            phase="probe",
            probe_purpose=purpose,
            attempt=1,
            name="operation-probe",
            target=root / label,
            flow_id=bundle.flow_id,
            flow_source_revision=bundle.source_revision,
            answers_fingerprint="sha256:" + "a" * 64,
            approval_fingerprint=None,
            dry_run=True,
            timeout_seconds=30,
            public_inputs={},
        )
        for error_kind, timed_out in (("adapter_timeout", True), ("adapter_empty_content", False)):
            observed = advisory_inference_probe_result(
                answers,
                request,
                _result(
                    request,
                    CheckpointStatus.FAILED,
                    error_kind=error_kind,
                    timed_out=timed_out,
                    retry_safe=False,
                    exit_code=None,
                ),
            )
            _require(
                observed.checkpoint_status is CheckpointStatus.VERIFIED
                and observed.error_kind == ADVISORY_ERROR_KIND,
                f"{label} {error_kind} is advisory for the selected served inference model",
            )
    exact_refusal = advisory_inference_probe_result(
        answers,
        request,
        _result(request, CheckpointStatus.FAILED, error_kind="inference_model_identity_mismatch"),
    )
    embedding = advisory_inference_probe_result(
        answers,
        replace(request, operation_ref="setup::models.qualify_embedding"),
        _result(request, CheckpointStatus.FAILED, error_kind="adapter_timeout"),
    )
    _require(
        exact_refusal.checkpoint_status is CheckpointStatus.FAILED
        and embedding.checkpoint_status is CheckpointStatus.FAILED,
        "exact inference identity refusal and embedding qualification remain decisive",
    )
    journal_attempts = tuple(
        {
            "operation_id": f"inference.{label}",
            "checkpoint_status": CheckpointStatus.VERIFIED.value,
            "error_kind": ADVISORY_ERROR_KIND,
            "timed_out": label.endswith("timeout"),
            "duration_ms": 0,
        }
        for label, _operation_ref, _purpose in controls[:5]
    )
    stage_attempts = tuple(
        {
            "probe_id": f"inference.{label}",
            "checkpoint_status": CheckpointStatus.VERIFIED.value,
            "error_kind": ADVISORY_ERROR_KIND,
            "timed_out": False,
            "duration_ms": 0,
        }
        for label, _operation_ref, _purpose in controls[5:]
    )
    doctor_advisories = collect_inference_probe_advisories(
        replace(
            transaction,
            operation_attempts=journal_attempts,
            stage_probe_attempts=stage_attempts,
        )
    )
    _require(
        len(doctor_advisories) == len(controls)
        and all(
            item["status"] == "warn" and item["blocking"] is False
            for item in doctor_advisories
        ),
        "doctor exposes every journaled inference probe result as advisory",
    )


def _approved_inventory_action_regression(
    bundle: ContractBundle,
    root: Path,
) -> None:
    """An approved action must apply even when a declared probe already verifies."""

    cases = (
        (
            "embedding-qualified",
            _operation(bundle, "configure_lm_studio_embeddings"),
            True,
            CheckpointStatus.PENDING,
        ),
        (
            "inference-qualified",
            _operation(bundle, "configure_lm_studio_inference"),
            True,
            CheckpointStatus.PENDING,
        ),
        (
            "operation-fallback",
            _operation(bundle, "open_background_items_settings"),
            False,
            CheckpointStatus.PENDING,
        ),
        (
            "embedding-resumed-awaiting-user",
            _operation(bundle, "configure_lm_studio_embeddings"),
            True,
            CheckpointStatus.AWAITING_USER,
        ),
    )
    for label, operation, preconditions_verified, prior_status in cases:
        adapter = _RouteSplitAdapter(
            operation,
            preconditions_verified=preconditions_verified,
        )
        _run_pending_case(
            bundle,
            root / label,
            operation,
            adapter,
            prior_status=prior_status,
        )
        _require(
            adapter.apply_calls == 1,
            f"{label} preserves its approved pending inventory action",
        )


def _true_drift_regression(bundle: ContractBundle, root: Path) -> None:
    operation = _operation(bundle, "install_postgresql")
    adapter = _RouteSplitAdapter(operation, pre_apply_revision="changed")
    approval = "sha256:" + "a" * 64
    refreshed = "sha256:" + "b" * 64
    outcome = _run_pending_case(
        bundle,
        root / "true-drift",
        operation,
        adapter,
        approval=approval,
        refreshed=refreshed,
    )
    _require(
        outcome.terminal_result is not None
        and outcome.terminal_result.error_kind == "probe_drift"
        and outcome.terminal_result.data["approval_fingerprint"] == refreshed,
        "changed parent action reports probe_drift with the refreshed approval fingerprint",
    )
    _require(
        refreshed != approval
        and not any(request.phase == "apply" for _runner, request in adapter.requests),
        "true parent-route drift cannot apply or return its rejected fingerprint",
    )
    reloaded = load_transaction(
        root / "true-drift" / "state" / "transactions" / "operation-probe.json"
    )
    _require(
        reloaded is not None
        and reloaded.operation_statuses[operation.operation_id]
        is CheckpointStatus.AWAITING_USER
        and reloaded.result_kind == "probe_drift"
        and reloaded.operation_attempts[-1]["phase"] == "pre_probe"
        and reloaded.operation_attempts[-1]["checkpoint_status"]
        != CheckpointStatus.AWAITING_USER.value,
        "probe-drift refusal journal reloads with its no-action pre-probe attempt",
    )

    same_fingerprint_adapter = _RouteSplitAdapter(operation, pre_apply_revision="changed")
    _raises_state_conflict(
        lambda: _run_pending_case(
            bundle,
            root / "same-fingerprint",
            operation,
            same_fingerprint_adapter,
            approval=approval,
            refreshed=approval,
        ),
        "probe_drift rejects a refreshed fingerprint equal to the rejected approval",
    )


def _contract_route_closure_and_probe_purity(
    bundle: ContractBundle,
    root: Path,
) -> None:
    runners: set[str] = set()
    for operation_id in _operation_ids_with_preconditions(bundle):
        runners.add(_assert_operation_route_reprobe(bundle, root, operation_id))
    _require(
        runners == {"bootstrap", "external_cli", "genesis", "hydration", "platform_process"},
        "every operation runner was exercised",
    )


def _operation_ids_with_preconditions(bundle: ContractBundle) -> tuple[str, ...]:
    operation_ids: list[str] = []
    for operation_id, definition in bundle.operations.items():
        idempotency = definition["idempotency"]
        if not isinstance(idempotency, dict):
            raise AssertionError(f"{operation_id} lacks idempotency")
        preconditions = idempotency["precondition_probe_refs"]
        if not isinstance(preconditions, list):
            raise AssertionError(f"{operation_id} preconditions are invalid")
        if preconditions:
            operation_ids.append(operation_id)
    return tuple(operation_ids)


def _assert_operation_route_reprobe(
    bundle: ContractBundle,
    root: Path,
    operation_id: str,
) -> str:
    operation = _operation(bundle, operation_id)
    adapter = _RouteSplitAdapter(operation)
    outcome = _run_pending_case(
        bundle,
        root / f"route-{operation_id}",
        operation,
        adapter,
    )
    parent_probes = _parent_pre_apply_probes(adapter, operation)
    _require(
        outcome.terminal_result is None
        and parent_probes
        and all(
            _is_safe_parent_reprobe(operation, runner, request)
            for runner, request in parent_probes
        ),
        f"{operation_id} drift inventory re-probes its own route as a dry run",
    )
    _require(
        all(
            request.request_id not in adapter.mutated_request_ids
            for _runner, request in parent_probes
        ),
        f"{operation_id} parent pre-apply probe is side-effect-free in its runner fixture",
    )
    return operation.runner


def _post_apply_block_regression(bundle: ContractBundle, root: Path) -> None:
    """A legacy bad postcondition must not retain a stale terminal result kind."""

    repaired_operation = _operation(bundle, "install_postgresql")
    legacy_operation = replace(
        repaired_operation,
        postcondition_probe_ids=("postgres_binary_version_valid", "pgvector_ready"),
    )
    legacy_adapter = _RouteSplitAdapter(
        legacy_operation,
        error_kind="postgres_probe_not_verified",
        blocked_postcondition_probe_id="pgvector_ready",
    )
    blocked = _run_pending_case(
        bundle,
        root / "post-apply-block",
        legacy_operation,
        legacy_adapter,
        result_kind="probe_drift",
    )
    _require(
        blocked.terminal_result is not None
        and blocked.terminal_result.error_kind == "postgres_probe_not_verified",
        "blocked declared postcondition retains its probe error kind",
    )
    _require(
        any(
            attempt["phase"] == "apply"
            and attempt["checkpoint_status"] == CheckpointStatus.APPLIED.value
            for attempt in blocked.transaction.operation_attempts
        ),
        "post-apply block retains the successful apply attempt",
    )
    _require(
        blocked.transaction.result_kind is None,
        "post-apply block clears stale probe_drift result_kind",
    )

    resumed_adapter = _RouteSplitAdapter(
        repaired_operation,
        preconditions_verified=True,
    )
    resumed = _run_existing_pending_case(
        bundle,
        root / "post-apply-resume",
        repaired_operation,
        blocked.transaction,
        resumed_adapter,
    )
    _require(
        resumed.terminal_result is None
        and not resumed_adapter.mutated_request_ids
        and len(legacy_adapter.mutated_request_ids) == 1,
        "a repaired contract resumes a legacy post-apply block without re-applying",
    )


def _parent_pre_apply_probes(
    adapter: _RouteSplitAdapter,
    operation: PlannedOperation,
) -> list[tuple[str, OperationRequest]]:
    return [
        (runner, request)
        for runner, request in adapter.requests
        if request.operation_id == operation.operation_id
        and request.phase == "probe"
        and request.probe_purpose == "pre_apply"
    ]


def _is_safe_parent_reprobe(
    operation: PlannedOperation,
    runner: str,
    request: OperationRequest,
) -> bool:
    return (
        runner == operation.runner
        and request.operation_ref == operation.operation_ref
        and request.dry_run
        and request.approval_fingerprint is None
    )


def _run_pending_case(
    bundle: ContractBundle,
    root: Path,
    operation: PlannedOperation,
    adapter: _RouteSplitAdapter,
    *,
    approval: str = "sha256:" + "a" * 64,
    refreshed: str = "sha256:" + "b" * 64,
    result_kind: str | None = None,
    prior_status: CheckpointStatus = CheckpointStatus.PENDING,
) -> operation_executor.OperationOutcome:
    root.mkdir(parents=True)
    transaction = _transaction(bundle, root / "target").bind_operations(
        {operation.operation_id: operation.stage_id}
    ).approve(approval)
    if prior_status is not CheckpointStatus.PENDING:
        transaction = transaction.with_operation_status(operation.operation_id, prior_status)
    if result_kind is not None:
        transaction = transaction.with_result_kind(result_kind)
    paths = ManagerPaths(root / "config", root / "state", root / "cache")
    paths.transactions_dir.mkdir(parents=True)
    paths.transactions_dir.chmod(0o700)
    approved_action = _planned_action(operation, "approved")
    approved_actions: dict[str, list[JsonValue]] = {
        operation.operation_id: [
            {"operation_id": operation.operation_id, **approved_action.to_dict()}
        ]
    }
    with patch.object(operation_executor, "invoke_adapter", adapter):
        return operation_executor._run_pending_operation(
            bundle=bundle,
            operation=operation,
            transaction=transaction,
            approved_actions=approved_actions,
            registry=_FixtureRegistry(),
            paths=paths,
            refresh_preview=lambda: CommandResult(
                kind="create",
                status="preview_ready",
                message="fixture refresh",
                exit_code=ExitCode.OK,
                error_kind=None,
                repair=None,
                data={"approval_fingerprint": refreshed},
            ),
        )


def _run_existing_pending_case(
    bundle: ContractBundle,
    root: Path,
    operation: PlannedOperation,
    transaction: Transaction,
    adapter: _RouteSplitAdapter,
) -> operation_executor.OperationOutcome:
    root.mkdir(parents=True)
    paths = ManagerPaths(root / "config", root / "state", root / "cache")
    paths.transactions_dir.mkdir(parents=True)
    paths.transactions_dir.chmod(0o700)
    approved_action = _planned_action(operation, "approved")
    approved_actions: dict[str, list[JsonValue]] = {
        operation.operation_id: [
            {"operation_id": operation.operation_id, **approved_action.to_dict()}
        ]
    }
    with patch.object(operation_executor, "invoke_adapter", adapter):
        return operation_executor._run_pending_operation(
            bundle=bundle,
            operation=operation,
            transaction=transaction,
            approved_actions=approved_actions,
            registry=_FixtureRegistry(),
            paths=paths,
            refresh_preview=lambda: CommandResult(
                kind="create",
                status="preview_ready",
                message="fixture refresh",
                exit_code=ExitCode.OK,
                error_kind=None,
                repair=None,
                data={"approval_fingerprint": "sha256:" + "b" * 64},
            ),
        )


def _planned_action(operation: PlannedOperation, revision: str) -> PlannedAction:
    return PlannedAction(
        id=f"fixture.{operation.operation_id}",
        title=f"Apply {operation.operation_id}",
        mutation_kind="fixture_mutation",
        target=f"$TARGET/{operation.operation_id}?revision={revision}",
        requires_confirmation=True,
        condition_or_evidence_ref=f"{operation.operation_id}.required",
    )


def _result(
    request: OperationRequest,
    status: CheckpointStatus,
    *,
    actions: tuple[PlannedAction, ...] = (),
    error_kind: str | None = None,
    timed_out: bool = False,
    retry_safe: bool = True,
    exit_code: int | None = 0,
) -> OperationResult:
    return OperationResult(
        request_id=request.request_id,
        operation_id=request.operation_id,
        phase=request.phase,
        probe_purpose=request.probe_purpose,
        checkpoint_status=status,
        error_kind=error_kind,
        retry_safe=retry_safe,
        exit_code=exit_code,
        timed_out=timed_out,
        duration_ms=0,
        stdout="",
        stderr="",
        planned_actions=actions,
        discovered_candidates=(),
        evidence=(),
        repair="fixture repair" if error_kind else None,
    )


def _raises_state_conflict(callback: object, message: str) -> None:
    try:
        callback()  # type: ignore[operator]
    except Exception as exc:
        _require(
            exc.__class__.__name__ == "StateConflictError",
            f"{message}; got {exc!r}",
        )
    else:
        raise AssertionError(message)


def _transaction(bundle: ContractBundle, target: Path) -> Transaction:
    target.mkdir()
    seed = SeedLock(
        "https://example.invalid/seed.git",
        "fixture",
        "a" * 40,
        "b" * 40,
        "c" * 64,
        "fixture",
    )
    return Transaction.create(
        name="operation-probe",
        target=target,
        input_fingerprint="sha256:" + "d" * 64,
        answers={
            "decisions": {
                "autostart": "disabled",
                "coding_agents": ["codex"],
                "embedding_model": "fixture-embedding",
                "inference_model": "fixture-inference",
                "embeddings_implementation": "lm_studio",
                "inference_implementation": "lm_studio",
            },
            "public_inputs": {"lm_studio_base_url": "http://localhost:1234/v1"},
        },
        seed=seed,
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        flow_contract_digest="sha256:" + "e" * 64,
        stage_ids=(_STAGE_ID,),
        completion_probe_ids=(),
    )


def _operation(bundle: ContractBundle, operation_id: str) -> PlannedOperation:
    definition = bundle.operations[operation_id]
    idempotency = definition["idempotency"]
    if not isinstance(idempotency, dict):
        raise AssertionError(f"{operation_id} lacks an idempotency definition")
    preconditions = idempotency["precondition_probe_refs"]
    postconditions = idempotency["postcondition_probe_refs"]
    if not isinstance(preconditions, list) or not isinstance(postconditions, list):
        raise AssertionError(f"{operation_id} probe declarations are invalid")
    return PlannedOperation(
        stage_id=_STAGE_ID,
        operation_id=operation_id,
        operation_ref=str(definition["operation_ref"]),
        runner=str(definition["runner"]),
        risk=str(definition["risk"]),
        requires_confirmation=bool(definition["requires_confirmation"]),
        precondition_probe_ids=tuple(str(value) for value in preconditions),
        postcondition_probe_ids=tuple(str(value) for value in postconditions),
        public_inputs={"solet_name": "operation-probe"}
        if operation_id == "configure_postgresql"
        else {},
    )


def _assert_parent_journal_owner(
    root: Path,
    transaction: Transaction,
    operation: PlannedOperation,
    raw_result: dict[str, object],
    request: object,
) -> None:
    if not hasattr(request, "request_id"):
        raise AssertionError("operation probe request has no request identity")
    result = OperationResult.from_dict(raw_result, request)
    bound = transaction.bind_operations({operation.operation_id: operation.stage_id})
    paths = ManagerPaths(root / "config", root / "state", root / "cache")
    paths.transactions_dir.mkdir(parents=True)
    paths.transactions_dir.chmod(0o700)
    updated = operation_executor._record_operation_result(
        bound,
        operation,
        result,
        phase="pre_probe",
        attempt=1,
        paths=paths,
    )
    attempt = updated.operation_attempts[-1]
    _require(
        attempt["operation_id"] == operation.operation_id,
        "probe result journals under the parent operation, not the probe identity",
    )
    _require(
        Transaction.from_dict(updated.to_dict()).operation_attempts[-1]["operation_id"]
        == operation.operation_id,
        "journal validation retains the parent operation attribution",
    )


def _unavailable_runner(
    command: list[str], **_kwargs: object
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(command, 1, "", "")


def _unavailable_which(_name: str) -> str | None:
    return None


def _fixed_now() -> datetime:
    return datetime(2026, 8, 31, tzinfo=UTC)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)
