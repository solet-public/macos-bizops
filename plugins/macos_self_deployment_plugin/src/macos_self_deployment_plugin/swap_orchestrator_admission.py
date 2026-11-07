"""Owned-deploy admission shared by the orchestrator's restart and rollback.

Admission is acquired per call, joins an existing same-thread admission, and
is released on return or exception. Dry runs bypass admission as before.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from functools import wraps
from typing import TYPE_CHECKING, Concatenate

from ananta.core.actions.owned_deploy_admission import OwnedDeployAdmission, OwnedDeployRefusalError
from ananta.interfaces.lifecycle_result_types import RestartResult, RestartStatus

from macos_self_deployment_plugin.constants import RestartReasonCode
from macos_self_deployment_plugin.swap_executor import build_failed_result

if TYPE_CHECKING:
    from macos_self_deployment_plugin.swap_orchestrator import SwapOrchestrator


@dataclass(frozen=True, slots=True)
class OwnedDeployGate:
    """The owned-deploy admission an orchestrator's entry points run under."""

    admission: OwnedDeployAdmission
    logger: logging.Logger

    def run(self, operation: str, *, reason: str, expected_etag: str, body: Callable[[], RestartResult]) -> RestartResult:
        """Run ``body`` admitted, released when it returns or raises; refuse before any side effect.

        A held lock (``owned_deploy_in_progress``) or an admission that cannot
        be established (``owned_deploy_exclusion_unavailable``) is a FAILED
        result before any router probe, build or spawn — the system is
        unchanged and the call is retryable once the owner finishes.
        """
        try:
            with self.admission.admit(operation):
                return body()
        except OwnedDeployRefusalError as refusal:
            return build_failed_result(
                status=RestartStatus.FAILED,
                reason_code=RestartReasonCode(refusal.reason_code),
                message=f"{operation} refused before any build or spawn: {refusal.reason_text}",
                reason=reason,
                expected_etag=expected_etag,
                logger=self.logger,
            )


type _Entry[**P] = Callable[Concatenate[SwapOrchestrator, P], RestartResult]


def _owned_deploy[**P](operation: str) -> Callable[[_Entry[P]], _Entry[P]]:
    """Admit a SwapOrchestrator entry point as an owned deploy (dry runs touch nothing and skip it).

    Every caller of the decorated entry — ``restart_with_manifest``, the
    reconciliation channel, the target-local adapter, the durable rollback
    verb — shares the one admitted body.
    """

    def decorate(entry: _Entry[P]) -> _Entry[P]:
        @wraps(entry)
        def admitted(orchestrator: SwapOrchestrator, *args: P.args, **kwargs: P.kwargs) -> RestartResult:
            if kwargs.get("dry_run") is True:
                return entry(orchestrator, *args, **kwargs)
            return orchestrator.owned_deploy_gate.run(
                operation,
                reason=str(kwargs.get("reason", "")),
                expected_etag=str(kwargs.get("expected_etag", "")),
                body=lambda: entry(orchestrator, *args, **kwargs),
            )

        return admitted

    return decorate


__all__ = ["OwnedDeployGate", "_Entry", "_owned_deploy"]
