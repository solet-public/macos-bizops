"""Local Claude Code JSONL transcripts as an LLM session ledger source.

Spec §17.3 M3 (filesystem variant). Walks
``<root_uri>/<encoded_cwd>/<session_id>.jsonl`` and surfaces every line
as a normalized session event. ``root_uri`` is yaml-authoritative (per
plugin authoring traps #10); the smoke overrides it through the
profile-config bridge so mutating tests run against a tmpdir, not
``~/.claude/projects/``.

Cursor semantics:

* Discovery cursor — ``{"mtime_high_water_iso": "<ISO>"}``. Files whose
  mtime is strictly greater are re-surfaced (append-only producer:
  unchanged files never re-emerge).
* Per-session read cursor — ``{"byte_offset": N}``. Per-file byte
  offset of the last fully-drained line. Append-only producer keeps the
  offset stable across passes; partial trailing line is left until the
  next tick (no fallback). Per-line lock-step ordering is preserved
  because every Claude Code JSONL line ends in ``\\n``.

Acceptance (spec §17.3): the vendor parser at
``ananta.llm.session_ledger.vendor.claude_code`` emits TOOL_CALL / TOOL_RESULT
events with the right ``vendor_event_id`` / ``vendor_parent_event_id``
discipline so the importer's tool-call projection resolves to
``status='succeeded'`` automatically.

Activated polling uses the shared filesystem metadata policy: newest-first
(mtime_ns, relative path) sweep continuation, per-session completed versions,
explicit EOF reset, and leased oversized selection. Legacy direct discovery
and vendor cursor methods below remain unchanged; the importer uses the safe
poll hooks. New task/ambient snapshots replay completely, and rollout offsets
continue only a proved same-inode append. No vendor event identity changes.
"""

from __future__ import annotations

import logging
import math
import os
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic, sleep
from typing import Any, ClassVar, cast

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
from ananta.llm.session_ledger.poll_advisories import (
    begin_claude_read,
    claude_line_context,
    complete_claude_read,
    completed_claude_read,
)
from ananta.llm.session_ledger.root_uri import root_uri_to_path
from ananta.llm.session_ledger.types import (
    EventType,
    ExternalSessionRef,
    IngestMode,
    IngestSourceKind,
    MessageRole,
    NormalizedSessionEvent,
    RawSessionEvent,
    SessionSourceDescriptor,
    SourceVendor,
)
from ananta.llm.session_ledger.vendor import claude_code as claude_code_vendor
from ananta.llm.session_ledger.vendor.claude_code import (
    PAYLOAD_KIND_MESSAGE,
    PAYLOAD_KIND_SYSTEM,
    PAYLOAD_KIND_TOOL_CALL,
    PAYLOAD_KIND_TOOL_RESULT,
    parse_line,
)

logger = logging.getLogger(__name__)

_CONFIG_GLOB = "glob"
_CURSOR_FIELD_MTIME = "mtime_high_water_iso"
_CURSOR_FIELD_OFFSET = "byte_offset"


# Admission stays on the existing single-slot ledger-poll worker. Bound its
# enumeration slices even when one directory is huge or the clock is coarse.
def _sweep_setting(name: str, default: float, *, integral: bool = False) -> float:
    """Read a positive finite startup setting; malformed configuration names its cause."""
    raw = os.environ.get(name, str(default))
    try:
        value = float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be positive and finite, got {raw!r}") from exc
    if not math.isfinite(value) or value <= 0 or integral and not value.is_integer():
        raise ValueError(f"{name} must be positive, finite and {'integral' if integral else 'numeric'}, got {raw!r}")
    return value


_DEFAULT_SWEEP_ENTRY_LIMIT = 128
_DEFAULT_SWEEP_SLICE_SECONDS = 0.002
_DEFAULT_SWEEP_YIELD_SECONDS = 0.001
_DEFAULT_SWEEP_INTERVAL_SECONDS = 1.0
_SWEEP_ENTRY_LIMIT = int(_sweep_setting("SOLET_CLAUDE_SWEEP_ENTRY_LIMIT", _DEFAULT_SWEEP_ENTRY_LIMIT, integral=True))
_SWEEP_SLICE_SECONDS = _sweep_setting("SOLET_CLAUDE_SWEEP_SLICE_SECONDS", _DEFAULT_SWEEP_SLICE_SECONDS)
_SWEEP_YIELD_SECONDS = _sweep_setting("SOLET_CLAUDE_SWEEP_YIELD_SECONDS", _DEFAULT_SWEEP_YIELD_SECONDS)
_SWEEP_INTERVAL_SECONDS = _sweep_setting("SOLET_CLAUDE_SWEEP_INTERVAL_SECONDS", _DEFAULT_SWEEP_INTERVAL_SECONDS)


class _SweepBudget:
    """Yield the worker between bounded entry/time slices, never truncate a sweep."""

    def __init__(self) -> None:
        self._entries = 0
        self._deadline = monotonic() + _SWEEP_SLICE_SECONDS

    def checkpoint(self) -> None:
        self._entries += 1
        if self._entries < _SWEEP_ENTRY_LIMIT and monotonic() < self._deadline:
            return
        sleep(_SWEEP_YIELD_SECONDS)
        self._entries = 0
        self._deadline = monotonic() + _SWEEP_SLICE_SECONDS


class ClaudeCodeFilesystemSessionSourcePlugin(
    PluginBase,
    LLMSessionSourceInterface,
    FilesystemPollingMixin,
    EdgeProcessProvider,
):
    """Walks ``~/.claude/projects/`` and surfaces each ``.jsonl`` as a session."""

    supports_whole_session_poll: ClassVar[bool] = True

    def __init__(self) -> None:
        super().__init__()
        self.name = "claude_code_filesystem_session_source_plugin"
        self._sweep_lock = threading.Lock()
        self._next_sweep_at: dict[Path, float] = {}

    # ------------------------------------------------------------------
    # PluginBase lifecycle
    # ------------------------------------------------------------------

    def prepare_for_readiness(self) -> None:
        # Config-driven ``root_uri`` is resolved lazily on first discover/read.
        # ``config_provider`` is bound later via ``initialize(config)`` per the
        # platform two-phase init contract; eager-probing here aborts the
        # plugin lifecycle before config is bound. The actual root path is
        # also resolved at discovery time so a missing directory does not
        # block readiness (the directory only exists once the user runs
        # Claude Code at least once on this machine).
        self.set_ready()

    def initialize(self, config: dict[str, object]) -> None:
        """Bind config_provider so yaml defaults + per-deployment overrides take effect."""
        from ananta.core.config.config_provider import ConfigProvider  # noqa: PLC0415

        self.config_provider = ConfigProvider(self.name, config)
        with self._sweep_lock:
            self._next_sweep_at.clear()

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        """Declare the bounded source-local qualification result contract."""
        return {
            "qualify": EdgeProcessDefinition(
                name="qualify",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                    result_type="claude_code_filesystem_session_source_qualification",
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
        output_description=("Bounded source-local Claude Code filesystem retrieval proof: {source_registered, source_kind, backfill_count, sample_content_retrieved}."),
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
            result_type="claude_code_filesystem_session_source_qualification",
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(retryable=True),
    )
    def qualify(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Prove this already-consented source can register, backfill, and retrieve."""
        del params, state
        try:
            proof = _qualify_source(
                ledger=self._require_ledger_service(),
                source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL.value,
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
        return SessionSourceDescriptor(
            source_kind=IngestSourceKind.CLAUDE_CODE_LOCAL,
            vendor=SourceVendor.CLAUDE_CODE,
            supported_modes=(IngestMode.PULLING,),
            default_pulling_root_uri="~/.claude/projects",
        )

    def normalize(self, raw: RawSessionEvent) -> NormalizedSessionEvent:
        payload = raw.payload
        kind = _require_str(payload, "kind")
        role_str = _require_str(payload, "role")
        if kind == PAYLOAD_KIND_MESSAGE:
            return NormalizedSessionEvent(
                external_session_id=raw.external_session_id,
                event_type=EventType.MESSAGE,
                role=_map_message_role(role_str),
                content_text=_optional_str(payload.get("text")),
                content_json=None,
                event_at=raw.event_at,
                vendor_event_id=raw.vendor_event_id,
                vendor_parent_event_id=raw.vendor_parent_event_id,
                attachment_blob_upload=None,
                attachment_mime_type=None,
                attachment_filename=None,
                usage_json=_optional_dict(payload.get("usage")),
            )
        if kind == PAYLOAD_KIND_TOOL_CALL:
            tool_name = _require_str(payload, "tool_name")
            tool_use_id = _require_str(payload, "tool_use_id")
            tool_input = payload.get("tool_input")
            if not isinstance(tool_input, dict):
                raise ValueError(
                    "claude_code filesystem: tool_call payload missing dict 'tool_input'",
                )
            return NormalizedSessionEvent(
                external_session_id=raw.external_session_id,
                event_type=EventType.TOOL_CALL,
                role=None,
                content_text=None,
                content_json={
                    "tool_name": tool_name,
                    "tool_use_id": tool_use_id,
                    "input": cast(dict[str, Any], tool_input),
                },
                event_at=raw.event_at,
                vendor_event_id=raw.vendor_event_id,
                vendor_parent_event_id=raw.vendor_parent_event_id,
                attachment_blob_upload=None,
                attachment_mime_type=None,
                attachment_filename=None,
            )
        if kind == PAYLOAD_KIND_TOOL_RESULT:
            return NormalizedSessionEvent(
                external_session_id=raw.external_session_id,
                event_type=EventType.TOOL_RESULT,
                role=MessageRole.TOOL,
                content_text=_optional_str(payload.get("text")) or "",
                content_json=None,
                event_at=raw.event_at,
                vendor_event_id=raw.vendor_event_id,
                vendor_parent_event_id=raw.vendor_parent_event_id,
                attachment_blob_upload=None,
                attachment_mime_type=None,
                attachment_filename=None,
            )
        if kind == PAYLOAD_KIND_SYSTEM:
            # Lift subtype into content_json so the M6 hybrid extractor
            # (operator ruling 2026-06-01 D8) can SQL-filter for
            # `away_summary` system events without re-parsing text. Field
            # is omitted entirely when the vendor saw no subtype, keeping
            # the JSON compact for pre-subtype-aware events.
            subtype = _optional_str(payload.get("subtype"))
            return NormalizedSessionEvent(
                external_session_id=raw.external_session_id,
                event_type=EventType.SYSTEM,
                role=MessageRole.SYSTEM,
                content_text=_optional_str(payload.get("text")) or "",
                content_json={"subtype": subtype} if subtype else None,
                event_at=raw.event_at,
                vendor_event_id=raw.vendor_event_id,
                vendor_parent_event_id=raw.vendor_parent_event_id,
                attachment_blob_upload=None,
                attachment_mime_type=None,
                attachment_filename=None,
            )
        raise ValueError(f"claude_code filesystem: unknown payload kind {kind!r}")

    # ------------------------------------------------------------------
    # PullingSourceMixin
    # ------------------------------------------------------------------

    def _poll_candidates(self, root_uri: str) -> Iterator[FilesystemCandidate]:
        root = self._resolve_root_uri(root_uri)
        if not root.is_dir():
            return
        with self._sweep_lock:
            remaining = self._next_sweep_at.get(root, 0.0) - monotonic()
            if remaining > 0:
                sleep(remaining)
            try:
                budget = _SweepBudget()
                for path in _poll_paths(root, root, self._resolve_glob(), budget):
                    shape = _classify_session_path(path, root)
                    if shape is None:
                        logger.warning("%s: unrecognized jsonl path shape: %s", self.name, path)
                        continue
                    candidate = observe_candidate(path, external_id=shape[1], relative_path=path.relative_to(root).as_posix(), json_lines=True)
                    if candidate is not None:
                        yield candidate
            finally:
                self._next_sweep_at[root] = monotonic() + _SWEEP_INTERVAL_SECONDS

    def _poll_ref(self, root_uri: str, candidate: FilesystemCandidate) -> ExternalSessionRef:
        root = self._resolve_root_uri(root_uri)
        shape = _classify_session_path(candidate.path, root)
        assert shape is not None
        metadata = claude_code_vendor.read_session_metadata(candidate.path)
        return ExternalSessionRef(
            external_session_id=candidate.external_id,
            vendor_session_label=metadata.agent_name,
            project_path=_decode_project_dir(shape[0].name),
            first_seen_at=datetime.fromtimestamp(candidate.path.stat().st_ctime, tz=UTC),
            originator_session_label=metadata.agent_name,
            originator_agent_instance_id=metadata.bridge_session_id,
            summary_text_seed=metadata.custom_title,
        )

    def discover_sessions(
        self,
        root_uri: str,
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[ExternalSessionRef]:
        """Walk the source's ``root_uri`` recursively for ingest-able JSONL files.

        The pre-fix single-level ``project_dir.glob(glob_pat)`` only saw root
        sessions at ``<project>/<session-uuid>.jsonl`` and silently missed
        sub-agent rollouts at
        ``<project>/<session-uuid>/subagents/agent-*.jsonl`` — 2,711 such
        files on operator's machine were un-ingested (the F-grade smoking
        gun). The new shape uses ``rglob`` with explicit path-shape
        classification:

        * **Root session** — depth-2: ``<project>/<uuid>.jsonl``. Each
          file's stem is the canonical Claude Code ``sessionId``.
        * **Subagent rollout** — depth-4:
          ``<project>/<root-uuid>/subagents/agent-*.jsonl``. Each file is
          its OWN session keyed on the agent's own ``sessionId`` line
          (the vendor parser at ``vendor/claude_code.py`` already keys
          events on ``line.sessionId``). Subagent sessions do NOT merge
          into parent session rows.
        * **Unrecognized** depth or filename shape: WARN-and-SKIP, do not
          fail-fast. ``rglob`` legitimately encounters editor temp files,
          ``.DS_Store``, and hypothetical depth-6 sub-sub-agents (out of
          M6.5 scope; a future M-section can lift the depth cap). Hard
          failure here would block ingest on every transient cruft file.
        """
        root = self._resolve_root_uri(root_uri)
        if not root.is_dir():
            return
        high_water = _parse_cursor_dt(cursor_payload, _CURSOR_FIELD_MTIME)
        glob_pat = self._resolve_glob()
        for jsonl in sorted(root.rglob(glob_pat)):
            shape = _classify_session_path(jsonl, root)
            if shape is None:
                logger.warning(
                    "%s: unrecognized jsonl path shape under projects/: %s; warn-and-skip (depth-2 root or depth-4 subagent expected)",
                    self.name,
                    jsonl,
                )
                continue
            project_dir, session_id = shape
            stat = _safe_stat(jsonl)
            if stat is None:
                continue
            mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
            if high_water is not None and mtime <= high_water:
                continue
            first_seen_at = datetime.fromtimestamp(stat.st_ctime, tz=UTC)
            project_path = _decode_project_dir(project_dir.name)
            # Per 2026-05-31 Architect ruling §3 (Option (a) per
            # Coordinator's 2026-05-31 contract lock): pre-scan the
            # rollout file for promoted session metadata. The vendor
            # parser still SKIPS these lines from event prose; this
            # extra pass extracts them once into a typed struct.
            metadata = claude_code_vendor.read_session_metadata(jsonl)
            yield ExternalSessionRef(
                external_session_id=session_id,
                vendor_session_label=metadata.agent_name,
                project_path=project_path,
                first_seen_at=first_seen_at,
                originator_session_label=metadata.agent_name,
                originator_agent_instance_id=metadata.bridge_session_id,
                summary_text_seed=metadata.custom_title,
            )

    def read_events(
        self,
        root_uri: str,
        session: ExternalSessionRef,
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[RawSessionEvent]:
        begin_claude_read(root_uri, session.external_session_id)
        yield from self._read_events(root_uri, session, cursor_payload)
        acknowledge_read(session)

    def _read_events(
        self,
        root_uri: str,
        session: ExternalSessionRef,
        cursor_payload: dict[str, object] | None,
    ) -> Iterator[RawSessionEvent]:
        jsonl = selected_path(session) or self._locate_session_file(root_uri, session)
        if jsonl is None:
            return
        offset = _parse_cursor_int(cursor_payload, _CURSOR_FIELD_OFFSET) or 0
        try:
            handle = jsonl.open("rb")
        except OSError as exc:
            raise ValueError(f"claude_code filesystem: cannot open {jsonl}: {exc}") from exc
        with handle:
            handle.seek(offset)
            while True:
                line = handle.readline()
                if not line:
                    complete_claude_read(root_uri, session.external_session_id, offset)
                    return
                if not line.endswith(b"\n"):
                    # Partial trailing line — leave it for the next tick.
                    # No fallback: refusing to parse half a record is the
                    # whole-line correctness invariant.
                    complete_claude_read(root_uri, session.external_session_id, offset)
                    return
                offset += len(line)
                try:
                    decoded = line.decode("utf-8")
                except UnicodeDecodeError as exc:
                    raise ValueError(f"claude_code filesystem: non-UTF8 line in {jsonl}: {exc}") from exc
                with claude_line_context(root_uri, session.external_session_id, offset):
                    events = parse_line(decoded)
                for event in events:
                    yield _augment_with_offset(event, offset)

    def session_discovery_cursor(
        self,
        root_uri: str,
        last_seen: ExternalSessionRef | None,
    ) -> dict[str, object]:
        if last_seen is None:
            return {_CURSOR_FIELD_MTIME: None}
        jsonl = self._locate_session_file(root_uri, last_seen)
        if jsonl is None:
            return {_CURSOR_FIELD_MTIME: last_seen.first_seen_at.isoformat()}
        stat = _safe_stat(jsonl)
        if stat is None:
            return {_CURSOR_FIELD_MTIME: last_seen.first_seen_at.isoformat()}
        mtime = datetime.fromtimestamp(stat.st_mtime, tz=UTC)
        return {_CURSOR_FIELD_MTIME: mtime.isoformat()}

    def event_read_cursor(
        self,
        root_uri: str,
        session: ExternalSessionRef,
        last_event: RawSessionEvent | None,
    ) -> dict[str, object]:
        completed = completed_claude_read(root_uri, session.external_session_id)
        if completed is not None:
            return {_CURSOR_FIELD_OFFSET: completed}
        if last_event is None:
            return {_CURSOR_FIELD_OFFSET: 0}
        offset = last_event.payload.get("_byte_offset")
        if not isinstance(offset, int):
            raise ValueError(
                "claude_code filesystem: last_event missing internal '_byte_offset'",
            )
        return {_CURSOR_FIELD_OFFSET: offset}

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _resolve_root_uri(self, root_uri: str) -> Path:
        """Resolve the polled source row's ``root_uri`` to the walk root (P1.1.E)."""
        return root_uri_to_path(root_uri)

    def _resolve_glob(self) -> str:
        provider = self.config_provider
        if provider is None:
            raise RuntimeError(
                f"{self.name}: config_provider not injected; cannot resolve glob",
            )
        raw = provider.get(_CONFIG_GLOB)
        if not isinstance(raw, str) or not raw:
            raise RuntimeError(
                f"{self.name}: yaml-default config 'glob' missing or empty",
            )
        return raw

    def _locate_session_file(
        self,
        root_uri: str,
        session: ExternalSessionRef,
    ) -> Path | None:
        """Find the JSONL on disk whose stem equals ``session.external_session_id``.

        Per 2026-06-11 M6.5 Bug 3: covers BOTH the depth-2 root-session
        shape (``<project>/<sessionId>.jsonl``) AND the depth-4 subagent
        shape (``<project>/<root-uuid>/subagents/<sessionId>.jsonl``).
        The subagent file's stem IS the subagent's own ``sessionId``;
        ``discover_sessions`` keyed the ``ExternalSessionRef`` on that
        stem, so a stem-equality match is correct for both shapes.

        Order of checks: depth-2 candidate first (the common case),
        then ``rglob`` for the depth-4 fallback. Both checks share the
        configured ``glob`` so a producer-side rename to a different
        extension still flows through one config knob.
        """
        root = self._resolve_root_uri(root_uri)
        if not root.is_dir():
            return None
        glob_pat = self._resolve_glob()
        # Depth-2 fast path (root session).
        for project_dir in root.iterdir():
            if not project_dir.is_dir():
                continue
            candidate = project_dir / f"{session.external_session_id}.jsonl"
            if candidate.is_file():
                return candidate
        # Depth-4 fallback (subagent rollout) + producer-rename fallback
        # within either depth via stem-equality match.
        for jsonl in root.rglob(glob_pat):
            if _classify_session_path(jsonl, root) is None:
                continue
            if jsonl.stem == session.external_session_id:
                return jsonl
        return None


# ---------------------------------------------------------------------------
# Helpers (module-private; no shared package between sibling plugins).
# ---------------------------------------------------------------------------


_SUBAGENT_DEPTH = 4
_ROOT_SESSION_DEPTH = 2
_SUBAGENT_DIR_NAME = "subagents"
_SUBAGENT_FILE_PREFIX = "agent-"


def _poll_paths(directory: Path, root: Path, pattern: str, budget: _SweepBudget, depth: int = 0) -> Iterator[Path]:
    """Stream only directories which can contain an accepted session shape.

    Path.rglob materializes an entire scandir before yielding any matches.
    Here no directory is materialized, and at most four handles are open.
    Like rglob, do not recurse through directory symlinks. Disappearance or
    unreadable directories remain availability advisories, never refusals.
    """
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                budget.checkpoint()
                path = Path(entry.path)
                if path.relative_to(root).full_match(f"**/{pattern}") and (not pattern.endswith("/") or entry.is_dir()):
                    yield path
                if entry.is_dir(follow_symlinks=False) and _session_directory(depth, entry.name):
                    yield from _poll_paths(path, root, pattern, budget, depth + 1)
    except OSError as exc:
        logger.warning("Claude session enumeration unavailable at %s: %s", directory, exc)


def _session_directory(parent_depth: int, name: str) -> bool:
    return parent_depth < _ROOT_SESSION_DEPTH or parent_depth == _ROOT_SESSION_DEPTH and name == _SUBAGENT_DIR_NAME


def _classify_session_path(
    jsonl: Path,
    root: Path,
) -> tuple[Path, str] | None:
    """Classify a discovered ``.jsonl`` under ``~/.claude/projects/``.

    Returns ``(project_dir, session_id)`` when the path matches a
    recognized shape, ``None`` when the shape should be warn-and-skipped.

    Recognized shapes:

    * **Root session** (depth-2): ``<project>/<uuid>.jsonl``. Returns
      ``(<project_dir>, <uuid>)``.
    * **Subagent rollout** (depth-4):
      ``<project>/<root-uuid>/subagents/agent-<uuid>.jsonl``. Returns
      ``(<project_dir>, agent-<uuid>)`` — the agent's own ``sessionId``
      is the filename stem and becomes the ``external_session_id`` of
      its own ``__session`` row.

    Anything else returns ``None``. ``rglob`` legitimately surfaces
    editor temp files, ``.DS_Store``, and hypothetical depth-6
    sub-sub-agents (future-M scope); none should fail-fast ingest.
    """
    try:
        rel_parts = jsonl.relative_to(root).parts
    except ValueError:
        return None
    if len(rel_parts) == _ROOT_SESSION_DEPTH:
        return (root / rel_parts[0], jsonl.stem)
    if len(rel_parts) == _SUBAGENT_DEPTH and rel_parts[2] == _SUBAGENT_DIR_NAME and rel_parts[3].startswith(_SUBAGENT_FILE_PREFIX):
        return (root / rel_parts[0], jsonl.stem)
    return None


def _augment_with_offset(event: RawSessionEvent, offset: int) -> RawSessionEvent:
    """Return a copy of ``event`` whose payload carries the post-line byte offset.

    The byte offset travels in the payload so the source plugin's
    ``event_read_cursor`` can extract it without the importer needing to
    know the cursor's internal shape.
    """
    new_payload = dict(event.payload)
    new_payload["_byte_offset"] = offset
    return RawSessionEvent(
        external_session_id=event.external_session_id,
        payload=new_payload,
        event_at=event.event_at,
        vendor_event_id=event.vendor_event_id,
        vendor_parent_event_id=event.vendor_parent_event_id,
    )


def _decode_project_dir(name: str) -> str | None:
    """Best-effort decode of Claude Code's encoded project-directory name.

    Claude Code encodes ``cwd`` as ``cwd.replace('/', '-')`` when picking
    the directory name. The reverse is ambiguous for paths that legitimately
    contain ``-``; we recover the absolute-path shape and surface it as
    ``project_path``. The first JSONL line still carries the authoritative
    ``cwd`` field for any consumer that needs to disambiguate.
    """
    if not name:
        return None
    if not name.startswith("-"):
        return None
    return "/" + name[1:].replace("-", "/")


def _safe_stat(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except OSError:
        return None


def _map_message_role(role: str) -> MessageRole:
    if role == "user":
        return MessageRole.USER
    if role == "assistant":
        return MessageRole.ASSISTANT
    if role == "system":
        return MessageRole.SYSTEM
    raise ValueError(
        f"claude_code filesystem: cannot map message role {role!r} to MessageRole",
    )


def _require_str(payload: dict[str, object], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"claude_code filesystem: payload missing non-empty {field!r}",
        )
    return value


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return None


def _optional_dict(value: object) -> dict[str, Any] | None:
    """T1 usage-capture lane (2026-08-05 ruling): the vendor parser's
    'usage' payload key, verbatim -- None for any non-dict value (never
    coerced, matches _optional_str's own tolerant-but-honest contract)."""
    return value if isinstance(value, dict) else None


def _parse_cursor_dt(
    cursor_payload: dict[str, object] | None,
    key: str,
) -> datetime | None:
    if cursor_payload is None:
        return None
    value = cursor_payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(
            f"claude_code filesystem: cursor field {key!r} must be ISO string or null",
        )
    s = value
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    parsed = datetime.fromisoformat(s)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _parse_cursor_int(
    cursor_payload: dict[str, object] | None,
    key: str,
) -> int | None:
    if cursor_payload is None:
        return None
    value = cursor_payload.get(key)
    if value is None:
        return None
    if not isinstance(value, int):
        raise ValueError(
            f"claude_code filesystem: cursor field {key!r} must be int or null",
        )
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


__all__ = ["ClaudeCodeFilesystemSessionSourcePlugin"]
