"""Shared catalog-guard fixtures; imported by the registered guard smoke."""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import httpx

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "plugins/agent_messaging_plugin/tests"))

import model_capability_refresh_smoke as refresh_fixtures  # noqa: E402  # pyright: ignore[reportMissingImports]
from _real_state_fake import RealShapeState  # noqa: E402  # pyright: ignore[reportMissingImports]
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from platform_health_plugin import model_capability_guard as guard  # noqa: E402

__all__ = [
    "ROLE",
    "_BASE",
    "_CatalogMessagingPlugin",
    "_Clock",
    "_detail_outage_handler",
    "_failing_handler",
    "_failure_envelope",
    "_healthy_handler",
    "_no_renewal_handler",
    "_success",
]

from agent_messaging_plugin.model_capability_store import CatalogError  # noqa: E402

ROLE = "Main-Seat"
_BASE = datetime(2026, 10, 9, 12, tzinfo=UTC)


class _Clock:
    """Fake clock: the guard's ``clock``. ``real`` follows the wall clock plus
    the offset (for scenarios whose cells come from the real refresh, which
    stamps ``measured_at`` with the wall clock); otherwise it is ``_BASE``."""

    def __init__(self, *, real: bool = False) -> None:
        self.offset = timedelta(0)
        self._real = real

    def __call__(self) -> datetime:
        return (datetime.now(UTC) if self._real else _BASE) + self.offset


def _healthy_handler(request: httpx.Request) -> httpx.Response:
    """Every fetch answers; gpt-6-astra max gets two agreeing sources, and the other scored cells are
    accepted on the leaderboard alone (a detail page that does not parse is not a failed source)."""
    if str(request.url).endswith("/models/claude-sonnet-5"):
        return httpx.Response(200, text="<html><body>no data here</body></html>")
    return refresh_fixtures._handler(request)  # noqa: SLF001 -- the fixture handler is the shared real-shaped transport


def _no_renewal_handler(request: httpx.Request) -> httpx.Response:
    """The leaderboard answers with every score 10 points past its prior reading and no detail page parses:
    readings are gathered, no source FAILS, and no cell can renew -- each single-source reading is past
    tolerance of the cell's prior accepted value, so the prior is kept (the opus row is dropped so no
    conflicted cell, which has no accepted prior, is accepted on the leaderboard alone)."""
    if str(request.url).endswith("/leaderboards/models"):
        moved = [(*row[:3], str(int(row[3]) + 10), *row[4:]) for row in refresh_fixtures._AI_ROWS if "Opus" not in row[0]]  # noqa: SLF001
        return httpx.Response(200, text=refresh_fixtures._leaderboard(moved))  # noqa: SLF001
    return httpx.Response(200, text="<html><body>no data here</body></html>")


def _failing_handler(request: httpx.Request) -> httpx.Response:  # noqa: ARG001
    return httpx.Response(500, text="upstream down")


def _detail_outage_handler(request: httpx.Request) -> httpx.Response:
    """The leaderboard answers and every detail page fails: one failed source per discovered cell."""
    if str(request.url).endswith("/leaderboards/models"):
        return refresh_fixtures._handler(request)  # noqa: SLF001
    return httpx.Response(500, text="upstream down")


def _failure_envelope(code: str, message: str) -> dict[str, Any]:
    return {"action_status": "failed", "data": {}, "error": {"code": code, "message": message}}


class _CatalogMessagingPlugin:
    """Stands in for ``agent_messaging_plugin``: the REAL refresh verb body
    over a MockTransport (or a canned renewal for the seeded-age scenarios) and
    a recording ``peer_send_by_name``."""

    def __init__(self, state: RealShapeState, *, handler: Any = _healthy_handler, delivery: str = "queued_wake") -> None:
        from ananta.interfaces.state_management_interface import StateManagementInterface

        from agent_messaging_plugin.model_catalog_policy import read_catalog_policy, set_catalog_configuration

        typed = cast("StateManagementInterface", state)
        if read_catalog_policy(typed, "catalog") is None:
            set_catalog_configuration(typed, {"stable_id": "catalog", "action": "set", "expected_revision": None, "reason": "explicit guard fixture", "values": {"staleness_window_hours": 72}}, actor="guard-smoke-operator", source=False)
        self.state = state
        self.handler = handler
        self.delivery = delivery
        self.touch_models: list[str] | None = None  # canned mode: refresh these seeded cells to "now"
        self.touch_clock: _Clock | None = None
        self.raise_on_refresh: Exception | None = None
        self.canned_envelope: dict[str, Any] | None = None
        self.refresh_calls: list[dict[str, Any]] = []
        self.refresh_states: list[dict[str, Any]] = []
        self.sends: list[dict[str, Any]] = []

    def refresh_model_capability_catalog(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return _success({"job_id": "queued-refresh", "status": "queued"})

    def refresh_model_capability_catalog_sync(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        self.refresh_calls.append(params)
        self.refresh_states.append(state)
        if self.raise_on_refresh is not None:
            raise self.raise_on_refresh
        if self.canned_envelope is not None:
            return self.canned_envelope
        if self.touch_models is not None:
            assert self.touch_clock is not None
            for model in self.touch_models:
                self.state.update_state(
                    AGENT_ROLE_BINDING_NAMESPACE,
                    {"table": guard.TABLE_MODEL_CAPABILITY_CELL, "filters": {"model": model}},
                    {"measured_at": self.touch_clock().isoformat()},
                )
            return _success(
                {
                    "run_id": "run-canned",
                    "trigger": params["trigger"],
                    "readings_gathered": 2 * len(self.touch_models),
                    "sources_ok": ["canned"],
                    "sources_failed": [],
                    "cells_accepted": len(self.touch_models),
                    "cells_conflicted": 0,
                    "cells_unchanged": 0,
                }
            )
        try:
            data = refresh_fixtures._refresh(self.state, self.handler, trigger=str(params["trigger"]))  # noqa: SLF001 -- the shared hermetic harness fixture
        except CatalogError as exc:
            return _failure_envelope(exc.code, exc.message)
        return _success(data)

    def peer_send_by_name(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        self.sends.append(params)
        return {"action_status": "completed", "data": {"delivery": self.delivery}}


def _success(data: dict[str, Any]) -> dict[str, Any]:
    return {"action_status": "completed", "data": data, "error": None}
