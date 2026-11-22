"""Daily model capability catalog refresh; a severity change is recorded in state and logged.

Problem ``wgr_96cceb7fb786427ea6fb20603a5ff4f2``; issues ``iss_4bfe859f``
(nothing schedules the refresh and nothing reports when it fails),
``iss_ec0f23d0`` (a run that gathered readings but refreshed no cell reports
success) and ``iss_63146325`` (every cell expires at one instant and no read
shows when). Operator ruling ``rul_becb4bed``: refresh at appropriate times
and say loudly when refreshing is not working; never block progress on stale
data. The selector keeps serving the last accepted readings; this module is
what makes those readings renew. Zero readings or a failed leaderboard
source prompt a durable advisory to the local Main role once per UTC day.

The guard uses shared marker and scheduling helpers:

* :func:`run_model_capability_guard` — the cron-fired check. It runs the
  refresh (``plugin::agent_messaging_plugin::refresh_model_capability_catalog``
  with ``trigger="cron"``), then classifies the result and the catalog as
  ``ok`` / ``warning`` / ``critical``. On a severity TRANSITION only
  (including the recovery back to ``ok``), never once per tick, it records the
  new status in the marker and logs one line: at ERROR for critical, WARNING
  for warning, INFO for ok.
  ``agent_messaging_plugin`` is a plugin, so it is resolved through
  ``orchestrator.plugin_manager.get_plugin`` and its public
  worker-only synchronous refresh implementation is called as ``method(params=..., state=...)``; dispatched checks enqueue their complete guard pass.
  Resolution failure raises: a guard that cannot reach the refresh must not
  report ``ok``.
* :func:`ensure_model_capability_guard_schedule` — idempotent install of the
  daily cron on the platform's own scheduler, found again by tag.

**Severity.** ``critical`` when the refresh raised or returned a failure
envelope, recorded any failed source, or a run that did not renew an accepted
cell leaves the oldest accepted cell older than the staleness window the cells
themselves carry (``staleness_window_hours``; the tightest known one). Null
cell windows are unknown and do not contribute to cadence; an all-null catalog
has no known cadence. ``warning`` when the run renewed no accepted cell, or a
run without successful renewal leaves the oldest accepted cell older than
``warning_age`` (48 hours, capped at the window). A successful renewal makes
the current run healthy even when historical cells remain unrenewable; those
cells are reported as a stale advisory with their stored timestamps intact.
The window is read from the catalog, never hard-coded here.

**What "renewed" means.** ``cells_unchanged`` in the run counts roster cells
that got a ``pending_crosscheck`` outcome — insufficient evidence — and is
therefore non-zero precisely when a run gathered too little to refresh
anything. A rule of "accepted plus unchanged is zero" would almost never fire,
so renewal is measured directly: a run renewed the catalog only if at least
one accepted cell's ``measured_at`` is newer after the run than before it.

**Configuration.** None: the guard needs no target and no role, so
:func:`ensure_model_capability_guard_schedule` installs the cron on any
deployment.

The marker (``StateManagementInterface`` key-value, no new table) holds the
last recorded status and is written in the same pass that logs the transition,
so a transition is logged exactly once. A session reads the current status from
the marker or from the check verb's result.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import TYPE_CHECKING, Any, Final, cast

from ananta.core.config.environment_config import EnvironmentConfig
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE
from ananta.llm.agent_messaging.state_results import require_completed, require_records

from platform_health_plugin.guard_support import (
    STATE_NAMESPACE,
    STATUS_CRITICAL,
    STATUS_OK,
    STATUS_WARNING,
    _get_state_service,
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

# System-owned flow_id/session_id for the cron's own schedule row.
# ActionFactory requires a flow_id at fire time.
_GUARD_FLOW_ID: Final[str] = "flow-model-capability-guard"
_GUARD_SESSION_ID: Final[str] = "sess-model-capability-guard"

_LAST_STATUS_KEY: Final[str] = "model_capability_guard:last_status"
_LAST_NOTICE_DAY_KEY: Final[str] = "model_capability_guard:last_notice_day"
_NOTICE_LOCK = Lock()

# agent_messaging_plugin owns these tables (schema.TABLE_MODEL_CAPABILITY_CELL
# and CATALOG_PAGE_SIZE / CATALOG_READ_CEILING in model_capability_store); this
# plugin does not depend on it, so the smoke pins these literals to the owner's.
TABLE_MODEL_CAPABILITY_CELL: Final[str] = "model_capability_cell"
SOURCE_LEADERBOARD_HTML: Final[str] = "artificial_analysis_leaderboard_html"
CELL_ACCEPTANCE_ACCEPTED: Final[str] = "accepted"
CATALOG_PAGE_SIZE: Final[int] = 100
CATALOG_READ_CEILING: Final[int] = 1000
MAX_FAILED_SOURCES_IN_TEXT: Final[int] = 3

_CellKey = tuple[str, str, str]


@dataclass(frozen=True)
class ModelCapabilityGuardConfig:
    """The guard's one tunable: how old the oldest accepted cell may be before the status is ``warning``."""

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
    run_id: str | None = None


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
    historical_unrenewed_cells: int


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
    run_id = data.get("run_id")
    run_id = run_id if isinstance(run_id, str) and run_id else None
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
            run_id=run_id,
        )
    except ValueError as exc:
        return RefreshOutcome(failure=f"unreadable refresh result: {exc}", run_id=run_id)


def _resolve_agent_messaging_plugin(orchestrator: object) -> object:
    """The ``agent_messaging_plugin`` instance, via structural typing.

    ``agent_messaging_plugin`` is deliberately NOT a bound service (see
    ``AgentMessagingServiceInterface``'s own module docstring — binding it
    would hide its ``plugin::*::*`` EDGE processes from the process
    registry), so it is consumed through
    ``orchestrator.plugin_manager.get_plugin(...)``. Fails fast if the
    orchestrator or the plugin is unavailable — never a silent ``None``
    swallowed into "ok".
    """
    plugin_manager = getattr(orchestrator, "plugin_manager", None)
    if plugin_manager is None:
        raise RuntimeError("model_capability_guard: orchestrator has no plugin_manager; cannot reach agent_messaging_plugin to refresh the catalog")
    plugin = plugin_manager.get_plugin("agent_messaging_plugin")
    if plugin is None:
        raise RuntimeError("model_capability_guard: agent_messaging_plugin not loaded; cannot refresh the catalog")
    return plugin


def run_refresh(orchestrator: object, *, state: dict[str, Any]) -> RefreshOutcome:
    """Call the catalog refresh once. The messaging plugin is resolved first
    and its absence raises; a refresh that raises or reports failure comes
    back as ``RefreshOutcome.failure`` (logged at ERROR with the traceback) so
    the caller classifies it critical instead of losing it."""
    plugin = _resolve_agent_messaging_plugin(orchestrator)
    method = plugin.refresh_model_capability_catalog_sync  # type: ignore[attr-defined]
    try:
        result = method(params={"trigger": REFRESH_TRIGGER}, state=state)
    except Exception as exc:  # noqa: BLE001 -- turned into a critical status and logged with the traceback, never dropped
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


def _historical_unrenewed_count(before: CatalogReading, after: CatalogReading) -> int:
    """Count accepted cells whose stored measurement did not advance this run."""
    return sum(1 for key, stamp in before.measured_at.items() if after.measured_at.get(key, datetime.min.replace(tzinfo=UTC)) <= stamp)


def _historical_unrenewed_reason(count: int) -> str | None:
    if count == 0:
        return None
    return f"{count} historical accepted cell(s) were not renewed; their measured_at timestamps remain unchanged and selector staleness remains visible"


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


def _age_reasons(
    *,
    renewed: bool,
    failures: list[str],
    age: timedelta | None,
    window: timedelta | None,
    warning_age: timedelta,
) -> tuple[list[str], list[str]]:
    if renewed and not failures:
        return [], []
    age_critical, age_warning = _age_finding(age, window, warning_age)
    critical = [age_critical] if age_critical else []
    warning = [age_warning] if age_warning else []
    return critical, warning


def _warning_reasons(
    *,
    outcome: RefreshOutcome,
    renewed: bool,
    age_warning: list[str],
    truncated_at: _CellKey | None,
) -> list[str]:
    warning = list(age_warning)
    if outcome.failure is None and not renewed:
        warning.append("the run renewed no accepted cell")
    if truncated_at is not None:
        warning.append(_truncation_reason(truncated_at))
    return warning


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
    historical_unrenewed_cells = _historical_unrenewed_count(before, after)
    oldest = after.oldest()
    age = None if oldest is None else now - oldest
    failures = _failure_reasons(outcome)
    # A successful renewal is the health signal for this run. Age findings
    # still classify a failed or non-renewing run, while historical cells that
    # could not renew remain visible as an advisory below.
    age_critical, age_warning = _age_reasons(
        renewed=renewed,
        failures=failures,
        age=age,
        window=after.window,
        warning_age=warning_age,
    )
    critical = failures + age_critical
    warning = _warning_reasons(
        outcome=outcome,
        renewed=renewed,
        age_warning=age_warning,
        truncated_at=after.truncated_at,
    )
    historical_reason = _historical_unrenewed_reason(historical_unrenewed_cells)
    reasons = critical + warning + ([historical_reason] if historical_reason else [])
    if critical:
        return Assessment(STATUS_CRITICAL, tuple(reasons), age, after.window, renewed, historical_unrenewed_cells)
    if warning:
        return Assessment(STATUS_WARNING, tuple(reasons), age, after.window, renewed, historical_unrenewed_cells)
    return Assessment(STATUS_OK, tuple(reasons), age, after.window, renewed, historical_unrenewed_cells)


def _hours(delta: timedelta | None) -> str:
    return "unknown" if delta is None else f"{delta.total_seconds() / 3600:.1f}h"


def _failed_sources_text(sources_failed: tuple[str, ...]) -> str:
    """The failed sources, capped: a total outage lists every roster cell."""
    if not sources_failed:
        return "none"
    shown = "; ".join(sources_failed[:MAX_FAILED_SOURCES_IN_TEXT])
    hidden = len(sources_failed) - MAX_FAILED_SOURCES_IN_TEXT
    return f"{len(sources_failed)} failed, e.g. {shown}" + (f"; (+{hidden} more)" if hidden > 0 else "")


def build_transition_text(*, assessment: Assessment, outcome: RefreshOutcome, accepted_cells: int) -> str:
    counts = f"readings_gathered={outcome.readings_gathered} cells_accepted={outcome.cells_accepted} cells_conflicted={outcome.cells_conflicted} cells_unchanged={outcome.cells_unchanged}"
    historical = _historical_unrenewed_reason(assessment.historical_unrenewed_cells)
    if assessment.status == STATUS_OK:
        suffix = f" {historical}." if historical else ""
        return f"MODEL CAPABILITY CATALOG OK (recovered): the daily refresh is renewing readings again. {counts}; {accepted_cells} accepted cell(s), oldest {_hours(assessment.oldest_age)} (window {_hours(assessment.window)}).{suffix}"
    failed = _failed_sources_text(outcome.sources_failed)
    return (
        f"MODEL CAPABILITY CATALOG {assessment.status.upper()}: {'; '.join(assessment.reasons)}. {counts}. "
        f"Oldest accepted cell {_hours(assessment.oldest_age)} old (window {_hours(assessment.window)}), "
        f"{accepted_cells} accepted cell(s). Failed sources: {failed}. "
        "The selector still works from the last accepted readings; nothing is blocked."
    )


def _read_last_status(orchestrator: object) -> str:
    data = require_completed(
        _typed_state(orchestrator).get_key_value(STATE_NAMESPACE, _LAST_STATUS_KEY),
        "model_capability_guard get last status",
    )
    value = data.get("value") if data.get("found") else None
    return str(value) if value in (STATUS_OK, STATUS_WARNING, STATUS_CRITICAL) else STATUS_OK


def _write_last_status(orchestrator: object, status: str) -> None:
    require_completed(
        _typed_state(orchestrator).set_key_value(STATE_NAMESPACE, _LAST_STATUS_KEY, status),
        "model_capability_guard set last status",
    )


def _notice_reason(outcome: RefreshOutcome) -> str | None:
    for failure in outcome.sources_failed:
        if failure.startswith(f"{SOURCE_LEADERBOARD_HTML}:"):
            return failure
    if outcome.readings_gathered == 0:
        return outcome.failure or "refresh gathered zero readings"
    return None


def _send_daily_notice(
    orchestrator: object,
    *,
    state: dict[str, Any],
    day: str,
    outcome: RefreshOutcome,
    transition_text: str,
) -> str | None:
    reason = _notice_reason(outcome)
    if reason is None:
        return None
    content = f"Model capability catalog advisory for {day} UTC, refresh run {outcome.run_id or 'unavailable'}. {reason}. {transition_text}"
    # Manual checks and the cron can overlap within the active process. Keep
    # the durable date read, send and write together for those callers.
    with _NOTICE_LOCK:
        try:
            service = _typed_state(orchestrator)
            marker = require_completed(service.get_key_value(STATE_NAMESPACE, _LAST_NOTICE_DAY_KEY), "read catalog notice day")
            last_day = marker.get("value") if marker.get("found") else None
            if isinstance(last_day, str) and last_day >= day:
                return None
            role = f"{EnvironmentConfig.solet_name().title()}-Main"
            plugin = _resolve_agent_messaging_plugin(orchestrator)
            response = plugin.peer_send_by_name(params={"name": role, "content": content}, state=state)  # type: ignore[attr-defined]
            _unwrap_response(response, action="catalog notice peer_send_by_name")
            require_completed(service.set_key_value(STATE_NAMESPACE, _LAST_NOTICE_DAY_KEY, day), "record catalog notice day")
        except Exception as exc:  # noqa: BLE001 -- notification errors are advisory so the assessment stays visible
            advisory = f"model capability catalog notice for {day} UTC to the local Main role: {exc}"
            logger.warning("%s", advisory, exc_info=True)
            return advisory
    return None


_TRANSITION_LOG_LEVEL: Final[dict[str, int]] = {
    STATUS_CRITICAL: logging.ERROR,
    STATUS_WARNING: logging.WARNING,
    STATUS_OK: logging.INFO,
}


def _utc_now() -> datetime:
    return datetime.now(UTC)


def run_model_capability_guard(
    orchestrator: object,
    *,
    config: ModelCapabilityGuardConfig | None = None,
    state: dict[str, Any],
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, Any]:
    """Refresh and classify the catalog, recording and logging severity
    changes. Notify the local Main role of zero readings or a failed leaderboard once
    per UTC day, independently of severity transitions.

    ``orchestrator`` needs ``get_service("state_service")`` (catalog reads and
    the durable marker) and ``plugin_manager.get_plugin`` (the refresh).
    ``state`` is the calling verb's own, handed through unchanged.
    """
    cfg = config or ModelCapabilityGuardConfig()
    before = read_accepted_catalog(orchestrator)
    outcome = run_refresh(orchestrator, state=state)
    after = read_accepted_catalog(orchestrator)
    now = clock()
    assessment = assess(outcome, before, after, now=now, warning_age=cfg.warning_age)
    last_status = _read_last_status(orchestrator)
    transitioned = assessment.status != last_status
    transition_text = build_transition_text(assessment=assessment, outcome=outcome, accepted_cells=len(after.measured_at))
    if transitioned:
        _write_last_status(orchestrator, assessment.status)
        logger.log(_TRANSITION_LOG_LEVEL[assessment.status], "%s", transition_text)
    notice_advisory = _send_daily_notice(orchestrator, state=state, day=now.astimezone(UTC).date().isoformat(), outcome=outcome, transition_text=transition_text)
    return {
        "status": assessment.status,
        "reasons": list(assessment.reasons),
        "advisories": [] if notice_advisory is None else [notice_advisory],
        "refresh_failure": outcome.failure,
        "readings_gathered": outcome.readings_gathered,
        "cells_accepted": outcome.cells_accepted,
        "cells_conflicted": outcome.cells_conflicted,
        "cells_unchanged": outcome.cells_unchanged,
        "sources_failed": list(outcome.sources_failed),
        "renewed": assessment.renewed,
        "historical_unrenewed_cell_count": assessment.historical_unrenewed_cells,
        "accepted_cell_count": len(after.measured_at),
        "oldest_cell_age_hours": None if assessment.oldest_age is None else assessment.oldest_age.total_seconds() / 3600,
        "staleness_window_hours": None if assessment.window is None else assessment.window.total_seconds() / 3600,
        "transitioned": transitioned,
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
    caller at call time. A scheduling error
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
