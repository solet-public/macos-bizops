#!/usr/bin/env python3
"""Regression smoke locking the 2026-05-31 claude_code vendor-drift fix.

Watchdog tag: ``dispatch:in_flight:claude_code_timestamp_vendor_fix``.

Symptom (pre-fix, from ``profile/data/logs/2026-05-31_profile.log``):

::

    WARNING - ledger poll: skipping session 471a05a5-d18c-4dc0-87c3-...
      (claude_code_local) on ValueError:
      claude_code: line missing non-empty string 'timestamp'

Three live sessions (``471a05a5...``, ``95072955...``, ``d6add7e5...``)
all had the same failure. Inspection of the real ``~/.claude/projects/``
jsonl files showed 4 session-metadata line types were NOT in
:data:`_SKIP_LINE_TYPES`:

* ``custom-title``   — operator-set custom title (``/rename`` slash command)
* ``agent-name``     — agent role label (e.g. ``"Claude-A"``)
* ``bridge-session`` — bridge-session id pinning
* ``ai-title``       — model-generated summary title (broader scan only)

None carry a ``timestamp`` field; they're session-config records, not
events. Pre-fix, parsing fell through ``_LINE_TYPE_USER`` / ``_LINE_TYPE_
ASSISTANT`` to :meth:`_LineContext.from_data` which called
:func:`_parse_timestamp` and raised ``ValueError`` on the missing field.
The per-session try/except in :class:`SessionLedgerImporter` swallowed
the error, the discovery cursor advanced regardless (``last_session_ref``
is set BEFORE the try block), and three same-day sessions silently
orphaned. Same vendor-drift bug class as the codex
``compacted`` / ``web_search_call`` fix that landed earlier today.

This smoke pins:

1. Every one of the 4 new skipped types parses cleanly when present in a
   real-shaped Claude-Code jsonl line.
2. A realistic ``~/.claude/projects/`` line sequence (mirroring the head
   of ``471a05a5-d18c-4dc0-87c3-e96a02b852ff.jsonl``) parses without
   raising; the conversational events that follow are yielded.
3. The pre-existing skipped types (``permission-mode``,
   ``file-history-snapshot``, ``last-prompt``, ``attachment``,
   ``summary``) keep their behavior — guarding against an accidental
   skip-list trim.

Run::

    .venv/bin/python3 plugins/claude_code_filesystem_session_source_plugin/tests/claude_code_vendor_recency_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.vendor import claude_code  # noqa: E402

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


# ───── Fixture line builders (real Claude Code shapes) ──────────────────────


_SESSION_ID = "471a05a5-d18c-4dc0-87c3-e96a02b852ff"


_NEWLY_SKIPPED_LINES: dict[str, dict[str, object]] = {
    "api-request-blob": {"type": "api-request-blob", "sessionId": _SESSION_ID},
    "api-request": {"type": "api-request", "sessionId": _SESSION_ID},
    "api-request-shape": {"type": "api-request-shape", "sessionId": _SESSION_ID},
    "atis-latch": {"type": "atis-latch", "sessionId": _SESSION_ID},
    "cost-state": {"type": "cost-state", "sessionId": _SESSION_ID},
    "file-history-delta": {"type": "file-history-delta", "sessionId": _SESSION_ID},
    "custom-title": {
        "type": "custom-title",
        "customTitle": "Claude-A",
        "sessionId": _SESSION_ID,
    },
    "agent-name": {
        "type": "agent-name",
        "agentName": "Claude-A",
        "sessionId": _SESSION_ID,
    },
    "bridge-session": {
        "type": "bridge-session",
        "sessionId": _SESSION_ID,
        "bridgeSessionId": "cse_0178ZLiM8pUT8jEVyStUqFFm",
        "lastSequenceNum": 0,
    },
    "ai-title": {
        "type": "ai-title",
        "aiTitle": "Vendor recency fix planning",
        "sessionId": _SESSION_ID,
    },
}


_PREEXISTING_SKIPPED_LINES: dict[str, dict[str, object]] = {
    "permission-mode": {
        "type": "permission-mode",
        "permissionMode": "bypassPermissions",
        "sessionId": _SESSION_ID,
    },
    "file-history-snapshot": {
        "type": "file-history-snapshot",
        "messageId": "msg_demo",
        "snapshot": [],
        "isSnapshotUpdate": False,
    },
    "last-prompt": {
        "type": "last-prompt",
        "leafUuid": "5a84b6b2-8b8f-407b-8fb1-b8ba874db567",
        "sessionId": _SESSION_ID,
    },
    "attachment": {
        "type": "attachment",
        "attachmentId": "att-demo",
    },
    "summary": {
        "type": "summary",
        "summary": "Old session summary",
    },
}


def _user_message_line(uuid: str, parent_uuid: str | None = None) -> dict[str, object]:
    line: dict[str, object] = {
        "type": "user",
        "uuid": uuid,
        "timestamp": "2026-05-31T16:00:00.000Z",
        "cwd": "/Users/alice/Workspace/example",
        "gitBranch": "master",
        "sessionId": _SESSION_ID,
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": "hello after metadata"}],
        },
    }
    if parent_uuid is not None:
        line["parentUuid"] = parent_uuid
    return line


# ───── (1) Each newly-skipped type parses to ZERO events, no ValueError ─────


def test_each_newly_skipped_type_yields_no_events() -> None:
    for type_name, data in _NEWLY_SKIPPED_LINES.items():
        raised: ValueError | None = None
        events: list[object] = []
        with patch.object(claude_code, "record_unknown_line") as record_unknown_line:
            try:
                events = list(claude_code.parse_line_data(dict(data)))
            except ValueError as exc:
                raised = exc
        _check(
            raised is None and events == [],
            f"line type {type_name!r} parses to 0 events without raising (raised={raised!r}, events={len(events)})",
        )
        if type_name in {"api-request-blob", "api-request", "api-request-shape", "atis-latch", "cost-state", "file-history-delta"}:
            _check(not record_unknown_line.called, f"line type {type_name!r} does not record unknown-line advisory")


# ───── (2) Pre-existing skipped types keep their behavior ───────────────────


def test_preexisting_skipped_types_unchanged() -> None:
    for type_name, data in _PREEXISTING_SKIPPED_LINES.items():
        raised: ValueError | None = None
        events: list[object] = []
        try:
            events = list(claude_code.parse_line_data(dict(data)))
        except ValueError as exc:
            raised = exc
        _check(
            raised is None and events == [],
            f"pre-existing skipped type {type_name!r} still yields 0 events without raising (raised={raised!r}, events={len(events)})",
        )


# ───── (3) Realistic 471a05a5 head sequence parses cleanly ──────────────────


def test_realistic_471a05a5_head_sequence_parses_cleanly() -> None:
    """Mirrors the first 9 lines of
    ``~/.claude/projects/-Users-alice-Workspace-example/471a05a5-d18c-4dc0-87c3-e96a02b852ff.jsonl``:

    last-prompt, custom-title, agent-name, permission-mode, bridge-session,
    user message, assistant message, last-prompt, file-history-snapshot.

    Pre-fix, the SECOND line (``custom-title``) raised because it wasn't
    in ``_SKIP_LINE_TYPES`` and the parser fell through to
    ``_parse_timestamp``. The whole session was orphaned; the importer's
    per-session try/except silently swallowed and advanced the cursor.

    Post-fix: every line parses; the 2 conversational lines yield events.
    """
    fixture_lines: list[dict[str, object]] = [
        dict(_PREEXISTING_SKIPPED_LINES["last-prompt"]),
        dict(_NEWLY_SKIPPED_LINES["custom-title"]),
        dict(_NEWLY_SKIPPED_LINES["agent-name"]),
        dict(_PREEXISTING_SKIPPED_LINES["permission-mode"]),
        dict(_NEWLY_SKIPPED_LINES["bridge-session"]),
        _user_message_line("evt-aaaa"),
        {
            "type": "assistant",
            "uuid": "evt-bbbb",
            "parentUuid": "evt-aaaa",
            "timestamp": "2026-05-31T16:00:05.000Z",
            "cwd": "/Users/alice/Workspace/example",
            "gitBranch": "master",
            "sessionId": _SESSION_ID,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Hi back."}],
            },
        },
        dict(_PREEXISTING_SKIPPED_LINES["last-prompt"]),
        dict(_PREEXISTING_SKIPPED_LINES["file-history-snapshot"]),
    ]
    yielded_events: list[object] = []
    raised: ValueError | None = None
    try:
        for line_data in fixture_lines:
            yielded_events.extend(claude_code.parse_line_data(line_data))
    except ValueError as exc:
        raised = exc
    _check(
        raised is None,
        f"realistic 471a05a5 head sequence does not raise (raised={raised!r})",
    )
    _check(
        len(yielded_events) == 2,
        f"exactly 2 conversational events yielded (user + assistant message); got {len(yielded_events)}",
    )


# Unknown top-level types are advisory success, including event-shaped objects.


def test_unknown_types_continue() -> None:
    """Unknown objects need neither a timestamp nor a session id to skip."""
    for extra in ({}, {"message": {"role": "user", "content": "synthetic"}}):
        _check(list(claude_code.parse_line_data({"type": "synthetic-unknown-line-type", **extra})) == [], "unknown type yields zero events")


def test_fallback_and_unknown_blocks() -> None:
    """Vendor control blocks preserve surrounding text, tools and usage."""
    from ananta.llm.session_ledger.poll_advisories import claude_line_context, collect_poll_advisories, poll_source_context
    from ananta.llm.session_ledger.types import UnknownLineAdvisory

    for kind in ("fallback", "synthetic-future-block-2099"):
        line = {
            "type": "assistant",
            "sessionId": _SESSION_ID,
            "uuid": "synthetic-block-line",
            "timestamp": "2026-10-22T00:00:00Z",
            "message": {
                "role": "assistant",
                "model": "fixture-model",
                "usage": {"input_tokens": 3},
                "content": [
                    {"type": "text", "text": "before"},
                    {"type": kind, "from": {"model": "old"}, "to": {"model": "new"}, "private": "PRIVATE_PAYLOAD"},
                    {"type": "text", "text": "after"},
                    {"type": "tool_use", "id": "synthetic-tool", "name": "Read", "input": {}},
                ],
            },
        }
        with collect_poll_advisories() as collector, poll_source_context("source"), claude_line_context("root", _SESSION_ID, 10):
            try:
                events = list(claude_code.parse_line_data(line))
            except ValueError as exc:
                _check(False, f"{kind} must preserve events: {exc}")
                continue
            _check(len(events) == 2 and events[0].payload["text"] == "before\nafter", f"{kind}: text and tool suffix survive")
            _check(events[0].payload["usage"] == {"input_tokens": 3, "model": "fixture-model"}, f"{kind}: usage survives unchanged")
            expected = () if kind == "fallback" else (UnknownLineAdvisory("source", _SESSION_ID, kind, 1),)
            _check(collector.snapshot() == expected, f"{kind}: exact kind/session advisory; fallback silent")


def test_unknown_block_warning_is_safe() -> None:
    """Direct parsing warns with kind/session only, never vendor payload."""
    from unittest.mock import patch

    line = _user_message_line("synthetic-warning")
    line["message"] = {"role": "user", "content": [{"type": "synthetic-future-block-2099", "private": "PRIVATE_PAYLOAD"}]}
    with patch("ananta.llm.session_ledger.poll_advisories.logger.warning") as warning:
        _check(list(claude_code.parse_line_data(line)) == [], "unknown-only block fabricates no events")
        warning.assert_called_once_with("claude_code: skipping unknown line type=%r session=%r", "synthetic-future-block-2099", _SESSION_ID)
        _check("PRIVATE_PAYLOAD" not in str(warning.call_args), "unknown block payload never logged")


# ───── Driver ───────────────────────────────────────────────────────────────


def main() -> int:
    print("=== claude_code_vendor_recency_smoke ===")
    test_each_newly_skipped_type_yields_no_events()
    test_preexisting_skipped_types_unchanged()
    test_realistic_471a05a5_head_sequence_parses_cleanly()
    test_unknown_types_continue()
    test_fallback_and_unknown_blocks()
    test_unknown_block_warning_is_safe()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
