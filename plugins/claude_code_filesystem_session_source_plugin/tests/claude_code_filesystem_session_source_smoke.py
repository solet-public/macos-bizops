#!/usr/bin/env python3
"""Smoke test for claude_code_filesystem_session_source_plugin (no pytest).

Coverage:

* Descriptor reports source_kind='claude_code_local', vendor='claude_code',
  supported_modes=('pulling',).
* Parser flattening: one ``.jsonl`` line containing 1 text block + 2
  tool_use blocks yields 1 MESSAGE + 2 TOOL_CALL raw events; one user line
  carrying a tool_result block yields 1 TOOL_RESULT raw event.
* vendor_event_id discipline (spec §17.3 acceptance — load-bearing for
  tool-call projection):
    - MESSAGE: vendor_event_id = line.uuid
    - TOOL_CALL: vendor_event_id = tool_use.id (the toolu_... value);
      vendor_parent_event_id = line.uuid
    - TOOL_RESULT: vendor_event_id = line.uuid;
      vendor_parent_event_id = content.tool_use_id (resolution key)
* Skip-list line types (permission-mode, file-history-snapshot, last-prompt,
  attachment, summary) produce zero events.
* Skip-list content blocks (thinking) are silently dropped without affecting
  surrounding text-block aggregation.
* discover_sessions walks a sandboxed tmpdir, respects the mtime high-water
  cursor, and surfaces ExternalSessionRef per .jsonl file.
* read_events seeks past a non-zero byte offset and stops at the last
  newline-terminated record (partial trailing line stays unconsumed).
* normalize() produces NormalizedSessionEvent shapes that pass
  ``SessionLedgerRepository._validate_event_shape``.
* Cursor producers: discovery (ISO mtime) and event-read (byte offset).
* End-to-end acceptance through ``SessionLedgerImporter`` against a stub
  state_service: TOOL_RESULT projection updates the matching tool_call
  row to ``status='succeeded'``.
* Sandboxing per [[sandbox-mutating-smokes]]: the test config points
  ``root_uri`` at a tmpdir created under ``tempfile`` and never touches
  ``~/.claude``.

Run:
    .venv/bin/python3 plugins/claude_code_filesystem_session_source_plugin/tests/claude_code_filesystem_session_source_smoke.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "claude_code_filesystem_session_source_plugin" / "src"),
)
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from ananta.core.config.config_provider import ConfigProvider  # noqa: E402
from ananta.llm.session_ledger.types import (  # noqa: E402
    EventType,
    ExternalSessionRef,
    IngestMode,
    IngestSourceKind,
    MessageRole,
    SourceVendor,
)
from ananta.llm.session_ledger.vendor.claude_code import parse_line  # noqa: E402
from claude_code_filesystem_session_source_plugin.plugin import (  # noqa: E402
    ClaudeCodeFilesystemSessionSourcePlugin,
)

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


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


def _make_plugin() -> ClaudeCodeFilesystemSessionSourcePlugin:
    plugin = ClaudeCodeFilesystemSessionSourcePlugin()
    # P1.1.E: root is threaded per-call as root_uri; only glob stays in config.
    plugin.set_config_provider(ConfigProvider(plugin.name, {"glob": "*.jsonl"}))
    plugin.prepare_for_readiness()
    return plugin


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


_SID = "01234567-89ab-cdef-0123-456789abcdef"
_USER_UUID = "u" + "1" * 35
_ASSISTANT_TEXT_AND_TOOL_USE_UUID = "a" + "2" * 35
_TOOL_RESULT_USER_UUID = "u" + "3" * 35
_TOOL_USE_ID_A = "toolu_A" + "a" * 17
_TOOL_USE_ID_B = "toolu_B" + "b" * 17


def _line_user_text() -> dict[str, Any]:
    return {
        "parentUuid": None,
        "promptId": "p1",
        "type": "user",
        "message": {"role": "user", "content": "help me debug"},
        "uuid": _USER_UUID,
        "timestamp": "2026-05-30T12:00:00.000Z",
        "cwd": "/Users/alice/Workspace/example",
        "sessionId": _SID,
        "version": "2.1.118",
        "gitBranch": "master",
    }


def _line_assistant_text_and_two_tool_uses() -> dict[str, Any]:
    return {
        "parentUuid": _USER_UUID,
        "type": "assistant",
        "message": {
            "model": "claude-opus-4-7",
            "id": "msg_1",
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "internal", "signature": "x"},
                {"type": "text", "text": "I'll read the file first."},
                {
                    "type": "tool_use",
                    "id": _TOOL_USE_ID_A,
                    "name": "Read",
                    "input": {"file_path": "/tmp/x"},
                },
                {
                    "type": "tool_use",
                    "id": _TOOL_USE_ID_B,
                    "name": "Bash",
                    "input": {"command": "ls"},
                },
            ],
        },
        "uuid": _ASSISTANT_TEXT_AND_TOOL_USE_UUID,
        "timestamp": "2026-05-30T12:00:01.000Z",
        "cwd": "/Users/alice/Workspace/example",
        "sessionId": _SID,
    }


def _line_user_tool_result_for_a() -> dict[str, Any]:
    return {
        "parentUuid": _ASSISTANT_TEXT_AND_TOOL_USE_UUID,
        "type": "user",
        "message": {
            "role": "user",
            "content": [
                {
                    "tool_use_id": _TOOL_USE_ID_A,
                    "type": "tool_result",
                    "content": [{"type": "text", "text": "file body"}],
                }
            ],
        },
        "uuid": _TOOL_RESULT_USER_UUID,
        "timestamp": "2026-05-30T12:00:02.000Z",
        "cwd": "/Users/alice/Workspace/example",
        "sessionId": _SID,
    }


def _line_permission_mode() -> dict[str, Any]:
    return {
        "type": "permission-mode",
        "permissionMode": "bypassPermissions",
        "sessionId": _SID,
    }


def _line_file_history() -> dict[str, Any]:
    return {
        "type": "file-history-snapshot",
        "messageId": "x",
        "snapshot": {
            "messageId": "x",
            "trackedFileBackups": {},
            "timestamp": "2026-05-30T12:00:00.000Z",
        },
        "isSnapshotUpdate": False,
    }


# ---------------------------------------------------------------------------
# Descriptor
# ---------------------------------------------------------------------------


def test_descriptor() -> None:
    plugin = _make_plugin()  # not exercising filesystem
    desc = plugin.describe()
    _check(
        desc.source_kind is IngestSourceKind.CLAUDE_CODE_LOCAL,
        "descriptor source_kind = claude_code_local",
    )
    _check(desc.vendor is SourceVendor.CLAUDE_CODE, "descriptor vendor = claude_code")
    _check(
        desc.supported_modes == (IngestMode.PULLING,),
        "descriptor supported_modes = (pulling,)",
    )


# ---------------------------------------------------------------------------
# Vendor parser
# ---------------------------------------------------------------------------


def test_parser_user_text_line() -> None:
    events = parse_line(json.dumps(_line_user_text()))
    _check(len(events) == 1, "user text line → 1 raw event")
    e = events[0]
    _check(e.payload.get("kind") == "message", "kind = message")
    _check(e.payload.get("role") == "user", "role = user")
    _check(e.payload.get("text") == "help me debug", "text body preserved")
    _check(e.vendor_event_id == _USER_UUID, "vendor_event_id = line.uuid")
    _check(e.vendor_parent_event_id is None, "first user message has no parent")


def test_parser_assistant_text_and_tool_uses_flatten() -> None:
    events = parse_line(json.dumps(_line_assistant_text_and_two_tool_uses()))
    _check(len(events) == 3, "1 text + 2 tool_use blocks → 3 raw events")
    kinds = [e.payload.get("kind") for e in events]
    _check(
        kinds.count("tool_call") == 2 and kinds.count("message") == 1,
        "2 tool_call + 1 message events emitted",
    )
    text_event = next(e for e in events if e.payload.get("kind") == "message")
    _check(
        text_event.payload.get("text") == "I'll read the file first.",
        "assistant text body preserved",
    )
    tool_calls = [e for e in events if e.payload.get("kind") == "tool_call"]
    _check(
        {tc.vendor_event_id for tc in tool_calls} == {_TOOL_USE_ID_A, _TOOL_USE_ID_B},
        "TOOL_CALL vendor_event_id = tool_use.id for both blocks (load-bearing for projection)",
    )
    _check(
        all(tc.vendor_parent_event_id == _ASSISTANT_TEXT_AND_TOOL_USE_UUID for tc in tool_calls),
        "TOOL_CALL vendor_parent_event_id = line.uuid",
    )


def test_parser_tool_result_event() -> None:
    events = parse_line(json.dumps(_line_user_tool_result_for_a()))
    _check(len(events) == 1, "user tool_result line → 1 raw event")
    e = events[0]
    _check(e.payload.get("kind") == "tool_result", "kind = tool_result")
    _check(
        e.vendor_event_id == _TOOL_RESULT_USER_UUID,
        "TOOL_RESULT vendor_event_id = line.uuid",
    )
    _check(
        e.vendor_parent_event_id == _TOOL_USE_ID_A,
        "TOOL_RESULT vendor_parent_event_id = content.tool_use_id (load-bearing for resolution)",
    )
    _check(e.payload.get("text") == "file body", "tool result body preserved")


def test_parser_skips_session_config_noise() -> None:
    for line in (_line_permission_mode(), _line_file_history()):
        _check(
            parse_line(json.dumps(line)) == [],
            f"skip-listed type {line.get('type')!r} → 0 events",
        )


def test_parser_skips_thinking_blocks_in_isolation() -> None:
    line: dict[str, Any] = {
        "parentUuid": None,
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "internal", "signature": "sig"},
            ],
        },
        "uuid": "a" + "9" * 35,
        "timestamp": "2026-05-30T12:00:00.000Z",
        "sessionId": _SID,
    }
    _check(parse_line(json.dumps(line)) == [], "thinking-only line → 0 events")


def test_parser_advises_on_unknown_block_kind() -> None:
    """Exercise the actual filesystem poll report, retaining strict known blocks."""
    import importer_smoke as runtime  # pyright: ignore[reportMissingImports]
    from ananta.llm.session_ledger.types import UnknownLineAdvisory

    line: dict[str, Any] = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "before"},
                {"type": "synthetic-future-block-2099", "private": "PRIVATE_PAYLOAD"},
                {"type": "text", "text": "after"},
            ],
        },
        "uuid": "a" + "8" * 35,
        "timestamp": "2026-05-30T12:00:00.000Z",
        "sessionId": _SID,
    }
    with tempfile.TemporaryDirectory(prefix="unknown_block_poll_") as td:
        root = Path(td)
        _write_jsonl(root / "synthetic" / (_SID + ".jsonl"), [_line_user_text(), line, _line_assistant_text_and_two_tool_uses()])
        importer, _, _ = runtime.runtime_importer(_make_plugin(), str(root))
        report = importer.poll_once()
        _check(report.batches_failed == 0 and report.events_persisted == 5, "unknown block: real poll preserves prefix and suffix with zero errors")
        _check(report.unknown_line_advisories == (UnknownLineAdvisory("runtime-source", _SID, "synthetic-future-block-2099", 1),), "unknown block: real poll report retains exact kind/session count")
    line["message"]["content"] = [{"type": "text", "text": 123}]
    try:
        parse_line(json.dumps(line))
    except ValueError as exc:
        _check("text block" in str(exc), "malformed known block remains strict")
    else:
        _check(False, "malformed known block must raise")


def test_parser_raises_on_invalid_json() -> None:
    try:
        parse_line('{"not": "closed"')
    except ValueError as exc:
        _check("valid JSON" in str(exc), "malformed JSON raises ValueError")
        return
    _check(False, "expected ValueError on malformed JSON")


# ---------------------------------------------------------------------------
# normalize() — repository shape validation
# ---------------------------------------------------------------------------


def test_normalize_message_user() -> None:
    plugin = _make_plugin()
    raw = parse_line(json.dumps(_line_user_text()))[0]
    n = plugin.normalize(raw)
    _check(n.event_type is EventType.MESSAGE, "MESSAGE event")
    _check(n.role is MessageRole.USER, "role mapped to USER")
    _check(n.content_text == "help me debug", "content_text preserved")
    _check(n.vendor_event_id == _USER_UUID, "vendor_event_id preserved")


def test_normalize_tool_call_carries_tool_name() -> None:
    plugin = _make_plugin()
    events = parse_line(json.dumps(_line_assistant_text_and_two_tool_uses()))
    tool_call_raw = next(e for e in events if e.payload.get("kind") == "tool_call")
    n = plugin.normalize(tool_call_raw)
    _check(n.event_type is EventType.TOOL_CALL, "TOOL_CALL event")
    _check(n.content_text is None, "TOOL_CALL has no content_text (repo validator)")
    _check(
        isinstance(n.content_json, dict)
        and n.content_json.get("tool_name")
        in {
            "Read",
            "Bash",
        },
        "content_json carries tool_name (importer's _extract_tool_name reads this)",
    )


def test_normalize_tool_result_round_trips_parent_link() -> None:
    plugin = _make_plugin()
    raw = parse_line(json.dumps(_line_user_tool_result_for_a()))[0]
    n = plugin.normalize(raw)
    _check(n.event_type is EventType.TOOL_RESULT, "TOOL_RESULT event")
    _check(
        n.vendor_parent_event_id == _TOOL_USE_ID_A,
        "vendor_parent_event_id round-trips for projection resolution",
    )
    _check(n.role is MessageRole.TOOL, "TOOL_RESULT role = TOOL")


# ---------------------------------------------------------------------------
# Filesystem walking + cursors
# ---------------------------------------------------------------------------


def _write_jsonl(target: Path, lines: Sequence[Mapping[str, object]]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("w", encoding="utf-8") as fh:
        for line in lines:
            fh.write(json.dumps(line) + "\n")


def test_discover_and_read_events_in_tmpdir() -> None:
    with tempfile.TemporaryDirectory(prefix="claude_code_smoke_") as tmp:
        root = Path(tmp)
        encoded_proj = root / "-Users-alice-Workspace-example"
        jsonl = encoded_proj / f"{_SID}.jsonl"
        _write_jsonl(
            jsonl,
            [
                _line_user_text(),
                _line_assistant_text_and_two_tool_uses(),
                _line_user_tool_result_for_a(),
            ],
        )
        plugin = _make_plugin()
        refs = list(plugin.discover_sessions(str(root), cursor_payload=None))
        _check(len(refs) == 1, "discover_sessions surfaced 1 fixture session")
        ref = refs[0]
        _check(ref.external_session_id == _SID, "session id matches filename stem")
        _check(
            ref.project_path == "/Users/alice/Workspace/example",
            "project_path decoded from directory name",
        )
        raw_events = list(plugin.read_events(str(root), ref, cursor_payload=None))
        # 1 user-text MESSAGE + 1 assistant-text MESSAGE + 2 TOOL_CALL +
        # 1 TOOL_RESULT = 5 events
        _check(len(raw_events) == 5, f"flattened to 5 raw events (got {len(raw_events)})")
        # event_read_cursor must carry the per-file byte offset
        last_cursor = plugin.event_read_cursor(str(root), ref, raw_events[-1])
        byte_offset = last_cursor.get("byte_offset")
        _check(
            isinstance(byte_offset, int) and byte_offset > 0,
            "event_read_cursor returns positive byte_offset after a full read",
        )


def test_discover_high_water_skips_unchanged_files() -> None:
    with tempfile.TemporaryDirectory(prefix="claude_code_smoke_") as tmp:
        root = Path(tmp)
        encoded_proj = root / "-Users-alice-Workspace-example"
        jsonl = encoded_proj / f"{_SID}.jsonl"
        _write_jsonl(jsonl, [_line_user_text()])
        # Set mtime explicitly to the past so we can craft a future high-water.
        past = datetime(2026, 5, 30, 11, 0, tzinfo=UTC).timestamp()
        os.utime(jsonl, (past, past))
        plugin = _make_plugin()
        future_iso = datetime(2026, 5, 30, 13, 0, tzinfo=UTC).isoformat()
        refs = list(plugin.discover_sessions(str(root), cursor_payload={"mtime_high_water_iso": future_iso}))
        _check(
            refs == [],
            "high-water mtime in the future suppresses the unchanged fixture",
        )


def test_read_events_resumes_from_offset() -> None:
    with tempfile.TemporaryDirectory(prefix="claude_code_smoke_") as tmp:
        root = Path(tmp)
        encoded_proj = root / "-Users-alice-Workspace-example"
        jsonl = encoded_proj / f"{_SID}.jsonl"
        _write_jsonl(jsonl, [_line_user_text(), _line_assistant_text_and_two_tool_uses()])
        plugin = _make_plugin()
        ref = ExternalSessionRef(
            external_session_id=_SID,
            vendor_session_label=None,
            project_path="/Users/alice/Workspace/example",
            first_seen_at=datetime.now(UTC),
        )
        # First pass: read everything and capture the post-final offset.
        all_events = list(plugin.read_events(str(root), ref, cursor_payload=None))
        final_offset = plugin.event_read_cursor(str(root), ref, all_events[-1])["byte_offset"]
        # Second pass: resume from final offset — should yield zero new events.
        replay = list(plugin.read_events(str(root), ref, cursor_payload={"byte_offset": final_offset}))
        _check(
            replay == [],
            "resuming at the final offset yields zero new events (idempotent re-poll)",
        )


def test_read_events_stops_at_partial_trailing_line() -> None:
    with tempfile.TemporaryDirectory(prefix="claude_code_smoke_") as tmp:
        root = Path(tmp)
        encoded_proj = root / "-Users-alice-Workspace-example"
        jsonl = encoded_proj / f"{_SID}.jsonl"
        _write_jsonl(jsonl, [_line_user_text()])
        # Append a half-line without a trailing newline.
        with jsonl.open("ab") as fh:
            fh.write(b'{"type":"user","message":{')
        plugin = _make_plugin()
        ref = ExternalSessionRef(
            external_session_id=_SID,
            vendor_session_label=None,
            project_path="/Users/alice/Workspace/example",
            first_seen_at=datetime.now(UTC),
        )
        events = list(plugin.read_events(str(root), ref, cursor_payload=None))
        _check(len(events) == 1, "consumed the one full line; left half-line unread")


class _QualificationLedger:
    def __init__(
        self,
        *,
        root_uri: str,
        registered: bool = True,
        events_persisted: int = 4,
    ) -> None:
        self.registered = registered
        self.root_uri = root_uri
        self.events_persisted = events_persisted
        self.polled_source_id: str | None = None

    def list_sources(self) -> dict[str, object]:
        rows: list[object] = []
        if self.registered:
            rows.append(
                {
                    "source_kind": "claude_code_local",
                    "source_id": "src-claude-fixture",
                    "enabled": True,
                    "root_uri": self.root_uri,
                }
            )
        return {"sources": rows}

    def poll_source(self, source_id: str) -> dict[str, int]:
        self.polled_source_id = source_id
        return {"events_persisted": self.events_persisted}

    def list_sessions(self, **kwargs: object) -> dict[str, object]:
        return {"sessions": [{"session_id": "les-claude-fixture"}]}

    def get_session_timeline(self, **kwargs: object) -> dict[str, object]:
        return {"events": [{"content_text": "fixture only"}]}


class _QualificationOrchestrator:
    def __init__(self, ledger: _QualificationLedger) -> None:
        self._ledger = ledger

    def get_service(self, name: str) -> _QualificationLedger | None:
        return self._ledger if name == "session_ledger_service" else None


def test_qualify_is_source_local_and_content_blind() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "fixture-session.jsonl").write_text("fixture only\n", encoding="utf-8")
        ledger = _QualificationLedger(root_uri=tmp)
        plugin = ClaudeCodeFilesystemSessionSourcePlugin()
        plugin.orchestrator_ref = _QualificationOrchestrator(ledger)  # type: ignore[assignment]
        result = plugin.qualify({}, {})
        _check(result["action_status"] == "completed", "qualify completes for fixture source")
        _check(ledger.polled_source_id == "src-claude-fixture", "qualify polls its registered source")
        _check(
            result["data"]
            == {
                "source_registered": True,
                "source_kind": "claude_code_local",
                "backfill_count": 4,
                "sample_content_retrieved": True,
            },
            "qualify returns the bounded proof shape without sample payload",
        )


def test_qualify_allows_absent_registered_root() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = _QualificationLedger(root_uri=str(Path(tmp) / "never-created"), events_persisted=0)
        plugin = ClaudeCodeFilesystemSessionSourcePlugin()
        plugin.orchestrator_ref = _QualificationOrchestrator(ledger)  # type: ignore[assignment]
        result = plugin.qualify({}, {})
    _check(result["action_status"] == "completed", "qualify accepts an absent registered root")
    _check(ledger.polled_source_id is None, "absent root bypasses the impossible backfill")
    _check(
        result["data"]
        == {
            "source_registered": True,
            "source_kind": "claude_code_local",
            "backfill_count": 0,
            "sample_content_retrieved": False,
        },
        "absent root returns an honest empty proof",
    )


def test_qualify_refuses_existing_empty_root() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = _QualificationLedger(root_uri=tmp, events_persisted=0)
        plugin = ClaudeCodeFilesystemSessionSourcePlugin()
        plugin.orchestrator_ref = _QualificationOrchestrator(ledger)  # type: ignore[assignment]
        result = plugin.qualify({}, {})
    _check(result["action_status"] == "failed", "qualify refuses an existing empty root")
    _check(ledger.polled_source_id == "src-claude-fixture", "existing root still runs backfill")


def test_qualify_refuses_unregistered_source() -> None:
    plugin = ClaudeCodeFilesystemSessionSourcePlugin()
    plugin.orchestrator_ref = _QualificationOrchestrator(  # type: ignore[assignment]
        _QualificationLedger(root_uri="/unused", registered=False)
    )
    result = plugin.qualify({}, {})
    _check(result["action_status"] == "failed", "qualify fails without a registered source")
    _check(result["data"] == {}, "failed qualification does not fabricate proof fields")


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


def _poll_case(root: Path, kind: str, plugin_name: str, fixture_name: str) -> Any:
    """Bind a real adapter fixture to a synthetic root and typed runtime.

    All source observations and durable cursors stay in this case."""
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ananta" / "tests" / "llm" / "session_ledger"))
    import importer_smoke as runtime  # pyright: ignore[reportMissingImports]
    from ananta.llm.session_ledger import types
    from ananta.llm.session_ledger.poll_budget import COMPLETED_VERSION_KEY, DiscoveryBudgetYield

    fixture = runtime._fixture_module(plugin_name, fixture_name)
    return SimpleNamespace(root=root, kind=kind, fixture=fixture, runtime=runtime, types=types, completed_key=COMPLETED_VERSION_KEY, yield_type=DiscoveryBudgetYield, source_key=("runtime-source", types.CursorScope.DISCOVERY, None))


def _poll_fresh(case: Any) -> Any:
    """Recreate the actual adapter using its real ConfigProvider.

    Only the configured glob or filename is supplied; discovery is unchanged."""
    configs = {"codex": {}, "claude": {"glob": "*.jsonl"}, "tasks": {"glob": "*.json"}, "ambient": {"filename": "ambient-suggestions.json"}}
    plugin = case.runtime._legacy_plugin(case.kind, case.fixture, case.root)
    plugin.initialize(configs[case.kind])
    return plugin


def _poll_seed(case: Any, distribution: str) -> None:
    """Create 53 valid sessions with tied or deliberately unordered mtimes.

    The future-dated legacy cursor must reopen rather than exclude them."""
    import os

    case.paths = [case.runtime._legacy_path(case.kind, case.fixture, case.root, index) for index in range(53)]
    for index, path in enumerate(case.paths):
        stamp = 1_700_000_000 + (index % 3 if distribution == "nonmonotonic" else 0)
        os.utime(path, ns=(stamp * 1_000_000_000, stamp * 1_000_000_000))
    plugin = _poll_fresh(case)
    assert plugin.supports_whole_session_poll, "real filesystem adapter must activate complete polling hooks"
    case.expected = []
    for ref in plugin.discover_sessions(str(case.root), None):
        case.expected.extend(plugin.normalize(raw) for raw in plugin.read_events(str(case.root), ref, None))
    case.order = [item.external_id for item in sorted(plugin._poll_candidates(str(case.root)), key=lambda item: item.pair, reverse=True)]
    _, case.repo, case.state = case.runtime.runtime_importer(plugin, str(case.root))
    case.repo.cursors[case.source_key] = {"mtime_high_water_iso": "2099-01-01T00:00:00+00:00", "file_mtime_iso": "2099-01-01T00:00:00+00:00", "highest_filename_stem": "zzzz"}
    case.observed, case.visited = [], []


def _poll_cut_visits(case: Any) -> None:
    """Recreate plugin and importer after every one-session deadline cut.

    Normalization advances only admission time; the admitted session finishes."""
    from ananta.llm.session_ledger.types import NormalizedSessionEvent, RawSessionEvent

    for _ in range(55):
        plugin = _poll_fresh(case)
        clock = [0.0]
        normalize = plugin.normalize

        def timed(raw: RawSessionEvent, normalize_current: Any = normalize, clock_current: list[float] = clock) -> NormalizedSessionEvent:
            normalized = normalize_current(raw)
            case.observed.append(normalized)
            if not case.visited or case.visited[-1] != raw.external_session_id:
                case.visited.append(raw.external_session_id)
            clock_current[0] = 61.0
            return normalized

        plugin.normalize = timed
        importer, _, _ = case.runtime.runtime_importer(plugin, str(case.root), repository=case.repo, clock=lambda clock=clock: clock[0])
        report = importer.poll_once()
        assert report.batches_failed == 0, "valid actual filesystem fixture must poll successfully"
        assert report.sessions_seen <= 1, "admission stops only after one complete actual session"
        cursor = case.repo.cursors[case.source_key]
        if report.sessions_seen:
            assert cursor["state"] == "backfilling", "deadline is pending, never EOF/caught_up"
        if cursor["state"] == "caught_up":
            return
    raise AssertionError("pair-resume must reach EOF and reset, not strand oldest anchor")


def _poll_assert_coverage(case: Any) -> None:
    """Compare ordered surviving paths and every successful EOF marker.

    This distinguishes a completed sweep from an accidentally exhausted suffix."""
    assert case.visited == case.order, "equal/nonmonotonic pair restart visits every surviving path in exact newest order"
    assert case.repo.cursors[case.source_key]["after"] is None, "actual EOF clears the after key"
    completed = [key for key, value in case.repo.cursors.items() if key[1] is case.types.CursorScope.EVENT_READ and case.completed_key in value]
    assert len(completed) == 53, "each whole successful version has one completed cursor"


def _poll_assert_events(case: Any) -> None:
    """Compare all normalized fields and existing vendor/derived identities.

    The expected stream is an uninterrupted read of the identical real inputs."""
    from collections import Counter

    from ananta.llm.session_ledger.importer_persistence import _event_external_id

    assert Counter(map(repr, case.observed)) == Counter(map(repr, case.expected)), "whole-session restart preserves exact vendor events, usage and model data"
    rows = [row.record for row in case.state.upserts if row.table == "event"]
    assert len(rows) == len(case.expected), "restart preserves exact event counts"
    # Derivation uses the actual persisted session ID and unchanged whole-walk
    # occurrence order, including sources without vendor-supplied IDs.
    ordinals = {}
    for row, normalized in zip(rows, case.observed, strict=True):
        derived = _event_external_id(normalized=normalized, session_id=row["session_id"], ordinals=ordinals)
        assert row["external_id"] == derived, "derived/vendor external IDs remain unchanged"


def _poll_prefix(case: Any) -> None:
    """Expire during an unchanged prefix that yields no session references.

    Its examined metadata must be acknowledged before resuming below the pair."""
    reads = [0]
    progress = case.types.PollDiscoveryProgress()

    def read_cursor(key: str) -> dict[str, object] | None:
        reads[0] += 1
        return case.repo.read_cursor(source_id="runtime-source", scope=case.types.CursorScope.EVENT_READ, scope_key=key)

    fresh = _poll_fresh(case)
    try:
        list(fresh.discover_sessions_for_poll(str(case.root), None, read_event_cursor=read_cursor, can_admit_next=lambda: reads[0] < 5, progress=progress))
    except case.yield_type:
        pass
    else:
        raise AssertionError("completed-prefix deadline must yield, not claim EOF")
    assert reads[0] == 5 and progress.last_examined is not None, "metadata prefix advances before budget yield"
    partial = fresh.completed_discovery_cursor(str(case.root), None, exhausted=False, pending=True, progress=progress)
    assert partial["state"] == "backfilling" and partial["after"] is not None, "budget stop preserves pending pair rather than EOF"
    assert not list(fresh.discover_sessions_for_poll(str(case.root), partial, read_event_cursor=read_cursor, can_admit_next=lambda: True, progress=case.types.PollDiscoveryProgress()))
    assert reads[0] == 53, "restart resumes strictly below the metadata prefix including path ties"


def _poll_append_old(case: Any) -> None:
    """Add a genuine event to an old version without changing parser rules.

    Snapshot sources replay their new population using existing stable IDs."""
    old = case.paths[0]
    if case.kind == "codex":
        line = case.fixture._basic_rollout_lines()[1]
        line["payload"]["content"][0]["text"] = "second-sweep"
        old.write_text(old.read_text() + json.dumps(line) + "\n")
    elif case.kind == "claude":
        line = case.fixture._line_user_text()
        line["uuid"] = "second-sweep"
        line["sessionId"] = old.stem
        old.write_text(old.read_text() + json.dumps(line) + "\n")
    elif case.kind == "tasks":
        case.fixture._write_task(old.with_name("0.json"), "second-sweep")
    else:
        obj = json.loads(old.read_text())
        obj["suggestions"].append({**obj["suggestions"][0], "id": "second-sweep"})
        old.write_text(json.dumps(obj))


def _poll_second_sweep(case: Any) -> None:
    """Observe old appended content and an inserted non-head session.

    A previous caught_up result is not permission to stop enumeration."""
    import os

    _poll_append_old(case)
    os.utime(case.paths[0], ns=(1_600_000_000_000_000_000, 1_600_000_000_000_000_000))
    case.added = case.runtime._legacy_path(case.kind, case.fixture, case.root, 99)
    os.utime(case.added, ns=(1_600_000_001_000_000_000, 1_600_000_001_000_000_000))
    case.importer, _, _ = case.runtime.runtime_importer(_poll_fresh(case), str(case.root), repository=case.repo)
    second = case.importer.poll_once()
    assert second.sessions_seen == 2, "second full sweep sees old append and new non-head file"
    assert any("second-sweep" in str(row.record) for row in case.state.upserts if row.table == "event"), "second sweep imports actual old-append event, not only metadata"
    assert case.repo.cursors[case.source_key] == {"version": 1, "state": "caught_up", "after": None}


def _poll_fail_reset(case: Any) -> None:
    """Inject cursor-write failure, retry it, then model confirmed reset.

    The previous durable version survives failure; reset reopens all sessions."""
    previous = dict(case.repo.cursors)
    case.added.write_bytes(case.added.read_bytes() + (b"\n" if case.kind in ("codex", "claude") else b" "))
    case.repo.fail_cursor = True
    case.importer.poll_once()
    assert case.repo.cursors == previous, "failed event/source writes preserve prior durable versions"
    case.repo.fail_cursor = False
    case.importer.poll_once()
    assert case.repo.cursors != previous, "retry corrects the changed completed version"
    case.repo.cursors.clear()
    replay = case.importer.poll_once()
    assert replay.sessions_seen == 54, "confirmed cursor reset reopens the full population"


def _poll_churn(case: Any) -> None:
    """Replace a visited inode, delete unseen content and insert a new head.

    Comparison boundaries survive deletion; the next reset includes changes."""
    import os

    paths = [case.runtime._legacy_path(case.kind, case.fixture, case.root, index) for index in range(4)]
    for path in paths:
        os.utime(path, ns=(1_700_000_000_000_000_000, 1_700_000_000_000_000_000))
    plugin = _poll_fresh(case)
    importer, repo, _ = case.runtime.runtime_importer(plugin, str(case.root), clock=case.runtime.expire_plugin_clock(plugin))
    importer.poll_once()
    cursor = dict(repo.cursors[case.source_key])
    assert cursor["state"] == "backfilling" and cursor["after"] is not None, "cut retains a tie-safe comparison boundary"
    replacement = paths[3].with_name(paths[3].name + ".replacement")
    replacement.write_bytes(paths[3].read_bytes())
    replacement.replace(paths[3])
    paths[0].unlink()
    added = case.runtime._legacy_path(case.kind, case.fixture, case.root, 99)
    os.utime(added, ns=(1_800_000_000_000_000_000, 1_800_000_000_000_000_000))
    for _ in range(2):
        plugin = _poll_fresh(case)
        importer, _, _ = case.runtime.runtime_importer(plugin, str(case.root), repository=repo)
        importer.poll_once()
    for item in plugin._poll_candidates(str(case.root)):
        saved = repo.read_cursor(source_id="runtime-source", scope=case.types.CursorScope.EVENT_READ, scope_key=item.external_id)
        assert saved is not None and saved.get(case.completed_key) == item.version, "next reset sweep includes surviving replacement/new versions above saved key"
    assert repo.cursors[case.source_key]["after"] is None


def _poll_has_marker(case: Any) -> bool:
    """Read successful version publication from the existing event cursors."""
    return any(case.completed_key in value for key, value in case.repo.cursors.items() if key[1] is case.types.CursorScope.EVENT_READ)


def _poll_partial(case: Any) -> None:
    """Finish a trailing partial input after a fresh importer restart.

    Parser return alone cannot certify a physically incomplete version."""
    path = case.runtime._legacy_path(case.kind, case.fixture, case.root, 0)
    complete = path.read_bytes()
    path.write_bytes(complete[:-2])
    importer, case.repo, _ = case.runtime.runtime_importer(_poll_fresh(case), str(case.root))
    importer.poll_once()
    assert not _poll_has_marker(case), "partial physical record must never receive a completed fingerprint"
    assert case.repo.cursors[case.source_key]["state"] == "backfilling", "incomplete version keeps regular sweep pending"
    path.write_bytes(complete)
    importer, _, _ = case.runtime.runtime_importer(_poll_fresh(case), str(case.root), repository=case.repo)
    importer.poll_once()
    assert _poll_has_marker(case), "later-completed record reopens and reaches successful EOF"


def test_resumable_filesystem_poll() -> None:
    """Real metadata/normalizer/restart coverage; no discovery substitution."""
    for distribution in ("equal", "nonmonotonic"):
        with tempfile.TemporaryDirectory(prefix="resumable_claude_") as scratch:
            root = Path(scratch) / "sessions"
            root.mkdir()
            case = _poll_case(root, "claude", "claude_code_filesystem_session_source_plugin", "claude_code_filesystem_session_source_smoke.py")
            _poll_seed(case, distribution)
            _poll_cut_visits(case)
            _poll_assert_coverage(case)
            _poll_assert_events(case)
            _poll_prefix(case)
            _poll_second_sweep(case)
            _poll_fail_reset(case)
    for scenario in ("churn", "partial", "empty"):
        with tempfile.TemporaryDirectory(prefix=scenario + "_claude_") as scratch:
            root = Path(scratch) / "sessions"
            root.mkdir()
            case = _poll_case(root, "claude", "claude_code_filesystem_session_source_plugin", "claude_code_filesystem_session_source_smoke.py")
            if scenario == "churn":
                _poll_churn(case)
            elif scenario == "partial":
                _poll_partial(case)
            else:
                importer, repo, _ = case.runtime.runtime_importer(_poll_fresh(case), str(root))
                assert importer.poll_once().sessions_seen == 0
                assert repo.cursors[case.source_key] == {"version": 1, "state": "caught_up", "after": None}, "empty/no-ref EOF resets state"
    print("PASS real claude pair/version/restart/EOF/reset coverage")


def _oversized_setup(case: Any) -> None:
    """Build equal-size inputs and grow one strictly above the test threshold.

    Both remain actual parseable vendor inputs, never a fake source adapter."""
    small = case.runtime._legacy_path(case.kind, case.fixture, case.root, 0)
    large = case.runtime._legacy_path(case.kind, case.fixture, case.root, 1)
    case.threshold = small.stat().st_size
    assert large.stat().st_size == case.threshold
    large.write_bytes(large.read_bytes() + (b"\n" if case.kind in ("codex", "claude") else b" "))
    case.large_key = large.stem if case.kind in ("codex", "claude") else large.parent.name
    case.plugin = _poll_fresh(case)
    case.importer, case.repo, _ = case.runtime.runtime_importer(case.plugin, str(case.root))
    case.reads = []


def _oversized_deferral(case: Any) -> None:
    """Prove equality stays regular and deferred bytes do not prevent caught_up.

    Deferral publishes diagnostics without recording false event completion."""
    progress = case.types.PollDiscoveryProgress()
    refs = list(case.plugin.discover_sessions_for_poll(str(case.root), None, read_event_cursor=lambda key: None, can_admit_next=lambda: True, progress=progress))
    assert len(refs) == 1 and refs[0].aggregate_bytes == case.threshold, "equal threshold remains regular; strictly greater defers"
    assert progress.deferred_count == 1 and progress.deferred_bytes == case.threshold + 1, "deferral names/counts exact aggregate bytes"
    assert case.importer.poll_once().sessions_seen == 1
    assert case.repo.cursors[case.source_key]["state"] == "caught_up", "regular EOF reaches caught_up despite oversized backlog"
    assert ("runtime-source", case.types.CursorScope.EVENT_READ, case.large_key) not in case.repo.cursors, "deferral never stamps completion"


def _oversized_fresh_regular(case: Any) -> None:
    """Insert regular work before the authoritative leased recheck.

    F1 may invalidate only state, preserving version and the existing boundary."""
    from collections.abc import Iterator

    case.runtime._legacy_path(case.kind, case.fixture, case.root, 2)
    original = dict(case.repo.cursors[case.source_key])
    case.repo.cursors[case.source_key] = dict(original)
    read_events = case.plugin.read_events

    def track(root_uri: str, ref: Any, cursor: dict[str, object] | None) -> Iterator[Any]:
        case.reads.append(ref.external_session_id)
        yield from read_events(root_uri, ref, cursor)

    case.plugin.read_events = track
    case.repo.trace.clear()
    case.importer.poll_one_oversized()
    assert not case.reads, "fresh regular pending must forbid every oversized read"
    assert case.repo.cursors[case.source_key] == {**original, "state": "backfilling"}, "leased F1 invalidation changes only state preserving version/after"
    assert case.repo.trace[0] == "acquire" and case.repo.trace.index("acquire") < case.repo.trace.index("write:discovery"), "source lease precedes authoritative selection and writes"
    case.importer.poll_once()
    assert case.reads and case.repo.cursors[case.source_key]["state"] == "caught_up", "next normal producer imports fresh regular work"
    case.reads.clear()


def _oversized_refusal_success(case: Any) -> None:
    """Contrast lease contention with a successful selected whole-session read.

    The worker must leave the regular discovery cursor byte-identical."""
    before = json.dumps(case.repo.cursors[case.source_key], sort_keys=True)
    case.repo.refuse = True
    case.repo.trace.clear()
    case.importer.poll_one_oversized()
    assert case.repo.trace == ["acquire"] and not case.reads, "contended real provider performs no source/event reads or writes"
    case.repo.refuse = False
    case.importer.poll_one_oversized()
    assert case.reads == [case.large_key], "eventual oversized attempt reads exactly one selected real session"
    assert case.repo.cursors["runtime-source", case.types.CursorScope.EVENT_READ, case.large_key][case.completed_key], "eventual oversized completion stores EOF fingerprint"
    assert json.dumps(case.repo.cursors[case.source_key], sort_keys=True) == before, "successful oversized completion leaves regular cursor byte-identical"
    assert not case.repo.held, "real oversized provider releases source lease"


def test_oversized_provider_policy() -> None:
    """Reach real regular caught_up, then exercise A's leased provider recheck."""
    from unittest.mock import patch

    from ananta.llm.session_ledger import filesystem_poll as policy
    from ananta.llm.session_ledger import importer as importer_mod

    with tempfile.TemporaryDirectory(prefix="large_policy_claude_") as scratch:
        root = Path(scratch) / "sessions"
        root.mkdir()
        case = _poll_case(root, "claude", "claude_code_filesystem_session_source_plugin", "claude_code_filesystem_session_source_smoke.py")
        _oversized_setup(case)
        with patch.object(policy, "OVERSIZED_SESSION_BYTES", case.threshold), patch.object(importer_mod, "OVERSIZED_SESSION_BYTES", case.threshold):
            _oversized_deferral(case)
            _oversized_fresh_regular(case)
            _oversized_refusal_success(case)
    print("PASS real claude threshold/regular-pending/leased-oversized coverage")


def test_unknown_offsets() -> None:
    """Completed physical bytes include unknown/metadata input, never partial tails."""
    from ananta.llm.session_ledger.poll_advisories import collect_poll_advisories, poll_source_context
    from ananta.llm.session_ledger.types import UnknownLineAdvisory

    with tempfile.TemporaryDirectory(prefix="unknown_offsets_") as tmp:
        root = Path(tmp)
        path = root / "synthetic" / f"{_SID}.jsonl"
        unknown = {"type": "synthetic-unknown-line-type", "message": {"role": "assistant", "content": "PRIVATE_PAYLOAD"}}
        _write_jsonl(path, [_line_assistant_text_and_two_tool_uses(), unknown, unknown])
        plugin = _make_plugin()
        ref = list(plugin.discover_sessions(str(root), None))[0]
        with collect_poll_advisories() as collector, poll_source_context("source"):
            events = list(plugin.read_events(str(root), ref, None))
            cursor = plugin.event_read_cursor(str(root), ref, events[-1])
            assert len(events) == 3, "MULTI_EVENT_KNOWN_LINE_PRESERVED"
            assert collector.snapshot() == (UnknownLineAdvisory("source", _SID, "synthetic-unknown-line-type", 2),), "PHYSICAL_UNKNOWN_LINES_NOT_EVENTS"
            assert cursor == {"byte_offset": path.stat().st_size}, "TRAILING_UNKNOWN_EOF_OFFSET"
            assert list(plugin.read_events(str(root), ref, cursor)) == [] and collector.snapshot()[0].count == 2, "RESUMED_READ_SKIPS_CONSUMED_UNKNOWNS"
        for lines in ([unknown], [{"type": "summary"}], []):
            _write_jsonl(path, lines)
            with collect_poll_advisories(), poll_source_context("source"):
                assert list(plugin.read_events(str(root), ref, None)) == [], "NO_EVENT_INPUT"
                assert plugin.event_read_cursor(str(root), ref, None) == {"byte_offset": path.stat().st_size}, "ALL_UNKNOWN_METADATA_EMPTY_CURSOR"
    _check(True, "unknown and metadata completion offsets")


def test_unknown_partial_tail() -> None:
    """Complete an appended partial line without rereading prior unknown bytes."""
    from ananta.llm.session_ledger.poll_advisories import collect_poll_advisories, poll_source_context

    with tempfile.TemporaryDirectory(prefix="unknown_partial_") as tmp:
        root = Path(tmp)
        path = root / "synthetic" / f"{_SID}.jsonl"
        plugin = _make_plugin()
        unknown = {"type": "synthetic-unknown-line-type"}
        _write_jsonl(path, [unknown])
        ref = list(plugin.discover_sessions(str(root), None))[0]
        _write_jsonl(path, [unknown])
        complete = path.stat().st_size
        partial = json.dumps(_line_user_text()).encode()
        with path.open("ab") as handle:
            handle.write(partial)
        with collect_poll_advisories(), poll_source_context("source"):
            assert list(plugin.read_events(str(root), ref, None)) == []
            cursor = plugin.event_read_cursor(str(root), ref, None)
            assert cursor == {"byte_offset": complete}, "PARTIAL_BYTES_STAY_REACHABLE"
        with path.open("ab") as handle:
            handle.write(b"\n")
        with collect_poll_advisories() as collector, poll_source_context("source"):
            events = list(plugin.read_events(str(root), ref, cursor))
            assert len(events) == 1 and collector.snapshot() == (), "COMPLETED_PARTIAL_IMPORTS_ONCE"
            assert plugin.event_read_cursor(str(root), ref, events[-1]) == {"byte_offset": path.stat().st_size}, "APPEND_AFTER_EOF_CURSOR"
    _check(True, "unknown and metadata completion offsets preserve partial tails")


def test_unknown_read_failure() -> None:
    """Missing files, malformed records and closed generators publish no completion."""
    from ananta.llm.session_ledger.poll_advisories import collect_poll_advisories, completed_claude_read, poll_source_context

    with tempfile.TemporaryDirectory(prefix="unknown_failure_") as tmp:
        root = Path(tmp)
        path = root / "synthetic" / f"{_SID}.jsonl"
        _write_jsonl(path, [_line_assistant_text_and_two_tool_uses(), {"type": "assistant"}])
        plugin = _make_plugin()
        ref = list(plugin.discover_sessions(str(root), None))[0]
        with collect_poll_advisories(), poll_source_context("source"):
            iterator = plugin.read_events(str(root), ref, None)
            next(iterator)
            iterator.close()
            assert completed_claude_read(str(root), _SID) is None, "CLOSED_GENERATOR_NO_COMPLETION"
            try:
                list(plugin.read_events(str(root), ref, None))
            except ValueError:
                pass
            else:
                raise AssertionError("MALFORMED_KNOWN_READ_MUST_RAISE")
            assert completed_claude_read(str(root), _SID) is None, "FAILED_READ_NO_COMPLETION"
            path.unlink()  # synthetic fixture only
            assert list(plugin.read_events(str(root), ref, None)) == [], "MISSING_READ_EMPTY"
            assert completed_claude_read(str(root), _SID) is None, "MISSING_FILE_NO_COMPLETION"
    _check(True, "failed and truncated reads never publish successful offsets")


def test_unknown_failed_persistence() -> None:
    """A read-ahead known line must not checkpoint past an unpersisted event."""
    from unittest.mock import patch

    import importer_smoke as runtime  # pyright: ignore[reportMissingImports]
    from ananta.llm.session_ledger.importer import SessionLedgerImporter
    from ananta.llm.session_ledger.repository import LedgerRepositoryError
    from ananta.llm.session_ledger.types import CursorScope

    fixture = runtime._fixture_module("claude_code_filesystem_session_source_plugin", "claude_code_filesystem_session_source_smoke.py")
    with tempfile.TemporaryDirectory(prefix="advisory_persist_failure_") as td:
        root = Path(td)
        path = root / "synthetic" / (fixture._SID + ".jsonl")
        fixture._write_jsonl(path, [fixture._line_user_text(), {"type": "synthetic-unknown-line-type"}, fixture._line_assistant_text_and_two_tool_uses()])
        plugin = fixture._make_plugin()
        importer, repo, _ = runtime.runtime_importer(plugin, str(root))
        persist = SessionLedgerImporter._persist_normalized_for_session
        reached: list[str | None] = []

        def fail_suffix(self: SessionLedgerImporter, **kwargs: Any) -> int:
            event = kwargs["normalized"]
            reached.append(event.vendor_event_id)
            if event.role is MessageRole.ASSISTANT:
                raise LedgerRepositoryError("synthetic suffix persist failure")
            return persist(self, **kwargs)

        with patch.object(SessionLedgerImporter, "_persist_normalized_for_session", fail_suffix):
            importer.poll_once()
        assert len(reached) == 2, "FAILED_SUFFIX_PERSIST_PATH_REACHED"
        assert ("runtime-source", CursorScope.EVENT_READ, fixture._SID) not in repo.cursors, "FAILED_PERSIST_NO_READAHEAD_CHECKPOINT"
    _check(True, "persistence failure leaves complete suffix reachable")


def _assert_oversized_unknown_append(importer: Any, repo: Any, importer_mod: Any, path: Path, unknown: Mapping[str, object]) -> None:
    """Append eligibility must resume beyond both previously consumed unknowns."""
    from unittest.mock import patch

    from ananta.llm.session_ledger.types import CursorScope, UnknownLineAdvisory

    with path.open("a") as stream:
        stream.write(json.dumps({**_line_user_text(), "uuid": "appended-known"}) + "\n")
        stream.write(json.dumps(unknown) + "\n")
    with patch.object(importer_mod.logger, "info") as log:
        importer.poll_one_oversized()
        after = [call.args[1] for call in log.call_args_list if call.args[0] == "ledger oversized DRAIN complete: unknown_line_advisories=%s"]
    assert repo.cursors["runtime-source", CursorScope.EVENT_READ, _SID]["byte_offset"] == path.stat().st_size, "OVERSIZED_APPENDED_COMPLETE_EOF"
    assert after == [(UnknownLineAdvisory("runtime-source", _SID, "synthetic-unknown-line-type", 1),)], "OVERSIZED_APPEND_DOES_NOT_REREAD_UNKNOWN"
    assert not repo.held_sources, "OVERSIZED_ADVISORY_SCOPE_RELEASES_LEASE"


def test_oversized_unknown_offsets() -> None:
    """The independent worker checkpoints consumed unknown bytes before append.

    Both an all-unknown input and a known-prefix/trailing-unknown input are
    admitted through actual regular discovery, deferred by the test threshold,
    then read through poll_one_oversized. No private collector is installed by
    this test: the worker must establish its own invocation scope. Its next
    eligible append must report only the newly appended unknown physical line.
    """
    from unittest.mock import patch

    import importer_smoke as runtime  # pyright: ignore[reportMissingImports]
    from ananta.llm.session_ledger import filesystem_poll as policy
    from ananta.llm.session_ledger import importer as importer_mod
    from ananta.llm.session_ledger.types import CursorScope, UnknownLineAdvisory

    for known_prefix in (False, True):
        with tempfile.TemporaryDirectory(prefix="oversized_unknown_") as tmp:
            root = Path(tmp)
            path = root / "synthetic" / f"{_SID}.jsonl"
            unknown = {"type": "synthetic-unknown-line-type", "message": {"role": "assistant"}}
            _write_jsonl(path, ([_line_user_text()] if known_prefix else []) + [unknown, unknown])
            plugin = _make_plugin()
            importer, repo, _ = runtime.runtime_importer(plugin, str(root))
            with patch.object(policy, "OVERSIZED_SESSION_BYTES", 1), patch.object(importer_mod, "OVERSIZED_SESSION_BYTES", 1):
                assert importer.poll_once().events_persisted == 0, "OVERSIZED_REGULAR_DEFERS"
                with patch.object(importer_mod.logger, "info") as log:
                    importer.poll_one_oversized()
                    first = [call.args[1] for call in log.call_args_list if call.args[0] == "ledger oversized DRAIN complete: unknown_line_advisories=%s"]
                cursor = repo.cursors["runtime-source", CursorScope.EVENT_READ, _SID]
                assert cursor.get("byte_offset") == path.stat().st_size, "OVERSIZED_CONSUMED_UNKNOWN_EOF"
                assert first == [(UnknownLineAdvisory("runtime-source", _SID, "synthetic-unknown-line-type", 1),)], "OVERSIZED_EXACT_INITIAL_ADVISORY"
                _assert_oversized_unknown_append(importer, repo, importer_mod, path, unknown)
    _check(True, "oversized unknown suffix and unknown-only reads checkpoint consumed bytes")


def main() -> int:
    test_oversized_unknown_offsets()
    test_unknown_failed_persistence()
    test_unknown_offsets()
    test_unknown_partial_tail()
    test_unknown_read_failure()
    test_resumable_filesystem_poll()
    test_oversized_provider_policy()
    print("=== claude_code_filesystem_session_source_smoke ===")
    test_descriptor()
    test_parser_user_text_line()
    test_parser_assistant_text_and_tool_uses_flatten()
    test_parser_tool_result_event()
    test_parser_skips_session_config_noise()
    test_parser_skips_thinking_blocks_in_isolation()
    test_parser_advises_on_unknown_block_kind()
    test_parser_raises_on_invalid_json()
    test_normalize_message_user()
    test_normalize_tool_call_carries_tool_name()
    test_normalize_tool_result_round_trips_parent_link()
    test_discover_and_read_events_in_tmpdir()
    test_discover_high_water_skips_unchanged_files()
    test_read_events_resumes_from_offset()
    test_read_events_stops_at_partial_trailing_line()
    test_qualify_is_source_local_and_content_blind()
    test_qualify_allows_absent_registered_root()
    test_qualify_refuses_existing_empty_root()
    test_qualify_refuses_unregistered_source()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
