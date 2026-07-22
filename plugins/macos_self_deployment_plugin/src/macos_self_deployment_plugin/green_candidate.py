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

import logging
import os
import time
from dataclasses import dataclass

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


def _probe_port_reachable(port: int, timeout_seconds: float = 0.5) -> bool:
    """Open a brief TCP connection to localhost:port; close it; report success.

    Used by :meth:`GreenCandidate.wait_until_registered` as a belt-and-
    suspenders check that the registered port is accepting connections — not
    just that the color said "I'm here" via the management socket. A
    successful TCP open implies the bridge HTTP server is bound; a failure
    implies a bind-then-close race or other transient condition warranting
    another poll cycle.
    """
    import socket as _socket

    sock = _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM)
    sock.settimeout(timeout_seconds)
    try:
        sock.connect(("127.0.0.1", port))
    except OSError:
        return False
    finally:
        sock.close()
    return True


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

    def wait_until_registered(self, instance_id: str) -> bool:
        """Poll router.status() until ``instance_id`` appears AND its port accepts TCP.

        The plain "color is listed in status" check confirms the color
        called ``register_color`` after
        :func:`heartbeat_lifecycle._wait_for_bridge_port` observed the
        bridge_port bound on its plugin instance. As a belt-and-suspenders
        defense against rare bind-then-close races (or a malformed register
        payload), this also opens a brief TCP socket to the registered port
        and closes it. Activate only proceeds when both the registry entry
        AND the port are confirmed reachable.
        """
        deadline = time.monotonic() + self._ready_timeout
        while time.monotonic() < deadline:
            try:
                snap = self._router.status()
            except RouterClientError:
                time.sleep(self._ready_poll)
                continue
            colors = snap.get("colors") or []
            if not isinstance(colors, list):
                time.sleep(self._ready_poll)
                continue
            for entry in colors:
                if (
                    isinstance(entry, dict)
                    and entry.get("instance_id") == instance_id
                ):
                    port = entry.get("port")
                    if isinstance(port, int) and _probe_port_reachable(port):
                        return True
                    # Registered but port not (yet) reachable — keep polling.
            time.sleep(self._ready_poll)
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

    def rollback_router(self, prior_color: str) -> bool:
        """Re-activate ``prior_color`` (still draining). Returns True iff confirmed.

        Returns ``False`` on BOTH an RPC error AND an explicit router refusal
        (``rolled_back`` falsey / drain window expired). The caller must NOT
        kill the candidate when this returns ``False`` — the router may still
        route to it, and killing it would route live traffic to a dead color.
        """
        try:
            result = self._router.rollback(prior_color)
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
        self, *, prior_color: str, instance_id: str, pid: int, exc: Exception,
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
        if not self.rollback_router(prior_color):
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
