"""Codex local-rollout filesystem session source.

Spec §6, §17.2. Thin adapter around ``ananta.llm.session_ledger.vendor.codex``
that implements ``LLMSessionSourceInterface + PullingSourceMixin``.

Cursor semantics (plugin-private; opaque to platform):

* Discovery cursor: ``{"mode": "full_rescan"}`` — each poll re-enumerates all
  sorted local and archived files, including older stems that failed or grew.
  A legacy ``highest_filename_stem`` is superseded, never an exclusion.
* Event-read cursor: ``{"line_offset": int}`` — 0-based line offset within the
  rollout file. Line-based survives partial writes better than byte offsets.

Each poll pays for full stat enumeration. Only selected changed versions pay
header/prefix model-context scanning; selected paths avoid another lookup walk.
There is no recency cutoff or constant-time claim.

Per ``feedback_one_plugin_per_scenario`` this is the LOCAL adapter only.
The cloud sibling (``codex_pushed_session_source_plugin``) is a separate
plugin; profile bindings pick one.

Per plugin-authoring trap #10: defaults declared in ``plugin.yaml``'s
``config:`` block; ``self.config_provider.get(...)`` MUST NOT pass a
second-argument literal.

Activated polling uses the shared filesystem metadata policy: newest-first
(mtime_ns, relative path) sweep continuation, per-session completed versions,
explicit EOF reset, and leased oversized selection. Legacy direct discovery
and vendor cursor methods below remain unchanged; the importer uses the safe
poll hooks. New task/ambient snapshots replay completely, and rollout offsets
continue only a proved same-inode append. No vendor event identity changes.
"""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ActionStatus, ProcessorPolicyCategory
from ananta.core.plugins.plugin_base import PluginBase
from ananta.interfaces.edge_process_provider import EdgeProcessDefinition, EdgeProcessProvider
from ananta.interfaces.llm_session_source_interface import (
    LLMSessionSourceInterface,
)
from ananta.llm.session_ledger.filesystem_poll import (
    FilesystemCandidate,
    FilesystemPollingMixin,
    acknowledge_read,
    observe_candidate,
    selected_path,
)
from ananta.llm.session_ledger.root_uri import root_uri_to_path
from ananta.llm.session_ledger.types import (
    ExternalSessionRef,
    IngestMode,
    IngestSourceKind,
    NormalizedSessionEvent,
    RawSessionEvent,
    SessionSourceDescriptor,
    SourceVendor,
)
from ananta.llm.session_ledger.vendor import codex as codex_vendor

logger = logging.getLogger(__name__)


_LINE_OFFSET_KEY = "line_offset"
_LINE_OFFSET_INTERNAL = "_codex_line_offset"


class CodexFilesystemSessionSourcePlugin(
    PluginBase,
    LLMSessionSourceInterface,
    FilesystemPollingMixin,
    EdgeProcessProvider,
):
    """Local-filesystem Codex rollout reader.

    P1.1.E: the walk root is the polled ``__source`` row's ``root_uri``, threaded
    into every ``PullingSourceMixin`` method by the importer and resolved via
    ``root_uri_to_path`` — no plugin-config singleton.
    """

    supports_whole_session_poll: ClassVar[bool] = True

    def __init__(self) -> None:
        super().__init__()
        self.name = "codex_filesystem_session_source_plugin"

    # ------------------------------------------------------------------
    # PluginBase lifecycle
    # ------------------------------------------------------------------

    def prepare_for_readiness(self) -> None:
        # Config-driven ``root_dir`` is resolved lazily on first discover/read.
        # ``config_provider`` is bound later via ``initialize(config)`` per the
        # platform two-phase init contract (prepare_for_readiness runs before
        # config is merged + dispatched to ``initialize``).
        self.set_ready()

    def initialize(self, config: dict[str, object]) -> None:
        """Bind config_provider so yaml defaults + per-deployment overrides take effect."""
        from ananta.core.config.config_provider import ConfigProvider  # noqa: PLC0415

        self.config_provider = ConfigProvider(self.name, config)

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        """Declare the bounded source-local qualification result contract."""
        return {
            "qualify": EdgeProcessDefinition(
                name="qualify",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type="codex_filesystem_session_source_qualification",
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
        }

    @platform_process(
        name="qualify",
        is_discoverable=True,
        context_handling=ContextHandling.NONE,
        parameters={},
        output_type="object",
        output_description=("Bounded source-local Codex filesystem retrieval proof: {source_registered, source_kind, backfill_count, sample_content_retrieved}."),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Registration, one bounded backfill, and a non-payload retrieval proof.",
            properties={
                "source_registered": ParameterMetadata(type=ParameterType.BOOLEAN),
                "source_kind": ParameterMetadata(type=ParameterType.STRING),
                "backfill_count": ParameterMetadata(type=ParameterType.INTEGER),
                "sample_content_retrieved": ParameterMetadata(type=ParameterType.BOOLEAN),
            },
        ),
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        result_processor_customizations=MergeResultProcessorCustomizations(
            result_type="codex_filesystem_session_source_qualification",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    def qualify(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Prove this already-consented source can register, backfill, and retrieve.

        The qualifier deliberately returns only booleans/counts and the source
        kind. It never returns a rollout line or timeline payload.
        """
        del params, state
        try:
            proof = _qualify_source(
                ledger=self._require_ledger_service(),
                source_kind=IngestSourceKind.CODEX_LOCAL.value,
            )
        except (RuntimeError, TypeError, ValueError) as exc:
            return _qualification_failure(exc)
        return {
            "action_status": ActionStatus.COMPLETED.value,
            "data": proof,
            "actions": [],
            "error": None,
            "timestamp": datetime.now(UTC).isoformat(),
        }

    def _require_ledger_service(self) -> Any:
        if self.orchestrator_ref is None:
            raise RuntimeError(f"{self.name}: orchestrator_ref unavailable")
        ledger = self.orchestrator_ref.get_service("session_ledger_service")
        if ledger is None:
            raise RuntimeError(f"{self.name}: session_ledger_service unavailable")
        return ledger

    # ------------------------------------------------------------------
    # LLMSessionSourceInterface
    # ------------------------------------------------------------------

    def describe(self) -> SessionSourceDescriptor:
        # Boot registers this root only when ``ledger_allowed_roots`` contains it,
        # which setup writes when the operator selects ``codex_local`` (opt-in).
        return SessionSourceDescriptor(
            source_kind=IngestSourceKind.CODEX_LOCAL,
            vendor=SourceVendor.CODEX,
            supported_modes=(IngestMode.PULLING,),
            default_pulling_root_uri="~/.codex/sessions",
        )

    def normalize(self, raw: RawSessionEvent) -> NormalizedSessionEvent:
        return codex_vendor.normalize_raw(raw)

    # ------------------------------------------------------------------
    # PullingSourceMixin
    # ------------------------------------------------------------------

    def _poll_candidates(self, root_uri: str) -> Iterator[FilesystemCandidate]:
        root = root_uri_to_path(root_uri)
        for path in codex_vendor.iter_session_files(root):
            candidate = observe_candidate(path, external_id=codex_vendor.external_session_id_for(path), relative_path=path.relative_to(root.parent).as_posix(), json_lines=True)
            if candidate is not None:
                yield candidate

    def _poll_ref(self, root_uri: str, candidate: FilesystemCandidate) -> ExternalSessionRef:
        del root_uri
        meta = codex_vendor.read_session_meta(candidate.path)
        return ExternalSessionRef(candidate.external_id, meta.session_uuid, meta.cwd, meta.started_at)

    def discover_sessions(
        self,
        root_uri: str,
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[ExternalSessionRef]:
        del cursor_payload
        root = root_uri_to_path(root_uri)
        for path in codex_vendor.iter_session_files(root):
            stem = codex_vendor.external_session_id_for(path)
            meta = codex_vendor.read_session_meta(path)
            yield ExternalSessionRef(
                external_session_id=stem,
                vendor_session_label=meta.session_uuid,
                project_path=meta.cwd,
                first_seen_at=meta.started_at,
            )

    def read_events(
        self,
        root_uri: str,
        session: ExternalSessionRef,
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[RawSessionEvent]:
        yield from self._read_events(root_uri, session, cursor_payload)
        acknowledge_read(session)

    def _read_events(
        self,
        root_uri: str,
        session: ExternalSessionRef,
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[RawSessionEvent]:
        path = selected_path(session) or self._path_for_session(root_uri, session.external_session_id)
        start_line = _parse_optional_int(cursor_payload, _LINE_OFFSET_KEY) or 0
        for line_offset, event in codex_vendor.iter_events_from_path(path, start_line=start_line):
            # Carry the line_offset into payload so event_read_cursor can
            # reconstruct the cursor from the last RawSessionEvent. Use a
            # private key prefixed with underscore so vendor.normalize_raw
            # never touches it.
            payload_with_offset = dict(event.payload)
            payload_with_offset[_LINE_OFFSET_INTERNAL] = line_offset
            yield RawSessionEvent(
                external_session_id=event.external_session_id,
                payload=payload_with_offset,
                event_at=event.event_at,
                vendor_event_id=event.vendor_event_id,
                vendor_parent_event_id=event.vendor_parent_event_id,
            )

    def session_discovery_cursor(
        self,
        root_uri: str,  # pyright: ignore[reportUnusedParameter]  # noqa: ARG002
        last_seen: ExternalSessionRef | None,
    ) -> dict[str, object]:
        del last_seen
        return {"mode": "full_rescan"}

    def event_read_cursor(
        self,
        root_uri: str,  # pyright: ignore[reportUnusedParameter]  # noqa: ARG002
        session: ExternalSessionRef,
        last_event: RawSessionEvent | None,
    ) -> dict[str, object]:
        if last_event is None:
            return {_LINE_OFFSET_KEY: None}
        offset = last_event.payload.get(_LINE_OFFSET_INTERNAL)
        if not isinstance(offset, int):
            raise ValueError(f"{self.name}: read_events did not stamp '{_LINE_OFFSET_INTERNAL}' on the yielded event; cursor cannot advance")
        # Persist the NEXT line we should read so resume picks up at the
        # event immediately after the last one we surfaced.
        return {_LINE_OFFSET_KEY: offset + 1}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _path_for_session(self, root_uri: str, external_session_id: str) -> Path:
        root = root_uri_to_path(root_uri)
        for path in codex_vendor.iter_session_files(root):
            if codex_vendor.external_session_id_for(path) == external_session_id:
                return path
        raise FileNotFoundError(f"{self.name}: no rollout file under {root} matches stem {external_session_id!r}")


def _parse_optional_int(cursor_payload: dict[str, object] | None, key: str) -> int | None:
    if cursor_payload is None:
        return None
    value = cursor_payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError(f"cursor field {key!r} must be int or null, got {value!r}")
    return value


def _qualify_source(*, ledger: Any, source_kind: str) -> dict[str, object]:
    """Run one source-local, bounded retrieval proof without exposing content."""
    source_id, root_uri = _require_registered_source(ledger, source_kind)
    if not root_uri_to_path(root_uri).exists():
        return {
            "source_registered": True,
            "source_kind": source_kind,
            "backfill_count": 0,
            "sample_content_retrieved": False,
        }
    backfill_count = _run_nonempty_backfill(ledger, source_id, source_kind)
    _require_retrievable_content(ledger, source_kind)
    return {
        "source_registered": True,
        "source_kind": source_kind,
        "backfill_count": backfill_count,
        "sample_content_retrieved": True,
    }


def _require_registered_source(ledger: Any, source_kind: str) -> tuple[str, str]:
    listed = ledger.list_sources()
    sources = listed.get("sources") if isinstance(listed, dict) else None
    if not isinstance(sources, list):
        raise RuntimeError("session_ledger_service.list_sources returned no sources list")
    source = _registered_source(sources, source_kind)
    if source is None:
        raise RuntimeError(f"no enabled registered source for {source_kind!r}")
    source_id = source.get("source_id")
    root_uri = source.get("root_uri")
    if not isinstance(source_id, str) or not source_id:
        raise RuntimeError(f"registered source for {source_kind!r} has no source id")
    if not isinstance(root_uri, str) or not root_uri:
        raise RuntimeError(f"registered source for {source_kind!r} has no filesystem root URI")
    return source_id, root_uri


def _run_nonempty_backfill(ledger: Any, source_id: str, source_kind: str) -> int:
    backfill = ledger.poll_source(source_id)
    if not isinstance(backfill, dict):
        raise RuntimeError("session_ledger_service.poll_source returned a malformed report")
    backfill_count = backfill.get("events_persisted")
    if not isinstance(backfill_count, int) or backfill_count <= 0:
        raise RuntimeError(f"source {source_kind!r} backfill produced no persisted events")
    return backfill_count


def _require_retrievable_content(ledger: Any, source_kind: str) -> None:
    sessions_result = ledger.list_sessions(source_kind=source_kind, limit=1)
    sessions = sessions_result.get("sessions") if isinstance(sessions_result, dict) else None
    if not isinstance(sessions, list) or not sessions:
        raise RuntimeError(f"source {source_kind!r} yielded no retrievable sessions")
    session_id = _session_id(sessions[0])
    timeline = ledger.get_session_timeline(session_id=session_id, limit=1)
    events = timeline.get("events") if isinstance(timeline, dict) else None
    if not isinstance(events, list) or not any(_has_content(event) for event in events):
        raise RuntimeError(f"source {source_kind!r} yielded no retrievable content")


def _registered_source(sources: list[object], source_kind: str) -> dict[str, object] | None:
    for source in sources:
        if not isinstance(source, dict) or source.get("source_kind") != source_kind:
            continue
        if source.get("enabled") is True:
            return source
    return None


def _session_id(session: object) -> str:
    if not isinstance(session, dict):
        raise RuntimeError("session_ledger_service.list_sessions returned a malformed row")
    session_id = session.get("session_id", session.get("id"))
    if not isinstance(session_id, str) or not session_id:
        raise RuntimeError("session_ledger_service.list_sessions row has no session id")
    return session_id


def _has_content(event: object) -> bool:
    if not isinstance(event, dict):
        return False
    return bool(event.get("content_text")) or bool(event.get("content_json"))


def _qualification_failure(exc: Exception) -> dict[str, object]:
    return {
        "action_status": ActionStatus.FAILED.value,
        "data": {},
        "actions": [],
        "error": {"code": "source_qualification_failed", "message": str(exc)},
        "timestamp": datetime.now(UTC).isoformat(),
    }


__all__ = ["CodexFilesystemSessionSourcePlugin"]
