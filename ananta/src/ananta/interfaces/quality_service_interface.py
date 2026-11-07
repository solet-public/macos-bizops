"""Quality Service Interface — run the platform's own quality gates + smokes.

The platform ships a suite of quality gates (``quality_gates/*.py``) and a
gate-eligible smoke register (``quality_gates/gate_smokes.txt`` together with
every fragment under ``quality_gates/gate_smokes.d/``, which register smokes on
equal terms). This
service exposes three thin, allowlist-only verbs over that machinery so an
external MCP client (or the ``run_joseki`` driver's live e2e run) can
enumerate the gates, run one gate by name, and run the smoke suite —
without ever passing a free-form path, argv, or flag.

Every command form is baked SERVER-side; callers pick a gate/smoke by NAME
from the server-side registry. An unknown name is a typed rejection, not a
free-form shell escape — the name allowlist IS the security boundary.

This service is READ-ONLY over the toolchain: it executes gate scripts and
smokes and reports their verdicts. It never mutates the repo. The verbs are
a convenience / driver surface — the AUTHORITATIVE pre-commit evidence for
Git-Controller is still the gate's own command forms run at commit time.

Plugins implementing this interface should:
1. Define ``service_interfaces`` returning a tuple containing
   ``QualityServiceInterface``.
2. Define ``supported_interface_versions`` mapping the interface to its version.
3. Bake every gate/smoke command form server-side (no caller-supplied argv).

See: ``workbench/2026-07-05_b3_repo_and_gates_primitives_design.md`` §3.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from ananta.core.services.call_context import CallContext


class QualitySmokeProofInterface(ABC):
    """The coherent frozen-selection, proof and campaign contract."""

    @abstractmethod
    def select_smokes(self, request: dict[str, Any], *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Compute immutable focused selection and current clean proof reuse."""
        ...

    @abstractmethod
    def run_selected_smokes(self, request: dict[str, Any], *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Execute the complete computed selection and append honest per-smoke attempts."""
        ...

    @abstractmethod
    def get_smoke_proof(self, key: str, as_of: int | None = None, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Read the current or historical smoke proof projection."""
        ...

    @abstractmethod
    def list_smoke_proofs(self, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """List current smoke proof projections without modifying history."""
        ...

    @abstractmethod
    def get_smoke_proof_history(self, key: str, as_of: int | None = None, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Read immutable smoke proof history, optionally through one version."""
        ...

    @abstractmethod
    def correct_smoke_proof(self, key: str, metadata: dict[str, Any], expected_version: int, reason: str, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Set or correct optional proof links with current-version authorization."""
        ...

    @abstractmethod
    def clear_smoke_proof(self, key: str, fields: list[str], clear_reuse: bool, expected_version: int, reason: str, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Clear optional metadata or active reuse while retaining all observations."""
        ...

    @abstractmethod
    def retire_smoke_proof(self, key: str, expected_version: int, reason: str, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Retire a proof from reuse with append-only versioned history."""
        ...

    @abstractmethod
    def reinstate_smoke_proof(self, key: str, request: dict[str, Any], expected_version: int, reason: str, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Revalidate a current frozen input key before reinstating a clean proof."""
        ...

    @abstractmethod
    def ensure_full_smoke_schedule(self, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Ensure the six-hour full battery and five-minute durable retry schedules."""
        ...

    @abstractmethod
    def trigger_full_smoke_battery(self, trigger: str = "schedule", *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Queue a full battery or retry pending work off the action queue."""
        ...


class QualityServiceInterface(QualitySmokeProofInterface):
    """Enumerate, run, and read-only verify platform quality/landing evidence."""

    INTERFACE_VERSION: ClassVar[str] = "1.2.0"

    @abstractmethod
    def list_gates(self, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Read-only enumeration of the server-side gate registry + smoke register.

        Returns the known gates (name, kind, description, direct/routed status,
        and ``timeout_seconds``, always ``None`` because there is no per-gate
        execution deadline) together with the smoke register read from
        the gate register: ``gate_smokes.txt`` plus its fragments. Executes nothing.
        """
        ...

    @abstractmethod
    def run_gate(self, gate: str, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Run ONE gate by allowlisted name; report pass/fail + bounded output.

        The name maps to a fixed ``quality_gates/<script>.py`` argv (with
        ``--allowlist`` / ``--warn-only`` where applicable) baked server-side.
        An unknown gate name raises a typed error — the name allowlist is the
        security boundary; no free-form argv ever reaches the shell.
        """
        ...

    @abstractmethod
    def run_test(self, smoke: str | None = None, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Run the gate-eligible smoke suite, or one registered smoke by path.

        With no argument, runs the full suite via ``run_smokes.py`` against the
        tracked register. With a ``smoke`` path, that path must be present in
        the register (allowlist boundary) or the verb raises a typed error;
        the single smoke is then run directly.
        """
        ...

    @abstractmethod
    def verify_hash_manifest(
        self,
        unit_id: str,
        root_path: str,
        manifest: dict[str, str],
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        """Rehash a declared manifest under one worktree/candidate-tree root."""
        ...

    @abstractmethod
    def predict_gate_smokes_merge(
        self,
        base_ref: str,
        lane_root_path: str,
        current_master: str,
        register_path: str = "quality_gates/gate_smokes.txt",
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        """Predict a no-write three-way merge of an append-only debt register."""
        ...

    @abstractmethod
    def detect_scope_regex_gaps(
        self,
        paths: list[str],
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        """Classify paths that fall outside the per-file static-gate surface."""
        ...
