"""Daily model capability catalog refresh + loud alert when it stops renewing.

Problem ``wgr_96cceb7fb786427ea6fb20603a5ff4f2``; issues ``iss_4bfe859f``
(nothing schedules the refresh and nothing alerts when it fails),
``iss_ec0f23d0`` (a run that gathered readings but refreshed no cell reports
success) and ``iss_63146325`` (every cell expires at one instant and no read
shows when). Operator ruling ``rul_becb4bed``: refresh at appropriate times
and tell the operator when refreshing is not working; never block progress on
stale data. The selector keeps serving the last accepted readings; this module
is what makes those readings actually renew, and says so when they do not.

The shape deliberately mirrors :mod:`platform_health_plugin.host_disk_guard`,
and reuses its delivery, marker and scheduling helpers rather than growing a
second copy that could drift:

* :func:`run_model_capability_guard` — the cron-fired check. It runs the
  refresh (``plugin::agent_messaging_plugin::refresh_model_capability_catalog``
  with ``trigger="cron"``), then classifies the result and the catalog as
  ``ok`` / ``warning`` / ``critical`` and alerts a durable role on a severity
  TRANSITION only (including the recovery back to ``ok``), never once per tick.
  ``agent_messaging_plugin`` is not a bound service (see
  ``host_disk_guard``'s module docstring), so it is resolved through
  ``orchestrator.plugin_manager.get_plugin`` and its public
  worker-only synchronous refresh implementation is called as ``method(params=..., state=...)``; dispatched checks enqueue their complete guard pass.
  Resolution failure raises: a guard that cannot reach the refresh must not
  report ``ok``.
* :func:`ensure_model_capability_guard_schedule` — idempotent install of the
  daily cron on the platform's own scheduler, found again by tag.

**Severity.** ``critical`` when the refresh raised or returned a failure
envelope, recorded any failed source, or the oldest accepted cell is older than
the staleness window the cells themselves carry (``staleness_window_hours``;
the tightest known one). Null cell windows are unknown and do not contribute
to cadence; an all-null catalog has no known cadence. ``warning`` when the run
renewed no accepted cell, or the oldest accepted cell is older than
``warning_age`` (48 hours, capped at the window). The window is read from the
catalog, never hard-coded here.

**What "renewed" means.** ``cells_unchanged`` in the run counts roster cells
that got a ``pending_crosscheck`` outcome — insufficient evidence — and is
therefore non-zero precisely when a run gathered too little to refresh
anything. A rule of "accepted plus unchanged is zero" would almost never fire,
so renewal is measured directly: a run renewed the catalog only if at least
one accepted cell's ``measured_at`` is newer after the run than before it.

**Configuration.** The alert target is the plugin's own ``alert_role`` config
key, read by the plugin the same way the host disk guard reads it, with no
baked-in default. With none configured the check STILL runs the refresh (the
refresh is the point), logs at ERROR, and returns ``alerted=False`` with
``alert_reason="alert_target_unconfigured"``. Unlike the host disk guard's
installer, :func:`ensure_model_capability_guard_schedule` therefore does not
refuse without a role: a cron that only refreshes is still worth installing.

The marker (``StateManagementInterface`` key-value, no new table) advances only
on a confirmed live delivery, so an undelivered transition is retried on the
next tick.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any, Final, cast

from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_completed, require_records

from platform_health_plugin.host_disk_guard import (
    REASON_ALERT_TARGET_UNCONFIGURED,
    STATE_NAMESPACE,
    STATUS_CRITICAL,
    STATUS_OK,
    STATUS_WARNING,
    _delivery_outcome,
    _get_state_service,
    _resolve_agent_messaging_plugin,
    _send_peer_alert,
    _unwrap_response,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)

CHECK_PROCESS_NAME: Final[str] = "check_model_capability_catalog"
ENSURE_SCHEDULE_PROCESS_NAME: Final[str] = "ensure_model_capability_guard_schedule"
CHECK_PROCESS_KEY: Final[str] = f"plugin::platform_health_plugin::{CHECK_PROCESS_NAME}"
# The cron's EDGE_SINK target. ensure_model_capability_guard_schedule spells
# this key as a literal (see there); the smoke pins the literal to this constant.
CHECK_CRON_PROCESS_KEY: Final[str] = "plugin::platform_health_plugin::check_model_capability_catalog_cron"

REFRESH_TRIGGER: Final[str] = "cron"

DEFAULT_SCHEDULE_CRON: Final[str] = "30 5 * * *"  # daily, 05:30 UTC
DEFAULT_SCHEDULE_TAG: Final[str] = "model_capability_guard"
DEFAULT_SCHEDULE_LABEL: Final[str] = "Model capability catalog refresh guard"
DEFAULT_WARNING_AGE: Final[timedelta] = timedelta(hours=48)

# System-owned flow_id/session_id for the cron's own schedule row (the
# action_factory._enforce_flow_id rule documented in host_disk_guard).
_GUARD_FLOW_ID: Final[str] = "flow-model-capability-guard"
_GUARD_SESSION_ID: Final[str] = "sess-model-capability-guard"

_LAST_NOTIFIED_KEY: Final[str] = "model_capability_guard:last_notified_status"

# agent_messaging_plugin owns these tables (schema.TABLE_MODEL_CAPABILITY_CELL
# and CATALOG_PAGE_SIZE / CATALOG_READ_CEILING in model_capability_store); this
# plugin does not depend on it, so the smoke pins these literals to the owner's.
TABLE_MODEL_CAPABILITY_CELL: Final[str] = "model_capability_cell"
CELL_ACCEPTANCE_ACCEPTED: Final[str] = "accepted"
CATALOG_PAGE_SIZE: Final[int] = 100
CATALOG_READ_CEILING: Final[int] = 1000
MAX_FAILED_SOURCES_IN_ALERT: Final[int] = 3

_CellKey = tuple[str, str, str]


@dataclass(frozen=True)
class ModelCapabilityGuardConfig:
    """``alert_role`` ``None`` means unconfigured (no baked-in identity)."""

    alert_role: str | None = None
    warning_age: timedelta = DEFAULT_WARNING_AGE

    def __post_init__(self) -> None:
        if self.warning_age <= timedelta(0):
            raise ValueError(f"warning_age must be positive; got {self.warning_age}")


@dataclass(frozen=True)
class RefreshOutcome:
    """What one refresh call reported. ``failure`` is set when the call raised,
    returned a non-completed envelope, or returned a result this module cannot
    read; the counts are then zero."""

    failure: str | None
    readings_gathered: int = 0
    cells_accepted: int = 0
    cells_conflicted: int = 0
    cells_unchanged: int = 0
    sources_failed: tuple[str, ...] = ()


@dataclass(frozen=True)
class CatalogReading:
    """The accepted cells of the catalog at one instant."""

    measured_at: dict[_CellKey, datetime]
    window: timedelta | None
    truncated_at: _CellKey | None = None
    """The first accepted cell the read did not reach past the ceiling; None when complete."""

    def oldest(self) -> datetime | None:
        return min(self.measured_at.values()) if self.measured_at else None


@dataclass(frozen=True)
class Assessment:
    status: str
    reasons: tuple[str, ...]
    oldest_age: timedelta | None
    window: timedelta | None
    renewed: bool


def _typed_state(orchestrator: object) -> StateManagementInterface:
    return cast("StateManagementInterface", _get_state_service(orchestrator))


def _parse_stamp(raw: object, field: str) -> datetime:
    if not isinstance(raw, str):
        raise ValueError(f"model capability cell {field} must be an ISO-8601 string; got {raw!r}")
    parsed = datetime.fromisoformat(raw)
    if parsed.tzinfo is None:
        raise ValueError(f"model capability cell {field} {raw!r} carries no timezone")
    return parsed


def _known_window(raw: object, key: _CellKey) -> int | None:
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        raise ValueError(f"model capability cell {key} has no positive integer staleness_window_hours: {raw!r}")
    return raw


def read_accepted_catalog(orchestrator: object) -> CatalogReading:
    """Accepted cells' ``measured_at`` per (runtime, model, effort) and the
    tightest known ``staleness_window_hours`` among them. Null windows
    are unknown and skipped; an all-null catalog has no known cadence.

    Reads only accepted rows, walked in ``CATALOG_PAGE_SIZE`` pages up to
    ``CATALOG_READ_CEILING``. Past the ceiling the reading keeps the rows it has
    and records ``truncated_at``; the assessment names it. It never raises for
    size. A malformed row raises: a guard that cannot read the catalog must not
    report it healthy.
    """
    state = _typed_state(orchestrator)
    rows: list[dict[str, Any]] = []
    after: list[object] | None = None
    truncated_at: _CellKey | None = None
    while True:
        query: dict[str, object] = {
            "table": TABLE_MODEL_CAPABILITY_CELL,
            "filters": {"acceptance": CELL_ACCEPTANCE_ACCEPTED},
            "order_by": [["runtime", "asc"], ["model", "asc"], ["effort", "asc"], ["id", "asc"]],
            "limit": CATALOG_PAGE_SIZE,
        }
        if after is not None:
            query["after"] = after
        page = require_records(state.query_ordered(AGENT_ROLE_BINDING_NAMESPACE, query))
        rows.extend(page)
        if len(rows) > CATALOG_READ_CEILING:
            beyond = rows[CATALOG_READ_CEILING]
            del rows[CATALOG_READ_CEILING:]
            truncated_at = (str(beyond.get("runtime")), str(beyond.get("model")), str(beyond.get("effort")))
            break
        if len(page) < CATALOG_PAGE_SIZE:
            break
        last = page[-1]
        after = [last["runtime"], last["model"], last["effort"], last["id"]]
    measured: dict[_CellKey, datetime] = {}
    windows: list[int] = []
    for row in rows:
        key = (str(row.get("runtime")), str(row.get("model")), str(row.get("effort")))
        measured[key] = _parse_stamp(row.get("measured_at"), "measured_at")
        window_hours = _known_window(row.get("staleness_window_hours"), key)
        if window_hours is not None:
            windows.append(window_hours)
    return CatalogReading(measured, timedelta(hours=min(windows)) if windows else None, truncated_at)


def _count(data: dict[str, Any], field: str) -> int:
    value = data.get(field)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"refresh result field {field!r} is not a non-negative integer: {value!r}")
    return value


def _failure_text(result: dict[str, Any]) -> str:
    error = result.get("error")
    if isinstance(error, dict):
        return f"{error.get('code')}: {error.get('message')}"
    return f"action_status={result.get('action_status')!r} error={error!r}"


def parse_refresh_result(result: object) -> RefreshOutcome:
    """The refresh envelope as an outcome; anything unreadable is a failure."""
    if not isinstance(result, dict):
        return RefreshOutcome(failure=f"unexpected refresh response shape: {type(result).__name__}")
    if result.get("action_status") != "completed":
        return RefreshOutcome(failure=_failure_text(result))
    data = result.get("data")
    if not isinstance(data, dict):
        return RefreshOutcome(failure="refresh result carried no data object")
    try:
        failed = data.get("sources_failed")
        if not isinstance(failed, list):
            raise ValueError(f"refresh result field 'sources_failed' is not a list: {failed!r}")
        return RefreshOutcome(
            failure=None,
            readings_gathered=_count(data, "readings_gathered"),
            cells_accepted=_count(data, "cells_accepted"),
            cells_conflicted=_count(data, "cells_conflicted"),
            cells_unchanged=_count(data, "cells_unchanged"),
            sources_failed=tuple(str(item) for item in failed),
        )
    except ValueError as exc:
        return RefreshOutcome(failure=f"unreadable refresh result: {exc}")


def run_refresh(orchestrator: object, *, state: dict[str, Any]) -> RefreshOutcome:
    """Call the catalog refresh once. The messaging plugin is resolved first
    and its absence raises; a refresh that raises or reports failure comes
    back as ``RefreshOutcome.failure`` (logged at ERROR with the traceback) so
    the caller alerts on it instead of losing it."""
    plugin = _resolve_agent_messaging_plugin(orchestrator)
    method = plugin.refresh_model_capability_catalog_sync  # type: ignore[attr-defined]
    try:
        result = method(params={"trigger": REFRESH_TRIGGER}, state=state)
    except Exception as exc:  # noqa: BLE001 -- turned into a critical alert and logged with the traceback, never dropped
        logger.exception("model_capability_guard: refresh_model_capability_catalog raised")
        return RefreshOutcome(failure=f"{type(exc).__name__}: {exc}")
    outcome = parse_refresh_result(result)
    if outcome.failure is not None:
        logger.error("model_capability_guard: refresh failed: %s", outcome.failure)
    return outcome


def _renewed(before: CatalogReading, after: CatalogReading, outcome: RefreshOutcome) -> bool:
    if outcome.cells_accepted <= 0:
        return False
    return any(stamp > before.measured_at.get(key, datetime.min.replace(tzinfo=UTC)) for key, stamp in after.measured_at.items())


def _failure_reasons(outcome: RefreshOutcome) -> list[str]:
    if outcome.failure is not None:
        return [f"refresh failed: {outcome.failure}"]
    if outcome.sources_failed:
        return [f"{len(outcome.sources_failed)} source fetch(es) failed"]
    return []


def _age_finding(age: timedelta | None, window: timedelta | None, warning_age: timedelta) -> tuple[str | None, str | None]:
    """``(critical, warning)`` reasons for the oldest accepted cell's age."""
    if age is None:
        return "no accepted cell in the catalog", None
    if window is None:
        return None, "accepted catalog freshness window is unknown"
    if age > window:
        return f"oldest accepted cell is past the {_hours(window)} staleness window", None
    threshold = min(warning_age, window)
    if age > threshold:
        return None, f"oldest accepted cell is older than {_hours(threshold)}"
    return None, None


def _truncation_reason(key: _CellKey) -> str:
    """The loud notice for a catalog read past the ceiling: the age check then covers only the rows read."""
    return f"catalog read truncated at {CATALOG_READ_CEILING} accepted cells; the first cell not considered is {'/'.join(key)}, so the age check covers only the cells read"


def assess(
    outcome: RefreshOutcome,
    before: CatalogReading,
    after: CatalogReading,
    *,
    now: datetime,
    warning_age: timedelta,
) -> Assessment:
    """Pure classifier over one run and the catalog before/after it."""
    renewed = _renewed(before, after, outcome)
    oldest = after.oldest()
    age = None if oldest is None else now - oldest
    age_critical, age_warning = _age_finding(age, after.window, warning_age)
    critical = _failure_reasons(outcome) + ([age_critical] if age_critical else [])
    truncation = [_truncation_reason(after.truncated_at)] if after.truncated_at is not None else []
    warning = ([age_warning] if age_warning else []) + (["the run renewed no accepted cell"] if outcome.failure is None and not renewed else []) + truncation
    if critical:
        return Assessment(STATUS_CRITICAL, tuple(critical + warning), age, after.window, renewed)
    if warning:
        return Assessment(STATUS_WARNING, tuple(warning), age, after.window, renewed)
    return Assessment(STATUS_OK, (), age, after.window, renewed)


def _hours(delta: timedelta | None) -> str:
    return "unknown" if delta is None else f"{delta.total_seconds() / 3600:.1f}h"


def _failed_sources_text(sources_failed: tuple[str, ...]) -> str:
    """The failed sources, capped: a total outage lists every roster cell."""
    if not sources_failed:
        return "none"
    shown = "; ".join(sources_failed[:MAX_FAILED_SOURCES_IN_ALERT])
    hidden = len(sources_failed) - MAX_FAILED_SOURCES_IN_ALERT
    return f"{len(sources_failed)} failed, e.g. {shown}" + (f"; (+{hidden} more)" if hidden > 0 else "")


def build_alert_text(*, assessment: Assessment, outcome: RefreshOutcome, accepted_cells: int) -> str:
    counts = f"readings_gathered={outcome.readings_gathered} cells_accepted={outcome.cells_accepted} cells_conflicted={outcome.cells_conflicted} cells_unchanged={outcome.cells_unchanged}"
    if assessment.status == STATUS_OK:
        return f"MODEL CAPABILITY CATALOG OK (recovered): the daily refresh is renewing readings again. {counts}; {accepted_cells} accepted cell(s), oldest {_hours(assessment.oldest_age)} (window {_hours(assessment.window)})."
    failed = _failed_sources_text(outcome.sources_failed)
    return (
        f"MODEL CAPABILITY CATALOG {assessment.status.upper()}: {'; '.join(assessment.reasons)}. {counts}. "
        f"Oldest accepted cell {_hours(assessment.oldest_age)} old (window {_hours(assessment.window)}), "
        f"{accepted_cells} accepted cell(s). Failed sources: {failed}. "
        "The selector still works from the last accepted readings; nothing is blocked."
    )


def _read_last_notified(orchestrator: object) -> str:
    data = require_completed(
        _typed_state(orchestrator).get_key_value(STATE_NAMESPACE, _LAST_NOTIFIED_KEY),
        "model_capability_guard get last-notified status",
    )
    value = data.get("value") if data.get("found") else None
    return str(value) if value in (STATUS_OK, STATUS_WARNING, STATUS_CRITICAL) else STATUS_OK


def _write_last_notified(orchestrator: object, status: str) -> None:
    require_completed(
        _typed_state(orchestrator).set_key_value(STATE_NAMESPACE, _LAST_NOTIFIED_KEY, status),
        "model_capability_guard set last-notified status",
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


def run_model_capability_guard(
    orchestrator: object,
    *,
    config: ModelCapabilityGuardConfig | None = None,
    state: dict[str, Any],
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Refresh the catalog, classify it, and on a severity TRANSITION alert
    ``config.alert_role`` — at most once per transition, never once per tick.

    ``orchestrator`` needs ``get_service("state_service")`` (catalog reads and
    the durable marker) and ``plugin_manager.get_plugin`` (the refresh and the
    alert). ``state`` is the calling verb's own, handed through unchanged.
    """
    cfg = config or ModelCapabilityGuardConfig()
    before = read_accepted_catalog(orchestrator)
    outcome = run_refresh(orchestrator, state=state)
    after = read_accepted_catalog(orchestrator)
    assessment = assess(outcome, before, after, now=clock(), warning_age=cfg.warning_age)
    last_notified = _read_last_notified(orchestrator)
    transitioned = assessment.status != last_notified
    alerted = False
    alert_reason: str | None = None
    if transitioned and not cfg.alert_role:
        alert_reason = REASON_ALERT_TARGET_UNCONFIGURED
        logger.error(
            "model_capability_guard: %s -> %s transition has no alert_role configured (platform_health_plugin config key 'alert_role' is unset); nobody was told; marker left at %r so this is retried every tick",
            last_notified,
            assessment.status,
            last_notified,
        )
    elif transitioned:
        assert cfg.alert_role  # excluded by the branch above; narrows the type
        text = build_alert_text(assessment=assessment, outcome=outcome, accepted_cells=len(after.measured_at))
        delivered, reason = _delivery_outcome(_send_peer_alert(orchestrator, name=cfg.alert_role, content=text, state=state))
        if delivered:
            alerted = True
            _write_last_notified(orchestrator, assessment.status)
        else:
            alert_reason = reason
            logger.error(
                "model_capability_guard: %s -> %s alert to role %r NOT delivered to a live holder (%s); marker left at %r so the next tick retries",
                last_notified,
                assessment.status,
                cfg.alert_role,
                reason,
                last_notified,
            )
    return {
        "status": assessment.status,
        "reasons": list(assessment.reasons),
        "refresh_failure": outcome.failure,
        "readings_gathered": outcome.readings_gathered,
        "cells_accepted": outcome.cells_accepted,
        "cells_conflicted": outcome.cells_conflicted,
        "cells_unchanged": outcome.cells_unchanged,
        "sources_failed": list(outcome.sources_failed),
        "renewed": assessment.renewed,
        "accepted_cell_count": len(after.measured_at),
        "oldest_cell_age_hours": None if assessment.oldest_age is None else assessment.oldest_age.total_seconds() / 3600,
        "staleness_window_hours": None if assessment.window is None else assessment.window.total_seconds() / 3600,
        "transitioned": transitioned,
        "alerted": alerted,
        "alert_role": cfg.alert_role if transitioned else None,
        "alert_reason": alert_reason,
    }


def ensure_model_capability_guard_schedule(
    scheduling_service: object,
    *,
    cron_expression: str = DEFAULT_SCHEDULE_CRON,
    tag: str = DEFAULT_SCHEDULE_TAG,
    label: str = DEFAULT_SCHEDULE_LABEL,
) -> dict[str, Any]:
    """Idempotently ensure the daily guard cron exists on the platform's own
    scheduler: a schedule already carrying ``tag`` is left alone, otherwise one
    is created. ``scheduling_service`` is the SERVICE binding resolved by the
    caller at call time (see ``host_disk_guard`` for why). A scheduling error
    response raises rather than being read as "no existing schedule".
    """
    schedule_state = {"flow_id": _GUARD_FLOW_ID, "session_id": _GUARD_SESSION_ID}
    existing = _unwrap_response(
        scheduling_service.get_schedules_by_tag(tag=tag, state=schedule_state),  # type: ignore[attr-defined]
        action="get_schedules_by_tag",
    )
    if existing.get("schedules"):
        return {"status": "already_present", "tag": tag}
    # LITERAL actions list and process_key: whole-tree gate C5.1 grants the
    # EDGE_SINK exemption only to a literal it can resolve. No
    # result_processor_kind key -- omitting it is the EDGE_SINK-canonical shape.
    created = _unwrap_response(
        scheduling_service.create_cron_schedule(  # type: ignore[attr-defined]
            cron_expression=cron_expression,
            actions=[
                {
                    "process_key": "plugin::platform_health_plugin::check_model_capability_catalog_cron",
                    "arguments": {},
                }
            ],
            label=label,
            tags=[tag],
            state=schedule_state,
        ),
        action="create_cron_schedule",
    )
    return {
        "status": "created",
        "tag": tag,
        "cron_expression": cron_expression,
        "schedule_id": created.get("schedule_id"),
    }
