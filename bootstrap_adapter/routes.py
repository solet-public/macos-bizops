"""Closed route registry and dispatch for the pre-venv adapter."""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .dependency import dependency_closure_route
from .homebrew import HomebrewInstallError, run_homebrew_install_required
from .lm_studio import lm_studio_route
from .models import (
    FORMULA_KEG_MARKER,
    SUPPORTED_POSTGRES_MAJOR,
    AdapterError,
    AdapterRequestError,
    AdapterRuntime,
    Clock,
    PostgresObservation,
    Runner,
    Which,
)
from .postgres import (
    apply_postgres_configuration,
    pgvector_package_available,
    postgres_configure_actions,
    postgres_evidence,
    postgres_incompatibility,
    postgres_install_actions,
    postgres_observation,
    psql_scalar,
    role_policy_evidence,
    role_policy_observation,
    run_required,
    unsafe_policy_error,
)
from .protocol import (
    Request,
    evidence,
    protocol_error_result,
    resolve_brew_executable,
    result,
    run_public,
    utc_now,
    validate_request,
)

_ROUTES: dict[str, tuple[str, str]] = {
    "request_homebrew_install": ("setup::homebrew.request_install", "operation"),
    "install_python_runtime": ("setup::python.install_313", "operation"),
    "build_instance_environment": (
        "bootstrap::environment.ensure_dependency_closure",
        "operation",
    ),
    "install_codex_cli": ("setup::coding_agents.install_codex", "operation"),
    "install_claude_cli": ("setup::coding_agents.install_claude", "operation"),
    "install_node": ("setup::coding_agents.install_node", "operation"),
    "install_lm_studio": ("setup::lm_studio.install", "operation"),
    "start_lm_studio_server": ("setup::lm_studio.start_server", "operation"),
    "pull_lm_studio_embedding_model": ("setup::lm_studio.pull_embedding", "operation"),
    "load_lm_studio_embedding_model": ("setup::lm_studio.load_embedding", "operation"),
    "pull_lm_studio_inference_model": ("setup::lm_studio.pull_inference", "operation"),
    "load_lm_studio_inference_model": ("setup::lm_studio.load_inference", "operation"),
    "install_lm_studio_login_agent": ("setup::lm_studio.install_login_agent", "operation"),
    "install_postgresql": ("bootstrap::postgres.install", "operation"),
    "configure_postgresql": ("bootstrap::postgres.configure_solet", "operation"),
    "git_checkout_valid": ("setup::git.verify_checkout", "probe"),
    "minimum_physical_memory_valid": ("bootstrap::host.probe_physical_memory", "probe"),
    "python_version_valid": ("bootstrap::python.probe_version", "probe"),
    "instance_environment_dependency_closure_valid": (
        "bootstrap::environment.probe_dependency_closure",
        "probe",
    ),
    "homebrew_available": ("bootstrap::homebrew.probe", "probe"),
    "postgres_binary_version_valid": ("bootstrap::postgres.probe_version", "probe"),
    "postgres_ready": ("bootstrap::postgres.probe_ready", "probe"),
    "pgvector_package_available": ("bootstrap::postgres.probe_pgvector_package", "probe"),
    "postgres_role_policy_valid": ("bootstrap::postgres.probe_role_policy", "probe"),
    "pgvector_ready": ("bootstrap::postgres.probe_pgvector", "probe"),
    "lm_studio_cli_available": ("setup::lm_studio.cli_available", "probe"),
    "lm_studio_server_ready": ("setup::lm_studio.server_ready", "probe"),
    "lm_studio_embedding_artifact_present": ("setup::lm_studio.embedding_artifact_present", "probe"),
    "lm_studio_embedding_model_served": ("setup::lm_studio.embedding_model_served", "probe"),
    "lm_studio_inference_artifact_present": ("setup::lm_studio.inference_artifact_present", "probe"),
    "lm_studio_inference_model_served": ("setup::lm_studio.inference_model_served", "probe"),
    "lm_studio_login_agent_valid": ("setup::lm_studio.login_agent_valid", "probe"),
    "lm_studio_jit_disabled": ("setup::lm_studio.jit_disabled", "probe"),
}

_MINIMUM_PHYSICAL_MEMORY_BYTES = 24_000_000_000
_REPAIR_MAX_LENGTH = 2048
_POSTGRES_CONFIGURATION_REPAIR_GUIDANCE = (
    "Inspect the PostgreSQL policy and resume after repairing the failed action."
)


def _postgres_configuration_repair(error: AdapterError) -> str:
    """Keep subprocess diagnostics within the closed result-envelope repair cap."""

    context = f"{error}. "
    repair = f"{context}{_POSTGRES_CONFIGURATION_REPAIR_GUIDANCE}"
    if len(repair) <= _REPAIR_MAX_LENGTH:
        return repair
    marker = f"... [truncated, {len(repair)} chars total]"
    available_context = _REPAIR_MAX_LENGTH - len(marker) - len(
        _POSTGRES_CONFIGURATION_REPAIR_GUIDANCE
    )
    if available_context < 0:
        raise AdapterError("PostgreSQL repair guidance exceeds the envelope repair cap")
    return (
        f"{context[:available_context]}{marker}"
        f"{_POSTGRES_CONFIGURATION_REPAIR_GUIDANCE}"
    )

_LM_STUDIO_OPERATION_IDS = frozenset(
    {
        "install_lm_studio",
        "start_lm_studio_server",
        "pull_lm_studio_embedding_model",
        "load_lm_studio_embedding_model",
        "pull_lm_studio_inference_model",
        "load_lm_studio_inference_model",
        "install_lm_studio_login_agent",
        "lm_studio_cli_available",
        "lm_studio_server_ready",
        "lm_studio_embedding_artifact_present",
        "lm_studio_embedding_model_served",
        "lm_studio_inference_artifact_present",
        "lm_studio_inference_model_served",
        "lm_studio_login_agent_valid",
        "lm_studio_jit_disabled",
    }
)

_CODING_TOOL_ACQUISITIONS: dict[str, tuple[str, str, str]] = {
    "install_codex_cli": ("codex", "cask", "codex"),
    "install_claude_cli": ("claude", "cask", "claude-code"),
    "install_node": ("node", "formula", "node"),
}


def _homebrew_failure_result(
    request: Request,
    *,
    error_kind: str,
    evidence_items: list[dict[str, Any]],
    repair: str,
    error: AdapterError,
) -> dict[str, Any]:
    """Retain Homebrew command evidence in the existing closed result fields."""

    failed = result(
        request,
        status="failed",
        error_kind=error_kind,
        retry_safe=True,
        evidence_items=evidence_items,
        repair=repair,
    )
    if isinstance(error, HomebrewInstallError):
        failed["stdout"] = error.stdout
        failed["stderr"] = error.stderr
    return failed


def _selected_python(runtime: AdapterRuntime) -> str | None:
    if runtime.base_python is not None:
        return runtime.base_python
    if sys.version_info[:2] == (3, 13):
        return sys.executable
    return None


def _python_route(request: Request, runtime: AdapterRuntime) -> dict[str, Any]:
    selected = _selected_python(runtime)
    version = "unresolved"
    safe_runtime = False
    if selected is not None and FORMULA_KEG_MARKER not in selected:
        completed = run_public(runtime, [selected, "--version"])
        if completed is not None:
            version = f"{completed.stdout} {completed.stderr}".strip()
            safe_runtime = completed.returncode == 0 and version.startswith("Python 3.13")
    evidence_items = [
        evidence(
            runtime,
            evidence_id="python.runtime_version",
            kind="runtime_version",
            status="verified" if safe_runtime else "blocked",
            summary="The separately resolved long-lived interpreter was executed.",
            observed=version,
            # The manager's evidence hygiene bans the literal keg marker in
            # every public adapter string, so the expectation is described
            # without quoting it (first exercised by a real driven create,
            # 2026-08-25: the quoted marker was refused as adapter_protocol_error).
            expected="3.13.x outside the manager formula keg",
            source=selected or "unresolved",
        ),
    ]
    if not safe_runtime:
        return result(
            request,
            status="blocked",
            error_kind="python_runtime_resolution_required",
            retry_safe=False,
            evidence_items=evidence_items,
            repair="Resolve an eligible long-lived Python 3.13 outside the manager formula keg, then resume.",
        )
    return result(
        request,
        status="applied" if request["phase"] == "apply" else "verified",
        evidence_items=evidence_items,
    )


def _checkout_commands(target: Path, git: str) -> dict[str, list[str]]:
    prefix = [git, "-C", str(target)]
    return {
        "head": [*prefix, "rev-parse", "HEAD^{commit}"],
        "main": [*prefix, "rev-parse", "main^{commit}"],
        "tree": [*prefix, "rev-parse", "HEAD^{tree}"],
        "tracked_changes": [*prefix, "status", "--porcelain", "--untracked-files=no"],
        "tracked_contracts": [
            *prefix,
            "ls-files",
            "--error-unmatch",
            "bootstrap.py",
            "plugins/github_midwife_plugin/knowledge_base/macos_setup_flow.json",
            "plugins/github_midwife_plugin/knowledge_base/setup_adapter_envelope.schema.json",
        ],
    }


def _checkout_outputs(runtime: AdapterRuntime) -> dict[str, str] | None:
    candidate = runtime.which("git")
    git = candidate if candidate is not None and Path(candidate).is_absolute() else None
    if git is None:
        return None
    outputs: dict[str, str] = {}
    for label, command in _checkout_commands(runtime.target, git).items():
        completed = run_public(runtime, command)
        if completed is None or completed.returncode != 0:
            return None
        outputs[label] = completed.stdout.strip()
    return outputs


def _checkout_valid(outputs: dict[str, str], revision: str) -> bool:
    return outputs["head"] == revision and outputs["main"] == revision and re.fullmatch(r"[0-9a-f]{40}", outputs["tree"]) is not None and not outputs["tracked_changes"] and len(outputs["tracked_contracts"].splitlines()) == 3


def _git_checkout_route(request: Request, runtime: AdapterRuntime) -> dict[str, Any]:
    outputs = _checkout_outputs(runtime)
    if outputs is None:
        return result(
            request,
            status="blocked",
            error_kind="source_identity_mismatch",
            retry_safe=False,
            repair="Restore the exact locked checkout and tracked setup contracts.",
        )
    revision = str(request["flow_source_revision"])
    valid = _checkout_valid(outputs, revision)
    evidence_items = [
        evidence(
            runtime,
            evidence_id="checkout.locked_revision",
            kind="source_identity",
            status="verified" if valid else "blocked",
            summary="HEAD, main, the peeled tree, and tracked setup contracts were checked.",
            observed=outputs["head"],
            expected=revision,
            source="$TARGET/.git",
        ),
    ]
    if not valid:
        return result(
            request,
            status="blocked",
            error_kind="source_identity_mismatch",
            retry_safe=False,
            evidence_items=evidence_items,
            repair="Restore the exact locked commit and tracked tree before setup.",
        )
    return result(request, status="verified", evidence_items=evidence_items)


def _physical_memory_route(request: Request, runtime: AdapterRuntime) -> dict[str, Any]:
    """Refuse setup before model provisioning when host RAM is unsupported."""

    completed = run_public(runtime, ["/usr/sbin/sysctl", "-n", "hw.memsize"])
    observed = completed.stdout.strip() if completed is not None and completed.returncode == 0 else ""
    memory_bytes = int(observed) if re.fullmatch(r"[1-9][0-9]*", observed) else None
    supported = memory_bytes is not None and memory_bytes >= _MINIMUM_PHYSICAL_MEMORY_BYTES
    evidence_items = [
        evidence(
            runtime,
            evidence_id="host.physical_memory_bytes",
            kind="host_capacity",
            status="verified" if supported else "blocked",
            summary="Physical memory was measured before dependency and model provisioning.",
            observed=memory_bytes if memory_bytes is not None else "unavailable",
            expected=_MINIMUM_PHYSICAL_MEMORY_BYTES,
            source="sysctl:hw.memsize",
        )
    ]
    if memory_bytes is None:
        return result(
            request,
            status="blocked",
            error_kind="physical_memory_unavailable",
            retry_safe=False,
            evidence_items=evidence_items,
            repair="Run /usr/sbin/sysctl -n hw.memsize successfully before setup; this host requires at least 24 GB of physical memory.",
        )
    if not supported:
        return result(
            request,
            status="blocked",
            error_kind="physical_memory_below_minimum",
            retry_safe=False,
            evidence_items=evidence_items,
            repair="This Mac has less than 24 GB of physical memory. Use a supported host before setup downloads or loads local models.",
        )
    return result(request, status="verified", evidence_items=evidence_items)


def _homebrew_probe_route(request: Request, runtime: AdapterRuntime) -> dict[str, Any]:
    brew = resolve_brew_executable(runtime)
    present = brew is not None
    evidence_items = [
        evidence(
            runtime,
            evidence_id="homebrew.available",
            kind="executable_resolution",
            status="verified" if present else "blocked",
            summary=(
                "Homebrew was resolved without running an installer."
                if present
                else "Homebrew could not be resolved without running an installer."
            ),
            observed=brew,
            expected="absolute executable path",
            source=brew or "unresolved",
        ),
    ]
    if present:
        return result(request, status="verified", evidence_items=evidence_items)
    return result(
        request,
        status="blocked",
        error_kind="homebrew_missing",
        retry_safe=False,
        evidence_items=evidence_items,
        repair="Install Homebrew from its reviewed distribution path, then resume.",
    )


def _homebrew_install_route(request: Request, runtime: AdapterRuntime) -> dict[str, Any]:
    """Present the reviewed Homebrew action without ever executing an installer."""
    brew = resolve_brew_executable(runtime)
    present = brew is not None
    evidence_items = [
        evidence(
            runtime,
            evidence_id="homebrew.available",
            kind="executable_resolution",
            status="verified" if present else "awaiting_user",
            summary=(
                "Homebrew was resolved without running an installer."
                if present
                else "Homebrew could not be resolved without running an installer."
            ),
            observed=brew,
            expected="absolute executable path",
            source=brew or "unresolved",
        ),
    ]
    if present:
        return result(request, status="verified", evidence_items=evidence_items)
    repair = (
        "Install Homebrew from its reviewed official distribution path, then resume."
        if request["phase"] == "probe"
        else "Complete the reviewed Homebrew installation, then resume this operation."
    )
    return result(
        request,
        status="awaiting_user",
        error_kind="homebrew_missing",
        retry_safe=True,
        evidence_items=evidence_items,
        repair=repair,
    )


def _resolved_tool(runtime: AdapterRuntime, executable_name: str) -> str | None:
    candidate = runtime.which(executable_name)
    if candidate is None or not Path(candidate).is_absolute():
        return None
    return candidate


def _coding_tool_route(request: Request, runtime: AdapterRuntime) -> dict[str, Any]:
    """Provision one reviewed coding tool before the target venv exists."""

    executable_name, kind, package = _CODING_TOOL_ACQUISITIONS[request["operation_id"]]
    executable = _resolved_tool(runtime, executable_name)
    evidence_items = [
        evidence(
            runtime,
            evidence_id=f"{executable_name}.available",
            kind="executable_resolution",
            status="verified" if executable is not None else "blocked",
            summary=f"The {executable_name} executable was resolved before Homebrew provisioning.",
            observed=executable,
            expected="absolute executable path",
            source=executable or "unresolved",
        ),
    ]
    if executable is not None:
        return result(request, status="verified", evidence_items=evidence_items)
    brew = resolve_brew_executable(runtime)
    if request["phase"] == "probe":
        planned_actions = [
            {
                "id": f"{executable_name}.install_homebrew_package",
                "title": f"Install {package} with Homebrew",
                "mutation_kind": "package_install",
                "target": f"{brew or 'unresolved'}:{package}",
                "requires_confirmation": True,
                "condition_or_evidence_ref": f"{executable_name}_missing",
            },
        ]
        return result(
            request,
            status="pending",
            planned_actions=planned_actions,
            evidence_items=evidence_items,
            repair=f"Approve the exact Homebrew {package} installation action.",
        )
    if brew is None:
        return result(
            request,
            status="blocked",
            error_kind="homebrew_missing",
            retry_safe=False,
            evidence_items=evidence_items,
            repair=f"Resolve Homebrew before applying the approved {package} installation.",
        )
    try:
        run_homebrew_install_required(
            runtime,
            brew,
            package,
            f"{executable_name} Homebrew install",
            kind=kind,
        )
    except AdapterError as exc:
        return _homebrew_failure_result(
            request,
            error_kind="coding_tool_install_failed",
            evidence_items=evidence_items,
            repair=f"Inspect the Homebrew package state for {package} and resume.",
            error=exc,
        )
    return result(request, status="applied", evidence_items=evidence_items)


def _validate_route_inputs(request: Request) -> None:
    inputs = request["public_inputs"]
    if request["operation_id"] in _LM_STUDIO_OPERATION_IDS:
        allowed = {
            "embeddings_implementation",
            "inference_implementation",
            "lm_studio_base_url",
        }
    else:
        allowed = {"solet_name"} if request["operation_id"] == "configure_postgresql" else set()
    if set(inputs) - allowed:
        raise AdapterRequestError("operation public_inputs are outside the closed registry")
    if "solet_name" in inputs and inputs["solet_name"] != request["name"]:
        raise AdapterRequestError("solet_name public input differs from request identity")


def _resolve_route(request: Request) -> tuple[str, str] | None:
    declared = _ROUTES.get(str(request["operation_id"]))
    if declared is None or declared[0] != request["operation_ref"]:
        return None
    return declared


def _postgres_install_route(request: Request, runtime: AdapterRuntime) -> dict[str, Any]:
    observed = postgres_observation(runtime)
    evidence_items = postgres_evidence(runtime, observed)
    incompatibility = postgres_incompatibility(observed)
    if incompatibility is not None:
        return result(
            request,
            status="blocked",
            error_kind=incompatibility,
            retry_safe=False,
            evidence_items=evidence_items,
            repair="Resolve the existing PostgreSQL installation explicitly.",
        )
    actions = postgres_install_actions(observed)
    if observed.brew_path is not None:
        actions = _bind_homebrew_actions(actions, observed.brew_path)
    if request["phase"] == "probe":
        return result(
            request,
            status="verified" if not actions else "pending",
            planned_actions=actions,
            evidence_items=evidence_items,
            repair=None if not actions else "Approve the exact Homebrew dependency actions.",
        )
    brew = observed.brew_path
    if brew is None:
        return result(
            request,
            status="blocked",
            error_kind="homebrew_missing",
            retry_safe=False,
            evidence_items=evidence_items,
            repair="Resolve Homebrew before applying the approved PostgreSQL actions.",
        )
    commands = {
        "postgres.install_homebrew_formula": (
            [brew, "install", "postgresql@17"],
            "PostgreSQL install",
        ),
        "postgres.start_homebrew_service": (
            [brew, "services", "start", "postgresql@17"],
            "PostgreSQL service start",
        ),
        "postgres.install_pgvector_formula": (
            [brew, "install", "pgvector"],
            "pgvector install",
        ),
    }
    try:
        for item in actions:
            action_id = str(item["id"])
            command, label = commands[action_id]
            if command[1:2] == ["install"]:
                run_homebrew_install_required(runtime, brew, command[2], label)
            else:
                run_required(runtime, command, label)
    except AdapterError as exc:
        return _homebrew_failure_result(
            request,
            error_kind="postgres_install_failed",
            evidence_items=evidence_items,
            repair="Inspect the Homebrew package state and resume after repairing it.",
            error=exc,
        )
    return result(request, status="applied", evidence_items=evidence_items)


def _bind_homebrew_actions(actions: list[dict[str, Any]], brew: str) -> list[dict[str, Any]]:
    """Make the reviewed action carry the executable that its apply will invoke."""

    return [{**action, "target": f"{brew}:{action['target']}"} for action in actions]


def _postgres_configure_route(request: Request, runtime: AdapterRuntime) -> dict[str, Any]:
    observed = postgres_observation(runtime)
    evidence_items = postgres_evidence(runtime, observed)
    incompatibility = postgres_incompatibility(observed)
    if incompatibility is not None:
        return result(
            request,
            status="blocked",
            error_kind=incompatibility,
            retry_safe=False,
            evidence_items=evidence_items,
            repair="Resolve the incompatible PostgreSQL installation before configuration.",
        )
    policy = role_policy_observation(runtime) if observed.ready else None
    if policy is not None:
        evidence_items.extend(role_policy_evidence(runtime, policy))
        unsafe = unsafe_policy_error(policy)
        if unsafe is not None:
            return result(
                request,
                status="blocked",
                error_kind=unsafe,
                retry_safe=False,
                evidence_items=evidence_items,
                repair="Inspect and reconcile the PostgreSQL policy by hand, then resume.",
            )
    actions = postgres_configure_actions(observed, policy, runtime.name)
    if request["phase"] == "probe":
        return result(
            request,
            status="verified" if not actions else "pending",
            planned_actions=actions,
            evidence_items=evidence_items,
            repair=None if not actions else "Approve the exact PostgreSQL configuration actions.",
        )
    if policy is None:
        return result(
            request,
            status="blocked",
            error_kind="postgres_service_not_ready",
            evidence_items=evidence_items,
            repair="Complete the approved PostgreSQL install/start operation and re-probe.",
        )
    try:
        apply_postgres_configuration(runtime, policy, actions)
    except AdapterError as exc:
        return result(
            request,
            status="failed",
            error_kind="postgres_configuration_failed",
            retry_safe=True,
            evidence_items=evidence_items,
            repair=_postgres_configuration_repair(exc),
        )
    return result(request, status="applied", evidence_items=evidence_items)


def _postgres_probe_status(
    request: Request,
    runtime: AdapterRuntime,
    observed: PostgresObservation,
    evidence_items: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    operation_id = request["operation_id"]
    if operation_id == "pgvector_package_available":
        if not observed.ready:
            return False, "postgres_service_not_ready"
        return pgvector_package_available(observed), None
    if operation_id == "pgvector_ready":
        if not observed.ready:
            return False, "postgres_service_not_ready"
        ok, value = psql_scalar(
            runtime,
            database=runtime.name,
            statement="SELECT CASE WHEN EXISTS (SELECT 1 FROM pg_extension WHERE extname='vector') THEN 1 ELSE 0 END",
        )
        present = ok and value == "1"
        evidence_items.append(
            evidence(
                runtime,
                evidence_id="postgres.pgvector_target_database",
                kind="postgres_probe",
                status="verified" if present else "blocked",
                summary="The vector extension was checked in the target database.",
                observed=present,
                expected=True,
                source=f"localhost-postgresql:{runtime.name}",
            )
        )
        return present, None
    return _postgres_non_pgvector_probe_status(
        operation_id,
        runtime,
        observed,
        evidence_items,
    )


def _postgres_non_pgvector_probe_status(
    operation_id: str,
    runtime: AdapterRuntime,
    observed: PostgresObservation,
    evidence_items: list[dict[str, Any]],
) -> tuple[bool, str | None]:
    basic_statuses = {
        "postgres_binary_version_valid": (
            observed.major == SUPPORTED_POSTGRES_MAJOR and observed.homebrew_managed,
            None,
        ),
        "postgres_ready": (observed.ready, None),
    }
    basic_status = basic_statuses.get(operation_id)
    if basic_status is not None:
        return basic_status
    policy = role_policy_observation(runtime) if observed.ready else None
    if policy is None:
        return False, None
    evidence_items.extend(role_policy_evidence(runtime, policy))
    unsafe = unsafe_policy_error(policy)
    return not postgres_configure_actions(observed, policy, runtime.name), unsafe


def _postgres_probe_route(request: Request, runtime: AdapterRuntime) -> dict[str, Any]:
    observed = postgres_observation(runtime)
    evidence_items = postgres_evidence(runtime, observed)
    incompatibility = postgres_incompatibility(observed)
    if incompatibility is not None:
        return result(
            request,
            status="blocked",
            error_kind=incompatibility,
            retry_safe=False,
            evidence_items=evidence_items,
            repair="Repair the selected Homebrew PostgreSQL 17 runtime and resume.",
        )
    valid, policy_error = _postgres_probe_status(request, runtime, observed, evidence_items)
    if policy_error is not None:
        return result(
            request,
            status="blocked",
            error_kind=policy_error,
            retry_safe=False,
            evidence_items=evidence_items,
            repair="Reconcile the existing PostgreSQL policy explicitly.",
        )
    if valid:
        return result(request, status="verified", evidence_items=evidence_items)
    return result(
        request,
        status="blocked",
        error_kind="postgres_probe_not_verified",
        evidence_items=evidence_items,
        repair="Run the declared PostgreSQL repair operation and re-probe.",
    )


_ExactRoute = Callable[[Request, AdapterRuntime], dict[str, Any]]

_EXACT_OPERATION_ROUTES: dict[str, _ExactRoute] = {
    "request_homebrew_install": _homebrew_install_route,
    "install_postgresql": _postgres_install_route,
    "configure_postgresql": _postgres_configure_route,
    "git_checkout_valid": _git_checkout_route,
    "minimum_physical_memory_valid": _physical_memory_route,
    "homebrew_available": _homebrew_probe_route,
}


def _dispatch(request: Request, runtime: AdapterRuntime) -> dict[str, Any]:
    operation_id = request["operation_id"]
    if operation_id in _LM_STUDIO_OPERATION_IDS:
        return lm_studio_route(request, runtime)
    if operation_id in {"install_python_runtime", "python_version_valid"}:
        return _python_route(request, runtime)
    if operation_id in {
        "build_instance_environment",
        "instance_environment_dependency_closure_valid",
    }:
        return dependency_closure_route(request, runtime)
    if operation_id in _CODING_TOOL_ACQUISITIONS:
        return _coding_tool_route(request, runtime)
    exact_route = _EXACT_OPERATION_ROUTES.get(operation_id)
    if exact_route is not None:
        return exact_route(request, runtime)
    return _postgres_probe_route(request, runtime)


def execute_adapter_request(
    raw: object,
    *,
    runner: Runner = subprocess.run,
    which: Which = shutil.which,
    now: Clock = utc_now,
    base_python: str | None = None,
) -> dict[str, Any]:
    """Validate and execute exactly one frozen pre-venv adapter request."""

    try:
        request = validate_request(raw)
        declared = _resolve_route(request)
        if declared is None:
            return result(
                request,
                status="blocked",
                error_kind="adapter_missing",
                retry_safe=False,
                repair="Add an explicitly reviewed pre-venv adapter route.",
            )
        _validate_route_inputs(request)
        if declared[1] == "probe" and request["phase"] != "probe":
            return result(
                request,
                status="blocked",
                error_kind="adapter_protocol_error",
                retry_safe=False,
                repair="Declared probes never accept an apply phase.",
            )
    except AdapterRequestError as exc:
        return protocol_error_result(raw, str(exc))
    runtime = AdapterRuntime(
        run=runner,
        which=which,
        now=now,
        name=str(request["name"]),
        target=Path(str(request["target"])),
        base_python=base_python,
    )
    return _dispatch(request, runtime)
