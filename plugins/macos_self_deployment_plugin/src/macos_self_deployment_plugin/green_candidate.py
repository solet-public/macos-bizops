"""The green candidate's router lifecycle, split out of ``SwapOrchestrator``.

A swap spawns a green CANDIDATE process and drives it against the
local-blue-green router across its lifecycle: wait for it to register +
accept connections, tear it down (SIGKILL + unregister), and — when a
post-``activate`` durable swap (cutover OR rollback) fails — compensate by
rolling the router back to the prior color and conditionally tearing the
candidate down (§4.7 F2), returning a typed :class:`CompensationOutcome`.

Extracted from :class:`~.swap_orchestrator.SwapOrchestrator` so the swap
*choreography* stays coherent and bounded (the god-class gate), mirroring
the ``ReleaseBuilder`` / ``ReleaseLedger`` split out of ``ReleaseManager``.
This is a pure relocation — no behaviour change; the orchestrator's smokes
(cutover_failure, instance_authority, poller_gate, swap_round_trip) are the
behaviour-equivalence proof. The controller is stateless: per-swap
identifiers (instance id, pid, prior color) are passed to each method.
"""

from __future__ import annotations

import http.client
import logging
import os
import time
from dataclasses import dataclass
from enum import StrEnum

from macos_self_deployment_plugin.router_client import (
    RouterClient,
    RouterClientError,
)


@dataclass(frozen=True, slots=True)
class CompensationOutcome:
    """Outcome of :meth:`GreenCandidate.compensate_failed_swap`.

    The compensation is context-agnostic (it does the same router-rollback +
    F2-gated kill for a failed forward cutover and a failed durable rollback);
    the caller maps these two outcomes to the right ``RestartStatus`` +
    ``reason_code``:

    - ``restored=True`` — the router rollback to the prior color CONFIRMED and
      the candidate was killed + unregistered, so the pre-swap pair is restored.
      The caller returns ``FAILED`` (system coherent, retryable).
    - ``restored=False`` — the router rollback did NOT take (RPC error / refusal
      / drain expired), so the candidate is LEFT ALIVE (the router may still
      route to it; killing it would route live traffic to a dead color). The
      caller returns ``NEEDS_INTERVENTION`` (a human must act).
    """

    restored: bool
    message: str


class CandidateReadiness(StrEnum):
    """Why :meth:`GreenCandidate.wait_until_registered` stopped waiting.

    ``TIMED_OUT`` and ``EXITED`` are both failures, but they are not the same
    failure and must not cost the same wall-clock. A candidate that is still
    running has simply not finished starting, and waiting out the full timeout
    is the correct thing to do. A candidate that is already DEAD will never
    register, and every further second spent polling for it is a second the
    swap holds the platform's action queue for nothing.
    """

    REGISTERED = "registered"
    EXITED = "exited"
    TIMED_OUT = "timed_out"


def _child_exited(pid: int) -> bool:
    """Whether the spawned candidate has exited — zombies included.

    ``os.kill(pid, 0)`` on its own quietly gets this wrong. A child that has
    died but not yet been reaped is a ZOMBIE: it still occupies the process
    table, so the signal probe succeeds and the corpse reads as perfectly
    healthy. That is precisely the state the 2026-08-31 spawn hang left behind
    and a liveness check that cannot see it would be a check in name only.
    ``waitpid`` with ``WNOHANG`` is what tells a zombie from a live process, and
    it reaps the corpse as a side effect.

    The order of the two probes is load-bearing:

    * The signal probe runs FIRST so a child already reaped by someone else
      reads as gone. ``subprocess`` opportunistically reaps garbage-collected
      ``Popen`` handles whenever any other subprocess is created anywhere in
      this process, and the swap path deliberately discards its handle — so
      "reaped by another thread" is a real, reachable state, not a theoretical one.
    * ``ChildProcessError`` from ``waitpid`` while the pid still exists is left
      deliberately as ALIVE. That pid is not ours to judge, and a false "died"
      verdict would abort a perfectly healthy swap — the strictly worse error.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
    except ChildProcessError:
        return False
    return reaped == pid


def _probe_port_reachable(port: int, timeout_seconds: float = 0.5) -> bool:
    """Require a bounded response from the candidate bridge's health endpoint.

    Used by :meth:`GreenCandidate.wait_until_registered` as a belt-and-
    suspenders check that the registered port serves its bridge surface — not
    just that the color said "I'm here" via the management socket. A TCP open
    proves only that a listener is bound; ``GET /api/v1/bridge/health`` must
    receive a 200 response before activation can proceed. A response timeout,
    malformed HTTP reply, or non-200 status warrants another poll cycle.
    """
    connection = http.client.HTTPConnection(
        "127.0.0.1",
        port,
        timeout=timeout_seconds,
    )
    try:
        connection.request("GET", "/api/v1/bridge/health")
        response = connection.getresponse()
        response.read()
    except (OSError, http.client.HTTPException):
        return False
    finally:
        connection.close()
    return response.status == http.HTTPStatus.OK


class GreenCandidate:
    """Stateless controller for a swap's green candidate vs. the router."""

    def __init__(
        self,
        *,
        router_client: RouterClient,
        logger: logging.Logger,
        ready_timeout_seconds: int,
        ready_poll_interval_seconds: float,
    ) -> None:
        self._router = router_client
        self._logger = logger
        self._ready_timeout = ready_timeout_seconds
        self._ready_poll = ready_poll_interval_seconds

    def wait_until_registered(
        self, instance_id: str, *, pid: int,
    ) -> CandidateReadiness:
        """Poll router.status() until ``instance_id`` appears AND its bridge serves.

        The plain "color is listed in status" check confirms the color
        called ``register_color`` after
        :func:`heartbeat_lifecycle._wait_for_bridge_port` observed the
        bridge_port bound on its plugin instance. As a belt-and-suspenders
        defense against bind-then-wedge races (or a malformed register
        payload), this sends a bounded health request to the registered bridge
        port. Activate only proceeds when both the registry entry AND the
        bridge response are confirmed.
        """
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            if self._registered_and_reachable(instance_id):
                return CandidateReadiness.REGISTERED
            # Liveness is checked AFTER registration so a candidate that came up
            # and exited within one poll cycle is still credited with the
            # registration it achieved.
            if _child_exited(pid):
                self._logger.error(
                    "swap candidate %s (pid %d) exited before registering with "
                    "the router; failing now instead of polling a corpse for "
                    "the remaining %.0fs",
                    instance_id, pid, max(0.0, deadline - time.monotonic()),
                )
                return CandidateReadiness.EXITED
            time.sleep(self._ready_poll)
        return CandidateReadiness.TIMED_OUT

    def _registered_and_reachable(self, instance_id: str) -> bool:
        """One poll: is ``instance_id`` in the registry AND serving bridge health?"""
        try:
            snap = self._router.status()
        except RouterClientError:
            return False
        colors = snap.get("colors") or []
        if not isinstance(colors, list):
            return False
        for entry in colors:
            if isinstance(entry, dict) and entry.get("instance_id") == instance_id:
                port = entry.get("port")
                if isinstance(port, int) and _probe_port_reachable(port):
                    return True
                # Registered but port not (yet) reachable — keep polling.
        return False

    def kill(self, pid: int) -> None:
        """SIGKILL the spawned child; swallow OSError on already-dead processes."""
        try:
            os.kill(pid, 9)
        except (ProcessLookupError, PermissionError) as exc:
            self._logger.warning("could not SIGKILL pid=%d: %s", pid, exc)

    def unregister(self, instance_id: str) -> None:
        """Unregister ``instance_id`` from the router; swallow RPC errors."""
        try:
            self._router.unregister_color(instance_id)
        except RouterClientError as exc:
            self._logger.warning("unregister_color(%s) failed: %s", instance_id, exc)

    def rollback_router(self, prior_color: str, prior_instance_id: str) -> bool:
        """Re-activate one draining instance. Returns True iff confirmed.

        Returns ``False`` on BOTH an RPC error AND an explicit router refusal
        (``rolled_back`` falsey / drain window expired). The caller must NOT
        kill the candidate when this returns ``False`` — the router may still
        route to it, and killing it would route live traffic to a dead color.
        """
        try:
            result = self._router.rollback(prior_color, prior_instance_id)
        except RouterClientError as exc:
            self._logger.error("router rollback(%s) failed: %s", prior_color, exc)
            return False
        if not result.get("rolled_back"):
            self._logger.error(
                "router refused rollback(%s): %s",
                prior_color, result.get("reason", "unknown"),
            )
            return False
        return True

    def compensate_failed_swap(
        self, *, prior_color: str, prior_instance_id: str, instance_id: str, pid: int, exc: Exception,
    ) -> CompensationOutcome:
        """§4.7 post-activate swap-failure compensation; return a typed outcome.

        Reached only when the durable symlink op (``cutover`` for a forward
        swap, ``rollback`` for the durable-rollback verb) raised AFTER a
        successful router ``activate`` and BEFORE ``complete_swap`` was
        enqueued. The symlink op reverts its own half-applied state on an
        in-process ``OSError`` (and never touches the symlinks on a pre-swap
        raise), so ``current``/``previous`` are already unchanged; this restores
        the *routing* side to match.

        F2 — the candidate kill is GATED on a CONFIRMED router rollback:

        - if ``rollback(prior_color)`` confirms, the prior color is
          authoritative again, so the candidate must not serve — SIGKILL +
          unregister it and return ``restored=True`` (the caller returns FAILED
          without enqueuing ``complete_swap``, so the prior process is never
          SIGTERM'd);
        - if the rollback does NOT take (RPC error or the router refuses /
          drain window expired), the router may STILL route to the candidate.
          Killing it then would route live traffic to a DEAD color — so leave
          the candidate ALIVE and return ``restored=False`` with a message that
          does NOT claim the prior color was restored. The caller escalates to
          NEEDS_INTERVENTION.
        """
        self._logger.error(
            "swap failed after activate; attempting router rollback to prior "
            "color=%s (candidate instance=%s pid=%d): %s",
            prior_color, instance_id, pid, exc,
        )
        if not self.rollback_router(prior_color, prior_instance_id):
            self._logger.critical(
                "swap failed AND router rollback to %s did not take; leaving "
                "candidate instance=%s pid=%d ALIVE to avoid routing live traffic "
                "to a dead color. Manual intervention required.",
                prior_color, instance_id, pid,
            )
            return CompensationOutcome(
                restored=False,
                message=(
                    f"durable swap failed after activate AND router rollback to "
                    f"{prior_color} did NOT take; candidate instance={instance_id} "
                    f"LEFT ALIVE (router may still route to it) — manual "
                    f"intervention required: {exc}"
                ),
            )
        self.kill(pid)
        self.unregister(instance_id)
        return CompensationOutcome(
            restored=True,
            message=(
                f"durable swap failed after activate; prior color {prior_color} "
                f"restored, candidate killed (current/previous unchanged): {exc}"
            ),
        )


__all__ = ["CompensationOutcome", "GreenCandidate"]
