"""Structural release/preflight contracts and the immutable pre-spawn swap plan.

These existing contracts are re-exported by ``swap_orchestrator``. The plan is
created for one restart and read by the executor handoff; it has no durable
state or independent update/retirement lifecycle.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ananta.interfaces.lifecycle_result_types import RestartResult

from macos_self_deployment_plugin.preflight_probe_runner import ProbeOutcome
from macos_self_deployment_plugin.release_manager import CandidatePaths, GcResult, SwapResult
from macos_self_deployment_plugin.schema_preflight import PreflightVerdict


class SchemaPreflightFn(Protocol):
    """The §3 gate: classify the candidate against the resolved current snapshot.

    ``current_snapshot`` is the orchestrator-resolved OLD side (read from the
    current ``VERSION`` or DERIVED from ``current/code``); ``current_release_exists``
    distinguishes a genuine bootstrap from a derive that failed to a None.
    """

    def __call__(
        self,
        candidate: CandidatePaths,
        *,
        current_snapshot: dict[str, object] | None,
        current_release_exists: bool,
    ) -> PreflightVerdict: ...


class PreflightProbeFn(Protocol):
    """GTE-06 L2 gate: fresh-source-probe the candidate with its OWN interpreter.

    Returns a classified :class:`ProbeOutcome`; the production seam
    (:func:`preflight_probe_runner.run_preflight_probe`) NEVER raises
    (A5a). The orchestrator still wraps the call defensively so an
    injected seam that violates the contract is contained to a
    ``PROBE_FAILED`` result rather than escaping as ``plugin_raised``
    core-side (which would leave the committed manifest bytes with no
    rollback).
    """

    def __call__(self, *, candidate: CandidatePaths, app_home: Path) -> ProbeOutcome: ...


class ReleaseManagerProtocol(Protocol):
    """The subset of :class:`~.release_manager.ReleaseManager` the swap uses.

    Declared structurally so smokes can inject a fake that returns a
    synthetic :class:`CandidatePaths` and records the ``cutover`` /
    ``rollback`` calls without CoW-cloning the real 1.8 GB tree — mirroring
    the ``spawn_fn`` injection seam. Carries both the forward-cutover surface
    (``build_candidate`` / ``cutover``) and the durable-rollback surface
    (``previous_release`` / ``candidate_for`` / ``rollback``).
    """

    @property
    def current_release(self) -> str | None: ...

    @property
    def previous_release(self) -> str | None: ...

    def build_candidate(
        self,
        *,
        manifest_etag: str = ...,
        manifest_plugins: tuple[str, ...] | None = ...,
        schema_snapshot_fn: Callable[[Path], dict[str, object]] | None = ...,
        reconciliation_provenance: dict[str, str] | None = ...,
    ) -> CandidatePaths: ...

    def candidate_for(self, release_id: str) -> CandidatePaths: ...

    def current_schema_snapshot(self) -> dict[str, object] | None: ...

    def cutover(self, candidate: CandidatePaths) -> SwapResult: ...

    def rollback(self) -> SwapResult: ...

    def gc(self, *, keep: int | None = ...) -> GcResult: ...


@dataclass(frozen=True, slots=True)
class _SwapPlan:
    """The validated pre-spawn plan: which color to bring up, on which release.

    ``probe_evidence`` is the GREEN L2 probe's success payload (Q5); the
    forward cutover attaches it to the QUEUED result so the applied
    envelope carries positive proof the probe executed.
    """

    next_color: str
    candidate: CandidatePaths
    probe_evidence: dict[str, Any] | None = None


class PreflightFailureFn(Protocol):
    """The orchestrator's existing failure builder, passed to preflight."""

    def __call__(
        self,
        *,
        reason: str,
        expected_etag: str,
        message: str,
        reason_code: str,
    ) -> RestartResult: ...


__all__ = ["SchemaPreflightFn", "PreflightProbeFn", "ReleaseManagerProtocol", "_SwapPlan", "PreflightFailureFn"]
