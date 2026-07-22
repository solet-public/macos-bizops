"""Read/query domain mixin for the session-ledger repository.

W5.O cycle 2 (`workbench/2026-06-13_w5o_session_ledger_repository_decomposition_design.md`
§3.1): the 12 read-side methods relocate from the monolith
``SessionLedgerRepository`` into this mixin. Each method body is lifted
verbatim; public signatures preserved per the cycle invariant (§5.4).

The mixin's verbs back the ``SessionLedgerReadAPI`` service-layer ABC plus the
read-side helpers consumed by other domains (notably ``list_quiescent_sessions``
which the SummarizeAPI auto-summarize cron reads, ``list_sessions_by_ids``
which SearchAPI's ``search_sessions`` reads for ID-to-envelope resolution, and
``find_latest_away_summary_for_session`` which the M6 D8 hybrid-extraction
service path reads).

Per Architect's §3.1 mapping the 12 methods are:

- ``list_sources``, ``list_sessions``, ``list_active_sessions``,
  ``get_session_timeline``, ``list_tool_calls`` (5 direct verb implementations)
- ``list_quiescent_sessions``, ``list_sessions_by_ids``,
  ``find_session_id_by_external_session_id``, ``find_event_id_by_vendor_id``,
  ``find_call_event_id_for_resolution``, ``find_latest_away_summary_for_session``,
  ``fetch_all_events_for_session`` (7 supporting helpers consumed across domains)

``get_source`` and ``find_source_id_by_kind_and_root_uri`` stay in
``repository.py`` until cycle 3 — per Architect's §3.2 mapping they relocate to
``ingest.py`` as after-insert verification helpers rather than to read.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import cast

from ananta.llm.session_ledger.base import SessionLedgerRepositoryBase, _naive_utc
from ananta.llm.session_ledger.read_support import (
    SessionWindow,
    _merge_active_leases,
    _pick_canonical_session_id,
    _select_latest_away_summary,
    build_canonical_contributors_via_group,
    build_census,
    list_sessions_via_junction,
    select_quiescent_sessions,
)
from ananta.llm.session_ledger.schema import (
    TABLE_ACTIVE_LEASE,
    TABLE_EVENT,
    TABLE_SESSION,
    TABLE_SOURCE,
    TABLE_SUMMARY,
    TABLE_TOOL_CALL,
)
from ananta.llm.session_ledger.shared import (
    SourceRow,
    _row_to_source,
)
from ananta.llm.session_ledger.types import (
    EventType,
    IngestSourceKind,
    SessionsOrderBy,
    SourceVendor,
)

# Module-level RELOAD_SAFE marker — pure mixin, no module-level mutable state.
RELOAD_SAFE = True


def _naive_dt(value: datetime | None) -> datetime | None:
    """Normalize an optional tz-aware datetime filter bound to naive UTC (F1 seam).

    ``list_sessions`` passes the window bounds to the Python fold, which compares
    naive-vs-naive against the naive-UTC ``*_event_at`` columns; a ``None`` bound
    stays ``None`` (unbounded).
    """
    return cast("datetime", _naive_utc(value)) if value is not None else None

# Two well-separated seeds for the census row-identity fingerprint — two
# order-independent XOR folds under distinct ``blake2b`` personalizations cut the
# single-seed collision risk. See :func:`read_support._fingerprint_component` for
# why the deterministic hash (NOT the salted builtin ``hash()``).
_FINGERPRINT_SEED_A = 0
_FINGERPRINT_SEED_B = 527612190

# Keyset page size for the census ``__event`` scan. The Gap-C cap
# (``_MAX_ORDERED_LIMIT`` = 100) is the max per ``query_ordered`` page, so it
# also minimizes the round-trip count over the ~1M-row corpus. Held at module
# level so a smoke can shrink it to exercise the multi-page cursor cheaply.
_CENSUS_EVENT_PAGE_SIZE = 100


class SessionLedgerReadMixin(SessionLedgerRepositoryBase):
    """Read/query domain mixin.

    Inherits the typed read seam (``_query`` / ``_query_ordered``) + ``_state``
    + ``_clock`` from :class:`SessionLedgerRepositoryBase`. Every method here is
    exposed on the concrete ``SessionLedgerRepository`` via MI composition;
    callers see no change vs the pre-decomposition monolith.
    """

    __slots__ = ()

    # ------------------------------------------------------------------
    # Source listing
    # ------------------------------------------------------------------

    def list_sources(self, *, enabled_only: bool = True) -> list[SourceRow]:
        filters: dict[str, object] = {"is_deleted": 0}
        if enabled_only:
            filters["enabled"] = True
        rows = self._query(TABLE_SOURCE, filters)
        # query_state gives no ordering; preserve created_at ASC (a NOT NULL
        # timestamp whose on-read form sorts chronologically as a string).
        rows.sort(key=lambda r: str(r.get("created_at", "")))
        return [_row_to_source(r) for r in rows]

    def census_source_rows(self) -> list[dict[str, object]]:
        """Per-source ledger census (SQL-lockdown GAP-1, D1-ruled Python fold).

        Delegates the full composition — bounded source/session/tool_call/batch
        reads + memory-bounded ``__event`` keyset paging + per-source fold — to
        :func:`build_census`, injecting the typed read seam (the SELECT-``*``
        ``query_state`` path cannot project, so the ~1M-row event scan pages so
        its inline-content corpus never materializes on the shared process). The
        clock is naive-UTC-normalized for the batch-age subtraction (F1 seam).
        """
        return build_census(
            query=self._query,
            query_ordered=self._query_ordered,
            now=cast(datetime, _naive_utc(self._clock())),
            page_size=_CENSUS_EVENT_PAGE_SIZE,
            fingerprint_seeds=(_FINGERPRINT_SEED_A, _FINGERPRINT_SEED_B),
        )

    # ------------------------------------------------------------------
    # Session listing + lookups
    # ------------------------------------------------------------------

    def list_sessions(
        self,
        *,
        limit: int = 50,
        since: datetime | None = None,
        until: datetime | None = None,
        first_event_since: datetime | None = None,
        first_event_until: datetime | None = None,
        project_path: str | None = None,
        vendor: SourceVendor | None = None,
        source_kind: IngestSourceKind | None = None,
        order_by: SessionsOrderBy = SessionsOrderBy.LAST_EVENT_AT_DESC,
        include_siblings: bool = False,
    ) -> list[dict[str, object]]:
        """M17 typed signature; SQL-lockdown junction read-then-route.

        Thin delegator to :func:`list_sessions_via_junction` (the junction
        source_kind route + the uncapped ``query_state`` session read + the
        Python two-window / sort / limit fold live there, keeping this mixin
        under the god-class budget). Window bounds are ``_naive_utc``-normalized
        here (F1 seam); ``limit`` clamps to ``[1, 200]`` (uncapped query_state —
        no cap-narrowing). See the helper for the source_kind-via-junction
        mechanism + the include_siblings+source_kind FULL-group expansion
        (canonical + siblings via the shared external_session_id).
        """
        return list_sessions_via_junction(
            self._query,
            window=SessionWindow(
                since=_naive_dt(since),
                until=_naive_dt(until),
                first_event_since=_naive_dt(first_event_since),
                first_event_until=_naive_dt(first_event_until),
            ),
            project_path=project_path,
            vendor=vendor,
            source_kind=source_kind,
            order_by=order_by,
            limit=max(1, min(limit, 200)),
            include_siblings=include_siblings,
        )

    def list_quiescent_sessions(
        self,
        *,
        quiescence_minutes: int,
        limit: int,
        trivial_sentinel: str,
    ) -> list[dict[str, object]]:
        """Canonical, past-cutoff sessions not yet embedded — the M6 summarize feed.

        SQL-lockdown read-then-route. ``query_state`` narrows to CANONICAL
        (``canonical_external_session_id IS NULL`` — M18 §3.4: only the canonical
        row is summarized; siblings COALESCE its summary at search time), live,
        past-cutoff candidates (``last_event_at <= cutoff``, Gap-A ``lte``; the
        cutoff is naive-UTC-normalized for the F1 seam). The anti-join +
        disjunction + post-filter LIMIT the flat grammar cannot express move to
        :func:`select_quiescent_sessions`: EXCLUDE sessions with a live
        ``__summary`` row (the idempotency seam — "done" == embedded, NOT
        ``summary_text``-set, per the 2026-06-01 Bug-1 ruling that un-broke
        custom_title-seeded claude_code sessions) + the trivial-sentinel
        marked-skip rows, project ``source_kind`` from ``__source``, order
        newest-quiescent-first (``last_event_at`` DESC, ``reverse=True`` in
        :func:`select_quiescent_sessions` — changed 2026-06-30), LIMIT (1..50).
        ``trivial_sentinel`` stays in the
        service layer (passed in). ``summary_text`` rides through so the caller's
        custom_title-seed branch short-circuits without a second read.
        """
        cutoff = self._clock() - timedelta(minutes=max(1, int(quiescence_minutes)))
        candidates = self._query(
            TABLE_SESSION,
            {
                "is_deleted": 0,
                "canonical_external_session_id": {"op": "is_null"},
                "last_event_at": {"op": "lte", "value": _naive_utc(cutoff)},
            },
        )
        if not candidates:
            return []
        summaries = self._query(
            TABLE_SUMMARY,
            {
                "session_id": [str(row["id"]) for row in candidates],
                "is_deleted": 0,
            },
        )
        # No is_deleted filter on __source — faithful to the original
        # INNER JOIN (which had none), so a soft-deleted source's sessions are
        # still summarized; a session whose source row is absent is dropped.
        sources = self._query(
            TABLE_SOURCE,
            {"id": sorted({str(row["source_id"]) for row in candidates})},
        )
        return select_quiescent_sessions(
            candidates,
            summarized_session_ids={str(row["session_id"]) for row in summaries},
            source_kind_by_id={
                str(row["id"]): row.get("source_kind") for row in sources
            },
            trivial_sentinel=trivial_sentinel,
            limit=max(1, min(int(limit), 50)),
        )

    def list_active_sessions(self) -> list[dict[str, object]]:
        """Live (non-expired) leases joined to their session, newest-expiry first.

        SQL-lockdown #11: the ``session INNER JOIN active_lease`` retires onto two
        single-namespace ``query_state`` reads + a Python inner-merge.

        * The lease read uses the Gap-A ``gt`` op for the ``expires_at > now``
          bound. The comparison value is normalized to naive UTC via
          ``_naive_utc`` so it matches the ``timestamp without time zone`` column
          exactly as the old ``execute_sql`` path did: that path ran
          ``_strip_tz_from_params`` on every bound param, whereas the autocommit
          ``query_state`` read path binds filter values raw — so the F1 strip
          moves to the callsite (``self._clock()`` is ``datetime.now(UTC)``,
          tz-aware).
        * Both reads are UNCAPPED ``query_state`` (not ``query_ordered``) because
          the old SQL had no ``LIMIT`` and the live-lease set can exceed the
          100-row Gap-C cap; ordering is re-established in Python (the Slice-1
          ``fetch_all_events_for_session`` precedent).
        * INNER-JOIN cardinality is preserved by emitting one row per live lease
          whose session is also live (lease ⋈ session on ``session_id``). The
          deterministic ``(expires_at, id)`` desc sort replaces the old
          ``ORDER BY expires_at DESC`` whose ties were arbitrary — a faithful
          superset. Datetime columns return as ISO strings on both the old and
          new paths (shared ``_serialize_for_json``); the sort parses
          ``expires_at`` so the order is chronological, not lexical-by-accident.
        """
        leases = self._query(
            TABLE_ACTIVE_LEASE,
            {
                "expires_at": {"op": "gt", "value": _naive_utc(self._clock())},
                "is_deleted": 0,
            },
        )
        if not leases:
            return []
        session_ids = list({str(lease["session_id"]) for lease in leases})
        sessions = self._query(
            TABLE_SESSION, {"id": session_ids, "is_deleted": 0},
        )
        return _merge_active_leases(leases, sessions)

    def list_sessions_by_ids(
        self,
        session_ids: list[str],
    ) -> list[dict[str, object]]:
        """Read session rows by id, for envelope joining in search_sessions."""
        if not session_ids:
            return []
        return self._query(
            TABLE_SESSION, {"id": list(session_ids), "is_deleted": 0},
        )

    def list_canonical_contributors(
        self,
        *,
        session_id: str,
    ) -> dict[str, object]:
        """Per-canonical-group provenance projection (W5.B §3.3).

        Given a session id (canonical or sibling), resolve the row's
        ``(vendor, external_session_id)`` pair via a CTE and return every
        live contributor in that group. The CTE+(vendor, external_session_id)
        form is correct for BOTH canonical-input AND sibling-input cases
        because — per ``canonical_pointer_repair.py:_lift_one_duplicate_group``
        — siblings carry the same ``external_session_id`` value as their
        canonical (the lift writes ``canonical_external_session_id = E``
        where E IS the canonical's ``external_session_id``).

        Return shape per Codex C3 (orphaned-canonical contract): three
        explicit top-level fields rather than overloading
        ``canonical_session_id`` with the external-id-pointer string. When
        the canonical row has been soft-deleted but siblings remain,
        ``canonical_session_id`` is ``None`` and ``orphaned_canonical``
        is ``True``.

        Raises ``LedgerRepositoryError`` when ``session_id`` resolves to
        no live row (input not found OR input + every contributor are
        ``is_deleted=1``).

        SQL-lockdown read-then-route (the LAST ledger raw-SQL read). Thin
        delegator to :func:`build_canonical_contributors_via_group`: the CTE
        becomes a ``query_state`` input-pair resolve, the main query becomes a
        ``(vendor, external_session_id)`` group read, the ``__source`` INNER JOIN
        becomes a ``= ANY`` source read (faithful to the predicate-less join —
        soft-deleted sources retained, absent-source rows dropped), and the
        projection + canonical-first / ``source_kind`` ASC sort happen in Python.
        """
        return build_canonical_contributors_via_group(
            self._query, session_id=session_id,
        )

    def find_session_id_by_external_session_id(
        self,
        external_session_id: str,
    ) -> str | None:
        """Return the canonical (M18-preferred) ``__session.id`` for an external id.

        Used by M20's ``lift_codex_stage1_summaries`` verb to resolve a
        Codex ``thread_id`` to the canonical __session row. With M18,
        multiple rows can share the external id across source kinds; the
        canonical row (canonical_external_session_id IS NULL) is the one
        M6 auto-summarizes and the one whose ``summary_text`` field the
        rewrite verb should target.

        Ordering: canonical-first, falling through to the oldest non-canonical
        row when no canonical exists (legacy pre-M18 data; orphan-keyed row).
        SQL-lockdown Slice 6 reads the rows sharing one ``external_session_id``
        (a bounded set — a few rows across source kinds) via the equality
        primitive and applies the canonical-first / ``created_at`` ASC top-1
        pick in :func:`_pick_canonical_session_id` (the composite ``ORDER BY``
        led with a computed boolean the flat grammar cannot express).
        """
        rows = self._query(
            TABLE_SESSION,
            {"external_session_id": external_session_id, "is_deleted": 0},
        )
        return _pick_canonical_session_id(rows)

    # ------------------------------------------------------------------
    # Event listing + lookups
    # ------------------------------------------------------------------

    def fetch_all_events_for_session(
        self,
        *,
        session_id: str,
    ) -> list[dict[str, object]]:
        """Return every ``__event`` row for ``session_id`` ordered by ``sequence``.

        Phase 2 Tier 1 codex re-ingest sub-strategy B: MESSAGE/SYSTEM rows
        carry no ``vendor_event_id`` in the codex parser's discipline, so
        the helper has to pair stripped rows with re-parsed events by
        positional ordering rather than key lookup. The pairing logic
        needs both stripped AND clean rows for the session — clean rows
        anchor the index alignment when re-parse counts match, and serve
        as type/timestamp references when the ±5s + event_type fallback
        heuristic runs against drift.

        Columns selected match the ``_restore_codex_local`` consumer:

        * ``id`` — platform event_id to pass to ``restore_event_content``
        * ``sequence`` — canonical per-session ordering key
        * ``event_at`` — for the ±5s positional-pairing heuristic
        * ``event_type`` — for the event_type-match positional-pairing
          predicate
        * ``content_text`` — NULL means stripped, NOT NULL means clean
        * ``vendor_event_id`` — informational; the caller already filters
          candidates without vendor_event_id before calling this
        """
        rows = self._query(
            TABLE_EVENT, {"session_id": session_id, "is_deleted": 0},
        )
        # query_state gives no ordering; the codex re-ingest positional-pairing
        # consumer needs per-session sequence order (sequence is a NOT NULL int).
        rows.sort(key=lambda r: cast(int, r.get("sequence", 0)))
        return rows

    def find_event_id_by_vendor_id(
        self,
        *,
        session_id: str,
        vendor_event_id: str,
    ) -> str | None:
        """Return the platform event_id whose ``(session_id, vendor_event_id)``
        matches, or ``None``.

        Used by the importer as an idempotency check: a session whose
        event_read cursor failed to persist (e.g. because the blob adapter
        raised mid-batch) would re-yield the same vendor events on the
        next poll; this lookup lets the importer short-circuit the insert
        before duplicating any rows. v1 has no DB-level UNIQUE constraint
        on (session_id, vendor_event_id) so the guard lives in code.
        """
        rows = self._query_ordered(
            TABLE_EVENT,
            filters={"session_id": session_id, "vendor_event_id": vendor_event_id},
            order_by=[["sequence", "desc"], ["id", "desc"]],
            limit=1,
        )
        return str(rows[0]["id"]) if rows else None

    def find_call_event_id_for_resolution(
        self,
        *,
        session_id: str,
        tool_use_vendor_id: str,
    ) -> str | None:
        """Return the platform event_id of the TOOL_CALL whose vendor_event_id
        equals ``tool_use_vendor_id`` within ``session_id``, or ``None``.

        Per spec §17.3 M3 acceptance the importer resolves a TOOL_RESULT to
        its matching TOOL_CALL via the vendor-supplied ``tool_use_id`` —
        the call carries it as ``vendor_event_id`` and the result carries
        it as ``vendor_parent_event_id``. This lookup hands the importer
        the platform-side ``event_id`` that :meth:`resolve_tool_call`
        requires.
        """
        rows = self._query_ordered(
            TABLE_EVENT,
            filters={
                "session_id": session_id,
                "vendor_event_id": tool_use_vendor_id,
                "event_type": EventType.TOOL_CALL.value,
            },
            order_by=[["sequence", "desc"], ["id", "desc"]],
            limit=1,
        )
        return str(rows[0]["id"]) if rows else None

    def list_tool_calls(
        self,
        *,
        session_id: str | None = None,
        tool_name: str | None = None,
        status: str | None = None,
        since: datetime | None = None,
        limit: int = 50,
    ) -> list[dict[str, object]]:
        """Tool-call rows newest-first, optionally filtered + ``called_at``-bounded.

        SQL-lockdown Slice 6b: migrated onto ``query_ordered``. Equality filters
        (``session_id``/``tool_name``/``status``) AND-combine with the ``since``
        lower bound (``called_at >= since``, inclusive -- Gap-A ``gte``);
        ``is_deleted = 0`` is the primitive default (== the old explicit filter).
        ``called_at`` is non-unique so ``id`` is the total-order tie-break
        (deterministic where the old SQL left ties arbitrary). Capped 1..100 (the
        old 1..200 had no consumer; a future >100 need adds a cursor, not a cap
        bump). Rows also carry the table's bookkeeping columns now (``SELECT *``;
        column projection deferred per D9) -- benign, unread widening.

        TZ seam (SQL-lockdown follow-up): a caller-supplied tz-aware ``since`` is
        normalized to naive UTC via ``_naive_utc`` before binding. The old
        ``execute_sql`` path ran ``_strip_tz_from_params`` on every param, but
        ``query_ordered`` only naive-izes the ``after`` *cursor*
        (``parse_ordered_query``) -- NOT filter-dict values, which
        ``select_ordered`` binds raw. Without this strip a tz-aware ``since``
        would compare tz-aware against the naive-UTC ``called_at`` column (the
        silent-0-rows / server-tz-dependent trap the F1 seam closes); the strip
        keeps the comparison naive-vs-naive, matching the pre-migration path.
        """
        filters: dict[str, object] = {}
        if session_id is not None:
            filters["session_id"] = session_id
        if tool_name is not None:
            filters["tool_name"] = tool_name
        if status is not None:
            filters["status"] = status
        if since is not None:
            filters["called_at"] = {"op": "gte", "value": _naive_utc(since)}
        return self._query_ordered(
            TABLE_TOOL_CALL,
            filters=filters,
            order_by=[["called_at", "desc"], ["id", "desc"]],
            limit=max(1, min(limit, 100)),
        )

    def get_session_timeline(
        self,
        *,
        session_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> list[dict[str, object]]:
        """Per-session events in ``sequence`` order, after a cursor, bounded.

        SQL-lockdown Slice 6b: migrated onto ``query_ordered``. The
        ``sequence > after_sequence`` cursor is the Gap-A ``gt`` op (strict,
        faithful to the old ``sequence > %s``); ``is_deleted = 0`` is the
        primitive default (== the old explicit filter); ``sequence`` is the
        per-session ordinal, so the ``id`` tie-break does not change the order in
        practice (and is required anyway for the composite-``order_by``
        total-order contract). Capped 1..100 (the old
        1..500 had no consumer -- the summarize cron reads 50, the public verb
        defaults to 100); longer timelines page by advancing ``after_sequence``.
        Rows also carry the table's bookkeeping/actor columns now (``SELECT *``;
        projection deferred per D9); ``content_json`` keeps its type (all read
        paths share one ``dict_row`` + ``_serialize_for_json`` pipeline).
        """
        return self._query_ordered(
            TABLE_EVENT,
            filters={
                "session_id": session_id,
                "sequence": {"op": "gt", "value": after_sequence},
            },
            order_by=[["sequence", "asc"], ["id", "asc"]],
            limit=max(1, min(limit, 100)),
        )

    def find_latest_away_summary_for_session(
        self,
        session_id: str,
    ) -> str | None:
        """Return the most-recent claude_code away_summary recap text for the session.

        D8 hybrid path (operator ruling 2026-06-01): claude_code emits
        ``type=system, subtype='away_summary'`` lines as it recaps a quiescence
        boundary. The vendor + source plugins lift that subtype into
        ``content_json->>'subtype'`` so the M6 auto-summarizer can reuse the
        recap text — zero inference, ~74% of claude_code sessions.

        Returns the trimmed recap text, or ``None`` when no away_summary event
        exists for this conversation. The lookup spans the canonical session AND
        its cross-source siblings (:meth:`_resolve_conversation_group`) — the
        recap is ingested on a sibling source row while M6 summarizes the
        canonical, so a canonical-only lookup misses it (the 2026-06-30 zero-reuse
        bug). SQL-lockdown Slice 6 narrows to SYSTEM events carrying a JSON
        payload via the ``= ANY`` / ``is_not_null`` query, then applies the
        ``away_summary`` subtype match + ``event_at`` DESC recency pick in
        :func:`_select_latest_away_summary` (the SQL's
        ``content_json->>'subtype'`` JSONB-path filter the flat grammar cannot
        express). The per-conversation SYSTEM-event set is small, so pulling it
        to Python is cheap.
        """
        rows = self._query(
            TABLE_EVENT,
            {
                "session_id": self._resolve_conversation_group(session_id),
                "event_type": EventType.SYSTEM.value,
                "is_deleted": 0,
                "content_json": {"op": "is_not_null"},
            },
        )
        return _select_latest_away_summary(rows)

    def _resolve_conversation_group(self, session_id: str) -> list[str]:
        """Canonical ``session_id`` + its sibling session_ids (cross-source dedup).

        claude_code ``away_summary`` recaps land on the SIBLING source rows (e.g.
        the claude_code_history sibling) while M6 summarizes the CANONICAL row.
        A recap lookup keyed on the canonical session_id alone is therefore blind
        to the recap — empirically 0/207 reuse on 2026-06-30, falling through to
        inference for every session Claude had already recapped. Widening to the
        whole logical conversation (the canonical plus every sibling pointing at
        its ``external_session_id``) lets M6 reuse the free recap.
        """
        rows = self._query(TABLE_SESSION, {"id": session_id, "is_deleted": 0})
        external_id = rows[0].get("external_session_id") if rows else None
        if not external_id:
            return [session_id]
        siblings = self._query(
            TABLE_SESSION,
            {"canonical_external_session_id": external_id, "is_deleted": 0},
        )
        return [session_id, *(str(row["id"]) for row in siblings)]


__all__ = ["RELOAD_SAFE", "SessionLedgerReadMixin"]
