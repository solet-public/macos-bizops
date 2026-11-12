"""Planner regression smoke for the retired Git-control question and topology projection."""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from solet_manager.config import CreateConfig  # noqa: E402
from solet_manager.contracts import ContractBundle  # noqa: E402
from solet_manager.flow import build_setup_plan  # noqa: E402
from solet_manager.models import JsonValue  # noqa: E402
from solet_manager.release_lock import SeedLock  # noqa: E402

_CONTRACTS = Path(__file__).resolve().parents[2] / "plugins" / "github_midwife_plugin" / "knowledge_base"
_checks = 0


def _check(condition: object, label: str) -> None:
    global _checks
    _checks += 1
    if not condition:
        raise AssertionError(label)


def _shell_inputs(execution_topology: str) -> dict[str, JsonValue]:
    seed = SeedLock(
        "https://github.com/solet-public/macos-bizops.git",
        "release-2026-08-20",
        "a" * 40,
        "b" * 40,
        "c" * 64,
        "macos-bizops",
    )
    bundle = ContractBundle.load(source_revision=seed.commit, directory=_CONTRACTS)
    config = CreateConfig(
        name="git-mutation-control",
        target=Path("/tmp/git-mutation-control"),
        autostart=True,
        decisions={
            "embeddings_implementation": "lm_studio",
            "inference_implementation": "lm_studio",
            "execution_topology": execution_topology,
        },
        decision_sources={
            "embeddings_implementation": "config",
            "inference_implementation": "config",
            "execution_topology": "config",
        },
    )
    plan = build_setup_plan(
        bundle=bundle,
        config=config,
        seed=seed,
        journal_path=Path("/tmp/git-mutation-control.json"),
        decision_selections=config.decisions,
        decision_sources=config.decision_sources,
    )
    return next(operation.public_inputs for operation in plan.operations if operation.operation_id == "install_shell_integration")


def main() -> int:
    for topology in ("solo", "fleet"):
        inputs = _shell_inputs(topology)
        _check(
            inputs == {"execution_topology": topology, "selected_coding_agents": ["claude_code"]},
            "shell integration receives topology independently of a controller setting",
        )
        _check("git_controller_name" not in inputs, "the retired controller input is never projected")
    print(f"git_mutation_control_plan_smoke OK: {_checks} checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
