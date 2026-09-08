"""Prove launchagent receives the selected profile and rejects its absence."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import cast

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "plugins" / "github_midwife_plugin" / "src"))
sys.path.insert(0, str(_REPO_ROOT / "solet_cli" / "src"))

from github_midwife_plugin import setup_adapter  # noqa: E402
from github_midwife_plugin.setup_adapter_contract import AdapterRequest  # noqa: E402
from solet_manager.config import CreateConfig  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.operation_records import operation_request  # noqa: E402
from solet_manager.plan_builder import build_setup_plan  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402
from solet_manager.transaction import Transaction, canonical_sha256  # noqa: E402

_KB_ROOT = _REPO_ROOT / "plugins" / "github_midwife_plugin" / "knowledge_base"
_CHECKS: list[str] = []


class SmokeFailureError(AssertionError):
    """Raised when this focused launchagent contract smoke fails."""


class MissingProfileRuntime:
    """Prove the guard returns before reaching the subprocess boundary."""

    home = Path("/tmp/install-launchagent-profile-guard-home")

    def run(self, *args: object, **kwargs: object) -> object:
        del args, kwargs
        raise AssertionError("missing setup_profile must block before subprocess execution")


def _check(condition: bool, label: str) -> None:
    _CHECKS.append(label)
    if not condition:
        raise SmokeFailureError(label)


def _launchagent_request() -> AdapterRequest:
    revision = "a" * 40
    target = Path("/tmp/install-launchagent-profile-contract")
    bundle = ContractBundle.load(source_revision=revision, directory=_KB_ROOT)
    seed = SeedLock(
        repository="example/seed",
        release_tag="v1.0.0",
        commit=revision,
        tree_hash="b" * 40,
        archive_sha256="c" * 64,
        profile="macos-bizops",
    )
    plan = build_setup_plan(
        bundle=bundle,
        config=CreateConfig(name="launchagent-contract", target=target, autostart=True),
        seed=seed,
        journal_path=Path("/tmp/install-launchagent-profile-contract.json"),
        prospective_consents=True,
        operation_stage_ids={"models"},
    )
    operation = next(
        item
        for item in plan.operations
        if item.operation_ref == "genesis::autostart.install"
    )
    transaction = Transaction.create(
        name="launchagent-contract",
        target=target,
        input_fingerprint=canonical_sha256({"fixture": "launchagent-profile-contract"}),
        answers=plan.answers,
        seed=seed,
        flow_id=bundle.flow_id,
        flow_source_revision=bundle.source_revision,
        flow_contract_digest=bundle.contract_digest,
        stage_ids=tuple(bundle.stages),
        completion_probe_ids=(),
    )
    request = operation_request(
        transaction,
        bundle,
        operation,
        phase="probe",
        probe_purpose="preview",
        approval=None,
        attempt=1,
    )
    return AdapterRequest.from_dict(cast(dict[str, object], request.to_dict()))


def _check_profile_projection_and_guard() -> None:
    request = _launchagent_request()
    _check(
        request.public_inputs == {"setup_profile": "macos-bizops", "autostart": "enabled"},
        "launchagent request projects selected setup_profile and autostart",
    )
    missing_preview_payload = dict(cast(dict[str, object], request_to_dict(request)))
    missing_preview_payload["public_inputs"] = {}
    preview_response = setup_adapter.dispatch_request(
        AdapterRequest.from_dict(missing_preview_payload),
        MissingProfileRuntime(),
    )
    preview_actions = preview_response.get("planned_actions")
    _check(
        preview_response.get("checkpoint_status") == "pending"
        and isinstance(preview_actions, list)
        and any(action.get("id") == "genesis.install_launchagent" for action in preview_actions),
        "preview without setup_profile remains actionable",
    )
    missing_payload = dict(cast(dict[str, object], request_to_dict(request)))
    missing_payload.update(
        {
            "phase": "apply",
            "probe_purpose": None,
            "approval_fingerprint": "sha256:" + "c" * 64,
            "dry_run": False,
            "target": str(Path(sys.executable).parents[2]),
            "public_inputs": {},
        }
    )
    response = setup_adapter.dispatch_request(
        AdapterRequest.from_dict(missing_payload),
        MissingProfileRuntime(),
    )
    _check(
        response.get("checkpoint_status") == "blocked"
        and response.get("error_kind") == "operation_input_missing"
        and response.get("repair")
        == "Resolve the selected setup profile before running genesis.",
        "missing setup_profile blocks before subprocess execution",
    )


def request_to_dict(request: AdapterRequest) -> dict[str, object]:
    """Build the closed wire request without reaching a production adapter."""

    return {
        "protocol_version": 1,
        "kind": "operation_request",
        "request_id": request.request_id,
        "operation_id": request.operation_id,
        "operation_ref": request.operation_ref,
        "phase": request.phase,
        "probe_purpose": request.probe_purpose,
        "attempt": request.attempt,
        "name": request.name,
        "target": str(request.target),
        "flow_id": "macos.repository_setup",
        "flow_source_revision": request.flow_source_revision,
        "answers_fingerprint": request.answers_fingerprint,
        "approval_fingerprint": request.approval_fingerprint,
        "dry_run": request.dry_run,
        "timeout_seconds": request.timeout_seconds,
        "public_inputs": request.public_inputs,
    }


def main() -> int:
    try:
        _check_profile_projection_and_guard()
    except SmokeFailureError as exc:
        print(f"FAIL: {exc}", file=sys.stderr)
        return 1
    print(f"install_launchagent_profile_input_smoke OK: {len(_CHECKS)} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
