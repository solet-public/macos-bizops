"""Inverted-bounds-repair domain mixin for the session-ledger repository.

W5.O cycle 7 §3.6: 5 public one-shot operator-repair verbs covering inverted
``first_event_at`` / ``last_event_at`` bounds, stale pgvector-internal
``__summary`` pointers, and orphan running ``__import_batch`` rows.

SQL-lockdown #0, Slice 3b: migrated off raw ``transactional()`` SQL onto the
state-interface primitives (``query_state`` / ``query_ordered`` / ``update_state``
via the ``base.py`` ``_query`` / ``_query_ordered`` / ``_update`` seams). Every
rewrite is ledger-side per the Architect ruling (``workbench/2026-06-20_ledger_
migration_slice_plan.md`` → ``★ 3b GAPS RESOLVED``) — NO new primitives, verbs,
or exceptions:

* **GAP-i** ``last_event_at < first_event_at`` is a CROSS-COLUMN predicate (not
  the col-vs-value filter grammar) → **Python-fold** over a ``query_state`` read.
  The per-session ``min/max(event_at)`` recompute → **2× ``query_ordered``
  limit-1** (asc→min, desc→max, ``(event_at, id)`` tie-break). The
  ``SELECT … FOR UPDATE`` row-lock is dropped: the recompute is deterministic and
  the verb runs inside the importer-poll pause/resume envelope (single writer),
  so the outer inverted-row re-read alone establishes idempotency (a repaired row
  is no longer inverted and is not re-selected on a re-run).
* **GAP-ii** ``embedding_vector_id LIKE 'emb-%'`` is a LIKE pattern (not in the
  grammar) → **Python ``str.startswith('emb-')``** over a ``query_state`` read.
* **GAP-iii** the cross-table ``UPDATE … FROM <pgvector>__embeddings`` join is
  replaced by **PATH-1 ledger-side recompute**: each broken ``__summary`` row's
  deterministic ``external_id`` is ``f"{session_id}:{chunk_index}"`` (the id
  ``SummaryWriter._store_vector`` supplies at ``summarization.py:234``), recomputed
  from the row's OWN columns and written back with a conditional ``update_state``
  keyed on the current internal pointer — zero pgvector access, no cross-namespace
  read, no join primitive.

The ``started_at < cutoff`` orphan-batch range predicate (also not in the
grammar) is handled the same way: the small running-batch set is read by equality
and Python-filtered on the cutoff, then the stale ids are failed in one ``=ANY``
``update_state`` whose ``status = RUNNING`` predicate IS the compare-and-set.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from ananta.llm.session_ledger.base import SessionLedgerRepositoryBase
from ananta.llm.session_ledger.schema import (
    TABLE_EVENT,
    TABLE_IMPORT_BATCH,
    TABLE_SESSION,
    TABLE_SUMMARY,
)
from ananta.llm.session_ledger.shared import _strip_nuls
from ananta.llm.session_ledger.types import ImportBatchStatus

RELOAD_SAFE = True

logger = logging.getLogger(__name__)

_EMB_INTERNAL_PREFIX = "emb-"


def _as_dt(value: object) -> datetime:
    """Coerce a state-primitive timestamp cell to an aware-UTC ``datetime``.

    The ledger stores timestamps as Postgres ``TIMESTAMP`` (no tz) holding the
    naive-UTC wall-clock (the 2026-06-12 F1 TZ-storage seam), so ``query_state`` /
    ``query_ordered`` surface them as **naive** datetimes (or naive ISO strings via
    ``provider._serialize_for_json``); an aware ``datetime`` can still arrive from
    an in-memory test double on a ``timestamptz`` column. Both normalize to an
    **aware UTC** ``datetime`` — naive cells are taken as UTC — so the cross-column
    and ``started_at < cutoff`` comparisons (the cutoff being an aware
    ``datetime.now(UTC)``) never mix naive and aware operands and are
    instant-correct rather than lexicographic. Fail fast on any other cell type —
    a non-timestamp here is an upstream contract violation, not something to
    silently coerce.
    """
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.fromisoformat(value)
    else:
        raise TypeError(
            f"expected a datetime or ISO-8601 string timestamp cell, got "
            f"{type(value).__name__!r}",
        )
    return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)


def _is_inverted(row: dict[str, object]) -> bool:
    """True iff a ``__session`` row's ``last_event_at`` precedes its ``first_event_at``.

    A row missing either bound is NOT inverted — mirroring the pre-migration SQL
    ``last_event_at < first_event_at`` whose ``<`` yields NULL (unmatched) on a
    NULL operand.
    """
    first = row.get("first_event_at")
    last = row.get("last_event_at")
    if first is None or last is None:
        return False
    return _as_dt(last) < _as_dt(first)


def _has_internal_pointer(row: dict[str, object]) -> bool:
    """True iff a ``__summary`` row still carries an internal ``emb-`` pointer."""
    pointer = row.get("embedding_vector_id")
    return isinstance(pointer, str) and pointer.startswith(_EMB_INTERNAL_PREFIX)


def _summary_external_id(row: dict[str, object]) -> str | None:
    """Recompute the deterministic ``external_id`` from a ``__summary`` row's own columns.

    Mirrors ``SummaryWriter._store_vector`` (``summarization.py:234``):
    ``f"{session_id}:{chunk_index}"``. Returns ``None`` when either column is
    absent so the caller can skip (rather than mint a malformed pointer).
    ``chunk_index`` is checked with ``is None`` — ``0`` is a valid first chunk.
    """
    session_id = row.get("session_id")
    chunk_index = row.get("chunk_index")
    if session_id is None or chunk_index is None:
        return None
    return f"{session_id}:{chunk_index}"


def _orphan_result(*, total: int, remaining: int) -> dict[str, int]:
    """Build the orphan-batch return envelope from before/after RUNNING counts.

    Post-state contract (matches the pre-migration SQL, which re-counted RUNNING
    after its UPDATE): ``repaired`` is the net reduction in RUNNING rows
    (``total - remaining``) and ``untouched`` is the rows STILL RUNNING
    (``remaining``) — the meaning the public metadata documents. A row a
    concurrent owner completed out of RUNNING is therefore neither lost from the
    repaired tally nor mis-counted as untouched.
    """
    return {
        "repaired_count": total - remaining,
        "untouched_count": remaining,
        "total_orphan_count_before": total,
    }


class SessionLedgerInvertedBoundsRepairMixin(SessionLedgerRepositoryBase):
    """Inverted-bounds-repair domain mixin (one-shot operator repairs)."""

    __slots__ = ()

    # ── GAP-i: inverted first/last event_at bounds ───────────────────────────

    def count_inverted_first_last_event_at_sessions(self) -> int:
        """Count live ``__session`` rows where ``last_event_at < first_event_at``.

        Cross-column predicate (GAP-i) → Python-fold over a ``query_state`` read
        of the live sessions (``query_state`` does not inject the soft-delete
        filter, so ``is_deleted: 0`` is passed explicitly).
        """
        rows = self._query(TABLE_SESSION, {"is_deleted": 0})
        return sum(1 for row in rows if _is_inverted(row))

    def repair_inverted_first_last_event_at(self) -> int:
        """Recompute ``first_event_at`` / ``last_event_at`` for inverted rows.

        For each inverted session the new bounds are the live-event extremes,
        each read as a single indexed ``query_ordered`` row (asc→min, desc→max)
        so a 32K-event session is never materialized. A session with no live
        events keeps its bounds (logged) — there is nothing to recompute from.

        The pre-migration ``SELECT … FOR UPDATE`` row-lock is gone: the verb runs
        inside the importer-poll pause/resume envelope (single writer) and the
        recompute is deterministic, so the ``update_state`` is filtered on ``id``
        alone — the outer inverted-row re-read makes a re-run a no-op (a repaired
        row is no longer inverted). Returns the rows-affected total.
        """
        repaired = 0
        now = self._clock()
        for row in self._query(TABLE_SESSION, {"is_deleted": 0}):
            if not _is_inverted(row):
                continue
            session_id = str(row["id"])
            new_first = self._extreme_event_at(session_id, direction="asc")
            new_last = self._extreme_event_at(session_id, direction="desc")
            if new_first is None or new_last is None:
                logger.warning(
                    "repair_inverted_first_last_event_at: session %s has no "
                    "events; leaving inverted bounds in place",
                    session_id,
                )
                continue
            repaired += self._update(
                TABLE_SESSION,
                {"id": session_id, "is_deleted": 0},
                {
                    "first_event_at": new_first,
                    "last_event_at": new_last,
                    "updated_at": now,
                },
            )
        return repaired

    def _extreme_event_at(
        self, session_id: str, *, direction: str
    ) -> datetime | None:
        """Read the min (``asc``) or max (``desc``) ``event_at`` for a session.

        One ``query_ordered`` limit-1 read with an ``(event_at, id)`` composite so
        the order is total + tie-safe; ``query_ordered`` applies ``is_deleted = 0``.
        Returns ``None`` when the session has no live events.
        """
        rows = self._query_ordered(
            TABLE_EVENT,
            filters={"session_id": session_id},
            order_by=[["event_at", direction], ["id", direction]],
            limit=1,
        )
        if not rows:
            return None
        event_at = rows[0].get("event_at")
        return _as_dt(event_at) if event_at is not None else None

    # ── GAP-ii / GAP-iii: stale pgvector-internal summary pointers ────────────

    def count_summary_rows_with_pgvector_internal_id_pointer(self) -> int:
        """Count active ``__summary`` rows whose ``embedding_vector_id`` is an internal ``emb-`` id.

        LIKE pattern (GAP-ii) → Python ``str.startswith('emb-')`` over a
        ``query_state`` read; NULL / non-str pointers do not match, mirroring SQL
        ``LIKE``.
        """
        rows = self._query(TABLE_SUMMARY, {"is_deleted": 0})
        return sum(1 for row in rows if _has_internal_pointer(row))

    def repair_summary_embedding_vector_ids(self) -> dict[str, int]:
        """Rewrite stale ``__summary.embedding_vector_id`` values to the deterministic external_id.

        GAP-iii PATH-1: each broken row's ``external_id`` is recomputed from its
        OWN ``session_id`` + ``chunk_index`` and written back with a conditional
        ``update_state`` keyed on ``(id, the-current-emb-pointer)`` — both plain
        strings, no datetime in the WHERE — so a row a concurrent pass already
        rewrote matches 0 rows (no double-write). Zero pgvector access; the
        pre-migration cross-table ``UPDATE … FROM`` join is gone.

        The return shape preserves the pre-migration contract, which is a
        **post-state** report — so the counts are derived from a fresh recount
        AFTER the writes (the original SQL re-`SELECT count`-ed after its UPDATE),
        NOT from this pass's own rows-affected. That matters under concurrency: a
        pointer-CAS we LOSE to a concurrent repair returns 0 rows-affected, but
        the row is now CORRECT — counting our own hits would mis-report it as
        skipped and undercount the correct rows. Deriving from before/after broken
        counts keeps each field's documented post-state meaning:

        * ``updated_count`` — net reduction in internal-pointer rows
          (``broken_before - broken_after``);
        * ``skipped_count`` — rows STILL carrying an internal pointer at
          completion (``broken_after`` — missing ``session_id`` / ``chunk_index``,
          genuinely unfixed);
        * ``total_rows_now_correct`` — active rows no longer internal
          (``active_after - broken_after``).
        """
        now = self._clock()
        active_before = self._query(TABLE_SUMMARY, {"is_deleted": 0})
        broken_before = sum(1 for row in active_before if _has_internal_pointer(row))
        for row in active_before:
            if not _has_internal_pointer(row):
                continue
            external_id = _summary_external_id(row)
            if external_id is None:
                logger.warning(
                    "repair_summary_embedding_vector_ids: summary %s lacks "
                    "session_id/chunk_index; cannot recompute external_id, "
                    "leaving internal pointer in place",
                    row.get("id"),
                )
                continue
            self._update(
                TABLE_SUMMARY,
                {
                    "id": str(row["id"]),
                    "embedding_vector_id": row["embedding_vector_id"],
                    "is_deleted": 0,
                },
                {"embedding_vector_id": external_id, "updated_at": now},
            )
        active_after = self._query(TABLE_SUMMARY, {"is_deleted": 0})
        broken_after = sum(1 for row in active_after if _has_internal_pointer(row))
        return {
            "updated_count": broken_before - broken_after,
            "skipped_count": broken_after,
            "total_rows_now_correct": len(active_after) - broken_after,
        }

    # ── orphan running import batches (started_at < cutoff range) ─────────────

    def backfill_orphan_running_batches_for_source(
        self,
        source_id: str,
        *,
        source_kind: str | None = None,
        stale_threshold_seconds: int = 86400,
    ) -> dict[str, int]:
        """Mark stale running ``__import_batch`` rows for a source as failed.

        ``started_at < cutoff`` is a range predicate (not in the filter grammar):
        the per-source running-batch set is small + bounded, so it is read by
        equality (``source_id`` + ``status = RUNNING``) and Python-filtered on the
        cutoff. The stale ids are failed in one ``=ANY`` ``update_state`` guarded
        on ``status = RUNNING`` — the predicate IS the compare-and-set, so a batch
        a concurrent pass already failed matches 0 rows.

        Return shape preserves the pre-migration **post-state** contract: the
        counts come from a fresh RUNNING recount AFTER the CAS (not the status-CAS
        rows-affected). ``repaired_count`` = net reduction in RUNNING
        (``total - remaining``), ``untouched_count`` = rows STILL RUNNING
        (``remaining``), ``total_orphan_count_before`` = RUNNING rows seen before.
        Deriving from the recount means a batch a concurrent owner completes out
        of RUNNING mid-repair is not mis-reported as ``untouched`` (it is no
        longer running) even though our ``status = RUNNING`` CAS missed it.
        """
        if source_kind is not None:
            source_row = self.get_source(source_id)  # type: ignore[attr-defined]
            if source_row is None or source_row.source_kind.value != source_kind:
                return _orphan_result(total=0, remaining=0)
        now = self._clock()
        cutoff = now - timedelta(seconds=stale_threshold_seconds)
        running = self._running_batches(source_id)
        total = len(running)
        if total == 0:
            return _orphan_result(total=0, remaining=0)
        stale_ids: list[str] = []
        for row in running:
            started_at = row.get("started_at")
            if started_at is not None and _as_dt(started_at) < cutoff:
                stale_ids.append(str(row["id"]))
        if stale_ids:
            self._update(
                TABLE_IMPORT_BATCH,
                {
                    "id": stale_ids,
                    "status": ImportBatchStatus.RUNNING.value,
                    "is_deleted": 0,
                },
                {
                    "status": ImportBatchStatus.FAILED.value,
                    "finished_at": now,
                    "error_message": _strip_nuls(
                        "orphan_repair: stale running batch repaired"
                    ),
                    "error_kind": _strip_nuls("orphan_repair"),
                    "updated_at": now,
                },
            )
        remaining = len(self._running_batches(source_id))
        return _orphan_result(total=total, remaining=remaining)

    def _running_batches(self, source_id: str) -> list[dict[str, object]]:
        """Live RUNNING ``__import_batch`` rows for one source (before + recount)."""
        return self._query(
            TABLE_IMPORT_BATCH,
            {
                "source_id": source_id,
                "status": ImportBatchStatus.RUNNING.value,
                "is_deleted": 0,
            },
        )


__all__ = ["RELOAD_SAFE", "SessionLedgerInvertedBoundsRepairMixin"]
