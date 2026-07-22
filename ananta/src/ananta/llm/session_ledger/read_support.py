"""Pure post-read helpers for the session-ledger read mixin.

The SQL-lockdown migration retires the read-side raw ``execute_sql`` calls onto
the typed ``query_state`` / ``query_ordered`` primitives + Python post-processing
(merges, folds, projections the flat filter grammar cannot express). That
post-processing lives here as module-level pure functions so
:class:`ananta.llm.session_ledger.read.SessionLedgerReadMixin` stays a focused,
sub-500-LOC query surface as each read migrates (the god-class coherence-over-
size budget): a migrated read is a thin mixin method that issues its
``query_state`` reads and delegates the Python shaping to a helper here.

Most helpers are pure functions over already-fetched dict rows — no DB handle, no
service reference — so each is unit-testable in isolation and reused unchanged
when a read's *fetch* mechanism later changes (e.g. a raw read becoming
read-then-route). A few (``fold_census_events``, ``build_census``) are read
*orchestration* that the heaviest migrations push down here for the god-class
budget: they DRIVE reads but still hold no DB handle or service reference — the
typed read seam (the mixin's bound ``_query`` / ``_query_ordered``) is passed in
as an injected callback, so they stay pure-by-injection and unit-testable with a
fake reader.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from ananta.llm.session_ledger.base import LedgerRepositoryError
from ananta.llm.session_ledger.schema import (
    TABLE_EVENT,
    TABLE_IMPORT_BATCH,
    TABLE_SESSION,
    TABLE_SESSION_SOURCE_KIND,
    TABLE_SOURCE,
    TABLE_TOOL_CALL,
)
from ananta.llm.session_ledger.types import (
    ImportBatchStatus,
    IngestSourceKind,
    SessionsOrderBy,
    SourceVendor,
)

RELOAD_SAFE = True

# Operator ruling 2026-06-30: the M6 auto-summarizer summarizes real conversations
# only. ``agent_messaging`` sessions are 1-event peer-coordination chatter (not
# user/assistant conversation), so they are excluded from the quiescent selection
# entirely — otherwise newest-first wastes every pass marking the continuous
# stream of recent coordination noise trivial instead of summarizing conversations.
_NON_CONVERSATION_SOURCE_KINDS = frozenset({IngestSourceKind.AGENT_MESSAGING.value})


def _content_json_subtype(value: object) -> str | None:
    """Best-effort ``content_json->>'subtype'`` over a dict-or-JSON-str payload.

    Mirrors the Postgres ``->>'subtype'`` semantics the migrated
    :meth:`SessionLedgerReadMixin.find_latest_away_summary_for_session`
    replaced: a payload that is not a JSON object (array / scalar /
    unparseable) yields ``None`` rather than raising, so an unrelated SYSTEM
    event cannot crash the lookup. JSONB reads back as a ``dict`` on the
    ``query_state`` path, but some read paths hand back the raw JSON ``str`` —
    handle both (the JSONB-deserialization seam).
    """
    if isinstance(value, str):
        if not value:
            return None
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            return None
    if isinstance(value, dict):
        subtype = value.get("subtype")
        return subtype if isinstance(subtype, str) else None
    return None


def _pick_canonical_session_id(rows: list[dict[str, object]]) -> str | None:
    """Canonical-first / ``created_at`` ASC top-1 pick over rows sharing one external id.

    SQL-lockdown Slice 6: the migrated
    :meth:`SessionLedgerReadMixin.find_session_id_by_external_session_id`
    replaced an ``ORDER BY (canonical_external_session_id IS NOT NULL),
    created_at ASC LIMIT 1`` whose leading key is a computed boolean the flat
    ``query_state`` grammar cannot express. The sort key mirrors the SQL
    exactly: ``canonical_external_session_id is not None`` (``False`` < ``True``
    → canonical first, matching Postgres BOOLEAN FALSE < TRUE) then
    ``created_at`` ASC — a NOT NULL timestamp whose on-read form sorts
    chronologically as a string (the ``list_sources`` precedent). Returns the
    winning row's ``id``, or ``None`` for an empty input.
    """
    if not rows:
        return None
    rows.sort(
        key=lambda r: (
            r.get("canonical_external_session_id") is not None,
            str(r.get("created_at", "")),
        )
    )
    return str(rows[0]["id"])


def _select_latest_away_summary(rows: list[dict[str, object]]) -> str | None:
    """Most-recent live away_summary recap text from a session's SYSTEM events.

    SQL-lockdown Slice 6: the migrated
    :meth:`SessionLedgerReadMixin.find_latest_away_summary_for_session`
    replaced a ``content_json->>'subtype' = 'away_summary'`` JSONB-path filter
    (which the flat grammar cannot express) + ``ORDER BY event_at DESC,
    sequence DESC LIMIT 1``. ``rows`` is the SYSTEM-event set the equality /
    ``is_not_null`` query already narrowed; the subtype match + recency pick
    happen here. ``event_at`` is a NOT NULL timestamp (string-sortable
    chronologically); ``sequence`` is the NOT NULL int tie-break. Returns the
    trimmed recap text, or ``None`` when no away_summary event is present.
    """
    candidates = [
        row
        for row in rows
        if _content_json_subtype(row.get("content_json")) == "away_summary"
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda r: (str(r.get("event_at", "")), cast(int, r.get("sequence", 0))),
        reverse=True,
    )
    raw = candidates[0].get("content_text")
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    return text or None


def _merge_active_leases(
    leases: list[dict[str, object]],
    sessions: list[dict[str, object]],
) -> list[dict[str, object]]:
    """Inner-merge live leases with their live sessions, newest-expiry first.

    SQL-lockdown #11: the replacement shaping for the raw
    ``session INNER JOIN active_lease`` that :meth:`list_active_sessions`
    retired. ``leases`` / ``sessions`` are the two ``query_state`` reads the
    method issued (active leases by ``expires_at > now``; their sessions by
    ``id`` =ANY). Emits one row per lease whose session is live — the INNER-JOIN
    cardinality — projecting the session-sourced fields + the lease's
    ``last_seen_at`` / ``expires_at``, then sorts ``expires_at`` DESC with an
    ``id`` total-order tie-break (the old ``ORDER BY expires_at DESC`` left ties
    arbitrary — a faithful superset). Datetime columns return as ISO strings on
    both the old and new read paths (shared ``_serialize_for_json``); the sort
    parses ``expires_at`` so the order is chronological, not lexical-by-accident.
    """
    session_by_id = {str(row["id"]): row for row in sessions}
    merged: list[dict[str, object]] = []
    for lease in leases:
        session = session_by_id.get(str(lease["session_id"]))
        if session is None:
            continue
        merged.append(
            {
                "id": session["id"],
                "source_id": session["source_id"],
                "external_session_id": session["external_session_id"],
                "vendor": session["vendor"],
                "vendor_session_label": session["vendor_session_label"],
                "project_path": session["project_path"],
                "last_event_at": session["last_event_at"],
                "last_seen_at": lease["last_seen_at"],
                "expires_at": lease["expires_at"],
            }
        )
    merged.sort(
        key=lambda row: (
            datetime.fromisoformat(str(row["expires_at"])),
            str(row["id"]),
        ),
        reverse=True,
    )
    return merged


def _build_canonical_contributors_result(
    rows: list[dict[str, object]],
    *,
    session_id: str,
) -> dict[str, object]:
    """Project a ``(vendor, external_session_id)`` group into the W5.B C3 shape.

    ``rows`` is the group's live members (canonical-first, ``source_kind`` ASC)
    the ``list_canonical_contributors`` read already fetched. Builds the three
    explicit top-level fields (``canonical_session_id`` / ``…external_session_id``
    / ``vendor``) + the ``orphaned_canonical`` flag (``True`` when the canonical
    row is soft-deleted but siblings remain) + the per-contributor projection.
    Raises :class:`LedgerRepositoryError` on an empty group (input not found, or
    input + every contributor ``is_deleted=1``) — the fail-fast contract the
    verb has always carried.
    """
    if not rows:
        raise LedgerRepositoryError(
            f"list_canonical_contributors: session_id={session_id!r} "
            "not found or has no live members in its "
            "(vendor, external_session_id) group"
        )
    vendor_value = str(rows[0].get("vendor", ""))
    external_session_id_value = str(rows[0].get("external_session_id", ""))
    canonical_row = next((r for r in rows if r.get("is_canonical")), None)
    canonical_session_id: str | None
    orphaned_canonical: bool
    if canonical_row is not None:
        canonical_session_id = str(canonical_row.get("session_id", ""))
        orphaned_canonical = False
    else:
        canonical_session_id = None
        orphaned_canonical = True
    contributors: list[dict[str, object]] = []
    for r in rows:
        count_raw = r.get("contributed_event_count")
        count = int(count_raw) if isinstance(count_raw, int) else 0
        contributors.append(
            {
                "session_id": str(r.get("session_id", "")),
                "source_id": str(r.get("source_id", "")),
                "source_kind": str(r.get("source_kind", "")),
                "first_event_at": r.get("first_event_at"),
                "last_event_at": r.get("last_event_at"),
                "contributed_event_count": count,
                "is_canonical": bool(r.get("is_canonical")),
            }
        )
    return {
        "canonical_session_id": canonical_session_id,
        "canonical_external_session_id": external_session_id_value,
        "vendor": vendor_value,
        "orphaned_canonical": orphaned_canonical,
        "contributors": contributors,
    }


def build_canonical_contributors_via_group(
    query: _StateReader,
    *,
    session_id: str,
) -> dict[str, object]:
    """list_canonical_contributors read-then-route (SQL-lockdown — the LAST ledger _fetch_all).

    Replaces the CTE + INNER-JOIN-``__source`` raw SQL with three ``query_state``
    reads + a Python project/sort, feeding the unchanged
    :func:`_build_canonical_contributors_result`:

    1. Resolve the input's group key — ``query(session, {id, is_deleted: 0})``.
       Empty (input not found / soft-deleted) → ``[]`` → the builder's fail-fast
       raise (the verb's always-carried contract).
    2. Read the live group — ``query(session, {vendor: V, external_session_id: E,
       is_deleted: 0})``. Siblings carry the same ``(vendor, external_session_id)``
       as their canonical (the ``canonical_pointer_repair`` lift), so this is the
       full group for BOTH canonical-input and sibling-input.
    3. Resolve per-contributor ``source_kind`` — :func:`_source_kind_by_id`
       (``query(source, {id: =ANY(source_ids)})``, NO ``is_deleted`` filter,
       faithful to the predicate-less INNER JOIN).
    4. :func:`_project_contributors` — the 9-key shape + the canonical-first /
       ``source_kind`` ASC sort, byte-identical to the pre-migration ``ORDER BY
       (canonical_external_session_id IS NOT NULL), source_kind ASC``
       (``query_state`` has no ORDER BY).
    5. :func:`_build_canonical_contributors_result` unchanged.

    Datetime fidelity (the Architect's catch): the projected ``first_event_at`` /
    ``last_event_at`` are parsed back to NAIVE datetime in step 4 — ``query_state``
    serializes them to ISO strings (``_serialize_for_json``) but the unchanged
    builder + its downstream consumer expect the ``datetime`` the raw ``_fetch_all``
    returned.
    """
    input_rows = query(TABLE_SESSION, {"id": session_id, "is_deleted": 0})
    if not input_rows:
        return _build_canonical_contributors_result([], session_id=session_id)
    head = input_rows[0]
    group = query(
        TABLE_SESSION,
        {
            "vendor": head["vendor"],
            "external_session_id": head["external_session_id"],
            "is_deleted": 0,
        },
    )
    source_kind_by_id = _source_kind_by_id(query, group)
    projected = _project_contributors(group, source_kind_by_id)
    return _build_canonical_contributors_result(projected, session_id=session_id)


def _source_kind_by_id(
    query: _StateReader, group: list[dict[str, object]],
) -> dict[str, object]:
    """``{source_id: source_kind}`` for the group's sources (faithful to the INNER JOIN).

    Reads ``__source`` by id WITHOUT an ``is_deleted`` filter — the pre-migration
    INNER JOIN carried no ``src.is_deleted`` predicate, so a soft-deleted source
    still contributes its kind (``query_state`` does not auto-exclude is_deleted).
    The caller drops a group row whose ``source_id`` has no row here (the
    INNER-JOIN drop). Empty group → no read.
    """
    source_ids = sorted({str(row["source_id"]) for row in group})
    if not source_ids:
        return {}
    return {
        str(row["id"]): row["source_kind"]
        for row in query(TABLE_SOURCE, {"id": source_ids})
    }


def _project_contributors(
    group: list[dict[str, object]],
    source_kind_by_id: dict[str, object],
) -> list[dict[str, object]]:
    """Project + sort the group into the 9-key contributor rows for the builder.

    Drops a row whose ``source_id`` is absent from ``source_kind_by_id`` (the
    INNER-JOIN drop). Sort: canonical-first (``canonical_external_session_id IS
    NULL``) then ``source_kind`` ASC — byte-identical to the pre-migration
    ``ORDER BY (canonical_external_session_id IS NOT NULL), source_kind ASC``.
    ``first_event_at`` / ``last_event_at`` are parsed back to NAIVE datetime via
    :func:`_parse_session_dt` (the type the raw ``_fetch_all`` returned;
    ``query_state`` serialized them to ISO strings) — both are ``not_null``
    session columns, so a parse failure is a fail-loud contract violation.
    """
    projected: list[dict[str, object]] = []
    for row in group:
        source_id = str(row["source_id"])
        if source_id not in source_kind_by_id:
            continue
        projected.append(
            {
                "session_id": str(row["id"]),
                "source_id": source_id,
                "source_kind": source_kind_by_id[source_id],
                "vendor": row["vendor"],
                "external_session_id": row["external_session_id"],
                "first_event_at": _parse_session_dt(row["first_event_at"]),
                "last_event_at": _parse_session_dt(row["last_event_at"]),
                "contributed_event_count": row["event_count"],
                "is_canonical": row.get("canonical_external_session_id") is None,
            }
        )
    projected.sort(
        key=lambda r: (not bool(r["is_canonical"]), str(r["source_kind"])),
    )
    return projected


def select_quiescent_sessions(
    candidates: list[dict[str, object]],
    *,
    summarized_session_ids: set[str],
    source_kind_by_id: dict[str, object],
    trivial_sentinel: str,
    limit: int,
) -> list[dict[str, object]]:
    """Read-then-route shaping for ``list_quiescent_sessions`` (M6 auto-summarize).

    ``candidates`` are the canonical, live, past-cutoff (``last_event_at <=
    cutoff``) sessions the ``query_state`` read already narrowed. The original's
    ``NOT EXISTS __summary`` anti-join, its ``(summary_text IS NULL OR !=
    sentinel)`` disjunction, and its ``last_event_at`` ASC + LIMIT applied AFTER
    those filters are all shapes the flat grammar cannot express — so they happen
    here:

    * EXCLUDE a session with a live ``__summary`` row (``summarized_session_ids``
      — the real idempotency seam: "done" == embedded, NOT
      ``summary_text``-populated, per the 2026-06-01 Bug-1 ruling that un-broke
      custom_title-seeded claude_code sessions);
    * EXCLUDE a session whose ``summary_text`` is the trivial sentinel (the
      marked-and-skipped rows the cron must not re-pick);
    * EXCLUDE a session whose source row is absent — the original
      ``INNER JOIN __source`` drop; a present source contributes ``source_kind``
      (faithfully WITHOUT an ``is_deleted`` filter, matching the original join,
      so a soft-deleted source's sessions are still summarized);
    * EXCLUDE non-conversation source kinds (``agent_messaging``) — operator
      2026-06-30: summarize real user/assistant conversations, not the continuous
      stream of peer-coordination chatter that otherwise dominates newest-first;
    * sort ``last_event_at`` DESC (newest-quiescent-first → recent sessions
      become searchable within ~a day rather than waiting behind the full
      historical import backlog) and return at most ``limit`` rows.

    Each survivor is the SELECT-``*`` session row + the projected ``source_kind``
    (the D9 widening; ``summary_text`` is already a session column the caller's
    custom_title-seed branch reads). ``last_event_at`` is a NOT NULL timestamp
    parsed (not lexical-sorted) for the chronological order.
    """
    survivors: list[dict[str, object]] = []
    for row in candidates:
        if str(row["id"]) in summarized_session_ids:
            continue
        if row.get("summary_text") == trivial_sentinel:
            continue
        source_id = str(row["source_id"])
        if source_id not in source_kind_by_id:
            continue
        source_kind = source_kind_by_id[source_id]
        if source_kind in _NON_CONVERSATION_SOURCE_KINDS:
            continue  # agent_messaging = peer-coordination noise, not a conversation
        survivors.append({**row, "source_kind": source_kind})
    # Newest-quiescent-FIRST (``reverse=True``, changed 2026-06-30 from ASC):
    # recent sessions become summarized — and therefore searchable via
    # ``search_sessions`` — within ~a day, instead of waiting behind the full
    # historical import backlog (~15k rows / many days at the cron cadence).
    # The operator's goal is finding RECENT conversations; the historical
    # backlog still drains underneath as newer rows are exhausted.
    survivors.sort(
        key=lambda row: datetime.fromisoformat(str(row["last_event_at"])),
        reverse=True,
    )
    return survivors[:limit]


# Census fingerprint: a deterministic, order-independent 64-bit XOR fold over
# per-event ``(id, content_blob_id)`` identity tuples under two well-separated
# seeds. Replaces the retired Postgres ``bit_xor(hashtextextended(...))``
# (SQL-lockdown GAP-1; operator D1 ruling 2026-06-20 — Python-fold + re-baseline,
# NO aggregate primitive). ``hashtextextended`` is a PG-internal hash with no
# faithful Python reproduction; the census is regenerable (no cross-era
# comparison against a stored old-method value), so the fingerprint is recomputed
# in Python on both diff sides. ``blake2b`` — NOT the builtin ``hash()``, which
# is process-salted (``PYTHONHASHSEED``) and therefore NOT reproducible across
# runs, defeating the determinism the fingerprint's whole purpose depends on. The
# 8-byte digest matches the retired bigint fingerprint width.
_FINGERPRINT_DIGEST_BYTES = 8
_FINGERPRINT_PERSON_BYTES = 16  # == hashlib.blake2b.PERSON_SIZE


def _fingerprint_component(identity: str, seed: int) -> int:
    """Deterministic 64-bit hash of a row-identity string under ``seed``."""
    digest = hashlib.blake2b(
        identity.encode("utf-8"),
        digest_size=_FINGERPRINT_DIGEST_BYTES,
        person=seed.to_bytes(_FINGERPRINT_PERSON_BYTES, "little"),
    ).digest()
    return int.from_bytes(digest, "little")


@dataclass
class _SessionTally:
    """Per-source session counts — total + canonical/sibling split."""

    session_count: int = 0
    canonical_count: int = 0
    sibling_count: int = 0


@dataclass
class _BatchTally:
    """Per-source import-batch health — running-batch split + oldest start."""

    owned_running: int = 0
    unclaimed_route: int = 0
    min_started: datetime | None = None


def _batch_age_seconds(batch: _BatchTally | None, now: datetime) -> int | None:
    """Whole-second age of the oldest running batch, or ``None`` when none run.

    Mirrors the retired ``extract(epoch FROM now() - min(started_at))::bigint``:
    ``now`` is the repository clock normalized to naive UTC by the caller (the
    F1 seam — matching the naive-UTC ``started_at`` parsed from its ISO string),
    and ``int(...)`` truncates toward zero exactly as ``::bigint`` does for the
    non-negative durations census produces.
    """
    if batch is None or batch.min_started is None:
        return None
    return int((now - batch.min_started).total_seconds())


class _CensusAggregator:
    """Pure per-source census fold — the D1 Python replacement for ``_CENSUS_SQL``.

    Holds the per-source session / tool_call / import-batch tallies (folded once
    from the bounded full reads) plus the order-independent event-row
    fingerprint, which is fed PAGE-BY-PAGE via :meth:`fold_event_page` so the
    ~1M-row ``__event`` scan never materializes the whole (inline-content-bearing)
    corpus on the shared state-service process — the memory-bounded keyset paging
    the SELECT-``*`` ``query_state`` path cannot express. The XOR fold is
    order-independent, so the keyset page order is irrelevant to the result.

    Faithful to the retired multi-CTE join semantics: events and tool_calls are
    attributed to their *session's* ``source_id`` and DROPPED when that session
    is not live (the old ``JOIN __session s ON … AND s.is_deleted = 0`` of the
    ``ev`` / ``tc`` CTEs); the output rows come ``FROM`` the live sources (the
    base of the old LEFT JOIN), so tallies under a missing/deleted source are
    silently absent; a source with no live events carries ``None`` fingerprints
    (the old LEFT JOIN NULL), distinct from a present-but-zero XOR.
    """

    def __init__(
        self,
        *,
        sources: list[dict[str, object]],
        sessions: list[dict[str, object]],
        tool_calls: list[dict[str, object]],
        import_batches: list[dict[str, object]],
        fingerprint_seeds: tuple[int, int],
    ) -> None:
        self._sources = sources
        self._seed_a, self._seed_b = fingerprint_seeds
        # Live-session id -> source_id. An event / tool_call whose session is
        # absent here (soft-deleted or never present) is dropped from the
        # per-source tally — the INNER-JOIN ``s.is_deleted = 0`` semantics.
        self._session_source: dict[str, str] = {
            str(row["id"]): str(row["source_id"]) for row in sessions
        }
        self._sessions = self._tally_sessions(sessions)
        self._tool_calls = self._tally_tool_calls(tool_calls)
        self._batches = self._tally_batches(import_batches)
        self._event_count: dict[str, int] = {}
        self._fingerprint_a: dict[str, int] = {}
        self._fingerprint_b: dict[str, int] = {}

    @staticmethod
    def _tally_sessions(
        sessions: list[dict[str, object]],
    ) -> dict[str, _SessionTally]:
        tally: dict[str, _SessionTally] = {}
        for row in sessions:
            entry = tally.setdefault(str(row["source_id"]), _SessionTally())
            entry.session_count += 1
            if row.get("canonical_external_session_id") is None:
                entry.canonical_count += 1
            else:
                entry.sibling_count += 1
        return tally

    def _tally_tool_calls(
        self, tool_calls: list[dict[str, object]],
    ) -> dict[str, int]:
        counts: dict[str, int] = {}
        for row in tool_calls:
            source_id = self._session_source.get(str(row["session_id"]))
            if source_id is None:
                continue
            counts[source_id] = counts.get(source_id, 0) + 1
        return counts

    @staticmethod
    def _tally_batches(
        import_batches: list[dict[str, object]],
    ) -> dict[str, _BatchTally]:
        tally: dict[str, _BatchTally] = {}
        for row in import_batches:
            entry = tally.setdefault(str(row["source_id"]), _BatchTally())
            if row.get("status") != ImportBatchStatus.RUNNING.value:
                continue
            if row.get("polling_lease_token") is not None:
                entry.owned_running += 1
            else:
                entry.unclaimed_route += 1
            started = row.get("started_at")
            if isinstance(started, str) and started:
                started_at = datetime.fromisoformat(started)
                if entry.min_started is None or started_at < entry.min_started:
                    entry.min_started = started_at
        return tally

    def fold_event_page(self, page: list[dict[str, object]]) -> None:
        """XOR-fold one page of live ``__event`` rows into the per-source fingerprint.

        Order-independent (the XOR), so the keyset page order does not matter.
        Events whose session is not live are dropped (the retired ``ev`` CTE's
        ``JOIN __session … AND s.is_deleted = 0``).
        """
        for row in page:
            source_id = self._session_source.get(str(row["session_id"]))
            if source_id is None:
                continue
            blob = row.get("content_blob_id")
            identity = f"{row['id']}\x1f{blob if blob is not None else ''}"
            self._event_count[source_id] = self._event_count.get(source_id, 0) + 1
            self._fingerprint_a[source_id] = self._fingerprint_a.get(
                source_id, 0,
            ) ^ _fingerprint_component(identity, self._seed_a)
            self._fingerprint_b[source_id] = self._fingerprint_b.get(
                source_id, 0,
            ) ^ _fingerprint_component(identity, self._seed_b)

    def result(self, *, now: datetime) -> list[dict[str, object]]:
        """Assemble per-source census rows, ordered ``(source_kind, source_id)``.

        Preserves the retired ``ORDER BY src.source_kind, src.id`` and the exact
        output keys the ``service.census`` consumer reads. ``fingerprint_a`` /
        ``fingerprint_b`` are ``None`` for a source with no live events (the old
        LEFT JOIN NULL); a present XOR value of ``0`` is a real fingerprint.
        """
        rows: list[dict[str, object]] = []
        for source in self._sources:
            source_id = str(source["id"])
            sessions = self._sessions.get(source_id, _SessionTally())
            batch = self._batches.get(source_id)
            rows.append(
                {
                    "source_id": source_id,
                    "source_kind": source.get("source_kind"),
                    "root_uri": source.get("root_uri"),
                    "session_count": sessions.session_count,
                    "canonical_count": sessions.canonical_count,
                    "sibling_count": sessions.sibling_count,
                    "event_count": self._event_count.get(source_id, 0),
                    "fingerprint_a": self._fingerprint_a.get(source_id),
                    "fingerprint_b": self._fingerprint_b.get(source_id),
                    "tool_call_count": self._tool_calls.get(source_id, 0),
                    "owned_running_batches": batch.owned_running if batch else 0,
                    "unclaimed_route_batches": (
                        batch.unclaimed_route if batch else 0
                    ),
                    "oldest_running_batch_age_seconds": _batch_age_seconds(
                        batch, now,
                    ),
                }
            )
        rows.sort(key=lambda row: (str(row["source_kind"]), str(row["source_id"])))
        return rows


class _StateReader(Protocol):
    """The mixin's bound ``_query`` read seam (``query_state``), as a callback."""

    def __call__(
        self, table: str, filters: dict[str, object],
    ) -> list[dict[str, object]]: ...


class _OrderedReader(Protocol):
    """The mixin's bound ``_query_ordered`` read seam, as a callback contract."""

    def __call__(
        self,
        table: str,
        *,
        filters: dict[str, object],
        order_by: list[list[str]],
        limit: int,
    ) -> list[dict[str, object]]: ...


def fold_census_events(
    aggregator: _CensusAggregator,
    *,
    query_ordered: _OrderedReader,
    table: str,
    page_size: int,
) -> None:
    """Page the live ``__event`` scan into ``aggregator`` via an ``id``-keyset cursor.

    Drives ``query_ordered`` with a Gap-A ``id > last_id`` lower bound over the
    unique PK, ordered ``(id, created_at)`` ASC — the ≥ 2-column composite
    ``query_ordered`` requires, with ``id`` already a total order so
    ``created_at`` is an inert tie-break (the ``get_session_timeline``
    filter-predicate paging precedent). ``is_deleted = 0`` is the primitive
    default. Each page is folded then DISCARDED, so peak memory is one page —
    never the ~1M-row corpus the SELECT-``*`` ``query_state`` path would
    materialize (operator D1 2026-06-20). The XOR fold is order-independent, so
    the ``id``-order scan is faithful. Pure orchestration: the DB read lives in
    the injected ``query_ordered`` (the mixin's bound ``_query_ordered``), the
    fold in the DB-free :class:`_CensusAggregator`. Stops on the first
    short / empty page.
    """
    last_id: str | None = None
    while True:
        filters: dict[str, object] = (
            {"id": {"op": "gt", "value": last_id}} if last_id is not None else {}
        )
        page = query_ordered(
            table,
            filters=filters,
            order_by=[["id", "asc"], ["created_at", "asc"]],
            limit=page_size,
        )
        if not page:
            return
        aggregator.fold_event_page(page)
        if len(page) < page_size:
            return
        last_id = str(page[-1]["id"])


def build_census(
    *,
    query: _StateReader,
    query_ordered: _OrderedReader,
    now: datetime,
    page_size: int,
    fingerprint_seeds: tuple[int, int],
) -> list[dict[str, object]]:
    """Compose the per-source ledger census — SQL-lockdown GAP-1 composition root.

    Issues the bounded source / session / tool_call / import_batch reads via the
    injected ``query`` (the mixin's ``_query`` → ``query_state``; small-row
    tables, read whole), pages the ~1M-row ``__event`` scan via ``query_ordered``
    (:func:`fold_census_events`), folds everything in the DB-free
    :class:`_CensusAggregator`, and returns the per-source rows. Operator D1
    ruling (2026-06-20): Python-fold + re-baseline, NO aggregate primitive. The
    read seam is dependency-injected (no held service reference), so this is the
    full census composition the mixin delegates to in one thin method — keeping
    ``SessionLedgerReadMixin`` a focused query surface (the god-class budget).
    ``now`` is the repository clock pre-normalized to naive UTC by the caller
    (the F1 batch-age seam).
    """
    aggregator = _CensusAggregator(
        sources=query(TABLE_SOURCE, {"is_deleted": 0}),
        sessions=query(TABLE_SESSION, {"is_deleted": 0}),
        tool_calls=query(TABLE_TOOL_CALL, {"is_deleted": 0}),
        import_batches=query(TABLE_IMPORT_BATCH, {"is_deleted": 0}),
        fingerprint_seeds=fingerprint_seeds,
    )
    fold_census_events(
        aggregator,
        query_ordered=query_ordered,
        table=TABLE_EVENT,
        page_size=page_size,
    )
    return aggregator.result(now=now)


# ─── list_sessions read-then-route fold (SQL-lockdown junction migration) ─────
# The source_kind EXISTS-over-canonical-group subquery retires onto a junction
# read (canonical_ids) + an UNCAPPED query_state session read; the two two-sided
# event_at windows (last_event_at since/until + first_event_at first_since/
# first_until) + the configurable SessionsOrderBy sort + the limit are not
# expressible in the one-condition-per-column grammar, so they are applied here
# over the full candidate set (the #11 list_active_sessions / Slice-1 pattern).

# SessionsOrderBy → (column, reverse) for the Python sort. Mirrors the
# pre-migration raw-SQL ORDER BY fragment ("<col> DESC/ASC").
_SESSIONS_ORDER_KEY: dict[SessionsOrderBy, tuple[str, bool]] = {
    SessionsOrderBy.LAST_EVENT_AT_DESC: ("last_event_at", True),
    SessionsOrderBy.LAST_EVENT_AT_ASC: ("last_event_at", False),
    SessionsOrderBy.FIRST_EVENT_AT_DESC: ("first_event_at", True),
    SessionsOrderBy.FIRST_EVENT_AT_ASC: ("first_event_at", False),
}

# The pre-migration verb's exact SELECT projection (10 columns); the uncapped
# query_state read returns SELECT *, so the fold narrows to these.
_LIST_SESSIONS_PROJECTION: tuple[str, ...] = (
    "id", "source_id", "external_session_id", "vendor", "vendor_session_label",
    "project_path", "first_event_at", "last_event_at", "event_count",
    "canonical_external_session_id",
)


@dataclass(frozen=True, slots=True)
class SessionWindow:
    """The two two-sided event_at windows, pre-normalized to naive UTC.

    All bounds are ``_naive_utc``-applied by the caller (F1 seam) so the compare
    is naive-vs-naive against the parsed ISO row values. ``None`` = unbounded.
    """

    since: datetime | None
    until: datetime | None
    first_event_since: datetime | None
    first_event_until: datetime | None


def _parse_session_dt(value: object) -> datetime:
    """Parse a session row's ISO ``*_event_at`` to a naive-UTC datetime.

    ``query_state`` serializes datetimes to naive ISO strings (the columns are
    ``timestamp without time zone``); an offline shim may hand back a datetime.
    Either way the result is naive UTC so it compares against the window bounds.
    """
    dt = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
    return dt.astimezone(UTC).replace(tzinfo=None) if dt.tzinfo is not None else dt


def _session_in_window(row: dict[str, object], window: SessionWindow) -> bool:
    """True iff the row satisfies BOTH two-sided event_at windows."""
    last = _parse_session_dt(row["last_event_at"])
    if window.since is not None and last < window.since:
        return False
    if window.until is not None and last > window.until:
        return False
    first = _parse_session_dt(row["first_event_at"])
    if window.first_event_since is not None and first < window.first_event_since:
        return False
    return not (
        window.first_event_until is not None and first > window.first_event_until
    )


def _project_session_row(row: dict[str, object]) -> dict[str, object]:
    """Narrow a SELECT-* session row to the verb's 10-column envelope."""
    return {key: row.get(key) for key in _LIST_SESSIONS_PROJECTION}


def select_sessions_page(
    candidates: list[dict[str, object]],
    *,
    window: SessionWindow,
    order_by: SessionsOrderBy,
    limit: int,
) -> list[dict[str, object]]:
    """Apply the two windows + the SessionsOrderBy sort + the limit; project.

    ``candidates`` is the full uncapped ``query_state`` session read (the source
    plugins / equality filters already applied in-query). The two-sided windows
    can't ride the flat filter grammar, so they are applied here, then the
    configurable sort, then the limit (the #11/Slice-1 uncapped + Python
    pattern). The ``(sort-col, id)`` key is total-order DETERMINISTIC where the
    pre-migration ``ORDER BY <col>`` left ties arbitrary (so a tie at the limit
    boundary no longer returns a non-deterministic subset).
    """
    kept = [row for row in candidates if _session_in_window(row, window)]
    column, reverse = _SESSIONS_ORDER_KEY[order_by]
    kept.sort(
        key=lambda row: (_parse_session_dt(row[column]), str(row.get("id", ""))),
        reverse=reverse,
    )
    return [_project_session_row(row) for row in kept[:limit]]


def _junction_canonical_ids(
    query: _StateReader, source_kind: IngestSourceKind,
) -> list[str]:
    """Canonical session ids whose group has a contributor of ``source_kind``.

    Reads the ``session_source_kind`` junction. BOTH attach paths populate it — a
    new canonical writes ``(its id, its kind)`` and a demoted sibling writes
    ``(the canonical's id, the SIBLING's kind)`` (``ingest.py`` lines 333-368) — so
    a hit means the canonical's ``(vendor, external_session_id)`` group has ANY
    contributor of that kind, byte-equivalent to the pre-migration
    EXISTS-over-the-group.
    """
    return [
        str(row["canonical_session_id"])
        for row in query(
            TABLE_SESSION_SOURCE_KIND,
            {"source_kind": source_kind.value, "is_deleted": 0},
        )
    ]


def _read_full_group_membership(
    query: _StateReader,
    *,
    canonical_ids: list[str],
    vendor: SourceVendor | None,
    project_path: str | None,
) -> list[dict[str, object]]:
    """Expand canonical ids to their groups' FULL membership (canonical + siblings).

    The ``include_siblings=True`` + ``source_kind`` path. The junction yields
    canonical SESSION ids, but a sibling links to its canonical via the SHARED
    ``external_session_id`` — NOT the session id (its
    ``canonical_external_session_id`` column holds the canonical's EXTERNAL id,
    ``ingest.py`` lines 357-359), so a direct ``id = ANY(canonical_ids)`` read
    drops siblings. Faithful expansion (byte-equivalent to the pre-migration
    EXISTS-over-canonical-group with ``include_siblings``):

    1. Read the qualifying canonicals (``id = ANY(canonical_ids)``) → their
       ``external_session_id`` values + ``(vendor, external_session_id)`` group keys.
    2. Read every session sharing those ``external_session_id`` values
       (``external_session_id = ANY(ext_ids)``) — captures canonical AND siblings
       of each group, because both carry the same ``external_session_id`` BY
       CONSTRUCTION (the demote fires on ``ON CONFLICT (vendor,
       external_session_id)``, ``ingest.py`` lines 324-332, so the sibling was
       inserted with the canonical's value).
    3. Python-filter to ``(vendor, external_session_id) ∈`` the qualifying group
       keys — restores the EXISTS's ``s2.vendor = s.vendor AND s2.external_session_id
       = s.external_session_id``, dropping a cross-vendor same-external-id
       over-match (practically impossible — external ids are vendor-namespaced —
       but ``pairs`` closes it provably; we just got burned on an "unreachable"
       assumption, so belt-and-suspenders).

    Robust to junction staleness: a stale ``canonical_id`` that now points at a
    demoted (now-sibling) row still yields the correct group key, since that row
    shares its group's ``(vendor, external_session_id)``.

    The ``vendor`` / ``project_path`` equality filters apply to the MEMBER read
    (the outer row's own columns, as the pre-migration EXISTS outer WHERE did);
    ``pairs`` handles group qualification.
    """
    canonicals = query(TABLE_SESSION, {"id": canonical_ids, "is_deleted": 0})
    if not canonicals:
        return []
    ext_ids = sorted({str(row["external_session_id"]) for row in canonicals})
    pairs = {
        (str(row["vendor"]), str(row["external_session_id"]))
        for row in canonicals
    }
    member_filters: dict[str, object] = {
        "external_session_id": ext_ids,
        "is_deleted": 0,
    }
    if project_path is not None:
        member_filters["project_path"] = project_path
    if vendor is not None:
        member_filters["vendor"] = vendor.value
    return [
        row
        for row in query(TABLE_SESSION, member_filters)
        if (str(row["vendor"]), str(row["external_session_id"])) in pairs
    ]


def list_sessions_via_junction(
    query: _StateReader,
    *,
    window: SessionWindow,
    project_path: str | None,
    vendor: SourceVendor | None,
    source_kind: IngestSourceKind | None,
    order_by: SessionsOrderBy,
    limit: int,
    include_siblings: bool,
) -> list[dict[str, object]]:
    """list_sessions junction read-then-route (Architect ruling 2026-06-22).

    * **source_kind via the ``session_source_kind`` junction.** Replaces the
      EXISTS-over-canonical-group subquery: ``query(junction, {source_kind: K})``
      → the canonical ids whose ``(vendor, external_session_id)`` group has a
      contributor of kind K → restrict the session read to that group; no
      junction match → ``[]``.
    * **Uncapped ``query_state`` + Python fold.** The two two-sided ``event_at``
      windows + the configurable ``SessionsOrderBy`` sort + ``limit`` are not
      expressible in the one-condition-per-column grammar, so the session read is
      UNCAPPED and :func:`select_sessions_page` applies the windows + sort + limit
      over the full candidate set (the #11 / Slice-1 pattern; query_ordered's cap
      would make the second window's post-filter unfaithful). Window bounds are
      ``_naive_utc``-normalized by the caller (F1 seam).
    * **Canonical-only by default** unless ``include_siblings=True``.

    ``include_siblings=True`` + ``source_kind`` returns each matching group's FULL
    membership (canonical + siblings), faithful to the pre-migration EXISTS. The
    junction yields canonical ids, but siblings link via the SHARED
    ``external_session_id`` (not the session id), so this combo expands via
    :func:`_read_full_group_membership` rather than the direct ``id = ANY`` read.
    ``include_siblings=False`` keeps the ``canonical_external_session_id IS NULL``
    filter — which also drops a stale junction id that now points at a demoted
    sibling (the blessed union-on-attach / recompute-on-repair staleness handling:
    the canonical-IS-NULL filter drops a non-canonical id).
    """
    if source_kind is not None and include_siblings:
        canonical_ids = _junction_canonical_ids(query, source_kind)
        if not canonical_ids:
            return []
        return select_sessions_page(
            _read_full_group_membership(
                query,
                canonical_ids=canonical_ids,
                vendor=vendor,
                project_path=project_path,
            ),
            window=window,
            order_by=order_by,
            limit=limit,
        )
    filters: dict[str, object] = {"is_deleted": 0}
    if not include_siblings:
        filters["canonical_external_session_id"] = {"op": "is_null"}
    if project_path is not None:
        filters["project_path"] = project_path
    if vendor is not None:
        filters["vendor"] = vendor.value
    if source_kind is not None:
        canonical_ids = _junction_canonical_ids(query, source_kind)
        if not canonical_ids:
            return []
        filters["id"] = canonical_ids
    return select_sessions_page(
        query(TABLE_SESSION, filters),
        window=window,
        order_by=order_by,
        limit=limit,
    )


__all__ = [
    "RELOAD_SAFE",
    "SessionWindow",
    "_CensusAggregator",
    "_build_canonical_contributors_result",
    "_content_json_subtype",
    "_fingerprint_component",
    "_merge_active_leases",
    "_pick_canonical_session_id",
    "_select_latest_away_summary",
    "build_canonical_contributors_via_group",
    "build_census",
    "fold_census_events",
    "list_sessions_via_junction",
    "select_quiescent_sessions",
    "select_sessions_page",
]
