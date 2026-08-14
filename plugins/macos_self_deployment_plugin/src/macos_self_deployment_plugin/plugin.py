"""macOS self-deployment plugin entry point.

Implements two service interfaces (per D16' strict-tuple-membership):

- :class:`SelfDeploymentServiceInterface` — the generic single-verb
  ``restart_with_manifest`` surface that ``apply_manifest`` delegates
  to. The local implementation runs an L2 probe (the canonical local
  L2 probe pattern), spawns the next color via ``python -m ananta.cli``
  (NOT ``launch.py``, which would kill blue before green is up), polls
  the router until green registers, activates green, quiesces local
  plugins via ``set_active(False)``, and enqueues a durable
  ``complete_swap`` action for green's ``action_queue_poller`` to pick
  up.
- :class:`LocalSelfDeploymentServiceInterface` — the local-blue-green
  3-verb operational surface (``complete_swap`` / ``swap_status`` /
  ``swap_rollback``) for the durable finisher, status introspection,
  and operator-triggered drain-window rollback.

Following the cloud sibling pattern at
``plugins/aws_self_deployment_plugin/.../plugin.py``: each verb has a
paired interface method (matching the ABC signature) and an
``@platform_process`` action method (matching the platform action
dispatcher's ``(params, state)`` signature). The orchestration
mechanics live in :mod:`swap_orchestrator`; this module keeps the
class body thin so the god-class gate stays well under threshold.

See ``workbench/2026-06-01_local_blue_green_L3_implementation_plan.md``
§3.3 for the slice spec.
"""

from __future__ import annotations

import logging
import os
import signal
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar, Final, cast

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ActionStatus, ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)
from ananta.interfaces.lifecycle_result_types import (
    AutostartResult,
    RestartResult,
    StopSelfResult,
)
from ananta.interfaces.local_self_deployment_service_interface import (
    LocalSelfDeploymentServiceInterface,
)
from ananta.interfaces.self_deployment_service_interface import (
    SelfDeploymentServiceInterface,
)

from macos_self_deployment_plugin import (
    drain_sentinel,
    heartbeat_lifecycle,
    process_identity,
    stale_runtime_cleanup,
    stop_self_runner,
    stop_self_watchdog,
)
from macos_self_deployment_plugin.autostart_manager import AutostartManager
from macos_self_deployment_plugin.constants import (
    AGENT_MESSAGING_PLUGIN_NAME,
    BRIDGE_PORT_ATTRIBUTE,
    COLOR_BLUE,
    CONFIG_KEY_PREFLIGHT_PROBE_TIMEOUT_SECONDS,
    DEFAULT_PREFLIGHT_PROBE_TIMEOUT_SECONDS,
    DEFAULT_PRIOR_TERM_GRACE_SECONDS,
    DEFAULT_PRIOR_TERM_POLL_INTERVAL_SECONDS,
    DEFAULT_ROUTER_SOCKET_POLL_INTERVAL_SECONDS,
    DEFAULT_ROUTER_SOCKET_WAIT_SECONDS,
    ENV_SOLET_COLOR,
    ENV_SOLET_INSTANCE_ID,
    ENV_SOLET_NAME,
    PLUGIN_NAME,
    RESULT_TYPE_AUTOSTART_INSTALL,
    RESULT_TYPE_AUTOSTART_STATUS,
    RESULT_TYPE_AUTOSTART_UNINSTALL,
    RESULT_TYPE_COMPLETE_SWAP,
    RESULT_TYPE_RESTART,
    RESULT_TYPE_ROLLBACK,
    RESULT_TYPE_ROLLBACK_RELEASE,
    RESULT_TYPE_STATUS,
    RESULT_TYPE_STOP_SELF,
    ROUTER_SOCKET_SUFFIX,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_ROLLBACK_NOT_APPLICABLE,
    STATUS_ROLLED_BACK,
    is_valid_color,
)
from macos_self_deployment_plugin.pending_finisher import (
    PendingFinisher,
    clear_pending_finisher,
    pending_finisher_path,
    read_pending_finisher,
)
from macos_self_deployment_plugin.preflight_probe_runner import (
    ProbeOutcome,
    run_preflight_probe,
)
from macos_self_deployment_plugin.release_manager import (
    CandidatePaths,
    ReleaseManager,
    ReleaseManagerError,
)
from macos_self_deployment_plugin.router_client import (
    RouterClient,
    RouterClientError,
)
from macos_self_deployment_plugin.schema_preflight import (
    KIND_CANDIDATE_SNAPSHOT_MISSING,
    KIND_CURRENT_SNAPSHOT_UNRESOLVED,
    PreflightVerdict,
    SchemaChange,
    SchemaSnapshot,
    classify_snapshot_diff,
)
from macos_self_deployment_plugin.swap_orchestrator import (
    SetActiveTarget,
    SpawnFn,
    SwapOrchestrator,
    default_spawn,
)

if TYPE_CHECKING:
    from ananta.core.actions.action_factory import ActionFactory


# ---------------------------------------------------------------------
# Parameter metadata (verb signatures)
# ---------------------------------------------------------------------

# The one finisher step-status meaning "the prior may STILL be alive" — SIGTERM
# was denied (PermissionError), unlike the converged statuses (gone / killed /
# pid-reused). complete_swap must NOT clear the durable record on this status, so
# the heartbeat backstop can retry (symmetry with the backstop's TERMINATE_FAILED
# which likewise preserves the record).
_PRIOR_SIGTERM_DENIED: Final[str] = "prior_sigterm_denied"


_NEW_MANIFEST_PARAM = ParameterMetadata(
    description=(
        "The manifest dict apply_manifest just wrote to disk. Carried "
        "for audit + forward-compat; the on-disk file at "
        "<APP_HOME>/config/manifest.yaml is canonical."
    ),
    required=True,
    type=ParameterType.OBJECT,
)
_REASON_PARAM = ParameterMetadata(
    description="Operator-supplied audit string recorded in the restart envelope.",
    required=True,
    type=ParameterType.STRING,
)
_EXPECTED_ETAG_PARAM = ParameterMetadata(
    description="Manifest ETag CAS lock (see lifecycle_interfaces_design §13.2).",
    required=True,
    type=ParameterType.STRING,
)
_EXPECTED_CURRENT_RELEASE_PARAM = ParameterMetadata(
    description=(
        "Concurrency CAS for rollback_release: the rel-<id> the caller observed "
        "as the live `current` release. A mismatch against the actual "
        "ReleaseManager.current_release returns FAILED(stale_current_release) "
        "before any spawn (someone else deployed/rolled back since)."
    ),
    required=True,
    type=ParameterType.STRING,
)
_DRY_RUN_PARAM = ParameterMetadata(
    description="If true, plan + report without spawning or activating.",
    required=False,
    type=ParameterType.BOOLEAN,
    default=False,
)
_PRIOR_PID_PARAM = ParameterMetadata(
    description="OS pid of the prior color, recorded at enqueue time.",
    required=True,
    type=ParameterType.INTEGER,
)
_PRIOR_INSTANCE_ID_PARAM = ParameterMetadata(
    description="Router-side instance id of the prior color.",
    required=True,
    type=ParameterType.STRING,
)
_PRIOR_COLOR_PARAM = ParameterMetadata(
    description="Color token of the prior color (blue/green).",
    required=True,
    type=ParameterType.STRING,
)
_ROLLBACK_REASON_PARAM = ParameterMetadata(
    description="Operator-supplied reason for the rollback, recorded for audit.",
    required=True,
    type=ParameterType.STRING,
)
_AUTOSTART_DRY_RUN_PARAM = ParameterMetadata(
    description=(
        "If true, plan + report without writing the LaunchAgent plist "
        "or invoking launchctl."
    ),
    required=False,
    type=ParameterType.BOOLEAN,
    default=False,
)
_STOP_SELF_REASON_PARAM = ParameterMetadata(
    description=(
        "Operator-supplied audit string for the stop. Required — a stop "
        "without an audit message is undisciplined operator action."
    ),
    required=True,
    type=ParameterType.STRING,
)
_STOP_SELF_DRY_RUN_PARAM = ParameterMetadata(
    description=(
        "If true, plan + report without writing the drain sentinel or "
        "spawning the SIGTERM watchdog."
    ),
    required=False,
    type=ParameterType.BOOLEAN,
    default=False,
)


# ---------------------------------------------------------------------



def _restart_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="restart_with_manifest / rollback_release outcome (RestartResult).",
        properties={
            "status": ParameterMetadata(type=ParameterType.STRING, description="."),
            "restart_action_id": ParameterMetadata(type=ParameterType.STRING, description="."),
            "message": ParameterMetadata(type=ParameterType.STRING, description="."),
            "reason": ParameterMetadata(type=ParameterType.STRING, description="."),
            "expected_etag": ParameterMetadata(type=ParameterType.STRING, description="."),
            "dry_run": ParameterMetadata(type=ParameterType.BOOLEAN, description="."),
            "reason_code": ParameterMetadata(type=ParameterType.STRING, description="."),
        },
    )


def _restart_result_to_envelope(result: RestartResult) -> dict[str, object]:
    """Flatten a :class:`RestartResult` into the success-envelope ``data`` dict.

    Shared by ``restart_with_manifest_action`` + ``rollback_release_action`` so
    the envelope shape (incl. the ``reason_code`` partition field) is built in
    one place and both action wrappers stay thin (plugin.py is MI-sensitive).
    """
    return {
        "status": result.status.value,
        "restart_action_id": result.restart_action_id,
        "message": result.message,
        "reason": result.reason,
        "expected_etag": result.expected_etag,
        "dry_run": result.dry_run,
        "reason_code": result.reason_code,
    }


def _complete_swap_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="complete_swap outcome.",
        properties={
            "status": ParameterMetadata(type=ParameterType.STRING, description="."),
            "prior_instance_id": ParameterMetadata(type=ParameterType.STRING, description="."),
            "prior_color": ParameterMetadata(type=ParameterType.STRING, description="."),
            "steps_completed": ParameterMetadata(type=ParameterType.LIST, description="."),
        },
    )


def _swap_status_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="swap_status outcome.",
        properties={
            "router_status": ParameterMetadata(type=ParameterType.OBJECT, description="."),
            "swap_in_progress": ParameterMetadata(type=ParameterType.BOOLEAN, description="."),
            "self_color": ParameterMetadata(type=ParameterType.STRING, description="."),
            "self_instance_id": ParameterMetadata(type=ParameterType.STRING, description="."),
        },
    )


def _swap_rollback_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="swap_rollback outcome.",
        properties={
            "status": ParameterMetadata(type=ParameterType.STRING, description="."),
            "rolled_back_to": ParameterMetadata(type=ParameterType.STRING, description="."),
            "reason": ParameterMetadata(type=ParameterType.STRING, description="."),
        },
    )


def _stop_self_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="stop_self outcome.",
        properties={
            "status": ParameterMetadata(type=ParameterType.STRING, description="."),
            "reason": ParameterMetadata(type=ParameterType.STRING, description="."),
            "duration_seconds": ParameterMetadata(type=ParameterType.FLOAT, description="."),
            "stopped_at": ParameterMetadata(type=ParameterType.STRING, description="."),
            "backend_action_id": ParameterMetadata(type=ParameterType.STRING, description="."),
            "dry_run": ParameterMetadata(type=ParameterType.BOOLEAN, description="."),
            "message": ParameterMetadata(type=ParameterType.STRING, description="."),
        },
    )


def _autostart_return_schema() -> ReturnValueSchema:
    return ReturnValueSchema(
        type=ParameterType.OBJECT,
        description="Autostart-verb outcome.",
        properties={
            "status": ParameterMetadata(type=ParameterType.STRING, description="."),
            "verb": ParameterMetadata(type=ParameterType.STRING, description="."),
            "solet_name": ParameterMetadata(type=ParameterType.STRING, description="."),
            "label": ParameterMetadata(type=ParameterType.STRING, description="."),
            "plist_path": ParameterMetadata(type=ParameterType.STRING, description="."),
            "prior_state": ParameterMetadata(type=ParameterType.STRING, description="."),
            "last_run_at": ParameterMetadata(type=ParameterType.STRING, description="."),
            "dry_run": ParameterMetadata(type=ParameterType.BOOLEAN, description="."),
            "message": ParameterMetadata(type=ParameterType.STRING, description="."),
        },
    )


# ---------------------------------------------------------------------
# Runtime-dir helper (matches port_manager's resolution convention).
# ---------------------------------------------------------------------


def _runtime_dir() -> Path:
    xdg_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if xdg_runtime:
        return Path(xdg_runtime) / "ananta"
    return Path.home() / ".ananta" / "runtime"


def _router_socket_path(solet_name: str) -> Path:
    return _runtime_dir() / f"{solet_name}{ROUTER_SOCKET_SUFFIX}"


def _wait_for_router_socket(socket_path: Path) -> None:
    """Bounded wait for the router's unix socket, then fail loudly if absent.

    The router is a SEPARATE KeepAlive LaunchAgent that comes up independently
    of the solet; at a fresh BIRTH both agents load ~simultaneously (RunAtLoad), so
    the main boot can reach the readiness router-socket check a beat before the
    router has created its socket. Poll a bounded window rather than failing
    one-shot on that benign race (the FATAL first-boot + LaunchAgent-restart
    cycle observed on fresh seed births). Still raises past the window -- a
    genuinely absent router is a real error (L3 plan §3.5).
    """
    deadline = time.monotonic() + DEFAULT_ROUTER_SOCKET_WAIT_SECONDS
    while not socket_path.exists() and time.monotonic() < deadline:
        time.sleep(DEFAULT_ROUTER_SOCKET_POLL_INTERVAL_SECONDS)
    if not socket_path.exists():
        raise RuntimeError(
            f"{PLUGIN_NAME}: router socket not found at {socket_path} after "
            f"waiting {DEFAULT_ROUTER_SOCKET_WAIT_SECONDS:.0f}s. Install the "
            "router via plugins/macos_self_deployment_plugin/src/"
            "macos_self_deployment_plugin/blue_green_router/install_router.py "
            "before binding deployment_service to this plugin."
        )


def _resolve_project_root_for_autostart() -> Path:
    """Resolve the live working-tree root from ``APP_HOME`` — NOT ``__file__``.

    A release-spawned colour runs ``<release>/code/.../plugin.py``, so the
    historical ``Path(__file__).parents[4]`` walk resolved to
    ``<release>/code`` — a directory with NO ``.venv`` (the venv is the
    *sibling* ``<release>/venv``). The ReleaseManager snapshots
    ``source_root/.venv`` to clone the next release, so a release-to-release
    cutover from there fails outright. ``APP_HOME`` is the SHARED profile
    (``<repo>/profile``) for every spawned colour regardless of which
    release's code is executing (cli.py bakes ``--app-home`` → the env var,
    child_spawn always passes the shared profile), so its parent is always
    the live ``.venv``-bearing working tree the ReleaseManager and the
    autostart plist must point at. Mirrors
    ``swap_orchestrator._resolve_project_root``; fast-fails by returning the
    profile itself if the parent lacks a project marker.
    """
    app_home = Path(os.environ["APP_HOME"]).resolve()
    candidate = app_home.parent
    if (candidate / "pyproject.toml").is_file() or (candidate / "ananta").is_dir():
        return candidate
    return app_home


def _autostart_envelope(result: AutostartResult) -> dict[str, Any]:
    """Flatten AutostartResult → action-method success-envelope dict."""
    return {
        "status": result.status.value,
        "verb": result.verb,
        "solet_name": result.solet_name,
        "label": result.label,
        "plist_path": result.plist_path,
        "prior_state": result.prior_state,
        "last_run_at": result.last_run_at,
        "dry_run": result.dry_run,
        "message": result.message,
    }


# ---------------------------------------------------------------------
# Plugin class
# ---------------------------------------------------------------------


class MacosSelfDeploymentPlugin(  # noqa: D101 — class docstring on first line below
    PluginBase,
    EdgeProcessProvider,
    LocalSelfDeploymentServiceInterface,
):
    """Local blue-green self-deployment plugin (router-mediated swap)."""

    name: str = PLUGIN_NAME

    service_interfaces: ClassVar[tuple[type, ...]] = (
        SelfDeploymentServiceInterface,
        LocalSelfDeploymentServiceInterface,
    )
    supported_interface_versions: ClassVar[dict[type, str]] = {
        SelfDeploymentServiceInterface: SelfDeploymentServiceInterface.INTERFACE_VERSION,
        LocalSelfDeploymentServiceInterface: (
            LocalSelfDeploymentServiceInterface.LOCAL_INTERFACE_VERSION
        ),
    }

    def __init__(self) -> None:
        super().__init__()
        self.name = PLUGIN_NAME
        self.logger: logging.Logger = logging.getLogger(self.name)
        self._solet_name: str = ""
        self._self_color: str = ""
        self._self_instance_id: str = ""
        self._router_client: RouterClient | None = None
        self._orchestrator: SwapOrchestrator | None = None
        self._swap_in_progress: bool = False
        self._heartbeat_thread: threading.Thread | None = None
        self._heartbeat_stop: threading.Event = threading.Event()
        # Overridable for smokes — production path uses default_spawn.
        self._spawn_fn: SpawnFn = default_spawn
        # Overridable for smokes — production lazy-creates from defaults
        # (canonical ~/Library/LaunchAgents/) at first verb call.
        self._autostart_manager: AutostartManager | None = None
        # Materialized-release lifecycle (design 2026-06-27). Overridable for
        # smokes; production lazy-creates against ``~/.ananta/releases/<name>/``
        # at first use. Owns build_candidate / cutover / reconcile.
        self._release_manager: ReleaseManager | None = None
        # Smoke-only overrides bundled into a single attribute so the
        # class stays under the god-class instance_attrs threshold. The
        # `set_*_for_smoke` setters mutate this dict in place;
        # production-only paths never touch it.
        self._smoke_overrides: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # Lifecycle / dependency wiring
    # ------------------------------------------------------------------

    def set_action_factory(self, action_factory: ActionFactory) -> None:
        """Platform-injected; required so restart_with_manifest can enqueue complete_swap."""
        self.action_factory = action_factory

    def set_spawn_fn(self, spawn_fn: SpawnFn) -> None:
        """Smoke-only override for the green-spawn helper."""
        self._spawn_fn = spawn_fn

    def set_release_manager(self, manager: ReleaseManager) -> None:
        """Smoke-only override for the materialized-release manager.

        Production verbs lazy-create a :class:`ReleaseManager` against the
        canonical ``~/.ananta/releases/<name>/`` root on first use. Smokes
        inject one configured against a throwaway ``~/.ananta`` scratch
        root so build/cutover/reconcile never touch the operator's real
        releases (and never CoW-clone the real 1.8 GB tree).
        """
        self._release_manager = manager

    def set_autostart_manager(self, manager: AutostartManager) -> None:
        """Smoke-only override for the LaunchAgent manager.

        Production verbs lazy-create an :class:`AutostartManager` with
        canonical paths (``~/Library/LaunchAgents/``) the first time an
        autostart verb is called. Smokes inject one configured against
        a tmpfs ``plist_dir`` so the round-trip never touches the
        operator's real LaunchAgents.
        """
        self._autostart_manager = manager

    def prepare_for_readiness(self) -> None:
        """Validate router socket exists, build client + orchestrator, kick off self-register.

        Per L3 plan §3.5: fail loudly if the router socket is absent — after a
        bounded wait (`_wait_for_router_socket`) that tolerates the birth-time
        race where the router KeepAlive agent creates its socket a beat after
        the main boot. Do NOT silently fall back to L0 — that violates fast-fail
        discipline and hides install gaps.
        """
        solet_name = os.environ.get(ENV_SOLET_NAME)
        if not solet_name:
            msg = (
                f"{PLUGIN_NAME}: {ENV_SOLET_NAME} env var is required "
                "(set by the launching script)."
            )
            raise RuntimeError(msg)
        self._solet_name = solet_name

        # F2 Phase 0c: scrub stale runtime files (left by a crashed prior
        # the solet or router) BEFORE the router-socket check or any port-
        # binding fires. Per the two-phase platform startup contract,
        # prepare_for_readiness runs before any plugin's start_services
        # (where port-binding happens), so this is the canonical cold-
        # start safety net for the crash-mid-drain window per Slice 1.5.
        stale_runtime_cleanup.cleanup_and_restore(solet_name)

        # §4.6 startup reconcile: if a prior cutover/rollback died mid-swap,
        # forward-complete the durable current/previous symlinks to the
        # ledger's recorded end state before anything (autostart, a later
        # swap) reads `current`. No-ops cleanly when no releases root exists
        # (the pre-materialized-release state). Has no DB/action_factory
        # dependency, so it is safe this early in readiness.
        self._reconcile_releases()

        socket_path = _router_socket_path(solet_name)
        _wait_for_router_socket(socket_path)
        self._router_client = RouterClient(socket_path)

        self._self_color = os.environ.get(ENV_SOLET_COLOR, "") or COLOR_BLUE
        if not is_valid_color(self._self_color):
            msg = (
                f"{PLUGIN_NAME}: {ENV_SOLET_COLOR}={self._self_color!r} "
                "is not a recognized color (expected blue/green)."
            )
            raise RuntimeError(msg)
        self._self_instance_id = (
            os.environ.get(ENV_SOLET_INSTANCE_ID, "").strip()
            or f"solet-{self._self_color}-{uuid.uuid4().hex[:8]}"
        )

        # SwapOrchestrator is lazily constructed by _require_orchestrator on
        # first use (Task #19 fix). Platform-side injection order is
        # `start_service_plugins` (which runs `prepare_for_readiness`) THEN
        # `init_actions` (which calls `set_action_factory`), so building the
        # orchestrator here would capture `self.action_factory=None`
        # permanently. Per `action_processor.py:_setup_plugin_context` the
        # architectural pattern is "platform pushes the current factory at
        # use time; plugins must read self.action_factory at each call." We
        # mirror `aws_self_deployment_plugin._build_deployer` — the deployer
        # is constructed per-verb with the live factory; same pattern here.
        self._spawn_heartbeat_thread()
        self.set_ready()

    def _spawn_heartbeat_thread(self) -> None:
        """Spawn the daemon thread that runs the heartbeat lifecycle.

        The lifecycle itself (bind-wait → bounded-window register →
        steady-state heartbeat) lives in :mod:`heartbeat_lifecycle`
        outside the class body so the god-class gate stays under
        threshold. The plugin owns only the threading primitives plus
        the cross-plugin port lookup (which needs ``orchestrator_ref``).
        """
        if self._heartbeat_thread is not None or self._router_client is None:
            return
        sigterm_callback = self._smoke_overrides.get(
            "sigterm_callback",
            heartbeat_lifecycle.real_sigterm_callback(self.logger),
        )
        target = self._build_heartbeat_runner(sigterm_callback)
        thread = threading.Thread(
            target=target,
            name=f"{PLUGIN_NAME}-heartbeat",
            daemon=True,
        )
        thread.start()
        self._heartbeat_thread = thread

    def _build_heartbeat_runner(
        self, sigterm_callback: heartbeat_lifecycle.SigtermCallback,
    ) -> Callable[[], None]:
        """Return a thread target bound to the current plugin state."""
        client = self._router_client
        if client is None:
            return lambda: None
        self_color = self._self_color
        self_instance_id = self._self_instance_id
        stop_event = self._heartbeat_stop
        logger = self.logger
        port_lookup = self._lookup_bridge_port
        budget_override = self._smoke_overrides.get("budget_seconds")
        # B2: the active color's heartbeat loop runs the pending-finisher
        # backstop against this path each tick (live runtime dir; the same
        # file the swap executor writes + complete_swap clears). The
        # current-release lookup gates the backstop on durability (B2·1): it
        # only acts once ``current`` names the candidate the record describes.
        pending_finisher_file = pending_finisher_path(
            _runtime_dir(), self._solet_name,
        )
        current_release_lookup = self._lookup_current_release

        def _run() -> None:
            kwargs: dict[str, Any] = {
                "client": client,
                "self_color": self_color,
                "self_instance_id": self_instance_id,
                "port_lookup": port_lookup,
                "stop_event": stop_event,
                "sigterm_callback": sigterm_callback,
                "pending_finisher_file": pending_finisher_file,
                "current_release_lookup": current_release_lookup,
                "logger": logger,
            }
            if budget_override is not None:
                kwargs["budget_seconds"] = budget_override
            heartbeat_lifecycle.run(**kwargs)

        return _run

    def set_sigterm_callback_for_smoke(
        self, callback: heartbeat_lifecycle.SigtermCallback,
    ) -> None:
        """Smoke-only override for the failed-registration SIGTERM callback."""
        self._smoke_overrides["sigterm_callback"] = callback

    def set_budget_seconds_for_smoke(self, seconds: float) -> None:
        """Smoke-only override for the unified transient-state budget."""
        self._smoke_overrides["budget_seconds"] = seconds

    def set_watchdog_spawner_for_smoke(
        self, spawner: stop_self_watchdog.WatchdogSpawner,
    ) -> None:
        """Smoke-only override for the stop_self detached watchdog spawner."""
        self._smoke_overrides["watchdog_spawner"] = spawner

    def _lookup_bridge_port(self) -> int | None:
        """Read ``agent_messaging_plugin.bridge_port`` via the plugin manager.

        Mirrors :meth:`_collect_set_active_targets`'s duck-typed
        cross-plugin lookup so the contract surface stays consistent.
        Returns the port held in-process by ``agent_messaging_plugin``
        once its ``start_interface`` has allocated and bound, or
        ``None`` during the readiness ordering window.
        """
        orch = getattr(self, "orchestrator_ref", None)
        if orch is None:
            return None
        plugin_manager = getattr(orch, "plugin_manager", None)
        if plugin_manager is None:
            return None
        plugins = getattr(plugin_manager, "plugins", None)
        if plugins is None:
            return None
        plugin = plugins.get(AGENT_MESSAGING_PLUGIN_NAME)
        if plugin is None or not hasattr(plugin, BRIDGE_PORT_ATTRIBUTE):
            return None
        port = getattr(plugin, BRIDGE_PORT_ATTRIBUTE)
        return port if isinstance(port, int) else None

    def _lookup_current_release(self) -> str | None:
        """Release id ``current`` names — the B2·1 durability gate for the backstop.

        Pure read of the release ledger's ``current`` symlink (no ledger
        mutation). The pending-finisher backstop only acts once this equals the
        record's ``candidate_release_id``, i.e. once the swap is observably
        durable.
        """
        return self._get_release_manager().current_release

    # ------------------------------------------------------------------
    # EdgeProcessProvider implementation
    # ------------------------------------------------------------------

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        return {
            "restart_with_manifest": EdgeProcessDefinition(
                name="restart_with_manifest",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=RESULT_TYPE_RESTART,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "rollback_release": EdgeProcessDefinition(
                name="rollback_release",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=RESULT_TYPE_ROLLBACK_RELEASE,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "stop_self": EdgeProcessDefinition(
                name="stop_self",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=RESULT_TYPE_STOP_SELF,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "complete_swap": EdgeProcessDefinition(
                name="complete_swap",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=RESULT_TYPE_COMPLETE_SWAP,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "swap_status": EdgeProcessDefinition(
                name="swap_status",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=RESULT_TYPE_STATUS,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            "swap_rollback": EdgeProcessDefinition(
                name="swap_rollback",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=RESULT_TYPE_ROLLBACK,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "install_autostart": EdgeProcessDefinition(
                name="install_autostart",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=RESULT_TYPE_AUTOSTART_INSTALL,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "uninstall_autostart": EdgeProcessDefinition(
                name="uninstall_autostart",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=RESULT_TYPE_AUTOSTART_UNINSTALL,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "status_autostart": EdgeProcessDefinition(
                name="status_autostart",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type=RESULT_TYPE_AUTOSTART_STATUS,
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
        }

    # ------------------------------------------------------------------
    # Interface methods (direct callables)
    # ------------------------------------------------------------------

    def restart_with_manifest(
        self,
        *,
        new_manifest: dict[str, Any],
        expected_etag: str,
        reason: str,
        dry_run: bool = False,
    ) -> RestartResult:
        """SelfDeploymentServiceInterface impl — router-mediated blue-green swap.

        The on-disk manifest at ``<APP_HOME>/config/manifest.yaml`` is
        the source of truth; ``new_manifest`` is carried for audit only.
        The on-disk ETag CAS is enforced at the apply_manifest layer;
        ``expected_etag`` is echoed in the result for caller correlation.
        """
        del new_manifest  # on-disk manifest is canonical per ABC.
        orchestrator = self._require_orchestrator()
        app_home = Path(os.environ.get("APP_HOME") or "/app")
        targets = list(self._collect_set_active_targets())
        self._swap_in_progress = True
        try:
            return orchestrator.restart(
                reason=reason,
                expected_etag=expected_etag,
                dry_run=dry_run,
                app_home=app_home,
                self_instance_id=self._self_instance_id,
                self_color=self._self_color,
                set_active_targets=targets,
            )
        finally:
            self._swap_in_progress = False

    def rollback_release(
        self,
        *,
        reason: str,
        expected_etag: str,
        expected_current_release: str,
    ) -> RestartResult:
        """LocalSelfDeploymentServicePublicAPI impl — durable code rollback.

        DISTINCT from ``swap_rollback`` (the in-drain-window router re-point):
        this brings the prior materialized release back up and flips the
        durable ``current``/``previous`` symlinks, so it works at any time.
        Thin delegator into :meth:`SwapOrchestrator.rollback_release` —
        gathers ``app_home`` + self identity + quiesce targets exactly as
        ``restart_with_manifest`` does. ``expected_current_release`` is the
        ``rel-<id>`` the caller observed as ``current`` — the concurrency CAS
        (Architect ruling (c)): a mismatch returns FAILED before any spawn.
        Returns the RestartResult partition (queued / failed /
        needs_intervention).
        """
        orchestrator = self._require_orchestrator()
        app_home = Path(os.environ.get("APP_HOME") or "/app")
        targets = list(self._collect_set_active_targets())
        self._swap_in_progress = True
        try:
            return orchestrator.rollback_release(
                reason=reason,
                expected_etag=expected_etag,
                expected_current_release=expected_current_release,
                app_home=app_home,
                self_instance_id=self._self_instance_id,
                self_color=self._self_color,
                set_active_targets=targets,
            )
        finally:
            self._swap_in_progress = False

    def stop_self(
        self,
        *,
        reason: str,
        dry_run: bool = False,
    ) -> StopSelfResult:
        """SelfDeploymentServiceInterface impl — see ABC docstring for the contract.

        Slice 4.5. Thin delegator into :func:`stop_self_runner.run`; the
        body lives there to keep the class under the god-class threshold.
        """
        return stop_self_runner.run(
            solet_name=self._solet_name,
            reason=reason,
            dry_run=dry_run,
            watchdog_spawner=self._smoke_overrides.get(
                "watchdog_spawner", stop_self_watchdog.spawn,
            ),
        )

    def complete_swap(
        self,
        prior_pid: int,
        prior_instance_id: str,
        prior_color: str,
    ) -> dict[str, Any]:
        """LocalSelfDeploymentServiceInterface impl — identity-verified SIGTERM + unregister.

        Held inside :func:`drain_sentinel.held` so the LaunchAgent's
        ``PathState`` predicate sees the sentinel for the entire SIGTERM +
        unregister window. The ``try/finally`` inside the context manager
        guarantees the sentinel is removed even if either step raises —
        without that guarantee a crash mid-drain would leave a stale
        sentinel that suppressed all future LaunchAgent respawns.

        Codex round-2 B2·3: the normal (action-driven) finisher and the
        heartbeat backstop now share ONE source of truth — the durable
        pending-finisher record — and the SAME PID-reuse guard. The prior is
        SIGTERM'd ONLY when its live start-time token still matches the token
        the swap captured (``process_identity``): a recycled pid is unmasked
        and unregistered-without-signalling, never killed. An ABSENT record
        means the backstop already converged (SIGTERM+unregister+clear) or no
        record was ever written — an idempotent no-op (no bare-pid kill). The
        action params are cross-checked against the record but the record is
        authoritative.
        """
        steps_completed: list[str] = []
        finisher_path = pending_finisher_path(_runtime_dir(), self._solet_name)
        with drain_sentinel.held(self._solet_name):
            record = read_pending_finisher(finisher_path)
            if record is None:
                steps_completed.append("pending_finisher_absent_noop")
            else:
                if (
                    record.prior_pid != prior_pid
                    or record.prior_instance_id != prior_instance_id
                ):
                    self.logger.warning(
                        "complete_swap action (pid=%d instance=%s) differs from the "
                        "durable record (pid=%d instance=%s); trusting the record.",
                        prior_pid, prior_instance_id,
                        record.prior_pid, record.prior_instance_id,
                    )
                signal_status = self._signal_verified_prior(record)
                steps_completed.append(signal_status)
                steps_completed.append(self._safe_unregister(record.prior_instance_id))
                # Symmetry with the backstop's TERMINATE_FAILED: only a denied
                # SIGTERM leaves the prior possibly-alive, so keep the record for
                # the backstop to retry; every other status means the prior is
                # gone / killed / pid-reused → safe to clear.
                if signal_status == _PRIOR_SIGTERM_DENIED:
                    steps_completed.append("pending_finisher_kept_sigterm_denied")
                else:
                    clear_pending_finisher(finisher_path)
                    steps_completed.append("pending_finisher_cleared")
        return {
            "status": STATUS_COMPLETED,
            "prior_instance_id": record.prior_instance_id if record else prior_instance_id,
            "prior_color": record.prior_color if record else prior_color,
            "steps_completed": steps_completed,
        }

    def _signal_verified_prior(self, record: PendingFinisher) -> str:
        """SIGTERM the prior ONLY if its live identity token still matches (B2·3).

        Mirrors the heartbeat backstop's identity gate so both finisher paths
        refuse to signal a recycled pid: a missing live token means the prior is
        already gone; a mismatched token means the pid was reused by an unrelated
        process (never signal it).
        """
        live_token = process_identity.start_token(record.prior_pid)
        if live_token is None:
            return "prior_already_gone"
        if live_token != record.prior_start_token:
            return "prior_pid_reused_skip_sigterm"
        return self._signal_and_wait(record.prior_pid)

    def swap_status(self) -> dict[str, Any]:
        """LocalSelfDeploymentServiceInterface impl — router.status() + local in-flight state."""
        client = self._require_client()
        try:
            router_snap = client.status()
        except RouterClientError as exc:
            return {
                "router_status": {"error": str(exc)},
                "swap_in_progress": self._swap_in_progress,
                "self_color": self._self_color,
                "self_instance_id": self._self_instance_id,
            }
        return {
            "router_status": router_snap,
            "swap_in_progress": self._swap_in_progress,
            "self_color": self._self_color,
            "self_instance_id": self._self_instance_id,
        }

    def swap_rollback(self, reason: str) -> dict[str, Any]:
        """LocalSelfDeploymentServiceInterface impl — drain-window rollback."""
        client = self._require_client()
        try:
            snap = client.status()
        except RouterClientError as exc:
            return {
                "status": STATUS_FAILED,
                "rolled_back_to": "",
                "reason": f"router status() failed: {exc}",
            }
        prior_color = self._resolve_prior_color(snap)
        if prior_color is None:
            return {
                "status": STATUS_ROLLBACK_NOT_APPLICABLE,
                "rolled_back_to": "",
                "reason": (
                    "no prior color is currently inside its drain window — "
                    "rollback is no longer applicable."
                ),
            }
        try:
            result = client.rollback(prior_color)
        except RouterClientError as exc:
            return {
                "status": STATUS_FAILED,
                "rolled_back_to": "",
                "reason": f"router rollback({prior_color}) failed: {exc}",
            }
        if not result.get("rolled_back"):
            return {
                "status": STATUS_ROLLBACK_NOT_APPLICABLE,
                "rolled_back_to": "",
                "reason": str(result.get("reason") or "router refused rollback"),
            }
        # C2: the prior color is router-active again, so restore this process's
        # action-queue poller gate (the swap set it False during quiesce). The
        # flag is per-process; this restores it on the reactivated instance.
        self._set_color_active(True)
        return {
            "status": STATUS_ROLLED_BACK,
            "rolled_back_to": str(result.get("active_color") or prior_color),
            "reason": reason,
        }

    # ------------------------------------------------------------------
    # Action-method wrappers (@platform_process — action dispatcher shape)
    # ------------------------------------------------------------------

    @platform_process(
        name="restart_with_manifest",
        context_handling=ContextHandling.NONE,
        parameters={
            "new_manifest": _NEW_MANIFEST_PARAM,
            "expected_etag": _EXPECTED_ETAG_PARAM,
            "reason": _REASON_PARAM,
            "dry_run": _DRY_RUN_PARAM,
        },
        output_type="object",
        output_description="Blue-green restart envelope.",
        return_value_schema=_restart_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=True,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=RESULT_TYPE_RESTART,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def restart_with_manifest_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        del state
        new_manifest_raw = params.get("new_manifest") or {}
        new_manifest = new_manifest_raw if isinstance(new_manifest_raw, dict) else {}
        expected_etag = str(params.get("expected_etag") or "")
        reason = str(params.get("reason") or "operator-restart")
        dry_run = bool(params.get("dry_run") or False)
        try:
            result = self.restart_with_manifest(
                new_manifest=new_manifest,
                expected_etag=expected_etag,
                reason=reason,
                dry_run=dry_run,
            )
        except Exception as err:  # noqa: BLE001 — return structured failure
            self.logger.exception("restart_with_manifest crashed")
            return self._error_envelope(STATUS_FAILED, str(err))
        return self._success_envelope(_restart_result_to_envelope(result))

    @platform_process(
        name="rollback_release",
        context_handling=ContextHandling.NONE,
        parameters={
            "reason": _REASON_PARAM,
            "expected_etag": _EXPECTED_ETAG_PARAM,
            "expected_current_release": _EXPECTED_CURRENT_RELEASE_PARAM,
        },
        output_type="object",
        output_description="rollback_release envelope (durable code rollback).",
        return_value_schema=_restart_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=True,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=RESULT_TYPE_ROLLBACK_RELEASE,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def rollback_release_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        del state
        expected_etag = str(params.get("expected_etag") or "")
        expected_current_release = str(params.get("expected_current_release") or "")
        reason = str(params.get("reason") or "operator-rollback-release")
        try:
            result = self.rollback_release(
                reason=reason, expected_etag=expected_etag,
                expected_current_release=expected_current_release,
            )
        except Exception as err:  # noqa: BLE001 — return structured failure
            self.logger.exception("rollback_release crashed")
            return self._error_envelope(STATUS_FAILED, str(err))
        return self._success_envelope(_restart_result_to_envelope(result))

    @platform_process(
        name="stop_self",
        context_handling=ContextHandling.NONE,
        parameters={
            "reason": _STOP_SELF_REASON_PARAM,
            "dry_run": _STOP_SELF_DRY_RUN_PARAM,
        },
        output_type="object",
        output_description="stop_self envelope (drain sentinel + detached watchdog).",
        return_value_schema=_stop_self_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=True,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=RESULT_TYPE_STOP_SELF,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def stop_self_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        del state
        reason = str(params.get("reason") or "")
        dry_run = bool(params.get("dry_run") or False)
        if not reason:
            return self._error_envelope(
                "missing_args",
                "stop_self requires a non-empty reason string.",
            )
        try:
            result = self.stop_self(reason=reason, dry_run=dry_run)
        except Exception as err:  # noqa: BLE001 — return structured failure
            self.logger.exception("stop_self crashed")
            return self._error_envelope(STATUS_FAILED, str(err))
        return self._success_envelope(
            {
                "status": result.status.value,
                "reason": result.reason,
                "duration_seconds": result.duration_seconds,
                "stopped_at": result.stopped_at,
                "backend_action_id": result.backend_action_id,
                "dry_run": result.dry_run,
                "message": result.message,
            },
        )

    @platform_process(
        name="complete_swap",
        context_handling=ContextHandling.NONE,
        parameters={
            "prior_pid": _PRIOR_PID_PARAM,
            "prior_instance_id": _PRIOR_INSTANCE_ID_PARAM,
            "prior_color": _PRIOR_COLOR_PARAM,
        },
        output_type="object",
        output_description="complete_swap outcome.",
        return_value_schema=_complete_swap_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        is_long_running=True,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=RESULT_TYPE_COMPLETE_SWAP,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def complete_swap_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        del state
        prior_pid = int(params.get("prior_pid") or 0)
        prior_instance_id = str(params.get("prior_instance_id") or "")
        prior_color = str(params.get("prior_color") or "")
        if prior_pid <= 0 or not prior_instance_id:
            return self._error_envelope(
                "missing_args",
                "complete_swap requires prior_pid + prior_instance_id.",
            )
        try:
            data = self.complete_swap(prior_pid, prior_instance_id, prior_color)
        except Exception as err:  # noqa: BLE001 — return structured failure
            self.logger.exception("complete_swap crashed")
            return self._error_envelope(STATUS_FAILED, str(err))
        return self._success_envelope(data)

    @platform_process(
        name="swap_status",
        context_handling=ContextHandling.NONE,
        parameters={},
        output_type="object",
        output_description="swap_status outcome.",
        return_value_schema=_swap_status_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=RESULT_TYPE_STATUS,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    def swap_status_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        del params, state
        try:
            data = self.swap_status()
        except Exception as err:  # noqa: BLE001 — return structured failure
            self.logger.exception("swap_status crashed")
            return self._error_envelope(STATUS_FAILED, str(err))
        return self._success_envelope(data)

    @platform_process(
        name="swap_rollback",
        context_handling=ContextHandling.NONE,
        parameters={"reason": _ROLLBACK_REASON_PARAM},
        output_type="object",
        output_description="swap_rollback outcome.",
        return_value_schema=_swap_rollback_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=RESULT_TYPE_ROLLBACK,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def swap_rollback_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        del state
        reason = str(params.get("reason") or "operator-rollback")
        try:
            data = self.swap_rollback(reason)
        except Exception as err:  # noqa: BLE001 — return structured failure
            self.logger.exception("swap_rollback crashed")
            return self._error_envelope(STATUS_FAILED, str(err))
        return self._success_envelope(data)

    # ------------------------------------------------------------------
    # Autostart verbs (LaunchAgent install / uninstall / status)
    # ------------------------------------------------------------------

    def install_autostart(self, *, dry_run: bool = False) -> AutostartResult:
        """Seed ``current`` if absent, then render + load the LaunchAgent.

        Idempotent: unloads any pre-existing definition, rewrites the
        plist, re-loads. Operator runs this once per machine post-birth
        (or via the macos_midwife runbook); the LaunchAgent then fires
        at every subsequent user login.

        §4.5 role 1 (Architect verdict 2026-06-28): the plist bakes the
        literal ``current/venv/bin/python3`` symlink path with no dev-venv
        fallback, so ``current`` MUST exist before the LaunchAgent can fire
        or a cold-start would exec a dangling path. The install verb
        therefore SEEDS release-0 from the working tree when no ``current``
        exists yet — orchestrated HERE (not in the plist renderer, which
        only depends on ``current``). Skipped on ``dry_run`` (a preview must
        not materialize a release).
        """
        if not dry_run:
            self._ensure_current_release()
        return self._get_autostart_manager().install(dry_run=dry_run)

    def _ensure_current_release(self) -> None:
        """Seed release-0 when no ``current`` exists (§4.5 role-1 guarantee).

        Pure filesystem (``cp -c`` clone + ``.pth`` rewrite + symlink/ledger
        via the passive :class:`ReleaseManager`) — no running the solet needed, so
        it is safe at install time. A no-op once a release has been
        materialized (a real deploy, or a prior seed).
        """
        manager = self._get_release_manager()
        if manager.current_release is not None:
            return
        candidate = manager.build_candidate()
        manager.cutover(candidate)
        self.logger.info(
            "seeded release-0 for autostart (no prior current): %s",
            candidate.release_id,
        )

    def uninstall_autostart(self, *, dry_run: bool = False) -> AutostartResult:
        """Unload + remove the per-solet LaunchAgent.

        Idempotent: an already-absent LaunchAgent returns success with
        ``prior_state='absent'``. After this verb, the solet no
        longer auto-starts at operator login; only manual
        ``SOLET_NAME=<name> ./launch.py`` or blue-green
        ``restart_with_manifest`` brings it up.
        """
        return self._get_autostart_manager().uninstall(dry_run=dry_run)

    def status_autostart(self) -> AutostartResult:
        """Report whether the LaunchAgent is installed + loaded.

        Read-only. The expected steady state under ``KeepAlive=false``
        is ``status=INSTALLED_LOADED`` with no live PID — the
        LaunchAgent ran once at login, booted the solet, and
        exited cleanly. Operators should NOT panic at the absent PID.
        """
        return self._get_autostart_manager().status()

    @platform_process(
        name="install_autostart",
        context_handling=ContextHandling.NONE,
        parameters={"dry_run": _AUTOSTART_DRY_RUN_PARAM},
        output_type="object",
        output_description="install_autostart outcome.",
        return_value_schema=_autostart_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=RESULT_TYPE_AUTOSTART_INSTALL,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def install_autostart_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        del state
        dry_run = bool(params.get("dry_run") or False)
        try:
            result = self.install_autostart(dry_run=dry_run)
        except Exception as err:  # noqa: BLE001 — return structured failure
            self.logger.exception("install_autostart crashed")
            return self._error_envelope(STATUS_FAILED, str(err))
        return self._success_envelope(_autostart_envelope(result))

    @platform_process(
        name="uninstall_autostart",
        context_handling=ContextHandling.NONE,
        parameters={"dry_run": _AUTOSTART_DRY_RUN_PARAM},
        output_type="object",
        output_description="uninstall_autostart outcome.",
        return_value_schema=_autostart_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=RESULT_TYPE_AUTOSTART_UNINSTALL,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=False),
    )
    def uninstall_autostart_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        del state
        dry_run = bool(params.get("dry_run") or False)
        try:
            result = self.uninstall_autostart(dry_run=dry_run)
        except Exception as err:  # noqa: BLE001 — return structured failure
            self.logger.exception("uninstall_autostart crashed")
            return self._error_envelope(STATUS_FAILED, str(err))
        return self._success_envelope(_autostart_envelope(result))

    @platform_process(
        name="status_autostart",
        context_handling=ContextHandling.NONE,
        parameters={},
        output_type="object",
        output_description="status_autostart outcome.",
        return_value_schema=_autostart_return_schema(),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type=RESULT_TYPE_AUTOSTART_STATUS,
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    def status_autostart_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        del params, state
        try:
            result = self.status_autostart()
        except Exception as err:  # noqa: BLE001 — return structured failure
            self.logger.exception("status_autostart crashed")
            return self._error_envelope(STATUS_FAILED, str(err))
        return self._success_envelope(_autostart_envelope(result))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _require_client(self) -> RouterClient:
        if self._router_client is None:
            msg = f"{PLUGIN_NAME}: router client not initialized — prepare_for_readiness not called?"
            raise RuntimeError(msg)
        return self._router_client

    def _require_orchestrator(self) -> SwapOrchestrator:
        """Return a SwapOrchestrator bound to the current action_factory.

        Lazy / late binding (Task #19 fix). A smoke-injected orchestrator
        (``plugin._orchestrator = SwapOrchestrator(...)``) wins; otherwise
        we build fresh per verb. Building per verb matches the cloud
        sibling's ``_build_deployer`` pattern and ensures the orchestrator
        always sees the live ``self.action_factory`` rather than the
        ``None`` it had at ``prepare_for_readiness`` time (before the
        platform's ``init_actions`` step injects the factory).
        """
        if self._orchestrator is not None:
            return self._orchestrator
        if self._router_client is None:
            msg = (
                f"{PLUGIN_NAME}: router client not initialized — "
                "prepare_for_readiness not called?"
            )
            raise RuntimeError(msg)
        if self.action_factory is None:
            msg = (
                f"{PLUGIN_NAME}: action_factory not yet injected — "
                "restart_with_manifest invoked before init_actions ran?"
            )
            raise RuntimeError(msg)
        return SwapOrchestrator(
            router_client=self._router_client,
            action_factory=self.action_factory,
            session_factory=self._create_swap_session,
            solet_name=self._solet_name,
            release_manager=self._get_release_manager(),
            schema_preflight=self._schema_preflight,
            preflight_probe=self._run_preflight_probe,
            set_color_active=self._set_color_active,
            spawn_fn=self._spawn_fn,
            logger=self.logger,
        )

    def _run_preflight_probe(
        self, *, candidate: CandidatePaths, app_home: Path
    ) -> ProbeOutcome:
        """Production ``PreflightProbeFn`` seam (GTE-06).

        Spawns the release-side probe entrypoint under the CANDIDATE's
        own interpreter, mirroring the green spawn env/cwd contract
        (inherited env + ``SOLET_NAME``; out-of-tree runtime-dir
        cwd). NEVER raises — the runner classifies every failure mode.
        """
        log_path = (
            app_home / "data" / "logs"
            / f"preflight_probe_{uuid.uuid4().hex[:8]}.log"
        )
        return run_preflight_probe(
            candidate=candidate,
            app_home=app_home,
            solet_name=self._solet_name,
            cwd=_runtime_dir(),
            log_path=log_path,
            timeout_seconds=self._preflight_probe_timeout_seconds(),
            logger=self.logger,
        )

    def _preflight_probe_timeout_seconds(self) -> float:
        """Q1 ruling: plugin-config-surfaced timeout, constants default."""
        provider = self.config_provider
        if provider is None:
            return DEFAULT_PREFLIGHT_PROBE_TIMEOUT_SECONDS
        raw: object = provider.get(
            CONFIG_KEY_PREFLIGHT_PROBE_TIMEOUT_SECONDS,
            DEFAULT_PREFLIGHT_PROBE_TIMEOUT_SECONDS,
        )
        if not isinstance(raw, (int, float)) or isinstance(raw, bool):
            raise ValueError(
                f"{PLUGIN_NAME}: config key "
                f"{CONFIG_KEY_PREFLIGHT_PROBE_TIMEOUT_SECONDS!r} must be a "
                f"number of seconds; got {raw!r}"
            )
        return float(raw)

    def _set_color_active(self, active: bool) -> None:
        """C2: flip the platform poller's color-active gate for this process.

        Assigns the EXISTING public ``EventOrchestrator.is_active_color``
        attribute (the Slice-D consumer's setter half — Architect-sanctioned,
        not a new core method). The orchestrator flips it ``False`` at quiesce
        so the draining color's action-queue poller stops claiming actions —
        crucially, before the ``complete_swap`` row exists, so the prior
        process can never claim its own finisher. ``swap_rollback`` restores
        it ``True`` on reactivation; a fresh next-color process inits ``True``.
        Duck-typed through ``orchestrator_ref`` (the injected EventOrchestrator)
        — the same seam ``_collect_set_active_targets`` uses; a no-op when the
        ref is absent (smoke wiring without a platform orchestrator).
        """
        orch = getattr(self, "orchestrator_ref", None)
        if orch is not None:
            orch.is_active_color = active

    def _get_release_manager(self) -> ReleaseManager:
        """Return the ReleaseManager, lazy-creating with production defaults.

        Smokes pre-set via :meth:`set_release_manager` against a throwaway
        ``~/.ananta`` scratch root; production hits the canonical
        ``~/.ananta/releases/<name>/`` path and snapshots the live working
        tree (``source_root`` = repo root).
        """
        if self._release_manager is not None:
            return self._release_manager
        self._release_manager = ReleaseManager(
            solet_name=self._solet_name
            or os.environ.get(ENV_SOLET_NAME, ""),
            source_root=_resolve_project_root_for_autostart(),
            logger=self.logger,
        )
        return self._release_manager

    def _reconcile_releases(self) -> None:
        """§4.6 startup reconcile of the durable ``current``/``previous`` symlinks.

        Forward-completes an interrupted cutover/rollback to the ledger's
        recorded end state. No-ops cleanly when the releases root does not
        yet exist (pre-materialized-release state). Errors are logged, not
        raised — a reconcile failure must not block the solet from
        booting on its existing ``current`` (the swap path re-attempts on
        the next deploy).
        """
        manager = self._get_release_manager()
        try:
            result = manager.reconcile()
        except ReleaseManagerError as exc:
            self.logger.error("release reconcile failed at startup: %s", exc)
            return
        if result.action != "noop":
            self.logger.info(
                "release reconcile at startup: action=%s current=%s previous=%s",
                result.action, result.current, result.previous,
            )
        # §4.7 lifecycle GC at boot — reap any release left orphaned by a crash
        # between a build and a never-landed cutover (the per-build GC in the
        # swap path covers the running-session case; this covers crash-then-idle
        # restarts). Best-effort: a cleanup failure must not block boot.
        try:
            gc_result = manager.gc()
        except (ReleaseManagerError, OSError) as exc:
            self.logger.warning("release gc at startup failed (non-fatal): %s", exc)
            return
        if gc_result.deleted:
            self.logger.info(
                "release gc at startup reaped %d: %s",
                len(gc_result.deleted), gc_result.deleted,
            )

    def _schema_preflight(
        self,
        candidate: CandidatePaths,
        *,
        current_snapshot: dict[str, object] | None,
        current_release_exists: bool,
    ) -> PreflightVerdict:
        """§3 DDL-free gate: classify candidate-vs-current schema, FAIL-CLOSED.

        The durable code-rollback guarantee only holds over an unchanged or
        additive schema (a rolled-back binary cannot undo a DROP COLUMN / type
        change / new ``NOT NULL`` it never learned to populate), so the deploy
        is refused unless the candidate's declared schema is provably additive
        vs the current release's.

        PURE: this gate takes the already-resolved ``current_snapshot`` (the
        orchestrator reads it from the current ``VERSION``, or DERIVES it from
        ``current/code`` when the current release predates the producer — the
        B1·1 baseline derive) plus ``current_release_exists``. Keeping the I/O in
        the orchestrator (next to the candidate-snapshot producer it reuses)
        makes this gate trivially testable, but the fail-closed DECISION stays
        HERE — the derive MUST return a non-None snapshot or raise, so a None
        ``current_snapshot`` alongside an existing current release is a derive
        regression, not a bootstrap (the ``KIND_CURRENT_SNAPSHOT_UNRESOLVED``
        cell). Five cells, all on ``is None`` IDENTITY (an empty ``{}`` is a
        valid snapshot — an old tree that declared nothing):

        * candidate=None, no current release → baseline allow (first/seed deploy).
        * candidate=None, current release EXISTS → **FAIL CLOSED**: the producer
          failed to snapshot the candidate; a missing snapshot cannot be
          certified rollback-safe (``candidate_schema_snapshot_missing``).
        * candidate=present, current=present → classify; non-additive → REFUSE.
        * candidate=present, current=None, no current release → true bootstrap
          allow (candidate becomes the baseline).
        * candidate=present, current=None, current release EXISTS → **FAIL
          CLOSED**: the derive returned None instead of a snapshot-or-raise
          (``current_schema_snapshot_unresolved``).

        ``cast`` reinterprets the JSON-able dicts as the canonical typed snapshot
        at this trust boundary.
        """
        cand_snapshot = candidate.schema_snapshot
        if cand_snapshot is None:
            if not current_release_exists:
                self.logger.info(
                    "§3 schema preflight: candidate %s has no snapshot and no "
                    "current release exists (first/seed deploy); allowing as baseline.",
                    candidate.release_id,
                )
                return PreflightVerdict(is_additive=True, breaking_changes=())
            self.logger.error(
                "§3 schema preflight FAIL-CLOSED: candidate %s carries no "
                "schema_snapshot but a current release exists (producer failed); "
                "refusing the deploy — cannot certify the change rollback-safe.",
                candidate.release_id,
            )
            return PreflightVerdict(
                is_additive=False,
                breaking_changes=(
                    SchemaChange(
                        kind=KIND_CANDIDATE_SNAPSHOT_MISSING,
                        namespace="", table=None, column=None,
                        detail=(
                            "candidate has no declared-schema snapshot in steady "
                            "state (producer failed) — fail-closed"
                        ),
                    ),
                ),
            )
        if current_snapshot is None:
            if not current_release_exists:
                self.logger.info(
                    "§3 schema preflight: candidate %s has a snapshot but no current "
                    "release exists (true bootstrap); allowing as baseline.",
                    candidate.release_id,
                )
                return PreflightVerdict(is_additive=True, breaking_changes=())
            self.logger.error(
                "§3 schema preflight FAIL-CLOSED: candidate %s has a snapshot and a "
                "current release exists, but the current snapshot is unresolved "
                "(the baseline derive returned None instead of a snapshot or a "
                "raise); refusing the deploy.",
                candidate.release_id,
            )
            return PreflightVerdict(
                is_additive=False,
                breaking_changes=(
                    SchemaChange(
                        kind=KIND_CURRENT_SNAPSHOT_UNRESOLVED,
                        namespace="", table=None, column=None,
                        detail=(
                            "current release exists but its schema snapshot could "
                            "not be resolved (derive returned None) — fail-closed"
                        ),
                    ),
                ),
            )
        verdict = classify_snapshot_diff(
            cast("SchemaSnapshot", current_snapshot),
            cast("SchemaSnapshot", cand_snapshot),
        )
        log = self.logger.info if verdict.is_additive else self.logger.error
        log("§3 schema preflight: %s", verdict.summary())
        return verdict

    def _create_swap_session(self) -> str:
        """Mint a real ``core.sessions`` row via the orchestrator's SessionManager.

        Goes through state_service per ``[[state-service-is-the-only-postgres-path]]``
        — synthetic id-shaped strings would violate the row-without-row
        invariant (``core__action_events.core__sessions_id`` referring to
        a session that doesn't exist in ``core.sessions``).
        """
        orch = getattr(self, "orchestrator_ref", None)
        if orch is None:
            raise RuntimeError(
                f"{PLUGIN_NAME}: orchestrator_ref not injected; cannot mint "
                "session for complete_swap durable handoff.",
            )
        return orch.create_session(
            namespace=PLUGIN_NAME,
            context_type="self_deployment_swap",
            metadata={
                "purpose": "complete_swap_durable_handoff",
            },
        )

    def _get_autostart_manager(self) -> AutostartManager:
        """Return the AutostartManager, lazy-creating with production defaults.

        Smokes pre-set via :meth:`set_autostart_manager` with a
        sandboxed ``plist_dir``; production verbs hit this on the
        canonical ``~/Library/LaunchAgents/`` path.
        """
        if self._autostart_manager is not None:
            return self._autostart_manager
        project_root = _resolve_project_root_for_autostart()
        self._autostart_manager = AutostartManager(
            solet_name=self._solet_name or os.environ.get(ENV_SOLET_NAME, ""),
            project_root=project_root,
            logger=self.logger,
        )
        return self._autostart_manager

    def _collect_set_active_targets(self) -> Iterable[SetActiveTarget]:
        """Iterate orchestrator's plugins; yield those with set_active.

        Skips self so the swap-initiator doesn't quiesce its own
        finisher-enqueue path. duck-type per advisor guidance + Slice D
        convention.
        """
        orch = getattr(self, "orchestrator_ref", None)
        if orch is None:
            return
        plugin_manager = getattr(orch, "plugin_manager", None)
        if plugin_manager is None:
            return
        plugins = getattr(plugin_manager, "plugins", None)
        if plugins is None:
            return
        for plugin in plugins.values():
            if plugin is self:
                continue
            if hasattr(plugin, "set_active"):
                yield plugin

    def _signal_and_wait(self, pid: int) -> str:
        """SIGTERM ``pid``; poll up to grace seconds; SIGKILL on overrun.

        Returns a step-status string for the result envelope.
        """
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            return "prior_already_gone"
        except PermissionError as exc:
            self.logger.error("SIGTERM denied on pid=%d: %s", pid, exc)
            return _PRIOR_SIGTERM_DENIED
        deadline = time.monotonic() + DEFAULT_PRIOR_TERM_GRACE_SECONDS
        while time.monotonic() < deadline:
            if not _pid_alive(pid):
                return "prior_terminated_cleanly"
            time.sleep(DEFAULT_PRIOR_TERM_POLL_INTERVAL_SECONDS)
        # Grace exhausted — escalate to SIGKILL.
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            return "prior_terminated_at_grace_boundary"
        return "prior_sigkilled"

    def _safe_unregister(self, instance_id: str) -> str:
        if self._router_client is None:
            return "unregister_skipped_no_client"
        try:
            result = self._router_client.unregister_color(instance_id)
        except RouterClientError as exc:
            self.logger.warning("unregister_color failed: %s", exc)
            return "unregister_failed"
        if result.get("unregistered"):
            return "unregister_succeeded"
        return "unregister_noop"

    def _resolve_prior_color(self, snap: dict[str, Any]) -> str | None:
        """Pick the most-recent drain entry — that's the rollback target."""
        drain_entries = snap.get("drain_entries") or []
        if not isinstance(drain_entries, list) or not drain_entries:
            return None
        last = drain_entries[-1]
        if not isinstance(last, dict):
            return None
        color = last.get("color")
        if isinstance(color, str) and is_valid_color(color):
            return color
        return None

    def _success_envelope(self, data: dict[str, Any]) -> dict[str, Any]:
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": data,
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def _error_envelope(self, code: str, message: str) -> dict[str, Any]:
        return {
            "action_status": ActionStatus.ERROR.value,
            "data": {},
            "actions": [],
            "error": {"code": code, "message": message, "details": {}},
            "timestamp": datetime.now(UTC).isoformat(),
        }


def _pid_alive(pid: int) -> bool:
    """Return True iff ``pid`` is alive (kill -0 probe)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Permission denied → process exists but we can't signal it.
        return True
    return True
