"""Fake-runtime cases for bounded command output on setup-adapter failure results."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType
from typing import Any
from unittest.mock import patch

_DEPENDENCY_STDOUT = "dependency-closure-known-stdout"
_DEPENDENCY_STDERR = "dependency-closure-known-stderr"
_POSTGRES_STDOUT = "psql: configuration stdout\n"
_POSTGRES_STDERR = "psql: configuration stderr\n"
_PARTIAL_STDOUT = b"partial-configuration-stdout"
_PARTIAL_STDERR = b"partial-configuration-stderr"
_ADAPTER_NAME = "adaptertest"

type Check = Callable[[object, str], None]
type RequestFactory = Callable[..., dict[str, Any]]
type Completed = Callable[..., subprocess.CompletedProcess[str]]


def check_dependency_closure_failure_detail(
    *,
    module: ModuleType,
    root: Path,
    request_factory: RequestFactory,
    execute: Callable[..., dict[str, Any]],
    completed: Completed,
    check: Check,
    prepare_target: Callable[[ModuleType, Path], None],
    fingerprint: str,
) -> None:
    """Apply failures retain bounded command evidence and the existing repair instruction."""
    target = root / "failure-detail"
    prepare_target(module, target)
    base = root / "stable-python3.13"
    base.write_text("fixture")

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == [str(base), "--version"]:
            return completed(stdout="Python 3.13.7\n")
        if "pip" in command and "setuptools" in command:
            return completed(17, stdout=_DEPENDENCY_STDOUT, stderr=_DEPENDENCY_STDERR)
        return completed()

    apply = request_factory(target, phase="apply", purpose=None, approval=fingerprint)
    failed = execute(module, apply, run, lambda _name: None, base_python=str(base))
    repair = failed["repair"]
    check(
        failed.get("stdout") == _DEPENDENCY_STDOUT and failed.get("stderr") == _DEPENDENCY_STDERR and failed.get("exit_code") == 17,
        "dependency closure failure carries captured stdout and stderr",
    )
    check(failed["checkpoint_status"] == "failed", "failure-detail apply reports failed")
    check(
        failed["error_kind"] == "dependency_closure_apply_failed",
        "failure-detail retains dependency-closure failure kind",
    )
    check(
        isinstance(repair, str) and "exit 17" in repair and _DEPENDENCY_STDERR in repair,
        "failure-detail carries exit and stderr marker (red: restore generic repair)",
    )
    check(
        isinstance(repair, str) and repair.endswith("Repair the target package or interpreter condition and resume."),
        "failure-detail retains original repair instruction",
    )


def check_postgres_configuration_failure_output(
    *,
    root: Path,
    request_factory: RequestFactory,
    check: Check,
    fingerprint: str,
) -> None:
    """A nonzero psql apply keeps its bounded stdout and stderr on the failed result."""

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(command, 1, _POSTGRES_STDOUT, _POSTGRES_STDERR)

    failed = _configure_failed(root, request_factory, fingerprint, run)
    check(
        failed.get("stdout") == _POSTGRES_STDOUT and failed.get("stderr") == _POSTGRES_STDERR and failed.get("exit_code") == 1,
        "PostgreSQL configuration failure carries captured stdout and stderr",
    )
    check(
        failed["error_kind"] == "postgres_configuration_failed",
        "PostgreSQL configuration failure keeps its error kind",
    )


def check_postgres_configuration_timeout_output(
    *,
    root: Path,
    request_factory: RequestFactory,
    check: Check,
    fingerprint: str,
) -> None:
    """A psql timeout keeps its partial byte streams decoded, with timed_out set."""

    def run(command: list[str]) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(command, 1, output=_PARTIAL_STDOUT, stderr=_PARTIAL_STDERR)

    failed = _configure_failed(root, request_factory, fingerprint, run)
    check(
        failed.get("timed_out") is True and failed.get("stdout") == _PARTIAL_STDOUT.decode() and failed.get("stderr") == _PARTIAL_STDERR.decode() and failed.get("exit_code") is None,
        "PostgreSQL configuration timeout retains partial output and timed_out",
    )
    check(
        failed["error_kind"] == "postgres_configuration_failed",
        "PostgreSQL configuration timeout keeps its error kind",
    )


def _configure_failed(
    root: Path,
    request_factory: RequestFactory,
    fingerprint: str,
    run_command: Callable[[list[str]], subprocess.CompletedProcess[str]],
) -> dict[str, Any]:
    """Drive the real configure route apply against a fake psql runtime; every probe is patched."""
    from bootstrap_adapter.models import AdapterRuntime, PostgresObservation, RolePolicyObservation
    from bootstrap_adapter.routes import _postgres_configure_route

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command[-1:] == ["--version"]:
            return subprocess.CompletedProcess(command, 0, "fixture version\n", "")
        return run_command(command)

    runtime = AdapterRuntime(
        run=run,
        which=lambda name: "/fixture/brew" if name == "brew" else None,
        now=lambda: datetime(2026, 10, 16, tzinfo=UTC),
        name="fixture",
        target=root,
    )
    request = request_factory(
        root,
        operation_id="configure_postgresql",
        operation_ref="bootstrap::postgres.configure_solet",
        phase="apply",
        purpose=None,
        approval=fingerprint,
        public_inputs={"solet_name": _ADAPTER_NAME},
    )
    observed = PostgresObservation(True, "/fixture/brew", True, True, 17, True, True)
    policy = RolePolicyObservation(
        role_exists=True,
        database_exists=True,
        role_safe=True,
        database_owner_matches=True,
        schema_exists=False,
        schema_owner_matches=None,
        public_connect_revoked=True,
        vector_installed=True,
        hba_path=None,
        hba_safe=True,
        hba_layout_recognized=True,
        scram_present=True,
    )
    with (
        patch("bootstrap_adapter.routes.postgres_observation", return_value=observed),
        patch("bootstrap_adapter.routes.role_policy_observation", return_value=policy),
        patch("bootstrap_adapter.postgres.postgres_binaries", return_value={"psql": "/fixture/psql"}),
    ):
        return _postgres_configure_route(request, runtime)
