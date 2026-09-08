#!/usr/bin/env python3
"""Regression checks for PostgreSQL installer pre-verification."""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_ROOT / "solet_cli" / "src"))
sys.path.insert(0, str(_ROOT / "solet_cli" / "tests"))

from operation_probe_adapter_checks import _operation, _result, _transaction  # noqa: E402
from solet_manager import contracts, operation_executor  # noqa: E402
from solet_manager.adapters import OperationRequest, OperationResult  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.models import CheckpointStatus  # noqa: E402

_CONTRACTS = _ROOT / "plugins/github_midwife_plugin/knowledge_base"
_LEGACY_FLOW = (
    _ROOT
    / "solet_cli/tests/fixtures/contracts/reconcile_contract_legacy_4ff38b3d"
    / "macos_setup_flow.json"
)
_LEGACY_DIGEST = "sha256:67c903ea332287f2c73f036ff055b793f50072ed3af0784d68a505801c040540"
_EXPECTED_PRE_PROBES = (
    "postgres_binary_version_valid",
    "postgres_ready",
    "pgvector_ready",
)


def _check(condition: bool, label: str) -> None:
    if not condition:
        raise AssertionError(label)


def _pre_probe_result(
    blocked_probe: str,
    observed: list[str],
    _registry: object,
    *,
    runner: str,
    request: OperationRequest,
) -> OperationResult:
    del runner
    observed.append(request.operation_id)
    if request.operation_id == blocked_probe:
        return _result(
            request,
            CheckpointStatus.BLOCKED,
            error_kind=f"fixture_{blocked_probe}_false",
        )
    return _result(request, CheckpointStatus.VERIFIED)


def _check_false_probe_does_not_preverify(
    bundle: ContractBundle,
    *,
    scenario: str,
    blocked_probe: str,
    expected_observed: tuple[str, ...],
) -> None:
    operation = _operation(bundle, "install_postgresql")
    observed: list[str] = []
    with tempfile.TemporaryDirectory(prefix="postgres_preprobe_") as raw:
        transaction = _transaction(bundle, Path(raw) / "target")
        with patch.object(
            operation_executor,
            "invoke_adapter",
            side_effect=lambda registry, runner, request: _pre_probe_result(
                blocked_probe,
                observed,
                registry,
                runner=runner,
                request=request,
            ),
        ):
            result = operation_executor._invoke_operation_probe(
                bundle=bundle,
                operation=operation,
                transaction=transaction,
                registry=object(),
                purpose="pre_apply",
                attempt=1,
            )
    _check(
        result.checkpoint_status is CheckpointStatus.BLOCKED
        and observed == list(expected_observed),
        f"{scenario} does not pre-verify install_postgresql",
    )


def main() -> int:
    bundle = ContractBundle.load(source_revision="a" * 40, directory=_CONTRACTS)
    operation = _operation(bundle, "install_postgresql")
    _check(
        operation.precondition_probe_ids == _EXPECTED_PRE_PROBES,
        "installer pre-probe requires binary, service readiness, and pgvector",
    )
    _check_false_probe_does_not_preverify(
        bundle,
        scenario="version-present-service-down",
        blocked_probe="postgres_ready",
        expected_observed=_EXPECTED_PRE_PROBES[:2],
    )
    _check_false_probe_does_not_preverify(
        bundle,
        scenario="version-present-service-up-pgvector-absent",
        blocked_probe="pgvector_ready",
        expected_observed=_EXPECTED_PRE_PROBES,
    )
    legacy_flow = json.loads(_LEGACY_FLOW.read_text(encoding="utf-8"))
    normalized = contracts._normalize_legacy_resume_flow_v1(_LEGACY_DIGEST, legacy_flow)
    legacy = normalized["operations"]["install_postgresql"]["idempotency"]
    _check(
        legacy["precondition_probe_refs"] == list(_EXPECTED_PRE_PROBES),
        "digest-pinned legacy resume strengthens the installer pre-probe",
    )
    print("install_postgresql_preprobe_smoke: 4/4 checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
