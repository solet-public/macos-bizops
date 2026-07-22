"""Concrete :class:`SessionLedgerService` — thin facade over the foundation modules.

Spec §11.1 / §14.9. Composition:

* ``SessionSourceRegistry`` — discovered via ``collect_llm_session_sources``.
* ``SessionLedgerRepository`` — SQL adapter over ``state_service``.
* ``SessionLedgerBlobAdapter`` — large-content / attachment staging via
  ``blob_storage_service``.
* ``SessionLedgerImporter`` — poll loop + push dispatch.

The service implements the eight split ABCs (``SessionLedgerReadAPI`` /
``SessionLedgerIngestAPI`` /
``SessionLedgerPollingDriverAPI`` / ``SessionLedgerCanonicalPointerRepairAPI``
/ ``SessionLedgerInvertedBoundsRepairAPI`` / ``SessionLedgerSummarizeAPI`` /
``SessionLedgerDeploymentAPI`` / ``SessionLedgerSearchAPI``) via multiple
inheritance and is constructed once
by ``startup_sequence._init_session_ledger_service`` after the platform
state + blob wrappers exist and the source plugins have been started.

"""

from __future__ import annotations

import logging
import os
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ananta.interfaces.inference_service_interface import InferenceRequest
from ananta.llm.session_ledger.blob_adapter import SessionLedgerBlobAdapter
from ananta.llm.session_ledger.event_embeddings import (
    EventEmbeddingServicesUnavailableError,
    EventEmbeddingWriter,
)
from ananta.llm.session_ledger.importer import SessionLedgerImporter
from ananta.llm.session_ledger.registry import SessionSourceRegistry
from ananta.llm.session_ledger.repository import (
    LedgerRepositoryError,
    SessionLedgerRepository,
)
from ananta.llm.session_ledger.root_uri import (
    canonicalize_root_uri_for_storage,
    normalize_root_uri,
)
from ananta.llm.session_ledger.summarization import (
    SearchResultEnvelope,
    SummaryServicesUnavailableError,
    SummaryWriter,
)
from ananta.llm.session_ledger.trigger_data import extract_authenticated_principal
from ananta.llm.session_ledger.types import (
    IngestSourceKind,
    MessageRole,
    SessionsOrderBy,
    SourceVendor,
)
from ananta.services.session_ledger_service.blob_identity_backfill import (
    ExportBlobIdentityBackfill,
)
from ananta.services.session_ledger_service.enforcement import (
    assert_operator_principal,
    assert_register_source_authorized,
)
from ananta.services.session_ledger_service.interfaces.public import (
    SessionLedgerCanonicalPointerRepairAPI,
    SessionLedgerDeploymentAPI,
    SessionLedgerEmbeddingDrainAPI,
    SessionLedgerEventExternalIdBackfillAPI,
    SessionLedgerEventSourceDenormBackfillAPI,
    SessionLedgerIngestAPI,
    SessionLedgerInvertedBoundsRepairAPI,
    SessionLedgerPollingDriverAPI,
    SessionLedgerReadAPI,
    SessionLedgerSearchAPI,
    SessionLedgerSessionSourceKindBackfillAPI,
    SessionLedgerSummarizeAPI,
)
from ananta.services.session_ledger_service.poll_drain import (
    start_importer_poll_drain,
)
from ananta.services.session_ledger_service.summary_executor import (
    BoundedSummaryExecutor,
    SummaryExecutor,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ananta.core.plugins.plugin_manager import PluginManager
    from ananta.core.services.call_context import CallContext
    from ananta.interfaces.blob_storage_service_interface import (
        BlobStorageServiceInterface,
    )
    from ananta.interfaces.embedding_service_interface import EmbeddingServiceInterface
    from ananta.interfaces.state_management_interface import StateManagementInterface
    from ananta.interfaces.vector_service_interface import VectorServiceInterface

logger = logging.getLogger(__name__)

# GAP-5 slice 3 — ``reset_ingest_state`` is now a NON-DESTRUCTIVE cursor reset.
# It clears every source's ``__source_cursor`` rows so the next poll replays
# each source and the live ``(session_id, external_id)`` upsert reconverges; no
# content is deleted. ``_RESET_ACTION`` namespaces the new semantics in the verb
# result (no magic string); ``_PRESERVED_CONTENT_TABLES`` are the data tables the
# reset KEEPS — their live counts are surfaced on the dry-run so the operator can
# see the content the reset preserves (in contrast to the pre-GAP-5 hard-delete).
_RESET_ACTION = "cursor_reset_replay"
_PRESERVED_CONTENT_TABLES = (
    "session_ledger__session",
    "session_ledger__event",
    "session_ledger__tool_call",
    "session_ledger__attachment",
    "session_ledger__import_batch",
)

# System-owned scheduler identifiers for the periodic-poll cron. Mirrors the
# pattern at default_scheduling_plugin.constants.HEARTBEAT_FLOW_ID — the
# scheduler-fired action_definition needs a stable flow_id/session_id at fire
# time (action_factory._enforce_flow_id refuses an absent flow_id with no
# context fallback). Distinct from the heartbeat constants per Architect
# 2026-05-31 so the periodic-poll fires are independently attributable in
# audit logs.
_LEDGER_PERIODIC_POLL_FLOW_ID = "flow-ledger-periodic-poll"
_LEDGER_PERIODIC_POLL_SESSION_ID = "sess-ledger-periodic-poll"
_LEDGER_PERIODIC_SUMMARIZE_FLOW_ID = "flow-ledger-periodic-summarize"
_LEDGER_PERIODIC_SUMMARIZE_SESSION_ID = "sess-ledger-periodic-summarize"
_LEDGER_PERIODIC_EMBED_FLOW_ID = "flow-ledger-periodic-embed"
_LEDGER_PERIODIC_EMBED_SESSION_ID = "sess-ledger-periodic-embed"
# Operator ruling 2026-06-01 Bug 1 fix: three discriminators on
# ``__summary.generated_by_client_id`` so every embed attributes back to
# its origin path. Architect's 2026-05-31 §3 mapping is preserved
# (custom_title remains the operator-authoritative summary text) while
# making those sessions searchable by also embedding the seed text.
_AUTO_SUMMARIZE_CLIENT_ID_CUSTOM_TITLE = (
    "internal:auto_summarize:custom_title_seed"
)
# M19 (Codex state_5.threads.title → __session.summary_text_seed): distinct
# discriminator per v2 §5.5 canonical (``_title`` suffix prevents collision
# if a future M-section adds another state_5-derived seed like
# ``first_user_message`` or ``preview``). Source-kind dispatch in
# ``_summarize_one_session`` picks this over CUSTOM_TITLE when the
# session's __source.source_kind is ``codex_state``.
_AUTO_SUMMARIZE_CLIENT_ID_CODEX_STATE_TITLE = (
    "internal:auto_summarize:codex_state_title_seed"
)
_AUTO_SUMMARIZE_CLIENT_ID_EXTRACTED = (
    "internal:auto_summarize:extracted_away_summary"
)
_AUTO_SUMMARIZE_CLIENT_ID_INFERRED = "internal:auto_summarize:inferred"

# Source-kind → seed-discriminator dispatch. Sessions whose source_kind
# is not present here fall back to CUSTOM_TITLE (the pre-M19 default,
# preserved for backward compat with claude_code's operator-set custom_title
# and any future source whose seed is a curated operator-set title).
_SEED_DISCRIMINATOR_BY_SOURCE_KIND: dict[str, str] = {
    "codex_state": _AUTO_SUMMARIZE_CLIENT_ID_CODEX_STATE_TITLE,
}
# Bound transcript size fed to the summarizer per session — caps inference
# cost when a quiescent session has thousands of events. ~24 KB matches the
# agent_messaging_plugin's run_turn prompt cap.
_AUTO_SUMMARIZE_MAX_EVENTS = 50
_AUTO_SUMMARIZE_MAX_CHARS = 24_000
# System prompt sets the model's ROLE as an outside analyst — NOT a
# participant. Without this, the configured instruct model
# (qwen/qwen3-30b-a3b-2507) gets pulled into CONTINUING the transcript
# (e.g. answering the last user turn / writing more of the same content),
# blows the token budget, and the LM Studio provider raises
# ``Response truncated`` → the pass records ``"skipped"`` with no sentinel →
# the row is re-picked forever (head-of-line clog). Proven 2026-06-30: the
# bare "summarize" prompt made the model write MORE transcript,
# finish_reason=length at 299 tokens; the analyst framing below returns a
# clean 3–5 sentence summary in ~150–200 tokens with finish_reason=stop.
_AUTO_SUMMARIZE_PROMPT = (
    "You are a transcript analyst. You are given a record of a past "
    "conversation between a user and an AI assistant. Your only job is to "
    "DESCRIBE what happened in it. Never continue, answer, or roleplay the "
    "conversation — only summarize it from the outside."
)
# The transcript is fenced between markers and the instruction is RE-STATED
# AFTER it — models follow a trailing instruction far more reliably than one
# buried before a long transcript. ``{transcript}`` is filled at call time.
_AUTO_SUMMARIZE_USER_TEMPLATE = (
    "Here is the session transcript, between the markers:\n"
    "<<<TRANSCRIPT\n"
    "{transcript}\n"
    "TRANSCRIPT>>>\n\n"
    "In 2–4 sentences of plain prose (no preamble, no bullet points), "
    "summarize the transcript above: the user's intent, the work the "
    "assistant performed, and any artifacts or decisions produced. "
    "Describe the session in the third person; do NOT continue or "
    "respond to it."
)
# Operator ruling 2026-06-01 D8: cap inference temp + length at the seam
# closest to the call so peer tuning lives in one place.
_AUTO_SUMMARIZE_INFERENCE_TEMPERATURE = 0.3
# 400 (was 300) — pure headroom. The analyst-framed prompt produces summaries
# in ~150–200 tokens; the extra budget absorbs the occasional longer session
# so a correct summary finishes (finish_reason=stop) rather than tripping the
# provider's length-truncation guard on a near-miss.
_AUTO_SUMMARIZE_INFERENCE_MAX_TOKENS = 400
# Trivial-session sentinel: written into ``session.summary_text`` so the
# quiescent-session list stops re-picking sessions that can't usefully be
# summarized (operator ruling 2026-06-01 D8). Distinct prose so it's
# trivially greppable in audits.
_AUTO_SUMMARIZE_TRIVIAL_SENTINEL = "(trivial session — no summarization)"
# Trivial-threshold tuning (operator ruling 2026-06-01 D8): "below 4 events
# OR zero assistant role events". Below the floor the transcript can't carry
# enough signal to summarize; the cron marks-and-moves-on.
_AUTO_SUMMARIZE_TRIVIAL_MIN_EVENTS = 4
# Operator ruling 2026-06-30: summarize ONLY real conversation — user + assistant
# messages. Everything else (tool results, system/hook events, the ~245K null-role
# claude_code noise, agent_messaging coordination chatter) is excluded from both
# the transcript and the trivial-session count, so the summary reflects the
# conversation and noise-only sessions are correctly marked trivial.
_CONVERSATION_ROLES = frozenset({MessageRole.USER.value, MessageRole.ASSISTANT.value})


class SessionLedgerService(
    SessionLedgerReadAPI,
    SessionLedgerIngestAPI,
    SessionLedgerPollingDriverAPI,
    SessionLedgerCanonicalPointerRepairAPI,
    SessionLedgerInvertedBoundsRepairAPI,
    SessionLedgerEventExternalIdBackfillAPI,
    SessionLedgerEventSourceDenormBackfillAPI,
    SessionLedgerSessionSourceKindBackfillAPI,
    SessionLedgerSummarizeAPI,
    SessionLedgerDeploymentAPI,
    SessionLedgerEmbeddingDrainAPI,
    SessionLedgerSearchAPI,
):
    """Thin delegation facade — every public method maps to one repository call.

    No business logic in the service itself beyond:
    * argument coercion (str → enum / ISO timestamp → datetime),
    * envelope shaping (dict[str, list[...]] return type), and
    * principal extraction for authz-bearing methods.

    Importer + repository do all heavy lifting.
    """

    __slots__ = (
        "_registry",
        "_repository",
        "_state_service",
        "_blob_adapter",
        "_importer",
        "_summary_writer",
        "_event_embedding_writer",
        "_operator_equivalent_check",
        "_scheduling_service",
        "_inference_service",
        "_ledger_allowed_roots",
    )

    def __init__(
        self,
        *,
        state_service: StateManagementInterface,
        blob_storage_service: BlobStorageServiceInterface,
        plugin_manager: PluginManager,
        embedding_service: EmbeddingServiceInterface | None = None,
        vector_service: VectorServiceInterface | None = None,
        scheduling_service: Any = None,
        inference_service: Any = None,
        summary_executor: SummaryExecutor | None = None,
        polling_lease_ttl_seconds: int = 600,
        ledger_allowed_roots: list[str] | None = None,
    ) -> None:
        self._state_service = state_service
        # P1.1.E authz containment: filesystem ``root_uri`` registrations
        # through the public ``register_source`` verb must be contained under
        # one of these operator-configured roots. Empty = deny every filesystem
        # registration (secure default); the export/pushed/boot paths use the
        # trusted internal seam and are unaffected.
        self._ledger_allowed_roots: list[str] = list(ledger_allowed_roots or [])
        self._repository = SessionLedgerRepository(state_service)
        self._blob_adapter = SessionLedgerBlobAdapter(blob_storage_service)
        self._registry = SessionSourceRegistry(plugin_manager.plugins)
        # v8 §D14.F: polling-lease TTL is operator-tunable via the
        # session_ledger_service plugin config (default 600s).
        self._importer = SessionLedgerImporter(
            registry=self._registry,
            repository=self._repository,
            blob_adapter=self._blob_adapter,
            polling_lease_ttl_seconds=polling_lease_ttl_seconds,
        )
        self._summary_writer, self._event_embedding_writer = _build_semantic_writers(
            repository=self._repository,
            embedding_service=embedding_service,
            vector_service=vector_service,
        )
        # M5 §13.3: bridge plugin wires this to vault.is_operator_equivalent
        # after the vault registry is available (M5.C). Default None means
        # only direct ownership-binding admits approve_pairing.
        self._operator_equivalent_check: Callable[[str], bool] | None = None
        # M5.C deferral #4: scheduling_service is bound via service_bindings
        # and looked up at startup. ``ensure_periodic_poll_schedule`` raises
        # cleanly if this is None (e.g., a profile that omits scheduling).
        self._scheduling_service = scheduling_service
        # 2026-05-31 Gap 2(A): inference_service for auto-summarize cron.
        # ``summarize_quiescent_sessions`` raises cleanly if None.
        self._inference_service = inference_service
        # Singleton drain guard — full contract on the ABC docstring of
        # ``summarize_quiescent_sessions``: one background drainer across
        # overlapping cron fires (second ``submit`` no-ops); injectable for
        # tests. Phase 5 will route drain inference through the resolver.
        self._summary_executor, self._embedding_executor, self._poll_executor = _build_drain_executors(summary_executor)
        logger.debug(
            "session_ledger_service constructed: %d source plugin(s) registered",
            len(self._registry.list_sources()),
        )

    # ------------------------------------------------------------------
    # Inspection helpers (used by startup smoke and operator diagnostics)
    # ------------------------------------------------------------------

    @property
    def registry(self) -> SessionSourceRegistry:
        return self._registry

    @property
    def blob_storage_service(self) -> BlobStorageServiceInterface:
        """The bound blob_storage_service wrapper — used by startup smoke."""
        return self._blob_adapter.blob_storage_service

    @property
    def importer(self) -> SessionLedgerImporter:
        """Exposed for the operator-bridge poll trigger and integration smoke."""
        return self._importer

    # ------------------------------------------------------------------
    # Read surface
    # ------------------------------------------------------------------

    def list_sources(self) -> dict[str, Any]:
        """Return registered ingest sources joined with their plugin descriptors.

        Per Coordinator Q1 ruling 2026-05-31 Option A: each result entry
        carries both the descriptor surface (source_kind, vendor,
        supported_modes, default_lease_ttl_seconds, default_pulling_root_uri)
        AND the ``session_ledger__source`` DB row surface (source_id, root_uri,
        account_label, enabled, config_json), matched on ``source_kind``.

        Three shapes appear:

        * Plugin loaded AND row registered → one entry per (source_kind,
          root_uri) row, all fields populated.
        * Plugin loaded, no row registered yet → one descriptor-only entry
          (DB fields null) so the operator can see what *could* be registered.
        * Row registered, no plugin loaded → one DB-only entry (descriptor
          fields null) so the operator sees the orphaned row.
        """
        descriptors = self._registry.list_sources()
        rows = self._repository.list_sources(enabled_only=False)
        descriptor_by_kind = {d.source_kind: d for d in descriptors}
        rows_by_kind: dict[IngestSourceKind, list[Any]] = {}
        for row in rows:
            rows_by_kind.setdefault(row.source_kind, []).append(row)
        sources: list[dict[str, Any]] = []
        for descriptor in descriptors:
            matching_rows = rows_by_kind.get(descriptor.source_kind, [])
            if not matching_rows:
                sources.append(_descriptor_only_entry(descriptor))
                continue
            for row in matching_rows:
                sources.append(_joined_entry(descriptor, row))
        for kind, kind_rows in rows_by_kind.items():
            if kind in descriptor_by_kind:
                continue
            for row in kind_rows:
                sources.append(_row_only_entry(row))
        return {"sources": sources}

    def census(self) -> dict[str, Any]:
        """SQL-aggregated read-only inventory of the whole ledger.

        One call answers "which sources, how much content, any duplicate source
        rows, any orphan batches" over the entire corpus — the read APIs clamp
        ``limit`` and cannot count a ~1M-event corpus. Per source it returns
        counts (sessions split canonical/sibling, events, tool_calls), batch
        health (owned-running vs unclaimed-route, oldest-running age), the
        normalized ``root_uri``, and the order-independent row-identity
        fingerprint. ``duplicate_source_groups`` counts ``(source_kind,
        normalized root_uri)`` keys with more than one live row.
        """
        rows = self._repository.census_source_rows()
        sources: list[dict[str, Any]] = []
        group_counts: dict[tuple[str, str], int] = {}
        totals = {
            "sessions": 0, "events": 0, "tool_calls": 0,
            "owned_running_batches": 0, "unclaimed_route_batches": 0,
        }
        for row in rows:
            normalized = normalize_root_uri(str(row["root_uri"]))
            fingerprint_a = row.get("fingerprint_a")
            fingerprint_b = row.get("fingerprint_b")
            fingerprint = (
                f"{fingerprint_a}:{fingerprint_b}"
                if fingerprint_a is not None
                else None
            )
            age_raw = row.get("oldest_running_batch_age_seconds")
            sources.append({
                "source_id": row["source_id"],
                "source_kind": row["source_kind"],
                "root_uri": row["root_uri"],
                "normalized_root_uri": normalized,
                "session_count": int(row["session_count"]),  # type: ignore[arg-type]
                "canonical_count": int(row["canonical_count"]),  # type: ignore[arg-type]
                "sibling_count": int(row["sibling_count"]),  # type: ignore[arg-type]
                "event_count": int(row["event_count"]),  # type: ignore[arg-type]
                "tool_call_count": int(row["tool_call_count"]),  # type: ignore[arg-type]
                "row_identity_fingerprint": fingerprint,
                "owned_running_batches": int(row["owned_running_batches"]),  # type: ignore[arg-type]
                "unclaimed_route_batches": int(row["unclaimed_route_batches"]),  # type: ignore[arg-type]
                "oldest_running_batch_age_seconds": (
                    int(age_raw) if age_raw is not None else None  # type: ignore[arg-type]
                ),
            })
            key = (str(row["source_kind"]), normalized)
            group_counts[key] = group_counts.get(key, 0) + 1
            totals["sessions"] += int(row["session_count"])  # type: ignore[arg-type]
            totals["events"] += int(row["event_count"])  # type: ignore[arg-type]
            totals["tool_calls"] += int(row["tool_call_count"])  # type: ignore[arg-type]
            totals["owned_running_batches"] += int(row["owned_running_batches"])  # type: ignore[arg-type]
            totals["unclaimed_route_batches"] += int(row["unclaimed_route_batches"])  # type: ignore[arg-type]
        return {
            "sources": sources,
            "source_count": len(sources),
            "duplicate_source_groups": sum(1 for n in group_counts.values() if n > 1),
            "total_sessions": totals["sessions"],
            "total_events": totals["events"],
            "total_tool_calls": totals["tool_calls"],
            "total_owned_running_batches": totals["owned_running_batches"],
            "total_unclaimed_route_batches": totals["unclaimed_route_batches"],
        }

    def list_sessions(
        self,
        limit: int = 50,
        since: datetime | str | None = None,
        until: datetime | str | None = None,
        first_event_since: datetime | str | None = None,
        first_event_until: datetime | str | None = None,
        project_path: str | None = None,
        vendor: SourceVendor | str | None = None,
        source_kind: IngestSourceKind | str | None = None,
        order_by: SessionsOrderBy | str = SessionsOrderBy.LAST_EVENT_AT_DESC,
        include_siblings: bool = False,
    ) -> dict[str, Any]:
        # M17 §2.2: ABC declares enum-typed params; the action_processor's
        # JSON-channel passes strings, so coerce at the service-entry seam.
        # First-party Python callers can pass typed enums directly and the
        # isinstance check is a no-op.
        # W5.B §3.7.A: include_siblings flows through to the repository's
        # canonical-only-by-default filter + EXISTS-over-canonical-group
        # source_kind handling (Codex C2 cross-source dedupe correction).
        rows = self._repository.list_sessions(
            limit=limit,
            since=_coerce_datetime(since),
            until=_coerce_datetime(until),
            first_event_since=_coerce_datetime(first_event_since),
            first_event_until=_coerce_datetime(first_event_until),
            project_path=project_path,
            vendor=_coerce_enum(vendor, SourceVendor),
            source_kind=_coerce_enum(source_kind, IngestSourceKind),
            order_by=_coerce_enum(order_by, SessionsOrderBy)
            or SessionsOrderBy.LAST_EVENT_AT_DESC,
            include_siblings=include_siblings,
        )
        return {"sessions": rows}

    def list_active_sessions(self) -> dict[str, Any]:
        return {"sessions": self._repository.list_active_sessions()}

    def get_session_timeline(
        self,
        session_id: str,
        after_sequence: int = 0,
        limit: int = 100,
    ) -> dict[str, Any]:
        events = self._repository.get_session_timeline(
            session_id=session_id,
            after_sequence=after_sequence,
            limit=limit,
        )
        return {"events": events}

    def list_tool_calls(
        self,
        session_id: str | None = None,
        tool_name: str | None = None,
        status: str | None = None,
        since_iso: str | None = None,
        limit: int = 50,
    ) -> dict[str, Any]:
        since = _parse_iso(since_iso) if since_iso is not None else None
        rows = self._repository.list_tool_calls(
            session_id=session_id,
            tool_name=tool_name,
            status=status,
            since=since,
            limit=limit,
        )
        return {"tool_calls": rows}

    def list_canonical_contributors(self, session_id: str) -> dict[str, Any]:
        """W5.B §3.3 provenance projection wrapper.

        Delegates to the repository's CTE+ (vendor, external_session_id)
        SELECT; passes the response envelope through unchanged. Per Codex
        C3 the envelope carries three top-level fields plus the
        ``contributors`` list (canonical-input + sibling-input + orphaned-
        canonical cases handled in the repository layer).
        """
        return self._repository.list_canonical_contributors(
            session_id=session_id,
        )

    def get_import_status(self, batch_id: str) -> dict[str, Any]:
        row = self._repository.get_import_status(batch_id)
        if row is None:
            raise LedgerRepositoryError(f"no import batch with id {batch_id!r}")
        return row

    # ------------------------------------------------------------------
    # Write surface
    # ------------------------------------------------------------------

    def register_source(
        self,
        source_kind: str,
        root_uri: str,
        account_label: str | None = None,
        config_json: dict[str, Any] | None = None,
        *,
        call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        """Register an ingest source; idempotent on ``(source_kind, root_uri)``.

        On hit, returns the existing row with ``outcome='existed'`` — never
        re-INSERTs. On miss, inserts a fresh row with
        ``outcome='registered'``. Mirrors the schema description's
        documented invariant (``session_ledger__source`` is
        one-row-per-``(source_kind, root_uri)``).

        ``account_label`` / ``config_json`` are applied only when a new row is
        inserted; an existing row's values are NOT overwritten. Operators who
        need to update those fields use a future ``update_source`` verb
        (out of M1 scope).

        **Authorization (P1.1.E).** A **filesystem** ``root_uri`` requires an
        operator/operator-equivalent ``call_context`` AND containment under a
        configured ``ledger_allowed_roots`` entry — because once the pulling
        plugins honor a per-source ``root_uri`` this verb can point the ledger
        at arbitrary local files. Blob-id / pushed / symbolic ``root_uri``
        values (exports/pushed) skip the path check. The boot auto-register and
        the export verbs use the trusted internal seam
        (:meth:`_register_source_internal`) and are NOT subject to this gate.

        NOTE: field name is ``outcome`` not ``action`` because the platform's
        result-contract validator treats a top-level ``action`` key as
        action_status; ``existed``/``registered`` are not valid ActionStatus
        values and trip ``result_status_not_completed`` (same pitfall the
        ``ensure_periodic_poll_schedule`` comment above flags for
        ``status``). 2026-05-31: Coordinator traced the ledger boot
        cascade to this exact footgun.
        """
        assert_register_source_authorized(
            root_uri=root_uri,
            call_context=call_context,
            allowed_roots=self._ledger_allowed_roots,
        )
        return self._register_source_internal(
            source_kind=source_kind,
            root_uri=root_uri,
            account_label=account_label,
            config_json=config_json,
        )

    def _register_source_internal(
        self,
        *,
        source_kind: str,
        root_uri: str,
        account_label: str | None = None,
        config_json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Idempotent registration seam — normalize then check-then-insert.

        The trusted in-process registration path (NOT a ``@service_interface``
        verb, so unreachable/unspoofable via the bridge). Boot auto-register
        (``startup_sequence``) and the export verbs (A1/A2) call this directly,
        bypassing the public verb's operator-authz gate. ``root_uri`` is
        canonicalized (normalize + realpath-when-exists) so ``~/x`` /
        ``file:///x`` / a symlink alias collapse to one row.
        """
        kind = IngestSourceKind(source_kind)
        canonical_root_uri = canonicalize_root_uri_for_storage(root_uri)
        existing_id = self._repository.find_source_id_by_kind_and_root_uri(
            source_kind=kind, root_uri=canonical_root_uri,
        )
        if existing_id is not None:
            return {"source_id": existing_id, "outcome": "existed"}
        source_id = self._repository.insert_source(
            source_kind=kind,
            root_uri=canonical_root_uri,
            account_label=account_label,
            config=config_json,
        )
        return {"source_id": source_id, "outcome": "registered"}

    def ingest_raw_chunk(
        self,
        source_kind: str,
        chunk_text: str,
        *,
        source_id: str | None = None,
    ) -> dict[str, Any]:
        kind = IngestSourceKind(source_kind)
        result = self._importer.dispatch_pushed(
            source_kind=kind, chunk_text=chunk_text, source_id=source_id,
        )
        return {
            "events_persisted": result.events_persisted,
            "batch_id": result.batch_id,
        }

    def poll_source(self, source_id: str) -> dict[str, Any]:
        """Poll one source by id synchronously; raise on any failure.

        Service-layer delegate for :meth:`SessionLedgerImporter.poll_source`
        (single raising failure channel). Used by the synchronous export-ingest
        kickoff (A1) and the duplicate-source repair completeness step — both
        need "one source polled, zero failed" to be a hard guarantee, not a
        re-interpreted silent zero. Raises
        :class:`~ananta.llm.session_ledger.importer.LedgerPollError`.
        """
        report = self._importer.poll_source(source_id)
        return {
            "sources_polled": report.sources_polled,
            "sessions_seen": report.sessions_seen,
            "events_persisted": report.events_persisted,
            "batches_failed": report.batches_failed,
        }

    def trigger_poll(self) -> dict[str, Any]:
        """Cron heartbeat: (re)start the singleton importer-poll drainer (full contract on the ABC)."""
        return start_importer_poll_drain(self._poll_executor, self._importer)

    def ensure_periodic_poll_schedule(
        self,
        cadence_minutes: int = 5,
        tag: str = "ledger:periodic_poll",
    ) -> dict[str, Any]:
        """Idempotently install a cron that fires trigger_poll every N minutes.

        Mirrors :meth:`scheduling_service.ensure_global_heartbeat`: clear by
        tag, then create one fresh cron schedule whose action invokes
        ``service_interface::session_ledger_service::trigger_poll``.

        Fails fast if scheduling_service was not bound at construction (a
        profile that omits scheduling has no business booting this verb).
        """
        if self._scheduling_service is None:
            raise RuntimeError(
                "ensure_periodic_poll_schedule requires scheduling_service "
                "to be bound at session_ledger_service construction",
            )
        if not 1 <= int(cadence_minutes) <= 59:
            raise ValueError(
                f"cadence_minutes must be between 1 and 59 (got {cadence_minutes})",
            )
        cron_expression = f"*/{int(cadence_minutes)} * * * *"

        clear_result = self._scheduling_service.clear_scheduled_actions_by_tag(
            tag=tag,
        )
        cleared_count = int(
            (clear_result or {}).get("data", {}).get("cleared_count", 0)
        ) if isinstance(clear_result, dict) else 0

        # The cron-fired action is TERMINAL/HEADLESS per the canonical
        # scheduler cron-action contract enforced at ``create_cron_schedule``
        # registration (see ``workbench/2026-06-17_scheduler_cron_action_contract_design.md``
        # for the validator design + the canonical KB article at
        # ``knowledge_bases/ananta_platform/21_scheduling_service/
        # 01_template_flow_record_lifecycle.md``): the action runs
        # synchronously, returns its dict envelope, and exits at
        # ``action_queue_poller._dispatch_*`` via the EDGE_SINK_SKIP branch
        # (``result_processor_kind is None and result_processor is None`` →
        # terminal action, no dispatch). No ``result_processor_kind``
        # declared at schedule level; no ``result_processor_customizations``
        # on the action definition (see ``interfaces/public.py``
        # ``trigger_poll`` ABC). No ``core.flows`` preseed needed — the
        # inference scaffold's ``<<get_flow_input>>`` macro never runs for a
        # terminal-skip action.
        create_result = self._scheduling_service.create_cron_schedule(
            cron_expression=cron_expression,
            actions=[{
                "process_key": "service_interface::session_ledger_service::trigger_poll",
                "arguments": {},
            }],
            label="Ledger periodic poll",
            tags=[tag],
            # Schedule-level flow_id/session_id are propagated into the
            # fired action_definition by action_executor._apply_context_to_definition;
            # action_factory._enforce_flow_id refuses an absent flow_id with no
            # context fallback. Use system-owned identifiers so the cron does
            # not couple to any caller session (mirrors heartbeat pattern;
            # bug detected 2026-05-31 against `dispatch:in_flight:ledger_e2e_functional_bugs`).
            state={
                "flow_id": _LEDGER_PERIODIC_POLL_FLOW_ID,
                "session_id": _LEDGER_PERIODIC_POLL_SESSION_ID,
            },
        )
        # The plugin wraps its envelope as {"data": {...}, "action_status": ...};
        # the service-interface call surface may also return the raw inner
        # data dict. Accept both shapes by extracting schedule_id from
        # whichever level it lives at.
        schedule_id = _extract_schedule_id(create_result)

        outcome = "normalized" if cleared_count > 0 else "created"
        logger.info(
            "session_ledger periodic poll schedule %s: schedule_id=%s tag=%s cadence=%dm",
            outcome, schedule_id, tag, cadence_minutes,
        )
        # NOTE: field name is ``outcome`` not ``status`` because the platform's
        # result-status invariant treats a top-level ``status`` key as the
        # action_status field; ``created``/``normalized`` are not valid
        # ActionStatus values and trip ``result_status_not_completed``.
        # Match the pattern in sibling methods (trigger_poll, ingest_raw_chunk,
        # register_source) which return raw inner dicts without a top-level
        # ``status`` field.
        return {
            "outcome": outcome,
            "schedule_id": schedule_id,
            "tag": tag,
            "cadence_minutes": int(cadence_minutes),
            "cleared_count": cleared_count,
        }

    def reset_ingest_state(self, confirm: bool = False) -> dict[str, Any]:
        """Reset every source's ingest cursor so the next poll replays + reconverges.

        GAP-5 slice 3 (idempotent-ingest design §4) — the verb is now
        NON-DESTRUCTIVE. It clears each source's ``__source_cursor`` rows (via
        the shipped per-source :meth:`reset_source_cursor`); the next poll pass
        re-walks every source from the start and the live
        ``(session_id, external_id)`` upsert dedups the replayed events, so the
        ledger reconverges WITHOUT deleting any content. All content tables
        (``session``/``event``/``tool_call``/``attachment``/``import_batch``)
        and the leases are PRESERVED — replay + upsert makes the old wipe
        unnecessary, and historical rows stay intact + forensically available.

        ``confirm=False`` (default) returns a dry-run: the active cursor count
        that WOULD be cleared per source, plus the live row counts of the
        content tables the reset PRESERVES (so the operator sees the data that
        is KEPT, not deleted). ``confirm=True`` performs the cursor reset and
        returns the per-source cleared-cursor counts.

        NOTE — this does NOT repopulate or correct existing rows. Under the
        landed DO-NOTHING upsert a replayed event that already exists is a
        no-op, so the pre-GAP-5 "reset + restart to re-walk and repopulate
        schema columns" recovery use case is RETIRED: reset only dedups; it
        never rewrites the rows it replays.

        PRECONDITION (``confirm=True``): every event must already carry a
        non-null ``external_id``. A legacy null-``external_id`` row is NOT
        covered by the ``(session_id, external_id)`` unique (NULLs are DISTINCT
        in Postgres), so a post-reset re-walk would derive a non-null id that
        does not conflict with it and INSERT A DUPLICATE. The verb therefore
        REFUSES (raises ``ValueError``) while any null-``external_id`` events
        remain — run ``backfill_event_external_ids`` to 0 nulls first. The
        ``confirm=False`` dry-run reports ``null_external_id_count`` +
        ``precondition_met`` so the operator can see this before committing.
        """
        null_external_id_count = self._repository.count_events_missing_external_id()
        if confirm and null_external_id_count > 0:
            raise ValueError(
                f"reset_ingest_state refused: {null_external_id_count} legacy "
                "event(s) still have a null external_id; run "
                "backfill_event_external_ids to 0 nulls first, else a post-reset "
                "re-walk would DUPLICATE them (NULLs are DISTINCT in the "
                "(session_id, external_id) unique, so a re-derived non-null id "
                "would not conflict with the null legacy row).",
            )
        sources = self._repository.list_sources(enabled_only=False)
        per_source: list[dict[str, object]] = []
        active_before = 0
        cleared_total = 0
        for source in sources:
            before = (
                self._repository.reset_source_cursor(source.id)
                if confirm
                else self._repository.count_active_source_cursors(source.id)
            )
            cleared = before if confirm else 0
            active_before += before
            cleared_total += cleared
            per_source.append(
                {
                    "source_id": source.id,
                    "active_cursor_count_before": before,
                    "deleted_count": cleared,
                },
            )
        result: dict[str, Any] = {
            "confirmed": confirm,
            "action": _RESET_ACTION,
            "content_preserved": True,
            "sources_total": len(sources),
            "active_cursor_count_before": active_before,
            "deleted_count": cleared_total,
            "per_source": per_source,
        }
        if confirm:
            logger.warning(
                "session_ledger reset_ingest_state confirmed (NON-DESTRUCTIVE "
                "cursor reset): cleared %d cursor row(s) across %d source(s); "
                "content preserved — next poll replays + the "
                "(session_id, external_id) upsert reconverges",
                cleared_total, len(sources),
            )
        else:
            counts = self._repository.count_rows_per_table(_PRESERVED_CONTENT_TABLES)
            result["preserved_content"] = [
                {"table": table, "rows_preserved": counts.get(table, 0)}
                for table in _PRESERVED_CONTENT_TABLES
            ]
            result["null_external_id_count"] = null_external_id_count
            result["precondition_met"] = null_external_id_count == 0
        return result

    def lift_canonical_pointer_for_duplicate_sessions(
        self,
        confirm: bool = False,
        tag: str = "ledger:periodic_poll",
        cadence_minutes: int = 5,
    ) -> dict[str, Any]:
        """One-shot pre-flight repair for the M18 partial-unique index landing.

        Mirrors the try/finally pause-resume pattern of
        :meth:`backfill_first_last_event_at_repair`: pauses the importer-
        poll cron so no concurrent poll INSERTs a third canonical sibling
        into a duplicate group mid-repair, then per-group picks the
        chronologically-first canonical survivor and demotes its siblings.
        """
        if not 1 <= int(cadence_minutes) <= 59:
            raise ValueError(
                f"cadence_minutes must be between 1 and 59 (got {cadence_minutes})",
            )
        duplicate_group_count = self._repository.count_canonical_duplicate_sessions()
        if not confirm:
            return {
                "confirmed": False,
                "duplicate_group_count": duplicate_group_count,
                "demoted_count": 0,
                "pause_tag": tag,
                "resume_outcome": "skipped",
            }
        if self._scheduling_service is None:
            raise RuntimeError(
                "lift_canonical_pointer_for_duplicate_sessions requires "
                "scheduling_service to be bound at "
                "session_ledger_service construction (to pause+resume "
                "the importer-poll cron around the repair)",
            )
        demoted_count = 0
        resume_outcome = "skipped"
        try:
            self._scheduling_service.clear_scheduled_actions_by_tag(tag=tag)
            demoted_count = (
                self._repository.lift_canonical_pointer_for_duplicate_sessions()
            )
            logger.info(
                "lift_canonical_pointer_for_duplicate_sessions: "
                "duplicate_group_count=%d demoted_count=%d",
                duplicate_group_count, demoted_count,
            )
        finally:
            resume_result = self.ensure_periodic_poll_schedule(
                cadence_minutes=int(cadence_minutes),
                tag=tag,
            )
            resume_outcome = str(resume_result.get("outcome", ""))
        return {
            "confirmed": True,
            "duplicate_group_count": duplicate_group_count,
            "demoted_count": demoted_count,
            "pause_tag": tag,
            "resume_outcome": resume_outcome,
        }

    def backfill_first_last_event_at_repair(
        self,
        confirm: bool = False,
        tag: str = "ledger:periodic_poll",
        cadence_minutes: int = 5,
    ) -> dict[str, Any]:
        """One-shot repair for ``__session`` rows with inverted event-at bounds.

        After the upsert path's ``LEAST/GREATEST`` fix landed, any rows already
        inverted from pre-fix ingestion need a one-time recompute from the
        canonical ``MIN(event_at)`` / ``MAX(event_at)`` over their
        ``__event`` rows. Wraps the repair in a try/finally pause-resume
        envelope around the importer-poll cron so a partial-loop exception
        still re-ensures ingest.
        """
        if not 1 <= int(cadence_minutes) <= 59:
            raise ValueError(
                f"cadence_minutes must be between 1 and 59 (got {cadence_minutes})",
            )
        inverted_count = self._repository.count_inverted_first_last_event_at_sessions()
        if not confirm:
            return {
                "confirmed": False,
                "inverted_count": inverted_count,
                "repaired_count": 0,
                "pause_tag": tag,
                "resume_outcome": "skipped",
            }
        if self._scheduling_service is None:
            raise RuntimeError(
                "backfill_first_last_event_at_repair requires "
                "scheduling_service to be bound at "
                "session_ledger_service construction (to pause+resume "
                "the importer-poll cron around the repair)",
            )
        repaired_count = 0
        resume_outcome = "skipped"
        try:
            self._scheduling_service.clear_scheduled_actions_by_tag(tag=tag)
            repaired_count = self._repository.repair_inverted_first_last_event_at()
            logger.info(
                "backfill_first_last_event_at_repair: "
                "inverted_count=%d repaired_count=%d",
                inverted_count, repaired_count,
            )
        finally:
            # MUST run even on exception so the importer-poll cron resumes.
            resume_result = self.ensure_periodic_poll_schedule(
                cadence_minutes=int(cadence_minutes),
                tag=tag,
            )
            resume_outcome = str(resume_result.get("outcome", ""))
        return {
            "confirmed": True,
            "inverted_count": inverted_count,
            "repaired_count": repaired_count,
            "pause_tag": tag,
            "resume_outcome": resume_outcome,
        }

    def backfill_summary_embedding_vector_ids(
        self,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """One-shot repair for pre-fix ``__summary.embedding_vector_id`` rows.

        Per commit ``4ea5eda81`` (2026-06-10): pre-fix ``_store_vector``
        returned the pgvector internal id; persist_summary wrote that into
        ``embedding_vector_id``. The ANN search join keys by
        ``embedding.external_id`` so the IN clause never matched and
        ``search_sessions`` silently returned ``[]``. This verb rewrites
        each stale pointer to the deterministic ``{session_id}:{chunk_index}``
        external_id, recomputed ledger-side from each ``__summary`` row's own
        columns (SQL-lockdown Slice 3b — no pgvector access, no cross-table
        join). Idempotent: post-fix pointers no longer carry the internal
        ``emb-`` prefix the repair targets.

        No cron pause needed: post-fix writes carry external_id-shaped
        pointers that don't carry the internal ``emb-`` prefix, so any
        concurrent M6 cron firing is invisible to the repair.
        """
        broken_count = (
            self._repository.count_summary_rows_with_pgvector_internal_id_pointer()
        )
        if not confirm:
            return {
                "confirmed": False,
                "updated_count": 0,
                "skipped_count": broken_count,
                "total_rows_now_correct": 0,
            }
        result = self._repository.repair_summary_embedding_vector_ids()
        logger.info(
            "backfill_summary_embedding_vector_ids: "
            "broken_count=%d updated_count=%d skipped_count=%d "
            "total_rows_now_correct=%d",
            broken_count,
            int(result.get("updated_count", 0)),
            int(result.get("skipped_count", 0)),
            int(result.get("total_rows_now_correct", 0)),
        )
        return {
            "confirmed": True,
            "updated_count": int(result.get("updated_count", 0)),
            "skipped_count": int(result.get("skipped_count", 0)),
            "total_rows_now_correct": int(result.get("total_rows_now_correct", 0)),
        }

    def backfill_orphan_running_batches_for_source(
        self,
        source_id: str,
        source_kind: str | None = None,
        confirm: bool = False,
        stale_threshold_seconds: int = 86400,
    ) -> dict[str, Any]:
        """One-shot repair for stale orphan ``__import_batch`` rows on one source.

        Cycle 4b D11. Operator-facing diagnostic verb that targets the
        orphan-running-batch class first surfaced by the chatgpt upload
        route + the Wave-4a crash-after-claim path: batches whose owner
        never terminated them. Any running batch older than
        ``stale_threshold_seconds`` (default 24 h) is marked failed with
        ``error_kind='orphan_repair'``.

        Per Codex impl note: this verb does NOT filter on
        ``polling_lease_token``, so token-owned batches whose owner
        crashed after claim are still reachable.

        Use ``confirm=True`` to actually rewrite rows. Default
        ``confirm=False`` returns a structured dry-run with the orphan
        counts so the operator can size the repair before committing.
        """
        if not confirm:
            return {
                "confirmed": False,
                "repaired_count": 0,
                "untouched_count": 0,
                "total_orphan_count_before": 0,
                "source_id": source_id,
            }
        result = self._repository.backfill_orphan_running_batches_for_source(
            source_id,
            source_kind=source_kind,
            stale_threshold_seconds=int(stale_threshold_seconds),
        )
        logger.info(
            "backfill_orphan_running_batches_for_source: "
            "source_id=%s source_kind=%s "
            "total_orphan_count_before=%d repaired_count=%d untouched_count=%d",
            source_id, source_kind,
            int(result.get("total_orphan_count_before", 0)),
            int(result.get("repaired_count", 0)),
            int(result.get("untouched_count", 0)),
        )
        return {
            "confirmed": True,
            "repaired_count": int(result.get("repaired_count", 0)),
            "untouched_count": int(result.get("untouched_count", 0)),
            "total_orphan_count_before": int(
                result.get("total_orphan_count_before", 0)
            ),
            "source_id": source_id,
        }

    def reset_source_cursor(
        self,
        source_id: str,
        confirm: bool = False,
    ) -> dict[str, Any]:
        """Hard-delete every active cursor row for one source.

        Cycle 4b D13. Operator recovery verb for the case where a
        source's discovery/event-read cursors have advanced past a point
        the operator wants to re-walk from (chatgpt-export ZIP re-walk
        being the immediate case). Hard-delete (not soft) per the operator's
        soft-delete-is-opt-out principle — a cursor reset has no recovery
        path, and ``write_cursor`` re-inserts a fresh row when none exists.

        Use ``confirm=True`` to actually delete. Default ``confirm=False``
        returns a structured dry-run with the active-cursor count so the
        operator can size the reset before committing.

        Idempotent: re-running on a source with no active cursors
        returns ``deleted_count=0``.

        PRECONDITION (``confirm=True``): every event must already carry a
        non-null ``external_id`` — the same exposure as
        :meth:`reset_ingest_state`. Re-walking a source with legacy
        null-``external_id`` events would DUPLICATE them (NULLs are DISTINCT
        in the ``(session_id, external_id)`` unique), so the verb REFUSES
        (raises ``ValueError``) while any remain — run
        ``backfill_event_external_ids`` to 0 nulls first. The dry-run reports
        ``null_external_id_count`` + ``precondition_met``. (The guard lives on
        this SERVICE verb — the direct operator path — NOT the repository
        method, so ``reset_ingest_state``'s loop, which calls the repository
        method and already guards globally upfront, is not double-guarded.)
        """
        null_external_id_count = self._repository.count_events_missing_external_id()
        if confirm and null_external_id_count > 0:
            raise ValueError(
                f"reset_source_cursor refused: {null_external_id_count} legacy "
                "event(s) still have a null external_id; run "
                "backfill_event_external_ids to 0 nulls first, else a re-walk "
                "would DUPLICATE them (NULLs are DISTINCT in the "
                "(session_id, external_id) unique, so a re-derived non-null id "
                "would not conflict with the null legacy row).",
            )
        if not confirm:
            active_count = self._repository.count_active_source_cursors(source_id)
            return {
                "confirmed": False,
                "deleted_count": 0,
                "source_id": source_id,
                "active_cursor_count_before": active_count,
                "null_external_id_count": null_external_id_count,
                "precondition_met": null_external_id_count == 0,
            }
        deleted_count = self._repository.reset_source_cursor(source_id)
        logger.info(
            "reset_source_cursor: source_id=%s deleted_count=%d",
            source_id, deleted_count,
        )
        return {
            "confirmed": True,
            "deleted_count": deleted_count,
            "source_id": source_id,
        }

    def list_events_by_source_window(
        self,
        source_kind: str | None = None,
        since: str | None = None,
        until: str | None = None,
        limit: int = 20,
        vendor: str | None = None,
    ) -> dict[str, Any]:
        """List events of a source kind / vendor in an optional event_at window.

        Cycle 4b D14b. Operator-facing diagnostic verb. Returns events
        ordered by ``event_at DESC`` (newest first) bounded by ``limit``
        (clamped to ``[1, 100]`` — SQL-lockdown Slice 7 narrowed the former
        200 to the ``query_ordered`` ≤100 cap). This verb has NO content-text
        predicate so callers can list a source kind's recent events without
        a search term.

        ``source_kind`` is the ``IngestSourceKind`` StrEnum value
        (codex_local / claude_code_local / chatgpt_export /
        agent_messaging / ...). ``since`` and ``until`` are ISO-8601
        timestamps (UTC). ``vendor``, when supplied, narrows to one
        ``SourceVendor`` (codex / claude_code / chatgpt /
        agent_messaging). SQL-lockdown Slice 7 (Architect-ruled denormalize):
        the read is a single-table ``query_ordered`` over ``__event`` using
        the ``session_vendor`` + ``source_kind`` columns denormalized onto
        each event — no JOIN.

        W5.E §5.5 G5 signature relaxation: all three filter args
        (``source_kind``, ``since``, ``until``) are now optional. At
        least one of ``source_kind`` / ``vendor`` MUST be supplied —
        unbounded scans across the entire ledger are not the verb's
        contract. When ``until`` is omitted, defaults to
        ``datetime.now(UTC)`` at the service-layer entry so vendor-only
        audit pulls return the most recent events without an explicit
        upper bound.
        """
        if source_kind is None and vendor is None:
            raise ValueError(
                "list_events_by_source_window: at least one of "
                "source_kind / vendor must be supplied (the verb's "
                "scope is intentional; unbounded ledger scans are not "
                "supported here)."
            )
        since_dt = _parse_iso(since) if since else None
        until_dt = _parse_iso(until) if until else datetime.now(UTC)
        rows = self._repository.list_events_by_source_window(
            source_kind=source_kind,
            since=since_dt,
            until=until_dt,
            limit=int(limit),
            vendor=vendor,
        )
        return {
            "events": [_public_event_envelope(row) for row in rows],
        }

    def backfill_event_source_denormalization(self) -> dict[str, Any]:
        """One-shot operator backfill of the Slice-7 ``__event`` denorm columns.

        Thin delegation to
        :meth:`SessionLedgerRepository.backfill_event_source_denormalization`.
        No pause-resume envelope (unlike the repair verbs): once the Slice-7
        ingest write-path landed, every NEW event is born with ``session_vendor``
        set, so the backfill's ``session_vendor IS NULL`` filter only ever
        touches pre-migration rows and never overlaps a live writer. Idempotent
        + fail-loud (see the repository docstring); returns
        ``{sessions_scanned, events_denormalized}``.
        """
        return self._repository.backfill_event_source_denormalization()

    def backfill_event_external_ids(self) -> dict[str, Any]:
        """One-shot operator backfill of ``__event.external_id`` on legacy rows.

        Thin delegation to
        :meth:`SessionLedgerRepository.backfill_event_external_ids`. Stamps the
        GAP-5 idempotent-ingest ``external_id`` on every pre-slice-1 null-
        ``external_id`` row, reproducing the live importer's derivation so
        historical re-ingest dedups. The service supplies ``fetch_blob_text`` from
        its blob adapter so OFFLOADED rows recompute the content-addressed key
        from the stored blob. Idempotent + fill-only (no ``confirm`` gate);
        skip-and-count on a live-window collision. Returns
        ``{sessions_scanned, events_stamped, collisions_skipped}``.
        """
        return self._repository.backfill_event_external_ids(
            fetch_blob_text=self._blob_adapter.fetch_event_text,
        )

    def backfill_session_source_kinds(self) -> dict[str, Any]:
        """One-shot operator backfill of the list_sessions source_kind junction.

        Thin delegation to
        :meth:`SessionLedgerRepository.backfill_session_source_kinds`. Populates
        the ``session_source_kind`` junction for pre-migration session groups so
        ``list_sessions(source_kind=K)`` finds historical sessions (the ingest
        attach-path maintains it going forward). Idempotent + additive (DO-NOTHING
        on the UNIQUE pair) so no pause-resume envelope is needed; fail-loud on a
        group with no canonical (run ``lift_canonical_pointer_for_duplicate_
        sessions`` first) or a missing source. Returns
        ``{sessions_scanned, junction_rows_written}``.
        """
        return self._repository.backfill_session_source_kinds()

    def lift_codex_stage1_summaries(
        self,
        confirm: bool = False,
        tag: str = "ledger:periodic_summarize",
        cadence_minutes: int = 10,
    ) -> dict[str, Any]:
        """G8-mitigated one-shot rewrite of ``__session.summary_text`` from Codex stage1.

        Reads ``~/.codex/memories_1.sqlite::stage1_outputs`` (filtered to
        ``selected_for_phase2 = 1``), joins to existing ``__session`` rows by
        ``external_session_id = thread_id``, and rewrites each match's
        ``summary_text`` to the stage1 ``rollout_summary`` with
        ``internal:auto_summarize:codex_stage1_seed`` attribution on the
        chunk push. PAUSES the M6 SUMMARIZE cron (NOT the poll cron) for the
        duration; re-ensures it in a try/finally.
        """
        # Local import to avoid coupling the session_ledger_service module to
        # the codex_memories vendor parser at import time.
        from ananta.llm.session_ledger.vendor import codex_memories  # noqa: PLC0415

        if not 1 <= int(cadence_minutes) <= 59:
            raise ValueError(
                f"cadence_minutes must be between 1 and 59 (got {cadence_minutes})",
            )
        db_path = Path(os.path.expanduser(codex_memories.DEFAULT_DB_PATH))
        if not db_path.exists():
            return {
                "confirmed": False,
                "stage1_row_count": 0,
                "candidate_count": 0,
                "lifted_count": 0,
                "pause_tag": tag,
                "resume_outcome": "skipped",
            }
        candidates = self._collect_codex_stage1_candidates(codex_memories, db_path)
        stage1_row_count = len(candidates)
        candidate_count = sum(1 for _, session_id in candidates if session_id is not None)
        if not confirm:
            return {
                "confirmed": False,
                "stage1_row_count": stage1_row_count,
                "candidate_count": candidate_count,
                "lifted_count": 0,
                "pause_tag": tag,
                "resume_outcome": "skipped",
            }
        if self._scheduling_service is None:
            raise RuntimeError(
                "lift_codex_stage1_summaries requires scheduling_service "
                "to be bound at session_ledger_service construction (to "
                "pause+resume the M6 summarize cron around the rewrite)",
            )
        lifted_count = 0
        resume_outcome = "skipped"
        try:
            self._scheduling_service.clear_scheduled_actions_by_tag(tag=tag)
            lifted_count = self._lift_stage1_candidates(candidates)
            logger.info(
                "lift_codex_stage1_summaries: stage1_row_count=%d "
                "candidate_count=%d lifted_count=%d",
                stage1_row_count, candidate_count, lifted_count,
            )
        finally:
            # MUST re-ensure the cron even on exception.
            resume_result = self.ensure_periodic_summarize_schedule(
                cadence_minutes=int(cadence_minutes),
                tag=tag,
            )
            resume_outcome = str(resume_result.get("outcome", ""))
        return {
            "confirmed": True,
            "stage1_row_count": stage1_row_count,
            "candidate_count": candidate_count,
            "lifted_count": lifted_count,
            "pause_tag": tag,
            "resume_outcome": resume_outcome,
        }

    def _collect_codex_stage1_candidates(
        self,
        codex_memories: Any,
        db_path: Path,
    ) -> list[tuple[Any, str | None]]:
        """Read stage1_outputs and resolve each thread_id to a __session row id.

        Returns ``(row, session_id_or_None)`` pairs preserving the read
        order. Rows without a matching __session land as ``(row, None)``
        and are skipped by the rewrite loop.
        """
        pairs: list[tuple[Any, str | None]] = []
        with codex_memories.open_readonly(db_path) as con:
            for row in codex_memories.iter_stage1_rows(con):
                session_id = self._repository.find_session_id_by_external_session_id(
                    row.thread_id,
                )
                pairs.append((row, session_id))
        return pairs

    def _lift_stage1_candidates(
        self,
        candidates: list[tuple[Any, str | None]],
    ) -> int:
        """Apply the per-row rewrite for each (row, session_id) pair with a match."""
        lifted_count = 0
        for row, session_id in candidates:
            if session_id is None:
                continue
            self._repository.overwrite_summary_text_for_codex_stage1(
                session_id=session_id,
                new_summary_text=row.rollout_summary,
            )
            try:
                self._summary_writer.push_summary_chunk(
                    session_id=session_id,
                    chunk_index=0,
                    summary_text=row.rollout_summary,
                    generated_by_client_id=(
                        "internal:auto_summarize:codex_stage1_seed"
                    ),
                )
            except Exception:
                logger.exception(
                    "lift_codex_stage1_summaries: chunk push failed for "
                    "session_id=%s; continuing with remaining candidates",
                    session_id,
                )
                continue
            lifted_count += 1
        return lifted_count

    def summarize_quiescent_sessions(
        self,
        quiescence_minutes: int = 10,
        batch_size: int = 50,
    ) -> dict[str, Any]:
        """Cron heartbeat: (re)start the singleton drain-until-empty summarizer.

        Does ZERO summarization on the calling (action-queue) thread — it only
        submits the whole drain (:meth:`_drain_all_quiescent`) to the single-slot
        :class:`BoundedSummaryExecutor` and returns in milliseconds (WI-0: an
        inline model call would park the queue). Returns ``{"drainer": "started"}``
        if this fire launched the drainer, or ``{"drainer": "already_running"}``
        if the slot is held (a drainer is already active → this fire is a no-op).
        The drain's per-session counts are logged when it completes (it runs
        asynchronously, so they cannot ride this return). ``batch_size`` is the
        drainer's per-iteration page (clamped by the read to 1..50), NOT a
        per-fire cap — the drainer loops until nothing eligible remains.
        """
        if self._inference_service is None:
            raise RuntimeError(
                "summarize_quiescent_sessions requires inference_service "
                "to be bound at session_ledger_service construction",
            )
        started = self._summary_executor.submit(
            lambda: self._drain_all_quiescent(
                quiescence_minutes=int(quiescence_minutes),
                batch_size=int(batch_size),
            ),
        )
        outcome = "started" if started else "already_running"
        logger.info(
            "auto-summarize drain %s (quiescence=%dm, batch=%d)",
            outcome, int(quiescence_minutes), int(batch_size),
        )
        return {
            "drainer": outcome,
            "quiescence_minutes": int(quiescence_minutes),
            "batch_size": int(batch_size),
        }

    def _drain_all_quiescent(
        self,
        *,
        quiescence_minutes: int,
        batch_size: int,
    ) -> dict[str, int]:
        """Drain every eligible quiescent session, in series, until none remain.

        Runs on the executor's daemon thread (off the action queue), so its
        synchronous inference calls cannot park the queue. Two properties:

        * LIVENESS (termination): ``attempted`` holds every id tried this drain;
          each pass processes only the fresh (not-yet-attempted) rows and stops
          when a page surfaces none. It grows ≥1 per pass over a finite universe,
          so the loop always terminates — even for a ``"skipped"`` return, which
          (in the transient inference-empty case) writes neither a ``__summary``
          row nor a sentinel and would otherwise re-list forever.
        * COVERAGE: progress requires processed rows to LEAVE eligibility.
          Summarized (→ ``__summary`` row) AND deterministic no-content skips
          (→ sentinel via ``_mark_unsummarizable``) both do, so no persistent
          no-content cluster can pin the DESC (newest-first) head page and strand
          older backlog behind it. Only a TRANSIENT inference-empty cluster
          filling the whole newest page can stall ONE drain
          (``_log_drain_stall``); it self-clears — a later cron-fired drain
          re-picks it once the backend recovers.
        """
        attempted: set[str] = set()
        counts = {"summarized": 0, "marked_trivial": 0, "skipped": 0}
        iterations = 0
        while True:
            candidates = self._repository.list_quiescent_sessions(
                quiescence_minutes=quiescence_minutes,
                limit=batch_size,
                trivial_sentinel=_AUTO_SUMMARIZE_TRIVIAL_SENTINEL,
            )
            if not candidates:
                break  # clean drain — nothing eligible remains
            fresh = [
                row for row in candidates if str(row.get("id")) not in attempted
            ]
            if not fresh:
                # Persistent-skip clog: candidates remain but all were already
                # tried this drain. The ``attempted`` set stops the spin; surface
                # it loudly so a real coverage gap is never a silent no-op.
                _log_drain_stall(candidates)
                break
            iterations += 1
            for row in fresh:
                session_id = str(row.get("id"))
                attempted.add(session_id)
                counts[_summarize_row(self._summarize_one_session, session_id, row)] += 1
        logger.info(
            "auto-summarize DRAIN complete: examined=%d summarized=%d "
            "marked_trivial=%d skipped=%d iterations=%d (quiescence=%dm, batch=%d)",
            len(attempted), counts["summarized"], counts["marked_trivial"],
            counts["skipped"], iterations, quiescence_minutes, batch_size,
        )
        return {
            "sessions_examined": len(attempted),
            "sessions_summarized": counts["summarized"],
            "sessions_marked_trivial": counts["marked_trivial"],
            "sessions_skipped": counts["skipped"],
            "drain_iterations": iterations,
        }

    def _summarize_one_session(
        self,
        session_id: str,
        *,
        existing_summary_text: str | None = None,
        source_kind: str | None = None,
    ) -> str:
        """One quiescent-session pass: seed → extraction → trivial → inference.

        Operator ruling 2026-06-01 (Bug 1 fix) drives the branch order:

        0. If the session already has ``summary_text`` set (operator-set
           ``custom_title`` per 2026-05-31 Architect §3, NOT the trivial
           sentinel), push that text through ``push_summary_chunk`` so it
           becomes searchable too — Architect's authoritative-title
           mapping was previously the silent reason claude_code sessions
           never got embedded by M6.
        1. Cheap SQL for an existing claude_code ``away_summary`` recap —
           if present, write it as the summary chunk (zero inference).
        2. DETERMINISTICALLY unsummarizable → write the trivial sentinel and
           return ``"marked_trivial"`` so the session LEAVES eligibility (never
           re-listed). Three cases: below the trivial floor (< 4 events OR no
           assistant turns), an empty timeline, and an empty transcript (all
           events blob-offloaded, content_text NULL). Marking is what stops a
           no-content cluster from pinning the DESC head page (Reviewer-C, §BLOCKER).
        3. Otherwise build a transcript and run the inference fallback
           SYNCHRONOUSLY (this method only ever runs on the drain's daemon
           thread, so the model call cannot park the action queue). A usable
           completion is pushed and returns ``"summarized"``; an empty/failed
           completion is a TRANSIENT skip → returns ``"skipped"`` WITHOUT a
           sentinel (summary_text stays NULL → deliberately re-picked on a later
           drain so a recovered backend self-resolves it).

        Returns one of
        ``{"summarized", "marked_trivial", "skipped"}``.
        The ``generated_by_client_id`` on each push attributes which
        branch produced the summary so post-hoc audits can split
        custom_title seeds, extracted recaps, and inference output.
        M19 added ``source_kind`` so branch 0 can pick a source-specific
        seed discriminator (e.g. Codex state_5 → ``codex_state_title_seed``)
        per v2 §5.5; sources not present in
        ``_SEED_DISCRIMINATOR_BY_SOURCE_KIND`` keep the pre-M19 default.
        """
        if (
            existing_summary_text
            and existing_summary_text != _AUTO_SUMMARIZE_TRIVIAL_SENTINEL
        ):
            seed_discriminator = _SEED_DISCRIMINATOR_BY_SOURCE_KIND.get(
                source_kind or "",
                _AUTO_SUMMARIZE_CLIENT_ID_CUSTOM_TITLE,
            )
            self._summary_writer.push_summary_chunk(
                session_id=session_id,
                chunk_index=0,
                summary_text=existing_summary_text,
                generated_by_client_id=seed_discriminator,
            )
            return "summarized"

        extracted = self._repository.find_latest_away_summary_for_session(
            session_id,
        )
        if extracted:
            self._summary_writer.push_summary_chunk(
                session_id=session_id,
                chunk_index=0,
                summary_text=extracted,
                generated_by_client_id=_AUTO_SUMMARIZE_CLIENT_ID_EXTRACTED,
            )
            return "summarized"

        events = self._repository.get_session_timeline(
            session_id=session_id,
            after_sequence=0,
            limit=_AUTO_SUMMARIZE_MAX_EVENTS,
        )
        if not events:
            # Deterministic skip: last_event_at set but an empty timeline.
            # Sentinel-mark so it LEAVES eligibility — else it pins the DESC
            # (newest-first) head page forever and strands older backlog behind
            # it (Reviewer-C MEDIUM, 2026-07-02).
            return _mark_unsummarizable(self._repository, session_id)

        if _is_trivial_session(events):
            return _mark_unsummarizable(self._repository, session_id)

        transcript = _assemble_transcript(events, _AUTO_SUMMARIZE_MAX_CHARS)
        if not transcript:
            # Deterministic skip: every event is blob-offloaded (content_text
            # NULL) so the transcript assembles empty. Sentinel-mark for the same
            # head-block reason. (A future un-blob summarizer would clear the
            # sentinel to re-enable these substantive sessions.)
            return _mark_unsummarizable(self._repository, session_id)
        # Inference runs SYNCHRONOUSLY here — this method only ever runs on the
        # drain's daemon thread (off the action queue), so a model call cannot
        # park the queue. NB the provider's ``timeout_seconds`` is now
        # load-bearing for SINGLETON liveness: a hung call holds the single drain
        # slot, so a future regression to None/huge would wedge every cron fire
        # (TMO-01 — currently a verified positive int, risk is future-only).
        summary_text = _request_inference_summary(
            inference_service=self._inference_service,
            transcript=transcript,
        )
        if not summary_text:
            # TRANSIENT skip (backend empty/down): deliberately NOT sentinel-marked
            # → re-picked on a later drain so it self-resolves. The attempted-set
            # prevents an intra-drain spin; a same-fire stall self-clears next fire.
            return "skipped"
        self._summary_writer.push_summary_chunk(
            session_id=session_id,
            chunk_index=0,
            summary_text=summary_text,
            generated_by_client_id=_AUTO_SUMMARIZE_CLIENT_ID_INFERRED,
        )
        return "summarized"

    def ensure_periodic_summarize_schedule(
        self,
        cadence_minutes: int = 10,
        tag: str = "ledger:periodic_summarize",
    ) -> dict[str, Any]:
        """Idempotently install a cron firing summarize_quiescent_sessions every N minutes."""
        cron_expression, cleared_count = _clear_and_prep_periodic_cron(
            self._scheduling_service, cadence_minutes=int(cadence_minutes), tag=tag,
        )
        # The create_cron_schedule call lives HERE (not in a shared helper) so its
        # literal process_key is AST-visible to the whole-tree C5.1 cron-target
        # gate, which grants the EDGE_SINK exemption only for a resolvable literal.
        create_result = self._scheduling_service.create_cron_schedule(
            cron_expression=cron_expression,
            actions=[{
                "process_key": (
                    "service_interface::session_ledger_service::summarize_quiescent_sessions"
                ),
                "arguments": {},
            }],
            label="Ledger periodic auto-summarize",
            tags=[tag],
            state={
                "flow_id": _LEDGER_PERIODIC_SUMMARIZE_FLOW_ID,
                "session_id": _LEDGER_PERIODIC_SUMMARIZE_SESSION_ID,
            },
        )
        return _periodic_cron_result(
            create_result, tag=tag, cadence_minutes=int(cadence_minutes),
            cleared_count=cleared_count,
        )

    def ensure_periodic_embed_schedule(
        self,
        cadence_minutes: int = 10,
        tag: str = "ledger:periodic_embed",
    ) -> dict[str, Any]:
        """Idempotently install a cron firing drain_event_embeddings every N minutes."""
        cron_expression, cleared_count = _clear_and_prep_periodic_cron(
            self._scheduling_service, cadence_minutes=int(cadence_minutes), tag=tag,
        )
        # Literal process_key inline for the same C5.1 gate-visibility reason as
        # ``ensure_periodic_summarize_schedule`` above.
        create_result = self._scheduling_service.create_cron_schedule(
            cron_expression=cron_expression,
            actions=[{
                "process_key": (
                    "service_interface::session_ledger_service::drain_event_embeddings"
                ),
                "arguments": {},
            }],
            label="Ledger periodic event-embedding drain",
            tags=[tag],
            state={
                "flow_id": _LEDGER_PERIODIC_EMBED_FLOW_ID,
                "session_id": _LEDGER_PERIODIC_EMBED_SESSION_ID,
            },
        )
        return _periodic_cron_result(
            create_result, tag=tag, cadence_minutes=int(cadence_minutes),
            cleared_count=cleared_count,
        )

    def drain_event_embeddings(self, page_size: int = 100) -> dict[str, Any]:
        """Cron heartbeat that (re)starts the singleton event-embedding drainer.

        Thin delegate; the full contract is on the ABC in ``interfaces/public.py``.
        """
        return _start_event_embedding_drain(
            self._embedding_executor,
            self._event_embedding_writer,
            max(1, min(int(page_size), 100)),
        )

    # ------------------------------------------------------------------
    # M5 — shipper bootstrap / pairing (spec §13)
    # ------------------------------------------------------------------

    def generate_ingest_setup(
        self,
        operating_system: str,
        sources: list[str],
        machine_id: str,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Create a pending shipper deployment row (spec §13 step 1).

        ``state`` carries the authenticated_principal; the caller's
        client_id becomes ``initiating_client_id`` for the §13.3
        ownership-binding check on ``approve_pairing``.

        The ``operating_system`` parameter is currently informational
        (operator-side install machinery materializes per-OS file
        artifacts; v1 ships the deployment registration only). The
        installer-package render path is M5 follow-on.
        """
        if state is None:
            raise PermissionError(
                "generate_ingest_setup requires server-injected state with "
                "authenticated_principal — refusing to act on caller-supplied identity"
            )
        principal = extract_authenticated_principal(state)
        # Validate sources up front so the deployment row carries only
        # IngestSourceKind values. Codex item 10: each is bound to the
        # deployment and later checked at ingest_blob / ingest_raw_chunk.
        validated: list[str] = []
        for source_kind in sources:
            kind = IngestSourceKind(source_kind)
            validated.append(kind.value)
        if not validated:
            raise ValueError("generate_ingest_setup requires non-empty sources")
        if not machine_id:
            raise ValueError("generate_ingest_setup requires non-empty machine_id")
        # operating_system kept for forward-compat with the installer
        # renderer (M5 follow-on); v1 doesn't materialize templates yet.
        _ = operating_system
        deployment_id = self._repository.insert_pending_deployment(
            machine_id=machine_id,
            initiating_client_id=principal.client_id,
            authorized_source_kinds=validated,
        )
        logger.info(
            "ledger pairing: deployment created deployment_id=%s "
            "initiating_client_id=%s sources=%s",
            deployment_id, principal.client_id, validated,
        )
        return {
            "deployment_id": deployment_id,
            "machine_id": machine_id,
            "authorized_source_kinds": validated,
        }

    def approve_pairing(
        self,
        deployment_id: str,
        user_code: str,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Ownership-bound transition pending→approved (spec §13.3)."""
        if state is None:
            raise PermissionError(
                "approve_pairing requires server-injected state with "
                "authenticated_principal — refusing to act on caller-supplied identity"
            )
        principal = extract_authenticated_principal(state)
        row = self._repository.get_deployment(deployment_id)
        if row is None:
            raise LedgerRepositoryError(
                f"no deployment with id {deployment_id!r}"
            )
        if row.get("pairing_status") != "pending":
            raise LedgerRepositoryError(
                f"deployment {deployment_id!r} is not in pending state "
                f"(got {row.get('pairing_status')!r})"
            )
        stored_user_code = row.get("user_code")
        if not isinstance(stored_user_code, str) or stored_user_code != user_code:
            raise PermissionError(
                f"approve_pairing: user_code mismatch for deployment {deployment_id!r}"
            )
        initiating = row.get("initiating_client_id")
        is_operator_eq = self._is_caller_operator_equivalent(principal.client_id)
        if principal.client_id != initiating and not is_operator_eq:
            raise PermissionError(
                f"approve_pairing forbidden: caller client_id {principal.client_id!r} "
                f"does not match deployment.initiating_client_id and is not "
                "operator_equivalent (§13.3)"
            )
        self._repository.transition_deployment_to_approved(
            deployment_id=deployment_id
        )
        logger.info(
            "ledger pairing: deployment approved deployment_id=%s by client_id=%s",
            deployment_id, principal.client_id,
        )
        return {"status": "approved", "deployment_id": deployment_id}

    def shipper_self_revoke(
        self,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Revoke the caller's own deployment (spec §14.1 pin 2).

        Target is server-derived from the authenticated principal's
        client_id — the handler accepts NO caller-supplied deployment_id.
        """
        if state is None:
            raise PermissionError(
                "shipper_self_revoke requires server-injected state with "
                "authenticated_principal — refusing to act on caller-supplied identity"
            )
        principal = extract_authenticated_principal(state)
        row = self._repository.get_deployment_by_oauth_client_id(
            principal.client_id
        )
        if row is None:
            raise PermissionError(
                f"shipper_self_revoke: no paired deployment for caller "
                f"client_id {principal.client_id!r}"
            )
        deployment_id = str(row["id"])
        self._repository.transition_deployment_to_revoked(
            deployment_id=deployment_id
        )
        logger.info(
            "ledger pairing: deployment revoked deployment_id=%s by self "
            "(client_id=%s)",
            deployment_id, principal.client_id,
        )
        return {"status": "revoked", "deployment_id": deployment_id}

    # ------------------------------------------------------------------
    # M6 — semantic search / summary intake (spec §17.6)
    # ------------------------------------------------------------------

    def push_session_summary_chunk(
        self,
        session_id: str,
        chunk_index: int,
        summary_text: str,
        state: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Embed + store-vector + insert a summary chunk row.

        ``state`` carries the authenticated_principal; the caller's
        client_id is persisted as ``generated_by_client_id`` (spec §8.10
        NOT NULL).
        """
        if state is None:
            raise PermissionError(
                "push_session_summary_chunk requires server-injected state with "
                "authenticated_principal — refusing to act on caller-supplied identity",
            )
        principal = extract_authenticated_principal(state)
        try:
            result = self._summary_writer.push_summary_chunk(
                session_id=session_id,
                chunk_index=chunk_index,
                summary_text=summary_text,
                generated_by_client_id=principal.client_id,
            )
        except SummaryServicesUnavailableError as exc:
            raise RuntimeError(
                "push_session_summary_chunk: M6 services unavailable in this profile",
            ) from exc
        return {
            "status": "completed",
            "summary_id": result["summary_id"],
            "embedding_vector_id": result["embedding_vector_id"],
            "chunk_index": result["chunk_index"],
        }

    def search_sessions(
        self,
        query: str,
        limit: int = 10,
        vendor: SourceVendor | str | None = None,
        source_kind: IngestSourceKind | str | None = None,
        since: datetime | str | None = None,
    ) -> dict[str, Any]:
        """Top-k summaries joined to sessions; re-scans content on the way out (M17 §2.2).

        M17 adds vendor / source_kind / since filters. The ANN search
        returns candidate envelopes; this impl post-filters them against
        the joined session row's vendor + source_kind + last_event_at.
        The post-filter passes through M18 §3.4's COALESCE seam (only
        canonical rows have summaries, so the result set is dedup'd by
        construction).
        """
        bounded_limit = max(1, min(int(limit), 50))
        vendor_typed = _coerce_enum(vendor, SourceVendor)
        source_kind_typed = _coerce_enum(source_kind, IngestSourceKind)
        since_typed = _coerce_datetime(since)
        try:
            envelopes = self._summary_writer.search(
                query=query, limit=bounded_limit,
            )
        except SummaryServicesUnavailableError as exc:
            raise RuntimeError(
                "search_sessions: M6 services unavailable in this profile",
            ) from exc
        if vendor_typed is not None or source_kind_typed is not None or since_typed is not None:
            envelopes = _filter_envelopes(
                envelopes,
                vendor=vendor_typed,
                source_kind=source_kind_typed,
                since=since_typed,
            )
        return {
            "results": [
                {
                    "session_id": env.session_id,
                    "chunk_index": env.chunk_index,
                    "summary_text": env.summary_text,
                    "score": env.score,
                    "session": env.session,
                }
                for env in envelopes
            ],
        }

    def search_event_content(
        self,
        query: str,
        limit: int = 10,
    ) -> dict[str, Any]:
        """ANN search over event-content embeddings (LED-01).

        Chunk-granular search over the raw USER/ASSISTANT message text in
        the ``session_ledger_event`` vector namespace — the content-level
        complement to ``search_sessions``' summary-level search. Only
        already-embedded events are reachable (the producer below fills the
        namespace). Results reuse the ``list_events_by_source_window``
        public event envelope plus ``chunk_index`` + ``score``.
        """
        bounded_limit = max(1, min(int(limit), 50))
        try:
            hits = self._event_embedding_writer.search(
                query=query, limit=bounded_limit,
            )
        except EventEmbeddingServicesUnavailableError as exc:
            raise RuntimeError(
                "search_event_content: embedding/vector services "
                "unavailable in this profile",
            ) from exc
        return {
            "results": [
                {
                    **_public_event_envelope(hit),
                    "chunk_index": hit.get("chunk_index"),
                    "score": hit.get("score"),
                }
                for hit in hits
            ],
        }

    def embed_missing_event_content(
        self,
        batch_limit: int = 25,
    ) -> dict[str, Any]:
        """Embed a bounded batch of not-yet-embedded events (LED-01 producer).

        Thin delegation to
        :meth:`EventEmbeddingWriter.embed_missing_events` with the verb's
        ``[1, 200]`` clamp. Synchronous within this call (bounded batch +
        ``is_long_running`` on the declaration); the caller loops while the
        returned ``exhausted`` is false.
        """
        bounded_batch_limit = max(1, min(int(batch_limit), 200))
        try:
            return self._event_embedding_writer.embed_missing_events(
                batch_limit=bounded_batch_limit,
            )
        except EventEmbeddingServicesUnavailableError as exc:
            raise RuntimeError(
                "embed_missing_event_content: embedding/vector services "
                "unavailable in this profile",
            ) from exc

    def event_embedding_coverage(self) -> dict[str, Any]:
        """Read-only LED-01 event-embedding coverage (frontier + chunk count).

        Thin delegation to :meth:`EventEmbeddingWriter.coverage`. Works even
        without embedding/vector bindings — an absent vector_service yields a
        0 ``embedded_chunk_count`` rather than failing, so the frontier read
        (cursor vs newest in-scope arrival) is always available.
        """
        return self._event_embedding_writer.coverage()

    def _is_caller_operator_equivalent(self, client_id: str) -> bool:
        """Dispatch through the bridge-wired callback.

        Production: the bridge plugin sets the callback via
        :meth:`set_operator_equivalent_check` once the vault registry
        is available. Until set (M1 default), returns False — only
        direct ownership-binding (caller.client_id ==
        initiating_client_id) admits an approve_pairing call.
        """
        if self._operator_equivalent_check is None:
            return False
        return bool(self._operator_equivalent_check(client_id))

    def set_operator_equivalent_check(
        self, callback: Callable[[str], bool],
    ) -> None:
        """Production wiring hook for the vault is_operator_equivalent lookup."""
        self._operator_equivalent_check = callback

    # ------------------------------------------------------------------
    # M4 — ChatGPT export (kept here from M4)
    # ------------------------------------------------------------------

    def register_chatgpt_export_source(
        self, *, blob_id: str, account_label: str | None
    ) -> dict[str, str]:
        """Register an uploaded export blob and start its running batch.

        Spec §10.10.1 step 5. Idempotent on the blob id (A1): routes through
        the trusted internal registration seam (so a re-upload of the same
        export reuses the existing ``session_ledger__source`` row, not a fresh
        one) and reuses the open ROUTE batch instead of orphaning a new running
        batch per call. Returns ``{"source_id": str, "batch_id": str}``.
        """
        registered = self._register_source_internal(
            source_kind=IngestSourceKind.CHATGPT_EXPORT.value,
            root_uri=blob_id,
            account_label=account_label,
            config_json=None,
        )
        source_id = registered["source_id"]
        # Per v8 D14.D: route batches carry polling_lease_token=NULL because the
        # HTTP handler holds no source lease. They are adoptable by any
        # importer's adopt_route_batch_for_source call. Idempotent (A1): reuse
        # the running route batch rather than orphan a fresh one per upload.
        batch_id = self._repository.ensure_open_route_batch_for_source(source_id)
        return {"source_id": source_id, "batch_id": batch_id}

    def register_claude_ai_export_source(
        self, *, blob_id: str, account_label: str | None
    ) -> dict[str, str]:
        """Register an uploaded Claude.ai export blob (M9; A2 asymmetric).

        Spec §10.10.1 step 5. ``claude_ai_export`` is a **PUSHED** source, so —
        unlike the PULLING ``register_chatgpt_export_source`` — this verb opens
        **no** batch: the push (``ingest_raw_chunk`` → ``dispatch_pushed``) owns
        and finalizes its own batch. Routes through the trusted internal
        registration seam (idempotent on the blob id) and returns
        ``{"source_id": str}`` only. The plugin's ``_trigger_import`` then binds
        the push to this ``source_id`` and surfaces the real push ``batch_id``.
        """
        registered = self._register_source_internal(
            source_kind=IngestSourceKind.CLAUDE_AI_EXPORT.value,
            root_uri=blob_id,
            account_label=account_label,
            config_json=None,
        )
        return {"source_id": registered["source_id"]}

    def backfill_export_blob_identity(
        self, confirm: bool = False, *, call_context: CallContext | None = None,
    ) -> dict[str, Any]:
        """Converge export blobs onto content-digest identity (A3; operator-only).

        ``confirm=False`` previews counts and mutates nothing. ``confirm=True``
        tags every referenced export blob with its durable ``kind`` (Phase 0),
        keys each blob by content digest / repoints each source onto the
        content-canonical blob (Phase 1), then deletes the now-unreferenced
        export blobs (Phase 2). Operator-only — it mutates blob metadata,
        repoints ``__source`` rows, and deletes blobs. **Forward-only**: once a
        confirmed run mutates, recovery is re-run-to-reconverge, not revert.

        **Sequencing guardrail (do NOT run standalone).** The Phase-1 repoint
        deliberately makes a duplicate source's ``root_uri`` collide with its
        content-twin on ``(source_kind, root_uri)`` — that collision is the
        INTENDED precondition for source-row dedup, NOT a finished state. A
        confirmed run MUST be followed by the source-row collapse step
        (``repair_duplicate_sources`` / the Deploy-3 ``(source_kind, root_uri)``
        unique index + ON-CONFLICT). In the pre-collapse window the two sources
        walk the same blob, but the M18 ``(vendor, external_session_id)``
        canonical index prevents any permanent session duplication.
        """
        assert_operator_principal(call_context, "backfill_export_blob_identity")
        backfill = ExportBlobIdentityBackfill(
            blob_storage_service=self._blob_adapter.blob_storage_service,
            repository=self._repository,
        )
        return backfill.run(confirm=confirm)

def _coerce_datetime(value: datetime | str | None) -> datetime | None:
    """M17 §2.2 / W5.E cycle-4 C3: coerce str/datetime payload at the entry.

    Routes both typed datetimes and string payloads through the shared
    ``_normalize_event_at`` helper so the typed-naïve path is also
    rejected at service entry (pre-W5.E the typed-datetime branch
    returned the value unchecked, leaving naïve ``since=`` callers
    silently accepted). First-party Python callers that pass typed
    aware datetimes get the same UTC-normalization the str-path does.
    """
    if value is None:
        return None
    return _normalize_event_at(value)


def _coerce_enum[E: StrEnum](value: E | str | None, enum_cls: type[E]) -> E | None:
    """M17 §2.2: coerce str payload to StrEnum at the entry; typed input passes through."""
    if value is None:
        return None
    if isinstance(value, enum_cls):
        return value
    return enum_cls(value)


def _envelope_session_row(env: SearchResultEnvelope) -> dict[str, object]:
    """Extract the envelope's denormalized session row dict; empty if absent."""
    return env.session if isinstance(env.session, dict) else {}


def _envelope_matches_filters(
    env: SearchResultEnvelope,
    *,
    vendor: SourceVendor | None,
    source_kind: IngestSourceKind | None,
    since: datetime | None,
) -> bool:
    """Predicate per envelope; conservative on missing fields when filter is set.

    W5.E G4 cross-pollination (Codex cycle-3 C6 PROMOTED): the ``since``
    branch routes ``last_event_at`` through the shared
    ``_normalize_event_at`` helper. Pre-W5.E this branch checked
    ``isinstance(row_last, datetime)`` — but ``PostgresProvider`` returns
    ISO-string rows (per the G4 root cause), so the check always failed
    and the envelope was DROPPED. The false-negative DROP meant
    ``search_sessions(since=...)`` returned 0 results when matching
    sessions existed. The strict helper closes the same seam at the
    filter path that the envelope seam closes.
    """
    row = _envelope_session_row(env)
    if vendor is not None and row.get("vendor") != vendor.value:
        return False
    if source_kind is not None and row.get("source_kind") != source_kind.value:
        return False
    if since is not None:
        row_last_raw = row.get("last_event_at")
        if row_last_raw is None:
            return False
        try:
            row_last = _normalize_event_at(row_last_raw)
        except ValueError:
            return False
        if row_last < since:
            return False
    return True


def _filter_envelopes(
    envelopes: list[SearchResultEnvelope],
    *,
    vendor: SourceVendor | None,
    source_kind: IngestSourceKind | None,
    since: datetime | None,
) -> list[SearchResultEnvelope]:
    """M17 §2.2: post-filter ANN envelopes against vendor / source_kind / since.

    Operates on envelope dicts produced by SummaryWriter.search; each
    envelope's ``session`` field carries the denormalized session row
    columns (vendor, source_id, last_event_at, ...). When a filter is
    requested but the envelope lacks the relevant field, the envelope is
    dropped (conservative — prevents leaking unfiltered results).
    """
    return [
        env
        for env in envelopes
        if _envelope_matches_filters(
            env, vendor=vendor, source_kind=source_kind, since=since
        )
    ]


def _build_semantic_writers(
    *,
    repository: SessionLedgerRepository,
    embedding_service: EmbeddingServiceInterface | None,
    vector_service: VectorServiceInterface | None,
) -> tuple[SummaryWriter, EventEmbeddingWriter]:
    """Assemble the two semantic-index policy writers over one collaborator set.

    M6's summary writer and LED-01's event-content writer wrap the same
    repository + optional embedding/vector bindings and share the same
    fail-closed posture when those bindings are absent (cloud profiles keep
    the M1-M5 surface; embedding surfaces raise their
    ``*ServicesUnavailableError`` on use).
    """
    summary_writer = SummaryWriter(
        repository=repository,
        embedding_service=embedding_service,
        vector_service=vector_service,
    )
    event_embedding_writer = EventEmbeddingWriter(
        repository=repository,
        embedding_service=embedding_service,
        vector_service=vector_service,
    )
    return summary_writer, event_embedding_writer


def _public_event_envelope(row: dict[str, Any]) -> dict[str, Any]:
    """Map a projected ``__event`` row to the public per-event envelope.

    Shared by ``list_events_by_source_window`` and ``search_event_content``
    so the two event-listing surfaces present one shape: the repository
    projection's ``session_vendor`` becomes the public ``vendor`` field and
    a ``datetime`` ``event_at`` is ISO-ified (rows read through the real
    provider arrive as ISO strings already; offline shims may hand back
    ``datetime`` values directly).
    """
    return {
        "event_id": row.get("event_id"),
        "session_id": row.get("session_id"),
        "sequence": row.get("sequence"),
        "event_at": (
            row["event_at"].isoformat()
            if isinstance(row.get("event_at"), datetime)
            else row.get("event_at")
        ),
        "role": row.get("role"),
        "content_text": row.get("content_text"),
        "vendor": row.get("session_vendor"),
        "source_kind": row.get("source_kind"),
    }


def _parse_iso(s: str) -> datetime:
    """Strict ISO-8601 parse. Always returns a UTC-aware ``datetime``.

    Bare ``Z`` suffix is normalized to ``+00:00`` for ``fromisoformat``.

    Naïve (timezone-unaware) inputs are REJECTED with ``ValueError`` per
    ``[[fast-fail-development-strategy]]``. Pre-2026-06-11 the helper
    silently coerced naïve datetimes to whatever local-or-no zone
    ``fromisoformat`` returned, which propagated through the
    ``list_sessions`` filter chain as a timezone offset slip: a
    UTC-aware filter value compared against a possibly-naïve persisted
    value let pre-cutoff sessions through (operator empirical evidence
    2026-06-11 PT: filter ``2025-11-01T00:00:00Z`` returned a session
    whose ``first_event_at`` is ``2025-10-31T18:31:59``).

    Aware inputs in any zone are accepted and normalized to UTC so all
    downstream SQL comparisons run on a single time axis.
    """
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    parsed = datetime.fromisoformat(s)
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        raise ValueError(
            f"_parse_iso: naïve datetime not accepted; require an explicit "
            f"timezone offset (e.g. 'Z' or '+00:00'). Got: {s!r}",
        )
    return parsed.astimezone(UTC)


def _parse_iso_with_naive_as_utc(s: str) -> datetime:
    """ISO-8601 parse for repository-row datetime ISO strings.

    The ledger ``timestamp without time zone`` columns surface as naïve
    ISO strings (no ``Z`` / ``+00:00``) through
    ``PostgresProvider._serialize_for_json``. The platform stores those
    naïve values as semantic UTC (DDL default
    ``(now() AT TIME ZONE 'UTC'::text)``), so the row-path parser
    attaches ``tzinfo=UTC`` rather than raising. Aware inputs are
    routed through ``_parse_iso`` so the strict-aware contract holds
    for values that DO carry an offset.

    Used by ``_normalize_event_at`` on the str-input path (live row
    dicts). First-party Python callers that supply typed datetimes
    still go through the strict-aware contract on the typed path
    inside ``_normalize_event_at`` itself.
    """
    if s.endswith("Z") or "+" in s or s[10:].count("-") > 0:
        return _parse_iso(s)
    parsed = datetime.fromisoformat(s)
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _normalize_event_at(value: object) -> datetime:
    """W5.E §5.3 shared strict-parse-at-seam helper for event_at /
    last_event_at across both repository-row and typed-API surfaces.

    Single source of truth for the rejection contract: both the
    ``_build_event_envelope`` envelope seam and the
    ``_envelope_matches_filters`` post-filter use this helper so the
    string-vs-typed-datetime asymmetry that fired G4 + C6 in production
    cannot recur.

    Two parse paths with different strictness, per the platform's actual
    storage convention (W5.E live-DB calibration 2026-06-14):

    * **Str path** — values arrive as naïve ISO strings from
      ``PostgresProvider._serialize_for_json``, because the ledger
      timestamp columns are declared ``timestamp without time zone``
      with ``DEFAULT (now() AT TIME ZONE 'UTC'::text)``. The platform
      convention is "stored naïve, semantically UTC." The str path
      parses via ``datetime.fromisoformat`` and attaches ``tzinfo=UTC``
      when the parsed value is naïve; aware ISO strings (``Z`` /
      ``+00:00``) flow through ``_parse_iso``.
    * **Typed-datetime path** — Codex cycle-4 C3 full-strictness:
      reject when ``tzinfo is None`` OR ``tzinfo.utcoffset(value)``
      returns None. First-party Python callers MUST pass aware
      datetimes; the typed path catches naïve-since-param regressions
      at service entry.

    G4 root cause (confirmed via reading
    ``plugins/postgres_state_management_plugin/src/.../provider.py:870-873``):
    ``PostgresProvider.execute_query`` calls ``_serialize_for_json``
    over every fetched row, which converts every ``datetime|date`` to
    its ISO ``isoformat()`` string. By the time the row dict reaches
    the envelope seam, ``event_at`` is a ``str``, not a ``datetime``.
    Pre-W5.E ``_build_event_envelope`` checked ``isinstance(..., datetime)``,
    failed silently, and fell back to ``datetime.now(UTC)`` — every
    result row carried wrong timestamps matching wall-clock-at-query,
    not the real event time. The helper closes that seam without
    breaking on the platform's naïve-stored-UTC convention.
    """
    if isinstance(value, str):
        return _parse_iso_with_naive_as_utc(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                f"_normalize_event_at: naïve or pseudo-aware typed "
                f"datetime not accepted; require explicit timezone "
                f"offset. Got: {value!r}",
            )
        return value.astimezone(UTC)
    raise ValueError(
        f"_normalize_event_at: expected str or datetime, got "
        f"{type(value).__name__}={value!r}",
    )


def _summarize_row(
    summarize_one: Callable[..., str],
    session_id: str,
    row: dict[str, object],
) -> str:
    """Summarize one drain row; map any per-session error to ``"skipped"``.

    A module-level free function (not a method) so it stays off
    ``SessionLedgerService``'s god-class LOC budget. It owns the per-session
    ``try/except`` so the drain's counting never sees an exception leak past
    here — one bad session cannot kill the drain.
    """
    existing_summary = row.get("summary_text")
    existing_summary_text = (
        existing_summary if isinstance(existing_summary, str) else None
    )
    row_source_kind = row.get("source_kind")
    source_kind = row_source_kind if isinstance(row_source_kind, str) else None
    try:
        return summarize_one(
            session_id,
            existing_summary_text=existing_summary_text,
            source_kind=source_kind,
        )
    except Exception:  # noqa: BLE001 — one bad session must not kill the drain
        logger.exception(
            "auto-summarize failed for session_id=%s; continuing drain",
            session_id,
        )
        return "skipped"


def _log_drain_stall(candidates: list[dict[str, object]]) -> None:
    """Warn that the drain stopped with un-summarizable candidates still queued.

    Fires when a page returns only sessions already attempted this drain — a
    persistent-skip clog (inference backend down, or a no-events / empty-
    transcript anomaly). Kept visible so a coverage gap is never a silent no-op.
    """
    logger.warning(
        "auto-summarize drain STALLED: %d candidate(s) remain but all were "
        "already attempted this drain (persistent skip / clog); ids=%s. "
        "Retried on the next cron-fired drain.",
        len(candidates),
        [str(row.get("id")) for row in candidates[:20]],
    )


def _mark_unsummarizable(repository: Any, session_id: str) -> str:
    """Sentinel-mark a DETERMINISTICALLY-unsummarizable session so it leaves the
    quiescent-eligibility set and returns ``"marked_trivial"``.

    Used for the genuinely-trivial floor AND the two deterministic no-content
    skips (empty timeline; all-blob-offloaded / empty transcript). Marking them
    the way the trivial floor does is what stops a no-content cluster from
    pinning the DESC (newest-first) head page and permanently stranding older
    backlog behind it (Reviewer-C MEDIUM, 2026-07-02). TRANSIENT skips
    (inference-returned-empty, caught exceptions) are deliberately NOT marked —
    they must stay eligible so a later drain re-picks and self-resolves them.
    """
    repository.mark_session_summary_text(
        session_id=session_id, summary_text=_AUTO_SUMMARIZE_TRIVIAL_SENTINEL,
    )
    return "marked_trivial"


def _assemble_transcript(events: list[dict[str, Any]], max_chars: int) -> str:
    """Render an event-row list into a bounded transcript for summarization.

    Output is intentionally simple — newline-separated ``role: text`` lines —
    so the summarizer prompt stays small and deterministic. Quarantined or
    blobbed events have no inline content; their column is null and we skip
    them rather than emitting empty lines. Stops when ``max_chars`` would be
    exceeded; the cap defends against a runaway-long session blowing out the
    inference prompt.
    """
    pieces: list[str] = []
    used = 0
    for event in events:
        role = str(event.get("role") or "")
        if role not in _CONVERSATION_ROLES:
            continue  # only user/assistant — skip tool, system, null-role noise
        text = event.get("content_text")
        if not isinstance(text, str) or not text:
            continue
        line = f"{role}: {text}\n"
        if used + len(line) > max_chars:
            break
        pieces.append(line)
        used += len(line)
    return "".join(pieces).strip()


def _is_trivial_session(events: list[dict[str, object]]) -> bool:
    """Trivial-session predicate (operator ruling 2026-06-01 D8; conversation-only 2026-06-30).

    Counts ONLY ``user``/``assistant`` conversation events — tool, system, and
    null-role noise is ignored (operator 2026-06-30: "I only care about user and
    assistant messages"). A session is trivial when **either** it has fewer than
    ``_AUTO_SUMMARIZE_TRIVIAL_MIN_EVENTS`` (4) conversation events **or** it
    contains zero ``role='assistant'`` events. Both make the transcript too thin
    to summarize usefully — the cron writes the sentinel and moves on rather than
    burning inference tokens on a noise-only session.
    """
    conversation = [
        event
        for event in events
        if str(event.get("role") or "") in _CONVERSATION_ROLES
    ]
    if len(conversation) < _AUTO_SUMMARIZE_TRIVIAL_MIN_EVENTS:
        return True
    assistant_value = MessageRole.ASSISTANT.value
    return not any(
        str(event.get("role") or "") == assistant_value for event in conversation
    )


def _request_inference_summary(*, inference_service: Any, transcript: str) -> str:
    """Call ``inference_service.generate_completion`` for a 2-4 sentence summary.

    Returns an empty string when the inference call fails or returns nothing
    usable — the caller treats that as a skip, not a fatal error. The cron
    re-tries on the next firing.
    """
    result = _call_inference_chat(inference_service, transcript)
    return _extract_summary_text(result)


def _call_inference_chat(inference_service: Any, transcript: str) -> Any:
    request = InferenceRequest(
        [
            {"role": "system", "content": _AUTO_SUMMARIZE_PROMPT},
            {
                "role": "user",
                "content": _AUTO_SUMMARIZE_USER_TEMPLATE.format(transcript=transcript),
            },
        ],
        temperature=_AUTO_SUMMARIZE_INFERENCE_TEMPERATURE,
        max_tokens=_AUTO_SUMMARIZE_INFERENCE_MAX_TOKENS,
        # Plain-prose summary — no action JSON, no response_format schema.
        use_structured_output=False,
        context_metadata={"purpose": "session_ledger_auto_summarize"},
    )
    try:
        return inference_service.generate_completion(request)
    except Exception:  # noqa: BLE001 — defensive boundary; logged here, treated as skip upstream
        logger.exception(
            "inference_service.generate_completion raised during auto-summarize",
        )
        return None


def _extract_summary_text(result: Any) -> str:
    """Pull the summary string out of the ActionResult envelope.

    Tracks the canonical extraction at ``inference_transaction._invoke_and_extract``:
    ``result.data.result.completion`` is the structured-output happy path.
    Falls back to the looser ``data.text`` / ``data.message.content`` shapes
    some providers return so the auto-summarize path stays vendor-tolerant.
    """
    data = _envelope_payload(result)
    if data is None:
        return ""
    for extractor in _SUMMARY_TEXT_EXTRACTORS:
        text = extractor(data)
        if text:
            return text
    return ""


def _envelope_payload(result: Any) -> dict[str, Any] | None:
    """Unwrap an ActionResult-shaped envelope to its inner data dict, or ``None``."""
    if not isinstance(result, dict):
        return None
    if result.get("error"):
        return None
    if result.get("action_status") not in (None, "completed"):
        return None
    data = result.get("data") if isinstance(result.get("data"), dict) else result
    return data if isinstance(data, dict) else None


def _strip_or_empty(value: Any) -> str:
    """Return ``value.strip()`` for non-empty strings; ``""`` otherwise."""
    if isinstance(value, str):
        stripped = value.strip()
        if stripped:
            return stripped
    return ""


def _extract_completion_field(data: dict[str, Any]) -> str:
    """Canonical structured-output shape: ``data.result.completion``."""
    inner = data.get("result")
    if not isinstance(inner, dict):
        return ""
    return _strip_or_empty(inner.get("completion"))


def _extract_flat_completion_field(data: dict[str, Any]) -> str:
    """Flat non-structured-output shape: ``data.completion``.

    ``_call_inference_chat`` requests ``use_structured_output=False``, so
    providers return the completion at the top level of ``data`` rather than
    nested under ``data.result``. ``mock_inference_plugin`` and the live
    Qwen path both follow this shape for non-structured calls.
    """
    return _strip_or_empty(data.get("completion"))


def _extract_text_field(data: dict[str, Any]) -> str:
    """Loose provider shape: ``data.text``."""
    return _strip_or_empty(data.get("text"))


def _extract_message_content(data: dict[str, Any]) -> str:
    """Chat-style provider shape: ``data.message.content``."""
    message = data.get("message")
    if not isinstance(message, dict):
        return ""
    return _strip_or_empty(message.get("content"))


_SUMMARY_TEXT_EXTRACTORS: tuple[
    Callable[[dict[str, Any]], str], ...
] = (
    _extract_completion_field,
    _extract_flat_completion_field,
    _extract_text_field,
    _extract_message_content,
)


def _extract_schedule_id(envelope: Any) -> str:
    """Pull schedule_id from either an action-envelope or a raw data dict.

    The scheduling plugin returns ``{"data": {"schedule_id": "..."}, ...}``;
    the service-interface direct-dispatch surface may return the raw inner
    dict. Both shapes resolve cleanly to the same string here.
    """
    if not isinstance(envelope, dict):
        return ""
    if "schedule_id" in envelope:
        return str(envelope["schedule_id"])
    data = envelope.get("data")
    if isinstance(data, dict) and "schedule_id" in data:
        return str(data["schedule_id"])
    return ""


def _clear_and_prep_periodic_cron(
    scheduling_service: Any, *, cadence_minutes: int, tag: str,
) -> tuple[str, int]:
    """Shared PRE step for the ledger's periodic EDGE_SINK cron installers.

    Fails fast on missing scheduling / out-of-range cadence, builds the cron
    expression, and clears any existing schedules for ``tag``; returns
    ``(cron_expression, cleared_count)``. The ``create_cron_schedule`` call
    itself deliberately stays in each installer so its LITERAL ``process_key``
    is AST-visible to the whole-tree C5.1 cron-target gate (which grants the
    EDGE_SINK exemption only for a resolvable literal — a variable process_key
    reads as an un-exempt EDGE target).
    """
    if scheduling_service is None:
        raise RuntimeError(
            "ensure periodic schedule requires scheduling_service to be bound "
            "at session_ledger_service construction",
        )
    if not 1 <= int(cadence_minutes) <= 59:
        raise ValueError(
            f"cadence_minutes must be between 1 and 59 (got {cadence_minutes})",
        )
    clear_result = scheduling_service.clear_scheduled_actions_by_tag(tag=tag)
    cleared_count = (
        int((clear_result or {}).get("data", {}).get("cleared_count", 0))
        if isinstance(clear_result, dict)
        else 0
    )
    return f"*/{int(cadence_minutes)} * * * *", cleared_count


def _periodic_cron_result(
    create_result: Any, *, tag: str, cadence_minutes: int, cleared_count: int,
) -> dict[str, Any]:
    """Shared POST step: extract schedule_id, report created-vs-normalized, log."""
    schedule_id = _extract_schedule_id(create_result)
    outcome = "normalized" if cleared_count > 0 else "created"
    logger.info(
        "session_ledger periodic schedule %s: schedule_id=%s tag=%s cadence=%dm",
        outcome, schedule_id, tag, int(cadence_minutes),
    )
    return {
        "outcome": outcome,
        "schedule_id": schedule_id,
        "tag": tag,
        "cadence_minutes": int(cadence_minutes),
        "cleared_count": cleared_count,
    }


def _build_drain_executors(
    summary_executor: SummaryExecutor | None,
) -> tuple[SummaryExecutor, SummaryExecutor, SummaryExecutor]:
    """Three independent single-slot drain guards (summarize + event-embedding + importer poll).

    Each is a singleton across overlapping cron fires (a second ``submit`` no-ops); the three are
    SEPARATE slots so the auto-summarize, LED-01 event-embedding, and importer-poll heartbeats run
    concurrently over their different corpora — none blocks another. ``summary_executor`` is
    injectable for tests.
    """
    return (
        summary_executor or BoundedSummaryExecutor(),
        BoundedSummaryExecutor(name="ledger-embedding"),
        BoundedSummaryExecutor(name="ledger-poll"),
    )


def _start_event_embedding_drain(
    executor: SummaryExecutor, writer: Any, page_size: int,
) -> dict[str, Any]:
    """Submit the event-embedding drain to its single slot; report started/no-op.

    Does ZERO embedding on the calling (action-queue) thread — it submits the
    whole cursor-forward drain (:meth:`EventEmbeddingWriter.drain_missing_events`)
    to the single-slot executor and returns in milliseconds with
    ``{"drainer": "started"}`` (this fire launched it) or
    ``{"drainer": "already_running"}`` (slot held → no-op). Per-fire counts are
    logged by the drain when it completes (async), so they cannot ride this
    return.
    """
    started = executor.submit(lambda: _run_event_embedding_drain(writer, page_size))
    outcome = "started" if started else "already_running"
    logger.info("event-embedding drain %s (page_size=%d)", outcome, page_size)
    return {"drainer": outcome, "page_size": page_size}


def _run_event_embedding_drain(writer: Any, page_size: int) -> None:
    """Background body of the ``drain_event_embeddings`` heartbeat.

    Runs on the single-slot embedding-drain thread (off the action queue) so
    the synchronous embedder/vector calls cannot park the queue. Fails soft on
    a vacant-embeddings profile: logs and returns rather than spamming a stack
    trace every cron fire (the drain's own per-page halt handles transient
    embedder outages).
    """
    try:
        result = writer.drain_missing_events(page_size=page_size)
    except EventEmbeddingServicesUnavailableError:
        logger.warning(
            "drain_event_embeddings: embedding/vector services unavailable in "
            "this profile — skipping the event-embedding drain",
        )
        return
    logger.info("event-embedding DRAIN complete: %s", result)


def _descriptor_only_entry(descriptor: Any) -> dict[str, Any]:
    """list_sources entry for a loaded plugin with no registered DB row."""
    return {
        "source_kind": descriptor.source_kind.value,
        "vendor": descriptor.vendor.value,
        "supported_modes": [m.value for m in descriptor.supported_modes],
        "default_lease_ttl_seconds": descriptor.default_lease_ttl_seconds,
        "default_pulling_root_uri": descriptor.default_pulling_root_uri,
        "source_id": None,
        "root_uri": None,
        "account_label": None,
        "enabled": None,
        "config_json": None,
    }


def _joined_entry(descriptor: Any, row: Any) -> dict[str, Any]:
    """list_sources entry for a loaded plugin paired with a registered DB row."""
    return {
        "source_kind": descriptor.source_kind.value,
        "vendor": descriptor.vendor.value,
        "supported_modes": [m.value for m in descriptor.supported_modes],
        "default_lease_ttl_seconds": descriptor.default_lease_ttl_seconds,
        "default_pulling_root_uri": descriptor.default_pulling_root_uri,
        "source_id": row.id,
        "root_uri": row.root_uri,
        "account_label": row.account_label,
        "enabled": row.enabled,
        "config_json": row.config_json,
    }


def _row_only_entry(row: Any) -> dict[str, Any]:
    """list_sources entry for a registered DB row whose plugin is not loaded."""
    return {
        "source_kind": row.source_kind.value,
        "vendor": None,
        "supported_modes": None,
        "default_lease_ttl_seconds": None,
        "default_pulling_root_uri": None,
        "source_id": row.id,
        "root_uri": row.root_uri,
        "account_label": row.account_label,
        "enabled": row.enabled,
        "config_json": row.config_json,
    }


__all__ = ["SessionLedgerService"]
