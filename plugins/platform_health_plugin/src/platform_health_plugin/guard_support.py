"""Shared state and scheduling helpers for platform health guards."""

from __future__ import annotations

from typing import Any, Final

__all__ = [
    "STATE_NAMESPACE",
    "STATUS_CRITICAL",
    "STATUS_OK",
    "STATUS_WARNING",
    "_get_state_service",
    "_unwrap_response",
]

STATE_NAMESPACE: Final[str] = "platform_health_plugin"
STATUS_OK: Final[str] = "ok"
STATUS_WARNING: Final[str] = "warning"
STATUS_CRITICAL: Final[str] = "critical"


def _get_state_service(orchestrator: object) -> object:
    get_service = getattr(orchestrator, "get_service", None)
    if get_service is None:
        raise RuntimeError("orchestrator has no get_service; cannot reach state_service")
    service = get_service("state_service")
    if service is None:
        raise RuntimeError("state_service is not bound on this solet")
    return service


def _unwrap_response(result: object, *, action: str) -> dict[str, Any]:
    """The scheduling plugin's ``build_response`` shape: unwraps ``data`` on
    success and RAISES on an error-status response instead of returning it as
    though it were data — a logical scheduling failure must fail loudly here
    too, not be silently read as "no existing schedule found"."""
    if not isinstance(result, dict):
        raise RuntimeError(f"{action}: expected a dict response, got {type(result).__name__}")
    status = result.get("action_status")
    if status != "completed":
        raise RuntimeError(f"{action}: {result.get('error')!r} (action_status={status!r})")
    data = result.get("data")
    return data if isinstance(data, dict) else {}
