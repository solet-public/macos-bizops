#!/usr/bin/env python3
"""S4 — GTE-06 orchestrator probe-gate smoke (no pytest).

Pins the ``_prepare_swap`` slot semantics through the PUBLIC
``SwapOrchestrator.restart`` path:

* [1-3] RED probe ⇒ ``PROBE_FAILED`` + ``reason_code=probe_rejected`` +
  the probe payload on the result; NO spawn, NO cutover — blue untouched.
* [4-5] a probe seam that RAISES is contained (A5a): still
  ``PROBE_FAILED`` (``ProbeHarnessError`` in the payload), never an
  escaping exception (which core would classify ``plugin_raised`` and
  LEAVE the committed bytes); NO spawn.
* [6-7] ordering: a non-additive schema preflight short-circuits BEFORE
  the probe seam is consulted (cheap gates first).
* [8-9] GREEN probe passes the gate through: the probe ran exactly once
  against the built candidate and the flow proceeded to spawn (a
  deliberately-failing spawn seam stops it there as
  ``FAILED/spawn_failed`` — decisively NOT a probe rejection).

Run:
    .venv/bin/python3 plugins/macos_self_deployment_plugin/tests/probe_gate_orchestrator_smoke.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

_PLUGIN_SRC = Path(__file__).resolve().parents[1] / "src"
_REPO_ROOT = Path(__file__).resolve().parents[3]
for _p in (str(_PLUGIN_SRC), str(_REPO_ROOT / "ananta" / "src")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from macos_self_deployment_plugin import swap_orchestrator as orchestrator_module  # noqa: E402
from macos_self_deployment_plugin import swap_orchestrator_preflight as preflight_module  # noqa: E402
from macos_self_deployment_plugin import swap_orchestrator_types as types_module  # noqa: E402
from macos_self_deployment_plugin.constants import (  # noqa: E402
    COLOR_BLUE,
    RestartReasonCode,
)
from macos_self_deployment_plugin.preflight_probe_runner import (  # noqa: E402
    PROBE_ERROR_HARNESS,
    ProbeOutcome,
)
from macos_self_deployment_plugin.release_manager import (  # noqa: E402
    CandidatePaths,
    GcResult,
    SwapResult,
)
from macos_self_deployment_plugin.router_client import RouterClient  # noqa: E402
from macos_self_deployment_plugin.schema_preflight import (  # noqa: E402
    PreflightVerdict,
    SchemaChange,
)
from macos_self_deployment_plugin.swap_orchestrator import (  # noqa: E402
    PRE_BUILD_PROBE_RELEASE_ID,
    SwapOrchestrator,
)
from macos_self_deployment_plugin.swap_orchestrator_admission import OwnedDeployGate  # noqa: E402

# Private runtime root for the orchestrator's owned-deploy lock and pending-finisher
# record, so this smoke never touches a live solet's runtime directory.
_RUNTIME = tempfile.TemporaryDirectory(prefix="probe_gate_orchestrator_runtime_")

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _StatusOnlyRouter:
    """Only ``status()`` is reached before the pre-spawn gates decide."""

    def status(self) -> dict[str, Any]:
        return {
            "active_color": COLOR_BLUE,
            "active_instance_id": "blue-id",
            "colors": [],
        }


class _RecordingReleaseManager:
    """Builds a synthetic candidate; records cutover/gc calls."""

    def __init__(self) -> None:
        self.build_count = 0
        self.cutover_count = 0
        self.gc_count = 0

    def gc(self, *, keep: int | None = None) -> GcResult:
        del keep
        self.gc_count += 1
        return GcResult(deleted=(), retained=())

    def build_candidate(
        self,
        *,
        manifest_etag: str = "",
        manifest_plugins: tuple[str, ...] | None = None,
        schema_snapshot_fn: object = None,
    ) -> CandidatePaths:
        del manifest_etag, manifest_plugins, schema_snapshot_fn
        self.build_count += 1
        base = Path("/nonexistent/rel-probe-gate")
        return CandidatePaths(
            release_id="rel-probe-gate",
            release_dir=base,
            code_root=base / "code",
            venv_python=base / "venv" / "bin" / "python3",
            version_file=base / "VERSION",
            missing_pth_targets=(),
            schema_snapshot=None,
        )

    def cutover(self, candidate: CandidatePaths) -> SwapResult:
        del candidate
        self.cutover_count += 1
        return SwapResult(current="rel-probe-gate", previous=None)

    @property
    def current_release(self) -> str | None:
        return None

    @property
    def previous_release(self) -> str | None:
        return None

    def current_schema_snapshot(self) -> dict[str, object] | None:
        return None

    def candidate_for(self, release_id: str) -> CandidatePaths:
        del release_id
        raise NotImplementedError("forward-path smoke: candidate_for unused")

    def rollback(self) -> SwapResult:
        raise NotImplementedError("forward-path smoke: rollback unused")


def _additive_preflight(
    candidate: CandidatePaths,
    *,
    current_snapshot: dict[str, object] | None,
    current_release_exists: bool,
) -> PreflightVerdict:
    del candidate, current_snapshot, current_release_exists
    return PreflightVerdict(is_additive=True, breaking_changes=())


def _non_additive_preflight(
    candidate: CandidatePaths,
    *,
    current_snapshot: dict[str, object] | None,
    current_release_exists: bool,
) -> PreflightVerdict:
    del candidate, current_snapshot, current_release_exists
    return PreflightVerdict(
        is_additive=False,
        breaking_changes=(
            SchemaChange(
                kind="column_removed",
                namespace="plugin",
                table="thing",
                column="gone",
                detail="column dropped",
            ),
        ),
    )


class _Harness:
    """One orchestrator wiring with recording spawn + probe seams."""

    def __init__(self, *, schema_preflight: Any, probe: Any) -> None:
        self.release_mgr = _RecordingReleaseManager()
        self.spawn_calls: list[str] = []
        # The post-build probe of the candidate; the r68 pre-build root-manifest
        # probe of the source tree is recorded apart so these pins stay about
        # the candidate gate.
        self.probe_calls: list[str] = []
        self.pre_build_probe_calls: list[str] = []
        self._probe = probe

        def _spawn(
            app_home: Path,
            next_color: str,
            next_instance_id: str,
            solet_name: str,
            candidate: CandidatePaths,
        ) -> int:
            del app_home, next_color, solet_name, candidate
            self.spawn_calls.append(next_instance_id)
            raise OSError("smoke spawn seam: deliberately failing after the gate")

        def _submit(
            action_definition: dict[str, object],
            context: dict[str, object] | None = None,
        ) -> str:
            del action_definition, context
            return "ae-should-not-happen"

        def _probe_seam(*, candidate: CandidatePaths, app_home: Path) -> ProbeOutcome:
            del app_home
            if candidate.release_id == PRE_BUILD_PROBE_RELEASE_ID:
                self.pre_build_probe_calls.append(candidate.release_id)
            else:
                self.probe_calls.append(candidate.release_id)
            outcome = self._probe(candidate)
            return cast("ProbeOutcome", outcome)

        action_factory = cast(
            "Any",
            type("_AF", (), {"submit_action_definition": staticmethod(_submit)})(),
        )
        self.orch = SwapOrchestrator(
            router_client=cast("RouterClient", _StatusOnlyRouter()),
            action_factory=action_factory,
            session_factory=lambda: "sess-probe-gate",
            solet_name="smoke",
            release_manager=self.release_mgr,
            schema_preflight=schema_preflight,
            preflight_probe=_probe_seam,
            set_color_active=lambda _active: None,
            spawn_fn=_spawn,
            ready_timeout_seconds=2,
            ready_poll_interval_seconds=0.01,
            runtime_dir=Path(_RUNTIME.name),
        )

    def restart(self) -> Any:
        return self.orch.restart(
            reason="probe-gate-smoke",
            expected_etag="etag-x",
            dry_run=False,
            app_home=Path("/nonexistent/profile"),
            self_instance_id="blue-id",
            self_color=COLOR_BLUE,
            set_active_targets=[],
        )


def _red_probe(candidate: CandidatePaths) -> ProbeOutcome:
    return ProbeOutcome(
        ok=False,
        payload={
            "failing_step": "L1.1_import",
            "error_class": "RuntimeError",
            "detail": "planted probe rejection",
            "failures": [{"check": "L1.1_import", "plugin": "p", "message": "m", "error_class": "RuntimeError"}],
            "release_id": candidate.release_id,
        },
    )


def _green_probe(candidate: CandidatePaths) -> ProbeOutcome:
    return ProbeOutcome(
        ok=True,
        payload={
            "ok": True,
            "duration_ms": 1,
            "release_id": candidate.release_id,
        },
    )


def _raising_probe(candidate: CandidatePaths) -> ProbeOutcome:
    del candidate
    raise RuntimeError("planted probe seam crash")


def _case_red_probe_blocks() -> None:
    """[1-3] RED probe blocks before spawn."""
    harness = _Harness(schema_preflight=_additive_preflight, probe=_red_probe)
    result = harness.restart()
    _check(
        result.status.value == "probe_failed" and result.reason_code == RestartReasonCode.PROBE_REJECTED,
        f"[1] RED probe ⇒ PROBE_FAILED/probe_rejected ({result.status}, {result.reason_code!r})",
    )
    _check(
        result.probe is not None and result.probe.get("error_class") == "RuntimeError" and result.probe.get("failures"),
        f"[2] rejection payload rides RestartResult.probe ({result.probe})",
    )
    _check(
        harness.spawn_calls == [] and harness.release_mgr.cutover_count == 0 and harness.probe_calls == ["rel-probe-gate"],
        f"[3] NO spawn, NO cutover; probe consulted once against the built candidate (spawn={harness.spawn_calls}, probe={harness.probe_calls})",
    )


def _case_raising_seam_contained() -> None:
    """[4-5] raising probe seam is CONTAINED (A5a)."""
    harness = _Harness(schema_preflight=_additive_preflight, probe=_raising_probe)
    try:
        result = harness.restart()
        escaped = False
    except Exception:  # noqa: BLE001 — the pin IS "nothing escapes"
        escaped = True
        result = None
    _check(
        not escaped and result is not None and result.status.value == "probe_failed" and result.probe is not None and result.probe.get("error_class") == PROBE_ERROR_HARNESS,
        f"[4] raising probe seam contained ⇒ PROBE_FAILED/ProbeHarnessError, nothing escaped (escaped={escaped})",
    )
    _check(
        harness.spawn_calls == [],
        f"[5] NO spawn on the contained-raise path ({harness.spawn_calls})",
    )


def _case_schema_refuses_first() -> None:
    """[6-7] ordering: schema preflight short-circuits BEFORE the probe."""
    harness = _Harness(schema_preflight=_non_additive_preflight, probe=_green_probe)
    result = harness.restart()
    _check(
        result.status.value == "failed" and result.reason_code == RestartReasonCode.SCHEMA_PREFLIGHT_REFUSED,
        f"[6] non-additive schema still refuses first ({result.status}, {result.reason_code!r})",
    )
    _check(
        harness.probe_calls == [],
        f"[7] probe NOT consulted when the cheaper gate already refused ({harness.probe_calls})",
    )


def _case_green_passes_through() -> None:
    """[8-9] GREEN probe passes the gate through to the spawn step."""
    harness = _Harness(schema_preflight=_additive_preflight, probe=_green_probe)
    result = harness.restart()
    _check(
        result.status.value == "failed" and result.reason_code == RestartReasonCode.SPAWN_FAILED,
        f"[8] GREEN probe ⇒ flow proceeds past the gate to spawn (stopped by the deliberate spawn failure: {result.reason_code!r})",
    )
    _check(
        harness.probe_calls == ["rel-probe-gate"] and len(harness.spawn_calls) == 1,
        f"[9] probe ran exactly once, spawn WAS attempted (probe={harness.probe_calls}, spawn={harness.spawn_calls})",
    )


def _case_extracted_api_exports() -> None:
    """The original imports and private entry still reach real preparation."""
    _check(
        orchestrator_module.OwnedDeployGate is OwnedDeployGate
        and orchestrator_module.ReleaseManagerProtocol is types_module.ReleaseManagerProtocol
        and orchestrator_module.SchemaPreflightFn is types_module.SchemaPreflightFn
        and orchestrator_module.PreflightProbeFn is types_module.PreflightProbeFn
        and orchestrator_module._SwapPlan is types_module._SwapPlan
        and orchestrator_module._resolve_current_snapshot is preflight_module._resolve_current_snapshot,
        "original module exports the extracted contracts and helper without wrappers",
    )


def _case_extracted_preparation() -> None:
    """Keep the private prepare entry and evidence handoff intact."""
    harness = _Harness(schema_preflight=_additive_preflight, probe=_green_probe)
    with patch.object(orchestrator_module, "prepare_swap", wraps=preflight_module.prepare_swap) as prepare:
        plan = harness.orch._prepare_swap(
            reason="extracted-preparation",
            expected_etag="etag-api",
            app_home=Path("/nonexistent/profile"),
            self_color=COLOR_BLUE,
            self_instance_id="blue-id",
        )
    _check(
        isinstance(plan, types_module._SwapPlan) and plan.next_color == "green" and plan.candidate.release_id == "rel-probe-gate" and plan.probe_evidence == {"ok": True, "duration_ms": 1, "release_id": "rel-probe-gate"} and harness.spawn_calls == [] and harness.release_mgr.gc_count == 1,
        "preparation returns the immutable real candidate and probe proof before spawn",
    )
    _check(
        prepare.call_count == 1 and prepare.call_args.kwargs["release_manager"] is harness.release_mgr and prepare.call_args.kwargs["failure"] == harness.orch._failure,
        "private entry delegates once with its existing manager and bound failure builder",
    )


def _case_extracted_failure_callback() -> None:
    """Refusals use the existing overridable failure method."""
    refused = _Harness(schema_preflight=_non_additive_preflight, probe=_green_probe)
    with patch.object(refused.orch, "_failure", wraps=refused.orch._failure) as failure:
        result = refused.restart()
    _check(
        failure.call_count == 1 and failure.call_args.kwargs["reason_code"] == RestartReasonCode.SCHEMA_PREFLIGHT_REFUSED and result.reason_code == RestartReasonCode.SCHEMA_PREFLIGHT_REFUSED and refused.spawn_calls == [] and refused.probe_calls == [],
        "extracted refusal preserves the orchestrator failure seam and stops before probe/spawn",
    )


def run_smoke() -> int:
    print("=== probe_gate_orchestrator_smoke (S4: probe gate in _prepare_swap) ===")
    _case_extracted_api_exports()
    _case_extracted_preparation()
    _case_extracted_failure_callback()
    _case_red_probe_blocks()
    _case_raising_seam_contained()
    _case_schema_refuses_first()
    _case_green_passes_through()

    print(f"\nprobe_gate_orchestrator_smoke: {_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(run_smoke())
