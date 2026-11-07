"""Platform dev-surface plugin — quality_service (GTE-02) implementation.

Thin service-provider plugin. Inherits the plain ``QualityServiceInterface``
contract ABC and is bound to the ``quality_service`` interface; the decorated
discovery surface lives in ``ananta/services/quality_service/interfaces/
public.py`` (framework services), and the verbs are reachable as
``service_interface::quality_service::{list_gates,run_gate,run_test}``.

The class body stays deliberately small: identity + readiness + three verbs
that log their server-built ``CallContext`` principal and delegate to
:class:`QualityOperations`. All gate/smoke command forms are baked server-side
in :mod:`quality.gate_registry`. (B3 Half-1 adds ``RepoServiceInterface`` to
this same plugin.)
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.quality_service_interface import QualityServiceInterface
from ananta.interfaces.repo_service_interface import RepoServiceInterface

from platform_dev_surface_plugin.quality.operations import QualityOperations
from platform_dev_surface_plugin.repo.operations import RepoOperations
from platform_dev_surface_plugin.repo.patch_store import PatchStore, get_patch_proposal_schema
from platform_dev_surface_plugin.repo_root import locate_repo_root

if TYPE_CHECKING:
    from ananta.core.actions.action_factory import ActionFactory
    from ananta.core.services.call_context import CallContext
    from ananta.types.schema_types import SchemaDefinition

PLUGIN_NAME = "platform_dev_surface_plugin"
_ENV_SOLET_NAME = "SOLET_NAME"


def _principal_label(call_context: CallContext | None) -> str:
    """A compact, log-safe description of the calling principal (never None-noise)."""
    if call_context is None:
        return "unknown"
    detail = call_context.calling_plugin or call_context.principal_id or "-"
    return f"{call_context.principal_kind}:{detail}"


class PlatformDevSurfacePlugin(PluginBase, QualityServiceInterface, RepoServiceInterface):
    """Serves ``quality_service`` (GTE-02) and ``repo_service`` (read-only repo)."""

    name: str = PLUGIN_NAME

    service_interfaces: ClassVar[tuple[type, ...]] = (
        QualityServiceInterface,
        RepoServiceInterface,
    )
    supported_interface_versions: ClassVar[dict[type, str]] = {
        QualityServiceInterface: QualityServiceInterface.INTERFACE_VERSION,
        RepoServiceInterface: RepoServiceInterface.INTERFACE_VERSION,
    }

    def __init__(self) -> None:
        super().__init__()
        self.name = PLUGIN_NAME
        self.logger: logging.Logger = logging.getLogger(self.name)
        self._quality: QualityOperations | None = None
        self._repo: RepoOperations | None = None

    def get_schema_definitions(self) -> list[SchemaDefinition]:
        """SchemaProvider: declare the propose_patch artifact table (auto-installed)."""
        return [get_patch_proposal_schema()]

    def prepare_for_readiness(self) -> None:
        """Locate the worktree root (via APP_HOME), capture identity, build ops."""
        solet_name = os.environ.get(_ENV_SOLET_NAME)
        if not solet_name:
            raise RuntimeError(f"{PLUGIN_NAME}: {_ENV_SOLET_NAME} env var is required (set by the launching script).")
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{PLUGIN_NAME}: orchestrator_ref not injected before readiness")
        # Anchor at APP_HOME (deploy-invariant, its parent IS the worktree) —
        # NOT __file__, which points at the materialized release copy at deploy.
        repo_root = locate_repo_root(Path(self.orchestrator_ref.APP_HOME))
        state_service = self.orchestrator_ref.get_service("state_service")
        if state_service is None:
            raise RuntimeError(f"{PLUGIN_NAME}: state_service not available (required for propose_patch)")
        self._quality = QualityOperations(repo_root, solet_name)
        self._repo = RepoOperations(repo_root, PatchStore(state_service))  # type: ignore[arg-type]
        self.logger.info("%s ready — worktree root %s", PLUGIN_NAME, repo_root)
        self.set_ready()

    def set_action_factory(self, action_factory: ActionFactory) -> None:
        """The actual late-boot hook, after init_service_manager (F1)."""
        super().set_action_factory(action_factory)
        if self._quality is not None:
            try:
                self.ensure_full_smoke_schedule()
            except Exception:
                self.logger.exception("full smoke schedule ensure failed; retry at next injection or explicit ensure")

    def set_active(self, active: bool) -> None:
        """Inactive blue/green providers retain schedules but produce no work."""
        if self._quality is not None:
            self._quality.full_battery.active = active

    def _quality_ops(self) -> QualityOperations:
        if self._quality is None:
            raise RuntimeError(f"{PLUGIN_NAME}: not ready (prepare_for_readiness not run)")
        return self._quality

    def _repo_ops(self) -> RepoOperations:
        if self._repo is None:
            raise RuntimeError(f"{PLUGIN_NAME}: not ready (prepare_for_readiness not run)")
        return self._repo

    # ------------------------------------------------------------------
    # QualityServiceInterface
    # ------------------------------------------------------------------

    def list_gates(self, *, call_context: CallContext | None = None) -> dict[str, Any]:
        self.logger.info("quality_service.list_gates by %s", _principal_label(call_context))
        return self._quality_ops().list_gates()

    def run_gate(self, gate: str, *, call_context: CallContext | None = None) -> dict[str, Any]:
        self.logger.info(
            "quality_service.run_gate gate=%s by %s",
            gate,
            _principal_label(call_context),
        )
        return self._quality_ops().run_gate(gate)

    def run_test(self, smoke: str | None = None, *, call_context: CallContext | None = None) -> dict[str, Any]:
        self.logger.info(
            "quality_service.run_test target=%s by %s",
            smoke or "suite",
            _principal_label(call_context),
        )
        return self._quality_ops().run_test(smoke)

    def verify_hash_manifest(
        self,
        unit_id: str,
        root_path: str,
        manifest: dict[str, str],
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        self.logger.info("quality_service.verify_hash_manifest unit=%s by %s", unit_id, _principal_label(call_context))
        return self._quality_ops().verify_hash_manifest(unit_id, root_path, manifest)

    def predict_gate_smokes_merge(
        self,
        base_ref: str,
        lane_root_path: str,
        current_master: str,
        register_path: str = "quality_gates/gate_smokes.txt",
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        self.logger.info("quality_service.predict_gate_smokes_merge by %s", _principal_label(call_context))
        return self._quality_ops().predict_gate_smokes_merge(
            base_ref,
            lane_root_path,
            current_master,
            register_path,
        )

    def detect_scope_regex_gaps(
        self,
        paths: list[str],
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        self.logger.info("quality_service.detect_scope_regex_gaps by %s", _principal_label(call_context))
        return self._quality_ops().detect_scope_regex_gaps(paths)

    # ------------------------------------------------------------------
    # RepoServiceInterface (read-only)
    # ------------------------------------------------------------------

    def search(
        self,
        query: str,
        path_glob: str | None = None,
        max_results: int = 50,
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        self.logger.info("repo_service.search by %s", _principal_label(call_context))
        return self._repo_ops().search(query, path_glob, max_results)

    def read_file(
        self,
        path: str,
        start_line: int | None = None,
        end_line: int | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        self.logger.info("repo_service.read_file path=%s by %s", path, _principal_label(call_context))
        return self._repo_ops().read_file(path, start_line, end_line)

    def list_files(
        self,
        path: str | None = None,
        depth: int = 1,
        glob: str | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        self.logger.info("repo_service.list_files path=%s by %s", path or ".", _principal_label(call_context))
        return self._repo_ops().list_files(path, depth, glob)

    def git_status(self, *, call_context: CallContext | None = None) -> dict[str, Any]:
        self.logger.info("repo_service.git_status by %s", _principal_label(call_context))
        return self._repo_ops().git_status()

    def git_diff(
        self,
        ref: str | None = None,
        path: str | None = None,
        staged: bool = False,
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        self.logger.info("repo_service.git_diff by %s", _principal_label(call_context))
        return self._repo_ops().git_diff(ref, path, staged)

    def propose_patch(self, unified_diff: str, *, call_context: CallContext | None = None) -> dict[str, Any]:
        self.logger.info("repo_service.propose_patch by %s", _principal_label(call_context))
        return self._repo_ops().propose_patch(unified_diff, principal=_principal_label(call_context))

    def select_smokes(self, request: dict[str, Any], *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Compute immutable focused selection and current clean proof reuse."""
        self.logger.info("quality_service.select_smokes by %s", _principal_label(call_context))
        return self._quality_ops().smoke_proof("select", principal=_principal_label(call_context), request=request)

    def run_selected_smokes(self, request: dict[str, Any], *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Execute the complete computed selection and append honest per-smoke attempts."""
        self.logger.info("quality_service.run_selected_smokes by %s", _principal_label(call_context))
        if call_context is None:
            raise PermissionError("proof mutation requires server CallContext")
        return self._quality_ops().smoke_proof("run", principal=_principal_label(call_context), request=request)

    def get_smoke_proof(self, key: str, as_of: int | None = None, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Read the current or historical smoke proof projection."""
        self.logger.info("quality_service.get_smoke_proof by %s", _principal_label(call_context))
        return self._quality_ops().smoke_proof("get", principal=_principal_label(call_context), key=key, as_of=as_of)

    def list_smoke_proofs(self, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """List current smoke proof projections without modifying history."""
        self.logger.info("quality_service.list_smoke_proofs by %s", _principal_label(call_context))
        return {"proofs": self._quality_ops().smoke_proof("list", principal=_principal_label(call_context))}

    def get_smoke_proof_history(self, key: str, as_of: int | None = None, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Read immutable smoke proof history, optionally through one version."""
        self.logger.info("quality_service.get_smoke_proof_history by %s", _principal_label(call_context))
        return {"events": self._quality_ops().smoke_proof("history", principal=_principal_label(call_context), key=key, as_of=as_of)}

    def correct_smoke_proof(self, key: str, metadata: dict[str, Any], expected_version: int, reason: str, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Set or correct optional proof links with current-version authorization."""
        self.logger.info("quality_service.correct_smoke_proof by %s", _principal_label(call_context))
        if call_context is None:
            raise PermissionError("proof mutation requires server CallContext")
        return self._quality_ops().smoke_proof("correct", principal=_principal_label(call_context), key=key, metadata=metadata, expected_version=expected_version, reason=reason)

    def clear_smoke_proof(self, key: str, fields: list[str], clear_reuse: bool, expected_version: int, reason: str, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Clear optional metadata or active reuse while retaining all observations."""
        self.logger.info("quality_service.clear_smoke_proof by %s", _principal_label(call_context))
        if call_context is None:
            raise PermissionError("proof mutation requires server CallContext")
        return self._quality_ops().smoke_proof("clear", principal=_principal_label(call_context), key=key, fields=fields, clear_reuse=clear_reuse, expected_version=expected_version, reason=reason)

    def retire_smoke_proof(self, key: str, expected_version: int, reason: str, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Retire a proof from reuse with append-only versioned history."""
        self.logger.info("quality_service.retire_smoke_proof by %s", _principal_label(call_context))
        if call_context is None:
            raise PermissionError("proof mutation requires server CallContext")
        return self._quality_ops().smoke_proof("retire", principal=_principal_label(call_context), key=key, expected_version=expected_version, reason=reason)

    def reinstate_smoke_proof(self, key: str, request: dict[str, Any], expected_version: int, reason: str, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Revalidate a current frozen input key before reinstating a clean proof."""
        self.logger.info("quality_service.reinstate_smoke_proof by %s", _principal_label(call_context))
        if call_context is None:
            raise PermissionError("proof mutation requires server CallContext")
        return self._quality_ops().smoke_proof("reinstate", principal=_principal_label(call_context), key=key, request=request, expected_version=expected_version, reason=reason)

    def ensure_full_smoke_schedule(self, *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Ensure the six-hour full battery and five-minute durable retry schedules."""
        self.logger.info("quality_service.ensure_full_smoke_schedule by %s", _principal_label(call_context))
        from platform_dev_surface_plugin.quality.full_battery import ensure_full_smoke_schedule

        scheduler = self.orchestrator_ref.get_service("scheduling_service") if self.orchestrator_ref else None
        return ensure_full_smoke_schedule(scheduler)

    def trigger_full_smoke_battery(self, trigger: str = "schedule", *, call_context: CallContext | None = None) -> dict[str, Any]:
        """Queue a full battery or retry pending work off the action queue."""
        self.logger.info("quality_service.trigger_full_smoke_battery by %s", _principal_label(call_context))
        if call_context is None:
            raise PermissionError("full battery trigger requires server CallContext")
        return self._quality_ops().full_battery.trigger(trigger)
