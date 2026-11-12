"""Code-only seat heartbeat; unchanged complete observations never send mail.

The register CLI remains the register owner. Marker history uses the existing
state-service KV namespace. An intent is persisted before sending and is never
replayed, including after a crash between the effect and its acknowledgement.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

from ananta.interfaces.state_management_interface import StateManagementInterface
from ananta.llm.agent_messaging.state_results import require_completed, require_records

from platform_health_plugin.host_disk_guard import (
    STATE_NAMESPACE,
    _get_state_service,
    _resolve_agent_messaging_plugin,
    _send_peer_alert,
    _unwrap_response,
)

logger = logging.getLogger(__name__)


def _integer_setting(name: str, default: int, *, maximum: int | None = None) -> int:
    """Unknown operator configuration is advisory; keep the documented default."""
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        value = int(raw)
        if value < 1:
            raise ValueError("must be positive")
        if maximum is not None and value > maximum:
            raise ValueError(f"must not exceed {maximum}")
        return value
    except ValueError as exc:
        logger.warning("seat heartbeat setting %s=%r: %s; using default %s", name, raw, exc, default)
        return default


CHECK_KEY = "plugin::platform_health_plugin::check_main_seat_heartbeat_cron"
ENSURE_NAME = "ensure_main_seat_heartbeat_schedule"
CRON = "7,27,47 * * * *"
TAG = "heartbeat:main-seat"
MARKER_KEY = "main_seat_heartbeat:current"
PAGE_SIZE = _integer_setting("MAIN_SEAT_HEARTBEAT_PAGE_SIZE", 100, maximum=100)
MAX_PAGES = _integer_setting("MAIN_SEAT_HEARTBEAT_MAX_PAGES", 100)
MAX_TOKENS = _integer_setting("MAIN_SEAT_HEARTBEAT_MAX_TOKENS", 10000)
READ_TIMEOUT_SECONDS = _integer_setting("MAIN_SEAT_HEARTBEAT_READ_TIMEOUT_SECONDS", 60)
RegisterRead = Callable[[tuple[str, ...]], object]


def register_read(arguments: tuple[str, ...]) -> object:
    """Invoke the existing read-only CLI, without a shell or database adapter."""
    cli = os.environ.get("PROJECT_SOLET_CLI") or str(Path.home() / "Workspace/project-solet/.venv/bin/psolet")
    result = subprocess.run((cli, *arguments), capture_output=True, text=True, timeout=READ_TIMEOUT_SECONDS, check=False)
    if result.returncode:
        raise RuntimeError(f"psolet {' '.join(arguments[:3])}: exit {result.returncode}: {(result.stderr or result.stdout)[:1000]}")
    return json.loads(result.stdout)


def _object(value: object) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("read returned no object receipt")
    return cast("dict[str, Any]", value)


def _rows(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("read returned no row list")
    return cast("list[dict[str, Any]]", value)


def _cursor_page(read: RegisterRead, argv: tuple[str, ...], records: str, cursor: str | None) -> tuple[list[dict[str, Any]], bool, str | None]:
    receipt = _object(read(argv))
    page = _rows(receipt.get(records))
    if receipt.get("returned_count") != len(page) or not isinstance(receipt.get("has_more"), bool):
        raise ValueError("page lacks complete count/has_more receipt")
    more = receipt["has_more"]
    next_cursor = receipt.get("next_cursor")
    if more and (not isinstance(next_cursor, str) or not next_cursor or next_cursor == cursor):
        raise ValueError("page lacks advancing continuation")
    return page, more, next_cursor


def _pages(read: RegisterRead, argv: tuple[str, ...], *, records: str | None = None) -> list[dict[str, Any]]:
    """Cursor receipts for ownership/events; CLI offset lists end at a short page."""
    rows: list[dict[str, Any]] = []
    cursor: str | None = None
    for page_number in range(MAX_PAGES):
        suffix = ("--limit", str(PAGE_SIZE))
        if records is not None:
            suffix += ("--after", cursor) if cursor is not None else ()
            page, more, cursor = _cursor_page(read, argv + suffix, records, cursor)
        else:
            page = _rows(read(argv + suffix + ("--offset", str(page_number * PAGE_SIZE))))
            more = len(page) == PAGE_SIZE
        rows.extend(page)
        if not more:
            return rows
    raise ValueError(f"read exceeded {MAX_PAGES} pages; domain unmeasured")


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _state(orchestrator: object) -> StateManagementInterface:
    return cast("StateManagementInterface", _get_state_service(orchestrator))


def read_marker(orchestrator: object) -> dict[str, Any]:
    receipt = require_completed(_state(orchestrator).get_key_value(STATE_NAMESPACE, MARKER_KEY), "heartbeat read marker")
    return _object(receipt["value"]) if receipt.get("found") else {}


def _save(orchestrator: object, marker: dict[str, Any]) -> None:
    """Retain each version before moving the current projection; no history edits."""
    state = _state(orchestrator)
    marker["record_id"] = str(uuid.uuid4())
    history_key = f"main_seat_heartbeat:history:{marker['record_id']}"
    require_completed(state.set_key_value(STATE_NAMESPACE, history_key, dict(marker)), "heartbeat append marker history")
    require_completed(state.set_key_value(STATE_NAMESPACE, MARKER_KEY, dict(marker)), "heartbeat update marker")


def maintain_marker(orchestrator: object, params: dict[str, Any]) -> dict[str, Any]:
    """Read history by record id; correction/clear preserve uncertain send custody.

    Caller must own the current projection (expected_record_id). Operations are
    serialized by the plugin's single-flight lock. A clear is a tombstone with
    a fresh baseline on the next tick; immutable history remains readable.
    """
    current = read_marker(orchestrator)
    operation = params.get("operation", "read")
    if operation == "read":
        record_id = params.get("record_id")
        if record_id:
            receipt = require_completed(_state(orchestrator).get_key_value(STATE_NAMESPACE, f"main_seat_heartbeat:history:{record_id}"), "heartbeat history read")
            return {"marker": receipt.get("value"), "found": receipt.get("found")}
        return {"marker": current, "found": bool(current)}
    if operation not in ("correct", "clear") or not params.get("reason"):
        raise ValueError("marker correction/clear requires an operation and reason")
    if current.get("record_id") != params.get("expected_record_id"):
        raise ValueError("stale marker record; read the current record before correcting")
    updated = dict(current)
    updated["previous_record_id"] = current.get("record_id")
    updated["correction_reason"] = str(params["reason"])
    # The observed baseline may be cleared/corrected; send intent is immutable.
    updated["tokens"] = [] if operation == "clear" else sorted(set(_rows_as_strings(params.get("tokens"))))
    updated["baseline_reset"] = True
    updated["retired"] = operation == "clear"
    _save(orchestrator, updated)
    return {"marker": updated}


def _rows_as_strings(value: object) -> list[str]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ValueError("tokens must be a list of strings")
    return cast("list[str]", value)


def _attempt(advisories: list[str], label: str, read: Callable[[], Any], default: Any) -> Any:
    try:
        return read()
    except Exception as exc:  # noqa: BLE001 -- an explicit unmeasured domain, never quiet
        advisories.append(f"{label}: {type(exc).__name__}: {exc}")
        return default


def _resume_complete(value: object) -> bool:
    receipt = _object(value)
    # Current CLI resume has per-section completeness; a digest is not a full read.
    if receipt.get("complete") is True:
        return True
    sections = receipt.get("sections")
    return isinstance(sections, dict) and bool(sections) and all(isinstance(section, dict) and section.get("complete") is True for section in sections.values())


def _unit_artifacts(read: RegisterRead, unit: str, advisories: list[str], tokens: set[str], actionable: set[str]) -> None:
    candidates = _attempt(advisories, f"candidates {unit}", lambda unit=unit: _pages(read, ("db", "candidate-revision", "list", "--unit", unit)), [])
    for row in candidates:
        token = f"candidate:{unit}:{row['revision_id']}:{row['state']}:{row.get('history_version')}"
        tokens.add(token)
        if row["state"] == "delivered":
            actionable.add(token)
    verdicts = _attempt(advisories, f"verdicts {unit}", lambda unit=unit: _rows(read(("db", "verdict", "list", "--unit", unit))), [])
    for row in verdicts:
        tokens.add(f"verdict:{unit}:{row['verdict_id']}:{row.get('passed')}")
    events = _attempt(advisories, f"events {unit}", lambda unit=unit: _pages(read, ("db", "unit", "events", unit), records="records"), [])
    _event_tokens(unit, events, tokens, actionable)


def _event_tokens(unit: str, events: list[dict[str, Any]], tokens: set[str], actionable: set[str]) -> None:
    for row in events:
        if row.get("withdrawn_by_event_id") or row.get("corrected_by_event_id"):
            continue
        token = f"event:{unit}:{row['event_id']}"
        tokens.add(token)
        # This is an explicit author label, not a guess at intent from prose.
        detail = str(row.get("effective_detail", ""))
        if detail.startswith(("STOP:", "STOP ", "BLOCKED:")):
            actionable.add(token)


def _retained_artifacts(previous: dict[str, Any], tracked: set[str]) -> set[str]:
    retained: set[str] = set()
    for token in previous.get("tokens", []):
        parts = str(token).split(":", 2)
        if len(parts) >= 2 and parts[0] in ("candidate", "verdict", "event") and parts[1] not in tracked:
            retained.add(str(token))
    return retained


def _fleet_read(messaging: Any, advisories: list[str], actionable: set[str]) -> dict[str, Any]:
    fleet = _attempt(advisories, "fleet", lambda: _unwrap_response(messaging.fleet_status(params={"scope": "lanes"}, state={}), action="fleet_status"), {})
    if fleet.get("sessions_truncated") != 0:
        advisories.append("fleet sessions incomplete; overdue domain unmeasured")
    for row in fleet.get("sessions", []):
        if row.get("classification") == "STALLED" or row.get("ledger_state") == "overdue":
            actionable.add(f"overdue-lane:{row['agent_instance_id']}:{row.get('lane_id')}")
    peers = _attempt(advisories, "peer liveness", lambda: _unwrap_response(messaging.peer_list(params={}, state={}), action="peer_list"), {})
    if not isinstance(peers.get("instances"), dict):
        advisories.append("peer_list lacks complete instances snapshot")
    return fleet


def _unit_state_tokens(read: RegisterRead, units: list[dict[str, Any]], owned: list[dict[str, Any]], previous: dict[str, Any], advisories: list[str], tokens: set[str], actionable: set[str]) -> None:
    for row in units:
        tokens.add(f"unit:{row['unit_id']}:{row['state']}:{row['state_observed_at']}")
    tracked = {str(row["unit_id"]) for row in owned}
    # Track formerly owned Units for one further pass to capture their terminal
    # review/delivery/event transitions. Stable unit state stays in the full list.
    tracked.update(str(unit) for unit in previous.get("owned_units", []))
    tokens.update(_retained_artifacts(previous, tracked))
    for row in owned:
        if row.get("coverage") == "stalled":
            actionable.add(f"overdue-unit:{row['unit_id']}:{row.get('actor')}")
    for unit in sorted(tracked):
        _unit_artifacts(read, unit, advisories, tokens, actionable)


def _landing_tokens(landings: list[dict[str, Any]], previous: dict[str, Any], tokens: set[str]) -> set[str]:
    # Window results accumulate; advancing since never makes a prior landing
    # vanish from the state and manufacture a recovery wake on a quiet tick.
    landing_ids = {str(value) for value in previous.get("landing_ids", [])}
    landing_ids.update(str(row["landing_id"]) for row in landings)
    tokens.update(f"landing:{value}" for value in landing_ids)
    return landing_ids


def _owned_ids(owned: list[dict[str, Any]] | None) -> list[str]:
    return sorted(str(row["unit_id"]) for row in owned or [])


def collect_snapshot(
    read: RegisterRead,
    messaging: Any,
    *,
    since: str,
    previous: dict[str, Any],
    solet: str,
) -> dict[str, Any]:
    """Read actual ownership, state, candidate, verdict, event and liveness domains.

    A complete full-unit list is also read on every pass. It supplies state
    transitions even when resume times out, and lets completion/reversal remain
    visible after ownership disappears. Timestamps of peer heartbeats and read
    observations never enter the semantic digest.
    """
    advisories: list[str] = []
    tokens: set[str] = set()
    actionable: set[str] = set()
    resume = _attempt(advisories, "resume (full-list fallback used)", lambda: read(("resume", "--solet", solet, "--since", since)), {})
    resume_complete = _attempt(advisories, "resume receipt (full-list fallback used)", lambda: _resume_complete(resume), False)
    units = _attempt(advisories, "unit states", lambda: _pages(read, ("db", "unit", "list", "--repo", solet)), None)
    owned = _attempt(advisories, "owned units", lambda: _pages(read, ("db", "unit", "who-has-work", "--solet", solet, "--stale-after-seconds", "900"), records="rows"), None)
    _unit_state_tokens(read, units or [], owned or [], previous, advisories, tokens, actionable)
    landings = _attempt(advisories, "landings", lambda: _rows(read(("db", "landing", "list", "--repo", solet, "--since", since))), None)
    landing_ids = _landing_tokens(landings or [], previous, tokens)
    fleet = _fleet_read(messaging, advisories, actionable)
    tokens.update(actionable)
    if len(tokens) > MAX_TOKENS:
        advisories.append(f"semantic state exceeds {MAX_TOKENS} tokens; truncated, not quiet")
    # A failed resume is covered by the complete unit/candidate/verdict/event
    # fallback. Other failures remain unmeasured and cannot advance since.
    incomplete = [item for item in advisories if not item.startswith("resume")]
    return {
        "tokens": sorted(tokens)[:MAX_TOKENS],
        "actionable": sorted(actionable),
        "landing_ids": sorted(landing_ids)[-MAX_TOKENS:],
        "owned_units": _owned_ids(owned),
        "complete": all(value is not None for value in (units, owned, landings)) and not incomplete,
        "resume_complete": resume_complete,
        "advisories": advisories,
        "legacy_direct_delivery_unknown": fleet.get("legacy_direct_delivery_unknown", True),
        "owed_messages_truncated": fleet.get("owed_messages_truncated"),
    }


def _reconcile(orchestrator: object, intent: dict[str, Any], role: str) -> dict[str, Any]:
    """Find this exact token in persisted role envelopes, including consumed ones.

    A missing or incomplete read is unresolved, never permission to replay. The
    persisted envelope proves custody only; actual model wakes require the seat's
    later telemetry. No transport schema or behavior is changed here.
    """
    if intent.get("outcome") not in ("intent", "uncertain"):
        return intent
    state = _state(orchestrator)
    after: list[object] | None = None
    for _ in range(MAX_PAGES):
        query: dict[str, Any] = {"table": "agent_role_message", "filters": {"recipient_kind": "role", "recipient_key": role}, "order_by": [["created_at", "desc"], ["id", "desc"]], "limit": PAGE_SIZE}
        if after is not None:
            query["after"] = after
        rows = require_records(state.query_ordered("core", query))
        for row in rows:
            if str(intent["token"]) in str(row.get("content", "")):
                return {**intent, "outcome": "persisted", "message_id": row.get("message_id"), "delivered": row.get("delivered"), "consumed": row.get("consumed")}
        if len(rows) < PAGE_SIZE:
            return {**intent, "outcome": "uncertain", "reconciliation": "no exact envelope found; never replay"}
        after = [rows[-1]["created_at"], rows[-1]["id"]]
    return {**intent, "outcome": "uncertain", "reconciliation": "history page ceiling; never replay"}


def _notify(orchestrator: object, marker: dict[str, Any], *, role: str, state: dict[str, Any], ids: list[str], digest: str, now: str) -> None:
    token = f"seat-heartbeat:{uuid.uuid4()}"
    marker["send_intent"] = {"token": token, "digest": digest, "role": role, "outcome": "intent", "at": now, "ids": ids[:100]}
    _save(orchestrator, marker)
    content = f"Seat heartbeat {token}: " + "; ".join(ids[:100])
    if len(ids) > 100:
        content += f"; +{len(ids) - 100} changed ids (see marker)"
    try:
        response = _object(_send_peer_alert(orchestrator, name=role, content=content, state=state))
        data = _object(response.get("data"))
        message_id = data.get("message_id")
        marker["send_intent"] = {**marker["send_intent"], "outcome": "persisted" if message_id else "uncertain", "message_id": message_id, "delivery": data.get("delivery"), "response_status": response.get("action_status")}
        if message_id:
            marker["last_notified_digest"] = digest
    except Exception as exc:  # noqa: BLE001 -- effect may have happened; reconcile, never replay
        marker["send_intent"] = {**marker["send_intent"], "outcome": "uncertain", "error": f"{type(exc).__name__}: {exc}"}
        marker["advisories"].append("uncertain role send; exact envelope reconciliation required, never replay")


def _prepare_marker(orchestrator: object, previous: dict[str, Any], snapshot: dict[str, Any], *, digest: str, now: str, role: str) -> tuple[dict[str, Any], list[str], bool, bool]:
    old_tokens = set(previous.get("tokens", []))
    new_tokens = set(snapshot["tokens"])
    baseline = not previous or bool(previous.get("baseline_reset"))
    intent = _recover_intent(orchestrator, previous.get("send_intent"), role, snapshot["advisories"])
    marker: dict[str, Any] = {**previous, **snapshot, "observed_digest": digest, "last_tick": now, "baseline_reset": False, "retired": False, "previous_record_id": previous.get("record_id"), "send_intent": intent}
    if isinstance(intent, dict) and intent.get("outcome") == "persisted":
        marker["last_notified_digest"] = intent["digest"]
    changed = sorted(new_tokens.symmetric_difference(old_tokens))
    if snapshot["complete"]:
        marker["last_successful_tick"] = now
    else:
        marker["tokens"] = sorted(old_tokens.union(new_tokens))[:MAX_TOKENS]
        changed = sorted(new_tokens - old_tokens)
    should_send = bool(snapshot["actionable"]) if baseline else bool(changed)
    if isinstance(intent, dict) and intent.get("digest") == digest:
        should_send = False
    marker["status"] = "changed" if should_send else ("quiet" if snapshot["complete"] else "unmeasured")
    return marker, changed, baseline, should_send


def _recover_intent(orchestrator: object, intent: Any, role: str, advisories: list[str]) -> Any:
    if not isinstance(intent, dict):
        return intent
    return _attempt(advisories, "send reconciliation", lambda: _reconcile(orchestrator, intent, str(intent.get("role") or role)), intent)


def run_heartbeat(
    orchestrator: object,
    *,
    state: dict[str, Any],
    role: str,
    solet: str | None = None,
    read: RegisterRead = register_read,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    previous = read_marker(orchestrator)
    now = clock().isoformat()
    since = str(previous.get("last_successful_tick") or now)
    messaging = _resolve_agent_messaging_plugin(orchestrator)
    scope = solet or os.environ.get("SOLET_NAME", "")
    snapshot = collect_snapshot(read, messaging, since=since, previous=previous, solet=scope)
    if not scope:
        snapshot["advisories"].append("SOLET_NAME unconfigured; scope unmeasured")
        snapshot["complete"] = False
    digest = _digest(snapshot["tokens"])
    marker, changed, baseline, should_send = _prepare_marker(orchestrator, previous, snapshot, digest=digest, now=now, role=role)
    if not role:
        marker["advisories"].append("role unconfigured; observations retained without send")
        marker["status"] = "unmeasured"
        should_send = False
    if should_send:
        ids = snapshot["actionable"] if baseline else changed
        _notify(orchestrator, marker, role=role, state=state, ids=ids, digest=digest, now=now)
    _save(orchestrator, marker)
    for advisory in marker["advisories"]:
        logger.warning("seat heartbeat: %s", advisory)
    return marker


def _schedule_inventory(scheduling: Any, tag: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    receipt = _unwrap_response(scheduling.get_schedules_by_tag(tag=tag, state=context), action="get_schedules_by_tag")
    if receipt.get("complete") is not True:
        raise ValueError("schedule inventory incomplete")
    return _rows(receipt.get("schedules"))


def _schedule_history(scheduling: Any, schedule_id: str, context: dict[str, Any]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    after: list[object] | None = None
    for _ in range(MAX_PAGES):
        receipt = _unwrap_response(scheduling.get_schedule_binding_history(schedule_id=schedule_id, after=after, state=context), action="get_schedule_binding_history")
        events.extend(_rows(receipt.get("events")))
        next_cursor = receipt.get("next_cursor")
        if next_cursor is None:
            return events
        if not isinstance(next_cursor, list) or next_cursor == after:
            raise ValueError("schedule history continuation malformed")
        after = next_cursor
    raise ValueError("schedule history truncated")


def _installed_role(schedules: list[dict[str, Any]]) -> str:
    for row in schedules:
        for action in row.get("action_definitions", []):
            if action.get("process_key") == CHECK_KEY:
                return str(action.get("arguments", {}).get("role") or "")
    return ""


def _matches_schedule(row: dict[str, Any], role: str) -> bool:
    expected = [{"process_key": CHECK_KEY, "arguments": {"role": role}, "result_processor": None, "result_processor_kind": None}]
    return row.get("cron_expression") == CRON and row.get("status") == "scheduled" and row.get("action_definitions") == expected


def _create_successor(scheduling: Any, role: str, context: dict[str, Any]) -> str:
    created = _unwrap_response(
        scheduling.create_cron_schedule(
            cron_expression=CRON,
            actions=[{"process_key": "plugin::platform_health_plugin::check_main_seat_heartbeat_cron", "arguments": {"role": role}}],
            label="Main seat heartbeat",
            tags=[TAG],
            state=context,
        ),
        action="create_cron_schedule",
    )
    schedule_id = created.get("schedule_id")
    if not isinstance(schedule_id, str) or not schedule_id:
        raise ValueError("schedule creation has no confirmed id; old schedules retained")
    matches = [row for row in _schedule_inventory(scheduling, TAG, context) if row.get("schedule_id") == schedule_id]
    if not matches or not _matches_schedule(matches[0], role):
        raise ValueError("successor not confirmed; old schedules retained")
    return schedule_id


def _clear_predecessors(scheduling: Any, schedules: list[dict[str, Any]], schedule_id: str, context: dict[str, Any]) -> list[str]:
    cleared: list[str] = []
    for row in schedules:
        if row["schedule_id"] == schedule_id:
            continue
        result = _unwrap_response(scheduling.clear_scheduled_action(schedule_id=row["schedule_id"], state=context), action="clear_scheduled_action")
        if result.get("cancelled") is not True:
            raise ValueError(f"predecessor {row['schedule_id']} clear unconfirmed")
        cleared.append(row["schedule_id"])
    return cleared


def _install_schedule(scheduling: Any, schedules: list[dict[str, Any]], role: str, context: dict[str, Any], advisories: list[str]) -> dict[str, Any]:
    histories = {row["schedule_id"]: _schedule_history(scheduling, row["schedule_id"], context) for row in schedules}
    desired = [row for row in schedules if _matches_schedule(row, role)]
    schedule_id = desired[0]["schedule_id"] if desired else _create_successor(scheduling, role, context)
    cleared = _clear_predecessors(scheduling, schedules, schedule_id, context)
    return {"status": "already_present" if desired else "created", "schedule_id": schedule_id, "cleared": cleared, "prior_definitions": schedules, "prior_history": histories, "advisories": advisories, "cron_expression": CRON, "tag": TAG}


def ensure_schedule(scheduling: Any, *, role: str = "", prior_tag: str | None = None, expected_revision: int | None = None) -> dict[str, Any]:
    """Create-confirm-clear; revision is advisory under the seat's recorded ruling.

    Fresh installation requires a runtime role; boot may reuse an installed one.
    An incomplete observation retains the predecessor and returns an advisory.
    Live mutation remains seat-owned after landing.
    """
    context = {"flow_id": "flow-seat-heartbeat", "session_id": "sess-seat-heartbeat"}
    advisories = ["scheduler replacement has no revision precondition; revision is advisory"]
    try:
        schedules = _schedule_inventory(scheduling, TAG, context)
        if prior_tag and prior_tag != TAG:
            known = {row["schedule_id"] for row in schedules}
            schedules.extend(row for row in _schedule_inventory(scheduling, prior_tag, context) if row["schedule_id"] not in known)
        role = role or _installed_role(schedules)
        if not role:
            return {"status": "unconfigured", "advisories": advisories + ["role required for fresh install; existing schedules retained"]}
        if expected_revision is not None:
            advisories.append("expected revision is advisory; stale or missing revisions cannot refuse replacement")
        return _install_schedule(scheduling, schedules, role, context, advisories)
    except (ValueError, RuntimeError, OSError) as exc:
        return {"status": "unmeasured", "advisories": advisories + [str(exc)]}
