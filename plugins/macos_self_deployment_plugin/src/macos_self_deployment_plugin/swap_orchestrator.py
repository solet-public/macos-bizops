"""Swap orchestration — the spawn-green + wait-ready + activate sequence.

The orchestrator is the only piece of the plugin that knows the swap
choreography. ``MacosSelfDeploymentPlugin`` keeps its surface thin
by delegating ``restart_with_manifest`` here.

Responsibility split:

- ``MacosSelfDeploymentPlugin`` owns the platform-facing
  ``@platform_process`` action surface, the service-interface methods,
  and the ``RouterClient`` lifecycle (one client per plugin instance).
- ``SwapOrchestrator`` owns the actual swap sequence: probe the live
  router, decide the next color, mint the next instance id, spawn the
  child, poll for ready, activate, quiesce the local plugins, enqueue
  the durable ``complete_swap`` finisher.

The orchestrator NEVER touches Postgres directly — it submits the
durable handoff action through an injected ``ActionFactoryProtocol``,
mirroring the cloud sibling's pattern in
``plugins/aws_self_deployment_plugin/.../deployer.py``.

The spawn helper is also pulled out so smokes can override it with a
fake that records the call but doesn't actually launch another the solet.
"""

from __future__ import annotations

import logging
import os
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import replace
from pathlib import Path

from ananta.core.actions.owned_deploy_admission import OwnedDeployAdmission
from ananta.core.runtime import get_runtime_dir
from ananta.interfaces.lifecycle_result_types import RestartResult, RestartStatus

from macos_self_deployment_plugin import process_identity
from macos_self_deployment_plugin.child_spawn import spawn_solet_child
from macos_self_deployment_plugin.constants import (
    AUDIT_TOKEN_PREFIX,
    COLOR_BLUE,
    COLOR_GREEN,
    DEFAULT_GREEN_READY_POLL_INTERVAL_SECONDS,
    DEFAULT_GREEN_READY_TIMEOUT_SECONDS,
    ENV_SOLET_COLOR,
    ENV_SOLET_INSTANCE_ID,
    ENV_SOLET_RELEASE_ID,
    FINISHER_ESCALATION_NEEDS_INTERVENTION,
    FINISHER_ESCALATION_SIGKILL_AFTER_GRACE,
    OWNED_DEPLOY_OPERATION_RESTART,
    OWNED_DEPLOY_OPERATION_ROLLBACK,
    PLUGIN_NAME,
    STATUS_FAILED,
    STATUS_QUEUED,
    RestartReasonCode,
    is_valid_color,
    opposite_color,
)
from macos_self_deployment_plugin.release_manager import (
    CandidatePaths,
    ReleaseManagerError,
)
from macos_self_deployment_plugin.router_client import (
    RouterClient,
    RouterClientError,
)
from macos_self_deployment_plugin.swap_executor import (
    ActionFactoryProtocol,
    SetActiveTarget,
    SetColorActiveFn,
    SpawnFn,
    SwapExecutor,
    build_failed_result,
)
from macos_self_deployment_plugin.swap_orchestrator_admission import (
    OwnedDeployGate as OwnedDeployGate,
)
from macos_self_deployment_plugin.swap_orchestrator_admission import (
    _Entry as _Entry,
)
from macos_self_deployment_plugin.swap_orchestrator_admission import (
    _owned_deploy as _owned_deploy,
)
from macos_self_deployment_plugin.swap_orchestrator_preflight import (
    PRE_BUILD_PROBE_RELEASE_ID as PRE_BUILD_PROBE_RELEASE_ID,
)
from macos_self_deployment_plugin.swap_orchestrator_preflight import (
    _capture_manifest_plugins as _capture_manifest_plugins,
)
from macos_self_deployment_plugin.swap_orchestrator_preflight import (
    _contained_probe_outcome as _contained_probe_outcome,
)
from macos_self_deployment_plugin.swap_orchestrator_preflight import (
    _derive_next_color as _derive_next_color,
)
from macos_self_deployment_plugin.swap_orchestrator_preflight import (
    _gc_releases as _gc_releases,
)
from macos_self_deployment_plugin.swap_orchestrator_preflight import (
    _pre_build_root_manifest_refusal as _pre_build_root_manifest_refusal,
)
from macos_self_deployment_plugin.swap_orchestrator_preflight import (
    _probe_failed_restart_result as _probe_failed_restart_result,
)
from macos_self_deployment_plugin.swap_orchestrator_preflight import (
    _provenance_kwargs as _provenance_kwargs,
)
from macos_self_deployment_plugin.swap_orchestrator_preflight import (
    _resolve_current_snapshot as _resolve_current_snapshot,
)
from macos_self_deployment_plugin.swap_orchestrator_preflight import (
    _root_manifest_restart_result as _root_manifest_restart_result,
)
from macos_self_deployment_plugin.swap_orchestrator_preflight import (
    prepare_swap,
)
from macos_self_deployment_plugin.swap_orchestrator_types import (
    PreflightProbeFn as PreflightProbeFn,
)
from macos_self_deployment_plugin.swap_orchestrator_types import (
    ReleaseManagerProtocol as ReleaseManagerProtocol,
)
from macos_self_deployment_plugin.swap_orchestrator_types import (
    SchemaPreflightFn as SchemaPreflightFn,
)
from macos_self_deployment_plugin.swap_orchestrator_types import (
    _SwapPlan as _SwapPlan,
)


def _now_audit_token() -> str:
    return f"{AUDIT_TOKEN_PREFIX}{int(time.time())}-{uuid.uuid4().hex[:8]}"


def _dry_run_envelope(reason: str, expected_etag: str, self_color: str) -> RestartResult:
    next_color = opposite_color(self_color) if is_valid_color(self_color) else COLOR_GREEN
    message = f"dry_run=True; would spawn next_color={next_color}, wait for register, activate, quiesce, enqueue complete_swap. Reason: {reason}"
    return RestartResult(
        status=RestartStatus.QUEUED,
        restart_action_id=_now_audit_token(),
        message=message,
        reason=reason,
        expected_etag=expected_etag,
        dry_run=True,
    )


def _rollback_cas_check(
    *,
    release_manager: ReleaseManagerProtocol,
    expected_current_release: str,
    expected_etag: str,
    reason: str,
    logger: logging.Logger,
) -> RestartResult | None:
    """Concurrency CAS for ``rollback_release`` (Architect ruling (c)).

    ``rollback_release`` is an operational verb invoked DIRECTLY (no
    ``apply_manifest`` wrapper performs the CAS upstream as it does for
    ``restart_with_manifest``), so the verb self-CASes against the ALREADY-
    injected release state: the caller passes the ``rel-<id>`` it observed as
    ``current``, and this confirms it still matches
    ``ReleaseManager.current_release`` before any spawn. ``current`` and
    ``previous`` move atomically, so CASing ``current`` ALONE is sufficient
    (never CAS ``previous``). A mismatch — a concurrent deploy/rollback moved
    ``current`` since the caller observed it — returns
    ``FAILED(stale_current_release)`` with the ACTUAL ``current`` echoed for
    re-read/retry; the system is left unchanged. Stateless (release state +
    logger only), so it lives at module level alongside ``default_spawn``.
    """
    actual_current = release_manager.current_release
    if expected_current_release == actual_current:
        return None
    return build_failed_result(
        status=RestartStatus.FAILED,
        reason_code=RestartReasonCode.STALE_CURRENT_RELEASE,
        message=(f"stale current release: caller asserted current={expected_current_release!r} but the live current is {actual_current!r}; re-read current_release and retry."),
        reason=reason,
        expected_etag=expected_etag,
        logger=logger,
    )


def default_spawn(
    app_home: Path,
    next_color: str,
    next_instance_id: str,
    solet_name: str,
    candidate: CandidatePaths,
) -> int:
    """Spawn the next-color solet FROM the materialized candidate release.

    §4.5 spawn invariant (design 2026-06-27): the green child is launched
    from the **candidate ``rel-<id>`` directly** — its own
    ``venv/bin/python3``, whose re-pointed ``.pth`` resolve imports to the
    candidate's ``code/`` — **not** from ``current`` (which still points
    at the live old release at spawn time, and is only flipped to the
    candidate by ``cutover`` after this child passes register + activate).
    So the candidate interpreter replaces the old repo-root
    ``_resolve_interpreter`` resolution.

    The entrypoint stays ``python -m ananta.cli`` (NOT ``launch.py``,
    which would terminate the running blue before green is up). The child
    runs in its own POSIX session (``start_new_session=True``) so a future
    SIGTERM to blue (the green-side ``complete_swap`` finisher) cannot
    cascade to green.

    Args:
        app_home: ``<APP_HOME>`` for the new instance (shared profile;
            ``profile/data/`` is shared across releases per §4.3).
        next_color: ``"blue"`` or ``"green"`` — the router routing/identity
            axis (independent of the release axis).
        next_instance_id: Pre-minted id the new the solet uses when it calls
            ``router.register_color``.
        solet_name: The shared solet name, propagated so the
            child can resolve the runtime dir.
        candidate: The freshly-built release this child runs — its
            ``venv_python`` is the interpreter and ``release_id`` is
            recorded in ``SOLET_RELEASE_ID`` (audit-only, §4.8).

    Returns:
        The OS pid of the spawned child.
    """
    # Capture green's stdout+stderr to a per-spawn log. Previously
    # DEVNULL'd, so a green that failed to register within the ready
    # timeout left no diagnostic — caller couldn't tell whether green
    # crashed at import, hung in kb_lifecycle, blocked on LM Studio,
    # etc. Per-spawn file (vs shared) keeps each boot trace isolated.
    log_path = app_home / "data" / "logs" / f"green_spawn_{next_color}_{next_instance_id}.log"
    # Shared spawn mechanics (cmd shape, out-of-tree §5 CWD,
    # start_new_session, stdio) live in ``child_spawn`` so this swap path
    # and the supervisor's cold-start/crash path cannot drift. The candidate
    # interpreter + explicit colour/instance/release env is the swap-specific
    # part: register + activate target this exact child.
    proc = spawn_solet_child(
        interpreter=str(candidate.venv_python),
        app_home=app_home,
        solet_name=solet_name,
        log_path=log_path,
        extra_env={
            ENV_SOLET_COLOR: next_color,
            ENV_SOLET_INSTANCE_ID: next_instance_id,
            # Audit-only (§4.8): which immutable release this child runs, so
            # the colour axis and the release axis stay separate, auditable
            # fields.
            ENV_SOLET_RELEASE_ID: candidate.release_id,
        },
    )
    return proc.pid


def _finisher_escalation_for(reconciliation_provenance: dict[str, str] | None) -> str:
    """The finisher's SIGTERM-overrun policy for this swap (iss_8d1ec833).

    A reconciliation swap (``cutover_release`` / target-local entry — the
    gated channel) must never reach signal 9, so its durable finisher record
    says ``needs_intervention``; an ordinary deploy keeps today's ladder.
    Provenance is the discriminator the executor already receives, so the
    policy cannot drift from the channel it protects.
    """
    if reconciliation_provenance is None:
        return FINISHER_ESCALATION_SIGKILL_AFTER_GRACE
    return FINISHER_ESCALATION_NEEDS_INTERVENTION


def _prior_identity(
    prior_pid: int | None,
    prior_start_token: str | None,
) -> tuple[int, str | None]:
    """Supply service-local identity only for direct compatibility callers."""
    resolved_pid = os.getpid() if prior_pid is None else prior_pid
    return (
        resolved_pid,
        process_identity.start_token(resolved_pid) if prior_start_token is None else prior_start_token,
    )


class SwapOrchestrator:
    """Stateless coordinator for one ``restart_with_manifest`` turn.

    ``stateless`` means: no in-flight bookkeeping is cached between calls;
    per-call state lives in local variables. Concurrent swaps are excluded
    by owned-deploy admission (:class:`OwnedDeployGate`): ``restart`` and
    ``rollback_release`` admit through the per-solet lock shared with the
    core ``apply_manifest`` commit and ``stop_self`` before any router
    probe, build or spawn. ``apply_manifest`` delegating here on its own
    thread joins its outer admission instead of re-locking.
    """

    def __init__(
        self,
        *,
        router_client: RouterClient,
        action_factory: ActionFactoryProtocol,
        session_factory: Callable[[], str],
        solet_name: str,
        release_manager: ReleaseManagerProtocol,
        schema_preflight: SchemaPreflightFn,
        preflight_probe: PreflightProbeFn,
        set_color_active: SetColorActiveFn,
        spawn_fn: SpawnFn = default_spawn,
        logger: logging.Logger | None = None,
        ready_timeout_seconds: int = DEFAULT_GREEN_READY_TIMEOUT_SECONDS,
        ready_poll_interval_seconds: float = DEFAULT_GREEN_READY_POLL_INTERVAL_SECONDS,
        runtime_dir: Path | None = None,
    ) -> None:
        self._router = router_client
        self._release_manager = release_manager
        self._schema_preflight = schema_preflight
        # GTE-06: REQUIRED, deliberately without a default — a defaulted
        # no-op probe would be a silent fail-open bypass of the L2 gate
        # (operator-confirmed fail-closed posture, design §11 Q2).
        self._preflight_probe = preflight_probe
        self._solet_name = solet_name
        self._logger = logger or logging.getLogger(PLUGIN_NAME)
        # B2: pending-finisher record dir (smokes inject a scratch dir so live
        # state is never touched); defaults to the live runtime dir.
        resolved_runtime_dir = runtime_dir or get_runtime_dir(solet_name)
        admission = OwnedDeployAdmission(runtime_dir=resolved_runtime_dir, solet_name=solet_name, identity_probe=process_identity.start_token)
        self.owned_deploy_gate = OwnedDeployGate(admission, self._logger)
        # The spawn → wait → activate → symlink-swap → quiesce → enqueue spine
        # (incl. the GreenCandidate router lifecycle) lives in the executor,
        # shared verbatim by the forward cutover and the durable rollback. The
        # orchestrator owns only the pre-spawn phase (status probe, color
        # derivation, build-or-rehydrate, schema preflight) and hands the
        # executor a candidate + the symlink op.
        self._executor = SwapExecutor(
            router_client=router_client,
            action_factory=action_factory,
            session_factory=session_factory,
            solet_name=solet_name,
            runtime_dir=resolved_runtime_dir,
            set_color_active=set_color_active,
            spawn_fn=spawn_fn,
            logger=self._logger,
            ready_timeout_seconds=ready_timeout_seconds,
            ready_poll_interval_seconds=ready_poll_interval_seconds,
        )

    # ------------------------------------------------------------------
    # Entry point: restart_with_manifest
    # ------------------------------------------------------------------

    @_owned_deploy(OWNED_DEPLOY_OPERATION_RESTART)
    def restart(
        self,
        *,
        reason: str,
        expected_etag: str,
        dry_run: bool,
        app_home: Path,
        self_instance_id: str,
        self_color: str,
        set_active_targets: Iterable[SetActiveTarget],
        reconciliation_provenance: dict[str, str] | None = None,
        prior_pid: int | None = None,
        prior_start_token: str | None = None,
        poller_gate: str = "local_service_quiesced",
    ) -> RestartResult:
        """Drive one swap end-to-end and return the typed RestartResult.

        Sequence (L3 plan §3.3 + §4.2, extended by the 2026-06-27
        materialized-release design §4.5/§4.7/§3):

        1. Read router.status() to confirm self is active + derive
           next color.
        2. Build the immutable candidate release (CoW clone + .pth
           re-point); §3 schema preflight on the candidate. (Both before
           any spawn — a build failure or non-additive schema diff leaves
           ``current``/``previous`` + the live router untouched.)
        3. Mint next-color instance id.
        4. Spawn next color FROM the candidate (§4.5: its own
           ``venv``/``code``, NOT ``current``) via ``self._spawn_fn``.
        5. Poll router.status() until next color reports registered.
        6. router.activate(next_color, next_instance_id).
        7. ``ReleaseManager.cutover(candidate)`` — flip the durable
           ``previous``/``current`` symlinks (§4.7, immediately after a
           successful activate, before quiesce). On failure: roll the router
           back to the prior (still-draining) color; the candidate is
           SIGTERM'd (bounded, never SIGKILL) + unregistered ONLY if that
           rollback is CONFIRMED — if it does NOT take (RPC error/refusal)
           the router may still route to the candidate, so it is LEFT ALIVE
           with a distinct 'manual intervention required' status (never a
           dead-color route). Either way do NOT enqueue complete_swap.
        8. Iterate set_active_targets → set_active(False) on each.
        9. Enqueue durable complete_swap action for green's poller.
        10. Return RestartResult(status=QUEUED, restart_action_id=<id>).

        Failure of any pre-activate step (1–5) leaves the prior color
        untouched and routing-side unchanged; we SIGTERM (bounded, never
        SIGKILL — iss_8d1ec833) + unregister a started-but-not-registered
        child: ``FAILED``, or ``NEEDS_INTERVENTION`` past the grace window.

        ``dry_run`` short-circuits at step 0 and returns a
        ``status=QUEUED`` envelope with a synthetic restart_action_id
        and an explanatory message — used by smoke tests + operator
        previews.
        """
        if dry_run:
            return _dry_run_envelope(reason, expected_etag, self_color)

        # F1 cutover preflight — refuses to spawn green on root-manifest drift
        # (design memo §6.1) — no longer runs HERE. §46.1: validating in this
        # process validated against whatever it imported at its own last
        # start, so it refused valid manifests right after an update. It now
        # runs inside the L2 fresh-source probe under the CANDIDATE's
        # interpreter (see _prepare_swap). The candidate does not exist yet at
        # this point in the sequence, which is why the gate had to move rather
        # than be re-pointed. Its refusal classification is unchanged —
        # see _root_manifest_restart_result.

        # Confirm self is the active color and materialize + schema-preflight
        # the candidate — all BEFORE any spawn (§4.5/§4.7/§3). A failure here
        # leaves the live router + ``current``/``previous`` untouched.
        plan = self._prepare_swap(
            reason=reason,
            expected_etag=expected_etag,
            app_home=app_home,
            self_color=self_color,
            self_instance_id=self_instance_id,
            reconciliation_provenance=reconciliation_provenance,
        )
        if isinstance(plan, RestartResult):
            return plan
        next_color = plan.next_color
        candidate = plan.candidate

        # Steps 3–10 — the spawn → wait → activate → durable cutover (§4.7,
        # F1/F2-gated compensation) → grace → quiesce → C2 gate-off → enqueue
        # complete_swap spine — are delegated to the shared executor. The
        # symlink op is ``cutover`` (the durable rollback verb passes
        # ``rollback`` instead); a register-timeout here is a plain FAILED
        # (system unchanged, retryable).
        resolved_prior_pid, resolved_prior_start_token = _prior_identity(
            prior_pid,
            prior_start_token,
        )
        result = self._executor.execute(
            app_home=app_home,
            candidate=candidate,
            next_color=next_color,
            reason=reason,
            expected_etag=expected_etag,
            self_instance_id=self_instance_id,
            self_color=self_color,
            prior_pid=resolved_prior_pid,
            prior_start_token=resolved_prior_start_token,
            poller_gate=poller_gate,
            set_active_targets=set_active_targets,
            symlink_swap=self._release_manager.cutover,
            spawn_failure=(RestartStatus.FAILED, RestartReasonCode.SPAWN_FAILED),
            register_failure=(RestartStatus.FAILED, RestartReasonCode.REGISTER_TIMEOUT),
            compensation_codes=(
                RestartReasonCode.CUTOVER_COMPENSATED,
                RestartReasonCode.CUTOVER_ROUTER_ROLLBACK_FAILED,
            ),
            finisher_escalation=_finisher_escalation_for(reconciliation_provenance),
        )
        if result.status is RestartStatus.QUEUED and plan.probe_evidence is not None:
            # Q5: the GREEN probe's evidence rides the QUEUED result so the
            # applied envelope carries positive proof the L2 probe executed.
            merged_probe = dict(plan.probe_evidence)
            if result.probe is not None:
                merged_probe.update(result.probe)
            return replace(result, probe=merged_probe)
        return result

    # ------------------------------------------------------------------
    # Entry point: rollback_release (durable code rollback — the escape hatch)
    # ------------------------------------------------------------------

    @_owned_deploy(OWNED_DEPLOY_OPERATION_ROLLBACK)
    def rollback_release(
        self,
        *,
        reason: str,
        expected_etag: str,
        expected_current_release: str,
        app_home: Path,
        self_instance_id: str,
        self_color: str,
        set_active_targets: Iterable[SetActiveTarget],
        prior_pid: int | None = None,
        prior_start_token: str | None = None,
    ) -> RestartResult:
        """Durably roll back to the prior release (design §4.5, PATH A).

        Rollback IS a swap whose target is the existing ``previous`` release,
        so it reuses the shared executor choreography VERBATIM, with two
        deltas only:

        - the candidate is the REHYDRATED ``previous`` release
          (``candidate_for``), not a freshly-built one — no
          ``build_candidate``, no §3 schema preflight (a release that was
          already ``current`` is schema-safe to return to by construction);
        - the symlink op is ``ReleaseManager.rollback`` (swap
          ``current``↔``previous``), not ``cutover``.

        Deliberately does NOT run the root-manifest preflight: rollback is the
        escape hatch FROM a bad current tree, so gating it on current-tree
        drift would defeat its purpose.

        Status partition (Architect §4.5):

        - ``FAILED`` (system unchanged + coherent + retryable): a stale
          ``expected_current_release`` (the concurrency CAS — current moved
          under the caller); no ``previous`` rollback target; self not the
          router-active instance; activate refused (current still
          authoritative); a ``rollback()`` that failed and whose compensation
          cleanly restored the pre-swap pair.
        - ``NEEDS_INTERVENTION`` (automated recovery exhausted, human must
          act): the rollback target cannot be brought up — its release dir is
          missing/corrupt, OR it never registers/health-checks within the
          timeout (the safety net itself is void); OR a compensation that could
          not complete (the durable pair MAY be incoherent).

        C2 is sidestepped: the reactivated release is a FRESH SPAWN (its
        poller is born ``is_active_color=True``), so no cross-process flip is
        needed — the broken current quiesces its OWN poller before
        ``complete_swap`` is enqueued (the landed C2 fix), which is necessary
        and sufficient. A second ``rollback_release`` rolls forward again
        (``rollback`` toggles ``current``↔``previous``) — undo/redo.

        Cold-boot fallback (DOCUMENTED, out of band — NOT this verb): if the solet
        is DEAD and so cannot serve this verb, the operator rolls back
        directly against the on-disk releases — call
        ``ReleaseManager.rollback()`` (flips the durable ``current``/``previous``
        symlinks) and re-launch via ``ananta.cli``, which cold-boots whatever
        ``current`` now points at. This verb is the zero-downtime *live* path;
        the cold-boot path needs no live router and is covered by the
        materialized-release foundation itself.
        """
        # CAS FIRST (Architect ruling (c)): confirm the caller is acting on the
        # ``current`` release they observed before anything else — ``current``
        # and ``previous`` move atomically, so CASing ``current`` alone is
        # sufficient. A stale assertion (a concurrent deploy/rollback moved
        # ``current``) → FAILED, system unchanged, no spawn.
        cas_failure = _rollback_cas_check(
            release_manager=self._release_manager,
            expected_current_release=expected_current_release,
            expected_etag=expected_etag,
            reason=reason,
            logger=self._logger,
        )
        if cas_failure is not None:
            return cas_failure
        previous = self._release_manager.previous_release
        if previous is None:
            return self._failure(
                reason=reason,
                expected_etag=expected_etag,
                message="no previous release to roll back to",
                reason_code=RestartReasonCode.NO_ROLLBACK_TARGET,
            )
        # Same identity gate as a forward swap (only the live router-active
        # instance may initiate) — and it yields the OPPOSITE color to bring
        # the previous release up on.
        try:
            status_snap = self._router.status()
        except RouterClientError as exc:
            return self._failure(
                reason=reason,
                expected_etag=expected_etag,
                message=f"router status() failed: {exc}",
                reason_code=RestartReasonCode.ROUTER_UNREACHABLE,
            )
        next_color = _derive_next_color(
            status_snap,
            self_color,
            self_instance_id,
            self._logger,
        )
        if next_color is None:
            return self._failure(
                reason=reason,
                expected_etag=expected_etag,
                message=("router status() did not show this instance as the active color; refusing to roll back from a non-active instance."),
                reason_code=RestartReasonCode.NOT_ACTIVE_INSTANCE,
            )
        # Rehydrate the previous release's spawn paths. A raise means the target
        # release dir is missing/corrupt — the safety net is VOID, and retrying
        # won't restore a gone artifact → NEEDS_INTERVENTION (not retryable
        # FAILED). Mirrors the register-timeout target-unbootable case below.
        try:
            candidate = self._release_manager.candidate_for(previous)
        except ReleaseManagerError as exc:
            return build_failed_result(
                status=RestartStatus.NEEDS_INTERVENTION,
                reason_code=RestartReasonCode.ROLLBACK_TARGET_UNBOOTABLE,
                message=f"rollback target {previous} is not materializable: {exc}",
                reason=reason,
                expected_etag=expected_etag,
                logger=self._logger,
            )
        self._logger.info(
            "rolling back: bringing up previous release=%s as color=%s",
            previous,
            next_color,
        )
        resolved_prior_pid, resolved_prior_start_token = _prior_identity(
            prior_pid,
            prior_start_token,
        )
        return self._executor.execute(
            app_home=app_home,
            candidate=candidate,
            next_color=next_color,
            reason=reason,
            expected_etag=expected_etag,
            self_instance_id=self_instance_id,
            self_color=self_color,
            prior_pid=resolved_prior_pid,
            prior_start_token=resolved_prior_start_token,
            poller_gate="local_service_quiesced",
            set_active_targets=set_active_targets,
            # rollback ignores the candidate (its target is the ledger's
            # previous); the executor still spawns FROM candidate's venv/code.
            symlink_swap=lambda _candidate: self._release_manager.rollback(),
            # A corrupt-executable previous (candidate_for passed dir+VERSION but
            # Popen raises) OR a previous that never health-checks both mean the
            # safety net itself is non-functional → NEEDS_INTERVENTION, not a
            # retryable spawn/register hiccup.
            spawn_failure=(
                RestartStatus.NEEDS_INTERVENTION,
                RestartReasonCode.ROLLBACK_TARGET_UNBOOTABLE,
            ),
            register_failure=(
                RestartStatus.NEEDS_INTERVENTION,
                RestartReasonCode.ROLLBACK_TARGET_UNBOOTABLE,
            ),
            compensation_codes=(
                RestartReasonCode.ROLLBACK_COMPENSATED,
                RestartReasonCode.COMPENSATION_INCOMPLETE,
            ),
        )

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _prepare_swap(
        self,
        *,
        reason: str,
        expected_etag: str,
        app_home: Path,
        self_color: str,
        self_instance_id: str,
        reconciliation_provenance: dict[str, str] | None = None,
    ) -> _SwapPlan | RestartResult:
        """Pre-spawn phase: confirm self active + materialize + preflight candidate.

        Returns a :class:`_SwapPlan` (next color + the built candidate
        release) on success, or a FAILED :class:`RestartResult` on any
        pre-spawn refusal — router unreachable, self not the router-active
        instance (C3: color AND instance must match), root-manifest drift
        (found before the build), a release build failure, or a non-additive
        schema diff (§3). On every failure path
        nothing is spawned and the live router + ``current``/``previous``
        stay untouched.

        ``reconciliation_provenance`` reaches the candidate build and nothing
        else — see :func:`_provenance_kwargs`.
        """

        return prepare_swap(
            router=self._router,
            release_manager=self._release_manager,
            schema_preflight=self._schema_preflight,
            preflight_probe=self._preflight_probe,
            solet_name=self._solet_name,
            logger=self._logger,
            failure=self._failure,
            reason=reason,
            expected_etag=expected_etag,
            app_home=app_home,
            self_color=self_color,
            self_instance_id=self_instance_id,
            reconciliation_provenance=reconciliation_provenance,
        )

    def _failure(
        self,
        *,
        reason: str,
        expected_etag: str,
        message: str,
        reason_code: str = RestartReasonCode.NONE,
    ) -> RestartResult:
        """Build a pre-spawn FAILED result (router/build/preflight refusals).

        Always FAILED — every pre-spawn refusal leaves the system unchanged
        and is retryable. Delegates to ``build_failed_result`` so the failure
        envelope (status, reason_code, message, error log) is constructed in
        exactly one place, shared with the executor's in-flight failures.
        """
        return build_failed_result(
            status=RestartStatus.FAILED,
            reason_code=reason_code,
            message=message,
            reason=reason,
            expected_etag=expected_etag,
            logger=self._logger,
        )


__all__ = [
    "ActionFactoryProtocol",
    "COLOR_BLUE",
    "COLOR_GREEN",
    "STATUS_FAILED",
    "STATUS_QUEUED",
    "PreflightProbeFn",
    "ReleaseManagerProtocol",
    "SchemaPreflightFn",
    "SetActiveTarget",
    "SetColorActiveFn",
    "SpawnFn",
    "SwapOrchestrator",
    "default_spawn",
]
