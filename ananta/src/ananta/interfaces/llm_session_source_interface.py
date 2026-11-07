"""LLM session source interface — marker + per-mode mixins.

Spec §5.1–§5.3. A concrete source plugin ALWAYS inherits both
``LLMSessionSourceInterface`` (marker) and exactly one of
``PullingSourceMixin`` / ``PushedSourceMixin``. The platform's
``SessionLedgerService.Registry`` dispatches by isinstance on the mixins.

Capability detection in ``ananta.core.plugins.capabilities`` provides
``is_llm_session_source`` and ``collect_llm_session_sources``; concrete
plugins MUST be located through that path, never bare ``isinstance``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from typing import ClassVar

from ananta.llm.session_ledger.types import (
    ExternalSessionRef,
    NormalizedSessionEvent,
    OversizedSelection,
    PollDiscoveryProgress,
    RawSessionEvent,
    SessionSourceDescriptor,
)


class LLMSessionSourceInterface(ABC):
    """Marker ABC for any plugin that ingests LLM conversation data.

    Concrete plugins ALSO inherit one of :class:`PullingSourceMixin` /
    :class:`PushedSourceMixin`.
    """

    INTERFACE_VERSION: ClassVar[str] = "1.0.0"

    @abstractmethod
    def describe(self) -> SessionSourceDescriptor:
        """Return descriptor: source_kind, supported_modes, vendor, default lease ttl."""

    @abstractmethod
    def normalize(self, raw: RawSessionEvent) -> NormalizedSessionEvent:
        """Convert a vendor-shaped raw event to the canonical normalized form.

        Raises ``ValueError`` on unrecognized payload types. No fallback coercion.
        """


class PullingSourceMixin(ABC):
    """Sources that actively read data (filesystem, DB, blob).

    P1.1.E: every method takes a leading ``root_uri: str`` — the ``root_uri`` of
    the ``__source`` row being polled — so the walk root is per-source, NOT a
    plugin-config singleton. The importer passes ``source_row.root_uri`` into
    all four. Filesystem/sqlite plugins resolve it via
    ``ananta.llm.session_ledger.root_uri.root_uri_to_path``; plugins whose
    method genuinely does not resolve a root (pure cursor math, symbolic or
    seed-path sources) accept the param and ignore it.
    """

    supports_whole_session_poll: ClassVar[bool] = False

    def discover_sessions_for_poll(
        self,
        root_uri: str,
        cursor_payload: dict[str, object] | None,
        *,
        read_event_cursor: Callable[[str], dict[str, object] | None],
        can_admit_next: Callable[[], bool],
        progress: PollDiscoveryProgress,
    ) -> Iterator[ExternalSessionRef]:
        """Default unchanged discovery; opt-in adapters own metadata ordering.

        Hooks receive a cursor callback, never a database provider. An opted-in
        adapter checks can_admit_next between metadata candidates and raises
        poll_budget.DiscoveryBudgetYield on partial progress. It acknowledges
        unchanged/deferred metadata before yielding; the importer acknowledges
        a selected PollSessionRef only after whole-session success.
        """
        return self.discover_sessions(root_uri, cursor_payload)

    def completed_discovery_cursor(
        self,
        root_uri: str,
        previous_cursor: dict[str, object] | None,
        *,
        exhausted: bool,
        pending: bool,
        progress: PollDiscoveryProgress,
    ) -> dict[str, object] | None:
        """Optional regular-sweep checkpoint, including an empty walk.

        Opt-in cursor JSON uses version, state (backfilling/caught_up), and
        after. Actual EOF clears after. caught_up covers regular work only.
        The low-priority recheck may change ONLY state to backfilling, keeping
        version/after byte-equivalent; oversized success changes neither.
        Default None means no hook-owned write.
        """
        return None

    def prepare_poll_event_cursor(
        self,
        root_uri: str,
        ref: ExternalSessionRef,
        stored_cursor: dict[str, object] | None,
    ) -> dict[str, object] | None:
        """Choose snapshot replay versus valid continuation from complete metadata.

        The importer passes the COMPLETE stored cursor, then strips its reserved
        _poll_completed_version before read_events. Default copies unchanged.
        """
        return dict(stored_cursor) if stored_cursor is not None else None

    def completed_session_fingerprint(
        self,
        root_uri: str,
        ref: ExternalSessionRef,
    ) -> dict[str, object] | None:
        """Stable, complete observed version after iterator EOF, or None/pending.

        Adapters distinguish physical complete-record EOF from early return and
        unstable input. Importer stores this with vendor EOF in ONE checkpoint;
        failure/partial session never publishes a completed marker.
        """
        return None

    def select_oversized_session(
        self,
        root_uri: str,
        *,
        read_event_cursor: Callable[[str], dict[str, object] | None],
        threshold: int,
    ) -> OversizedSelection:
        """Under source lease, recheck regular work before selecting one large ref.

        Default is dormant. Return regular_pending OR a candidate OR neither.
        Eligibility is recomputed next heartbeat, not kept in an in-memory queue.
        """
        return OversizedSelection()

    @abstractmethod
    def discover_sessions(
        self,
        root_uri: str,
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[ExternalSessionRef]:
        """Yield session refs newer than the cursor, under ``root_uri``.

        Implementations MUST be deterministic for a given
        ``(root_uri, cursor_payload)``. Raises ``ValueError`` on malformed
        cursor.
        """

    @abstractmethod
    def read_events(
        self,
        root_uri: str,
        session: ExternalSessionRef,
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[RawSessionEvent]:
        """Yield events newer than the cursor for the given session, under ``root_uri``.

        Same determinism + ValueError contract as ``discover_sessions``.
        """

    @abstractmethod
    def session_discovery_cursor(
        self,
        root_uri: str,
        last_seen: ExternalSessionRef | None,
    ) -> dict[str, object]:
        """Produce the cursor_payload to persist after a discovery pass."""

    @abstractmethod
    def event_read_cursor(
        self,
        root_uri: str,
        session: ExternalSessionRef,
        last_event: RawSessionEvent | None,
    ) -> dict[str, object]:
        """Produce the cursor_payload to persist after an event-read pass."""


class PushedSourceMixin(ABC):
    """Sources that receive data via MCP push."""

    @abstractmethod
    def parse_chunk(self, chunk_text: str) -> Iterator[RawSessionEvent]:
        """Parse one MCP-delivered chunk into raw events.

        ``chunk_text`` is decoded UTF-8 text. The MCP boundary takes ``str`` (not
        bytes); decoding happens in the ingest endpoint before this is called.

        Raises ``ValueError`` on malformed input — no recovery, no skip.
        """


__all__ = [
    "LLMSessionSourceInterface",
    "PullingSourceMixin",
    "PushedSourceMixin",
]
