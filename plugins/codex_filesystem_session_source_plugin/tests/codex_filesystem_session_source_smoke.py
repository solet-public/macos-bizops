#!/usr/bin/env python3
"""Smoke test for codex_filesystem_session_source_plugin (no pytest).

Spec §17.2 acceptance: fixture JSONL imports; cursors persist across
restart; ValueError on unrecognized payload type. Per
[[sandbox-mutating-smokes]] the smoke writes its fixture rollouts under
``tempfile.TemporaryDirectory()`` and threads that temp dir as the
per-call ``root_uri`` (P1.1.E) — the plugin resolves it via
``root_uri_to_path``. It NEVER touches ``~/.codex/sessions``.

Run:
    .venv/bin/python3 plugins/codex_filesystem_session_source_plugin/tests/codex_filesystem_session_source_smoke.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(
    0,
    str(REPO_ROOT / "plugins" / "codex_filesystem_session_source_plugin" / "src"),
)

from ananta.llm.session_ledger.types import (  # noqa: E402
    EventType,
    ExternalSessionRef,
    IngestMode,
    IngestSourceKind,
    MessageRole,
    RawSessionEvent,
    SourceVendor,
)
from codex_filesystem_session_source_plugin.plugin import (  # noqa: E402
    CodexFilesystemSessionSourcePlugin,
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


# ─── Fixture rollout writers ────────────────────────────────────────────────


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
        for line in lines:
            fh.write(json.dumps(line) + "\n")
    return path


def _basic_rollout_lines(
    session_uuid: str = "0199e828-1e95-71e1-af26-c5b8eb723703",
) -> list[dict[str, object]]:
    return [
        {
            "timestamp": "2025-10-15T13:56:07.504Z",
            "type": "session_meta",
            "payload": {
                "id": session_uuid,
                "timestamp": "2025-10-15T13:56:07.445Z",
                "cwd": "/proj/x",
                "originator": "codex_cli_rs",
                "cli_version": "0.45.0",
            },
        },
        {
            "timestamp": "2025-10-15T13:56:08.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": "hello"}],
            },
        },
        # Known-skip lines — must NOT raise:
        {
            "timestamp": "2025-10-15T13:56:09.000Z",
            "type": "event_msg",
            "payload": {"type": "token_count", "info": None},
        },
        {
            "timestamp": "2025-10-15T13:56:10.000Z",
            "type": "turn_context",
            "payload": {"cwd": "/proj/x"},
        },
        {
            "timestamp": "2025-10-15T13:56:11.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "shell",
                "arguments": '{"command":["bash","-lc","ls"]}',
                "call_id": "call_abc",
            },
        },
        {
            "timestamp": "2025-10-15T13:56:12.000Z",
            "type": "response_item",
            "payload": {
                "type": "function_call_output",
                "call_id": "call_abc",
                "output": "AGENTS.md\\nREADME.md\\n",
            },
        },
        {
            "timestamp": "2025-10-15T13:56:13.000Z",
            "type": "response_item",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "world"}],
            },
        },
    ]


def _make_plugin(root_dir: Path) -> tuple[CodexFilesystemSessionSourcePlugin, str]:
    """Build the plugin + the ``root_uri`` to thread into every pulling call.

    P1.1.E: the walk root is the polled ``__source`` row's ``root_uri`` (here a
    bare temp-dir path, which ``root_uri_to_path`` accepts), NOT a plugin-private
    ``_root_dir`` singleton. Returns ``(plugin, root_uri)`` so the caller passes
    the same root to ``discover_sessions`` / ``read_events`` / cursors.
    """
    plugin = CodexFilesystemSessionSourcePlugin()
    plugin.set_ready()
    return plugin, str(root_dir)


# ─── Descriptor ─────────────────────────────────────────────────────────────


def test_descriptor() -> None:
    plugin = CodexFilesystemSessionSourcePlugin()
    desc = plugin.describe()
    _check(
        desc.source_kind is IngestSourceKind.CODEX_LOCAL,
        "descriptor source_kind = codex_local",
    )
    _check(desc.vendor is SourceVendor.CODEX, "descriptor vendor = codex")
    _check(
        desc.supported_modes == (IngestMode.PULLING,),
        "descriptor supported_modes = (pulling,)",
    )


# ─── discover_sessions ──────────────────────────────────────────────────────


def test_discover_sessions_yields_known_files() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path_a = _write_rollout(
            root,
            date_subpath="2025/10/15",
            filename="rollout-2025-10-15T06-56-07-0199e828-aaaa.jsonl",
            lines=_basic_rollout_lines(session_uuid="aaaa-uuid"),
        )
        path_b = _write_rollout(
            root,
            date_subpath="2025/10/16",
            filename="rollout-2025-10-16T08-00-00-0199e828-bbbb.jsonl",
            lines=_basic_rollout_lines(session_uuid="bbbb-uuid"),
        )
        plugin, root_uri = _make_plugin(root)
        refs = list(plugin.discover_sessions(root_uri, cursor_payload=None))
        _check(len(refs) == 2, "discover yields 2 refs over 2 rollouts")
        _check(
            refs[0].external_session_id == path_a.stem and refs[1].external_session_id == path_b.stem,
            "refs are emitted in time-monotonic filename order",
        )
        _check(
            refs[0].vendor_session_label == "aaaa-uuid" and refs[0].project_path == "/proj/x",
            "session_meta UUID + cwd propagate to ExternalSessionRef",
        )


def test_discover_sessions_rescans_old_stems() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_rollout(
            root,
            date_subpath="2025/10/15",
            filename="rollout-2025-10-15T06-56-07-0199e828-aaaa.jsonl",
            lines=_basic_rollout_lines("aaaa-uuid"),
        )
        _write_rollout(
            root,
            date_subpath="2025/10/16",
            filename="rollout-2025-10-16T08-00-00-0199e828-bbbb.jsonl",
            lines=_basic_rollout_lines("bbbb-uuid"),
        )
        plugin, root_uri = _make_plugin(root)
        cursor = {"highest_filename_stem": "rollout-2025-10-15T06-56-07-0199e828-aaaa"}
        refs = list(plugin.discover_sessions(root_uri, cursor_payload=cursor))
        _check(
            [ref.external_session_id for ref in refs] == ["rollout-2025-10-15T06-56-07-0199e828-aaaa", "rollout-2025-10-16T08-00-00-0199e828-bbbb"],
            "legacy discovery cursor retains old and newer rollouts",
        )
        _check(list(plugin.discover_sessions(root_uri, {"mode": "full_rescan"})) == refs, "full rescan mode retains the complete sorted corpus")


# ─── read_events ────────────────────────────────────────────────────────────


def test_read_events_skips_session_meta_and_noise() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = _write_rollout(
            root,
            date_subpath="2025/10/15",
            filename="rollout-2025-10-15T06-56-07-0199e828-aaaa.jsonl",
            lines=_basic_rollout_lines("aaaa-uuid"),
        )
        plugin, root_uri = _make_plugin(root)
        ref = ExternalSessionRef(
            external_session_id=path.stem,
            vendor_session_label="aaaa-uuid",
            project_path="/proj/x",
            first_seen_at=plugin.discover_sessions(root_uri, None).__next__().first_seen_at,
        )
        raws = list(plugin.read_events(root_uri, ref, cursor_payload=None))
        # 7 input lines: session_meta + 2 messages + event_msg (skip) +
        # turn_context (skip) + function_call + function_call_output = 4 events
        _check(len(raws) == 4, f"4 ledger events emitted; got {len(raws)}")
        _check(
            all(isinstance(r, RawSessionEvent) for r in raws),
            "all yielded items are RawSessionEvent",
        )
        _check(
            all("_codex_item_type" in r.payload for r in raws),
            "every emitted event carries _codex_item_type marker",
        )


def test_read_events_resume_from_line_offset() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = _write_rollout(
            root,
            date_subpath="2025/10/15",
            filename="rollout-2025-10-15T06-56-07-0199e828-aaaa.jsonl",
            lines=_basic_rollout_lines("aaaa-uuid"),
        )
        plugin, root_uri = _make_plugin(root)
        ref = ExternalSessionRef(
            external_session_id=path.stem,
            vendor_session_label="aaaa-uuid",
            project_path="/proj/x",
            first_seen_at=plugin.discover_sessions(root_uri, None).__next__().first_seen_at,
        )
        # First pass: read everything, capture the last line_offset
        all_events = list(plugin.read_events(root_uri, ref, cursor_payload=None))
        last_offset = all_events[-1].payload["_codex_line_offset"]
        # Use the plugin's own cursor producer
        next_cursor = plugin.event_read_cursor(root_uri, ref, all_events[-1])
        _check(
            next_cursor == {"line_offset": int(last_offset) + 1},  # type: ignore[arg-type]
            "event_read_cursor advances past last consumed line",
        )
        # Resume from the cursor: should yield NO events (we already read them)
        resumed = list(plugin.read_events(root_uri, ref, cursor_payload=next_cursor))
        _check(
            len(resumed) == 0,
            "resuming after exhausting the file yields no further events",
        )


def test_read_events_resume_partial_yields_remainder() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = _write_rollout(
            root,
            date_subpath="2025/10/15",
            filename="rollout-2025-10-15T06-56-07-0199e828-aaaa.jsonl",
            lines=_basic_rollout_lines("aaaa-uuid"),
        )
        plugin, root_uri = _make_plugin(root)
        ref = ExternalSessionRef(
            external_session_id=path.stem,
            vendor_session_label="aaaa-uuid",
            project_path="/proj/x",
            first_seen_at=plugin.discover_sessions(root_uri, None).__next__().first_seen_at,
        )
        # Read just the first event, then resume; the rest must arrive.
        all_events = list(plugin.read_events(root_uri, ref, cursor_payload=None))
        cursor_after_first = plugin.event_read_cursor(root_uri, ref, all_events[0])
        remainder = list(plugin.read_events(root_uri, ref, cursor_payload=cursor_after_first))
        _check(
            len(all_events) == 4 and len(remainder) == 3,
            "resume after first event surfaces remaining 3 events",
        )


# ─── normalize ──────────────────────────────────────────────────────────────


def test_normalize_message_role_mapping() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = _write_rollout(
            root,
            date_subpath="2025/10/15",
            filename="rollout-2025-10-15T06-56-07-0199e828-aaaa.jsonl",
            lines=_basic_rollout_lines("aaaa-uuid"),
        )
        plugin, root_uri = _make_plugin(root)
        ref = ExternalSessionRef(
            external_session_id=path.stem,
            vendor_session_label="aaaa-uuid",
            project_path="/proj/x",
            first_seen_at=plugin.discover_sessions(root_uri, None).__next__().first_seen_at,
        )
        normalized = [plugin.normalize(r) for r in plugin.read_events(root_uri, ref, None)]
        types = [n.event_type for n in normalized]
        _check(
            types
            == [
                EventType.MESSAGE,
                EventType.TOOL_CALL,
                EventType.TOOL_RESULT,
                EventType.MESSAGE,
            ],
            f"event types in order: {[t.value for t in types]}",
        )
        _check(normalized[0].role is MessageRole.USER, "first message → MessageRole.USER")
        _check(
            normalized[-1].role is MessageRole.ASSISTANT,
            "last message → MessageRole.ASSISTANT",
        )
        _check(
            normalized[1].content_json is not None and normalized[1].content_json["tool_name"] == "shell",
            "TOOL_CALL carries tool_name + tool_call_id + arguments",
        )
        _check(
            normalized[2].vendor_parent_event_id == "call_abc",
            "TOOL_RESULT parent points at TOOL_CALL.call_id",
        )


def test_tool_call_and_result_have_distinct_vendor_event_ids() -> None:
    """Per-vendor-event uniqueness: CALL.vendor_event_id != RESULT.vendor_event_id.

    Codex's wire format reuses call_id on both function_call and
    function_call_output lines; the parser synthesizes a ``:result``
    suffix on RESULT raw events so they don't collide. Linkage stays in
    vendor_parent_event_id.
    """
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        path = _write_rollout(
            root,
            date_subpath="2025/10/15",
            filename="rollout-2025-10-15T06-56-07-0199e828-aaaa.jsonl",
            lines=_basic_rollout_lines("aaaa-uuid"),
        )
        plugin, root_uri = _make_plugin(root)
        ref = ExternalSessionRef(
            external_session_id=path.stem,
            vendor_session_label="aaaa-uuid",
            project_path="/proj/x",
            first_seen_at=plugin.discover_sessions(root_uri, None).__next__().first_seen_at,
        )
        raws = list(plugin.read_events(root_uri, ref, None))
        call_raw = next(r for r in raws if r.payload.get("_codex_item_type") == "function_call")
        result_raw = next(r for r in raws if r.payload.get("_codex_item_type") == "function_call_output")
        _check(
            call_raw.vendor_event_id == "call_abc",
            f"CALL.vendor_event_id == call_id; got {call_raw.vendor_event_id!r}",
        )
        _check(
            result_raw.vendor_event_id == "call_abc:result",
            f"RESULT.vendor_event_id == '<call_id>:result'; got {result_raw.vendor_event_id!r}",
        )
        _check(
            call_raw.vendor_event_id != result_raw.vendor_event_id,
            "CALL.vendor_event_id != RESULT.vendor_event_id (per-vendor-event uniqueness)",
        )
        _check(
            result_raw.vendor_parent_event_id == "call_abc",
            "RESULT.vendor_parent_event_id still points at the parent CALL's call_id",
        )
        # Normalize both and verify normalization preserves the distinction.
        call_norm = plugin.normalize(call_raw)
        result_norm = plugin.normalize(result_raw)
        _check(
            call_norm.vendor_event_id == "call_abc" and result_norm.vendor_event_id == "call_abc:result",
            "normalized event ids preserve the CALL/RESULT distinction",
        )


# ─── ValueError on unrecognized line type ───────────────────────────────────


def test_unrecognized_top_level_type_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bad = _basic_rollout_lines("aaaa-uuid")
        bad.append(
            {
                "timestamp": "2025-10-15T13:57:00.000Z",
                "type": "totally_unknown_codex_line_type",
                "payload": {},
            }
        )
        path = _write_rollout(
            root,
            date_subpath="2025/10/15",
            filename="rollout-2025-10-15T06-56-07-0199e828-aaaa.jsonl",
            lines=bad,
        )
        plugin, root_uri = _make_plugin(root)
        ref = ExternalSessionRef(
            external_session_id=path.stem,
            vendor_session_label="aaaa-uuid",
            project_path="/proj/x",
            first_seen_at=plugin.discover_sessions(root_uri, None).__next__().first_seen_at,
        )
        try:
            list(plugin.read_events(root_uri, ref, cursor_payload=None))
        except ValueError as exc:
            _check(
                "totally_unknown_codex_line_type" in str(exc),
                "ValueError on unrecognized top-level line type",
            )
            return
        _check(False, "expected ValueError on unrecognized top-level type")


def test_unrecognized_response_item_type_raises() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        bad = _basic_rollout_lines("aaaa-uuid")
        bad.append(
            {
                "timestamp": "2025-10-15T13:57:00.000Z",
                "type": "response_item",
                "payload": {"type": "brand_new_unknown_subtype"},
            }
        )
        path = _write_rollout(
            root,
            date_subpath="2025/10/15",
            filename="rollout-2025-10-15T06-56-07-0199e828-aaaa.jsonl",
            lines=bad,
        )
        plugin, root_uri = _make_plugin(root)
        ref = ExternalSessionRef(
            external_session_id=path.stem,
            vendor_session_label="aaaa-uuid",
            project_path="/proj/x",
            first_seen_at=plugin.discover_sessions(root_uri, None).__next__().first_seen_at,
        )
        try:
            list(plugin.read_events(root_uri, ref, cursor_payload=None))
        except ValueError as exc:
            _check(
                "brand_new_unknown_subtype" in str(exc),
                "ValueError on unrecognized response_item.payload.type",
            )
            return
        _check(False, "expected ValueError on unrecognized response_item type")


# ─── Cursor producers ───────────────────────────────────────────────────────


def test_session_discovery_cursor_round_trip() -> None:
    plugin = CodexFilesystemSessionSourcePlugin()
    # session_discovery_cursor derives the cursor purely from last_seen; the
    # P1.1.E root_uri is unused here, so any path-shaped value satisfies it.
    root_uri = "/tmp"
    ref = ExternalSessionRef(
        external_session_id="rollout-2025-10-15T06-56-07-uuid",
        vendor_session_label="uuid",
        project_path=None,
        first_seen_at=plugin_first_dt(),
    )
    cursor = plugin.session_discovery_cursor(root_uri, ref)
    _check(
        cursor == {"mode": "full_rescan"},
        "discovery cursor records full rescan mode",
    )
    null_cursor = plugin.session_discovery_cursor(root_uri, None)
    _check(
        null_cursor == {"mode": "full_rescan"},
        "discovery cursor retains full rescan mode for empty corpus",
    )


def plugin_first_dt():  # type: ignore[no-untyped-def]
    from datetime import UTC
    from datetime import datetime as _dt

    return _dt(2025, 10, 15, 6, 56, 7, tzinfo=UTC)


# ─── Archived sessions sibling ──────────────────────────────────────────────


def test_archived_sessions_sibling_is_walked() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # Codex's layout: <CODEX_HOME>/sessions/  and  <CODEX_HOME>/archived_sessions/
        codex_home = Path(tmp)
        sessions_root = codex_home / "sessions"
        archived_root = codex_home / "archived_sessions"
        sessions_root.mkdir(parents=True)
        archived_root.mkdir(parents=True)
        # Live rollout under sessions/YYYY/MM/DD
        _write_rollout(
            sessions_root,
            date_subpath="2025/10/15",
            filename="rollout-2025-10-15T06-56-07-0199e828-aaaa.jsonl",
            lines=_basic_rollout_lines("aaaa-uuid"),
        )
        # Archived rollout directly under archived_sessions/
        archived_path = archived_root / "rollout-2026-04-13T13-48-33-019d889a-bbbb.jsonl"
        with archived_path.open("w", encoding="utf-8") as fh:
            for line in _basic_rollout_lines("bbbb-uuid"):
                fh.write(json.dumps(line) + "\n")
        plugin, root_uri = _make_plugin(sessions_root)
        refs = list(plugin.discover_sessions(root_uri, cursor_payload=None))
        stems = {r.external_session_id for r in refs}
        _check(
            "rollout-2025-10-15T06-56-07-0199e828-aaaa" in stems and "rollout-2026-04-13T13-48-33-019d889a-bbbb" in stems,
            "discover walks both sessions/ tree AND sibling archived_sessions/",
        )


class _QualificationLedger:
    def __init__(
        self,
        *,
        root_uri: str,
        registered: bool = True,
        events_persisted: int = 3,
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
                    "source_kind": "codex_local",
                    "source_id": "src-codex-fixture",
                    "enabled": True,
                    "root_uri": self.root_uri,
                }
            )
        return {"sources": rows}

    def poll_source(self, source_id: str) -> dict[str, int]:
        self.polled_source_id = source_id
        return {"events_persisted": self.events_persisted}

    def list_sessions(self, **kwargs: object) -> dict[str, object]:
        return {"sessions": [{"session_id": "les-codex-fixture"}]}

    def get_session_timeline(self, **kwargs: object) -> dict[str, object]:
        return {"events": [{"content_text": "fixture only"}]}


class _QualificationOrchestrator:
    def __init__(self, ledger: _QualificationLedger) -> None:
        self._ledger = ledger

    def get_service(self, name: str) -> _QualificationLedger | None:
        return self._ledger if name == "session_ledger_service" else None


def test_qualify_is_source_local_and_content_blind() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "fixture-rollout.jsonl").write_text("fixture only\n", encoding="utf-8")
        ledger = _QualificationLedger(root_uri=tmp)
        plugin = CodexFilesystemSessionSourcePlugin()
        plugin.orchestrator_ref = _QualificationOrchestrator(ledger)  # type: ignore[assignment]
        result = plugin.qualify({}, {})
        _check(result["action_status"] == "completed", "qualify completes for fixture source")
        _check(ledger.polled_source_id == "src-codex-fixture", "qualify polls its registered source")
        _check(
            result["data"]
            == {
                "source_registered": True,
                "source_kind": "codex_local",
                "backfill_count": 3,
                "sample_content_retrieved": True,
            },
            "qualify returns the bounded proof shape without sample payload",
        )


def test_qualify_allows_absent_registered_root() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = _QualificationLedger(root_uri=str(Path(tmp) / "never-created"), events_persisted=0)
        plugin = CodexFilesystemSessionSourcePlugin()
        plugin.orchestrator_ref = _QualificationOrchestrator(ledger)  # type: ignore[assignment]
        result = plugin.qualify({}, {})
    _check(result["action_status"] == "completed", "qualify accepts an absent registered root")
    _check(ledger.polled_source_id is None, "absent root bypasses the impossible backfill")
    _check(
        result["data"]
        == {
            "source_registered": True,
            "source_kind": "codex_local",
            "backfill_count": 0,
            "sample_content_retrieved": False,
        },
        "absent root returns an honest empty proof",
    )


def test_qualify_refuses_existing_empty_root() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        ledger = _QualificationLedger(root_uri=tmp, events_persisted=0)
        plugin = CodexFilesystemSessionSourcePlugin()
        plugin.orchestrator_ref = _QualificationOrchestrator(ledger)  # type: ignore[assignment]
        result = plugin.qualify({}, {})
    _check(result["action_status"] == "failed", "qualify refuses an existing empty root")
    _check(ledger.polled_source_id == "src-codex-fixture", "existing root still runs backfill")


def test_qualify_refuses_unregistered_source() -> None:
    plugin = CodexFilesystemSessionSourcePlugin()
    plugin.orchestrator_ref = _QualificationOrchestrator(  # type: ignore[assignment]
        _QualificationLedger(root_uri="/unused", registered=False)
    )
    result = plugin.qualify({}, {})
    _check(result["action_status"] == "failed", "qualify fails without a registered source")
    _check(result["data"] == {}, "failed qualification does not fabricate proof fields")


def _poll_case(root: Path, kind: str, plugin_name: str, fixture_name: str) -> Any:
    """Bind a real adapter fixture to a synthetic root and typed runtime.

    All source observations and durable cursors stay in this case."""
    from types import SimpleNamespace

    sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "ananta" / "tests" / "llm" / "session_ledger"))
    import importer_smoke as runtime
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
    from ananta.llm.session_ledger.types import NormalizedSessionEvent

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

    from ananta.llm.session_ledger.importer import _event_external_id

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
        with tempfile.TemporaryDirectory(prefix="resumable_codex_") as scratch:
            root = Path(scratch) / "sessions"
            root.mkdir()
            case = _poll_case(root, "codex", "codex_filesystem_session_source_plugin", "codex_filesystem_session_source_smoke.py")
            _poll_seed(case, distribution)
            _poll_cut_visits(case)
            _poll_assert_coverage(case)
            _poll_assert_events(case)
            _poll_prefix(case)
            _poll_second_sweep(case)
            _poll_fail_reset(case)
    for scenario in ("churn", "partial", "empty"):
        with tempfile.TemporaryDirectory(prefix=scenario + "_codex_") as scratch:
            root = Path(scratch) / "sessions"
            root.mkdir()
            case = _poll_case(root, "codex", "codex_filesystem_session_source_plugin", "codex_filesystem_session_source_smoke.py")
            if scenario == "churn":
                _poll_churn(case)
            elif scenario == "partial":
                _poll_partial(case)
            else:
                importer, repo, _ = case.runtime.runtime_importer(_poll_fresh(case), str(root))
                assert importer.poll_once().sessions_seen == 0
                assert repo.cursors[case.source_key] == {"version": 1, "state": "caught_up", "after": None}, "empty/no-ref EOF resets state"
    print("PASS real codex pair/version/restart/EOF/reset coverage")


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

    with tempfile.TemporaryDirectory(prefix="large_policy_codex_") as scratch:
        root = Path(scratch) / "sessions"
        root.mkdir()
        case = _poll_case(root, "codex", "codex_filesystem_session_source_plugin", "codex_filesystem_session_source_smoke.py")
        _oversized_setup(case)
        with patch.object(policy, "OVERSIZED_SESSION_BYTES", case.threshold), patch.object(importer_mod, "OVERSIZED_SESSION_BYTES", case.threshold):
            _oversized_deferral(case)
            _oversized_fresh_regular(case)
            _oversized_refusal_success(case)
    print("PASS real codex threshold/regular-pending/leased-oversized coverage")


def _worker_wait_free(executor: Any) -> None:
    """Measure slot release without starting an extra producer pass."""
    assert executor._slot.acquire(timeout=3), "actual oversized/normal worker releases its single slot"
    executor._slot.release()


def _worker_two_passes(normal: Any, low: Any, importer: Any, state: Any, runtime: Any, fixture: Any, other: Path, reads: list[str]) -> None:
    """Both other-row producers must persist new real events before release."""
    from ananta.services.session_ledger_service.poll_drain import start_importer_poll_drain

    for index in (99, 100):
        _worker_wait_free(normal)
        baseline = sum(row.table == "event" for row in state.upserts)
        runtime._legacy_path("codex", fixture, other, index)
        assert start_importer_poll_drain(normal, importer, low) == {"poller": "started"}
        _worker_wait_free(normal)
        assert sum(row.table == "event" for row in state.upserts) > baseline, "TWO regular other-source passes persist actual events while oversized blocks"
        assert len(reads) == 1, "occupied oversized slot never starts a second candidate"


def _worker_completed(repo: Any, before: str) -> None:
    """Check selected EOF publication while regular progress stays identical."""
    from ananta.llm.session_ledger.types import CursorScope

    assert "runtime-source" not in repo.held_sources, "whole-session oversized worker finally releases lease"
    assert json.dumps(repo.cursors["runtime-source", CursorScope.DISCOVERY, None], sort_keys=True) == before, "oversized worker leaves regular cursor byte-identical"
    completed = [key for key, value in repo.cursors.items() if key[0] == "runtime-source" and key[1] is CursorScope.EVENT_READ and "_poll_completed_version" in value]
    assert len(completed) == 2, "one regular plus one completed oversized EOF/fingerprint"


def test_real_oversized_worker_custody() -> None:
    """The actual Codex provider remains leased while TWO other-row polls finish."""
    import threading
    from collections.abc import Iterator
    from unittest.mock import patch

    sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))
    import importer_smoke as runtime
    from ananta.llm.session_ledger import filesystem_poll as policy
    from ananta.llm.session_ledger import importer as importer_mod
    from ananta.llm.session_ledger.types import CursorScope, PollSessionRef
    from ananta.services.session_ledger_service.poll_drain import start_importer_poll_drain
    from ananta.services.session_ledger_service.summary_executor import BoundedSummaryExecutor

    with tempfile.TemporaryDirectory(prefix="actual_large_worker_") as scratch:
        root = Path(scratch) / "main" / "sessions"
        other = Path(scratch) / "other" / "sessions"
        root.mkdir(parents=True)
        other.mkdir(parents=True)
        fixture = runtime._fixture_module("codex_filesystem_session_source_plugin", "codex_filesystem_session_source_smoke.py")
        small = runtime._legacy_path("codex", fixture, root, 0)
        threshold = small.stat().st_size
        for index in (1, 2):
            path = runtime._legacy_path("codex", fixture, root, index)
            path.write_bytes(path.read_bytes() + b"\n")
        plugin = CodexFilesystemSessionSourcePlugin()
        plugin.initialize({})
        importer, repo, state = runtime.runtime_importer(plugin, str(root))
        repo.sources.append(runtime.SourceRow("zz-other", IngestSourceKind.CODEX_LOCAL, str(other), None, True, {}))
        entered, release = threading.Event(), threading.Event()
        read_events = plugin.read_events
        reads = []

        def blocked(root_uri: str, ref: ExternalSessionRef, cursor: dict[str, object] | None) -> Iterator[RawSessionEvent]:
            if isinstance(ref, PollSessionRef) and ref.aggregate_bytes is not None and ref.aggregate_bytes > threshold:
                reads.append(ref.external_session_id)
                if not entered.is_set():
                    entered.set()
                    assert release.wait(5), "fixture releases blocked whole-session worker"
            yield from read_events(root_uri, ref, cursor)

        plugin.read_events = blocked
        normal = BoundedSummaryExecutor(name="B-actual-normal")
        low = BoundedSummaryExecutor(name="B-actual-oversized")

        with patch.object(policy, "OVERSIZED_SESSION_BYTES", threshold), patch.object(importer_mod, "OVERSIZED_SESSION_BYTES", threshold):
            try:
                assert start_importer_poll_drain(normal, importer, low) == {"poller": "started"}
                assert entered.wait(2), "actual regular caught_up admits oversized session on separate worker"
                assert "runtime-source" in repo.held_sources, "actual oversized source lease held while blocked"
                before = json.dumps(repo.cursors["runtime-source", CursorScope.DISCOVERY, None], sort_keys=True)
                _worker_two_passes(normal, low, importer, state, runtime, fixture, other, reads)
                release.set()
                _worker_wait_free(low)
                _worker_completed(repo, before)
                assert start_importer_poll_drain(normal, importer, low) == {"poller": "started"}
                _worker_wait_free(normal)
                _worker_wait_free(low)
                assert len(set(reads)) == 2, "later normal heartbeat resubmits next actual oversized session"
            finally:
                release.set()
                _worker_wait_free(normal)
                _worker_wait_free(low)
    print("PASS actual Codex oversized worker/two-other-polls/resubmission custody")


def main() -> int:
    test_resumable_filesystem_poll()
    test_oversized_provider_policy()
    test_real_oversized_worker_custody()
    print("=== codex_filesystem_session_source_smoke ===")
    test_descriptor()
    test_discover_sessions_yields_known_files()
    test_discover_sessions_rescans_old_stems()
    test_read_events_skips_session_meta_and_noise()
    test_read_events_resume_from_line_offset()
    test_read_events_resume_partial_yields_remainder()
    test_normalize_message_role_mapping()
    test_tool_call_and_result_have_distinct_vendor_event_ids()
    test_unrecognized_top_level_type_raises()
    test_unrecognized_response_item_type_raises()
    test_session_discovery_cursor_round_trip()
    test_archived_sessions_sibling_is_walked()
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
