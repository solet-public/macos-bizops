"""Quality-service verb bodies — list_gates / run_gate / run_test.

Thin orchestration over :mod:`gate_registry` (the server-side allowlist +
argv builder) and :mod:`bounded_subprocess` (server-side argv, timeout,
bounded output). No caller-supplied path/argv/flag ever reaches the shell —
the gate/smoke NAME is the only caller input, and it is validated against the
server-side registry / register before any subprocess runs.

``SOLET_NAME`` is passed through explicitly on every run: the god-class
gate imports in-scope modules whose vault-scoped constants fail loud without
it (KB ``22_testing/03``).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from platform_dev_surface_plugin.bounded_subprocess import TIMEOUT_EXIT_CODE, run_bounded
from platform_dev_surface_plugin.quality import gate_registry
from platform_dev_surface_plugin.quality.landing_verify import (
    detect_scope_regex_gaps,
    predict_append_only_register_merge,
    verify_hash_manifest,
)

_SMOKE_REGISTER_REL = "quality_gates/gate_smokes.txt"
# The per-candidate registration route beside the register (prb_ae6d81ea): a
# smoke listed in a fragment here is registered on the same terms as one listed
# in the main file, and two candidates registering smokes share no path.
_SMOKE_REGISTER_FRAGMENTS_REL = "quality_gates/gate_smokes.d/"
_RUN_SMOKES_REL = "quality_gates/run_smokes.py"

# The automake/Meson/CTest SKIP_RETURN_CODE convention, matching
# run_smokes.py's own _SKIP_EXIT_CODE — a smoke exits this code to report a
# disclosed, non-blocking dependency gap, not a genuine failure. Undeclared-
# dependency audit: workbench/2026-08-08_undeclared_system_dependencies_findings_d3-impl.md.
_SMOKE_SKIP_EXIT_CODE = 77


class QualityGateError(ValueError):
    """Typed rejection: an unknown gate name or an unregistered smoke path."""


def _last_meaningful_line(output: str) -> str:
    """The last non-blank line — gate verdicts / smoke summaries land at the end."""
    for line in reversed(output.splitlines()):
        stripped = line.strip()
        if stripped:
            return stripped
    return ""


def _bind_proof_source(repo_root: Path) -> None:
    """Bind the shared library to the selected repo, even outside repo cwd.

    A previously loaded module from another checkout is a measured identity
    contradiction, not permission to run it against this candidate.
    """
    for name, module in tuple(sys.modules.items()):
        origin = getattr(module, "__file__", None)
        if name.startswith("quality_gates.") and origin and not Path(origin).resolve().is_relative_to(repo_root.resolve()):
            raise ValueError(f"quality proof library source mismatch: {name} at {origin}")
    sys.path.insert(0, str(repo_root.resolve()))


class QualityOperations:
    """Bind the quality verbs to a concrete repo root + solet identity."""

    def __init__(self, repo_root: Path, solet_name: str) -> None:
        self._repo_root = repo_root
        self._solet_name = solet_name
        _bind_proof_source(repo_root)
        from platform_dev_surface_plugin.quality.full_battery import FullBatteryProducer

        self.full_battery = FullBatteryProducer(repo_root, solet_name)
        self._venv_python = repo_root / ".venv" / "bin" / "python3"
        if not self._venv_python.exists():
            raise FileNotFoundError(f"platform_dev_surface_plugin: repo venv interpreter not found at {self._venv_python}")

    def _env(self) -> dict[str, str]:
        return {"SOLET_NAME": self._solet_name}

    def _read_smoke_register(self) -> list[str]:
        """The EFFECTIVE register: ``gate_smokes.txt`` plus its fragments.

        This is the server-side allowlist ``run_test`` validates a caller's one
        smoke NAME against, so it has to see a smoke registered in a fragment
        under ``gate_smokes.d/`` on exactly the same terms as one listed in the
        main file (prb_ae6d81ea) -- otherwise the fragment route would register
        a smoke for the battery but not for this verb. Read through the shared
        loader rather than a second hand-written copy of the grammar.

        An unreadable fragment is an advisory, never a refusal (rul_becb4bed):
        the entries that could be read remain runnable.
        """
        from quality_gates.gate_smoke_register import load as load_effective_register

        effective = load_effective_register(self._repo_root / _SMOKE_REGISTER_REL)
        for advisory in effective.advisories:
            print(advisory, file=sys.stderr, flush=True)
        return list(effective.entries)

    def list_gates(self) -> dict[str, Any]:
        """Enumerate the server-side gate registry + the smoke register. No execution."""
        gates = [
            {
                "name": spec.name,
                "kind": spec.kind,
                "description": spec.description,
                "timeout_seconds": spec.timeout_seconds,
                "directly_runnable": spec.directly_runnable,
                "run_via": ("run_gate" if spec.directly_runnable else gate_registry.AGGREGATE_GATE_NAME),
            }
            for spec in gate_registry.all_gates()
        ]
        smokes = self._read_smoke_register()
        return {
            "gates": gates,
            "smokes": smokes,
            "smoke_count": len(smokes),
            "proof_schema": "platform-smoke-proof/v1",
            "proof_lifecycle": ["select_smokes", "run_selected_smokes", "get_smoke_proof", "list_smoke_proofs", "get_smoke_proof_history", "correct_smoke_proof", "clear_smoke_proof", "retire_smoke_proof", "reinstate_smoke_proof", "ensure_full_smoke_schedule", "trigger_full_smoke_battery"],
        }

    def run_gate(self, gate: str) -> dict[str, Any]:
        """Run one allowlisted gate without an outer deadline; retain raw exits."""
        spec = gate_registry.runnable_gate(gate)
        if spec is None:
            raise QualityGateError(f"unknown or non-directly-runnable gate '{gate}'. Runnable gates: {', '.join(gate_registry.runnable_gate_names())}. The coherence trio runs via the '{gate_registry.AGGREGATE_GATE_NAME}' aggregate.")
        argv = gate_registry.build_gate_argv(spec, self._repo_root, self._venv_python)
        result = run_bounded(
            argv,
            cwd=self._repo_root,
            timeout=None,
            extra_env=self._env(),
        )
        return {
            "gate": spec.name,
            "passed": result.exit_code == 0,
            # Static gates never emit the smoke SKIP convention; always False.
            "skipped": False,
            "exit_code": result.exit_code,
            "harness_failure": result.exit_code == TIMEOUT_EXIT_CODE,
            "timed_out": result.timed_out,
            "summary": _last_meaningful_line(result.output),
            "output": result.output,
            "truncated": result.truncated,
            "output_chars_total": result.output_chars_total,
        }

    def run_test(self, smoke: str | None = None) -> dict[str, Any]:
        """Run the suite without an outer deadline, or one shared-producer smoke."""
        if smoke is not None:
            if smoke not in self._read_smoke_register():
                raise QualityGateError(f"smoke '{smoke}' is not in the gate register ({_SMOKE_REGISTER_REL} or a fragment under {_SMOKE_REGISTER_FRAGMENTS_REL}); only registered smokes may be run. Register it by adding its repo-relative path as one line in your own fragment file under {_SMOKE_REGISTER_FRAGMENTS_REL}.")
            return _run_named_smoke(smoke)
        target = "suite"
        from quality_gates.full_smoke_campaign import campaign_budget
        from quality_gates.smoke_pass_store import default_store_root

        # Historical campaign timing is an estimate, never kill authorization.
        budget = {**campaign_budget(default_store_root().parent, registered_count=len(self._read_smoke_register())), "enforcement": "advisory_only"}
        with tempfile.TemporaryDirectory() as directory:
            # The campaign file is the suite's structured result: its
            # harness_defects names each smoke whose own exit 124 the runner
            # disclosed instead of counting it as a verdict.
            results = Path(directory) / "results.json"
            argv = [
                str(self._venv_python),
                str(self._repo_root / _RUN_SMOKES_REL),
                "--register",
                str(self._repo_root / _SMOKE_REGISTER_REL),
                "--results-output",
                str(results),
            ]
            result = run_bounded(
                argv,
                cwd=self._repo_root,
                timeout=None,
                extra_env=self._env(),
            )
            # No file means the runner published nothing (for example a
            # refused receipt): the defects are unknown, never an empty list.
            harness_defects = json.loads(results.read_bytes())["harness_defects"] if results.exists() else None
        # Single-smoke path: the smoke's own raw exit code is ground truth for
        # SKIP. Suite path: run_smokes.py's exit code already encodes its own
        # pass/fail policy over any skips it saw (see its --fail-on-skip) --
        # this verb doesn't currently parse the suite's per-smoke skip lines
        # out of `output`, so `skipped` stays False there rather than guessing
        # from prose.
        skipped = smoke is not None and result.exit_code == _SMOKE_SKIP_EXIT_CODE
        return {
            "target": target,
            "passed": result.exit_code == 0,
            "skipped": skipped,
            "exit_code": result.exit_code,
            "harness_failure": result.exit_code == TIMEOUT_EXIT_CODE or bool(harness_defects),
            "harness_defects": harness_defects,
            "timed_out": result.timed_out,
            "summary": _last_meaningful_line(result.output),
            "output": result.output,
            "truncated": result.truncated,
            "output_chars_total": result.output_chars_total,
            "budget": budget,
        }

    def smoke_proof(self, operation: str, *, principal: str = "", **arguments: Any) -> Any:
        """Closed proof operations share one library/store across every surface."""
        from quality_gates.smoke_pass_store import SmokePassStore
        from quality_gates.smoke_proof import proof_operation, run_selected_smokes, select_smokes

        if operation == "select":
            return select_smokes(self._repo_root, arguments["request"])
        if operation == "run":
            return run_selected_smokes(self._repo_root, arguments["request"], principal=principal)
        return proof_operation(SmokePassStore(), operation, root=self._repo_root, principal=principal, **arguments)

    def verify_hash_manifest(
        self,
        unit_id: str,
        root_path: str,
        manifest: dict[str, str],
    ) -> dict[str, Any]:
        """Rehash a landing manifest and disclose editable-install shadowing."""
        return verify_hash_manifest(
            unit_id=unit_id,
            root_path=root_path,
            manifest=manifest,
        )

    def predict_gate_smokes_merge(
        self,
        base_ref: str,
        lane_root_path: str,
        current_master: str,
        register_path: str = "quality_gates/gate_smokes.txt",
    ) -> dict[str, Any]:
        """Predict a tracked-debt-register merge without changing any Git state."""
        return predict_append_only_register_merge(
            base_ref=base_ref,
            lane_root_path=lane_root_path,
            current_master=current_master,
            register_path=register_path,
        )

    def detect_scope_regex_gaps(self, paths: list[str]) -> dict[str, Any]:
        """Report landing paths which need static analysis beyond the aggregate gate."""
        return detect_scope_regex_gaps(paths)


def _run_named_smoke(smoke: str) -> dict[str, Any]:
    from quality_gates.run_smokes import execute_selected

    result = execute_selected([smoke])
    row = result["smoke_results"][smoke]
    output = "\n".join(attempt["output"] for attempt in row["attempts"])
    skipped = row["verdict"] == "skipped"
    # The shared runner discloses a smoke's own exit 124 as a harness defect,
    # never a verdict, and leaves its aggregate code to measured outcomes:
    # report the smoke's raw 124 here, as a skip keeps its raw 77. A producer
    # result without the list leaves the defects unknown (null).
    harness_defects = result.get("harness_defects")
    harness = harness_defects is not None and smoke in harness_defects
    code = _SMOKE_SKIP_EXIT_CODE if skipped else TIMEOUT_EXIT_CODE if harness else result["exit_code"]
    return {
        "target": smoke,
        "passed": row["verdict"] == "passed",
        "skipped": skipped,
        "exit_code": code,
        "harness_failure": harness,
        "harness_defects": harness_defects,
        "timed_out": "timed out" in output.lower(),
        "summary": _last_meaningful_line(output),
        "output": output[-16384:],
        "truncated": len(output) > 16384,
        "output_chars_total": len(output),
        "attempts": row["attempts"],
        "identity_advisory": "run_test is an uncached working-tree measurement; use run_selected_smokes for frozen proof",
    }
