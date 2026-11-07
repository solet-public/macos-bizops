"""Free-function ``stop_self`` body for macos_self_deployment_plugin.

Slice 4.5 of
``workbench/2026-06-05_bridge_port_routing_and_session_lifecycle_design.md``.
The plugin class would push over the god-class non-process-LOC
threshold if it carried this body inline; the helper lives at module
scope and the plugin's ``stop_self`` method is a thin delegator.

See the ABC docstring at
:meth:`ananta.interfaces.self_deployment_service_interface.SelfDeploymentServiceInterface.stop_self`
for the full contract; this module just packages the macOS execution.

``stop_self`` is an owned operation: it admits through the solet's
owned-deploy lock before touching the drain sentinel or spawning the
watchdog, so it can never interleave with an apply, restart or rollback.
On success the held lock descriptor moves to the watchdog, which keeps it
through the delayed signals.
"""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from ananta.core.actions.owned_deploy_admission import (
    IdentityProbe,
    OwnedDeployAdmission,
    OwnedDeployLease,
    OwnedDeployRefusalError,
)
from ananta.interfaces.lifecycle_result_types import (
    StopSelfResult,
    StopSelfStatus,
)

from macos_self_deployment_plugin import drain_sentinel, process_identity, stop_self_watchdog
from macos_self_deployment_plugin.constants import OWNED_DEPLOY_OPERATION_STOP_SELF

# What a failed watchdog launch raises (``subprocess.Popen`` and fakes).
_SPAWN_ERRORS = (OSError, subprocess.SubprocessError, ValueError)


def run(
    *,
    solet_name: str,
    reason: str,
    dry_run: bool,
    watchdog_spawner: stop_self_watchdog.WatchdogSpawner,
    identity_probe: IdentityProbe = process_identity.start_token,
) -> StopSelfResult:
    """Admit, then write the drain sentinel + hand the lock to the detached watchdog.

    Idempotent: returns ``StopSelfStatus.ALREADY_STOPPED`` when the
    sentinel is already on disk so a re-invocation by an operator who
    forgot to run ``./launch.py`` doesn't spawn a redundant watchdog
    that would SIGTERM whatever process happens to inherit our pid
    later. A held owned-deploy lock (or one that cannot be established)
    returns ``FAILED`` with ``owned_deploy_in_progress`` /
    ``owned_deploy_exclusion_unavailable`` in the message and touches
    nothing.
    """
    sentinel = drain_sentinel.sentinel_path(solet_name)
    if dry_run:
        return StopSelfResult(
            status=StopSelfStatus.DRY_RUN,
            reason=reason,
            duration_seconds=0.0,
            stopped_at="",
            backend_action_id="",
            dry_run=True,
            message=(f"dry_run=True; would write {sentinel}, spawn detached watchdog SIGTERM-ing pid {os.getpid()}"),
        )
    try:
        admission = OwnedDeployAdmission.for_solet(solet_name, identity_probe=identity_probe)
        with admission.admit(OWNED_DEPLOY_OPERATION_STOP_SELF) as lease:
            return _stop_owned(
                lease,
                solet_name=solet_name,
                sentinel=sentinel,
                reason=reason,
                watchdog_spawner=watchdog_spawner,
                identity_probe=identity_probe,
            )
    except OwnedDeployRefusalError as refusal:
        return _failed(reason, f"stop_self refused before any sentinel or watchdog: {refusal.reason_text}")


def _stop_owned(
    lease: OwnedDeployLease,
    *,
    solet_name: str,
    sentinel: Path,
    reason: str,
    watchdog_spawner: stop_self_watchdog.WatchdogSpawner,
    identity_probe: IdentityProbe,
) -> StopSelfResult:
    """Sentinel + watchdog under admission; undo only this attempt's sentinel on failure."""
    timestamp = datetime.now(UTC).isoformat()
    if sentinel.exists():
        return StopSelfResult(
            status=StopSelfStatus.ALREADY_STOPPED,
            reason=reason,
            duration_seconds=0.0,
            stopped_at=timestamp,
            backend_action_id="",
            dry_run=False,
            message=(f"drain sentinel already at {sentinel}; no-op idempotent return"),
        )
    target_pid = os.getpid()
    target_token = identity_probe(target_pid)
    if target_token is None:
        return _failed(
            reason,
            f"cannot prove this process's identity (no start token for pid {target_pid}); refusing to arm a watchdog that could signal a reused pid",
        )
    drain_sentinel.write(solet_name)
    spawned = False
    try:
        watchdog_pid = lease.transfer(
            lambda lock_fd: watchdog_spawner(
                stop_self_watchdog.WatchdogRequest(target_pid, target_token, lock_fd),
            ),
        )
        spawned = True
    except _SPAWN_ERRORS as exc:
        return _failed(
            reason,
            f"watchdog spawn failed ({type(exc).__name__}: {exc}); the drain sentinel this attempt wrote at {sentinel} was removed",
        )
    finally:
        if not spawned:
            sentinel.unlink(missing_ok=True)
    return StopSelfResult(
        status=StopSelfStatus.SUCCESS,
        reason=reason,
        duration_seconds=0.0,
        stopped_at=timestamp,
        backend_action_id=str(watchdog_pid),
        dry_run=False,
        message=(f"sentinel at {sentinel}; detached watchdog pid {watchdog_pid} holds the owned-deploy lock and will SIGTERM this process shortly"),
    )


def _failed(reason: str, message: str) -> StopSelfResult:
    return StopSelfResult(
        status=StopSelfStatus.FAILED,
        reason=reason,
        duration_seconds=0.0,
        stopped_at="",
        backend_action_id="",
        dry_run=False,
        message=message,
    )
