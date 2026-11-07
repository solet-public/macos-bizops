#!/usr/bin/env python3
"""Regression smoke locking the 2026-05-31 vendor-drift recency fix.

Watchdog tag: ``dispatch:in_flight:codex_local_recency_blind``.

Symptom (pre-fix): the ``codex_local`` source had ingested 193 sessions /
17,467 events with max ``last_event_at = 2026-01-15``, but on-disk
rollouts went through 2026-05-31. Discovery cursor had advanced past
every May file — the cursor at investigation time was
``rollout-2026-05-31T11-41-08-...`` — yet ``session_ledger__session``
had ZERO rows for any rollout after ``rollout-2026-01-14T19-17-25-...``.

Root cause (verified against ``~/.codex/sessions``): Codex added two
new line types after 2026-01-14 that the vendor parser refused:

* Top-level ``"type": "compacted"`` — context-compaction summary marker.
  Its ``payload.replacement_history`` carries messages that were
  already in the rollout file above this line, so the marker adds no
  operator-visible signal. ``_RECOGNIZED_LINE_TYPES`` did not list it
  → ``ValueError`` on every rollout containing one.

* Response-item ``payload.type = "web_search_call"`` — Codex's built-in
  web-search tool surface. No ``call_id`` / ``id`` field.
  ``_RECOGNIZED_ITEM_TYPES`` did not list it → ``ValueError``.

Both raised through ``iter_events_from_path``; the per-session
try/except in :class:`SessionLedgerImporter` (landed earlier today as
``per_session_resilience_smoke`` Bug-4 fix) silently skipped the
affected session and ``last_session_ref`` was set BEFORE the try block
so the discovery cursor advanced regardless. 4.5 months of orphaned
sessions; recency-blind from Jan 15 onward.

Fix (this smoke pins):

1. ``compacted`` is added to both ``_RECOGNIZED_LINE_TYPES`` and
   ``_SKIPPED_LINE_TYPES`` — known-skip alongside ``event_msg`` and
   ``turn_context``.
2. ``web_search_call`` is added to ``_RECOGNIZED_ITEM_TYPES`` with a
   new normalizer ``_normalize_web_search_call`` → ``TOOL_CALL``
   event, ``tool_name="web_search"``, arguments from
   ``payload.action.query`` / ``payload.action.queries``.
3. ``vendor_event_id`` for ``web_search_call`` is synthesized as
   ``f"web_search:{timestamp}:{line_no}"`` because Codex sometimes
   emits multiple ``web_search_call`` lines at the same millisecond
   (2026-05-07 fixtures: 6 lines at the same ts) — timestamp alone
   would collide and the importer's idempotency check would drop the
   collisions on re-poll.

Run::

    .venv/bin/python3 plugins/codex_filesystem_session_source_plugin/tests/codex_vendor_recency_smoke.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "codex_filesystem_session_source_plugin" / "src"),
)

from ananta.llm.session_ledger.types import (  # noqa: E402
    EventType,
    MessageRole,
)
from ananta.llm.session_ledger.vendor import codex as codex_vendor  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


# ───── Fixture builders ─────────────────────────────────────────────────────


_SESSION_META: dict[str, object] = {
    "timestamp": "2026-05-30T15:53:02.000Z",
    "type": "session_meta",
    "payload": {
        "id": "019e7b17-4222-70d3-8ae8-fa5858235a3a",
        "timestamp": "2026-05-30T15:53:02.000Z",
        "cwd": "/proj/recency",
        "originator": "codex_cli_rs",
        "cli_version": "0.99.0",
    },
}

_USER_MESSAGE: dict[str, object] = {
    "timestamp": "2026-05-30T15:53:10.000Z",
    "type": "response_item",
    "payload": {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "what changed today?"}],
    },
}

_COMPACTED_LINE: dict[str, object] = {
    "timestamp": "2026-05-30T15:53:20.000Z",
    "type": "compacted",
    "payload": {
        "message": "",
        "replacement_history": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "earlier note"}],
            },
        ],
    },
}

_POST_COMPACTED_MESSAGE: dict[str, object] = {
    "timestamp": "2026-05-30T15:53:25.000Z",
    "type": "response_item",
    "payload": {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "after compaction body"}],
    },
}


def _web_search_call_line(ts: str, query: str = "q1") -> dict[str, object]:
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "web_search_call",
            "status": "completed",
            "action": {
                "type": "search",
                "query": query,
                "queries": [query, f"{query} variant"],
            },
        },
    }


def _bare_web_search_call_line(ts: str) -> dict[str, object]:
    """Vendor-drift earlier shape: web_search_call line with no `action`.

    Confirmed empirically in spring-2026 codex rollouts (e.g.
    rollout-2026-03-25T16-13-09-*.jsonl). Pre-fix this raised
    ValueError aborting the entire session ingest.
    """
    return {
        "timestamp": ts,
        "type": "response_item",
        "payload": {
            "type": "web_search_call",
            "status": "completed",
        },
    }


def _write_rollout(
    root: Path,
    *,
    date_subpath: str,
    filename: str,
    lines: list[dict[str, object]],
) -> Path:
    target = root / date_subpath
    target.mkdir(parents=True, exist_ok=True)
    path = target / filename
    with path.open("w", encoding="utf-8") as fh:
        for entry in lines:
            fh.write(json.dumps(entry) + "\n")
    return path


# ───── (1) `compacted` is silently skipped — no ValueError, no event ────────


def test_compacted_line_is_skipped_not_raised() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = _write_rollout(
            root,
            date_subpath="2026/05/30",
            filename=(
                "rollout-2026-05-30T15-53-02-019e7b17-4222-70d3-8ae8-"
                "fa5858235a3a.jsonl"
            ),
            lines=[_SESSION_META, _USER_MESSAGE, _COMPACTED_LINE,
                   _POST_COMPACTED_MESSAGE],
        )
        events = list(codex_vendor.iter_events_from_path(path, start_line=0))
    _check(
        len(events) == 2,
        f"compacted + session_meta both skipped, only the 2 message "
        f"response_items yielded (got {len(events)})",
    )
    # Line numbers are 0-based offsets; user message is line 1 (after
    # session_meta), post-compaction message is line 3 (after compacted).
    line_offsets = [offset for offset, _ in events]
    _check(
        line_offsets == [1, 3],
        f"line_offsets == [1, 3] — proves compacted at line 2 was "
        f"SKIPPED not silently dropped from numbering (got {line_offsets})",
    )


# ───── (2) `web_search_call` normalizes to TOOL_CALL with tool_name=web_search


def test_web_search_call_normalizes_as_tool_call() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = _write_rollout(
            root,
            date_subpath="2026/05/30",
            filename=(
                "rollout-2026-05-30T15-53-02-019e7b17-4222-70d3-8ae8-"
                "fa5858235a3a.jsonl"
            ),
            lines=[
                _SESSION_META,
                _web_search_call_line("2026-05-30T16:00:00.000Z", query="alpha"),
            ],
        )
        events = list(codex_vendor.iter_events_from_path(path, start_line=0))
    _check(len(events) == 1, f"single event yielded (got {len(events)})")
    if not events:
        return
    _, raw = events[0]
    normalized = codex_vendor.normalize_raw(raw)
    _check(
        normalized.event_type is EventType.TOOL_CALL,
        f"event_type == TOOL_CALL (got {normalized.event_type})",
    )
    _check(
        normalized.role is MessageRole.ASSISTANT,
        f"role == ASSISTANT (got {normalized.role})",
    )
    body = normalized.content_json or {}
    _check(
        body.get("tool_name") == "web_search",
        f"content_json.tool_name == 'web_search' (got {body.get('tool_name')!r})",
    )
    args = body.get("arguments") or {}
    _check(
        args.get("query") == "alpha",
        f"arguments.query == 'alpha' (got {args.get('query')!r})",
    )
    _check(
        args.get("queries") == ["alpha", "alpha variant"],
        f"arguments.queries preserves multi-query list "
        f"(got {args.get('queries')!r})",
    )


# ───── (2b) Bare `web_search_call` (no action) — vendor-drift tolerance ─────


def test_bare_web_search_call_falls_back_to_empty_arguments() -> None:
    """Pre-2026-06-17 fix this raised ``ValueError: codex web_search_call
    payload missing dict 'action'`` and aborted the entire session ingest.
    Post-fix: the line still yields a TOOL_CALL event with tool_name=
    web_search and empty arguments (status preserved when present)."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = _write_rollout(
            root,
            date_subpath="2026/03/25",
            filename=(
                "rollout-2026-03-25T16-13-09-019d2745-f4d6-7e42-b91c-"
                "570761cfa6f6.jsonl"
            ),
            lines=[
                _SESSION_META,
                _bare_web_search_call_line("2026-03-26T00:52:26.792Z"),
            ],
        )
        events = list(codex_vendor.iter_events_from_path(path, start_line=0))
    _check(len(events) == 1, f"single event yielded (got {len(events)})")
    if not events:
        return
    _, raw = events[0]
    normalized = codex_vendor.normalize_raw(raw)
    _check(
        normalized.event_type is EventType.TOOL_CALL,
        f"event_type == TOOL_CALL on bare shape (got {normalized.event_type})",
    )
    body = normalized.content_json or {}
    _check(
        body.get("tool_name") == "web_search",
        f"content_json.tool_name == 'web_search' on bare shape "
        f"(got {body.get('tool_name')!r})",
    )
    args = body.get("arguments") or {}
    _check(
        "query" not in args and "queries" not in args,
        f"arguments carries no query/queries on bare shape "
        f"(got {sorted(args.keys())!r})",
    )
    _check(
        args.get("status") == "completed",
        f"arguments.status preserved when present (got {args.get('status')!r})",
    )


# ───── (3) Same-millisecond web_search_call collision → distinct ids ────────


def test_web_search_call_vendor_event_ids_distinct_on_same_ts() -> None:
    """Codex 2026-05-07 fixture has 6 ``web_search_call`` lines at the same
    millisecond timestamp. ``vendor_event_id = f"web_search:{ts}:{line_no}"``
    must yield 6 distinct values; if it didn't, the importer's
    ``find_event_id_by_vendor_id`` idempotency check (landed today as Bug 6
    fix) would drop 5 of 6 on the second poll."""
    same_ts = "2026-05-07T22:40:26.733Z"
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = _write_rollout(
            root,
            date_subpath="2026/05/07",
            filename=(
                "rollout-2026-05-07T15-40-26-019e0499-74c8-77c2-86fb-"
                "7d1b9b68cf5c.jsonl"
            ),
            lines=[
                _SESSION_META,
                _web_search_call_line(same_ts, query="q-a"),
                _web_search_call_line(same_ts, query="q-b"),
                _web_search_call_line(same_ts, query="q-c"),
                _web_search_call_line(same_ts, query="q-d"),
                _web_search_call_line(same_ts, query="q-e"),
                _web_search_call_line(same_ts, query="q-f"),
            ],
        )
        events = list(codex_vendor.iter_events_from_path(path, start_line=0))
    _check(
        len(events) == 6,
        f"all 6 web_search_call lines yielded (got {len(events)})",
    )
    vendor_ids: list[str] = [
        raw.vendor_event_id for _, raw in events
        if isinstance(raw.vendor_event_id, str)
    ]
    _check(
        len(vendor_ids) == 6 and len(set(vendor_ids)) == 6,
        f"all 6 vendor_event_ids are distinct strings "
        f"(got {len(set(vendor_ids))} unique of {len(vendor_ids)}); "
        "if this drops to 1 the importer's idempotency check silently "
        "discards 5 of 6 lines on re-poll",
    )
    # Spot-check the synthesis pattern.
    expected_pattern = "web_search:2026-05-07T22:40:26.733Z:"
    _check(
        all(v.startswith(expected_pattern) for v in vendor_ids),
        f"every vendor_event_id matches 'web_search:<ts>:<line>' pattern "
        f"(got {sorted(vendor_ids)!r})",
    )


# ───── (4) Multi-month-tree walk is recency-monotonic and fully visited ─────


def test_multi_month_tree_walk_visits_all_months_in_order() -> None:
    """Per dispatch acceptance: ``discover_sessions`` on a fixture
    filesystem with files spanning multiple month-trees returns ALL of
    them (no recency blindness). The cursor key is the alphabetically-
    sortable filename stem, so 2025-12 < 2026-05 lexically — but we
    pin the actual walk behavior here."""
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        dec = _write_rollout(
            root, date_subpath="2025/12/03", lines=[_SESSION_META],
            filename=(
                "rollout-2025-12-03T10-00-00-019b0001-1111-1111-1111-"
                "111111111111.jsonl"
            ),
        )
        jan = _write_rollout(
            root, date_subpath="2026/01/14", lines=[_SESSION_META],
            filename=(
                "rollout-2026-01-14T19-17-25-019bbfa8-6dac-7312-ae39-"
                "0a13da1e6a4f.jsonl"
            ),
        )
        may = _write_rollout(
            root, date_subpath="2026/05/30", lines=[_SESSION_META],
            filename=(
                "rollout-2026-05-30T15-53-02-019e7b17-4222-70d3-8ae8-"
                "fa5858235a3a.jsonl"
            ),
        )
        listed = list(codex_vendor.iter_session_files(root))
    _check(
        listed == [dec, jan, may],
        "iter_session_files yields all three months in stem-sorted "
        f"(time-monotonic) order (got {[p.name for p in listed]})",
    )


# ───── (5) End-to-end May 30-style fixture parses cleanly ───────────────────


def test_realistic_may_30_fixture_parses_without_value_error() -> None:
    """Mirrors the actual 2026-05-30 rollout shape: session_meta + message
    + reasoning + function_call + function_call_output + compacted +
    web_search_call + final-message. Pre-fix this raised ValueError on the
    ``compacted`` line; with fix every event yields, every normalize succeeds."""
    lines: list[dict[str, object]] = [
        _SESSION_META,
        _USER_MESSAGE,
        {
            "timestamp": "2026-05-30T15:53:12.000Z",
            "type": "response_item",
            "payload": {
                "type": "reasoning",
                "summary": [
                    {"type": "summary_text", "text": "thinking about it"},
                ],
            },
        },
        {
            "timestamp": "2026-05-30T15:53:14.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "call_id": "call_abc",
                "name": "Bash",
                "arguments": '{"cmd":"ls"}',
            },
        },
        {
            "timestamp": "2026-05-30T15:53:15.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_abc",
                "output": "file1\nfile2\n",
            },
        },
        _COMPACTED_LINE,
        _web_search_call_line("2026-05-30T16:00:00.000Z", query="docs query"),
        _POST_COMPACTED_MESSAGE,
    ]
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = _write_rollout(
            root,
            date_subpath="2026/05/30",
            filename=(
                "rollout-2026-05-30T15-53-02-019e7b17-4222-70d3-8ae8-"
                "fa5858235a3a.jsonl"
            ),
            lines=lines,
        )
        events = list(codex_vendor.iter_events_from_path(path, start_line=0))
    # session_meta + compacted skipped → 6 yielded.
    _check(
        len(events) == 6,
        f"6 events yielded (session_meta + compacted skipped); "
        f"got {len(events)}",
    )
    types: list[EventType] = []
    for _, raw in events:
        normalized = codex_vendor.normalize_raw(raw)
        types.append(normalized.event_type)
    expected_types = [
        EventType.MESSAGE,     # user message
        EventType.SYSTEM,      # reasoning → SYSTEM
        EventType.TOOL_CALL,   # function_call
        EventType.TOOL_RESULT, # function_call_output
        EventType.TOOL_CALL,   # web_search_call
        EventType.MESSAGE,     # post-compaction assistant message
    ]
    _check(
        types == expected_types,
        f"event-type sequence matches expected ({expected_types}); "
        f"got {types}",
    )


# ───── (6) `developer` role maps to USER (same as `user`) ───────────────────


def test_developer_role_maps_to_user() -> None:
    """Operator-approved 2026-06-01 dispatch ``codex_developer_role``:
    Codex's ``developer``-role transcripts carry operator instruction prose
    (e.g. ``<permissions instructions>``, ``<collaboration_mode>``).
    Semantically equivalent to ``user`` from the ledger's perspective.

    Pre-fix the parser raised ``ValueError(codex role 'developer' cannot be
    mapped to MessageRole)``; the importer's per-session try/except (Bug-4
    fix earlier today) silently orphaned the entire session. May 2026 Codex
    sessions blocked on this until operator-invoked reset+rewalk.
    """
    # Mirror the existing message-line shape and let the vendor parse it
    # end-to-end so the test covers both ``_line_to_raw_event`` and
    # ``normalize_raw``.
    line: dict[str, object] = {
        "timestamp": "2026-05-31T13:12:10.256Z",
        "type": "response_item",
        "payload": {
            "type": "message",
            "role": "developer",
            "content": [
                {"type": "input_text",
                 "text": "<permissions instructions>...</permissions instructions>"},
            ],
        },
    }
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = _write_rollout(
            root, date_subpath="2026/05/31",
            filename=(
                "rollout-2026-05-31T13-12-10-019e7f57-0113-7ad3-acc8-"
                "5e2cb11a4c48.jsonl"
            ),
            lines=[_SESSION_META, line],
        )
        events = list(codex_vendor.iter_events_from_path(path, start_line=0))
    _check(
        len(events) == 1,
        f"developer-role line yields 1 event (got {len(events)})",
    )
    if not events:
        return
    _, raw = events[0]
    # Sanity: ``user`` and ``developer`` should normalize to the same role.
    developer_normalized = codex_vendor.normalize_raw(raw)
    _check(
        developer_normalized.role is MessageRole.USER,
        f"developer → MessageRole.USER (got {developer_normalized.role!r}); "
        "matches the `user` mapping per operator-approved 2026-06-01 dispatch",
    )
    # Pin the equivalence by parsing the same line with role='user' and
    # confirming the normalized role matches.
    user_line = dict(line)
    user_payload: dict[str, object] = dict(cast(dict[str, object], line["payload"]))
    user_payload["role"] = "user"
    user_line["payload"] = user_payload
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = _write_rollout(
            root, date_subpath="2026/05/31",
            filename=(
                "rollout-2026-05-31T13-12-11-019e7f57-0113-7ad3-acc8-"
                "5e2cb11a4c49.jsonl"
            ),
            lines=[_SESSION_META, user_line],
        )
        user_events = list(codex_vendor.iter_events_from_path(path, start_line=0))
    if user_events:
        _, user_raw = user_events[0]
        user_normalized = codex_vendor.normalize_raw(user_raw)
        _check(
            developer_normalized.role is user_normalized.role,
            f"developer.role == user.role "
            f"(developer={developer_normalized.role!r}, "
            f"user={user_normalized.role!r})",
        )


# ───── Driver ───────────────────────────────────────────────────────────────


def main() -> int:
    print("=== codex_vendor_recency_smoke ===")
    test_compacted_line_is_skipped_not_raised()
    test_web_search_call_normalizes_as_tool_call()
    test_bare_web_search_call_falls_back_to_empty_arguments()
    test_web_search_call_vendor_event_ids_distinct_on_same_ts()
    test_multi_month_tree_walk_visits_all_months_in_order()
    test_realistic_may_30_fixture_parses_without_value_error()
    test_developer_role_maps_to_user()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
