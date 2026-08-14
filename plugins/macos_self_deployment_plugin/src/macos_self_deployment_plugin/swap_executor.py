"""Shared swap-choreography executor.

One parameterized choreography drives BOTH the forward cutover
(``restart_with_manifest``) and the durable rollback (``rollback_release``):
spawn the next-color solet from a materialized release, wait for it to register,
``activate`` it on the router, swap the durable ``current``/``previous``
symlinks, quiesce the prior color, and enqueue the durable ``complete_swap``
finisher. The two callers differ ONLY in:

- which release is brought up — a freshly-built candidate (forward) vs the
  rehydrated ``previous`` release (rollback);
- the symlink op — :meth:`ReleaseManager.cutover` vs
  :meth:`ReleaseManager.rollback` (passed as ``symlink_swap``);
- the failure status when the brought-up release never registers — a forward
  register-timeout is a plain ``FAILED`` (system unchanged, retryable); a
  rollback target that will not boot is ``NEEDS_INTERVENTION`` (the safety net
  itself is void). Passed as ``register_failure``.

By construction the two paths share every other step, so cutover and rollback
can never drift. Extracted from :class:`~.swap_orchestrator.SwapOrchestrator`
so both that class and this one stay under the god-class LOC bound (the spine
was ~half of the old ``restart``); the orchestrator keeps only the pre-spawn
phase (status probe, color derivation, build-or-rehydrate, preflight).
"""

from __future__ import annotations

import logging
import os
import shlex
import time
import uuid
from collections.abc import Callable, Iterable
from json import JSONDecodeError
from pathlib import Path
from typing import Protocol

from ananta.interfaces.lifecycle_result_types import RestartResult, RestartStatus

from macos_self_deployment_plugin import process_identity
from macos_self_deployment_plugin.constants import (
    COMPLETE_SWAP_PROCESS_KEY,
    DEFAULT_POST_ACTIVATE_GRACE_SECONDS,
    FLOW_ID_PREFIX,
    STATUS_QUEUED,
    RestartReasonCode,
)
from macos_self_deployment_plugin.green_candidate import GreenCandidate
from macos_self_deployment_plugin.pending_finisher import (
    PendingFinisher,
    clear_pending_finisher,
    pending_finisher_path,
    write_pending_finisher,
)
from macos_self_deployment_plugin.release_manager import (
    CandidatePaths,
    ReleaseManagerError,
    SwapResult,
)
from macos_self_deployment_plugin.router_client import (
    RouterClient,
    RouterClientError,
)


class ActionFactoryProtocol(Protocol):
    """Subset of ``ActionFactory`` used to enqueue ``complete_swap``.

    Parameter name ``action_definition`` mirrors the platform signature
    so an injected real ``ActionFactory`` is type-compatible without
    glue.
    """

    def submit_action_definition(
        self,
        action_definition: dict[str, object],
        context: dict[str, object] | None = None,
    ) -> str: ...


class SetActiveTarget(Protocol):
    """Anything with an idempotent ``set_active(bool)``.

    Used to quiesce in-flight local plugins after the router has
    cut over to the new color. ``LifecycleManaged`` plugins on the
    draining color get ``set_active(False)`` during the local quiesce
    sweep.
    """

    def set_active(self, active: bool) -> None: ...


# Type for the spawn helper. Returns the spawned child's pid. Tests inject a
# fake that records the call and returns a synthetic pid. The trailing
# ``CandidatePaths`` is the materialized release the child is spawned FROM
# (design 2026-06-27 §4.5): its own ``venv/bin/python3`` + re-pointed
# ``code/``, NOT ``current`` (which still points at the live old release at
# spawn time).
SpawnFn = Callable[[Path, str, str, str, CandidatePaths], int]

# C2 fix: flips the platform action-queue poller's color-active gate
# (``EventOrchestrator.is_active_color``) for THIS process. Injected so the
# executor stays free of the ananta-core EventOrchestrator; the plugin supplies
# an assignment to the existing public attribute. Flipped ``False`` at quiesce —
# before the ``complete_swap`` row exists — so the draining color's poller
# cannot claim its own finisher and SIGTERM itself.
SetColorActiveFn = Callable[[bool], None]

# The post-activate durable symlink op. ``cutover`` consumes the candidate;
# ``rollback`` ignores it (the target is the ledger's ``previous``). Raises the
# release manager's failure contract on error; the executor's compensation
# catches the broadened raw types too (F1 defense-in-depth).
SymlinkSwapFn = Callable[[CandidatePaths], SwapResult]


def _mint_instance_id(color: str) -> str:
    """Mint a fresh instance id of the form ``solet-<color>-<uuid8>``.

    The shape matches the convention used by the cloud sibling's version
    labels while staying short enough for grep-ability in router logs.
    """
    return f"solet-{color}-{uuid.uuid4().hex[:8]}"


def _mint_flow_id() -> str:
    """Mint a fresh ``flow-<localbg>-<uuid8>`` for the durable finisher row."""
    return f"{FLOW_ID_PREFIX}{uuid.uuid4().hex[:8]}"


def build_failed_result(
    *,
    status: RestartStatus,
    reason_code: str,
    message: str,
    reason: str,
    expected_etag: str,
    logger: logging.Logger,
) -> RestartResult:
    """Build a non-success :class:`RestartResult` (``FAILED`` or
    ``NEEDS_INTERVENTION``), logging at error level.

    Shared by the executor's in-flight failures AND the orchestrator's
    pre-spawn refusals so the (status, reason_code, message, log) shape is
    constructed in exactly one place — the single source of truth for the
    failure envelope.
    """
    logger.error("blue-green swap %s [%s]: %s", status.value, reason_code or "-", message)
    return RestartResult(
        status=status,
        restart_action_id="",
        message=message,
        reason=reason,
        expected_etag=expected_etag,
        dry_run=False,
        reason_code=reason_code,
    )


class SwapExecutor:
    """Drives the spawn → wait → activate → symlink-swap → quiesce → enqueue
    spine shared by forward cutover and durable rollback.

    Stateless per call (every public call is reentrant; per-swap identifiers
    are local). Owns the green candidate's router lifecycle (via
    :class:`GreenCandidate`), the durable-finisher enqueue, and the local
    quiesce sweep. The pre-spawn phase (which release, which color) stays with
    the orchestrator, which calls :meth:`execute` once it has a candidate.
    """

    def __init__(
        self,
        *,
        router_client: RouterClient,
        action_factory: ActionFactoryProtocol,
        session_factory: Callable[[], str],
        solet_name: str,
        runtime_dir: Path,
        set_color_active: SetColorActiveFn,
        spawn_fn: SpawnFn,
        logger: logging.Logger,
        ready_timeout_seconds: int,
        ready_poll_interval_seconds: float,
        post_activate_grace_seconds: float = DEFAULT_POST_ACTIVATE_GRACE_SECONDS,
    ) -> None:
        self._router = router_client
        self._action_factory = action_factory
        self._session_factory = session_factory
        self._solet_name = solet_name
        # B2: durable, additive post-cutover finisher record. Lives in the
        # runtime dir (NOT the release ledger), so writing/clearing it never
        # touches current/previous. Injected runtime_dir keeps smokes on a
        # scratch root.
        self._pending_finisher_path = pending_finisher_path(runtime_dir, solet_name)
        self._set_color_active = set_color_active
        self._spawn_fn = spawn_fn
        self._logger = logger
        self._ready_timeout = ready_timeout_seconds
        self._post_activate_grace = post_activate_grace_seconds
        self._candidate = GreenCandidate(
            router_client=router_client,
            logger=logger,
            ready_timeout_seconds=ready_timeout_seconds,
            ready_poll_interval_seconds=ready_poll_interval_seconds,
        )

    def execute(
        self,
        *,
        app_home: Path,
        candidate: CandidatePaths,
        next_color: str,
        reason: str,
        expected_etag: str,
        self_instance_id: str,
        self_color: str,
        set_active_targets: Iterable[SetActiveTarget],
        symlink_swap: SymlinkSwapFn,
        spawn_failure: tuple[RestartStatus, str],
        register_failure: tuple[RestartStatus, str],
        compensation_codes: tuple[str, str],
    ) -> RestartResult:
        """Spawn ``candidate`` as ``next_color``, activate it, swap symlinks,
        quiesce, and enqueue ``complete_swap``; return the typed result.

        ``symlink_swap`` is the durable pointer op (cutover vs rollback).
        ``spawn_failure`` / ``register_failure`` are the (status, reason_code)
        to return if the brought-up release fails to spawn / never registers:
        ``(FAILED, spawn_failed)`` / ``(FAILED, register_timeout)`` for a forward
        cutover; both ``(NEEDS_INTERVENTION, rollback_target_unbootable)`` for a
        rollback whose target will not boot — a corrupt-executable ``previous``
        passes ``candidate_for``'s dir+VERSION check yet ``Popen`` raises on the
        missing/non-exec interpreter, and a registered-but-unhealthy ``previous``
        never passes the TCP health probe; either way the safety net itself is
        non-functional, not a retryable spawn hiccup. ``compensation_codes`` is
        the (confirmed_code, unconfirmed_code) pair for a post-activate swap
        failure: ``confirmed_code`` pairs with FAILED when the router rollback
        is confirmed (pre-swap pair restored), ``unconfirmed_code`` with
        NEEDS_INTERVENTION when it is not (candidate LEFT ALIVE).
        """
        next_instance_id = _mint_instance_id(next_color)
        spawned = self._spawn_or_fail(
            app_home=app_home, next_color=next_color,
            next_instance_id=next_instance_id, candidate=candidate,
            spawn_failure=spawn_failure, reason=reason, expected_etag=expected_etag,
        )
        if isinstance(spawned, RestartResult):
            return spawned
        spawned_pid = spawned

        if not self._candidate.wait_until_registered(next_instance_id):
            # Kill AND unregister: a registered-but-unhealthy candidate (it
            # passed register but failed the TCP health probe) is in the router's
            # registry, so killing it without unregistering leaves a stale
            # binding (heartbeat GC self-heals it, but we clean up eagerly).
            # unregister is idempotent — a no-op when the child never registered.
            self._candidate.kill(spawned_pid)
            self._candidate.unregister(next_instance_id)
            return build_failed_result(
                status=register_failure[0], reason_code=register_failure[1],
                message=(
                    f"next color {next_color} pid={spawned_pid} did not register "
                    f"with router within {self._ready_timeout}s; SIGKILL + "
                    f"unregister issued."
                ),
                reason=reason, expected_etag=expected_etag, logger=self._logger,
            )

        activate_result = self._activate_or_fail(
            next_color=next_color, next_instance_id=next_instance_id,
            pid=spawned_pid, reason=reason, expected_etag=expected_etag,
        )
        if isinstance(activate_result, RestartResult):
            return activate_result

        swap = self._swap_or_compensate(
            candidate=candidate, symlink_swap=symlink_swap, prior_color=self_color,
            self_instance_id=self_instance_id,
            instance_id=next_instance_id, pid=spawned_pid,
            reason=reason, expected_etag=expected_etag,
            compensation_codes=compensation_codes,
        )
        if isinstance(swap, RestartResult):
            return swap

        return self._finish_queued(
            next_color=next_color, next_instance_id=next_instance_id,
            pid=spawned_pid, self_instance_id=self_instance_id, self_color=self_color,
            set_active_targets=set_active_targets, activate_result=activate_result,
            reason=reason, expected_etag=expected_etag,
        )

    # ------------------------------------------------------------------
    # Spine steps (each a small early-return helper so execute stays B-rank)
    # ------------------------------------------------------------------

    def _spawn_or_fail(
        self, *, app_home: Path, next_color: str, next_instance_id: str,
        candidate: CandidatePaths, spawn_failure: tuple[RestartStatus, str],
        reason: str, expected_etag: str,
    ) -> int | RestartResult:
        try:
            pid = self._spawn_fn(
                app_home, next_color, next_instance_id, self._solet_name,
                candidate,
            )
        except OSError as exc:
            # A forward-cutover spawn failure is a retryable FAILED(spawn_failed);
            # a rollback whose ``previous`` interpreter is missing/non-exec
            # (``Popen`` raises despite ``candidate_for`` passing dir+VERSION) is
            # NEEDS_INTERVENTION(rollback_target_unbootable) — the safety net is
            # non-functional, not a transient hiccup. Parameterized by the caller
            # exactly like ``register_failure``.
            return build_failed_result(
                status=spawn_failure[0], reason_code=spawn_failure[1],
                message=f"spawn next color failed: {exc}",
                reason=reason, expected_etag=expected_etag, logger=self._logger,
            )
        self._logger.info(
            "spawned next color=%s instance=%s pid=%d",
            next_color, next_instance_id, pid,
        )
        return pid

    def _activate_or_fail(
        self, *, next_color: str, next_instance_id: str, pid: int,
        reason: str, expected_etag: str,
    ) -> dict[str, object] | RestartResult:
        try:
            activate_result = self._router.activate(next_color, next_instance_id)
        except RouterClientError as exc:
            self._candidate.kill(pid)
            return build_failed_result(
                status=RestartStatus.FAILED, reason_code=RestartReasonCode.ACTIVATE_REFUSED,
                message=f"router activate({next_color}) failed: {exc}",
                reason=reason, expected_etag=expected_etag, logger=self._logger,
            )
        if not activate_result.get("activated"):
            self._candidate.kill(pid)
            return build_failed_result(
                status=RestartStatus.FAILED, reason_code=RestartReasonCode.ACTIVATE_REFUSED,
                message=(
                    f"router refused activate({next_color}, {next_instance_id}): "
                    f"{activate_result.get('reason', 'unknown')}"
                ),
                reason=reason, expected_etag=expected_etag, logger=self._logger,
            )
        self._logger.info(
            "activated next color=%s previous_color=%s drain_window=%ss",
            next_color,
            activate_result.get("previous_color"),
            activate_result.get("drain_window_seconds"),
        )
        return activate_result

    def _swap_or_compensate(
        self, *, candidate: CandidatePaths, symlink_swap: SymlinkSwapFn,
        prior_color: str, self_instance_id: str, instance_id: str, pid: int,
        reason: str, expected_etag: str, compensation_codes: tuple[str, str],
    ) -> SwapResult | RestartResult:
        """Run the durable symlink swap; on failure compensate + return the
        typed failure (FAILED if compensation restored the pre-swap pair,
        NEEDS_INTERVENTION if it could not — candidate LEFT ALIVE).

        §4.7 / F1 defense-in-depth: ``symlink_swap``'s contract is to raise only
        ``ReleaseManagerError``, but a raw ``OSError``/``JSONDecodeError``/
        ``KeyError`` from its bracketing ledger I/O could escape — catching them
        HERE guarantees the compensation runs even on a contract violation
        (router cut over, no rollback) instead of the raw exception escaping.
        """
        # B2 (Codex rec#1): persist the durable post-cutover finisher record
        # IMMEDIATELY BEFORE the irreversible symlink swap, so it provably
        # exists the instant the cutover is durable — closing the
        # {swap → record-write} window rather than merely narrowing it. The
        # prior color is THIS draining process (our own pid + self_instance_id).
        # Written OUTSIDE the try: a write failure must abort before the
        # irreversible swap (no undurable-finisher cutover), so it propagates.
        #
        # Codex round-2 B2·1: the record carries ``candidate_release_id`` so the
        # backstop stays INERT until ``current`` actually names this candidate —
        # writing-before-swap closed the post-swap window without opening a
        # premature-action one. B2·3: ``prior_start_token`` is captured here about
        # OUR OWN live pid, so the backstop can prove a later kill targets the
        # same process (not a reused pid).
        write_pending_finisher(
            self._pending_finisher_path,
            PendingFinisher(
                prior_pid=os.getpid(),
                prior_instance_id=self_instance_id,
                prior_color=prior_color,
                candidate_release_id=candidate.release_id,
                prior_start_token=process_identity.start_token(os.getpid()),
            ),
        )
        try:
            swap = symlink_swap(candidate)
        except (ReleaseManagerError, OSError, JSONDecodeError, KeyError) as exc:
            # Swap aborted: the router rollback (confirmed) or the operator-
            # handled NEEDS_INTERVENTION owns the prior color now — the cutover
            # never became durable, so no orphan is owed. Clear the record we
            # just wrote so the heartbeat backstop never SIGTERMs the prior.
            clear_pending_finisher(self._pending_finisher_path)
            outcome = self._candidate.compensate_failed_swap(
                prior_color=prior_color, instance_id=instance_id, pid=pid, exc=exc,
            )
            confirmed_code, unconfirmed_code = compensation_codes
            # F2-iv: a CONFIRMED router rollback restored the pre-swap pair →
            # FAILED (retryable). An UNCONFIRMED rollback left the candidate
            # ALIVE / the durable pair possibly incoherent → NEEDS_INTERVENTION.
            status, reason_code = (
                (RestartStatus.FAILED, confirmed_code)
                if outcome.restored
                else (RestartStatus.NEEDS_INTERVENTION, unconfirmed_code)
            )
            return build_failed_result(
                status=status, reason_code=reason_code,
                message=outcome.message, reason=reason, expected_etag=expected_etag,
                logger=self._logger,
            )
        self._logger.info(
            "durable swap pointers current=%s previous=%s",
            swap.current, swap.previous,
        )
        return swap

    def _finish_queued(
        self, *, next_color: str, next_instance_id: str, pid: int,
        self_instance_id: str, self_color: str,
        set_active_targets: Iterable[SetActiveTarget],
        activate_result: dict[str, object], reason: str, expected_etag: str,
    ) -> RestartResult:
        # Brief grace before quiescing prior-color background work. The drain
        # window gates new routing immediately on activate, but in-flight
        # clients on the prior color may emit one more request whose response
        # benefits from the prior color still being fully alive.
        time.sleep(self._post_activate_grace)
        self._quiesce_local_plugins(set_active_targets)
        # C2 fix: gate THIS (now-draining) color's poller OFF BEFORE the
        # complete_swap row is enqueued below, so only the NEW color's poller
        # ever claims the finisher (the draining poller cannot SIGTERM itself).
        self._set_color_active(False)
        # B2: the durable pending-finisher record was already written before the
        # (now-completed) irreversible swap. Enqueue the FAST-path finisher, but
        # a StateService/session-row failure here must NOT fail an already-
        # durable cutover — the record + the heartbeat backstop on the NEW active
        # color guarantee the prior color is still SIGTERM'd + unregistered.
        restart_action_id = ""
        try:
            restart_action_id = self._enqueue_complete_swap(
                prior_pid=os.getpid(), prior_instance_id=self_instance_id,
                prior_color=self_color, reason=reason,
            )
        except Exception:  # noqa: BLE001 — cutover is durable; the backstop completes cleanup
            self._logger.exception(
                "complete_swap enqueue FAILED after a durable cutover; the "
                "pending-finisher backstop on the new active color will "
                "complete prior-color cleanup (no orphan).",
            )
        return RestartResult(
            status=RestartStatus.QUEUED,
            restart_action_id=restart_action_id,
            message=shlex.join(
                [
                    "next_color", next_color,
                    "next_instance_id", next_instance_id,
                    "spawned_pid", str(pid),
                    "drain_window_seconds",
                    str(activate_result.get("drain_window_seconds", 0)),
                    "reason", reason,
                ],
            ),
            reason=reason,
            expected_etag=expected_etag,
            dry_run=False,
            reason_code=RestartReasonCode.NONE,
        )

    def _quiesce_local_plugins(
        self, set_active_targets: Iterable[SetActiveTarget]
    ) -> None:
        """Call set_active(False) on each target. Errors logged + swallowed."""
        for target in set_active_targets:
            try:
                target.set_active(False)
            except Exception as exc:  # noqa: BLE001 — quiesce is best-effort
                self._logger.exception(
                    "set_active(False) raised on %r: %s", target, exc,
                )

    def _enqueue_complete_swap(
        self,
        *,
        prior_pid: int,
        prior_instance_id: str,
        prior_color: str,
        reason: str,
    ) -> str:
        """Submit the durable ``complete_swap`` action targeting the new color's
        poller.

        Task #19 fix: the prior None-guard + synthetic-token fallback was
        deleted. Under the plugin's lazy-construction pattern the orchestrator
        can only be built once ``action_factory`` is non-None, so
        ``self._action_factory`` here is guaranteed non-None.
        """
        flow_id = _mint_flow_id()
        # session_id MUST come from the state_service via session_factory —
        # synthetic id-shaped strings would land in
        # ``core__action_events.core__sessions_id`` without a paired row in
        # ``core.sessions``, drifting from the state_service-mediated row
        # invariant. The factory call writes the session row and returns
        # the generated id.
        session_id = self._session_factory()
        action_def: dict[str, object] = {
            "name": "complete_swap",
            "process_key": COMPLETE_SWAP_PROCESS_KEY,
            "flow_id": flow_id,
            "session_id": session_id,
            "arguments": {
                "prior_pid": prior_pid,
                "prior_instance_id": prior_instance_id,
                "prior_color": prior_color,
            },
            "action_status": STATUS_QUEUED,
            "reason": reason,
            # Plan-derived EDGE actions must carry result_processor_kind or
            # trip RESULT_CONTRACT_VIOLATION (result_processor_kind_missing)
            # on success-path validation. INFERENCE is the codebase default
            # (BRIDGE_DELIVERY is forbidden for plan-derived actions per
            # `ananta/core/result_processing/enums.py`; DETERMINISTIC_CONTINUATION
            # requires an active plan with a next step and a process-level
            # error_processor, neither of which applies to a one-shot durable
            # handoff). The action_factory's metadata-preservation list
            # propagates this onto the runtime action.
            "result_processor_kind": "inference",
        }
        return self._action_factory.submit_action_definition(action_def)


__all__ = [
    "ActionFactoryProtocol",
    "SetActiveTarget",
    "SetColorActiveFn",
    "SpawnFn",
    "SwapExecutor",
    "SymlinkSwapFn",
    "build_failed_result",
]
