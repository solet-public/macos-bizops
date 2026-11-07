"""Pre-spawn preparation with the orchestrator's explicit injected dependencies.

Router identity, root-manifest checks, release build/GC, schema checks and the
candidate probe retain their existing order and failure classifications.
This module performs no deployment activation or durable finisher handoff.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Final

from ananta.core.plugins.profile_manifest import load_manifest_plugin_set
from ananta.interfaces.lifecycle_result_types import RestartResult, RestartStatus

from macos_self_deployment_plugin.constants import RestartReasonCode, is_valid_color, opposite_color, resolve_project_root
from macos_self_deployment_plugin.preflight_probe_runner import CHECK_ROOT_MANIFEST, PROBE_ERROR_HARNESS, ProbeOutcome
from macos_self_deployment_plugin.release_manager import VENV_BIN_DIRNAME, VENV_PYTHON_BASENAME, VENV_SUBTREE, VERSION_FILENAME, CandidatePaths, ReleaseManagerError
from macos_self_deployment_plugin.router_client import RouterClient, RouterClientError
from macos_self_deployment_plugin.schema_snapshot_producer import SchemaSnapshotFn, build_schema_snapshot_fn
from macos_self_deployment_plugin.swap_executor import build_failed_result
from macos_self_deployment_plugin.swap_orchestrator_types import PreflightFailureFn, PreflightProbeFn, ReleaseManagerProtocol, SchemaPreflightFn, _SwapPlan

PRE_BUILD_PROBE_RELEASE_ID: Final[str] = "pre-build-source-tree"


def _contained_probe_outcome(
    probe: PreflightProbeFn,
    *,
    candidate: CandidatePaths,
    app_home: Path,
    logger: logging.Logger,
) -> ProbeOutcome:
    """A5a: nothing escapes — a raising probe seam is a RED probe.

    An exception escaping ``restart_with_manifest`` is classified
    core-side as ``plugin_raised``, which routes to the
    leave-the-committed-bytes envelope — bypassing the manifest rollback
    exactly when the probe is least trustworthy. Containing the seam
    here guarantees every probe-path failure lands on the
    ``PROBE_FAILED`` → rollback path.
    """
    try:
        return probe(candidate=candidate, app_home=app_home)
    except Exception as exc:  # noqa: BLE001 — A5a containment is the contract
        logger.exception("preflight probe seam raised — contained to PROBE_FAILED")
        return ProbeOutcome(
            ok=False,
            payload={
                "failing_step": "harness",
                "error_class": PROBE_ERROR_HARNESS,
                "detail": f"probe seam raised {type(exc).__name__}: {exc}",
                "failures": [],
                "release_id": candidate.release_id,
            },
        )


def _root_manifest_restart_result(
    outcome: ProbeOutcome,
    *,
    reason: str,
    expected_etag: str,
    logger: logging.Logger,
) -> RestartResult | None:
    """F1 drift found by the probe → the F1 classification, not the probe's.

    ``None`` when this rejection is not root-manifest drift.

    §46.1 moved WHERE this gate executes (the outgoing process's stale
    imports → the candidate's own interpreter) and deliberately changed
    NOTHING about what a refusal means. That distinction is load-bearing
    and is why this function exists:

    * ``status`` is what core actually routes on —
      ``PROBE_FAILED`` triggers the manifest-bytes rollback, ``FAILED``
      does not. Root-manifest drift is a property of the DEPLOYMENT ROOT,
      not of the manifest bytes being applied; rolling those bytes back
      because the repo root carries a stray entry would punish the wrong
      artifact. It stays ``FAILED`` — system unchanged, coherent,
      retryable — exactly as it was before the move.
    * ``reason_code`` is reported rather than routed on, and is preserved
      for observability: an operator keeps seeing ``root_manifest_drift``
      instead of a generic ``probe_rejected``.

    Letting either flatten into the probe's own classification would have
    changed live behaviour as a side effect of a bug fix.
    """
    failures = outcome.payload.get("failures")
    if not isinstance(failures, list):
        return None
    drift = next(
        (failure for failure in failures if isinstance(failure, dict) and failure.get("check") == CHECK_ROOT_MANIFEST),
        None,
    )
    if drift is None:
        return None
    envelope = str(drift.get("message", ""))
    logger.warning("cutover preflight refused green spawn:\n%s", envelope)
    return build_failed_result(
        status=RestartStatus.FAILED,
        reason_code=RestartReasonCode.ROOT_MANIFEST_DRIFT,
        message="cutover preflight blocked on root_manifest drift:\n" + envelope,
        reason=reason,
        expected_etag=expected_etag,
        logger=logger,
    )


def _pre_build_root_manifest_refusal(
    probe: PreflightProbeFn,
    *,
    app_home: Path,
    reason: str,
    expected_etag: str,
    logger: logging.Logger,
) -> RestartResult | None:
    """Refuse root-manifest drift BEFORE the release build, or return ``None``.

    The drift is a property of the deployment root, so it is known without a
    built release; finding it only after the build cost a full build for a
    refusal that needed none of it (iss_aca2ea39).  §46.1 forbids running the
    check in this process (its imports are as old as its last start), so the
    SAME probe seam runs it under the source checkout's own interpreter: the
    candidate is a clone of that checkout's code and venv, which makes the
    code doing the validating the code about to be activated.

    Only a root-manifest failure refuses here, with the same classification
    and message the post-build probe gives.  Anything else the early probe
    reports (a plugin-manifest failure, a harness error, a missing source
    venv) is left to the post-build probe, which stays the authoritative gate
    on the candidate's own interpreter.
    """
    source_root = resolve_project_root(app_home)
    source_target = CandidatePaths(
        release_id=PRE_BUILD_PROBE_RELEASE_ID,
        release_dir=source_root,
        code_root=source_root,
        venv_python=source_root / VENV_SUBTREE / VENV_BIN_DIRNAME / VENV_PYTHON_BASENAME,
        version_file=source_root / VERSION_FILENAME,
        missing_pth_targets=(),
        schema_snapshot=None,
    )
    outcome = _contained_probe_outcome(
        probe,
        candidate=source_target,
        app_home=app_home,
        logger=logger,
    )
    if outcome.ok:
        return None
    refusal = _root_manifest_restart_result(
        outcome,
        reason=reason,
        expected_etag=expected_etag,
        logger=logger,
    )
    if refusal is None:
        logger.info(
            "pre-build probe of the source tree did not report root-manifest drift (%s); the post-build probe of the candidate decides",
            outcome.payload.get("error_class"),
        )
    return refusal


def _probe_failed_restart_result(
    outcome: ProbeOutcome,
    *,
    reason: str,
    expected_etag: str,
    logger: logging.Logger,
) -> RestartResult:
    """Typed PROBE_FAILED result carrying the rejection payload.

    EXCEPT for root-manifest drift, which keeps its own classification —
    see :func:`_root_manifest_restart_result`.
    """
    root_manifest_failure = _root_manifest_restart_result(
        outcome,
        reason=reason,
        expected_etag=expected_etag,
        logger=logger,
    )
    if root_manifest_failure is not None:
        return root_manifest_failure
    error_class = outcome.payload.get("error_class")
    detail = outcome.payload.get("detail")
    logger.error(
        "L2 preflight probe REJECTED the deploy [%s]: %s",
        error_class,
        detail,
    )
    return RestartResult(
        status=RestartStatus.PROBE_FAILED,
        restart_action_id="",
        message=(f"L2 fresh-source preflight probe rejected the deploy: {error_class}: {detail}"),
        reason=reason,
        expected_etag=expected_etag,
        dry_run=False,
        reason_code=RestartReasonCode.PROBE_REJECTED,
        probe=outcome.payload,
    )


def _capture_manifest_plugins(app_home: Path) -> tuple[str, ...] | None:
    """Snapshot the LIVE profile plugin set for the release being built (BLG-02).

    Called at build time because that is the one moment the live manifest still
    describes the tree being built: ``apply_manifest`` commits the new manifest
    before delegating a restart, and the NEXT deploy overwrites it again. The §3
    derive path therefore cannot read it back later and must consult the
    release's own persisted copy instead.

    ``None`` is preserved rather than flattened to an empty tuple: ``None``
    means "ungated — load everything installed", while ``()`` would claim the
    release had NO plugins and gate a later derive to an empty discovery, which
    would silently produce an empty snapshot. Module-level alongside
    ``_resolve_current_snapshot`` so ``SwapOrchestrator`` stays under the
    god-class LOC bound.
    """
    live = load_manifest_plugin_set(app_home)
    return None if live is None else tuple(sorted(live))


def _resolve_current_snapshot(
    release_manager: ReleaseManagerProtocol,
    snapshot_fn: SchemaSnapshotFn,
) -> dict[str, object] | None:
    """Resolve the OLD side of the §3 diff — read it, or DERIVE it (B1·1).

    The current release's persisted snapshot is the OLD side. When the current
    release predates the producer (its ``VERSION`` has none) but a current
    release EXISTS, DERIVE the snapshot by running the SAME collector
    (``snapshot_fn``) against ``current/code`` — so a pre-producer current (e.g.
    the live ``af157a1fe`` transition) is still gated.

    The derive passes the OLD release's OWN captured plugin set as the
    collector's manifest (BLG-02). By the time this runs, ``apply_manifest``
    has already written the INCOMING deploy's manifest over the live one, so
    letting the collector read ``APP_HOME`` would gate the old tree by the new
    profile — and a deploy that GROWS the manifest would then false-REFUSE on a
    plugin the old tree never had. ``None`` (a release built before capture
    landed) preserves today's live-manifest behaviour rather than inventing a
    more permissive one; it is a compat rung that stops firing once every
    release in the chain postdates this.

    FAIL-CLOSED on both legs: ``current_schema_snapshot`` raises on a torn
    ``VERSION`` and the derive raises on any collect failure, so the only
    outcomes are a snapshot or a ``ReleaseManagerError`` — NEVER a silent None
    that would let the gate read a non-additive change as a bootstrap. A ``None``
    return therefore means, unambiguously, "no current release". Module-level
    (stateless beyond release state + the fn) alongside ``_rollback_cas_check``,
    so the orchestrator class stays under the god-class LOC bound.
    """
    current_release = release_manager.current_release
    current_snapshot = release_manager.current_schema_snapshot()
    if current_snapshot is None and current_release is not None:
        old_candidate = release_manager.candidate_for(current_release)
        # THE point of use, and therefore the only place a rejected capture may
        # refuse. Rehydrate deliberately reports rather than raises, so that a
        # garbage schema-gate field cannot make a bootable release
        # un-rollback-able; the cost of that choice is paid here, loudly.
        if old_candidate.manifest_plugins_error is not None:
            msg = f"§3 derive refused: {old_candidate.manifest_plugins_error} — refusing to snapshot an old tree against an untrustworthy manifest"
            raise ReleaseManagerError(msg)
        current_snapshot = snapshot_fn(
            old_candidate.code_root,
            old_candidate.manifest_plugins,
        )
    return current_snapshot


def _derive_next_color(
    status_snap: dict[str, Any],
    self_color: str,
    self_instance_id: str,
    logger: logging.Logger,
) -> str | None:
    """Confirm ``self`` is THE router-active instance; return opposite color or None.

    C3: require both ``active_color == self_color`` AND ``active_instance_id ==
    self_instance_id``. Color alone is not identity — a stale same-color process
    (not the router-active instance) must not drive a swap and enqueue a
    ``complete_swap`` for its own stale id. Module-level (pure but for the
    injected logger) so :class:`SwapOrchestrator` stays under the god-class bound.
    """
    active_color = status_snap.get("active_color")
    active_instance_id = status_snap.get("active_instance_id")
    if not isinstance(active_color, str) or not isinstance(active_instance_id, str):
        return None
    if active_color != self_color:
        logger.warning(
            "self_color=%s but router.active_color=%s; refusing swap",
            self_color,
            active_color,
        )
        return None
    if active_instance_id != self_instance_id:
        logger.warning(
            "self_instance_id=%s but router.active_instance_id=%s (stale non-active same-color process); refusing swap",
            self_instance_id,
            active_instance_id,
        )
        return None
    if not is_valid_color(self_color):
        return None
    return opposite_color(self_color)


def _provenance_kwargs(
    reconciliation_provenance: dict[str, str] | None,
) -> dict[str, dict[str, str]]:
    """Pass the provenance kwarg ONLY on the reconciliation path.

    An ordinary deploy must call ``build_candidate`` with exactly the argument
    list it always did, so widening that seam cannot silently change deploy
    behaviour — and a release-manager stand-in that does not understand
    provenance then fails loudly the moment a reconciliation actually uses it,
    instead of quietly dropping the one field that records which approved act
    produced these bytes.

    Provenance reaches the build and nothing else: it never influences colour
    selection, preflight, or any refusal, so a reconciliation swap and a deploy
    swap remain the same operation with the same safety properties.
    """
    if reconciliation_provenance is None:
        return {}
    return {"reconciliation_provenance": reconciliation_provenance}


def _gc_releases(manager: ReleaseManagerProtocol, logger: logging.Logger) -> None:
    """Best-effort release cleanup that never aborts a healthy swap."""
    try:
        result = manager.gc()
    except (ReleaseManagerError, OSError) as exc:
        logger.warning("release gc failed (non-fatal): %s", exc)
        return
    if result.deleted:
        logger.info("release gc reaped %d: %s", len(result.deleted), result.deleted)


def prepare_swap(
    *,
    router: RouterClient,
    release_manager: ReleaseManagerProtocol,
    schema_preflight: SchemaPreflightFn,
    preflight_probe: PreflightProbeFn,
    solet_name: str,
    logger: logging.Logger,
    failure: PreflightFailureFn,
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
    try:
        status_snap = router.status()
    except RouterClientError as exc:
        return failure(
            reason=reason,
            expected_etag=expected_etag,
            message=f"router status() failed: {exc}",
            reason_code=RestartReasonCode.ROUTER_UNREACHABLE,
        )
    next_color = _derive_next_color(
        status_snap,
        self_color,
        self_instance_id,
        logger,
    )
    if next_color is None:
        return failure(
            reason=reason,
            expected_etag=expected_etag,
            message=("router status() did not show this instance as the active color; refusing to spawn a parallel green from a non-active blue."),
            reason_code=RestartReasonCode.NOT_ACTIVE_INSTANCE,
        )
    # Root-manifest drift needs no built release (iss_aca2ea39).
    early_refusal = _pre_build_root_manifest_refusal(
        preflight_probe,
        app_home=app_home,
        reason=reason,
        expected_etag=expected_etag,
        logger=logger,
    )
    if early_refusal is not None:
        return early_refusal
    # §4.5/§4.7: materialize the immutable candidate BEFORE the spawn so the
    # green child is launched from the candidate's own venv/code — NOT
    # ``current``, which still names the live old release until ``cutover``
    # flips it post-activate.
    snapshot_fn = build_schema_snapshot_fn(
        solet_name=solet_name,
        app_home=app_home,
        source_root=resolve_project_root(app_home),
    )
    try:
        candidate = release_manager.build_candidate(
            manifest_etag=expected_etag,
            manifest_plugins=_capture_manifest_plugins(app_home),
            schema_snapshot_fn=snapshot_fn,
            **_provenance_kwargs(reconciliation_provenance),
        )
    except ReleaseManagerError as exc:
        return failure(
            reason=reason,
            expected_etag=expected_etag,
            message=f"build candidate release failed: {exc}",
            reason_code=RestartReasonCode.BUILD_FAILED,
        )
    logger.info(
        "built candidate release=%s missing_pth=%d",
        candidate.release_id,
        len(candidate.missing_pth_targets),
    )
    # §4.7 lifecycle GC: having materialized a new release, reap the stale
    # tail (keep last K; §4.6 GC-safety never reaps current/previous/
    # in_progress). Running it on EVERY build — before the preflight that
    # may refuse this candidate — bounds BOTH accumulation sources to ~K:
    # superseded releases AND rejected candidates from a run of
    # build-then-refuse / post-build failures (the just-built candidate is
    # newest, so it survives; the next build reaps it if it never landed).
    _gc_releases(release_manager, logger)
    # §3 preflight DDL-free gate: durable code rollback only holds over an
    # unchanged/additive schema. A non-additive diff is refused HERE, before
    # any spawn. The rejected candidate dir is left for the NEXT build's GC.
    try:
        current_snapshot = _resolve_current_snapshot(release_manager, snapshot_fn)
    except ReleaseManagerError as exc:
        return failure(
            reason=reason,
            expected_etag=expected_etag,
            message=f"schema preflight could not resolve the current snapshot: {exc}",
            reason_code=RestartReasonCode.SCHEMA_PREFLIGHT_REFUSED,
        )
    verdict = schema_preflight(
        candidate,
        current_snapshot=current_snapshot,
        current_release_exists=release_manager.current_release is not None,
    )
    if not verdict.is_additive:
        return failure(
            reason=reason,
            expected_etag=expected_etag,
            message=(f"schema preflight refused deploy — durable rollback only protects code over an additive schema. {verdict.summary()}"),
            reason_code=RestartReasonCode.SCHEMA_PREFLIGHT_REFUSED,
        )
    # GTE-06 L2 fresh-source probe — the LAST pre-spawn gate (design
    # §3.1: cheap gates first; the probe pays a subprocess + full
    # import pass, so it runs only on candidates that passed
    # everything cheaper). RED ⇒ PROBE_FAILED before any spawn; core
    # reacts by rolling the committed manifest bytes back.
    probe_outcome = _contained_probe_outcome(
        preflight_probe,
        candidate=candidate,
        app_home=app_home,
        logger=logger,
    )
    if not probe_outcome.ok:
        return _probe_failed_restart_result(
            probe_outcome,
            reason=reason,
            expected_etag=expected_etag,
            logger=logger,
        )
    return _SwapPlan(
        next_color=next_color,
        candidate=candidate,
        probe_evidence=probe_outcome.payload,
    )
