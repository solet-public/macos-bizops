#!/usr/bin/env python3
"""Vendor-drift-tolerance smoke for the claude_code session-source parser.

Anchored to the 2026-06-12 ingest-stall incidents:

- Sub-2.5: Claude Code added the timestamp-absent metadata line type ``mode``
  (key: ``mode``, sessionId-only, no ``timestamp``). The parser was too
  strict — aborted the whole session on first ``mode`` line.
- Task #14: Claude Code emits timestamp-present marker lines of type
  ``queue-operation`` (912 occurrences across today's session corpus).
  These have a ``timestamp`` but lack the event-shape ``message`` dict;
  the parser's old "unrecognized line 'type'" raise burned every session.

Unknown top-level string types produce advisory success, including event-shaped
objects. Known event integrity validation and explicit metadata skips stay strict.
This smoke covers suffix usage continuity, physical grouping, safe direct warnings,
immutable reports, replay, nesting, concurrency and exception cleanup.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/claude_code_vendor_drift_tolerance_smoke.py
"""

from __future__ import annotations

import sys
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.poll_advisories import (  # noqa: E402
    begin_claude_read,
    claude_line_context,
    collect_poll_advisories,
    complete_claude_read,
    completed_claude_read,
    poll_source_context,
)
from ananta.llm.session_ledger.types import ImporterReport, UnknownLineAdvisory  # noqa: E402
from ananta.llm.session_ledger.vendor.claude_code import (  # noqa: E402
    _SKIP_LINE_TYPES,
    parse_line,
    parse_line_data,
)


def _assert(cond: object, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def test_mode_line_yields_zero_events() -> None:
    """The 2026-06-12 ``mode`` line type must skip cleanly (no exception)."""
    _assert(
        "mode" in _SKIP_LINE_TYPES,
        "regression: 'mode' must be in _SKIP_LINE_TYPES; was the canonical entry removed?",
    )
    line = {
        "type": "mode",
        "mode": "default",
        "sessionId": "test-session-abc",
    }
    events = list(parse_line_data(line))
    _assert(
        events == [],
        f"expected zero events for 'mode' metadata line; got {events!r}",
    )


def test_queue_operation_line_yields_zero_events() -> None:
    """The 2026-06-12 Task #14 ``queue-operation`` line type must skip cleanly.

    HAS a ``timestamp`` field but no event-shape ``message`` dict; the
    structural tolerance + explicit allowlist both keep ingest alive.
    """
    _assert(
        "queue-operation" in _SKIP_LINE_TYPES,
        "regression: 'queue-operation' must be in _SKIP_LINE_TYPES; was the canonical entry removed?",
    )
    line = {
        "type": "queue-operation",
        "operation": "enqueue",
        "timestamp": "2026-06-12T16:00:00Z",
        "sessionId": "test-session-abc",
        "content": "some payload",
    }
    events = list(parse_line_data(line))
    _assert(
        events == [],
        f"expected zero events for 'queue-operation' marker line; got {events!r}",
    )


def test_unknown_timestamp_absent_line_is_tolerated() -> None:
    """Forward-compat: an UNKNOWN timestamp-absent line type must NOT raise.

    Plants a synthetic line type Claude Code might add in the future without
    a `timestamp` field. The parser should treat it as metadata + skip with
    an advisory, NOT raise — that keeps ingest alive across vendor drift.
    """
    line = {
        "type": "hypothetical-future-metadata-2027-01-01",
        "sessionId": "test-session-xyz",
        "futureField": "anything",
    }
    # MUST NOT raise.
    events = list(parse_line_data(line))
    _assert(
        events == [],
        f"expected zero events for unknown timestamp-absent metadata line; got {events!r}",
    )


def test_unknown_timestamp_present_no_event_shape_is_tolerated() -> None:
    """Forward-compat: an UNKNOWN timestamp-present line type WITHOUT event-shape tolerated.

    Mirrors the Task #14 ``queue-operation`` pattern: timestamp-present
    marker line that doesn't carry the event-shape ``message`` dict signal.
    The structural tolerance branch must NOT raise on this shape — it
    keeps ingest alive across future vendor marker-line additions.
    """
    line = {
        "type": "hypothetical-future-marker-2027-01-01",
        "sessionId": "test-session-xyz",
        "timestamp": "2027-01-01T00:00:00Z",
        "operation": "something-new",
        "content": "marker payload",
    }
    # MUST NOT raise.
    events = list(parse_line_data(line))
    _assert(
        events == [],
        f"expected zero events for unknown timestamp-present marker line (no event-shape); got {events!r}",
    )


def test_unknown_suffix() -> None:
    """An event-shaped unknown cannot abandon later known usage."""
    known = {
        "type": "assistant",
        "sessionId": "synthetic",
        "timestamp": "2026-10-18T00:00:00Z",
        "message": {"role": "assistant", "content": "synthetic", "usage": {"input_tokens": 3, "output_tokens": 4}, "model": "fixture-model"},
    }
    lines = [known, {"type": "synthetic-unknown-line-type", "message": {"role": "assistant", "content": "PRIVATE_PAYLOAD"}}, known]
    with collect_poll_advisories() as collector, poll_source_context("source"):
        events = []
        for offset, line in enumerate(lines, 1):
            with claude_line_context("root", "synthetic", offset):
                try:
                    events.extend(parse_line_data(line))
                except ValueError as exc:
                    raise AssertionError("KNOWN_SUFFIX_CONTINUES") from exc
        _assert(len(events) == 2, "KNOWN_SUFFIX_CONTINUES")
        _assert([event.payload["usage"] for event in events] == [{"input_tokens": 3, "output_tokens": 4, "model": "fixture-model"}] * 2, "KNOWN_SUFFIX_USAGE_UNCHANGED")
        _assert(collector.snapshot() == (UnknownLineAdvisory("source", "synthetic", "synthetic-unknown-line-type", 1),), "UNKNOWN_GROUP_EXACT")


def test_groups() -> None:
    """Count physical lines once, with independent source/session/type groups."""
    with collect_poll_advisories() as collector:
        for source, session, kind, offset in [("a", "one", "new", 10), ("a", "one", "new", 10), ("a", "one", "new", 20), ("a", "one", "other", 30), ("a", "two", "new", 10), ("b", "one", "new", 10)]:
            with poll_source_context(source), claude_line_context("root", session, offset):
                _assert(list(parse_line_data({"type": kind})) == [], "UNKNOWN_NO_EVENTS")
        with poll_source_context("a"), claude_line_context("root", "one", 40):
            for kind in _SKIP_LINE_TYPES:
                _assert(list(parse_line_data({"type": kind})) == [], "METADATA_NO_EVENTS")
        snapshot = collector.snapshot()
        _assert(snapshot == (UnknownLineAdvisory("a", "one", "new", 2), UnknownLineAdvisory("a", "one", "other", 1), UnknownLineAdvisory("a", "two", "new", 1), UnknownLineAdvisory("b", "one", "new", 1)), "PHYSICAL_GROUP_COUNTS")
        report = ImporterReport(0, 0, 0, 0, snapshot)
        try:
            snapshot[0].count = 99  # type: ignore[misc]
        except FrozenInstanceError:
            pass
        else:
            raise AssertionError("ADVISORY_IMMUTABLE")
    _assert(report.unknown_line_advisories == snapshot, "SNAPSHOT_SURVIVES_RESET")
    _assert(ImporterReport(0, 0, 0, 0).unknown_line_advisories == (), "DEFAULT_EMPTY_REPORT")


def test_contexts() -> None:
    """Nested/concurrent invocation state and completed reads cannot escape a poll."""

    def walk(source: str) -> tuple[UnknownLineAdvisory, ...]:
        with collect_poll_advisories() as collector, poll_source_context(source):
            begin_claude_read("root", "session")
            _assert(completed_claude_read("root", "session") is None, "READ_START_CLEARS_COMPLETION")
            with claude_line_context("root", "session", 10):
                list(parse_line_data({"type": "new"}))
            complete_claude_read("root", "session", 10)
            _assert(completed_claude_read("root", "session") == 10, "READ_COMPLETE_OFFSET")
            begin_claude_read("root", "session")
            _assert(completed_claude_read("root", "session") is None, "RETRY_CLEARS_COMPLETION")
            return collector.snapshot()

    with collect_poll_advisories() as outer:
        walk("nested")
        _assert(outer.snapshot() == (), "NESTED_POLL_ISOLATION")
        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(walk, ["left", "right"]))
        _assert(results == [(UnknownLineAdvisory("left", "session", "new", 1),), (UnknownLineAdvisory("right", "session", "new", 1),)], "CONCURRENT_POLL_ISOLATION")
    _assert(walk("replay") == walk("replay"), "NEW_POLL_REPLAY_COUNTS_AGAIN")
    try:
        with collect_poll_advisories(), poll_source_context("error"), claude_line_context("root", "session", 10):
            raise ValueError("synthetic")
    except ValueError:
        pass
    with patch("ananta.llm.session_ledger.poll_advisories.logger.warning") as warning:
        _assert(parse_line('{"type":"new\\nkind","message":{"role":"assistant","content":"PRIVATE_PAYLOAD"}}') == [], "DIRECT_UNKNOWN_SUCCESS")
        warning.assert_called_once_with("claude_code: skipping unknown line type=%r session=%r", "new\nkind", None)
        _assert("PRIVATE_PAYLOAD" not in str(warning.call_args), "NO_PAYLOAD_LOGGED")
    with collect_poll_advisories() as empty:
        _assert(empty.snapshot() == (), "SECOND_EMPTY_POLL_RESET")
    _assert(completed_claude_read("root", "session") is None, "COMPLETION_RESET")


def test_integrity() -> None:
    """Malformed known records and malformed block shapes remain strict."""
    for line in ["{", "[]", "{}", '{"type":3}', '{"type":"assistant"}']:
        try:
            parse_line(line)
        except ValueError:
            pass
        else:
            raise AssertionError("KNOWN_INTEGRITY_ERROR: " + line)
    for block in [None, [], {}, {"type": 3}, {"type": "text", "text": 3}, {"type": "tool_use"}, {"type": "tool_result"}, {"type": "server_tool_use"}]:
        line_data = {"type": "assistant", "sessionId": "s", "timestamp": "2026-10-18T00:00:00Z", "message": {"role": "assistant", "content": [block]}}
        try:
            list(parse_line_data(line_data))
        except ValueError:
            pass
        else:
            raise AssertionError("KNOWN_BLOCK_INTEGRITY_ERROR: " + repr(block))


def test_existing_metadata_types_still_skipped() -> None:
    """Regression: the pre-existing skip-list entries must still skip cleanly."""
    pre_existing = [
        "permission-mode",
        "file-history-snapshot",
        "last-prompt",
        "custom-title",
        "agent-name",
        "bridge-session",
        "ai-title",
        "attachment",
        "summary",
    ]
    for t in pre_existing:
        line = {"type": t, "sessionId": "test"}
        events = list(parse_line_data(line))
        _assert(
            events == [],
            f"regression: pre-existing skip-type {t!r} no longer skips cleanly; got {events!r}",
        )


def main() -> int:
    tests = [
        test_mode_line_yields_zero_events,
        test_queue_operation_line_yields_zero_events,
        test_unknown_timestamp_absent_line_is_tolerated,
        test_unknown_timestamp_present_no_event_shape_is_tolerated,
        test_unknown_suffix,
        test_groups,
        test_contexts,
        test_integrity,
        test_existing_metadata_types_still_skipped,
    ]
    for t in tests:
        t()
        print(f"  ok: {t.__name__}")
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
