#!/usr/bin/env python3
"""Offline fold smoke for the SQL-lockdown census migration (GAP-1, no pytest).

The census ``census_source_rows`` retires the multi-CTE
``bit_xor(hashtextextended(...))`` aggregate onto a Python fold over the typed
``query_state`` / ``query_ordered`` primitives (operator D1, 2026-06-20 —
Python-fold + re-baseline, NO aggregate primitive). The fold logic lives in
``read_support`` as a DB-free :class:`_CensusAggregator` plus the
keyset-paging :func:`fold_census_events` / :func:`build_census`, which take the
read seam as an INJECTED callback — so the whole composition is unit-testable
here with a fake reader, no live DB.

This smoke proves the parts the live-schema smoke cannot make cheap or
deterministic:

* per-source session counts + canonical/sibling split;
* event ``event_count`` + the two-seed fingerprint, attributed to the event's
  *session's* source, and DROPPING events/tool_calls whose session is not live
  (the retired ``JOIN __session … AND s.is_deleted = 0``);
* fingerprint DETERMINISM (build twice → byte-identical) — the whole reason for
  ``blake2b`` over the salted builtin ``hash()``;
* fingerprint ORDER-INDEPENDENCE (the XOR) — pages folded in any order, and at
  any page size, yield the identical result, so the ``id``-keyset paging is
  faithful to the retired set-wise aggregate;
* a source with NO live events carries ``None`` fingerprints (the old LEFT JOIN
  NULL), distinct from a present-but-zero XOR;
* import-batch health (owned-running vs unclaimed-route) + oldest-running age;
* output rows ordered ``(source_kind, source_id)`` with exactly the keys the
  ``service.census`` consumer reads.

Run:
    .venv/bin/python3 ananta/tests/llm/session_ledger/census_fold_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.llm.session_ledger.read_support import (  # noqa: E402
    _CensusAggregator,
    build_census,
)
from ananta.llm.session_ledger.repository import (  # noqa: E402
    SessionLedgerRepository,
)
from ananta.llm.session_ledger.schema import (  # noqa: E402
    TABLE_EVENT,
    TABLE_IMPORT_BATCH,
    TABLE_SESSION,
    TABLE_SOURCE,
    TABLE_TOOL_CALL,
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


_NOW = datetime(2026, 6, 15, 12, 0, 0)  # naive UTC — what the mixin passes
_SEEDS = (0, 527612190)


def _iso(seconds_before: int) -> str:
    # started_at reads back as a naive-UTC ISO string (the live read path).
    return (_NOW - timedelta(seconds=seconds_before)).isoformat()


# --- Fixtures -------------------------------------------------------------
# Two live sources + a third with sessions but zero events. Sessions s1/s2 ->
# src-A (one canonical, one sibling), s3 -> src-B (canonical), s4 -> src-C
# (canonical, no events). Events/tool_calls for the non-live session "s-del"
# must be DROPPED (its row is absent from the live `sessions` read).
_SOURCES: list[dict[str, object]] = [
    {"id": "src-A", "source_kind": "claude_code", "root_uri": "/a"},
    {"id": "src-B", "source_kind": "codex", "root_uri": "/b"},
    {"id": "src-C", "source_kind": "zzz_other", "root_uri": "/c"},
]
_SESSIONS: list[dict[str, object]] = [
    {"id": "s1", "source_id": "src-A", "canonical_external_session_id": None},
    {"id": "s2", "source_id": "src-A", "canonical_external_session_id": "ext-x"},
    {"id": "s3", "source_id": "src-B", "canonical_external_session_id": None},
    {"id": "s4", "source_id": "src-C", "canonical_external_session_id": None},
]
_EVENTS: list[dict[str, object]] = [
    {"id": "e1", "session_id": "s1", "content_blob_id": None},
    {"id": "e2", "session_id": "s1", "content_blob_id": "b2"},
    {"id": "e3", "session_id": "s2", "content_blob_id": None},
    {"id": "e4", "session_id": "s3", "content_blob_id": "b4"},
    {"id": "e5", "session_id": "s-del", "content_blob_id": "b5"},  # dropped
]
_TOOL_CALLS: list[dict[str, object]] = [
    {"id": "t1", "session_id": "s1"},
    {"id": "t2", "session_id": "s3"},
    {"id": "t3", "session_id": "s-del"},  # dropped
]
_IMPORT_BATCHES: list[dict[str, object]] = [
    {"id": "ib1", "source_id": "src-A", "status": "running",
     "polling_lease_token": "tok", "started_at": _iso(100)},
    {"id": "ib2", "source_id": "src-A", "status": "running",
     "polling_lease_token": None, "started_at": _iso(50)},
    {"id": "ib3", "source_id": "src-B", "status": "completed",
     "polling_lease_token": None, "started_at": _iso(999)},
    {"id": "ib4", "source_id": "src-A", "status": "running",
     "polling_lease_token": "tok2", "started_at": _iso(200)},  # oldest
]


class _FakeReader:
    """In-memory stand-in for the mixin's bound ``_query`` / ``_query_ordered``.

    ``query`` returns the planted list for a table verbatim (the bounded reads);
    ``query_ordered`` FAITHFULLY implements the ``id``-keyset cursor — sort by
    ``id`` ASC, apply the Gap-A ``id > last_id`` lower bound, slice to ``limit``
    — so the paging loop in ``fold_census_events`` is exercised for real (a
    skip/dup would corrupt the counts).
    """

    def __init__(self, tables: dict[str, list[dict[str, Any]]]) -> None:
        self._tables = tables

    def query(
        self, table: str, filters: dict[str, object],
    ) -> list[dict[str, object]]:
        # Honor the is_deleted predicate the bounded reads pass (faithful to
        # query_state; all fixtures are live so this is a pass-through here).
        deleted = filters.get("is_deleted")
        return [
            dict(row)
            for row in self._tables.get(table, [])
            if deleted is None or row.get("is_deleted", 0) == deleted
        ]

    def query_ordered(
        self,
        table: str,
        *,
        filters: dict[str, object],
        order_by: list[list[str]],
        limit: int,
    ) -> list[dict[str, object]]:
        cols = [pair[0] for pair in order_by]
        rows = sorted(
            self._tables.get(table, []),
            key=lambda r: tuple(str(r.get(col, "")) for col in cols),
        )
        op = filters.get("id")
        if isinstance(op, dict) and op.get("op") == "gt":
            last = str(op["value"])
            rows = [r for r in rows if str(r["id"]) > last]
        return [dict(row) for row in rows[:limit]]


class _StateServiceShim:
    """Filter-honoring StateManagementInterface stand-in over ``_FakeReader``.

    Lets the REAL :meth:`SessionLedgerRepository.census_source_rows` run offline
    — exercising the public method + the base ``_query`` / ``_query_ordered``
    envelope extraction + the delegator's ``_naive_utc(self._clock())`` clock
    normalization + the batch-age subtraction TOGETHER (the other tests call
    ``build_census`` with an already-naive ``now``, so they bypass exactly the
    tz-aware-clock-vs-naive-``started_at`` path). census never opens a
    transaction, so ``transactional`` is intentionally absent.
    """

    def __init__(self, reader: _FakeReader) -> None:
        self._reader = reader

    def query_state(self, namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        filters = query.get("filters") or {}
        rows = self._reader.query(
            str(query["table"]),
            cast("dict[str, object]", filters) if isinstance(filters, dict) else {},
        )
        return {"action_status": "completed", "data": {"records": rows},
                "actions": [], "error": None}

    def query_ordered(self, namespace: str, data: dict[str, Any]) -> dict[str, Any]:
        rows = self._reader.query_ordered(
            str(data["table"]),
            filters=cast("dict[str, object]", data["filters"]),
            order_by=cast("list[list[str]]", data["order_by"]),
            limit=int(cast("int", data["limit"])),
        )
        return {"action_status": "completed", "data": {"records": rows},
                "actions": [], "error": None}


def _reader() -> _FakeReader:
    return _FakeReader(
        {
            TABLE_SOURCE: _SOURCES,
            TABLE_SESSION: _SESSIONS,
            TABLE_EVENT: _EVENTS,
            TABLE_TOOL_CALL: _TOOL_CALLS,
            TABLE_IMPORT_BATCH: _IMPORT_BATCHES,
        }
    )


def _walker(reader: _FakeReader) -> Any:
    """A ``walk`` seam over the fake that yields rows ONE AT A TIME.

    A GENERATOR on purpose. ``build_census`` streams its three large tables now
    (read-cap sweep, 2026-08-16), and a fake that handed back a list would let a
    double-consumption bug pass unnoticed — which is exactly the defect this
    change had to fix: ``_CensusAggregator`` used to walk ``sessions`` twice, once
    to build the id->source index and once to tally. With lists that is merely
    wasteful; with generators the second pass sees an exhausted iterator and every
    session tally silently comes back EMPTY.

    ``counts`` records how many times each table was walked, so
    ``test_census_streams_each_table_exactly_once`` can assert the property
    directly rather than inferring it from the tallies being right.
    """

    class _Walk:
        def __init__(self) -> None:
            self.counts: dict[str, int] = {}

        def __call__(
            self, table: str, filters: dict[str, Any], *, ceiling: int, reason: str,
        ) -> Any:
            self.counts[table] = self.counts.get(table, 0) + 1
            yield from (dict(row) for row in reader.query(table, {**filters, "is_deleted": 0}))

    return _Walk()


def _census(*, page_size: int) -> list[dict[str, object]]:
    reader = _reader()
    return build_census(
        query=reader.query,
        query_ordered=reader.query_ordered,
        walk=_walker(reader),
        now=_NOW,
        page_size=page_size,
        fingerprint_seeds=_SEEDS,
    )


def _by_source(rows: list[dict[str, object]]) -> dict[str, dict[str, object]]:
    return {str(r["source_id"]): r for r in rows}


def test_per_source_tallies() -> None:
    rows = _census(page_size=100)
    by = _by_source(rows)

    _check(len(rows) == 3, "one census row per live source")
    _check(
        [str(r["source_id"]) for r in rows] == ["src-A", "src-B", "src-C"],
        "rows ordered (source_kind, source_id): claude_code, codex, zzz_other",
    )

    a = by["src-A"]
    _check(
        a["session_count"] == 2
        and a["canonical_count"] == 1
        and a["sibling_count"] == 1,
        "src-A session counts: 2 total, 1 canonical, 1 sibling",
    )
    _check(a["event_count"] == 3, "src-A event_count=3 (e1,e2,e3; e5/s-del dropped)")
    _check(a["tool_call_count"] == 1, "src-A tool_call_count=1 (t1; t3/s-del dropped)")
    _check(
        a["owned_running_batches"] == 2 and a["unclaimed_route_batches"] == 1,
        "src-A batch health: 2 owned-running, 1 unclaimed-route (completed ignored)",
    )
    _check(
        a["oldest_running_batch_age_seconds"] == 200,
        "src-A oldest-running age = now - min(started_at over running) = 200s",
    )
    _check(
        isinstance(a["fingerprint_a"], int) and isinstance(a["fingerprint_b"], int),
        "src-A has present (non-None) two-seed fingerprints",
    )

    b = by["src-B"]
    _check(
        b["session_count"] == 1 and b["canonical_count"] == 1
        and b["sibling_count"] == 0 and b["event_count"] == 1
        and b["tool_call_count"] == 1,
        "src-B: 1 canonical session, 1 event, 1 tool_call",
    )
    _check(
        b["owned_running_batches"] == 0
        and b["unclaimed_route_batches"] == 0
        and b["oldest_running_batch_age_seconds"] is None,
        "src-B has no running batches → 0/0/None",
    )

    c = by["src-C"]
    _check(
        c["session_count"] == 1 and c["event_count"] == 0
        and c["tool_call_count"] == 0,
        "src-C: 1 session, no events, no tool_calls",
    )
    _check(
        c["fingerprint_a"] is None and c["fingerprint_b"] is None,
        "src-C (no live events) carries None fingerprints (old LEFT JOIN NULL)",
    )


def test_expected_output_keys() -> None:
    rows = _census(page_size=100)
    expected = {
        "source_id", "source_kind", "root_uri",
        "session_count", "canonical_count", "sibling_count",
        "event_count", "fingerprint_a", "fingerprint_b", "tool_call_count",
        "owned_running_batches", "unclaimed_route_batches",
        "oldest_running_batch_age_seconds",
    }
    _check(
        all(set(r.keys()) == expected for r in rows),
        "every census row has exactly the service.census-consumed keys",
    )


def test_determinism_and_paging_invariance() -> None:
    # Build twice — fingerprints must be byte-identical (blake2b, not salted hash()).
    first = _by_source(_census(page_size=100))
    second = _by_source(_census(page_size=100))
    _check(
        first["src-A"]["fingerprint_a"] == second["src-A"]["fingerprint_a"]
        and first["src-A"]["fingerprint_b"] == second["src-A"]["fingerprint_b"],
        "fingerprint is deterministic across runs (re-baseline-safe)",
    )
    # Page size must not change the result — exercises multi-page keyset paging
    # (2+2+1) and proves no row is skipped or double-counted at a page boundary.
    paged = _by_source(_census(page_size=2))
    single = first
    _check(
        all(
            paged[s]["event_count"] == single[s]["event_count"]
            and paged[s]["fingerprint_a"] == single[s]["fingerprint_a"]
            and paged[s]["fingerprint_b"] == single[s]["fingerprint_b"]
            for s in ("src-A", "src-B", "src-C")
        ),
        "page_size=2 (multi-page) == page_size=100 (single page): paging is faithful",
    )


def test_fingerprint_order_independence() -> None:
    # The XOR fold must be order-independent: fold the SAME events split into
    # pages in one order vs the reverse → identical fingerprints.
    def fold(pages: list[list[dict[str, object]]]) -> _CensusAggregator:
        agg = _CensusAggregator(
            sources=_SOURCES, sessions=_SESSIONS, tool_calls=_TOOL_CALLS,
            import_batches=_IMPORT_BATCHES, fingerprint_seeds=_SEEDS,
        )
        for page in pages:
            agg.fold_event_page(page)
        return agg

    forward = _by_source(fold([_EVENTS[:2], _EVENTS[2:]]).result(now=_NOW))
    reverse = _by_source(
        fold([list(reversed(_EVENTS[2:])), list(reversed(_EVENTS[:2]))]).result(
            now=_NOW,
        )
    )
    _check(
        forward["src-A"]["fingerprint_a"] == reverse["src-A"]["fingerprint_a"]
        and forward["src-A"]["fingerprint_b"] == reverse["src-A"]["fingerprint_b"]
        and forward["src-B"]["fingerprint_a"] == reverse["src-B"]["fingerprint_a"],
        "fingerprint is order-independent (XOR) across re-ordered pages",
    )


def test_fingerprint_distinguishes_content() -> None:
    # Two corpora differing only in one event's content_blob_id must produce
    # different fingerprints (the fold actually folds the blob identity).
    base = _by_source(_census(page_size=100))["src-A"]
    reader = _FakeReader(
        {
            TABLE_SOURCE: _SOURCES,
            TABLE_SESSION: _SESSIONS,
            TABLE_EVENT: [
                {"id": "e1", "session_id": "s1", "content_blob_id": "CHANGED"},
                *_EVENTS[1:],
            ],
            TABLE_TOOL_CALL: _TOOL_CALLS,
            TABLE_IMPORT_BATCH: _IMPORT_BATCHES,
        }
    )
    mutated = _by_source(
        build_census(
            query=reader.query, query_ordered=reader.query_ordered,
            walk=_walker(reader),
            now=_NOW, page_size=100, fingerprint_seeds=_SEEDS,
        )
    )["src-A"]
    _check(
        base["fingerprint_a"] != mutated["fingerprint_a"],
        "changing one event's content_blob_id changes the source fingerprint",
    )


def test_real_method_clock_normalization() -> None:
    # Run the REAL public census_source_rows() with a TZ-AWARE production-shape
    # clock (datetime.now(UTC) form) so the delegator's _naive_utc normalization
    # meets the naive started_at subtraction. A tz-aware − naive bug would
    # TypeError ONLY when a running batch exists — invisible to every other
    # (naive-now) test here. The clock is fixed-tz-aware (12:00:00 UTC) so the
    # oldest running batch (started 200s earlier) yields a deterministic age.
    repo = SessionLedgerRepository(
        state_service=cast("Any", _StateServiceShim(_reader())),
        clock=lambda: datetime(2026, 6, 15, 12, 0, 0, tzinfo=UTC),
    )
    rows = repo.census_source_rows()
    by = _by_source(rows)
    a = by["src-A"]
    _check(
        a["oldest_running_batch_age_seconds"] == 200,
        "real census_source_rows(): tz-aware clock normalized to naive UTC → "
        "batch age = 200s (no tz-aware − naive TypeError)",
    )
    _check(
        a["event_count"] == 3 and a["fingerprint_a"] is not None,
        "real census_source_rows() folds events through the base _query_ordered "
        "envelope seam",
    )


def test_census_streams_each_table_exactly_once() -> None:
    """Each large table is walked ONCE, and the tallies survive being streamed.

    Read-cap sweep, 2026-08-16 (lane-ak). ``build_census`` used to read
    ``session`` / ``tool_call`` / ``import_batch`` WHOLE through ``query`` —
    measured 27,208 / 637,496 / 284,787 rows, and refused outright on the serving
    release (``cap_rows: 100`` on ``session``), so ``census`` was dead rather than
    merely at risk. They stream now.

    Streaming exposed a latent trap that lists had been hiding:
    ``_CensusAggregator`` consumed ``sessions`` TWICE — once to build the
    id->source index, once to tally. Harmless for a list; with a generator the
    second pass sees an exhausted iterator and **every session tally silently
    comes back empty, with no error**. The two passes are now one.

    This asserts the property directly (walk count == 1 per table) rather than
    inferring it from the tallies being correct, because a future refactor could
    reintroduce a second pass over a re-materialized list and leave the tallies
    right while quietly discarding the memory guarantee.
    """
    reader = _reader()
    walker = _walker(reader)
    rows = build_census(
        query=reader.query,
        query_ordered=reader.query_ordered,
        walk=walker,
        now=_NOW,
        page_size=100,
        fingerprint_seeds=_SEEDS,
    )
    for table in (TABLE_SESSION, TABLE_TOOL_CALL, TABLE_IMPORT_BATCH):
        _check(
            walker.counts.get(table) == 1,
            f"{table} was walked exactly once (got {walker.counts.get(table)})",
        )
    by = _by_source(rows)
    _check(
        by["src-A"]["session_count"] > 0,
        f"session tallies survived streaming (src-A session_count="
        f"{by['src-A']['session_count']}) — 0 here is the exhausted-iterator bug",
    )
    _check(
        by["src-A"]["tool_call_count"] > 0,
        "tool_call tallies survived streaming — they depend on the session index "
        "being fully built BEFORE the tool_call stream is consumed",
    )


def main() -> int:
    print("=== census_fold_smoke ===")
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            print(name)
            fn()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
