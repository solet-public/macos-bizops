"""Private-PG selector and cross-process exclusion smoke for event-text reclaim."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Generator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "ananta" / "src"))

from ananta.core.actions.action_processor import _REGISTERED_JOB_OPERATIONS  # noqa: E402
from ananta.core.services.call_context import CallContext  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.llm.session_ledger.blob_adapter import SessionLedgerBlobAdapter  # noqa: E402
from ananta.llm.session_ledger.importer_persistence import persist_normalized_for_session  # noqa: E402
from ananta.llm.session_ledger.schema import get_session_ledger_schema  # noqa: E402
from ananta.llm.session_ledger.types import EventType, IngestSourceKind, MessageRole, NormalizedSessionEvent, SourceVendor  # noqa: E402
from ananta.services.schema_manager import SchemaManager  # noqa: E402
from ananta.services.session_ledger_service.blob_reclaim import EventTextBlobReclaimer  # noqa: E402
from ananta.services.session_ledger_service.enforcement import LedgerAuthorizationError  # noqa: E402
from ananta.services.session_ledger_service.interfaces.public import SessionLedgerIngestAPI  # noqa: E402
from ananta.services.session_ledger_service.service import SessionLedgerService  # noqa: E402

from default_blob_storage_plugin.providers.filesystem_provider import FilesystemProvider  # noqa: E402
from default_blob_storage_plugin.schema import get_blob_storage_schema  # noqa: E402
from quality_gates.private_test_pg import MANIFEST_ENV, ledger_fixture, skip_fixture  # noqa: E402


def _ok(result: Mapping[str, Any]) -> dict[str, Any]:
    assert result.get("action_status") == "completed", result
    return cast(dict[str, Any], result["data"])


def _reference(state: StateManagementInterface, blob_id: str) -> None:
    now = datetime.now(UTC)
    _ok(
        state.write_state(
            "session_ledger",
            {
                "table": "event",
                "record": {
                    "session_id": "synthetic-session",
                    "sequence": int(uuid4().hex[:7], 16),
                    "event_type": EventType.MESSAGE.value,
                    "role": "user",
                    "event_at": now,
                    "imported_at": now,
                    "batch_id": "synthetic-batch",
                    "content_blob_id": blob_id,
                },
            },
        )
    )


def _blob(state: StateManagementInterface, blobs: FilesystemProvider, *, namespace: str = "session_ledger", kind: object = "event_content_text", age: int = 7200) -> str:
    blob_id = str(_ok(blobs.store_blob(namespace, b"synthetic event text", {"kind": kind}))["blob_id"])
    _ok(
        state.update_state(
            "default_blob_storage_plugin",
            {
                "table": "metadata",
                "filters": {"blob_id": blob_id},
            },
            {"created_at": datetime.now(UTC) - timedelta(seconds=age)},
        )
    )
    return blob_id


def _run_selector(state: StateManagementInterface, blobs: FilesystemProvider) -> None:
    reclaim = EventTextBlobReclaimer(state=state, blobs=blobs, blob_root=blobs.blobs_dir, page_size=2, lock_timeout=0.05)
    orphans = [_blob(state, blobs) for _ in range(5)]
    referenced = _blob(state, blobs)
    _reference(state, referenced)
    young = _blob(state, blobs, age=60)
    other_kind = _blob(state, blobs, kind="attachment_text")
    other_namespace = _blob(state, blobs, namespace="assets")
    ambiguous = _blob(state, blobs, kind=None)
    unreadable = _blob(state, blobs)
    (blobs.blobs_dir / unreadable).unlink()
    preview = reclaim.run()
    assert preview["candidate_count"] == 5 and preview["candidate_bytes"] == 5 * len(b"synthetic event text"), preview
    assert preview["deleted_count"] == 0 and preview["ambiguous_count"] == 2, preview
    assert all((blobs.blobs_dir / orphan).exists() for orphan in orphans), "T1 dry run must not unlink"
    assert not (blobs.blobs_dir / ".session_ledger_write_guard.lock").exists(), "dry run must not create a lock"
    result = reclaim.run(confirm=True)
    assert result["deleted_count"] == 5 and result["deleted_bytes"] == 5 * len(b"synthetic event text"), result
    assert all(not (blobs.blobs_dir / orphan).exists() for orphan in orphans), "T2 confirmed pages must not skip deleted rows"
    for blob_id in (referenced, young, other_kind, other_namespace, ambiguous):
        assert (blobs.blobs_dir / blob_id).exists(), f"T3/T4 protected blob was deleted: {blob_id}"
    print("PASS T1-T4 dry-run counts, exact confirmed selector, protected kinds/namespaces/ages/references and ambiguous row")


def _run_batches(state: StateManagementInterface, blobs: FilesystemProvider) -> None:
    """Budgeted batches with a caller-held cursor.

    The fixture store already holds the rows the earlier checks left, so every
    row count is measured against an unstopped call on the same store.
    """
    counted = ("scanned_count", "candidate_count", "candidate_bytes", "deleted_count", "ambiguous_count", "excluded_count")
    text = b"synthetic event text"
    t = [0.0]
    seen: list[dict[str, Any]] = []

    def reclaimer(**options: Any) -> EventTextBlobReclaimer:
        return EventTextBlobReclaimer(state=state, blobs=blobs, blob_root=blobs.blobs_dir, **options)

    def tick() -> float:
        t[0] += 0.5
        return t[0]

    def drain(reclaim: EventTextBlobReclaimer, *, confirm: bool, budget: float, after: list[str] | None = None) -> list[dict[str, Any]]:
        results = [reclaim.run(confirm=confirm, after=after, budget_seconds=budget)]
        while not results[-1]["complete"]:
            results.append(reclaim.run(confirm=confirm, after=json.loads(json.dumps(results[-1]["next_after"])), budget_seconds=budget))
        seen.extend(results)
        return results

    def totals(results: list[dict[str, Any]]) -> dict[str, int]:
        return {key: sum(result[key] for result in results) for key in counted}

    def walk_order(limit: int) -> list[list[str]]:
        query: dict[str, object] = {"table": "metadata", "filters": {"plugin_namespace": "session_ledger"}, "order_by": [["created_at", "asc"], ["id", "asc"]], "limit": limit, "include_deleted": True}
        return [[row["created_at"], row["id"]] for row in _ok(state.query_ordered("default_blob_storage_plugin", query))["records"]]

    def unstopped() -> dict[str, Any]:
        seen.append(reclaimer(monotonic=lambda: 0.0).run())
        assert seen[-1]["complete"] is True and seen[-1]["next_after"] is None, seen[-1]
        return seen[-1]

    def dry_batches() -> tuple[dict[str, Any], list[str], str]:
        before = unstopped()
        orphans = [_blob(state, blobs) for _ in range(250)]
        referenced = _blob(state, blobs)
        _reference(state, referenced)
        whole = unstopped()
        assert (whole["scanned_count"] - before["scanned_count"], before["candidate_count"], whole["candidate_count"]) == (251, 0, 250), (before, whole)
        dry = drain(reclaimer(page_size=100, monotonic=tick), confirm=False, budget=3)
        first, second = dry[0], dry[1]
        order = walk_order(first["scanned_count"] + second["scanned_count"])
        assert first["complete"] is False and 0 < first["scanned_count"] < 100, first
        assert first["next_after"] == order[first["scanned_count"] - 1], "T1 cursor must be the last row scanned"
        assert second["next_after"] == order[-1], "T1 a cursor that went through JSON must continue with the next row"
        assert totals(dry) == totals([whole]) and dry[-1]["next_after"] is None, (totals(dry), whole)
        return whole, orphans, referenced

    def confirmed_batches(whole: dict[str, Any], orphans: list[str], referenced: str) -> float:
        confirmed = drain(reclaimer(), confirm=True, budget=1)
        assert totals(confirmed) == {**totals([whole]), "deleted_count": 250}, (totals(confirmed), whole)
        assert sum(result["deleted_bytes"] for result in confirmed) == whole["candidate_bytes"] == 250 * len(text), confirmed
        assert not any((blobs.blobs_dir / orphan).exists() for orphan in orphans), "T3 confirmed batches must delete every orphan"
        assert (blobs.blobs_dir / referenced).read_bytes() == text, "T3 referenced blob must stay readable"
        return max(result["slowest_row_seconds"] for result in confirmed)

    def slow_page() -> None:
        rest = unstopped()["scanned_count"]
        slow = [_blob(state, blobs, age=30 * 86400) for _ in range(10)]
        waits: list[float] = []

        @contextmanager
        def busy_guard(root: Path, *, timeout: float | None = None) -> Generator[bool]:
            waits.append(cast(float, timeout))
            t[0] += waits[-1]
            yield False

        with patch("ananta.services.session_ledger_service.blob_reclaim.blob_write_guard", busy_guard):
            stopped = reclaimer(lock_timeout=5, monotonic=lambda: t[0]).run(confirm=True, budget_seconds=6)
        seen.append(stopped)
        expected = {"scanned_count": 2, "skipped_locked": 2, "deleted_count": 0, "complete": False, "elapsed_seconds": 6.0, "next_after": walk_order(2)[1]}
        assert waits == [5.0, 1.0] and {key: stopped[key] for key in expected} == expected, (waits, stopped)
        resumed = drain(reclaimer(), confirm=True, budget=60, after=json.loads(json.dumps(stopped["next_after"])))
        assert len(resumed) == 1 and (resumed[0]["scanned_count"] - rest, resumed[0]["deleted_count"]) == (8, 8), resumed
        assert [(blobs.blobs_dir / blob_id).exists() for blob_id in slow] == [True, True] + [False] * 8, "T2 skipped blobs stay readable; the resume loses no row"
        seen.append(reclaimer().run(confirm=True, budget_seconds=60))
        assert seen[-1]["deleted_count"] == 2 and seen[-1]["complete"] is True, seen[-1]
        assert not any((blobs.blobs_dir / blob_id).exists() for blob_id in slow), "T2 a new run without after deletes the skipped blobs"

    def refused(arguments: dict[str, Any]) -> bool:
        try:
            reclaimer().run(**arguments)
        except (TypeError, ValueError):
            return True
        return False

    def shaped(result: dict[str, Any]) -> bool:
        cursor = result["next_after"]
        started = datetime.fromisoformat(result["started_at"]).utcoffset() == timedelta(0)
        timed = result["elapsed_seconds"] >= 0 and result["slowest_row_seconds"] >= 0
        positioned = (cursor is None) is result["complete"] and result["position_created_at"] == (None if cursor is None else cursor[0])
        return started and timed and positioned

    slowest = confirmed_batches(*dry_batches())
    slow_page()
    invalid: list[dict[str, Any]] = [{"after": "x"}, {"after": ["x"]}, {"after": [1, 2]}, {"budget_seconds": 0}, {"budget_seconds": 61}, {"budget_seconds": True}]
    with patch.object(state, "query_ordered", side_effect=AssertionError("batch arguments are validated before any read")):
        assert all(refused(arguments) for arguments in invalid), "T4 invalid after or budget_seconds must be refused"
    assert all(shaped(result) for result in seen), seen
    print(f"PASS batches slowest_row_seconds={slowest:.3f}; budget checked after every row, capped guard wait, JSON cursor resume, counts equal an unstopped call")


def _run_process_lock(state: StateManagementInterface, blobs: FilesystemProvider) -> None:
    blob_id = _blob(state, blobs)
    reclaim = EventTextBlobReclaimer(state=state, blobs=blobs, blob_root=blobs.blobs_dir, page_size=2, lock_timeout=0.05)
    child_code = "from pathlib import Path; import sys; sys.path.insert(0, sys.argv[2]); from ananta.llm.session_ledger.blob_adapter import blob_write_guard\nwith blob_write_guard(Path(sys.argv[1])):\n print('HELD', flush=True)\n sys.stdin.readline()\n"
    child = subprocess.Popen([sys.executable, "-c", child_code, str(blobs.blobs_dir), str(ROOT / "ananta" / "src")], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert child.stdout is not None and child.stdout.readline().strip() == "HELD", "child failed to acquire shared guard"
        result = reclaim.run(confirm=True)
        assert result["skipped_locked"] >= 1 and result["deleted_count"] == 0, result
        assert (blobs.blobs_dir / blob_id).exists(), "reclaim must not delete through another process guard"
        assert child.stdin is not None
        child.stdin.write("release\n")
        child.stdin.flush()
        assert child.wait(timeout=10) == 0
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=10)
    result = reclaim.run(confirm=True)
    assert result["deleted_count"] == 1 and not (blobs.blobs_dir / blob_id).exists(), result
    print("PASS cross-process flock timeout skips, then deletes after release")


def _run_importer_reference_race(state: StateManagementInterface, blobs: FilesystemProvider) -> None:
    """Exercise the real importer wrap across obtain/reuse and append_event."""
    entered, release = threading.Event(), threading.Event()
    obtained: list[str] = []
    failures: list[BaseException] = []

    def append_event(**kwargs: Any) -> SimpleNamespace:
        obtained.append(str(kwargs["content_blob_id"]))
        entered.set()
        assert release.wait(10), "reclaim did not release the fake importer"
        _reference(state, obtained[0])
        return SimpleNamespace(deduped=True)

    repository = SimpleNamespace(find_event_id_by_vendor_id=lambda **kwargs: None, append_event=append_event)
    event = NormalizedSessionEvent(
        external_session_id="race-session",
        event_type=EventType.MESSAGE,
        role=MessageRole.USER,
        content_text="synthetic oversized event " * 2000,
        content_json=None,
        event_at=datetime.now(UTC),
        vendor_event_id="race-event",
        vendor_parent_event_id=None,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )
    adapter = SessionLedgerBlobAdapter(blobs)
    blob_id = adapter.store_event_text(content_text=cast(str, event.content_text), session_id="race-session", external_session_id="race-session", sequence=-1)
    _ok(state.update_state("default_blob_storage_plugin", {"table": "metadata", "filters": {"blob_id": blob_id}}, {"created_at": datetime.now(UTC) - timedelta(hours=2)}))

    def importer() -> None:
        try:
            persist_normalized_for_session(cast(Any, repository), adapter, session_id="race-session", normalized=event, batch_id="race-batch", session_vendor=SourceVendor.CODEX, source_kind=IngestSourceKind.CODEX_PUSHED, ordinals={})
        except BaseException as exc:
            failures.append(exc)
            entered.set()

    thread = threading.Thread(target=importer)
    thread.start()
    try:
        assert entered.wait(10) and not failures, failures
        assert obtained == [blob_id], "importer must reuse the old hashed blob"
        reclaim = EventTextBlobReclaimer(state=state, blobs=blobs, blob_root=blobs.get_blob_store_root(), lock_timeout=0.05)
        blocked = reclaim.run(confirm=True)
        assert blocked["deleted_count"] == 0 and blocked["skipped_locked"] > 0, blocked
    finally:
        release.set()
        thread.join(timeout=10)
    assert not thread.is_alive() and not failures, failures
    after = reclaim.run(confirm=True)
    assert after["deleted_count"] == 0 and (blobs.blobs_dir / blob_id).exists(), after
    print("PASS real importer holds shared guard through reference publication; reused old blob survives")


def _run_registration_and_auth(state: StateManagementInterface, blobs: FilesystemProvider) -> None:
    assert "reclaim_orphan_event_text_blobs" in _REGISTERED_JOB_OPERATIONS["session_ledger_service"]
    metadata = SessionLedgerIngestAPI.reclaim_orphan_event_text_blobs._service_interface_metadata  # type: ignore[attr-defined]
    assert metadata.is_async and metadata.requires_call_context
    assert set(metadata.return_value_schema.properties) == {"job_id", "status"}, "admission must describe a queued receipt"
    service = SimpleNamespace(_state_service=state, _blob_adapter=SessionLedgerBlobAdapter(blobs))
    for context in (None, CallContext.for_external("synthetic")):
        try:
            SessionLedgerService.reclaim_orphan_event_text_blobs(cast(Any, service), call_context=context)
        except LedgerAuthorizationError:
            pass
        else:
            raise AssertionError("missing or external caller must not reclaim")
    result = SessionLedgerService.reclaim_orphan_event_text_blobs(cast(Any, service), call_context=CallContext.for_operator())
    assert result["confirmed"] is False and result["deleted_count"] == 0, result
    print("PASS registered-job receipt and trusted operator authorization; service defaults to preview")


def _run_reimport_after_reclaim(state: StateManagementInterface, blobs: FilesystemProvider) -> None:
    text = "synthetic reclaimed hash text " * 2000
    adapter = SessionLedgerBlobAdapter(blobs)
    old = adapter.store_event_text(content_text=text, session_id="old-orphan", external_session_id="old-orphan", sequence=-1)
    _ok(state.update_state("default_blob_storage_plugin", {"table": "metadata", "filters": {"blob_id": old}}, {"created_at": datetime.now(UTC) - timedelta(hours=2)}))
    reclaimed = EventTextBlobReclaimer(state=state, blobs=blobs, blob_root=blobs.get_blob_store_root()).run(confirm=True)
    assert reclaimed["deleted_count"] == 1 and not (blobs.blobs_dir / old).exists(), reclaimed
    tombstone = _ok(state.read_state("default_blob_storage_plugin", {"table": "metadata", "filters": {"blob_id": old}, "limit": 1}))["records"][0]
    assert tombstone["is_deleted"] == 1, "provider keeps its historical metadata tombstone"
    preview = EventTextBlobReclaimer(state=state, blobs=blobs, blob_root=blobs.get_blob_store_root()).run()
    assert preview["ambiguous_count"] == 2, "known deleted metadata must not become unreadable-blob findings"
    lookup = _ok(blobs.search_blobs("session_ledger", {"file_hash": sha256(text.encode()).hexdigest(), "limit": 1}))
    assert lookup["files"] == [], "RECLAIMED_TOMBSTONE_MUST_NOT_BE_REUSABLE_BY_HASH"
    referenced: list[str] = []

    def append_event(**kwargs: Any) -> SimpleNamespace:
        referenced.append(str(kwargs["content_blob_id"]))
        _reference(state, referenced[-1])
        return SimpleNamespace(deduped=True)

    event = NormalizedSessionEvent(
        external_session_id="reimport-session",
        event_type=EventType.MESSAGE,
        role=MessageRole.USER,
        content_text=text,
        content_json=None,
        event_at=datetime.now(UTC),
        vendor_event_id="reimport-event",
        vendor_parent_event_id=None,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )
    repository = SimpleNamespace(find_event_id_by_vendor_id=lambda **kwargs: None, append_event=append_event)
    persist_normalized_for_session(cast(Any, repository), adapter, session_id="reimport-session", normalized=event, batch_id="reimport-batch", session_vendor=SourceVendor.CODEX, source_kind=IngestSourceKind.CODEX_PUSHED, ordinals={})
    assert len(referenced) == 1 and referenced[0] != old, "reimport must publish a new blob id"
    assert (blobs.blobs_dir / referenced[0]).read_bytes() == text.encode(), "reimported reference must resolve to readable bytes"
    print("PASS deletion preserves tombstone history; hash lookup excludes it and identical reimport references readable new blob")


def _publish_text(state: StateManagementInterface, adapter: SessionLedgerBlobAdapter, text: str) -> str:
    references: list[str] = []

    def append_event(**kwargs: Any) -> SimpleNamespace:
        references.append(str(kwargs["content_blob_id"]))
        _reference(state, references[-1])
        return SimpleNamespace(deduped=True)

    event = NormalizedSessionEvent(
        external_session_id="delete-error-reimport",
        event_type=EventType.MESSAGE,
        role=MessageRole.USER,
        content_text=text,
        content_json=None,
        event_at=datetime.now(UTC),
        vendor_event_id=uuid4().hex,
        vendor_parent_event_id=None,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )
    repository = SimpleNamespace(find_event_id_by_vendor_id=lambda **kwargs: None, append_event=append_event)
    persist_normalized_for_session(cast(Any, repository), adapter, session_id="delete-error-reimport", normalized=event, batch_id="delete-error-batch", session_vendor=SourceVendor.CODEX, source_kind=IngestSourceKind.CODEX_PUSHED, ordinals={})
    assert len(references) == 1
    return references[0]


def _expect_delete_failure(reclaim: EventTextBlobReclaimer) -> None:
    try:
        reclaim.run(confirm=True)
    except RuntimeError as exc:
        assert "'failed_count': 1" in str(exc) and "'deleted_count': 0" in str(exc) and "'unlinked_after_tombstone': 0" in str(exc), str(exc)
    else:
        raise AssertionError("FAILED_METADATA_OR_UNLINK_MUST_NOT_REPORT_RECLAIM_SUCCESS")


def _run_delete_failure_recovery(state: StateManagementInterface, blobs: FilesystemProvider) -> None:
    adapter = SessionLedgerBlobAdapter(blobs)
    reclaim = EventTextBlobReclaimer(state=state, blobs=blobs, blob_root=blobs.get_blob_store_root(), lock_timeout=0.05)
    text = "synthetic metadata error preserves text " * 2000
    old = adapter.store_event_text(content_text=text, session_id="delete-error", external_session_id="delete-error", sequence=-1)
    _ok(state.update_state("default_blob_storage_plugin", {"table": "metadata", "filters": {"blob_id": old}}, {"created_at": datetime.now(UTC) - timedelta(hours=2)}))
    with patch.object(state, "delete_records", return_value={"action_status": "error", "error": {"message": "planted metadata failure"}}):
        _expect_delete_failure(reclaim)
    assert (blobs.blobs_dir / old).read_bytes() == text.encode(), "metadata error must not remove live bytes"
    assert _publish_text(state, adapter, text) == old, "metadata failure permits safe existing readable reuse"
    assert (blobs.blobs_dir / old).read_bytes() == text.encode()

    print("PASS metadata failure preserves live readable reuse through the real importer")


def _run_tombstone_recovery(state: StateManagementInterface, blobs: FilesystemProvider) -> None:
    adapter = SessionLedgerBlobAdapter(blobs)
    reclaim = EventTextBlobReclaimer(state=state, blobs=blobs, blob_root=blobs.get_blob_store_root(), lock_timeout=0.05)
    text = "synthetic unlink error retires before unlink " * 2000
    old = adapter.store_event_text(content_text=text, session_id="unlink-error", external_session_id="unlink-error", sequence=-1)
    _ok(state.update_state("default_blob_storage_plugin", {"table": "metadata", "filters": {"blob_id": old}}, {"created_at": datetime.now(UTC) - timedelta(hours=2)}))
    with patch.object(Path, "unlink", side_effect=PermissionError("planted unlink failure")):
        _expect_delete_failure(reclaim)
    row = _ok(state.read_state("default_blob_storage_plugin", {"table": "metadata", "filters": {"blob_id": old}, "limit": 1}))["records"][0]
    assert row["is_deleted"] == 1 and (blobs.blobs_dir / old).read_bytes() == text.encode(), "failed unlink must never leave live metadata"
    fresh = _publish_text(state, adapter, text)
    assert fresh != old and (blobs.blobs_dir / fresh).read_bytes() == text.encode(), "retired old id must never be republished"
    with patch.object(state, "delete_records", side_effect=AssertionError("no second metadata delete")), patch.object(Path, "unlink", side_effect=PermissionError("planted terminal unlink failure")):
        _expect_delete_failure(reclaim)
    with adapter.write_guard() as held:
        assert held
        preview = reclaim.run()
        assert preview["candidate_count"] == 1 and preview["unlinked_after_tombstone"] == 0, preview
        blocked = reclaim.run(confirm=True)
        assert blocked["skipped_locked"] > 0 and blocked["unlinked_after_tombstone"] == 0, blocked
        assert (blobs.blobs_dir / old).exists(), "tombstone cleanup obeys confirmation and importer guard"
    with patch.object(state, "delete_records", side_effect=AssertionError("no second metadata delete")), patch.object(state, "update_state", side_effect=AssertionError("no metadata update")), patch.object(state, "write_state", side_effect=AssertionError("no metadata insert")):
        recovered = reclaim.run(confirm=True)
    assert recovered["deleted_count"] == 0 and recovered["unlinked_after_tombstone"] == 1 and recovered["failed_count"] == 0, recovered
    assert not (blobs.blobs_dir / old).exists() and (blobs.blobs_dir / fresh).read_bytes() == text.encode()
    again = reclaim.run(confirm=True)
    assert again["unlinked_after_tombstone"] == 0 and again["candidate_count"] == 0, again
    print("PASS metadata failure preserves readable reuse; unlink failure is failed, fresh reimport readable, leftover tombstone unlinked only under confirmation/guard without metadata writes")


def main() -> int:
    if not os.environ.get(MANIFEST_ENV):
        return skip_fixture()
    assert any(index.name == "idx_event_content_blob_id" and index.columns == ["content_blob_id"] for index in get_session_ledger_schema().tables["event"].indexes), "reference equality probe must have its declared index"
    with ledger_fixture("ledger_ingest", schema=f"blob_reclaim_{uuid4().hex[:12]}") as fixture:
        state = cast(StateManagementInterface, fixture.state)
        schemas = SchemaManager(fixture.state, fixture.plugin)
        schemas.initialize_schemas([get_session_ledger_schema(), get_blob_storage_schema()])
        fixture.state.set_schema_manager(schemas)
        blobs = FilesystemProvider(str(fixture.app_home), {})
        blobs.set_state_service(fixture.state)
        _run_selector(state, blobs)
        _run_process_lock(state, blobs)
        _run_importer_reference_race(state, blobs)
        _run_registration_and_auth(state, blobs)
        _run_reimport_after_reclaim(state, blobs)
        _run_delete_failure_recovery(state, blobs)
        _run_tombstone_recovery(state, blobs)
        # Last: its 271 metadata rows would stretch every locked walk above past the fake importer's wait.
        _run_batches(state, blobs)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
