"""Windowed usage folds and harness adapters, with no pricing or plugin dependency.

UsageVector is structurally equivalent to the plugin usage_economics vector;
S2 owns conversion and pricing. Thinking is a breakdown of output, never added.
Only one usage-bearing eligible sibling contributes to each runtime session.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Protocol, TypedDict

RELOAD_SAFE = True
MAX_USAGE_PAGES_PER_SESSION = 120
MAX_ENUMERATION_PAGES = 120
UNKNOWN_MODEL = "unknown"


class UsageVector(TypedDict):
    input_tokens: int | float | None
    cached_input_tokens: int | float | None
    cache_write_input_tokens: int | float | None
    output_tokens: int | float | None
    reasoning_output_tokens: int | float | None
    tool_calls: int | float | None
    breakdown: dict[str, Any]


class UsageReader(Protocol):
    def list_session_usage_events(self, *, session_id: str, until: datetime | None = None, before: datetime | None = None, after: tuple[object, ...] | None = None, limit: int = 100) -> list[dict[str, object]]: ...
    def usage_session_siblings(self, runtime_session_ids: list[str], vendor: str | None = None) -> list[dict[str, object]]: ...
    def enumerate_usage_sessions_bounded(self, *, vendor: str, since: datetime, until: datetime) -> dict[str, Any]: ...
    def usage_last_poll_at(self, source_id: str) -> object: ...


UsageAdapter = Callable[[Sequence[Mapping[str, Any]], datetime | None, datetime | None], dict[str, Any]]
HARNESS_USAGE_ADAPTERS: dict[str, UsageAdapter] = {}
METERED_SOURCES = {"claude_code": frozenset({"claude_code_local"}), "codex": frozenset({"codex_local"})}
ADAPTER_NOTES = {
    "claude_code": "claude_code_local metered per message_id; cloud/pushed raw identity only and history not metered.",
    "codex": "codex_local metered by codex_token_count cumulative totals; cloud/pushed/history/ambient/state/goals/memories not separate meters.",
}


def register_usage_adapter(runtime: str, adapter: UsageAdapter) -> None:
    """Register or replace a pure adapter; callers retain registry snapshots for reversal."""
    HARNESS_USAGE_ADAPTERS[runtime] = adapter


def stamp(value: object) -> datetime:
    parsed = value if isinstance(value, datetime) else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def _number(value: object) -> int | float | None:
    return value if isinstance(value, int | float) and not isinstance(value, bool) else None


def _vector(values: Mapping[str, Any], *, claude: bool) -> UsageVector:
    details = values.get("output_tokens_details") or {}
    return UsageVector(
        input_tokens=_number(values.get("input_tokens")),
        cached_input_tokens=_number(values.get("cache_read_input_tokens" if claude else "cached_input_tokens")),
        cache_write_input_tokens=_number(values.get("cache_creation_input_tokens")) if claude else None,
        output_tokens=_number(values.get("output_tokens")),
        reasoning_output_tokens=_number(details.get("thinking_tokens")) if claude else _number(values.get("reasoning_output_tokens")),
        tool_calls=None,
        breakdown={},
    )


def _add(target: UsageVector, incoming: UsageVector) -> None:
    for key in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "tool_calls"):
        value = incoming[key]
        if value is not None:
            target[key] = (target[key] or 0) + value


def _empty() -> UsageVector:
    return UsageVector(input_tokens=None, cached_input_tokens=None, cache_write_input_tokens=None, output_tokens=None, reasoning_output_tokens=None, tool_calls=None, breakdown={})


def _in_window(event: Mapping[str, Any], since: datetime | None, until: datetime | None) -> bool:
    when = stamp(event["event_at"])
    return (since is None or when >= since) and (until is None or when <= until)


def _window_events(events: Sequence[Mapping[str, Any]], since: datetime | None, until: datetime | None) -> list[Mapping[str, Any]]:
    return sorted((event for event in events if _in_window(event, since, until)), key=lambda event: (stamp(event["event_at"]), int(event.get("sequence") or 0), str(event.get("id") or "")))


def _model(usage: Mapping[str, Any]) -> str:
    raw = usage.get("model")
    return raw if isinstance(raw, str) and raw else UNKNOWN_MODEL


def _unknown_advisory(count: int) -> list[str]:
    return [f"model_unavailable:{count} events; their model is not recorded"] if count else []


def _breakdown(target: UsageVector, usage: Mapping[str, Any]) -> None:
    cache = usage.get("cache_creation") or {}
    for ttl, source in (("cache_write_5m_input_tokens", "ephemeral_5m_input_tokens"), ("cache_write_1h_input_tokens", "ephemeral_1h_input_tokens")):
        value = _number(cache.get(source))
        if value is not None:
            target["breakdown"][ttl] = target["breakdown"].get(ttl, 0) + value
    for category in ("speed", "service_tier"):
        if usage.get(category) is not None:
            counts = target["breakdown"].setdefault(category, {})
            label = str(usage[category])
            counts[label] = counts.get(label, 0) + 1


def claude_usage(events: Sequence[Mapping[str, Any]], since: datetime | None, until: datetime | None) -> dict[str, Any]:
    models: dict[str, UsageVector] = {}
    seen: set[str] = set()
    missing = unknown = used = 0
    window = _window_events(events, since, until)
    for event in window:
        usage = event.get("usage_json")
        if not isinstance(usage, dict):
            continue
        identity = usage.get("message_id")
        if isinstance(identity, str) and identity:
            if identity in seen:
                continue
            seen.add(identity)
        else:
            missing += 1
        model = _model(usage)
        unknown += model == UNKNOWN_MODEL
        used += 1
        target = models.setdefault(model, _empty())
        _add(target, _vector(usage, claude=True))
        _breakdown(target, usage)
    advisories = ([f"dedupe_unavailable:{missing} events"] if missing else []) + _unknown_advisory(unknown)
    return {"by_model": models, "usage_event_count": used, "advisories": advisories}


def _codex_events(events: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [event for event in events if isinstance(event.get("content_json"), dict) and event["content_json"].get("subtype") == "codex_token_count" and isinstance(event.get("usage_json"), dict) and isinstance(event["usage_json"].get("total"), dict)]


def _difference(latest: UsageVector, baseline: UsageVector, advisories: list[str]) -> UsageVector:
    result = _empty()
    for key in ("input_tokens", "cached_input_tokens", "cache_write_input_tokens", "output_tokens", "reasoning_output_tokens", "tool_calls"):
        end, start = latest[key], baseline[key]
        if end is not None:
            difference = end - (start or 0)
            result[key] = difference
            if difference < 0:
                advisories.append(f"cumulative_decreased:{key}")
    return result


def codex_usage(events: Sequence[Mapping[str, Any]], since: datetime | None, until: datetime | None) -> dict[str, Any]:
    eligible = _codex_events(events)
    ordered = _window_events(eligible, None, until)
    if not ordered:
        return {"by_model": {}, "usage_event_count": 0, "advisories": []}
    latest = ordered[-1]
    if since is not None and stamp(latest["event_at"]) < since:
        return {"by_model": {}, "usage_event_count": 0, "advisories": []}
    before = [event for event in ordered if since is not None and stamp(event["event_at"]) < since]
    baseline = _vector(before[-1]["usage_json"]["total"], claude=False) if before else _empty()
    advisories: list[str] = []
    vector = _difference(_vector(latest["usage_json"]["total"], claude=False), baseline, advisories)
    model = _model(latest["usage_json"])
    unknown = sum(_model(event["usage_json"]) == UNKNOWN_MODEL for event in ordered)
    advisories.extend(_unknown_advisory(unknown))
    return {"by_model": {model: vector}, "usage_event_count": len(_window_events(eligible, since, until)), "advisories": advisories}


register_usage_adapter("claude_code", claude_usage)
register_usage_adapter("codex", codex_usage)


def totals_for_events(runtime: str, events: Sequence[Mapping[str, Any]], since: datetime | None = None, until: datetime | None = None) -> dict[str, Any]:
    """Pure adapter fold; absent categories stay None and unsupported harnesses stay explicit."""
    adapter = HARNESS_USAGE_ADAPTERS.get(runtime)
    selected = _window_events(events, since, until)
    folded = adapter(events, since, until) if adapter else {"by_model": {}, "usage_event_count": 0, "advisories": [f"adapter_unavailable:{runtime}"]}
    return {**folded, "adapter": runtime if adapter else None, "adapter_notes": ADAPTER_NOTES.get(runtime, f"No built-in metering table entry for {runtime}."), "event_count": len(selected), "first_event_at": selected[0]["event_at"] if selected else None, "last_event_at": selected[-1]["event_at"] if selected else None}


def _read_events(reader: UsageReader, session: Mapping[str, Any], since: datetime | None, until: datetime | None) -> tuple[list[dict[str, object]], list[str]]:
    identity = str(session["id"])
    # Codex still subtracts cumulative snapshots, but counts require the same
    # complete keyset walk as Claude, including the latest pre-window baseline.
    events: list[dict[str, object]] = []
    after: tuple[object, ...] | None = None
    for _page in range(MAX_USAGE_PAGES_PER_SESSION):
        page = reader.list_session_usage_events(session_id=identity, until=until, after=after)
        events.extend(page)
        if len(page) < 100 or (since is not None and stamp(page[-1]["event_at"]) < since):
            return events, []
        after = (page[-1]["event_at"], page[-1]["id"])
    return events, [f"usage_pages_truncated:{MAX_USAGE_PAGES_PER_SESSION}"]


def _winner(reader: UsageReader, siblings: list[dict[str, Any]]) -> tuple[dict[str, Any], bool]:
    eligible = [row for row in siblings if row.get("source_kind") in METERED_SOURCES.get(str(row["vendor"]), frozenset())]
    bearing = [row for row in eligible if reader.list_session_usage_events(session_id=str(row["id"]), limit=1)]
    pool = bearing or eligible or siblings
    return max(pool, key=lambda row: (int(row.get("event_count") or 0), str(row.get("last_event_at") or ""), str(row["id"]))), bool(eligible)


def _session_total(reader: UsageReader, siblings: list[dict[str, Any]], since: datetime | None, until: datetime | None) -> dict[str, Any]:
    winner, supported = _winner(reader, siblings)
    events, truncated = _read_events(reader, winner, since, until) if supported else ([], [])
    folded = totals_for_events(str(winner["vendor"]), events, since, until)
    poll = reader.usage_last_poll_at(str(winner["source_id"]))
    advisories = folded["advisories"] + truncated + ([] if poll is not None else ["last_poll_at_unavailable"])
    advisories.extend(sorted({notice for row in siblings for notice in row.get("usage_identity_advisories", [])}))
    if not supported:
        advisories.append(f"meter_unsupported:{winner.get('source_kind')}")
    return {
        **folded,
        "runtime_session_id": winner.get("resolved_runtime_session_id", winner["external_session_id"]),
        "vendor": winner["vendor"],
        "session_id": winner["id"],
        "status": "partial" if truncated else "complete",
        "meter": "supported" if supported else "unsupported",
        "last_poll_at": poll,
        "sibling_rows_resolved": len(siblings) - 1,
        "advisories": advisories,
    }


def _bounds(since: datetime | str | None, until: datetime | str | None) -> tuple[datetime | None, datetime | None]:
    try:
        start, end = stamp(since) if since is not None else None, stamp(until) if until is not None else None
    except (ValueError, TypeError) as exc:
        raise ValueError("parameter_invalid: since/until must be ISO timestamps") from exc
    if start is not None and end is not None and start > end:
        raise ValueError("parameter_invalid: since must not exceed until")
    return start, end


def _requested_sessions(reader: UsageReader, runtime_session_ids: list[str] | None, vendor: str | None, start: datetime | None, end: datetime | None) -> tuple[list[str], dict[str, Any]]:
    ids = list(dict.fromkeys(runtime_session_ids or []))
    if runtime_session_ids is not None and len(runtime_session_ids) > 200:
        raise ValueError("parameter_invalid: runtime_session_ids supports at most 200 ids")
    enumeration: dict[str, Any] = {}
    if not ids:
        if vendor is None or start is None or end is None:
            raise ValueError("parameter_invalid: provide ids or vendor, since and until")
        enumeration = reader.enumerate_usage_sessions_bounded(vendor=vendor, since=start, until=end)
        ids = [str(row["external_session_id"]) for row in enumeration["sessions"]]
    return ids, enumeration


def session_usage_totals(reader: UsageReader, runtime_session_ids: list[str] | None = None, since: datetime | str | None = None, until: datetime | str | None = None, vendor: str | None = None) -> dict[str, Any]:
    start, end = _bounds(since, until)
    ids, enumeration = _requested_sessions(reader, runtime_session_ids, vendor, start, end)
    siblings = reader.usage_session_siblings(ids, vendor=vendor) if ids else []
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for row in siblings:
        groups.setdefault((str(row["vendor"]), str(row.get("resolved_runtime_session_id", row["external_session_id"]))), []).append(row)
    sessions = [_session_total(reader, group, start, end) for group in groups.values()]
    found = {str(alias) for row in siblings for alias in _session_aliases(row)}
    not_found = [identity for identity in ids if identity not in found]
    return _response(sessions, not_found, enumeration)


def _session_aliases(row: Mapping[str, Any]) -> list[str]:
    """Readers without derived identity metadata retain their exact-id contract."""
    return row.get("runtime_session_aliases", [str(row["external_session_id"])])


def _response(sessions: list[dict[str, Any]], not_found: list[str], enumeration: dict[str, Any]) -> dict[str, Any]:
    status = "partial" if any(row["status"] == "partial" for row in sessions) else "complete"
    if enumeration.get("overflow"):
        status = "unknown:bounded"
    elif enumeration.get("partial"):
        status = "partial"
    return {"status": status, "sessions": sessions, "not_found": not_found, "advisories": enumeration.get("advisories", []), **({"overflow": enumeration["overflow"], "sessions_at_least": enumeration["sessions_at_least"]} if enumeration else {})}
