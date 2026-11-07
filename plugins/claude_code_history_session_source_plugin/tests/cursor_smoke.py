"""M7 §1.5 — cursor advance + idempotent re-read smoke.

Sandbox: writes a tmp history.jsonl, exercises the plugin's
discover_sessions twice. First pass yields all sessions; second pass with
the advanced cursor yields nothing (idempotent re-read).
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(_REPO_ROOT / "plugins" / "claude_code_history_session_source_plugin" / "src"),
)

from claude_code_history_session_source_plugin.plugin import (  # noqa: E402
    ClaudeCodeHistorySessionSourcePlugin,
)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _write_lines(path: Path, rows: list[dict[str, object]]) -> None:
    with path.open("ab") as f:
        for r in rows:
            f.write((json.dumps(r) + "\n").encode("utf-8"))


def _verify_whole_history(plugin: ClaudeCodeHistorySessionSourcePlugin, history: Path, report: Any, repository: Any, state: Any, population: int, total: int) -> dict[str, object]:
    """Verify full EOF and stable vendor IDs before exercising a fresh reader."""
    from ananta.llm.session_ledger.types import CursorScope

    assert report.sessions_seen == population, "NON-YIELDING actual history must finish every session"
    assert report.events_persisted == total, "NON-YIELDING history must finish >1000-line session"
    assert report.batches_failed == 0
    cursor = repository.cursors["runtime-source", CursorScope.DISCOVERY, None]
    assert cursor["byte_offset"] == history.stat().st_size, "history discovery checkpoint is full-file EOF only"
    expected_ids = {raw.vendor_event_id for ref in plugin.discover_sessions(str(history), None) for raw in plugin.read_events(str(history), ref, None)}
    actual_ids = {w.record["vendor_event_id"] for w in state.upserts if w.table == "event"}
    assert actual_ids == expected_ids, "history IDs match uninterrupted baseline"
    return cursor


def whole_file_importer_deadline_regression() -> None:
    """Actual 3/51-session files stay whole despite an expired cooperative clock."""
    sys.path.insert(0, str(_REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))
    from importer_smoke import expire_plugin_clock, runtime_importer

    for population in (51, 3):
        with tempfile.TemporaryDirectory(prefix="whole_history_") as td:
            history = Path(td) / "history.jsonl"
            rows = []
            for index in range(1001 + population - 1):
                key = "session-0" if index < 1001 else "session-" + str(index - 1000)
                rows.append({"sessionId": key, "timestamp": 1_700_000_000_000 + index, "display": "synthetic", "project": "/synthetic"})
            history.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
            plugin = ClaudeCodeHistorySessionSourcePlugin()
            plugin.prepare_for_readiness()
            importer, repository, state = runtime_importer(plugin, str(history), clock=expire_plugin_clock(plugin))
            report = importer.poll_once()
            cursor = _verify_whole_history(plugin, history, report, repository, state, population, len(rows))
            fresh = ClaudeCodeHistorySessionSourcePlugin()
            assert not list(fresh.discover_sessions(str(history), cursor)), "restart leaves no unread history sessions"
            with history.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps({"sessionId": "append", "timestamp": 1_700_000_100_000, "display": "append", "project": "/synthetic"}) + "\n")
            restarted, _, _ = runtime_importer(fresh, str(history), repository=repository)
            appended_report = restarted.poll_once()
            assert appended_report.sessions_seen == 1 and appended_report.events_persisted == 1, "restart imports appended history without loss"
    print("PASS actual whole-file 3/51-session deadline regression")


def main() -> int:
    whole_file_importer_deadline_regression()
    with tempfile.TemporaryDirectory() as td:
        history = Path(td) / "history.jsonl"
        # Initial three lines, two unique sessions.
        _write_lines(
            history,
            [
                {"display": "a1", "timestamp": 1_700_000_000_000, "sessionId": "uuid-a", "project": "/p"},
                {"display": "a2", "timestamp": 1_700_000_001_000, "sessionId": "uuid-a", "project": "/p"},
                {"display": "b1", "timestamp": 1_700_000_002_000, "sessionId": "uuid-b", "project": "/p"},
            ],
        )
        plugin = ClaudeCodeHistorySessionSourcePlugin()
        # P1.1.E: root is threaded per-call as root_uri, not read from config.
        root_uri = str(history)

        # Pass 1: cold start (cursor=None) — must yield 2 sessions.
        sessions_first = list(plugin.discover_sessions(root_uri, None))
        _assert(
            {s.external_session_id for s in sessions_first} == {"uuid-a", "uuid-b"},
            f"pass1 sessions: {[s.external_session_id for s in sessions_first]}",
        )
        events_a = list(plugin.read_events(root_uri, sessions_first[0], None))
        events_b = list(plugin.read_events(root_uri, sessions_first[1], None))
        total_first_pass = len(events_a) + len(events_b)
        _assert(total_first_pass == 3, f"expected 3 events across both sessions, got {total_first_pass}")
        cursor_after_first = plugin.session_discovery_cursor(root_uri, sessions_first[-1])
        _assert(
            "byte_offset" in cursor_after_first,
            f"cursor must carry byte_offset, got {cursor_after_first}",
        )
        offset = cursor_after_first["byte_offset"]
        _assert(isinstance(offset, int) and offset == history.stat().st_size, f"cursor offset {offset} should equal file size {history.stat().st_size}")

        # Pass 2: re-scan with advanced cursor — must yield nothing.
        sessions_second = list(plugin.discover_sessions(root_uri, cursor_after_first))
        _assert(
            len(sessions_second) == 0,
            f"pass2 (no new bytes) should yield nothing; got {len(sessions_second)} sessions",
        )

        # Pass 3: append one new line, re-scan from cursor — must yield the
        # new session only.
        _write_lines(
            history,
            [{"display": "c1", "timestamp": 1_700_000_003_000, "sessionId": "uuid-c", "project": "/p"}],
        )
        sessions_third = list(plugin.discover_sessions(root_uri, cursor_after_first))
        _assert(
            [s.external_session_id for s in sessions_third] == ["uuid-c"],
            f"pass3 should yield only uuid-c, got {[s.external_session_id for s in sessions_third]}",
        )
        events_c = list(plugin.read_events(root_uri, sessions_third[0], None))
        _assert(len(events_c) == 1, f"expected 1 event for uuid-c, got {len(events_c)}")
        _assert(events_c[0].payload["display"] == "c1", events_c[0].payload["display"])

    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
