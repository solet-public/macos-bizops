"""Private-PG regression for durable parse failures and advisory-once reads.

Invoked by the registered importer_smoke on the final candidate. Standalone
execution is for the RED reproduction only; no live-profile route exists.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ananta" / "src"))
for _plugin in ("codex_filesystem_session_source_plugin", "claude_code_tasks_session_source_plugin", "postgres_state_management_plugin"):
    sys.path.insert(0, str(ROOT / "plugins" / _plugin / "src"))

from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.llm.session_ledger.blob_adapter import SessionLedgerBlobAdapter  # noqa: E402
from ananta.llm.session_ledger.importer import LedgerPollError, SessionLedgerImporter  # noqa: E402
from ananta.llm.session_ledger.registry import SessionSourceRegistry  # noqa: E402
from ananta.llm.session_ledger.repository import SessionLedgerRepository  # noqa: E402
from ananta.llm.session_ledger.types import CursorScope, ExternalSessionRef, RawSessionEvent  # noqa: E402
from ananta.llm.session_ledger.vendor import claude_code_tasks, codex  # noqa: E402
from claude_code_tasks_session_source_plugin.plugin import ClaudeCodeTasksSessionSourcePlugin  # noqa: E402
from codex_filesystem_session_source_plugin.plugin import CodexFilesystemSessionSourcePlugin  # noqa: E402

from quality_gates.private_test_pg import MANIFEST_ENV, ledger_fixture, skip_fixture  # noqa: E402

_TS = "2026-11-11T12:00:00Z"


class _CountingSource(CodexFilesystemSessionSourcePlugin):
    def __init__(self) -> None:
        super().__init__()
        self.reads = 0

    def read_events(self, root_uri: str, session: ExternalSessionRef, cursor_payload: dict[str, object] | None) -> Iterator[RawSessionEvent]:
        self.reads += 1
        yield from super().read_events(root_uri, session, cursor_payload)


class _Warnings(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def _message(text: str) -> dict[str, object]:
    return {"timestamp": _TS, "type": "response_item", "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": text}]}}


def _write(root: Path, session: str, middle: list[dict[str, object]], *, malformed: bool = False) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"rollout-{session}.jsonl"
    records = [{"timestamp": _TS, "type": "session_meta", "payload": {"id": session, "cwd": "/synthetic"}}, _message("prefix"), *middle, _message("suffix")]
    path.write_text("".join(json.dumps(row) + "\n" for row in records) + ("{invalid json}\n" if malformed else ""))
    return path


def _importer(repo: SessionLedgerRepository, plugin: _CountingSource) -> SessionLedgerImporter:
    return SessionLedgerImporter(registry=SessionSourceRegistry({"synthetic": plugin}), repository=repo, blob_adapter=SessionLedgerBlobAdapter(None))  # type: ignore[arg-type]


def _source(repo: SessionLedgerRepository, plugin: _CountingSource, root: Path) -> str:
    return repo.insert_source(source_kind=plugin.describe().source_kind, root_uri=str(root), account_label=None, config={})


def test_unknown_keeps_suffix(repo: SessionLedgerRepository, root: Path) -> None:
    plugin = _CountingSource()
    _write(root, "unknown", [{"type": "synthetic_future_line"}, {"timestamp": _TS, "type": "response_item", "payload": {"type": "synthetic_future_item"}}])
    _source(repo, plugin, root)
    report = _importer(repo, plugin).poll_once()
    assert report.events_persisted == 2, "T1: unknown records must preserve prefix AND suffix"
    assert report.batches_failed == 0, "T1: unknown records advise without failing the session"
    assert len(report.unknown_line_advisories) == 1, "T1: one advisory per session/parser, even with two unknown types"


def test_second_poll_does_not_read(repo: SessionLedgerRepository, root: Path) -> None:
    plugin = _CountingSource()
    _write(root, "twice", [{"type": "synthetic_future_line"}])
    source = _source(repo, plugin, root)
    importer = _importer(repo, plugin)
    with patch.object(codex, "read_session_meta", wraps=codex.read_session_meta) as header:
        first = importer.poll_source(source)
        second = _importer(repo, plugin).poll_source(source)
    assert plugin.reads == 1 and header.call_count == 1, "T2: unchanged file is not read twice, including discovery header"
    assert first.events_persisted == 2 and second.events_persisted == 0
    assert first.unknown_line_advisories and not second.unknown_line_advisories, "T2: restart does not re-advise"


def test_failed_once_and_change(repo: SessionLedgerRepository, root: Path) -> None:
    plugin = _CountingSource()
    path = _write(root, "broken", [], malformed=True)
    source = _source(repo, plugin, root)
    warning = _Warnings()
    log = logging.getLogger("ananta.llm.session_ledger")
    log.addHandler(warning)
    try:
        with patch.object(codex, "read_session_meta", wraps=codex.read_session_meta) as header:
            first = _importer(repo, plugin).poll_once()
            second = _importer(repo, plugin).poll_once()
        assert plugin.reads == 1 and header.call_count == 1, "T3: failed unchanged file is not read again after restart"
        assert first.batches_failed == second.batches_failed == 1, "T3: cached failure remains a failed batch"
        assert repo.read_cursor(source_id=source, scope=CursorScope.EVENT_READ, scope_key=path.stem) is None, "T3: failure never publishes an event cursor"
        rows = repo._query("session_failure", {"source_id": source, "is_current": True})
        assert len(rows) == 1 and rows[0]["status"] == "failed" and rows[0]["notice_sent"] is True
        assert rows[0]["first_failed_at"] is not None and rows[0]["content_fingerprint"] and rows[0]["parser_version"]
        assert len([m for m in warning.messages if "FAILED session" in m]) == 1, "T3: one operator failure notice, including restart"
        _write(root, "broken", [], malformed=False)
        repaired = _importer(repo, plugin).poll_once()
        assert repaired.batches_failed == 0 and plugin.reads == 2, "T3: changed content/mtime retries and clears failure"
        history = repo._query("session_failure", {"source_id": source})
        assert len(history) >= 2 and any(row["status"] == "cleared" for row in history), "T3: failure and clear history retained"
    finally:
        log.removeHandler(warning)


def test_failed_batch_keeps_siblings(repo: SessionLedgerRepository, root: Path) -> None:
    plugin = _CountingSource()
    _write(root, "good-sibling", [])
    _write(root, "bad-sibling", [], malformed=True)
    source = _source(repo, plugin, root)
    report = _importer(repo, plugin).poll_once()
    assert report.batches_failed == 1 and report.events_persisted == 4, "T4: failed batch counts failure and keeps understood events"
    batches = repo._query("import_batch", {"source_id": source})
    assert batches and batches[0]["status"] == "failed", "T4: batch with failed session must never report completed"
    try:
        _importer(repo, plugin).poll_source(source)
    except LedgerPollError:
        pass
    else:
        raise AssertionError("T4: targeted poll must surface a cached session failure")


def test_named_vendor_records(repo: SessionLedgerRepository, root: Path) -> None:
    del repo
    path = _write(
        root,
        "named",
        [
            {"timestamp": _TS, "type": "inter_agent_communication_metadata", "payload": {"trigger_turn": False}},
            {"timestamp": _TS, "type": "inter_agent_communication", "payload": {"author": "synthetic-a", "recipient": "synthetic-b", "other_recipients": [], "content": "synthetic peer message", "trigger_turn": False}},
            {"timestamp": _TS, "type": "response_item", "payload": {"type": "tool_search_call", "call_id": "search-one", "arguments": {"query": "synthetic"}, "status": "completed"}},
        ],
    )
    normalized = [codex.normalize_raw(raw) for _, raw in codex.iter_events_from_path(path)]
    assert len(normalized) == 4, "T5: retain conversation, peer communication and tool search"
    assert any(row.content_json and row.content_json.get("tool_name") == "tool_search" for row in normalized)
    task = root / "1.json"
    task.write_text(json.dumps({"id": "1", "subject": "synthetic", "description": "synthetic", "status": "pending", "blocks": [], "blockedBy": []}))
    parsed = claude_code_tasks.parse_task_file(task)
    assert parsed.active_form is None, "T5: missing activeForm is explicit absence"
    raw_task = RawSessionEvent(external_session_id="tasks", payload=claude_code_tasks.to_payload(parsed), event_at=parsed.event_at, vendor_event_id="1", vendor_parent_event_id=None)
    assert ClaudeCodeTasksSessionSourcePlugin().normalize(raw_task).content_json["activeForm"] is None  # type: ignore[index]


def test_schema_startup(repo: SessionLedgerRepository, root: Path) -> None:
    del root
    assert repo._query("session_failure", {}) == [], "new table exists through real SchemaManager startup"


def test_parser_change_reopens(repo: SessionLedgerRepository, root: Path) -> None:
    plugin = _CountingSource()
    _write(root, "new-parser", [{"type": "future_type"}], malformed=True)
    _source(repo, plugin, root)
    assert _importer(repo, plugin).poll_once().batches_failed == 1
    with patch("ananta.llm.session_ledger.session_failures.parser_version", return_value="synthetic-new-parser"):
        retried = _importer(repo, plugin).poll_once()
        skipped = _importer(repo, plugin).poll_once()
    assert retried.batches_failed == skipped.batches_failed == 1
    assert plugin.reads == 2, "parser change retries once, then caches its own failed version"
    assert retried.unknown_line_advisories and not skipped.unknown_line_advisories


def test_receipt_history_and_stale_writes(repo: SessionLedgerRepository, root: Path) -> None:
    from ananta.llm.session_ledger.lease import LeaseHeartbeat
    from ananta.llm.session_ledger.repository import LeaseLostError, LedgerRepositoryError
    from ananta.llm.session_ledger.session_failures import SessionFailureRecords

    plugin = _CountingSource()
    _write(root, "history", [], malformed=True)
    source_id = _source(repo, plugin, root)
    source = repo.get_source(source_id)
    handle = repo.try_acquire_polling_lease(source_id)
    assert source is not None and handle is not None
    heartbeat = LeaseHeartbeat(repo, handle, 600)
    records = SessionFailureRecords(repo, source, plugin, heartbeat)
    candidate = next(plugin._poll_candidates(str(root)))
    ref = plugin._selected_ref(str(root), candidate)
    records.fail(ref, ValueError("synthetic"))
    previous = records.lookup(ref.external_session_id)
    _write(root, "history", [], malformed=False)
    changed_ref = plugin._selected_ref(str(root), next(plugin._poll_candidates(str(root))))
    records.clear_on_change(changed_ref)
    current = records.lookup(ref.external_session_id)
    assert previous is not None and current is not None and current["status"] == "cleared"
    try:
        records._append(changed_ref, previous, {"advisory_raised": True})
    except LedgerRepositoryError:
        pass
    else:
        raise AssertionError("stale expected receipt must be refused")
    repo.release_polling_lease(handle)
    try:
        records._append(changed_ref, current, {"advisory_raised": True})
    except LeaseLostError:
        pass
    else:
        raise AssertionError("stale source lease must be refused")
    history = repo._query("session_failure", {"source_id": source_id})
    assert len(history) == 2 and sum(row["is_current"] is True for row in history) == 1
    assert next(row for row in history if row["id"] == previous["id"])["first_failed_at"] == previous["first_failed_at"]


def main() -> int:
    if not os.environ.get(MANIFEST_ENV):
        return skip_fixture()
    failures = 0
    tests = (test_schema_startup, test_unknown_keeps_suffix, test_second_poll_does_not_read, test_failed_once_and_change, test_failed_batch_keeps_siblings, test_named_vendor_records, test_parser_change_reopens, test_receipt_history_and_stale_writes)
    for index, test in enumerate(tests):
        # Each test has its own namespace so source enumeration stays exact.
        with ledger_fixture("ledger_ingest", schema=f"parse_failures_{index}") as fixture, tempfile.TemporaryDirectory(prefix="parse-failures-") as directory:
            repo = SessionLedgerRepository(cast(StateManagementInterface, fixture.state))
            try:
                test(repo, Path(directory) / "sessions")
                print(f"PASS {test.__name__}", flush=True)
            except Exception as exc:
                failures += 1
                print(f"FAIL {test.__name__}: {type(exc).__name__}: {exc}", flush=True)
    return int(failures != 0)


if __name__ == "__main__":
    raise SystemExit(main())
