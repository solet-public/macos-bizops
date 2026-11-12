"""Codex CLI rollout-JSONL parser + normalizer (vendor='codex').

Codex's rollout files at ``~/.codex/sessions/YYYY/MM/DD/rollout-<iso>-<uuid>.jsonl``
and ``~/.codex/archived_sessions/rollout-<iso>-<uuid>.jsonl`` are line-delimited
JSON. Each line carries::

    {"timestamp": "<ISO8601>", "type": "<line_type>", "payload": {...}}

Recognized ``type`` values (from a real 2025-10-15 rollout):

* ``session_meta``    — first line of each file; identifies the Codex session
                        (``payload.id`` is the Codex UUID, ``payload.cwd`` is the
                        working directory). Yields NO ledger event; surfaces
                        session-level metadata via :func:`read_session_meta`.
* ``response_item``   — actual conversation surface. ``payload.type`` in
                        ``{message, reasoning, function_call,
                        function_call_output, custom_tool_call,
                        custom_tool_call_output}`` → produces one
                        :class:`RawSessionEvent`.
* ``event_msg``       — token_count with dict info yields a usage SYSTEM event;
                        other runtime messages are skipped.
* ``turn_context``    — supplies the preceding model; yields no event.
* ``world_state`` / ``token_usage_record`` — non-conversation records, skipped;
                        token_count remains the only rollout usage meter.

Unknown string line/item/content kinds advise and leave understood events
intact. Malformed JSON and malformed known events still raise ValueError.
Peer communication is retained as SYSTEM content; peer metadata is skipped.
Tool discovery calls are retained as TOOL_CALL events.

The pushed adapter receives chunks as ``{"external_session_id": str,
"events": [<line-shaped dicts>]}`` JSON. The filesystem adapter walks
on-disk rollout files. Both call :func:`normalize_raw` to produce the
canonical :class:`NormalizedSessionEvent`.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ananta.llm.session_ledger.session_failures import record_unknown_line
from ananta.llm.session_ledger.types import (
    EventType,
    MessageRole,
    NormalizedSessionEvent,
    RawSessionEvent,
)

# ───────────────────────────────────────────────────────────────────────────
# Filename / path discovery
# ───────────────────────────────────────────────────────────────────────────

ROLLOUT_FILENAME_GLOB = "rollout-*.jsonl"
DEFAULT_ROOT_DIR = "~/.codex/sessions"
ARCHIVED_SUBDIR = "archived_sessions"
logger = logging.getLogger(__name__)


def iter_session_files(root_dir: Path) -> Iterator[Path]:
    """Yield rollout-JSONL paths under ``root_dir`` in alphabetical order.

    Codex names files ``rollout-<ISO timestamp>-<uuid>.jsonl``; lexical sort
    gives a stable walk, without excluding older files that can still grow.
    Walks both the dated ``YYYY/MM/DD/`` tree AND a sibling
    ``archived_sessions/`` directory if it exists; archived files are
    yielded after live ones (alphabetically merged).
    """
    if not root_dir.exists() or not root_dir.is_dir():
        return
    candidates: list[Path] = []
    for path in root_dir.rglob(ROLLOUT_FILENAME_GLOB):
        if path.is_file():
            candidates.append(path)
    archived_root = root_dir.parent / ARCHIVED_SUBDIR
    if archived_root.exists() and archived_root.is_dir():
        for path in archived_root.glob(ROLLOUT_FILENAME_GLOB):
            if path.is_file():
                candidates.append(path)
    for path in sorted(candidates, key=lambda p: p.name):
        yield path


def external_session_id_for(path: Path) -> str:
    """The session's external id is the rollout filename stem (no `.jsonl`).

    Spec §8.4: external_session_id is the 'Codex filename stem'.
    """
    return path.stem


# ───────────────────────────────────────────────────────────────────────────
# Per-file parsing
# ───────────────────────────────────────────────────────────────────────────

# Top-level `type` values we recognize. Unknown strings advise and continue.
#
# ``compacted`` is the context-compaction marker Codex started emitting after
# 2026-01-14 — its ``payload.replacement_history`` carries the messages that
# were summarized; those messages were ALREADY in the rollout file above this
# line, so the marker itself adds no operator-visible signal and is safe to
# skip. Per 2026-05-31 dispatch ``codex_local_recency_blind``: omitting this
# from the recognized set caused every post-Jan-14 rollout file containing a
# ``compacted`` line to raise ValueError, the per-session try/except in
# :class:`SessionLedgerImporter` silently skipped the session, and the
# discovery cursor advanced regardless → 4.5 months of orphaned sessions.
_RECOGNIZED_LINE_TYPES: frozenset[str] = frozenset({"session_meta", "response_item", "event_msg", "turn_context", "compacted", "world_state", "token_usage_record", "inter_agent_communication_metadata", "inter_agent_communication"})
_SKIPPED_LINE_TYPES: frozenset[str] = frozenset({"event_msg", "turn_context", "compacted", "world_state", "token_usage_record", "inter_agent_communication_metadata"})

# `response_item.payload.type` we recognize. Unknown strings advise and continue.
#
# ``web_search_call`` is Codex's built-in web-search tool surface (added
# after 2026-01-14 alongside ``compacted``). Unlike ``function_call`` the
# payload carries NO ``call_id`` / ``id`` field, so :func:`_line_to_raw_event`
# synthesizes a deterministic ``vendor_event_id = web_search:<ts>:<line>``
# (timestamp alone collides — May 7 fixtures show 6 lines at the same
# millisecond) and :func:`_normalize_web_search_call` maps the line onto a
# TOOL_CALL with ``tool_name="web_search"``.
_RECOGNIZED_ITEM_TYPES: frozenset[str] = frozenset(
    {
        "message",
        "reasoning",
        "function_call",
        "function_call_output",
        "custom_tool_call",
        "custom_tool_call_output",
        "web_search_call",
        "tool_search_call",
    }
)


@dataclass(frozen=True, slots=True)
class CodexSessionMeta:
    """Per-file session header extracted from the first ``session_meta`` line."""

    session_uuid: str
    cwd: str | None
    started_at: datetime


def read_session_meta(path: Path) -> CodexSessionMeta:
    """Read the FIRST line of ``path`` as the session_meta header.

    Raises ``ValueError`` if the first line is missing the expected shape
    (no fallback to mtime-derived metadata — see KB "Critical Development Guidelines v2").
    """
    with path.open("r", encoding="utf-8") as fh:
        first = fh.readline()
    if not first:
        raise ValueError(f"empty rollout file: {path}")
    obj = _load_line(first, path=path, line_no=1)
    if obj.get("type") != "session_meta":
        raise ValueError(f"{path}: expected first line type='session_meta', got {obj.get('type')!r}")
    payload = _require_dict(obj, "payload", path, 1)
    uuid = payload.get("id")
    if not isinstance(uuid, str) or not uuid:
        raise ValueError(f"{path}: session_meta.payload.id missing or non-string")
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    timestamp_raw = obj.get("timestamp")
    if not isinstance(timestamp_raw, str):
        raise ValueError(f"{path}: session_meta line lacks string 'timestamp'")
    started_at = _parse_iso(timestamp_raw)
    return CodexSessionMeta(session_uuid=uuid, cwd=cwd, started_at=started_at)


def iter_events_from_path(
    path: Path,
    *,
    start_line: int = 0,
) -> Iterator[tuple[int, RawSessionEvent]]:
    """Yield ``(line_offset, RawSessionEvent)`` for every event-bearing line.

    ``start_line`` is 0-based; the first emitted line will have
    ``line_offset >= start_line``. ``session_meta`` lines are skipped.
    ``turn_context`` supplies model context; token_count runtime messages
    yield usage events. Other runtime messages are skipped.
    Unknown string top-level/item types advise; malformed known records raise.
    """
    external_session_id = external_session_id_for(path)
    context = _ParseContext()
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line_no, raw_line in enumerate(fh):
                if line_no < start_line:
                    _read_prefix_context(raw_line, context)
                    continue
                if not raw_line.strip():
                    continue
                obj = _load_line(raw_line, path=path, line_no=line_no + 1)
                event = _parse_with_context(
                    obj=obj,
                    external_session_id=external_session_id,
                    source=str(path),
                    line_no=line_no + 1,
                    context=context,
                )
                if event is not None:
                    yield line_no, event
    finally:
        context.report()


# ───────────────────────────────────────────────────────────────────────────
# Pushed-chunk parsing
# ───────────────────────────────────────────────────────────────────────────


def parse_chunk(chunk_text: str) -> Iterator[RawSessionEvent]:
    """Parse one pushed Codex chunk.

    Wire shape (operator-side Codex pusher SHIPS this):

        {"external_session_id": "<filename-stem-or-uuid>",
         "events": [
             {"timestamp": "...", "type": "response_item", "payload": {...}},
             ...
         ]}

    Each ``events[i]`` follows the SAME line schema as the filesystem
    rollout JSONL (top-level ``type`` + ``payload`` + ``timestamp``). The
    pushed plugin is RECEIVE-ONLY; the chunk format is the Codex-side
    pusher's contract and lives in v2 plan §3.
    """
    try:
        envelope = json.loads(chunk_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"codex pushed chunk is not valid JSON: {exc}") from exc
    if not isinstance(envelope, dict):
        raise ValueError("codex pushed chunk must be a JSON object")
    external_session_id = envelope.get("external_session_id")
    if not isinstance(external_session_id, str) or not external_session_id:
        raise ValueError("codex pushed chunk requires non-empty 'external_session_id' field")
    events = envelope.get("events")
    if not isinstance(events, list):
        raise ValueError("codex pushed chunk requires 'events' list")
    yield from _parse_chunk_events(events, external_session_id)


def _parse_chunk_events(events: list[object], external_session_id: str) -> Iterator[RawSessionEvent]:
    context = _ParseContext()
    try:
        for idx, item in enumerate(events):
            if not isinstance(item, dict):
                raise ValueError(f"codex pushed events[{idx}] is not a dict")
            event = _parse_with_context(
                obj=item,
                external_session_id=external_session_id,
                source=f"pushed_chunk[{idx}]",
                line_no=idx + 1,
                context=context,
            )
            if event is not None:
                yield event
    finally:
        context.report()


# ───────────────────────────────────────────────────────────────────────────
# Normalization (RawSessionEvent → NormalizedSessionEvent)
# ───────────────────────────────────────────────────────────────────────────


def normalize_raw(raw: RawSessionEvent) -> NormalizedSessionEvent:
    """Map a Codex RawSessionEvent to the canonical NormalizedSessionEvent.

    Dispatches on ``raw.payload['_codex_item_type']`` (set by the parser).
    """
    item_type = raw.payload.get("_codex_item_type")
    if item_type == "token_count":
        return _normalize_token_count(raw)
    if item_type == "message":
        return _normalize_message(raw)
    if item_type == "reasoning":
        return _normalize_reasoning(raw)
    if item_type in {"function_call", "custom_tool_call"}:
        return _normalize_tool_call(raw)
    if item_type in {"function_call_output", "custom_tool_call_output"}:
        return _normalize_tool_result(raw)
    if item_type == "web_search_call":
        return _normalize_web_search_call(raw)
    if item_type in {"tool_search_call", "inter_agent_communication"}:
        return _normalize_discovery_or_peer(raw, item_type)
    raise ValueError(f"normalize_raw: unrecognized _codex_item_type {item_type!r} in payload")


# ───────────────────────────────────────────────────────────────────────────
# Internals
# ───────────────────────────────────────────────────────────────────────────


@dataclass(slots=True)
class _ParseContext:
    model: str | None = None
    null_info: int = 0
    model_missing: int = 0

    def report(self) -> None:
        """One aggregate diagnostic per consumed parse call, including zero counts."""
        logger.debug("codex token_count: null-info skipped %d, model-missing %d", self.null_info, self.model_missing)


def _update_model(obj: dict[str, Any], context: _ParseContext) -> None:
    if obj.get("type") == "turn_context":
        payload = obj.get("payload")
        context.model = _optional_str(payload.get("model")) if isinstance(payload, dict) else None


def _read_prefix_context(raw_line: str, context: _ParseContext) -> None:
    """Scan prefix text without parsing unrelated or malformed historical lines."""
    if "turn_context" not in raw_line:
        return
    try:
        obj = json.loads(raw_line)
    except json.JSONDecodeError:
        return
    if isinstance(obj, dict):
        _update_model(obj, context)


def _parse_with_context(
    *,
    obj: dict[str, Any],
    external_session_id: str,
    source: str,
    line_no: int,
    context: _ParseContext,
) -> RawSessionEvent | None:
    _update_model(obj, context)
    payload = obj.get("payload")
    if obj.get("type") == "event_msg" and isinstance(payload, dict) and payload.get("type") == "token_count":
        return _token_count_event(obj, payload, external_session_id, line_no, context)
    return _line_to_raw_event(obj=obj, external_session_id=external_session_id, source=source, line_no=line_no)


def _token_count_event(
    obj: dict[str, Any],
    payload: dict[str, Any],
    external_session_id: str,
    line_no: int,
    context: _ParseContext,
) -> RawSessionEvent | None:
    info = payload.get("info")
    if not isinstance(info, dict):
        context.null_info += 1
        return None
    if context.model is None:
        context.model_missing += 1
    timestamp_raw = obj.get("timestamp")
    if not isinstance(timestamp_raw, str):
        raise ValueError("codex token_count line lacks string 'timestamp'")
    vendor_event_id, parent = _resolve_vendor_ids(
        item_type="token_count",
        call_id=None,
        timestamp_raw=timestamp_raw,
        line_no=line_no,
    )
    return RawSessionEvent(
        external_session_id=external_session_id,
        payload={
            "_codex_item_type": "token_count",
            "usage": {
                "total": info.get("total_token_usage"),
                "last": info.get("last_token_usage"),
                "model_context_window": info.get("model_context_window"),
                "model": context.model,
            },
        },
        event_at=_parse_iso(timestamp_raw),
        vendor_event_id=vendor_event_id,
        vendor_parent_event_id=parent,
    )


def _normalize_token_count(raw: RawSessionEvent) -> NormalizedSessionEvent:
    return NormalizedSessionEvent(
        external_session_id=raw.external_session_id,
        event_type=EventType.SYSTEM,
        role=MessageRole.SYSTEM,
        content_text=None,
        content_json={"subtype": "codex_token_count"},
        event_at=raw.event_at,
        vendor_event_id=raw.vendor_event_id,
        vendor_parent_event_id=raw.vendor_parent_event_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
        usage_json=raw.payload["usage"],
    )


def _line_to_raw_event(
    *,
    obj: dict[str, Any],
    external_session_id: str,
    source: str,
    line_no: int,
) -> RawSessionEvent | None:
    """Convert one parsed line to a RawSessionEvent, or None to skip.

    ``session_meta`` returns None (caller already consumed it via
    :func:`read_session_meta`). ``event_msg`` / ``turn_context`` return None
    (known metadata). Unknown string kinds advise and allow later known lines.
    """
    top_type = obj.get("type")
    if not isinstance(top_type, str):
        raise ValueError(f"{source}:{line_no} line lacks string 'type'")
    if top_type not in _RECOGNIZED_LINE_TYPES:
        record_unknown_line(top_type, external_session_id)
        return None
    if top_type == "session_meta":
        return None
    if top_type in _SKIPPED_LINE_TYPES:
        return None
    # Peer payloads have no nested item type; their top-level kind identifies
    # them. Keep the full payload rather than discarding cross-agent content.
    payload = _require_dict(obj, "payload", source, line_no)
    item_type = top_type if top_type == "inter_agent_communication" else payload.get("type")
    if not isinstance(item_type, str):
        raise ValueError(f"{source}:{line_no} response_item.payload lacks string 'type'")
    if item_type not in _RECOGNIZED_ITEM_TYPES and item_type != "inter_agent_communication":
        record_unknown_line(f"response_item.{item_type}", external_session_id)
        return None
    timestamp_raw = obj.get("timestamp")
    if not isinstance(timestamp_raw, str):
        raise ValueError(f"{source}:{line_no} line lacks string 'timestamp'")
    event_at = _parse_iso(timestamp_raw)
    # Stash the item type in payload so normalize_raw can dispatch without
    # re-parsing the line.
    augmented = dict(payload)
    augmented["_codex_item_type"] = item_type
    call_id = _optional_str(payload.get("call_id"))
    vendor_event_id, vendor_parent_event_id = _resolve_vendor_ids(
        item_type=item_type,
        call_id=call_id,
        timestamp_raw=timestamp_raw,
        line_no=line_no,
    )
    return RawSessionEvent(
        external_session_id=external_session_id,
        payload=augmented,
        event_at=event_at,
        vendor_event_id=vendor_event_id,
        vendor_parent_event_id=vendor_parent_event_id,
    )


def _resolve_vendor_ids(
    *,
    item_type: str,
    call_id: str | None,
    timestamp_raw: str,
    line_no: int,
) -> tuple[str | None, str | None]:
    """Compute (vendor_event_id, vendor_parent_event_id) for a response_item.

    Three shape branches collapse here to keep ``_line_to_raw_event`` simple:

    * ``function_call_output`` / ``custom_tool_call_output`` are RESULT rows.
      Codex's wire format does NOT mint a per-item id for result lines
      (``call_id`` is the only handle), so a naive ``vendor_event_id = call_id``
      collides with the corresponding CALL row in the same session. Synthesize
      a deterministic ``<call_id>:result`` suffix so CALL and RESULT remain
      distinguishable while their linkage stays in vendor_parent_event_id.
    * ``web_search_call`` has no payload id and no payload.call_id; same-
      millisecond multi-query searches collide on timestamp alone (fixtures
      from 2026-05-07 carry 6 web_search_call lines at one ts), so combine
      the ISO timestamp with the rollout-file line number. Idempotency holds
      across re-polls because the rollout file is append-only.
    * Default: ``vendor_event_id = call_id``, no parent.
    """
    if item_type in {"function_call_output", "custom_tool_call_output"}:
        result_id = f"{call_id}:result" if call_id is not None else None
        return result_id, call_id
    if item_type == "web_search_call":
        return f"web_search:{timestamp_raw}:{line_no}", None
    if item_type == "token_count":
        return f"token_count:{timestamp_raw}:{line_no}", None
    if item_type in {"tool_search_call", "inter_agent_communication"}:
        return call_id or f"{item_type}:{timestamp_raw}:{line_no}", None
    return call_id, None


def _normalize_discovery_or_peer(raw: RawSessionEvent, item_type: str) -> NormalizedSessionEvent:
    """Preserve newly documented vendor records without inventing fields."""
    peer = item_type == "inter_agent_communication"
    payload = {key: value for key, value in raw.payload.items() if not key.startswith("_")}
    content_json = {"subtype": item_type, **payload} if peer else {"tool_name": "tool_search", "tool_call_id": raw.vendor_event_id, "arguments": payload.get("arguments"), "status": payload.get("status")}
    return NormalizedSessionEvent(
        external_session_id=raw.external_session_id,
        event_type=EventType.SYSTEM if peer else EventType.TOOL_CALL,
        role=MessageRole.SYSTEM if peer else MessageRole.ASSISTANT,
        content_text=_optional_str(payload.get("content")) if peer else None,
        content_json=content_json,
        event_at=raw.event_at,
        vendor_event_id=raw.vendor_event_id,
        vendor_parent_event_id=raw.vendor_parent_event_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


def _normalize_message(raw: RawSessionEvent) -> NormalizedSessionEvent:
    payload = raw.payload
    role_str = payload.get("role")
    if not isinstance(role_str, str):
        raise ValueError("codex message payload missing string 'role'")
    role = _map_codex_role(role_str)
    content_parts = payload.get("content")
    if not isinstance(content_parts, list):
        raise ValueError("codex message payload missing list 'content'")
    text = _extract_text_parts(content_parts)
    return NormalizedSessionEvent(
        external_session_id=raw.external_session_id,
        event_type=EventType.TOOL_RESULT if role is MessageRole.TOOL else EventType.MESSAGE,
        role=role,
        content_text=text or None,
        # Tool messages can carry correlation and non-text vendor fields.
        # Retain them verbatim; a message id is not evidence of a tool-use id.
        content_json=dict(payload) if role is MessageRole.TOOL else (None if text else {"raw_content": content_parts}),
        event_at=raw.event_at,
        vendor_event_id=raw.vendor_event_id,
        vendor_parent_event_id=raw.vendor_parent_event_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


def _normalize_reasoning(raw: RawSessionEvent) -> NormalizedSessionEvent:
    """Reasoning lines surface as SYSTEM events with the summary text.

    Per spec §9 SYSTEM allows content_text or content_json. Codex reasoning
    payloads carry a ``summary`` list (``[{"type":"summary_text","text":...}]``)
    and an opaque ``encrypted_content`` string we do NOT persist (it would
    trip the high-entropy detector and gain nothing for operator review).
    """
    summary = raw.payload.get("summary")
    text = ""
    if isinstance(summary, list):
        text_chunks: list[str] = []
        for entry in summary:
            if isinstance(entry, dict) and entry.get("type") == "summary_text":
                value = entry.get("text")
                if isinstance(value, str):
                    text_chunks.append(value)
        text = "\n".join(text_chunks)
    return NormalizedSessionEvent(
        external_session_id=raw.external_session_id,
        event_type=EventType.SYSTEM,
        role=MessageRole.SYSTEM,
        content_text=text or None,
        content_json=None if text else {"summary": summary},
        event_at=raw.event_at,
        vendor_event_id=raw.vendor_event_id,
        vendor_parent_event_id=raw.vendor_parent_event_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


def _normalize_tool_call(raw: RawSessionEvent) -> NormalizedSessionEvent:
    payload = raw.payload
    tool_name = payload.get("name")
    if not isinstance(tool_name, str) or not tool_name:
        raise ValueError("codex function_call payload missing non-empty 'name'")
    arguments_raw = payload.get("arguments")
    arguments: Any
    if isinstance(arguments_raw, str):
        try:
            arguments = json.loads(arguments_raw) if arguments_raw else {}
        except json.JSONDecodeError:
            arguments = {"_raw": arguments_raw}
    else:
        arguments = arguments_raw if arguments_raw is not None else {}
    call_id = payload.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        raise ValueError("codex function_call payload missing non-empty 'call_id'")
    return NormalizedSessionEvent(
        external_session_id=raw.external_session_id,
        event_type=EventType.TOOL_CALL,
        role=MessageRole.ASSISTANT,
        content_text=None,
        content_json={
            "tool_name": tool_name,
            "tool_call_id": call_id,
            "arguments": arguments,
        },
        event_at=raw.event_at,
        vendor_event_id=call_id,
        vendor_parent_event_id=raw.vendor_parent_event_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


def _normalize_tool_result(raw: RawSessionEvent) -> NormalizedSessionEvent:
    payload = raw.payload
    call_id = payload.get("call_id")
    if not isinstance(call_id, str) or not call_id:
        raise ValueError("codex function_call_output payload missing non-empty 'call_id'")
    output_raw = payload.get("output")
    output: Any = output_raw
    if isinstance(output_raw, str):
        try:
            output = json.loads(output_raw)
        except json.JSONDecodeError:
            output = output_raw  # keep as string when not JSON
    text = output if isinstance(output, str) else None
    content_json: dict[str, Any] | None
    if text is not None:
        content_json = {"tool_call_id": call_id}
    else:
        content_json = {"tool_call_id": call_id, "output": output}
    return NormalizedSessionEvent(
        external_session_id=raw.external_session_id,
        event_type=EventType.TOOL_RESULT,
        role=MessageRole.TOOL,
        content_text=text,
        content_json=content_json,
        event_at=raw.event_at,
        vendor_event_id=raw.vendor_event_id,
        vendor_parent_event_id=call_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


def _normalize_web_search_call(raw: RawSessionEvent) -> NormalizedSessionEvent:
    """Codex built-in ``web_search_call`` line → TOOL_CALL event.

    Vendor-drift-tolerant. Two shapes coexist on disk:

    * Current (post mid-2026)::

        {"type": "web_search_call",
         "status": "completed",
         "action": {"type": "search",
                    "query": "<single query string>",
                    "queries": ["<query>", ...]}}

    * Earlier (e.g. spring 2026)::

        {"type": "web_search_call",
         "status": "completed"}

    The earlier shape carries no ``action`` payload; we still want to record
    that a web search happened (the TOOL_CALL itself is the load-bearing
    fact for downstream tool-call projection + replay), so the parser
    degrades gracefully: ``arguments`` is empty when ``action`` is absent
    or not a dict. Status is preserved on either path.

    Unlike ``function_call`` there is no ``call_id`` — :func:`_line_to_raw_event`
    already synthesized ``vendor_event_id = web_search:<ts>:<line>`` so the
    event row carries a deterministic, idempotent id.
    """
    payload = raw.payload
    action = payload.get("action")
    arguments: dict[str, Any] = {}
    if isinstance(action, dict):
        query = action.get("query")
        queries = action.get("queries")
        if isinstance(query, str):
            arguments["query"] = query
        if isinstance(queries, list):
            arguments["queries"] = [q for q in queries if isinstance(q, str)]
    status = payload.get("status")
    if isinstance(status, str):
        arguments["status"] = status
    return NormalizedSessionEvent(
        external_session_id=raw.external_session_id,
        event_type=EventType.TOOL_CALL,
        role=MessageRole.ASSISTANT,
        content_text=None,
        content_json={
            "tool_name": "web_search",
            "tool_call_id": raw.vendor_event_id,
            "arguments": arguments,
        },
        event_at=raw.event_at,
        vendor_event_id=raw.vendor_event_id,
        vendor_parent_event_id=raw.vendor_parent_event_id,
        attachment_blob_upload=None,
        attachment_mime_type=None,
        attachment_filename=None,
    )


def _map_codex_role(role: str) -> MessageRole:
    # ``developer`` is the role Codex uses for operator-injected instruction
    # prose (e.g. <permissions instructions>, <collaboration_mode>). Semantically
    # equivalent to ``user`` from the ledger's perspective — the human operator
    # is the one supplying the directive. Operator-approved 2026-06-01 dispatch
    # ``codex_developer_role`` (same vendor-drift class as the ``compacted`` +
    # ``web_search_call`` + ``agent-name`` fixes earlier).
    if role in {"user", "developer"}:
        return MessageRole.USER
    if role == "assistant":
        return MessageRole.ASSISTANT
    if role == "system":
        return MessageRole.SYSTEM
    if role == "tool":
        return MessageRole.TOOL
    raise ValueError(f"codex role {role!r} cannot be mapped to MessageRole")


def _extract_text_parts(content_parts: list[object]) -> str:
    """Concatenate ``text`` fields from Codex content parts.

    Codex uses ``{"type": "input_text" | "output_text" | "text", "text": "..."}``
    parts. Anything else is ignored (operator-side noise).
    """
    chunks: list[str] = []
    for part in content_parts:
        if not isinstance(part, dict):
            continue
        kind = part.get("type")
        if kind not in {"input_text", "output_text", "text"}:
            record_unknown_line(f"content.{kind}")
            continue
        value = part.get("text")
        if isinstance(value, str):
            chunks.append(value)
    return "\n".join(chunks)


def _load_line(line: str, *, path: Path | str, line_no: int) -> dict[str, Any]:
    try:
        obj = json.loads(line)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}:{line_no} invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise ValueError(f"{path}:{line_no} line is not a JSON object")
    return obj


def _require_dict(obj: dict[str, Any], field: str, source: Path | str, line_no: int) -> dict[str, Any]:
    value = obj.get(field)
    if not isinstance(value, dict):
        raise ValueError(f"{source}:{line_no} missing dict field {field!r}")
    return value


def _parse_iso(value: str) -> datetime:
    text = value
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _optional_str(value: object) -> str | None:
    if isinstance(value, str) and value:
        return value
    return None


__all__ = [
    "ARCHIVED_SUBDIR",
    "CodexSessionMeta",
    "DEFAULT_ROOT_DIR",
    "ROLLOUT_FILENAME_GLOB",
    "external_session_id_for",
    "iter_events_from_path",
    "iter_session_files",
    "normalize_raw",
    "parse_chunk",
    "read_session_meta",
]
