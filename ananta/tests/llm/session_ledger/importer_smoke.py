#!/usr/bin/env python3
"""Smoke test for SessionLedgerImporter (no pytest).

Coverage (post-2026-06-11 SecretGate v1 rip-out — content always lands

* Pulling poll loop:
  - Reads discovery cursor; iterates sessions; reads event cursor.
  - Persists events via ``Repository.append_event``.
  - Writes both cursors (discovery + event_read) after each pass.
  - Marks batch COMPLETED on a clean pass.
* Pushed dispatch:
  - Clean chunk parses + persists.
  - Normalize ``ValueError`` propagates and the batch is marked
    ``FAILED`` with ``error_kind='value_error'``.
* Cursor advancement: ``write_cursor`` called once per session for
  ``event_read`` + once per source for ``discovery``.

The two SecretGate-quarantine cases were retired at the 2026-06-11
SecretGate v1 rip-out (the importer no longer pre-parses chunks or
quarantines events); their fixtures and assertions were deleted as
part of the 2026-06-13 broken-smokes cleanup.

Cooperative runtime regression map:

* Safe admission is measured with an injected monotonic clock. The source
  records requests before yielding, so an accidental extra next() is visible.
  Time advances inside read_events; both events of that session must land.
* RuntimeRepository retains real normalization and typed persistence but
  records cursor payloads and per-source lease custody. This isolates ordering
  assertions from PostgreSQL scheduling; the unchanged lease/reset smokes
  retain their repository-level responsibilities.
* The opted-in contract source exercises the immutable A-to-B interface. Its
  fingerprint is synthetic: these tests do not claim a real file was stable,
  nor that a real filesystem provider opted in before Part B.
* Failure cases distinguish event persistence, event checkpoint, lease loss,
  unstable version, metadata budget yield and actual empty discovery EOF.
  None may publish a false completion marker or strand the source lease.
* The actual-plugin fixtures reuse existing synthetic fixture writers. Four
  unmarked filesystem providers each consume 51 sessions despite expiry,
  with equal and nonmonotonic mtimes. This preserves their existing full-poll
  behavior; it does not repair discovery ordering or historical source bugs.
* The real Claude parser sees 999 earlier messages and one text/two-tool line.
  Expiry after event 1000 must never publish that line's shared byte offset
  until its remaining events and the entire session have completed.
* Actual Agent Messaging and ChatGPT adapters resume from their own opaque
  cursors in fresh instances. Their owning service/blob fixtures supply data;
  no fake replacement implements the adapters' cursor logic.
* Whole-file history regressions live in the two history smoke modules. They
  import this runtime harness and compare their actual vendor IDs with an
  uninterrupted read of the same synthetic 3/51-session population.
* Real-importer concurrent executor custody is exercised by the companion
  trigger_poll smoke: one leased oversized session blocks while another
  source completes two regular passes, then the heartbeat resubmits work.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/importer_smoke.py
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import tempfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any, ClassVar, cast
from unittest.mock import patch
from zipfile import ZipFile

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "session_ledger"))

from _stub_state_service import (  # noqa: E402
    StubBlobStorageService,
    StubStateService,
)
from ananta.interfaces.llm_session_source_interface import (  # noqa: E402
    LLMSessionSourceInterface,
    PullingSourceMixin,
    PushedSourceMixin,
)
from ananta.llm.session_ledger.blob_adapter import SessionLedgerBlobAdapter  # noqa: E402
from ananta.llm.session_ledger.importer import SessionLedgerImporter  # noqa: E402
from ananta.llm.session_ledger.registry import SessionSourceRegistry  # noqa: E402
from ananta.llm.session_ledger.repository import LedgerRepositoryError, PollingLeaseHandle, SessionLedgerRepository, SourceRow  # noqa: E402
from ananta.llm.session_ledger.schema import (  # noqa: E402
    TABLE_EVENT,
    TABLE_IMPORT_BATCH,
    TABLE_SOURCE_CURSOR,
)
from ananta.llm.session_ledger.types import (  # noqa: E402
    CursorScope,
    EventType,
    ExternalSessionRef,
    IngestMode,
    IngestSourceKind,
    MessageRole,
    NormalizedSessionEvent,
    OversizedSelection,
    PollDiscoveryProgress,
    PollSessionRef,
    RawSessionEvent,
    SessionSourceDescriptor,
    SourceVendor,
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


def _now() -> datetime:
    return datetime(2026, 5, 24, 12, 0, 0, tzinfo=UTC)


# ─── Stub source plugins ────────────────────────────────────────────────────


class _StubPullingSource(LLMSessionSourceInterface, PullingSourceMixin):
    """Yields a deterministic single-session, two-event sequence."""

    def __init__(self) -> None:
        self.discover_cursor_in: list[dict[str, object] | None] = []
        self.event_cursor_in: list[dict[str, object] | None] = []

    def describe(self) -> SessionSourceDescriptor:
        return SessionSourceDescriptor(
            source_kind=IngestSourceKind.AGENT_MESSAGING,
            vendor=SourceVendor.AGENT_MESSAGING,
            supported_modes=(IngestMode.PULLING,),
        )

    def normalize(self, raw: RawSessionEvent) -> NormalizedSessionEvent:
        return NormalizedSessionEvent(
            external_session_id=raw.external_session_id,
            event_type=EventType.MESSAGE,
            role=MessageRole.USER,
            content_text=str(raw.payload.get("text", "")),
            content_json=None,
            event_at=raw.event_at,
            vendor_event_id=raw.vendor_event_id,
            vendor_parent_event_id=None,
            attachment_blob_upload=None,
            attachment_mime_type=None,
            attachment_filename=None,
        )

    def discover_sessions(
        self,
        root_uri: str,  # pyright: ignore[reportUnusedParameter]  # noqa: ARG002
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[ExternalSessionRef]:
        self.discover_cursor_in.append(cursor_payload)
        yield ExternalSessionRef(
            external_session_id="ext_session_1",
            vendor_session_label="Stub Session",
            project_path=None,
            first_seen_at=_now(),
        )

    def read_events(
        self,
        root_uri: str,  # pyright: ignore[reportUnusedParameter]  # noqa: ARG002
        session: ExternalSessionRef,
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[RawSessionEvent]:
        self.event_cursor_in.append(cursor_payload)
        yield RawSessionEvent(
            external_session_id=session.external_session_id,
            payload={"text": "hello world", "cursor": 1},
            event_at=_now(),
            vendor_event_id="v_1",
            vendor_parent_event_id=None,
        )
        yield RawSessionEvent(
            external_session_id=session.external_session_id,
            payload={"text": "second clean event", "cursor": 2},
            event_at=_now(),
            vendor_event_id="v_2",
            vendor_parent_event_id=None,
        )

    def session_discovery_cursor(
        self,
        root_uri: str,  # pyright: ignore[reportUnusedParameter]  # noqa: ARG002
        last_seen: ExternalSessionRef | None,
    ) -> dict[str, object]:
        return {"high_water_iso": last_seen.first_seen_at.isoformat()} if last_seen else {}

    def event_read_cursor(
        self,
        root_uri: str,  # pyright: ignore[reportUnusedParameter]  # noqa: ARG002
        session: ExternalSessionRef,  # pyright: ignore[reportUnusedParameter]  # noqa: ARG002
        last_event: RawSessionEvent | None,
    ) -> dict[str, object]:
        return {"cursor_high_water": int(last_event.payload["cursor"])} if last_event else {}


class _StubPushedSource(LLMSessionSourceInterface, PushedSourceMixin):
    """Parses a chunk_text into one message event (one line of text)."""

    def __init__(self, *, raise_on_normalize: bool = False) -> None:
        self._raise_on_normalize = raise_on_normalize

    def describe(self) -> SessionSourceDescriptor:
        return SessionSourceDescriptor(
            source_kind=IngestSourceKind.CODEX_PUSHED,
            vendor=SourceVendor.CODEX,
            supported_modes=(IngestMode.PUSHED,),
        )

    def normalize(self, raw: RawSessionEvent) -> NormalizedSessionEvent:
        if self._raise_on_normalize:
            raise ValueError("planted normalize failure")
        return NormalizedSessionEvent(
            external_session_id=raw.external_session_id,
            event_type=EventType.MESSAGE,
            role=MessageRole.USER,
            content_text=str(raw.payload.get("text", "")),
            content_json=None,
            event_at=raw.event_at,
            vendor_event_id=raw.vendor_event_id,
            vendor_parent_event_id=None,
            attachment_blob_upload=None,
            attachment_mime_type=None,
            attachment_filename=None,
        )

    def parse_chunk(self, chunk_text: str) -> Iterator[RawSessionEvent]:
        yield RawSessionEvent(
            external_session_id="pushed_session_1",
            payload={"text": chunk_text},
            event_at=_now(),
            vendor_event_id=None,
            vendor_parent_event_id=None,
        )


# ─── Setup helper ───────────────────────────────────────────────────────────


def _make_importer(plugin: Any) -> tuple[SessionLedgerImporter, StubStateService]:
    state = StubStateService()
    state.add_select_response(
        "FROM session_ledger__source WHERE",
        [
            {
                "id": "src_pull_1",
                "source_kind": plugin.describe().source_kind.value,
                "root_uri": "stub",
                "account_label": None,
                "enabled": True,
                "config_json": {},
            }
        ],
    )
    # The importer's ``_poll_one_pulling_source`` short-circuits to (0,0,0,0)
    # when ``try_acquire_polling_lease`` returns None. ``try_acquire`` now rides
    # the ``acquire_lease`` primitive, whose stub verdict defaults to True
    # (lease acquired) — so the happy-path walk runs without any priming.
    repo = SessionLedgerRepository(state, clock=_now)  # type: ignore[arg-type]
    blob_adapter = SessionLedgerBlobAdapter(StubBlobStorageService())  # type: ignore[arg-type]
    registry = SessionSourceRegistry({plugin.describe().source_kind.value + "_stub": plugin})
    importer = SessionLedgerImporter(
        registry=registry,
        repository=repo,
        blob_adapter=blob_adapter,
    )
    return importer, state


# ─── Tests ──────────────────────────────────────────────────────────────────


def test_pulling_poll_clean_persists_two_events() -> None:
    plugin = _StubPullingSource()
    importer, state = _make_importer(plugin)
    report = importer.poll_once()
    _check(report.sources_polled == 1, "1 source polled")
    _check(report.sessions_seen == 1, "1 session discovered")
    _check(report.events_persisted == 2, "2 events persisted")
    _check(report.batches_failed == 0, "no batch failures")
    # cursor writes: 1 event_read + 1 discovery (write_cursor → typed
    # write_state INSERT branch / update_state revive branch).
    cursor_writes = [w for w in state.writes if w.table == TABLE_SOURCE_CURSOR]
    cursor_updates = [u for u in state.updates if u.table == TABLE_SOURCE_CURSOR]
    _check(
        len(cursor_writes) + len(cursor_updates) == 2,
        "2 cursor writes (one event_read, one discovery)",
    )


def test_pushed_clean_chunk_persists_and_completes_batch() -> None:
    plugin = _StubPushedSource()
    importer, state = _make_importer(plugin)
    state.add_select_response(
        "FROM session_ledger__source WHERE",
        [
            {
                "id": "src_push_1",
                "source_kind": plugin.describe().source_kind.value,
                "root_uri": "pushed:codex_pushed",
                "account_label": None,
                "enabled": True,
                "config_json": {},
            }
        ],
    )
    result = importer.dispatch_pushed(
        source_kind=IngestSourceKind.CODEX_PUSHED,
        chunk_text="plain chunk text",
    )
    _check(result.events_persisted == 1, "pushed clean chunk persisted 1 event")
    _check(bool(result.batch_id), "push returned a real batch_id (PushDispatchResult)")
    batch_updates = [u for u in state.updates if u.table == TABLE_IMPORT_BATCH]
    _check(
        any(u.updates.get("status") == "completed" for u in batch_updates),
        "import_batch finished with status as a typed parameter (not literal)",
    )


def test_pushed_normalize_failure_marks_batch_failed() -> None:
    plugin = _StubPushedSource(raise_on_normalize=True)
    importer, state = _make_importer(plugin)
    state.add_select_response(
        "FROM session_ledger__source WHERE",
        [
            {
                "id": "src_push_3",
                "source_kind": plugin.describe().source_kind.value,
                "root_uri": "pushed:codex_pushed",
                "account_label": None,
                "enabled": True,
                "config_json": {},
            }
        ],
    )
    try:
        importer.dispatch_pushed(
            source_kind=IngestSourceKind.CODEX_PUSHED,
            chunk_text="clean chunk that normalize will reject",
        )
    except ValueError as e:
        _check("planted normalize failure" in str(e), "normalize ValueError surfaced")
        batch_updates = [u for u in state.updates if u.table == TABLE_IMPORT_BATCH]
        _check(len(batch_updates) >= 1, "batch marked failed after normalize")
        return
    _check(False, "expected ValueError from normalize")


class RuntimeRepository(SessionLedgerRepository):
    """Real persistence with typed in-memory cursor/lease observations for runtime tests."""

    def __init__(self, state: StubStateService, source: SourceRow) -> None:
        super().__init__(state, clock=_now)  # type: ignore[arg-type]
        self.sources = [source]
        self.cursors: dict[tuple[str, CursorScope, str | None], dict[str, object]] = {}
        self.trace: list[str] = []
        self.held_sources: set[str] = set()
        self.lose_lease = False
        self.refuse = False
        self.fail_cursor = False

    @property
    def held(self) -> bool:
        return bool(self.held_sources)

    def refresh_polling_lease(self, handle: PollingLeaseHandle, *, ttl_seconds: int = 600) -> PollingLeaseHandle | None:
        return None if self.lose_lease else super().refresh_polling_lease(handle, ttl_seconds=ttl_seconds)

    def list_sources(self, *, enabled_only: bool = False) -> list[SourceRow]:
        return list(self.sources)

    def get_source(self, source_id: str) -> SourceRow | None:
        return next((row for row in self.sources if row.id == source_id), None)

    def try_acquire_polling_lease(self, source_id: str, *, ttl_seconds: int = 600) -> PollingLeaseHandle | None:
        self.trace.append("acquire")
        if self.refuse or source_id in self.held_sources:
            return None
        handle = super().try_acquire_polling_lease(source_id, ttl_seconds=ttl_seconds)
        if handle is not None:
            self.held_sources.add(source_id)
        return handle

    def release_polling_lease(self, handle: PollingLeaseHandle) -> None:
        self.trace.append("release")
        self.held_sources.discard(handle.source_id)
        super().release_polling_lease(handle)

    def read_cursor(self, *, source_id: str, scope: CursorScope, scope_key: str | None = None) -> dict[str, object] | None:
        self.trace.append("read:" + scope.value)
        cursor = self.cursors.get((source_id, scope, scope_key))
        return dict(cursor) if cursor is not None else None

    def write_cursor(self, *, source_id: str, scope: CursorScope, cursor_payload: dict[str, Any], scope_key: str | None = None) -> None:
        self.trace.append("write:" + scope.value)
        assert source_id in self.held_sources, "cursor write must hold the selected source lease"
        if self.fail_cursor:
            raise LedgerRepositoryError("planted checkpoint failure")
        self.cursors[source_id, scope, scope_key] = dict(cursor_payload)


def runtime_importer(
    plugin: Any,
    root_uri: str,
    *,
    clock: Callable[[], float] = lambda: 0.0,
    repository: RuntimeRepository | None = None,
    lease_ttl: int = 600,
) -> tuple[SessionLedgerImporter, RuntimeRepository, StubStateService]:
    """Construct one importer over synthetic source rows and typed persistence.

    Reusing a repository models process restart while retaining durable cursors.
    The injected clock controls admission only, never per-event lease renewal."""
    state = StubStateService()
    source = SourceRow("runtime-source", plugin.describe().source_kind, root_uri, None, True, {})
    repo = repository if repository is not None else RuntimeRepository(state, source)
    registry = SessionSourceRegistry({"runtime": plugin})
    return (
        SessionLedgerImporter(
            registry=registry,
            repository=repo,
            blob_adapter=SessionLedgerBlobAdapter(StubBlobStorageService()),  # type: ignore[arg-type]
            poll_clock=clock,
            polling_lease_ttl_seconds=lease_ttl,
        ),
        repo,
        state,
    )


class _TimedSource(_StubPullingSource):
    def __init__(self) -> None:
        super().__init__()
        self.time = 0.0
        self.requested: list[str] = []

    def clock(self) -> float:
        return self.time

    def discover_sessions(self, root_uri: str, cursor_payload: dict[str, object] | None) -> Iterator[ExternalSessionRef]:
        for key in ("one", "two"):
            self.requested.append(key)
            yield ExternalSessionRef(key, None, None, _now())

    def read_events(self, root_uri: str, session: ExternalSessionRef, cursor_payload: dict[str, object] | None) -> Iterator[RawSessionEvent]:
        for raw in super().read_events(root_uri, session, cursor_payload):
            self.time = 61.0
            yield raw


def test_whole_session_deadline() -> None:
    plugin = _TimedSource()
    importer, repo, state = runtime_importer(plugin, "synthetic", clock=lambda: plugin.time)
    report = importer.poll_once()
    assert report.events_persisted == 2, "whole admitted session must finish after deadline"
    assert plugin.requested == ["one"], "no next-ref side effect after deadline"
    assert repo.cursors["runtime-source", CursorScope.EVENT_READ, "one"] == {"cursor_high_water": 2}
    assert len([w for w in state.upserts if w.table == TABLE_EVENT]) == 2
    assert not repo.held
    _check(True, "whole-session expiry checkpoints EOF before yielding without next()")


class _ContractSource(_TimedSource):
    supports_whole_session_poll: ClassVar[bool] = True

    def __init__(self, repo: RuntimeRepository | None = None) -> None:
        super().__init__()
        self.repo = repo
        self.empty = False
        self.partial = False
        self.fail = False
        self.no_raw = False
        self.regular_pending = False
        self.selected = False
        self.no_candidate = False
        self.completed_calls: list[tuple[bool, bool, tuple[int, str] | None]] = []
        self.fingerprint: dict[str, object] | None = {"version": 1}
        self.prepared: list[dict[str, object] | None] = []

    def describe(self) -> SessionSourceDescriptor:
        return SessionSourceDescriptor(IngestSourceKind.CODEX_LOCAL, SourceVendor.CODEX, (IngestMode.PULLING,))

    def discover_sessions_for_poll(
        self,
        root_uri: str,
        cursor_payload: dict[str, object] | None,
        *,
        read_event_cursor: Callable[[str], dict[str, object] | None],
        can_admit_next: Callable[[], bool],
        progress: PollDiscoveryProgress,
    ) -> Iterator[ExternalSessionRef]:
        read_event_cursor("contract")
        if self.partial:
            from ananta.llm.session_ledger.poll_budget import DiscoveryBudgetYield

            progress.last_examined = (9, "unchanged")
            self.time = 61.0
            assert not can_admit_next(), "metadata clock reached deadline"
            raise DiscoveryBudgetYield
        if not self.empty:
            yield PollSessionRef("contract", None, None, _now(), discovery_pair=(8, "selected"))

    def completed_discovery_cursor(
        self,
        root_uri: str,
        previous_cursor: dict[str, object] | None,
        *,
        exhausted: bool,
        pending: bool,
        progress: PollDiscoveryProgress,
    ) -> dict[str, object] | None:
        self.completed_calls.append((exhausted, pending, progress.last_examined))
        return {"version": 1, "state": "caught_up" if exhausted and not pending else "backfilling", "after": None if exhausted else progress.last_examined}

    def prepare_poll_event_cursor(self, root_uri: str, ref: ExternalSessionRef, stored_cursor: dict[str, object] | None) -> dict[str, object] | None:
        self.prepared.append(stored_cursor)
        return super().prepare_poll_event_cursor(root_uri, ref, stored_cursor)

    def completed_session_fingerprint(self, root_uri: str, ref: ExternalSessionRef) -> dict[str, object] | None:
        return self.fingerprint

    def read_events(self, root_uri: str, session: ExternalSessionRef, cursor_payload: dict[str, object] | None) -> Iterator[RawSessionEvent]:
        assert cursor_payload is None or "_poll_completed_version" not in cursor_payload
        if self.fail:
            raise ValueError("planted persist-path failure")
        if not self.no_raw:
            yield from super().read_events(root_uri, session, cursor_payload)

    def select_oversized_session(
        self,
        root_uri: str,
        *,
        read_event_cursor: Callable[[str], dict[str, object] | None],
        threshold: int,
    ) -> OversizedSelection:
        assert self.repo is not None and self.repo.held, "authoritative oversized recheck requires lease FIRST"
        self.repo.trace.append("select")
        read_event_cursor("contract")
        assert threshold == 50_000_000
        self.selected = True
        if self.no_candidate:
            return OversizedSelection()
        return OversizedSelection(regular_pending=self.regular_pending, candidate=None if self.regular_pending else PollSessionRef("contract", None, None, _now()))


def _run_contract_mode(mode: str) -> tuple[_ContractSource, RuntimeRepository]:
    """Arrange a single failure or completion premise before visiting the source.

    A persistence failure uses the existing typed state-service error envelope.
    Cursor failure is separate because events and checkpoints are not atomic."""
    plugin = _ContractSource()
    plugin.empty = mode == "empty"
    plugin.partial = mode == "metadata"
    plugin.fail = mode == "failure"
    plugin.no_raw = mode == "no_raw"
    if mode == "unstable":
        plugin.fingerprint = None
    importer, repo, state = runtime_importer(plugin, "synthetic", clock=plugin.clock)
    repo.fail_cursor = mode == "cursor_failure"
    if plugin.no_raw:
        repo.cursors["runtime-source", CursorScope.EVENT_READ, "contract"] = {"offset": 123, "_poll_completed_version": {"old": True}}
    if mode == "persist_failure":
        state.add_upsert_failure(TABLE_EVENT, lambda record: True, "planted persistence failure")
    importer.poll_once()
    assert not repo.held, "lease releases on all checkpoint branches"
    return plugin, repo


def _assert_contract_events(mode: str, plugin: _ContractSource, repo: RuntimeRepository) -> None:
    """Inspect the published checkpoint rather than an adapter-local flag.

    A stable marker must share the vendor EOF write. A no-event EOF keeps the
    previous vendor payload, including when normalization produced no new rows."""
    saved = repo.cursors.get(("runtime-source", CursorScope.EVENT_READ, "contract"), {})
    if mode in ("failure", "persist_failure", "cursor_failure", "unstable"):
        assert "_poll_completed_version" not in saved, "failed or unstable whole session cannot be completed"
    elif mode == "no_raw":
        assert saved == {"offset": 123, "_poll_completed_version": {"version": 1}}, "no-raw EOF preserves vendor cursor"
        assert plugin.prepared == [{"offset": 123, "_poll_completed_version": {"old": True}}]
    elif mode == "normal":
        assert saved == {"cursor_high_water": 2, "_poll_completed_version": {"version": 1}}, "EOF and fingerprint share one checkpoint"
        assert sum(entry == "write:event_read" for entry in repo.trace) == 1, "EOF and fingerprint use ONE write"


def _assert_contract_hooks(mode: str, plugin: _ContractSource) -> None:
    """Distinguish actual EOF from a deadline or metadata-prefix yield.

    The expected selected pair proves the importer acknowledged success before
    requesting another item, avoiding reliance on post-yield generator effects."""
    expected = {
        "normal": [(False, True, (8, "selected"))],
        "empty": [(True, False, None)],
        "metadata": [(False, True, (9, "unchanged"))],
    }
    if mode in expected:
        assert plugin.completed_calls == expected[mode], "EOF/partial and success ACK occur before another next()"


def test_contract_checkpoints() -> None:
    """Exercise every completion-hook outcome using a fresh per-visit context.

    Empty discovery must still checkpoint state; incomplete work must remain
    pending. Invalid selection combinations are an internal contract error."""
    for mode in ("normal", "empty", "metadata", "unstable", "failure", "persist_failure", "cursor_failure", "no_raw"):
        plugin, repo = _run_contract_mode(mode)
        _assert_contract_events(mode, plugin, repo)
        _assert_contract_hooks(mode, plugin)
    try:
        OversizedSelection(True, PollSessionRef("invalid", None, None, _now()))
    except ValueError:
        pass
    else:
        raise AssertionError("contradictory selection is invalid")
    _check(True, "typed seam complete/partial/empty/no-raw/failure lifecycle")


def _assert_oversized_regular(importer: SessionLedgerImporter, plugin: _ContractSource, repo: RuntimeRepository, original: dict[str, object]) -> None:
    """Observe state-only invalidation after the authoritative leased recheck.

    The original version and after pair are retained exactly. The subsequent
    regular visit proves the shipped producer can consume the newly pending work."""
    source_key = ("runtime-source", CursorScope.DISCOVERY, None)
    assert repo.cursors[source_key] == {**original, "state": "backfilling"}, "fresh regular work changes state ONLY"
    assert repo.trace.index("acquire") < repo.trace.index("select") < repo.trace.index("write:discovery")
    assert not plugin.event_cursor_in, "fresh regular work forbids oversized read"
    plugin.regular_pending = False
    importer.poll_once()
    assert plugin.event_cursor_in, "next normal heartbeat handles regular work"


def _assert_oversized_outcome(mode: str, plugin: _ContractSource, repo: RuntimeRepository, original: dict[str, object]) -> None:
    """Check contention and successful whole-session cursor isolation separately.

    A refused source lease cannot write or inspect event/discovery cursors.
    Successful low-priority work publishes only its selected event checkpoint."""
    if mode == "refused":
        assert repo.trace == ["acquire"], "refused lease causes zero source/event cursor reads or writes"
        assert not plugin.selected
    elif mode == "success":
        assert repo.cursors["runtime-source", CursorScope.DISCOVERY, None] == original, "successful oversized work preserves regular cursor byte identity"
        assert repo.cursors["runtime-source", CursorScope.EVENT_READ, "contract"]["_poll_completed_version"] == {"version": 1}
    assert not repo.held


def test_oversized_authoritative_lease() -> None:
    """Reproduce R2 F1 at the importer entry, not merely inside the selector.

    The trace includes acquisition and all cursor reads/writes, so moving the
    state change before acquisition is an observable integrity violation."""
    for mode in ("refused", "regular", "success"):
        plugin = _ContractSource()
        importer, repo, _ = runtime_importer(plugin, "synthetic")
        plugin.repo = repo
        plugin.regular_pending = mode == "regular"
        repo.refuse = mode == "refused"
        original: dict[str, object] = {"version": 7, "state": "caught_up", "after": [8, "preserve"]}
        repo.cursors["runtime-source", CursorScope.DISCOVERY, None] = original.copy()
        importer.poll_one_oversized()
        _assert_oversized_outcome(mode, plugin, repo, original)
        if mode == "regular":
            _assert_oversized_regular(importer, plugin, repo, original)
    _check(True, "F1 leased recheck, state-only invalidation, no-write contention, source cursor preservation")


def _fixture_module(plugin: str, filename: str) -> ModuleType:
    """Read existing fixture builders without running their smoke main()."""
    sys.path.insert(0, str(REPO_ROOT / "plugins" / plugin / "src"))
    path = REPO_ROOT / "plugins" / plugin / "tests" / filename
    spec = importlib.util.spec_from_file_location("fixture_" + plugin, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _plugin_present(plugin: str) -> bool:
    root = Path("plugins") / plugin / "src" / plugin
    if (REPO_ROOT / root).is_dir():
        return True
    print(f"SKIP: actual-plugin cases; missing root {root.as_posix()}")
    return False


def _legacy_modules() -> dict[str, ModuleType]:
    """Load existing fixture builders while retaining the actual plugin classes.

    Their module main functions are not run here; each dependency smoke has its
    own final run. Fixture files remain beneath the caller-provided scratch root."""
    fixtures = {
        "codex": ("codex_filesystem_session_source_plugin", "codex_filesystem_session_source_smoke.py"),
        "claude": ("claude_code_filesystem_session_source_plugin", "claude_code_filesystem_session_source_smoke.py"),
        "tasks": ("claude_code_tasks_session_source_plugin", "multi_session_discovery_smoke.py"),
        "ambient": ("codex_ambient_session_source_plugin", "multi_project_smoke.py"),
    }
    return {kind: _fixture_module(plugin, filename) for kind, (plugin, filename) in fixtures.items() if _plugin_present(plugin)}


def _legacy_path(kind: str, module: ModuleType, root: Path, index: int) -> Path:
    """Build one source-shaped session from its existing synthetic writer.

    Session IDs are distinct across 51 refs. File names and metadata meet the
    real adapter predicates, so an early cut cannot hide behind invalid input."""
    session_id = f"00000000-0000-0000-0000-{index:012d}"
    if kind == "codex":
        return module._write_rollout(root, date_subpath="2026/01/01", filename=f"rollout-{session_id}.jsonl", lines=module._basic_rollout_lines(session_id))
    if kind == "claude":
        path = root / "-synthetic-project" / (session_id + ".jsonl")
        line = module._line_user_text()
        line["sessionId"] = session_id
        line["uuid"] = session_id
        module._write_jsonl(path, [line])
        return path
    if kind == "tasks":
        path = root / session_id / "1.json"
        path.parent.mkdir()
        module._write_task(path, session_id)
        return path
    path = root / session_id / "ambient-suggestions.json"
    path.parent.mkdir()
    module._write_ambient_file(path, 1)
    return path


def _legacy_plugin(kind: str, module: ModuleType, root: Path) -> Any:
    """Create a fresh actual adapter with its real configuration lookup contract.

    Only glob/filename configuration is supplied; discovery and event reading
    are not patched. Part A leaves every filesystem capability marker false."""
    if kind == "codex":
        from codex_filesystem_session_source_plugin.plugin import CodexFilesystemSessionSourcePlugin

        plugin = CodexFilesystemSessionSourcePlugin()
        plugin.prepare_for_readiness()
        return plugin
    if kind == "claude":
        return module._make_plugin()
    if kind == "tasks":
        return module._build_plugin(root)[0]
    from codex_ambient_session_source_plugin.plugin import CodexAmbientSessionSourcePlugin

    plugin = CodexAmbientSessionSourcePlugin()
    plugin.config_provider = module._StubProvider({"filename": "ambient-suggestions.json"})
    return plugin


def _verify_legacy_poll(kind: str, module: ModuleType, root: Path) -> None:
    """Verify complete coverage before accepting a legacy source discovery cursor.

    Expiry is injected during normalization of actual raw input. A fresh plugin
    must find no unread mtime-gated ref after the unchanged whole-poll route."""
    plugin = _legacy_plugin(kind, module, root)
    assert plugin.supports_whole_session_poll, "B actual adapter must explicitly opt in"
    plugin.supports_whole_session_poll = False
    assert not plugin.supports_whole_session_poll, "legacy actual adapter must lack explicit capability"
    importer, repo, state = runtime_importer(plugin, str(root), clock=expire_plugin_clock(plugin))
    report = importer.poll_once()
    assert report.sessions_seen == 51, "actual legacy adapter must finish all 51 refs despite expired clock"
    actual_ids = {str(w.record["external_session_id"]) for w in state.upserts if w.table == "session"}
    assert len(actual_ids) == 51, "actual legacy fixture must persist every session identity"
    assert report.batches_failed == 0

    def verify_marked_poll() -> None:
        """Compare activated whole-session restart with the unmarked population."""
        # Preserve the negative unmarked case above, then exercise the marked path.
        marked = _legacy_plugin(kind, module, root)
        assert marked.supports_whole_session_poll, "fresh B adapter remains explicitly marked"
        activated, active_repo, active_state = runtime_importer(marked, str(root), clock=expire_plugin_clock(marked))
        active_report = activated.poll_once()
        assert active_report.sessions_seen == 1, "marked adapter finishes one whole session before yielding"
        resumed, _, _ = runtime_importer(_legacy_plugin(kind, module, root), str(root), repository=active_repo)
        resumed_report = resumed.poll_once()
        assert active_report.sessions_seen + resumed_report.sessions_seen == 51, "marked restart covers all 51 actual sessions"
        marked_ids = {str(w.record["external_session_id"]) for w in active_state.upserts if w.table == "session"}
        assert marked_ids == actual_ids, "marked and unmarked whole-session populations agree"
        assert active_repo.cursors["runtime-source", CursorScope.DISCOVERY, None]["state"] == "caught_up"

    verify_marked_poll()
    cursor = repo.cursors["runtime-source", CursorScope.DISCOVERY, None]
    if kind != "codex":
        fresh = _legacy_plugin(kind, module, root)
        assert not list(fresh.discover_sessions(str(root), cursor)), "fresh legacy whole-poll cursor leaves no unread ref"


def test_actual_legacy_filesystem_sources_remain_whole() -> None:
    """Run equal and nonmonotonic mtime populations for all four legacy adapters.

    These are safety regressions for Part A staging. They deliberately make no
    claim about Part B continuation, threshold deferral, or file-version policy."""
    for kind, module in _legacy_modules().items():
        with tempfile.TemporaryDirectory(prefix="legacy_" + kind) as td:
            root = Path(td) / "sessions"
            root.mkdir()
            paths = [_legacy_path(kind, module, root, index) for index in range(51)]
            for distribution in ("equal", "nonmonotonic"):
                for index, path in enumerate(paths):
                    stamp = 1_700_000_000 if distribution == "equal" else 1_700_000_000 + (index % 3)
                    os.utime(path, (stamp, stamp))
                _verify_legacy_poll(kind, module, root)
    _check(True, "available actual unmarked filesystem adapters retain full >50 equal/nonmonotonic-mtime polls")


def test_actual_claude_multi_event_line_finishes_whole() -> None:
    """Expose the physical-line checkpoint hazard with the actual Claude parser.

    The fake clock expires on the text event after 999 earlier messages. Both
    tool calls share its byte cursor and must land before that cursor is saved."""
    claude = _fixture_module("claude_code_filesystem_session_source_plugin", "claude_code_filesystem_session_source_smoke.py")
    with tempfile.TemporaryDirectory(prefix="claude_boundary_") as td:
        root = Path(td)
        path = root / "-synthetic-project" / (claude._SID + ".jsonl")
        lines = []
        for index in range(999):
            row = claude._line_user_text()
            row["uuid"] = "user-" + str(index)
            lines.append(row)
        lines.append(claude._line_assistant_text_and_two_tool_uses())
        claude._write_jsonl(path, lines)
        plugin = claude._make_plugin()
        expected = list(plugin.read_events(str(root), list(plugin.discover_sessions(str(root), None))[0], None))
        assert len(expected) == 1002, "fixture has 999 earlier events and text plus two tools"
        observed = [0]
        clock = [0.0]
        normalize = plugin.normalize

        def expire_in_line(raw: RawSessionEvent) -> NormalizedSessionEvent:
            observed[0] += 1
            if observed[0] == 1000:
                clock[0] = 61.0
            return normalize(raw)

        plugin.normalize = expire_in_line
        importer, repo, state = runtime_importer(plugin, str(root), clock=lambda: clock[0])
        report = importer.poll_once()
        assert clock[0] == 61.0, "mid-line expiry reached after event 1000"
        assert report.events_persisted == 1002, "entire multi-event line AND whole session must finish before checkpoint"
        written = [w.record for w in state.upserts if w.table == TABLE_EVENT]
        assert {row["vendor_event_id"] for row in written} == {raw.vendor_event_id for raw in expected}, "whole-session vendor IDs match uninterrupted baseline"
        assert repo.cursors["runtime-source", CursorScope.EVENT_READ, claude._SID]["byte_offset"] == path.stat().st_size
        restarted = claude._make_plugin()
        after = list(
            restarted.discover_sessions_for_poll(
                str(root),
                None,
                read_event_cursor=lambda key: repo.read_cursor(source_id="runtime-source", scope=CursorScope.EVENT_READ, scope_key=key),
                can_admit_next=lambda: True,
                progress=PollDiscoveryProgress(),
            )
        )
        assert not after, "fresh plugin leaves no missing tool/session at saved EOF"
    _check(True, "actual Claude 999+text/two-tools fixture keeps whole-line/session EOF custody")


def expire_plugin_clock(plugin: Any) -> Callable[[], float]:
    """Expire an injected monotonic clock during the first actual raw event."""
    clock = [0.0]
    normalize = plugin.normalize

    def expire(raw: RawSessionEvent) -> NormalizedSessionEvent:
        clock[0] = 61.0
        return normalize(raw)

    plugin.normalize = expire
    return lambda: clock[0]


class _OwningMessagingFixture:
    """Owning-service fixture; the actual source retains all cursor/normalizer code."""

    def list_threads(self, request: Any) -> Any:
        from ananta.llm.agent_messaging.thread_cursor import decode_thread_cursor

        after = decode_thread_cursor(request.after_cursor)[1] if request.after_cursor is not None else ""
        rows = [SimpleNamespace(id=key, title=key, working_directory=None, created_at=_now(), originator_session_label=None, originator_agent_instance_id=None, recipient_session_label=None, recipient_agent_instance_id=None) for key in ("thread-1", "thread-2") if key > after]
        return SimpleNamespace(threads=rows, next_cursor=None)

    def read_thread_messages(self, request: Any) -> Any:
        rows = [
            SimpleNamespace(thread_id=request.thread_id, cursor=index, role=SimpleNamespace(value="originator"), kind=SimpleNamespace(value="message"), content=[SimpleNamespace(type="text", text="synthetic")], error=None, metadata={}, created_at=_now(), id=f"message-{request.thread_id}-{index}", action_id=None)
            for index in (1, 2)
            if index > request.after_cursor
        ]
        return SimpleNamespace(messages=rows, next_cursor=2)


def test_actual_safe_sources_resume_after_whole_session_cut() -> None:
    def agent() -> Any:
        sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_session_source_plugin" / "src"))
        from agent_messaging_session_source_plugin.plugin import AgentMessagingSessionSourcePlugin

        plugin = AgentMessagingSessionSourcePlugin()
        plugin.orchestrator_ref = SimpleNamespace(plugin_manager=SimpleNamespace(plugins={"agent_messaging_plugin": _OwningMessagingFixture()}))
        return plugin

    chat = _fixture_module("chatgpt_export_session_source_plugin", "resolve_blob_path_smoke.py") if _plugin_present("chatgpt_export_session_source_plugin") else None
    with tempfile.TemporaryDirectory(prefix="safe_cursor_") as td:
        archive = Path(td) / "export.zip"
        conversations = []
        for index in (1, 2):
            node = f"node-{index}"
            conversations.append(
                {
                    "conversation_id": f"conversation-{index}",
                    "title": "synthetic",
                    "create_time": 1_700_000_000,
                    "update_time": 1_700_000_000,
                    "mapping": {node: {"id": node, "parent": None, "children": [], "message": {"id": node, "author": {"role": "user"}, "create_time": 1_700_000_000, "content": {"content_type": "text", "parts": ["synthetic"]}}}},
                }
            )
        with ZipFile(archive, "w") as stream:
            stream.writestr("conversations.json", json.dumps(conversations))
        for kind in ("agent", "chatgpt"):
            if kind == "agent":
                if not _plugin_present("agent_messaging_session_source_plugin"):
                    continue
                plugin, fresh, root, expected_events = agent(), agent(), "local:agent_messaging", 2
            else:
                if chat is None:
                    continue
                plugin, fresh = chat._build_plugin_with_blob(str(archive))[0], chat._build_plugin_with_blob(str(archive))[0]
                root, expected_events = "fixture-blob", 1
            _safe_source_roundtrip(plugin, fresh, root, expected_events)
    _check(True, "available actual Agent Messaging and ChatGPT resume whole-session cuts without lost IDs")


def test_absent_optional_safe_source_does_not_load_fixture() -> None:
    """A seed without the optional ChatGPT adapter never loads its absent fixture module.

    Agent Messaging, when present, still runs its actual cursor roundtrip; no
    ChatGPT coverage is claimed for the omitted adapter.
    """
    agent_present = _plugin_present("agent_messaging_session_source_plugin")
    module = sys.modules[__name__]
    with (
        patch.object(module, "_plugin_present", side_effect=lambda plugin: agent_present and plugin == "agent_messaging_session_source_plugin"),
        patch.object(module, "_fixture_module", side_effect=AssertionError("absent optional provider fixture was loaded")) as fixture,
        patch.object(module, "_safe_source_roundtrip", wraps=_safe_source_roundtrip) as roundtrip,
    ):
        test_actual_safe_sources_resume_after_whole_session_cut()
    _check(fixture.call_count == 0, "absent optional ChatGPT fixture is never loaded")
    _check(roundtrip.call_count == int(agent_present), "available Agent Messaging keeps its actual cursor roundtrip")


def _safe_source_roundtrip(plugin: Any, fresh: Any, root: str, expected_events: int) -> None:
    """Resume the second conversation/thread through a fresh actual adapter.

    The first pass has already written the adapter-specific discovery cursor.
    Exact distinct vendor IDs expose skipped or replayed source populations."""
    importer, repo, state = runtime_importer(plugin, root, clock=expire_plugin_clock(plugin))
    report = importer.poll_once()
    assert report.sessions_seen == 1, "actual safe source admits one whole session before deadline"
    assert report.events_persisted == expected_events
    restarted, _, _ = runtime_importer(fresh, root, repository=repo, clock=expire_plugin_clock(fresh))
    resumed = restarted.poll_once()
    assert resumed.sessions_seen == 1 and resumed.events_persisted == expected_events, "fresh actual-plugin cursor resumes the other whole session"
    saved_ids = {w.record["vendor_event_id"] for w in state.upserts if w.table == TABLE_EVENT}
    assert len(saved_ids) == 2 * expected_events, "safe-source restart retains exact distinct event IDs"


def test_classification_and_default_seam() -> None:
    from ananta.llm.session_ledger.poll_budget import FILESYSTEM_POLL_KINDS, poll_yield_reason

    for kind in FILESYSTEM_POLL_KINDS:
        assert poll_yield_reason(kind, completed_capability=False) == "awaiting_safe_adapter"
        assert poll_yield_reason(kind, completed_capability=True) is None
    for kind in (IngestSourceKind.CODEX_HISTORY, IngestSourceKind.CLAUDE_CODE_HISTORY, IngestSourceKind.CODEX_GOALS, IngestSourceKind.CODEX_STATE, IngestSourceKind.CODEX_CLOUD):
        assert poll_yield_reason(kind, completed_capability=True) is not None, "unknown and permanent exceptions never opt in"
    plugin = _StubPullingSource()
    ref = ExternalSessionRef("default", None, None, _now())
    stored: dict[str, object] = {"offset": 7}
    assert plugin.prepare_poll_event_cursor("synthetic", ref, stored) == stored
    assert plugin.completed_session_fingerprint("synthetic", ref) is None
    assert plugin.select_oversized_session("synthetic", read_event_cursor=lambda key: None, threshold=1) == OversizedSelection()
    assert plugin.completed_discovery_cursor("synthetic", None, exhausted=True, pending=False, progress=PollDiscoveryProgress()) is None
    _check(True, "explicit allowlist and backwards-compatible default hooks")


def test_lease_loss_and_cursor_replay() -> None:
    plugin = _ContractSource()
    importer, repo, _ = runtime_importer(plugin, "synthetic", lease_ttl=0)
    repo.lose_lease = True
    report = importer.poll_once()
    assert report.batches_failed == 1 and not repo.cursors, "lost lease publishes no event/discovery checkpoint"
    assert not repo.held
    plugin.time = 0
    repo.lose_lease = False
    recovered, _, _ = runtime_importer(plugin, "synthetic", repository=repo)
    recovered.poll_once()
    key = ("runtime-source", CursorScope.EVENT_READ, "contract")
    assert repo.cursors[key]["_poll_completed_version"] == {"version": 1}, "fresh importer retries pending session"
    repo.cursors.clear()  # model the existing explicit cursor-reset API's removed state
    plugin.time = 0
    recovered.poll_once()
    assert repo.cursors[key]["_poll_completed_version"] == {"version": 1}, "reset reopens whole-session checkpoint"
    _check(True, "lease loss/restart/reset cursor lifecycle")


def test_oversized_rows_rotate_and_legacy_stays_dormant() -> None:
    plugin = _ContractSource()
    importer, repo, _ = runtime_importer(plugin, "synthetic")
    plugin.repo = repo
    second = SourceRow("zz-second", IngestSourceKind.CODEX_LOCAL, "synthetic", None, True, {})
    repo.sources.append(second)
    for source in repo.sources:
        repo.cursors[source.id, CursorScope.DISCOVERY, None] = {"version": 1, "state": "caught_up", "after": None}
    importer.poll_one_oversized()
    importer.poll_one_oversized()
    completed = {source_id for source_id, scope, _key in repo.cursors if scope is CursorScope.EVENT_READ}
    assert completed == {"runtime-source", "zz-second"}, "last-completed rotation reaches later pending rows"
    plugin.no_candidate = True
    before = repo.trace.count("write:event_read")
    importer.poll_one_oversized()
    assert repo.trace.count("write:event_read") == before, "none selection leaves cursor writes unchanged"
    unmarked = _StubPullingSource()
    dormant, legacy_repo, _ = runtime_importer(unmarked, "synthetic")
    dormant.poll_one_oversized()
    assert not legacy_repo.trace, "no low-priority hook/cursor/lease call on unsupported adapters"
    _check(True, "oversized stable row rotation and dormant legacy path")


def test_advisory_drain() -> None:
    """Actual adapter diagnostics reach the existing drain logger once per group.

    The file holds two distinct physical unknown records between known input.
    One known assistant record expands into three ledger events, so the event
    tally and physical unknown-line tally intentionally have different units.
    The existing background runner receives the real importer rather than a
    report stub; its independent oversized executor is held idle for this case.
    Capturing the existing completion call proves the report reaches that sink
    without adding a new public result key or logging transcript payloads.

    The next poll exercises successful context retirement. An explicit fixture
    replay then uses targeted poll_source and must count the unknown records
    again, proving deduplication is scoped to a poll rather than global history.
    A raised repository enumeration exercises cleanup before source acquisition.
    Its next poll must retain neither advisories nor a completion offset from
    the failed invocation. These are synthetic cursors in RuntimeRepository;
    no configured runtime source or operator transcript participates.
    """
    from ananta.llm.session_ledger.types import UnknownLineAdvisory
    from ananta.services.session_ledger_service.poll_drain import run_importer_poll_drain
    from ananta.services.session_ledger_service.summary_executor import SummaryExecutor

    fixture = _fixture_module("claude_code_filesystem_session_source_plugin", "claude_code_filesystem_session_source_smoke.py")
    with tempfile.TemporaryDirectory(prefix="advisory_drain_") as td:
        root = Path(td)
        path = root / "synthetic" / (fixture._SID + ".jsonl")
        fixture._write_jsonl(path, [fixture._line_user_text(), {"type": "synthetic-unknown-line-type"}, {"type": "synthetic-unknown-line-type"}, fixture._line_assistant_text_and_two_tool_uses()])
        plugin = fixture._make_plugin()
        importer, repo, _ = runtime_importer(plugin, str(root))
        expected = (UnknownLineAdvisory("runtime-source", fixture._SID, "synthetic-unknown-line-type", 1),)
        with patch("ananta.services.session_ledger_service.poll_drain.logger.info") as log, patch("ananta.llm.session_ledger.poll_advisories.logger.warning") as warning:
            run_importer_poll_drain(importer, cast(SummaryExecutor, SimpleNamespace(submit=lambda _fn: False)))
            completions = [call.args[1] for call in log.call_args_list if call.args[0] == "ledger poll DRAIN complete: %s"]
            assert len(completions) == 1 and completions[0].unknown_line_advisories == expected, "ONE_DRAIN_GROUPED_REPORT"
            assert completions[0].events_persisted == 4 and completions[0].batches_failed == 0, "DRAIN_SUFFIX_WITHOUT_FAILURE"
            warning.assert_not_called()
        empty = importer.poll_once()
        assert empty.unknown_line_advisories == () and empty.events_persisted == 0, "NEXT_POLL_EMPTY_ADVISORIES"
        repo.cursors.clear()  # explicit synthetic replay, never a live cursor
        replay = importer.poll_source("runtime-source")
        assert replay.unknown_line_advisories == expected, "TARGETED_REPLAY_COUNTS_NEW_POLL"
        with patch.object(repo, "list_sources", side_effect=RuntimeError("synthetic exception")):
            try:
                importer.poll_once()
            except RuntimeError:
                pass
            else:
                raise AssertionError("EXCEPTION_SETUP_PATH_NOT_REACHED")
        assert importer.poll_once().unknown_line_advisories == (), "POLL_EXCEPTION_RESETS_COLLECTOR"
    _check(True, "drain and targeted poll expose immutable advisories without per-line warnings")


def main() -> int:
    test_advisory_drain()
    print("=== session_ledger importer_smoke ===")
    test_classification_and_default_seam()
    test_lease_loss_and_cursor_replay()
    test_oversized_rows_rotate_and_legacy_stays_dormant()
    test_actual_safe_sources_resume_after_whole_session_cut()
    test_absent_optional_safe_source_does_not_load_fixture()
    test_actual_legacy_filesystem_sources_remain_whole()
    test_actual_claude_multi_event_line_finishes_whole()
    test_whole_session_deadline()
    test_contract_checkpoints()
    test_oversized_authoritative_lease()
    test_pulling_poll_clean_persists_two_events()
    test_pushed_clean_chunk_persists_and_completes_batch()
    test_pushed_normalize_failure_marks_batch_failed()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    from importer_parse_failures_smoke import main as parse_failures_main

    return parse_failures_main()


if __name__ == "__main__":
    sys.exit(main())
