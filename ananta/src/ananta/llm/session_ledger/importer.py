"""Poll loop that drives ``SessionLedgerService``.

Spec §11.1. The Importer owns the per-poll-pass orchestration:

1. List enabled sources from the repository.
2. For each pulling source: read its discovery cursor, iterate sessions,
   for each session read its event cursor, normalize, persist, advance
   cursors.
3. For each push: ``_dispatch_pushed(chunk_text)`` is called externally
   by ``SessionLedgerService.ingest_raw_chunk(...)``; we keep the same
   dispatch path so the persistence semantics are identical.

All persistence routes through ``SessionLedgerRepository``. Events always
land with full ``content_text`` — the importer never filters content at
ingest time. Future secret-identification is a periodic-search problem
(see ``workbench/2026-06-14_secretgate_full_eradication_design.md``),
not an ingest-validation problem.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from ananta.interfaces.llm_session_source_interface import (
    LLMSessionSourceInterface,
    PullingSourceMixin,
    PushedSourceMixin,
)
from ananta.llm.session_ledger.blob_adapter import (
    BlobAdapterError,
    SessionLedgerBlobAdapter,
)
from ananta.llm.session_ledger.importer_persistence import (
    _OrdinalCounter,
    persist_normalized_for_session,
)
from ananta.llm.session_ledger.lease import LeaseHeartbeat
from ananta.llm.session_ledger.poll_advisories import (
    collect_poll_advisories,
    completed_claude_read,
    poll_source_context,
    report_poll_advisories,
)
from ananta.llm.session_ledger.poll_budget import (
    COMPLETED_VERSION_KEY,
    FILESYSTEM_POLL_KINDS,
    OVERSIZED_SESSION_BYTES,
    DiscoveryBudgetYield,
    PollBudget,
    poll_yield_reason,
)
from ananta.llm.session_ledger.registry import SessionSourceRegistry
from ananta.llm.session_ledger.repository import (
    LeaseLostError,
    LedgerRepositoryError,
    PollingLeaseHandle,
    SessionLedgerRepository,
    SourceRow,
)
from ananta.llm.session_ledger.session_failures import (
    SessionReadError,
    discovery_source,
    failure_records,
    reading_session,
    record_persisted_events,
)
from ananta.llm.session_ledger.types import (
    CursorScope,
    ExternalSessionRef,
    ImportBatchStatus,
    ImporterReport,
    IngestSourceKind,
    NormalizedSessionEvent,
    PollDiscoveryProgress,
    PollSessionRef,
    RawSessionEvent,
    SourceVendor,
)

logger = logging.getLogger(__name__)

# Module-level RELOAD_SAFE marker — pure class adapter, no module-level
# mutable state, no background threads, no held service references.
RELOAD_SAFE = True


_VENDOR_FROM_SOURCE_KIND: dict[IngestSourceKind, SourceVendor] = {
    IngestSourceKind.AGENT_MESSAGING: SourceVendor.AGENT_MESSAGING,
    IngestSourceKind.CODEX_LOCAL: SourceVendor.CODEX,
    IngestSourceKind.CODEX_CLOUD: SourceVendor.CODEX,
    IngestSourceKind.CODEX_PUSHED: SourceVendor.CODEX,
    IngestSourceKind.CODEX_STATE: SourceVendor.CODEX,
    IngestSourceKind.CODEX_HISTORY: SourceVendor.CODEX,
    IngestSourceKind.CODEX_GOALS: SourceVendor.CODEX,
    IngestSourceKind.CODEX_MEMORIES: SourceVendor.CODEX,
    IngestSourceKind.CODEX_AMBIENT: SourceVendor.CODEX,
    IngestSourceKind.CLAUDE_CODE_LOCAL: SourceVendor.CLAUDE_CODE,
    IngestSourceKind.CLAUDE_CODE_CLOUD: SourceVendor.CLAUDE_CODE,
    IngestSourceKind.CLAUDE_CODE_PUSHED: SourceVendor.CLAUDE_CODE,
    IngestSourceKind.CLAUDE_CODE_HISTORY: SourceVendor.CLAUDE_CODE,
    IngestSourceKind.CLAUDE_CODE_TASKS: SourceVendor.CLAUDE_CODE,
    IngestSourceKind.CLAUDE_AI_EXPORT: SourceVendor.CLAUDE_AI,
    IngestSourceKind.CHATGPT_EXPORT: SourceVendor.CHATGPT,
}

_DEFAULT_POLLING_LEASE_TTL_SECONDS = 600
_ADOPT_RECENCY_WINDOW_MINUTES = 10


# GAP-5 idempotent ingest: the per-ingest-pass source-order occurrence counter
# for the null-``vendor_event_id`` fallback ordinal. Keyed by
# ``(session_id, event_type, role, content_key, event_at)`` so ONE counter dict
# serves both the per-session polling loop AND the multi-session push chunk loop;
# the value is the next ordinal (0-based) for that group. Re-ingest replays the
# same source order → same ordinals → the derived external_id dedups.
class LedgerPollError(Exception):
    """Raised by :meth:`SessionLedgerImporter.poll_source` (single failure channel).

    The targeted single-source poll has exactly ONE failure channel — it raises
    — instead of the three the batch poller exposes (a silent ``(0,0,0)`` on
    lease contention, an ``ImporterReport.batches_failed`` count, and an
    in-walk exception). Raised on: lease contention, a missing / deleted /
    disabled source, a non-PULLING source, or any source-pass failure. Callers
    (the A1 export kickoff, the duplicate-source repair completeness step)
    branch on raise / no-raise.
    """


@dataclass(frozen=True, slots=True)
class PushDispatchResult:
    """Result of one :meth:`SessionLedgerImporter.dispatch_pushed` call.

    Push-specific so the real push ``batch_id`` can be surfaced without
    widening :class:`ImporterReport` (whose contract means a full poll pass).
    """

    events_persisted: int
    batch_id: str


class SessionLedgerImporter:
    """Drives one poll pass over all enabled pulling sources.

    Pushed sources are driven by ``SessionLedgerService.ingest_raw_chunk``,
    which calls :meth:`dispatch_pushed` directly. Both paths reuse
    ``_persist_normalized`` so the persistence + blob-stage semantics
    are identical.
    """

    __slots__ = (
        "_registry",
        "_repository",
        "_blob_adapter",
        "_polling_lease_ttl_seconds",
        "_poll_clock",
        "_source_visit_seconds",
        "_last_oversized_source",
    )

    def __init__(
        self,
        *,
        registry: SessionSourceRegistry,
        repository: SessionLedgerRepository,
        blob_adapter: SessionLedgerBlobAdapter,
        polling_lease_ttl_seconds: int = _DEFAULT_POLLING_LEASE_TTL_SECONDS,
        poll_clock: Callable[[], float] = time.monotonic,
        source_visit_seconds: float = 60.0,
    ) -> None:
        self._registry = registry
        self._repository = repository
        self._blob_adapter = blob_adapter
        # Operator-tunable polling-lease TTL per v8 §D14.F. Read once at
        # construction so each poll pass uses a consistent value.
        self._polling_lease_ttl_seconds = polling_lease_ttl_seconds
        self._poll_clock = poll_clock
        self._source_visit_seconds = source_visit_seconds
        self._last_oversized_source: str | None = None

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    @report_poll_advisories
    def poll_once(self) -> ImporterReport:
        """Run one poll pass over every enabled pulling source."""
        sources_polled = 0
        sessions_seen = 0
        events_persisted = 0
        batches_failed = 0
        # Stable partition: cooperative/empty work runs before known blockers.
        sources = self._repository.list_sources(enabled_only=True)
        sources.sort(key=lambda row: _yield_reason(self, row) is not None)
        for source_row in sources:
            plugin = self._registry.get_by_kind(source_row.source_kind)
            if plugin is None or not isinstance(plugin, PullingSourceMixin):
                continue
            sources_polled += 1
            seen, persisted, failed = self._poll_one_pulling_source(source_row, plugin)
            sessions_seen += seen
            events_persisted += persisted
            batches_failed += failed
        return ImporterReport(
            sources_polled=sources_polled,
            sessions_seen=sessions_seen,
            events_persisted=events_persisted,
            batches_failed=batches_failed,
        )

    def poll_one_oversized(self) -> None:
        """Recompute and attempt one oversized session on the independent slot."""
        # This worker runs after the regular drain has retired its context.
        # Keep successful Claude EOF offsets available until its checkpoint.
        with collect_poll_advisories() as advisories:
            _poll_one_oversized(self)
            logger.info("ledger oversized DRAIN complete: unknown_line_advisories=%s", advisories.snapshot())

    def dispatch_pushed(
        self,
        *,
        source_kind: IngestSourceKind,
        chunk_text: str,
        source_id: str | None = None,
    ) -> PushDispatchResult:
        """Drive one push payload through the same persistence path as polling.

        When ``source_id`` is given (A2 — the claude_ai export bind) the push is
        dispatched against THAT row (validated to match ``source_kind`` and be
        enabled) instead of the first-enabled-by-kind row, and the row's own
        batch is opened and returned. When ``source_id`` is ``None`` (the
        codex / claude_code shipper push) the legacy resolve-or-create path
        runs. Returns a :class:`PushDispatchResult` carrying the real push
        ``batch_id``; raises on any parse / persist failure (the batch is
        finalized FAILED first).
        """
        plugin = self._registry.require_by_kind(source_kind)
        if not isinstance(plugin, PushedSourceMixin):
            raise ValueError(f"source_kind {source_kind.value!r} does not support pushed mode")
        source_row = _resolve_pushed_source(
            self._repository,
            plugin,
            source_kind,
            source_id,
        )
        # Push path is single-caller; ephemeral per-call token satisfies
        # finish_batch's contract without needing a real source lease.
        push_token = uuid.uuid4().hex
        batch_id = self._repository.start_batch(
            source_row.id,
            polling_lease_token=push_token,
        )
        persisted = 0
        ordinals: _OrdinalCounter = {}  # source-order fallback counter, this chunk
        try:
            for raw in plugin.parse_chunk(chunk_text):
                normalized = plugin.normalize(raw)
                persisted += self._persist_normalized(
                    source=source_row,
                    raw=raw,
                    normalized=normalized,
                    batch_id=batch_id,
                    ordinals=ordinals,
                )
            self._repository.finish_batch(
                batch_id,
                polling_lease_token=push_token,
                status=ImportBatchStatus.COMPLETED,
            )
        except ValueError as e:
            self._repository.finish_batch(
                batch_id,
                polling_lease_token=push_token,
                status=ImportBatchStatus.FAILED,
                error_message=str(e),
                error_kind="value_error",
            )
            raise
        except (LedgerRepositoryError, BlobAdapterError) as e:
            self._repository.finish_batch(
                batch_id,
                polling_lease_token=push_token,
                status=ImportBatchStatus.FAILED,
                error_message=str(e),
                error_kind=type(e).__name__,
            )
            raise
        return PushDispatchResult(events_persisted=persisted, batch_id=batch_id)

    # ------------------------------------------------------------------
    # Pulling-source orchestration
    # ------------------------------------------------------------------

    @report_poll_advisories
    def poll_source(self, source_id: str) -> ImporterReport:
        """Poll exactly one source by id, with a single (raising) failure channel.

        Targeted counterpart to :meth:`poll_once`. Used by the synchronous
        export-ingest kickoff (A1) and the duplicate-source repair (so the
        caller can prove "one source polled, zero failed" or see a raise — not
        re-interpret a silent zero). Raises :class:`LedgerPollError` on a
        missing / deleted / disabled source, a non-PULLING source, lease
        contention, OR any source-pass failure. On success returns an
        :class:`ImporterReport` with ``sources_polled=1, batches_failed=0``.
        """
        source_row = self._repository.get_source(source_id)
        if source_row is None:
            raise LedgerPollError(
                f"poll_source: source {source_id!r} not found or deleted",
            )
        if not source_row.enabled:
            raise LedgerPollError(
                f"poll_source: source {source_id!r} is disabled",
            )
        plugin = self._registry.get_by_kind(source_row.source_kind)
        if plugin is None or not isinstance(plugin, PullingSourceMixin):
            raise LedgerPollError(
                f"poll_source: source {source_id!r} ({source_row.source_kind.value}) is not a pulling source",
            )
        handle = self._repository.try_acquire_polling_lease(
            source_row.id,
            ttl_seconds=self._polling_lease_ttl_seconds,
        )
        if handle is None:
            raise LedgerPollError(
                f"poll_source: source {source_id!r} lease held by another poller",
            )
        seen, persisted, batches_failed = self._drive_acquired_pulling_source(
            source_row,
            plugin,
            handle,
        )
        if batches_failed:
            raise LedgerPollError(
                f"poll_source: poll pass for source {source_id!r} ({source_row.source_kind.value}) failed; see batch error",
            )
        return ImporterReport(
            sources_polled=1,
            sessions_seen=seen,
            events_persisted=persisted,
            batches_failed=0,
        )

    def _poll_one_pulling_source(
        self,
        source_row: SourceRow,
        plugin: LLMSessionSourceInterface,
    ) -> tuple[int, int, int]:
        if not isinstance(plugin, PullingSourceMixin):  # narrowed by caller
            raise TypeError(  # pragma: no cover - defensive
                f"plugin for {source_row.source_kind.value!r} is not PullingSourceMixin"
            )
        handle = self._repository.try_acquire_polling_lease(
            source_row.id,
            ttl_seconds=self._polling_lease_ttl_seconds,
        )
        if handle is None:
            # Lease contention is a silent skip for the batch poller — one
            # contended source must not abort the whole pass. poll_source
            # RAISES instead (it polls exactly one source on purpose).
            logger.info(
                "ledger poll: source %s lease held by another poller; skipping",
                source_row.id,
            )
            return 0, 0, 0
        return self._drive_acquired_pulling_source(source_row, plugin, handle)

    def _drive_acquired_pulling_source(
        self,
        source_row: SourceRow,
        plugin: PullingSourceMixin,
        handle: PollingLeaseHandle,
        *,
        oversized: bool = False,
    ) -> tuple[int, int, int]:
        """Drive one pulling source on an already-acquired lease.

        Shared by :meth:`_poll_one_pulling_source` (batch poller) and
        :meth:`poll_source` (targeted). Returns ``(seen, persisted,
        batches_failed)``; finalizes the batch FAILED + logs on any in-walk
        exception (it does NOT re-raise — the failure surfaces via the
        ``batches_failed`` count, which the two callers translate differently).
        """
        ttl_seconds = self._polling_lease_ttl_seconds
        # v8 D14.C: adopt BEFORE start_batch so route-uploaded batches
        # become this importer's owned batch atomically. The claim sets the
        # batch row's polling_lease_token to handle.lease_token, fencing
        # subsequent finish_batch calls against any stale owner.
        batch_id = _adopt_or_start_batch(self._repository, source_row, handle)
        heartbeat = LeaseHeartbeat(self._repository, handle, ttl_seconds)
        batches_failed = 0
        try:
            run_pass = self._run_oversized_pass if oversized else self._run_pulling_pass
            with poll_source_context(source_row.id):
                seen, persisted, batches_failed = run_pass(
                    source_row=source_row,
                    plugin=plugin,
                    batch_id=batch_id,
                    heartbeat=heartbeat,
                )
        except LeaseLostError as exc:
            batches_failed = 1
            seen = persisted = 0
            _finalize_lease_lost(
                self._repository,
                batch_id,
                heartbeat,
                exc,
                source_row.id,
            )
        except ValueError as e:
            batches_failed = 1
            seen = persisted = 0
            _finalize_failed(
                self._repository,
                batch_id,
                heartbeat,
                e,
                source_row,
                "value_error",
            )
        except (LedgerRepositoryError, BlobAdapterError) as e:
            batches_failed = 1
            seen = persisted = 0
            _finalize_failed(
                self._repository,
                batch_id,
                heartbeat,
                e,
                source_row,
                type(e).__name__,
            )
        finally:
            # Owner-aware release: silent no-op when our token no longer
            # matches the persisted row (a successor has taken over).
            self._repository.release_polling_lease(heartbeat.handle)
        return seen, persisted, batches_failed

    def _run_pulling_pass(
        self,
        *,
        source_row: SourceRow,
        plugin: PullingSourceMixin,
        batch_id: str,
        heartbeat: LeaseHeartbeat,
    ) -> tuple[int, int, int]:
        return _run_regular_visit(self, source_row=source_row, plugin=plugin, batch_id=batch_id, heartbeat=heartbeat)

    def _run_oversized_pass(
        self,
        *,
        source_row: SourceRow,
        plugin: PullingSourceMixin,
        batch_id: str,
        heartbeat: LeaseHeartbeat,
    ) -> tuple[int, int, int]:
        return _run_oversized_visit(self, source_row=source_row, plugin=plugin, batch_id=batch_id, heartbeat=heartbeat)

    # ------------------------------------------------------------------
    # Persistence helpers
    # ------------------------------------------------------------------

    def _persist_normalized(
        self,
        *,
        source: SourceRow,
        raw: RawSessionEvent,
        normalized: NormalizedSessionEvent,
        batch_id: str,
        ordinals: _OrdinalCounter,
    ) -> int:
        vendor = _VENDOR_FROM_SOURCE_KIND[source.source_kind]
        session_id = self._repository.upsert_session(
            source_id=source.id,
            external_session_id=normalized.external_session_id,
            vendor=vendor,
            source_kind=source.source_kind,
            vendor_session_label=None,
            project_path=None,
            first_event_at=raw.event_at,
            last_event_at=normalized.event_at,
        )
        return self._persist_normalized_for_session(
            session_id=session_id,
            normalized=normalized,
            batch_id=batch_id,
            session_vendor=vendor,
            source_kind=source.source_kind,
            ordinals=ordinals,
        )

    def _poll_one_session(
        self,
        *,
        source_row: SourceRow,
        session_ref: ExternalSessionRef,
        batch_id: str,
        plugin: PullingSourceMixin,
        heartbeat: LeaseHeartbeat,
        progress: PollDiscoveryProgress | None = None,
    ) -> int:
        """Process events for one session; advance its event_read cursor on success.

        ``heartbeat.check()`` fires once per yielded event so a slow
        single-conversation walk extends the polling lease without depending
        on session boundaries. The check raises ``LeaseLostError`` which the
        outer ``_poll_one_pulling_source`` catches (NOT the per-session
        ``except (ValueError, LedgerRepositoryError, BlobAdapterError)``
        block — lease-loss is a batch-terminal signal).
        """
        event_cursor = self._repository.read_cursor(
            source_id=source_row.id,
            scope=CursorScope.EVENT_READ,
            scope_key=session_ref.external_session_id,
        )
        stored_cursor = event_cursor
        if progress is not None:
            event_cursor = plugin.prepare_poll_event_cursor(source_row.root_uri, session_ref, event_cursor)
            if event_cursor is not None:
                event_cursor = _vendor_cursor(event_cursor)
        events_persisted = 0
        last_raw: RawSessionEvent | None = None
        ordinals: _OrdinalCounter = {}  # source-order fallback counter, this session walk
        for raw in plugin.read_events(source_row.root_uri, session_ref, event_cursor):
            heartbeat.check()
            last_raw = raw
            normalized = plugin.normalize(raw)
            vendor = _VENDOR_FROM_SOURCE_KIND[source_row.source_kind]
            session_id = self._repository.upsert_session(
                source_id=source_row.id,
                external_session_id=session_ref.external_session_id,
                vendor=vendor,
                source_kind=source_row.source_kind,
                vendor_session_label=session_ref.vendor_session_label,
                project_path=session_ref.project_path,
                first_event_at=session_ref.first_seen_at,
                last_event_at=normalized.event_at,
                originator_session_label=session_ref.originator_session_label,
                originator_agent_instance_id=session_ref.originator_agent_instance_id,
                recipient_session_label=session_ref.recipient_session_label,
                recipient_agent_instance_id=session_ref.recipient_agent_instance_id,
                summary_text_seed=session_ref.summary_text_seed,
            )
            # Per 2026-05-31 Architect ruling §2: single-actor sessions
            # denormalize the event's actor from the session row. The plugin
            # may also set ``normalized.actor_*`` directly (peer-thread case
            # where each event has a different sender_session_label).
            if normalized.actor_session_label is None and normalized.actor_agent_instance_id is None:
                normalized = replace(
                    normalized,
                    actor_session_label=session_ref.originator_session_label,
                    actor_agent_instance_id=session_ref.originator_agent_instance_id,
                )
            persisted = self._persist_normalized_for_session(
                session_id=session_id,
                normalized=normalized,
                batch_id=batch_id,
                session_vendor=vendor,
                source_kind=source_row.source_kind,
                ordinals=ordinals,
            )
            events_persisted += persisted
            record_persisted_events(persisted)
        _checkpoint_session(
            self,
            source_row=source_row,
            session_ref=session_ref,
            plugin=plugin,
            heartbeat=heartbeat,
            progress=progress,
            last_raw=last_raw,
            stored_cursor=stored_cursor,
        )
        return events_persisted

    def _persist_normalized_for_session(
        self,
        *,
        session_id: str,
        normalized: NormalizedSessionEvent,
        batch_id: str,
        session_vendor: SourceVendor,
        source_kind: IngestSourceKind,
        ordinals: _OrdinalCounter,
    ) -> int:
        return persist_normalized_for_session(
            self._repository,
            self._blob_adapter,
            session_id=session_id,
            normalized=normalized,
            batch_id=batch_id,
            session_vendor=session_vendor,
            source_kind=source_kind,
            ordinals=ordinals,
        )


def _resolve_pushed_source(
    repository: SessionLedgerRepository,
    plugin: LLMSessionSourceInterface,
    source_kind: IngestSourceKind,
    source_id: str | None,
) -> SourceRow:
    """Resolve the ``__source`` row a push dispatches against.

    ``source_id`` given → resolve THAT row and validate it matches
    ``source_kind`` + is enabled (A2 export bind — avoids dispatching against
    the wrong row when more than one exists). ``source_id`` None → legacy
    first-enabled-by-kind resolve-or-create (the shipper push path).
    """
    if source_id is None:
        return _resolve_or_create_pushed_source(repository, plugin, source_kind)
    row = repository.get_source(source_id)
    if row is None:
        raise ValueError(
            f"dispatch_pushed: source {source_id!r} not found or deleted",
        )
    if row.source_kind is not source_kind:
        raise ValueError(
            f"dispatch_pushed: source {source_id!r} is {row.source_kind.value!r}, not {source_kind.value!r}",
        )
    if not row.enabled:
        raise ValueError(
            f"dispatch_pushed: source {source_id!r} is disabled",
        )
    return row


def _resolve_or_create_pushed_source(
    repository: SessionLedgerRepository,
    plugin: LLMSessionSourceInterface,
    source_kind: IngestSourceKind,
) -> SourceRow:
    for row in repository.list_sources(enabled_only=True):
        if row.source_kind is source_kind:
            return row
    descriptor = plugin.describe()
    source_id = repository.insert_source(
        source_kind=source_kind,
        root_uri=f"pushed:{source_kind.value}",
        account_label=descriptor.vendor.value,
    )
    row = repository.get_source(source_id)
    if row is None:
        raise LedgerRepositoryError(
            f"failed to read back inserted source {source_id}",
        )
    return row


def _finalize_lease_lost(
    repository: SessionLedgerRepository,
    batch_id: str,
    heartbeat: LeaseHeartbeat,
    exc: LeaseLostError,
    source_id: str,
) -> None:
    """Finalize a batch the heartbeat declared lease-lost.

    Caller is the outer ``_poll_one_pulling_source`` exception handler;
    owner-aware ``finish_batch`` is what makes the late-finish safe.
    """
    finished = repository.finish_batch(
        batch_id,
        polling_lease_token=heartbeat.handle.lease_token,
        status=ImportBatchStatus.FAILED,
        error_message=str(exc),
        error_kind="lease_lost",
    )
    if not finished:
        # v8 invariant: this path is unreachable for adopted/owned batches
        # because a successor cannot adopt a batch whose token != NULL. Log
        # defensively if it ever fires.
        logger.warning(
            "ledger poll: lease lost on source %s; finish_batch returned no rows for batch %s (rare race; likely no harm)",
            source_id,
            batch_id,
        )
    logger.warning(
        "ledger poll: lease lost during walk for source %s batch %s; aborted (other poller continues)",
        source_id,
        batch_id,
    )


def _finalize_failed(
    repository: SessionLedgerRepository,
    batch_id: str,
    heartbeat: LeaseHeartbeat,
    exc: Exception,
    source_row: SourceRow,
    error_kind: str,
) -> None:
    """Finalize a batch terminated by an in-walk exception (non-lease)."""
    repository.finish_batch(
        batch_id,
        polling_lease_token=heartbeat.handle.lease_token,
        status=ImportBatchStatus.FAILED,
        error_message=str(exc),
        error_kind=error_kind,
    )
    logger.exception(
        "%s during poll of source %s (%s)",
        error_kind,
        source_row.id,
        source_row.source_kind.value,
    )


def utcnow_iso() -> str:
    """Helper for log timestamps. Importer is otherwise clock-free."""
    return datetime.now(UTC).isoformat(timespec="seconds")


__all__ = ["SessionLedgerImporter"]


def _poll_one_oversized(importer: SessionLedgerImporter) -> None:
    """Attempt at most one whole oversized session on the separate worker.

    Only explicit-capability filesystem adapters participate. Read eligibility
    and recheck selection under the source lease; contention performs no
    cursor writes. The rotation hint is process-local fairness only: durable
    completion metadata is always reread after restart. Same-source refresh
    waits for this lease; unrelated sources keep their normal worker.
    """
    for source, plugin in _oversized_sources(importer):
        handle = importer._repository.try_acquire_polling_lease(
            source.id,
            ttl_seconds=importer._polling_lease_ttl_seconds,
        )
        if handle is None:
            logger.info("ledger oversized: source %s lease contended; retry next heartbeat", source.id)
            continue
        seen, _, failed = importer._drive_acquired_pulling_source(source, plugin, handle, oversized=True)
        if seen or failed:
            if seen and not failed:
                importer._last_oversized_source = source.id
            return


def _run_regular_visit(
    importer: SessionLedgerImporter,
    *,
    source_row: SourceRow,
    plugin: PullingSourceMixin,
    batch_id: str,
    heartbeat: LeaseHeartbeat,
) -> tuple[int, int, int]:
    """Cooperative whole-session visit; unproved contracts remain whole polls."""
    budget = PollBudget(importer._source_visit_seconds, importer._poll_clock)
    reason = _yield_reason(importer, source_row)
    capability = source_row.source_kind in FILESYSTEM_POLL_KINDS and plugin.supports_whole_session_poll
    progress = PollDiscoveryProgress()
    failures = failure_records(importer._repository, source_row, plugin, heartbeat)
    discovery = discovery_source(plugin, failures)
    sessions_seen = events_persisted = sessions_failed = 0
    discovery_cursor = importer._repository.read_cursor(
        source_id=source_row.id,
        scope=CursorScope.DISCOVERY,
    )
    last_session_ref: ExternalSessionRef | None = None
    iterator = (
        discovery.discover_sessions_for_poll(
            source_row.root_uri,
            discovery_cursor,
            read_event_cursor=lambda key: _read_event_cursor(importer, source_row, key),
            can_admit_next=budget.can_admit_next,
            progress=progress,
        )
        if capability
        else plugin.discover_sessions(source_row.root_uri, discovery_cursor)
    )
    try:
        for session_ref in _admitted_sessions(iterator, budget, progress, yielding=reason is None, capability=capability):
            heartbeat.check()
            sessions_seen += 1
            try:
                with reading_session(failures, session_ref):
                    events_persisted += importer._poll_one_session(
                        source_row=source_row,
                        session_ref=session_ref,
                        batch_id=batch_id,
                        plugin=plugin,
                        heartbeat=heartbeat,
                        progress=progress if capability else None,
                    )
                last_session_ref = session_ref
                _acknowledge_discovery_ref(session_ref, progress)
            except SessionReadError as exc:
                sessions_failed += 1
                events_persisted += exc.events_persisted
                progress.regular_pending = True
            except (ValueError, LedgerRepositoryError, BlobAdapterError) as exc:
                sessions_failed += 1
                progress.regular_pending = True
                logger.warning(
                    "ledger poll: skipping session %s (%s) on %s: %s",
                    session_ref.external_session_id,
                    source_row.source_kind.value,
                    type(exc).__name__,
                    exc,
                )
    finally:
        close = getattr(iterator, "close", None)
        if callable(close):
            close()
    heartbeat.check()
    _write_discovery_checkpoint(importer, source_row, plugin, discovery_cursor, progress, last_session_ref, capability)
    _finish_session_batch(importer._repository, batch_id, heartbeat, sessions_failed)
    elapsed = budget.elapsed
    logger.info(
        "ledger poll source=%s kind=%s policy=%s elapsed=%.3fs budget=%.3fs overrun=%.3fs sessions=%d failures=%d exhausted=%s pending=%s deferred=%d deferred_bytes=%d",
        source_row.id,
        source_row.source_kind.value,
        reason or "whole_session_cooperative",
        elapsed,
        budget.seconds,
        max(0.0, elapsed - budget.seconds),
        sessions_seen,
        sessions_failed,
        progress.exhausted,
        progress.regular_pending,
        progress.deferred_count,
        progress.deferred_bytes,
    )
    return sessions_seen, events_persisted, int(sessions_failed > 0)


def _run_oversized_visit(
    importer: SessionLedgerImporter,
    *,
    source_row: SourceRow,
    plugin: PullingSourceMixin,
    batch_id: str,
    heartbeat: LeaseHeartbeat,
) -> tuple[int, int, int]:
    """Authoritative recheck under the common acquired-source batch custody."""
    heartbeat.check()
    cursor = importer._repository.read_cursor(source_id=source_row.id, scope=CursorScope.DISCOVERY)
    seen = persisted = failed = 0
    failures = failure_records(importer._repository, source_row, plugin, heartbeat)
    discovery = discovery_source(plugin, failures)
    if cursor is not None and cursor.get("state") == "caught_up":
        selection = discovery.select_oversized_session(
            source_row.root_uri,
            read_event_cursor=lambda key: _read_event_cursor(importer, source_row, key),
            threshold=OVERSIZED_SESSION_BYTES,
        )
        heartbeat.check()
        if selection.regular_pending:
            importer._repository.write_cursor(
                source_id=source_row.id,
                scope=CursorScope.DISCOVERY,
                cursor_payload={**cursor, "state": "backfilling"},
            )
        elif selection.candidate is not None:
            seen = 1
            try:
                with reading_session(failures, selection.candidate):
                    persisted = importer._poll_one_session(
                        source_row=source_row,
                        session_ref=selection.candidate,
                        batch_id=batch_id,
                        plugin=plugin,
                        heartbeat=heartbeat,
                        progress=PollDiscoveryProgress(),
                    )
            except SessionReadError as exc:
                failed = 1
                persisted = exc.events_persisted
            # Never checkpoint regular discovery on an oversized success.
    _finish_session_batch(importer._repository, batch_id, heartbeat, failed)
    return seen, persisted, failed


def _acknowledge_discovery_ref(ref: ExternalSessionRef, progress: PollDiscoveryProgress) -> None:
    if isinstance(ref, PollSessionRef) and ref.discovery_pair is not None:
        progress.last_examined = ref.discovery_pair


def _finish_session_batch(repository: SessionLedgerRepository, batch_id: str, heartbeat: LeaseHeartbeat, failed: int) -> None:
    repository.finish_batch(
        batch_id,
        polling_lease_token=heartbeat.handle.lease_token,
        status=ImportBatchStatus.FAILED if failed else ImportBatchStatus.COMPLETED,
        error_kind="session_failure" if failed else None,
        error_message=f"{failed} session(s) failed; see per-session diagnostics" if failed else None,
    )


def _checkpoint_session(
    importer: SessionLedgerImporter,
    *,
    source_row: SourceRow,
    session_ref: ExternalSessionRef,
    plugin: PullingSourceMixin,
    heartbeat: LeaseHeartbeat,
    progress: PollDiscoveryProgress | None,
    last_raw: RawSessionEvent | None,
    stored_cursor: dict[str, object] | None,
) -> None:
    """Publish stable completion only with a successful whole-session checkpoint."""
    fingerprint = None
    if progress is not None:
        fingerprint = plugin.completed_session_fingerprint(source_row.root_uri, session_ref)
        if fingerprint is None:
            progress.regular_pending = True
    # Only the Claude filesystem adapter publishes this poll-local marker.
    has_read = last_raw is not None or completed_claude_read(source_row.root_uri, session_ref.external_session_id) is not None
    checkpoint = plugin.event_read_cursor(source_row.root_uri, session_ref, last_raw) if has_read else _vendor_cursor(stored_cursor)
    if fingerprint is not None:
        checkpoint = {**(checkpoint or {}), COMPLETED_VERSION_KEY: fingerprint}
    if has_read or fingerprint is not None:
        heartbeat.check()
        importer._repository.write_cursor(
            source_id=source_row.id,
            scope=CursorScope.EVENT_READ,
            cursor_payload=checkpoint or {},
            scope_key=session_ref.external_session_id,
        )


def _read_event_cursor(importer: SessionLedgerImporter, source: SourceRow, key: str) -> dict[str, object] | None:
    return importer._repository.read_cursor(
        source_id=source.id,
        scope=CursorScope.EVENT_READ,
        scope_key=key,
    )


def _yield_reason(importer: SessionLedgerImporter, source: SourceRow) -> str | None:
    plugin = importer._registry.get_by_kind(source.source_kind)
    return poll_yield_reason(
        source.source_kind,
        completed_capability=isinstance(plugin, PullingSourceMixin) and plugin.supports_whole_session_poll,
    )


def _oversized_sources(
    importer: SessionLedgerImporter,
) -> Iterator[tuple[SourceRow, PullingSourceMixin]]:
    """Stable row order plus a transient last-completed-source rotation hint."""
    sources = sorted(importer._repository.list_sources(enabled_only=True), key=lambda row: row.id)
    last = importer._last_oversized_source
    if last is not None:
        sources = [row for row in sources if row.id > last] + [row for row in sources if row.id <= last]
    for source in sources:
        plugin = importer._registry.get_by_kind(source.source_kind)
        if source.source_kind in FILESYSTEM_POLL_KINDS and isinstance(plugin, PullingSourceMixin) and plugin.supports_whole_session_poll:
            yield source, plugin


def _admitted_sessions(
    iterator: Iterator[ExternalSessionRef],
    budget: PollBudget,
    progress: PollDiscoveryProgress,
    *,
    yielding: bool,
    capability: bool,
) -> Iterator[ExternalSessionRef]:
    """Admission happens before next(), never inside a session/event iterator."""
    first = True
    while True:
        if yielding and not first and not budget.can_admit_next():
            progress.regular_pending = True
            return
        first = False
        try:
            session_ref = next(iterator)
        except StopIteration:
            progress.exhausted = True
            return
        except DiscoveryBudgetYield:
            if not capability:
                raise ValueError("budget-yield signal from a non-capability adapter") from None
            progress.regular_pending = True
            return
        yield session_ref


def _write_discovery_checkpoint(
    importer: SessionLedgerImporter,
    source_row: SourceRow,
    plugin: PullingSourceMixin,
    discovery_cursor: dict[str, object] | None,
    progress: PollDiscoveryProgress,
    last_session_ref: ExternalSessionRef | None,
    capability: bool,
) -> None:
    """The regular walk alone checkpoints its source progress, including empty EOF."""
    completed_cursor = (
        plugin.completed_discovery_cursor(
            source_row.root_uri,
            discovery_cursor,
            exhausted=progress.exhausted,
            pending=progress.regular_pending,
            progress=progress,
        )
        if capability
        else None
    )
    if completed_cursor is not None:
        importer._repository.write_cursor(
            source_id=source_row.id,
            scope=CursorScope.DISCOVERY,
            cursor_payload=completed_cursor,
        )
    elif last_session_ref is not None:
        importer._repository.write_cursor(
            source_id=source_row.id,
            scope=CursorScope.DISCOVERY,
            cursor_payload=plugin.session_discovery_cursor(source_row.root_uri, last_session_ref),
        )


def _vendor_cursor(cursor: dict[str, object] | None) -> dict[str, object] | None:
    """Copy the vendor payload; reserved completion metadata never reaches parsers."""
    if cursor is None:
        return None
    return {key: value for key, value in cursor.items() if key != COMPLETED_VERSION_KEY}


def _adopt_or_start_batch(repository: SessionLedgerRepository, source: SourceRow, handle: PollingLeaseHandle) -> str:
    """Retain route adoption and release custody even if batch setup fails."""
    batch_id: str | None = None
    try:
        batch_id = repository.adopt_route_batch_for_source(
            source.id,
            polling_lease_token=handle.lease_token,
            recency_window_minutes=_ADOPT_RECENCY_WINDOW_MINUTES,
        )
        if batch_id is None:
            batch_id = repository.start_batch(source.id, polling_lease_token=handle.lease_token)
        return batch_id
    finally:
        if batch_id is None:
            repository.release_polling_lease(handle)
