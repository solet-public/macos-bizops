"""``AgentMessagingService`` — orchestrates threads, messages, and turns.

The service composes:

- :class:`AgentMessagingRepository` for SQL-backed thread/message
  persistence.
- :class:`BackendRouter` for resolving a logical ``backend`` to a
  ``GuardedAgentInterface`` plugin.
- ``FlowManager`` + ``ActionFactory`` to submit
  ``plugin::agent_messaging_plugin::run_turn`` actions whose results
  are bridge-delivered through ``agent_messaging_plugin``'s own bridge
  surface.
- :class:`AgentMessagingConfig` for policy (allowed backends, message
  size caps, timeouts).

The runner process (``run_turn``) lives in ``agent_messaging_plugin``
and calls back into :meth:`AgentMessagingService.execute_turn`.

This module is **not** an HTTP layer.  Validation errors raise typed
exceptions; the calling plugin maps them to HTTP responses.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ananta.core.result_processing import ErrorProcessorKind, ResultProcessorKind
from ananta.interfaces.state_management_interface import StateManagementInterface
from ananta.services.state_service.ordered_query import normalize_sort_value

from .models import (
    AgentMessageQueued,
    AgentMessageRow,
    AgentMessagesPage,
    AgentThreadClosed,
    AgentThreadMessagesPage,
    AgentThreadOpened,
    AgentThreadRow,
    AgentThreadsPage,
    AgentThreadStatus,
    ListAgentMessagesRequest,
    ListAgentThreadsRequest,
    MessageContent,
    MessageKind,
    MessageRole,
    OpenAgentThreadRequest,
    OriginatorType,
    PeerInbox,
    PeerInboxEntry,
    PeerInboxRequest,
    PeerSendRequest,
    PeerSendResult,
    ReadThreadMessagesRequest,
    RoleSectionStatus,
    SendAgentMessageRequest,
    TextPart,
    ThreadStatus,
)
from .prompt import assemble_prompt
from .repository import (
    AgentMessagingRepository,
    NewMessage,
    RepositoryError,
    ThreadStatusUpdate,
)
from .role_binding import (
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_AGENT_INSTANCE_ID,
    COL_ROLE,
    TABLE_ROLE_BINDING,
)
from .role_cursor import (
    RoleCursorOutcome,
    RoleCursorRejectedError,
    RoleCursorScope,
    decode_role_cursor,
)
from .role_inbox import build_role_section, merge_undelivered_oldest_first
from .routing import BackendResolutionError, BackendRouter
from .schema import (
    COL_CONSUMED,
    COL_CONSUMED_AT,
    COL_EMIT_COUNT,
    COL_EMITTED_TO_AGENT_INSTANCE_ID,
    COL_ESCALATED,
    COL_ESCALATED_AT,
    COL_ESCALATION_REASON,
    COL_LAST_EMITTED_AT,
    ESCALATION_REASON_GONE,
    RECIPIENT_KIND_ROLE,
    ROLE_THREAD_PREFIX,
    TABLE_AGENT_DIRECT_WAKE,
    TABLE_AGENT_ROLE_MESSAGE,
)
from .schema import NAMESPACE as _ROLE_NAMESPACE
from .state_results import require_completed, require_records, require_updated
from .thread_cursor import decode_thread_cursor, encode_thread_cursor

if TYPE_CHECKING:
    from ananta.llm.guarded_agent.models import ExecutionParams, ExecutionResult


logger = logging.getLogger(__name__)

RUN_TURN_PROCESS_KEY = "plugin::agent_messaging_plugin::run_turn"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AgentMessagingError(Exception):
    """Base error for the service surface.

    ``code`` is a stable string suitable for inclusion in HTTP error
    payloads (e.g. ``"agent_thread_busy"``).  ``http_status`` is a
    suggestion to the calling HTTP layer; the layer is free to remap.
    """

    code: str = "agent_messaging_error"
    http_status: int = 400


class AgentMessagingDisabledError(AgentMessagingError):
    code = "agent_messaging_disabled"
    http_status = 503


class AgentThreadNotFoundError(AgentMessagingError):
    code = "agent_thread_not_found"
    http_status = 404


class AgentThreadUnauthorizedError(AgentMessagingError):
    code = "agent_thread_unauthorized"
    http_status = 403


class AgentThreadBusyError(AgentMessagingError):
    code = "agent_thread_busy"
    http_status = 409


class AgentThreadClosedError(AgentMessagingError):
    code = "agent_thread_closed"
    http_status = 409


class AgentThreadRunningError(AgentMessagingError):
    """Refusing to close a thread that has an active turn."""

    code = "agent_thread_running"
    http_status = 409


class AgentBackendUnavailableError(AgentMessagingError):
    code = "agent_backend_unavailable"
    http_status = 400


class AgentRequestInvalidError(AgentMessagingError):
    code = "agent_request_invalid"
    http_status = 400


# ---------------------------------------------------------------------------
# Config + dependencies
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentMessagingConfig:
    """Policy snapshot bound at service construction time."""

    enabled: bool = True
    allowed_backends: tuple[str, ...] = ("codex", "claude_code")
    allowed_working_directory_roots: tuple[str, ...] = ()
    max_message_bytes: int = 65_536
    max_thread_messages: int = 1_000
    default_timeout_seconds: int = 600
    max_timeout_seconds: int = 1_800


class _FlowManagerLike(Protocol):
    def create_flow(
        self,
        session_id: str,
        trigger_type: str,
        trigger_data: dict[str, object],
        priority: int = 5,
    ) -> str: ...

    def update_flow_status(self, flow_id: str, status: str) -> None: ...


class _ActionFactoryLike(Protocol):
    def submit_action_definition(
        self,
        action_definition: dict[str, object],
        context: dict[str, object] | None = None,
    ) -> str: ...


class _CompilationContextBuilderLike(Protocol):
    def build_context(
        self, *, session_id: str, flow_id: str,
    ) -> dict[str, object]: ...


@dataclass(slots=True)
class _BridgeDeliveryEndpoint:
    """Delivery target for bridge-delivered run_turn results."""

    plugin_namespace: str
    deliver_result_process_key: str
    deliver_error_process_key: str


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _PendingTurn:
    """Internal projection passed from send → submit.

    Holds the originator-side message row plus the freshly-allocated
    flow id so the bridge-delivery dispatcher can correlate the
    eventual ``deliver_result`` payload back to the request.
    """

    thread: AgentThreadRow
    originator_message: AgentMessageRow
    flow_id: str
    action_id: str


def role_message_external_id(
    recipient_kind: str, recipient_key: str, message_id: str,
) -> str:
    """Deterministic idempotency key for a role-addressed envelope.

    ``message_id`` is stable per logical send (not per transport attempt),
    so transport retries of the same send collapse to one row while
    distinct sends get distinct keys.
    """
    return f"{recipient_kind}:{recipient_key}:{message_id}"


def _serialize_role_content(content: MessageContent) -> list[dict[str, object]]:
    """Render the typed message parts to JSON-storable dicts for the envelope."""
    return [{"type": part.type, "text": part.text} for part in content]


def _result_records(result: object) -> list[dict[str, object]]:
    """Records from a COMPLETED query ActionResult — fails loud on a provider error.

    Delegates to :func:`require_records` (Codex BLOCKER-1): a non-completed
    ``action_status`` RAISES ``StateOperationError`` rather than masquerading as
    an empty result (which let a DB fault read as an empty-but-healthy section).
    """
    return require_records(result)


# REL-05 re-emit window + cap defaults (plugin-config-surfaced; Q1). The window
# is the minimum gap between emissions of the same owed message (most sessions
# turn within it, so the first re-emit is genuine deafness, not impatience); the
# cap bounds total emissions (original + re-emits) before escalation.
DEFAULT_RE_EMIT_WINDOW_S = 300.0
DEFAULT_RE_EMIT_CAP = 3

# REL-05 QUIET-GAP: how long a session must have been free of model-initiated
# activity BEFORE an emission for that emission to count as the thing that
# started the next turn. Activity strictly after an emission is NOT proof the
# emission was surfaced — a turn already in flight makes its next call within
# seconds, which marked every wake to a BUSY session consumed on its first emit
# and retired it from the owed set unseen (the silent-loss class). Sized above a
# working session's inter-call spacing so a mid-turn arrival cannot clear it.
TURN_BOUNDARY_QUIET_S = 45.0


def _emitted_after_turn_boundary(
    emitted: datetime, prev_activity_at: datetime | None,
) -> bool:
    """True when ``emitted`` landed in a quiet gap long enough to be a turn start.

    ``prev_activity_at`` is the model-activity stamp immediately preceding the one
    being reconciled, so ``emitted - prev_activity_at`` is exactly the idle span
    the emission arrived in. ``None`` (no prior activity on this session) means the
    entire session so far was quiet — the gap is unbounded, so it qualifies.
    """
    if prev_activity_at is None:
        return True
    return (emitted - prev_activity_at).total_seconds() >= TURN_BOUNDARY_QUIET_S


def _parse_iso(value: object) -> datetime | None:
    """Parse a stored ISO-8601 timestamp cell to an aware (UTC) datetime, or ``None``.

    State-store ``DATETIME`` columns are ``timestamp without time zone``, so every
    stored cell reads back offset-NAIVE (UTC wall-clock) while this module's live
    datetimes (``_clock`` = ``datetime.now(UTC)``, the bridge activity stamp) are
    offset-AWARE UTC. Coerce a naive parse to UTC at this single boundary so every
    comparison in the module is aware-vs-aware (all values are UTC) and the
    owed-delivery sweep/drain never hits a naive-vs-aware ``TypeError``.
    """
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _as_int(value: object) -> int:
    """A state-row integer cell coerced to ``int`` (0 for a missing/other cell)."""
    return value if isinstance(value, int) else 0


def _within_reemit_window(
    last_emitted_at: object, now: datetime, re_emit_window_s: float,
) -> bool:
    """True iff an emission happened too recently to re-emit yet.

    A row never emitted (``last_emitted_at`` NULL) is NOT within the window — it
    is eligible immediately (the role original's first drain). Otherwise the row
    must wait ``re_emit_window_s`` from its last emission before re-emit.
    """
    emitted = _parse_iso(last_emitted_at)
    if emitted is None:
        return False
    return (now - emitted).total_seconds() < re_emit_window_s


def _owed_after_window_and_cap(
    rows: list[dict[str, object]],
    *,
    now: datetime,
    re_emit_window_s: float,
    cap: int,
) -> list[dict[str, object]]:
    """Keep only rows eligible for (re-)emit — the window/cap inequalities the
    equality-only state filter cannot express.

    A row is dropped when it has already been emitted ``cap`` times, or when its
    last emission is still inside the re-emit window. Order-preserving.
    """
    eligible: list[dict[str, object]] = []
    for row in rows:
        if _as_int(row.get(COL_EMIT_COUNT)) >= cap:
            continue
        if _within_reemit_window(row.get(COL_LAST_EMITTED_AT), now, re_emit_window_s):
            continue
        eligible.append(row)
    return eligible


def _escalatable(
    rows: list[dict[str, object]],
    *,
    now: datetime,
    cap: int,
    re_emit_window_s: float,
) -> list[dict[str, object]]:
    """Owed rows the server sweep should escalate — shared by role + direct.

    A row is escalatable when it has hit the emit CAP (a live recipient that
    stayed deaf through every re-emit → ``cap_reached``) OR when it has been owed
    past the cap-equivalent time ``cap * re_emit_window_s`` (a recipient whose
    bridge died so its drain never ran → ``recipient_gone``). The caller decides
    the reason and stamps the row terminal.
    """
    deadline_s = cap * re_emit_window_s
    out: list[dict[str, object]] = []
    for row in rows:
        if _as_int(row.get(COL_EMIT_COUNT)) >= cap:
            out.append(row)
            continue
        created = _parse_iso(row.get("created_at"))
        if created is not None and (now - created).total_seconds() > deadline_s:
            out.append(row)
    return out


class AgentMessagingService:
    """Public façade for the agent-messaging layer.

    Construct once at plugin readiness; reused for the lifetime of the
    plugin.  All methods are synchronous; the underlying turn execution
    runs in the action queue (so HTTP callers see a queued response,
    not a blocked socket).
    """

    def __init__(
        self,
        *,
        repository: AgentMessagingRepository,
        state_service: StateManagementInterface,
        backend_router: BackendRouter,
        flow_manager: _FlowManagerLike,
        action_factory: _ActionFactoryLike,
        compilation_context_builder: _CompilationContextBuilderLike,
        bridge_delivery: _BridgeDeliveryEndpoint,
        config: AgentMessagingConfig,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._repo = repository
        # The v10 role-addressed delivery store (core__agent_role_message)
        # is written + read ONLY through the high-level state interface
        # (upsert_state / query_ordered / update_state) — never raw SQL.
        self._state = state_service
        self._router = backend_router
        self._flows = flow_manager
        self._actions = action_factory
        self._context = compilation_context_builder
        self._delivery = bridge_delivery
        self._config = config
        self._clock = clock or (lambda: datetime.now(UTC))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def open_thread(self, request: OpenAgentThreadRequest) -> AgentThreadOpened:
        self._require_enabled()
        self._validate_open_thread_request(request)
        backend, plugin_name = self._validate_backend(request.backend)
        working_directory = self._validate_working_directory(
            request.working_directory,
        )
        thread = self._repo.create_thread(
            originator_type=OriginatorType.MCP_BRIDGE,
            originator_bridge_id=request.bridge_id,
            originator_session_id=request.session_id,
            target_backend=backend,
            target_plugin_name=plugin_name,
            status=ThreadStatus.OPEN,
            title=(request.title or "").strip() or None,
            working_directory=working_directory,
            metadata=_build_open_thread_metadata(request),
        )

        if request.initial_message is None:
            return AgentThreadOpened(
                thread_id=thread.id,
                status=thread.status,
            )

        pending = self._dispatch_turn(
            thread=thread,
            content=request.initial_message.content,
            timeout_seconds=request.initial_message.timeout_seconds,
        )
        return AgentThreadOpened(
            thread_id=thread.id,
            message_id=pending.originator_message.id,
            action_id=pending.action_id,
            flow_id=pending.flow_id,
            status=ThreadStatus.QUEUED,
        )

    def _validate_open_thread_request(
        self, request: OpenAgentThreadRequest,
    ) -> None:
        """Validate an ``open_thread`` request before any DB writes.

        Catches missing ``bridge_id``/``session_id`` and any defect in
        the optional ``initial_message`` (bad content shape, oversize
        payload, non-positive timeout) so the caller fails fast without
        leaving an orphan thread row in OPEN state.
        """
        if not request.bridge_id:
            raise AgentRequestInvalidError("bridge_id is required")
        if not request.session_id:
            raise AgentRequestInvalidError(
                "session_id is required (passed in by the plugin's bridge "
                "surface from the bridge's minted platform session)",
            )
        if request.initial_message is not None:
            self._validate_content(request.initial_message.content)
            # _clamp_timeout raises AgentRequestInvalidError on negative input.
            self._clamp_timeout(request.initial_message.timeout_seconds)

    def send_message(
        self, request: SendAgentMessageRequest,
    ) -> AgentMessageQueued:
        self._require_enabled()
        thread = self._require_owned_thread(request.thread_id, request.bridge_id)
        self._require_open_for_send(thread)
        self._validate_content(request.content)
        self._enforce_message_cap(thread.id)

        pending = self._dispatch_turn(
            thread=thread,
            content=request.content,
            timeout_seconds=request.timeout_seconds,
        )
        return AgentMessageQueued(
            thread_id=thread.id,
            message_id=pending.originator_message.id,
            action_id=pending.action_id,
            flow_id=pending.flow_id,
            status=ThreadStatus.QUEUED,
        )

    def list_messages(
        self, request: ListAgentMessagesRequest,
    ) -> AgentMessagesPage:
        self._require_enabled()
        thread = self._require_owned_thread(request.thread_id, request.bridge_id)
        rows = self._repo.list_messages(
            thread.id,
            after_cursor=max(0, request.after_cursor),
            limit=request.limit,
        )
        next_cursor = rows[-1].cursor if rows else request.after_cursor
        return AgentMessagesPage(
            thread_id=thread.id,
            messages=tuple(rows),
            next_cursor=next_cursor,
            status=thread.status,
        )

    def list_threads(
        self, request: ListAgentThreadsRequest,
    ) -> AgentThreadsPage:
        """Enumerate threads globally, cursor-paginated by ``(created_at, id)``.

        Unscoped by design (no ``bridge_id`` / ``_require_owned_thread``): this
        is the GAP-5/D1 substrate read consumed by structural typing for
        downstream projection (e.g. the session ledger), not a per-bridge
        owned-thread read like ``list_messages``. Only the enablement gate
        applies. The composite cursor is carried verbatim across pages; an
        empty page returns the request's own cursor (the caller stops when a
        page is short or empty).
        """
        self._require_enabled()
        after = (
            decode_thread_cursor(request.after_cursor)
            if request.after_cursor is not None
            else None
        )
        rows = self._repo.list_threads(
            after=after,
            limit=request.limit,
            include_deleted=request.include_deleted,
        )
        if rows:
            last = rows[-1]
            next_cursor: str | None = encode_thread_cursor(
                created_at_iso=normalize_sort_value(last.created_at),
                row_id=last.id,
            )
        else:
            next_cursor = request.after_cursor
        return AgentThreadsPage(
            threads=tuple(rows),
            next_cursor=next_cursor,
        )

    def read_thread_messages(
        self, request: ReadThreadMessagesRequest,
    ) -> AgentThreadMessagesPage:
        """Unscoped per-thread message read (no bridge-ownership gate).

        Identical to ``list_messages``'s repository read — same
        ``self._repo.list_messages`` (the repo layer is pure persistence, NOT
        ownership-scoped) and the same int-cursor pagination — but WITHOUT
        ``_require_owned_thread``: the session-ledger projection (the only
        consumer) reads threads it does not own. Returns a minimal page (no
        thread ``status``: the owned-thread fetch is intentionally skipped).
        Only the enablement gate applies.
        """
        self._require_enabled()
        rows = self._repo.list_messages(
            request.thread_id,
            after_cursor=max(0, request.after_cursor),
            limit=request.limit,
        )
        next_cursor = rows[-1].cursor if rows else request.after_cursor
        return AgentThreadMessagesPage(
            thread_id=request.thread_id,
            messages=tuple(rows),
            next_cursor=next_cursor,
        )

    def get_status(
        self, *, thread_id: str, bridge_id: str,
    ) -> AgentThreadStatus:
        self._require_enabled()
        thread = self._require_owned_thread(thread_id, bridge_id)
        return AgentThreadStatus(
            thread_id=thread.id,
            status=thread.status,
            backend=thread.target_backend,
            last_message_cursor=thread.last_message_cursor,
            updated_at=thread.updated_at,
            active_action_id=thread.active_action_id,
            active_flow_id=thread.active_flow_id,
            backend_session_id=thread.backend_session_id,
        )

    def peer_send(self, request: PeerSendRequest) -> PeerSendResult:
        """Persist a peer-message and return what the caller should deliver.

        Cross-bridge addressing (live MCP session ↔ MCP session)
        layers on top of the existing thread/message tables: one
        durable thread per ``(sender_bridge, peer_agent_id)`` pair,
        identified by ``target_backend = 'peer:<peer_agent_id>'``.
        Each ``peer_send`` allocates a cursor and appends a message
        the same way ``send_message`` does.

        The actual event enqueue on the *target* bridge happens in
        ``agent_messaging_plugin``'s bridge surface: this method just
        records the outgoing side and returns the structured payload
        the plugin should fan out.  Service-level routing keeps things
        decoupled from the bridge's in-memory peer registry.
        """
        self._require_enabled()
        if not request.sender_bridge_id:
            raise AgentRequestInvalidError("sender_bridge_id is required")
        if not request.sender_agent_id:
            raise AgentRequestInvalidError("sender_agent_id is required")
        if not request.sender_agent_instance_id:
            raise AgentRequestInvalidError("sender_agent_instance_id is required")
        if not request.peer_agent_id:
            raise AgentRequestInvalidError("peer_agent_id is required")
        if not request.peer_agent_instance_id:
            raise AgentRequestInvalidError("peer_agent_instance_id is required")
        # Same-instance self-send is invalid regardless of agent_id.
        # The instance_id is the durable UUID; if both sides resolve to
        # the same instance it is a self-send no matter what agent_id
        # either side currently advertises (e.g. after a manual
        # peer_register that mutated agent_id).  Same-kind cross-instance
        # sends (codex_A -> codex_B) are valid.
        if (
            request.sender_agent_instance_id
            == request.peer_agent_instance_id
        ):
            raise AgentRequestInvalidError(
                "cannot peer_send to your own instance "
                f"({request.sender_agent_instance_id})",
            )
        self._validate_content(request.content)

        thread = self._repo.find_peer_thread(
            originator_bridge_id=request.sender_bridge_id,
            peer_agent_id=request.peer_agent_id,
            peer_agent_instance_id=request.peer_agent_instance_id,
        )
        if thread is None:
            thread = self._create_peer_thread(request)
        appended = self._repo.append_message(
            thread_id=thread.id,
            message=NewMessage(
                role=MessageRole.ORIGINATOR,
                kind=MessageKind.MESSAGE,
                content=request.content,
                metadata={
                    "peer": True,
                    "sender_agent_id": request.sender_agent_id,
                    "sender_agent_instance_id": (
                        request.sender_agent_instance_id
                    ),
                    "sender_session_label": request.sender_session_label,
                    "peer_agent_id": request.peer_agent_id,
                    "peer_agent_instance_id": request.peer_agent_instance_id,
                    # ``important`` discriminates wake-bound messages
                    # (sender used the IMPORTANT marker) from silent FYIs.
                    # peer_inbox uses it for explicit silent-only filters while
                    # leaving the public default as durable catch-up.
                    "important": request.important,
                },
            ),
            require_status_in=(
                ThreadStatus.OPEN,
                ThreadStatus.IDLE,
                ThreadStatus.INTERRUPTED,
            ),
            update=ThreadStatusUpdate(status=ThreadStatus.IDLE),
        )
        return PeerSendResult(
            thread_id=thread.id,
            message_id=appended.id,
            cursor=appended.cursor,
            # The plugin overrides this after looking up the target's
            # current bridge_id from the registry; we don't track it
            # here (service stays bridge-agnostic).
            delivered_to_bridge_id="",
        )

    def peer_inbox(self, request: PeerInboxRequest) -> PeerInbox:
        """Return originator peer messages addressed to ``recipient_agent_id``.

        Lets a receiver pull messages that were persisted silently
        (no IMPORTANT marker, so no notification fired).  Spans
        every peer thread targeting this agent regardless of which
        bridge owns the thread — peer threads get a small carve-out
        from strict bridge-bound ownership because the recipient is,
        by definition, not the originator.

        Pagination uses ``after_created_at`` as the high-water mark
        (per-thread cursor wouldn't span multiple threads cleanly).
        Caller passes the previous page's ``next_after_created_at``
        back on the next call.
        """
        self._require_enabled()
        if not request.recipient_agent_id:
            raise AgentRequestInvalidError("recipient_agent_id is required")
        if not request.recipient_agent_instance_id:
            raise AgentRequestInvalidError(
                "recipient_agent_instance_id is required",
            )
        rows = self._repo.list_peer_messages_for(
            recipient_agent_id=request.recipient_agent_id,
            recipient_agent_instance_id=(
                request.recipient_agent_instance_id
            ),
            recipient_agent_session_id=request.recipient_agent_session_id,
            after_created_at=request.after_created_at,
            limit=request.limit,
            silent_only=not request.include_important,
        )
        entries = tuple(
            PeerInboxEntry(
                thread_id=message.thread_id,
                sender_agent_id=str(
                    message.metadata.get("sender_agent_id", ""),
                ),
                sender_agent_instance_id=str(
                    message.metadata.get("sender_agent_instance_id", ""),
                ),
                sender_session_label=str(
                    message.metadata.get("sender_session_label", ""),
                ),
                message=message,
            )
            for message in rows
        )
        next_at = rows[-1].created_at if rows else request.after_created_at
        # v10 Control #1a: the role section is ADDITIVE — the instance section
        # above (entries + next_after_created_at + its raw-SQL read) is
        # untouched, so existing peer messaging is byte-for-byte unaffected.
        #
        # v10 Q1 fault-domain boundary (Architect-ruled 2026-06-19): the role
        # section is computed AFTER + INDEPENDENTLY of the instance section, and
        # ONLY its single seam (enumerate-held-roles + per-role query + k-way
        # merge) is wrapped. A role-side fault — a client's malformed
        # ``role_after`` (fails closed by contract), a k-way-merge edge, a
        # transient role-table read error — must NOT cost the caller its already
        # computed INSTANCE messages. This is NOT defensive swallowing: it is
        # loud (ERROR + traceback) and surfaced (``role_section_status=error`` +
        # the error repr) — correct partial-failure semantics for a two-section
        # response. A genuine SHARED fault (state store down) still fails the
        # instance read above first, so this never masks a total outage.
        role_entries, next_role_cursor, role_status, role_error = (
            self._collect_role_section(request)
        )
        return PeerInbox(
            recipient_agent_id=request.recipient_agent_id,
            entries=entries,
            next_after_created_at=next_at,
            role_entries=role_entries,
            next_role_cursor=next_role_cursor,
            role_section_status=role_status,
            role_section_error=role_error,
        )

    def _collect_role_section(
        self, request: PeerInboxRequest,
    ) -> tuple[tuple[PeerInboxEntry, ...], str | None, RoleSectionStatus, str | None]:
        """Compute the role section inside the Q1 fault-domain boundary.

        Returns ``(role_entries, next_role_cursor, status, error)``. On success:
        the merged page + cursor + ``OK`` + ``None``. On ANY role-side failure:
        empty page + no cursor + ``ERROR`` + ``repr(exc)`` (logged at ERROR with
        traceback). The broad ``except`` is the deliberate fault-domain boundary
        — its scope is this one seam, so it cannot hide a fault anywhere else.
        """
        try:
            role_entries, next_role_cursor = self.list_silent_for_roles(
                agent_instance_id=request.recipient_agent_instance_id,
                include_important=request.include_important,
                limit=request.limit,
                role_after=request.role_after,
            )
        except Exception as exc:  # noqa: BLE001 — the Q1 fault-domain boundary
            logger.exception(
                "peer_inbox role section failed for instance %s (role_after=%r); "
                "serving instance section only",
                request.recipient_agent_instance_id,
                request.role_after,
            )
            return (), None, RoleSectionStatus.ERROR, repr(exc)
        return role_entries, next_role_cursor, RoleSectionStatus.OK, None

    # ------------------------------------------------------------------
    # Role-addressed delivery store (v10 Control #1) — state-interface ONLY
    #
    # core__agent_role_message is the single authoritative delivery envelope
    # for role-addressed peer messages. Every access below goes through the
    # high-level state interface (upsert_state / query_ordered / update_state);
    # there is deliberately NO raw SQL and NO core__agent_message projection.
    # ------------------------------------------------------------------

    def persist_role_message(
        self,
        *,
        recipient_kind: str,
        recipient_key: str,
        message_id: str,
        sender_agent_id: str,
        sender_agent_instance_id: str,
        sender_session_label: str | None,
        important: bool,
        content: MessageContent,
    ) -> str:
        """Single authoritative write for a role-addressed message (B2 keystone).

        Exactly ONE persistence write — an idempotent ``upsert_state`` into
        ``core__agent_role_message`` keyed on the deterministic
        ``external_id``. No second table, no thread row, no
        ``core__agent_message`` projection — so there is no non-atomic
        dual-write and no "durable message with no delivery record" gap.
        Returns the ``message_id`` (the durable handle the caller surfaces).
        """
        self._require_enabled()
        record: dict[str, object] = {
            "external_id": role_message_external_id(
                recipient_kind, recipient_key, message_id,
            ),
            "recipient_kind": recipient_kind,
            "recipient_key": recipient_key,
            "message_id": message_id,
            "sender_agent_id": sender_agent_id,
            "sender_agent_instance_id": sender_agent_instance_id,
            "sender_session_label": sender_session_label,
            # Synthetic, deterministic role-channel handle — display only,
            # never dereferenced as a live thread.
            "thread_id": f"{ROLE_THREAD_PREFIX}{recipient_key}",
            "important": important,
            "delivered": False,
            # REL-05 (Q5): a fresh role message is un-consumed. Written EXPLICITLY
            # (like ``delivered``) so the consumed-gated drain predicate matches it
            # under an equality filter, not only via the column's DB default.
            COL_CONSUMED: False,
            # RIDER-1: a fresh role message is not escalated. Explicit (same
            # reason as ``consumed``) so the drain's ``escalated=false`` filter
            # matches it under the equality grammar.
            COL_ESCALATED: False,
            "content": _serialize_role_content(content),
        }
        require_completed(
            self._state.upsert_state(
                _ROLE_NAMESPACE,
                {
                    "table": TABLE_AGENT_ROLE_MESSAGE,
                    "record": record,
                    "conflict_columns": ["external_id"],
                },
            ),
            "upsert agent_role_message",
        )
        return message_id

    def list_undelivered_for(
        self, *, recipient_kind: str, recipient_key: str, limit: int,
    ) -> list[dict[str, object]]:
        """Oldest-first page of un-CONSUMED IMPORTANT messages for a recipient.

        The at-least-once drain (Control #5) calls this repeatedly until the
        page is empty. REL-05 (Q5): the stop predicate is ``consumed = false``
        (equality) — NOT ``delivered = false``. ``delivered`` stays the
        emission-bookkeeping bit (flipped on emission), but a message that was
        emitted yet never entered a turn context (Vector B) has
        ``consumed=false`` and is re-owed here. The re-emit WINDOW + CAP
        inequalities (which the equality-only state filter cannot express) are
        applied in Python by the caller (:meth:`list_undelivered_for_instance`).
        The composite ``(created_at, id)`` ascending order is genuinely
        oldest-first (the primitive rejects a non-composite order), so an old
        row's wait is bounded by its FIFO position and never starves under
        newer arrivals. A role recipient keys on ``recipient_key`` only →
        cross-kind takeover.
        """
        result = self._state.query_ordered(
            _ROLE_NAMESPACE,
            {
                "table": TABLE_AGENT_ROLE_MESSAGE,
                "filters": {
                    "recipient_kind": recipient_kind,
                    "recipient_key": recipient_key,
                    "important": True,
                    COL_CONSUMED: False,
                    # RIDER-1 (starvation fix): terminal-escalated rows drop from
                    # the owed query, so a capped-dormant row can no longer fill
                    # the oldest limit-page and starve a genuinely-owed newer row.
                    COL_ESCALATED: False,
                },
                "order_by": [("created_at", "asc"), ("id", "asc")],
                "limit": limit,
            },
        )
        return _result_records(result)

    def mark_delivered(self, *, external_id: str) -> None:
        """Flip ``delivered = true`` once the holder's transport emitted it.

        A single atomic ``update_state`` — idempotent (re-setting ``true`` is a
        no-op), so a dedup-suppressed re-encounter that still re-marks is
        safe. The row PERSISTS (audit); it is never deleted to signal delivery.
        REL-05 note: this flips ONLY ``delivered`` (the emission bit); the
        consumption stop (``consumed``) is stamped separately by
        :meth:`reconcile_role_consumption`, and the emit bookkeeping (F3 +
        cap) rides :meth:`mark_delivered_for_instance`.
        """
        require_completed(
            self._state.update_state(
                _ROLE_NAMESPACE,
                {
                    "table": TABLE_AGENT_ROLE_MESSAGE,
                    "filters": {"external_id": external_id},
                },
                {"delivered": True},
            ),
            "update agent_role_message.delivered",
        )

    def mark_delivered_for_instance(
        self, *, external_id: str, recipient_key: str, agent_instance_id: str,
    ) -> bool:
        """Ownership-fenced emission confirm (Control #5 fence + REL-05 F3).

        Records an emission ONLY if ``agent_instance_id`` currently holds
        ``recipient_key`` — a displaced/zombie holder cannot mark deliveries or
        suppress another holder's drain. Flips ``delivered=true`` (the emission
        bit) AND records the REL-05 bookkeeping: ``emit_count += 1``,
        ``last_emitted_at = now``, and (F3) ``emitted_to_agent_instance_id =
        agent_instance_id`` — so the consumption stamp later requires activity
        from THIS instance, and displacement re-owes to the new holder. Returns
        ``True`` when recorded, ``False`` when the fence rejected.

        emit_count precision note: the count is incremented per confirmed
        emission. A rare live-event-vs-repair-drain race for the same id can
        double-count by 1 (both settle callers reach the confirm while the
        single-flight ledger emits once) — bounded, and it biases toward FEWER
        re-emits (safe: never a false-positive loss).
        """
        if recipient_key not in self._enumerate_held_roles(agent_instance_id):
            return False
        row = self._read_owed_row(TABLE_AGENT_ROLE_MESSAGE, external_id)
        emit_count = _as_int(row.get(COL_EMIT_COUNT)) if row else 0
        require_completed(
            self._state.update_state(
                _ROLE_NAMESPACE,
                {
                    "table": TABLE_AGENT_ROLE_MESSAGE,
                    "filters": {"external_id": external_id},
                },
                {
                    "delivered": True,
                    COL_EMIT_COUNT: emit_count + 1,
                    COL_LAST_EMITTED_AT: self._clock().isoformat(),
                    COL_EMITTED_TO_AGENT_INSTANCE_ID: agent_instance_id,
                },
            ),
            "confirm agent_role_message emission",
        )
        return True

    def list_undelivered_for_instance(
        self,
        *,
        agent_instance_id: str,
        limit: int,
        now: datetime | None = None,
        re_emit_window_s: float = DEFAULT_RE_EMIT_WINDOW_S,
        cap: int = DEFAULT_RE_EMIT_CAP,
    ) -> list[dict[str, object]]:
        """Oldest-first repair-drain page across every role the instance holds.

        The Control #5 repair loop (and the ``POST /peer/drain`` route) call
        this each pass: enumerate the roles bound to ``agent_instance_id`` (the
        fence — only roles this instance currently holds), gather their
        un-CONSUMED IMPORTANT rows oldest-first, and global-merge to the oldest
        ``limit``. REL-05 (Q5): a row is owed while ``consumed=false``; the
        re-emit WINDOW (no re-emit sooner than ``re_emit_window_s`` after the
        last emission) and the CAP (``emit_count < cap``) are applied in Python
        here (the equality-only state filter cannot express those inequalities),
        PER ROLE before the merge so a within-window row never occupies a merge
        slot ahead of an eligible one. Returns ``[]`` for a holder with no roles.
        """
        held_roles = self._enumerate_held_roles(agent_instance_id)
        if not held_roles:
            return []
        cutoff = now or self._clock()
        per_role = [
            _owed_after_window_and_cap(
                self.list_undelivered_for(
                    recipient_kind=RECIPIENT_KIND_ROLE,
                    recipient_key=role,
                    limit=limit,
                ),
                now=cutoff,
                re_emit_window_s=re_emit_window_s,
                cap=cap,
            )
            for role in held_roles
        ]
        return merge_undelivered_oldest_first(per_role, limit)

    def reconcile_role_consumption(
        self,
        *,
        agent_instance_id: str,
        activity_at: datetime,
        prev_activity_at: datetime | None = None,
    ) -> list[str]:
        """Stamp ``consumed`` on role rows this instance provably entered a turn on.

        REL-05 (§4.3 + F3): a role message counts as consumed when the instance
        it was EMITTED TO performs model-initiated activity AFTER the emission
        AND that emission landed in a turn-boundary quiet gap (QUIET-GAP; see
        :meth:`_stamp_consumed_rows` — activity after an emission does not prove
        a turn SURFACED it when a turn was already running).
        Marks every un-consumed role row whose ``emitted_to_agent_instance_id``
        is ``agent_instance_id`` and whose ``last_emitted_at`` precedes
        ``activity_at``; returns the external_ids stamped. Fenced to the
        emitted-to instance (F3): a NEW holder's activity does not consume a
        message only the OLD holder ever received; a displaced-but-alive old
        holder cannot consume a message the role has moved away from.
        """
        result = self._state.query_state(
            _ROLE_NAMESPACE,
            {
                "table": TABLE_AGENT_ROLE_MESSAGE,
                "filters": {
                    COL_EMITTED_TO_AGENT_INSTANCE_ID: agent_instance_id,
                    COL_CONSUMED: False,
                },
            },
        )
        return self._stamp_consumed_rows(
            TABLE_AGENT_ROLE_MESSAGE,
            require_records(result),
            activity_at=activity_at,
            prev_activity_at=prev_activity_at,
        )

    def mark_role_consumed_on_ack(self, *, external_id: str) -> bool:
        """Watcher events-ack consumption for ONE role row.

        A no-MCP watcher acknowledged the bridge event carrying this role
        delivery — its long-poll cursor moved past it, so the bytes are
        provably streamed into the watch output. That is the pull-recipient
        equivalent of entering a turn: stamp ``consumed`` (drops the row from
        the owed drain and the escalation sweep) and flip ``delivered`` (the
        ack IS the emission confirm a forwarder would otherwise POST).
        Predicated on ``important=true AND consumed=false`` so re-acks and
        silent rows are no-ops. Returns True when a row was stamped.
        """
        return 0 < require_updated(
            self._state.update_state(
                _ROLE_NAMESPACE,
                {
                    "table": TABLE_AGENT_ROLE_MESSAGE,
                    "filters": {
                        "external_id": external_id,
                        "important": True,
                        COL_CONSUMED: False,
                    },
                },
                {
                    "delivered": True,
                    COL_CONSUMED: True,
                    COL_CONSUMED_AT: self._clock().isoformat(),
                },
            ),
        )

    # ------------------------------------------------------------------
    # REL-05 direct-IMPORTANT-send outbox (core__agent_direct_wake)
    #
    # The direct sibling of the role delivery methods above: one outbox row per
    # IMPORTANT DIRECT peer_send, with the SAME consumption-gated re-emit. The
    # recipient instance is FIXED (Q3), so direct rows carry no recipient_kind /
    # emitted_to and are F3-immune. All access is state-interface-only.
    # ------------------------------------------------------------------

    def persist_direct_wake(
        self,
        *,
        message_id: str,
        thread_id: str,
        recipient_agent_id: str,
        recipient_agent_instance_id: str,
        recipient_agent_session_id: str | None,
        sender_agent_id: str,
        sender_agent_instance_id: str,
        sender_session_label: str | None,
        sender_bridge_id: str,
        content: MessageContent,
    ) -> None:
        """Write the outbox row for an IMPORTANT direct send (idempotent).

        Called from ``dispatch_peer_send`` AFTER the original live wake /
        queued_notification event is queued, so the ORIGINAL emission is recorded
        here optimistically (``emit_count=1``, ``last_emitted_at=now``). Re-emits
        (if the recipient never enters a turn) come from the repair drain, gated
        by the window — so the original queued event and the first drain do not
        double-deliver (the 5-min window suppresses the drain until then). Stores
        the ORIGINAL content (marker still embedded); the drain serializer strips
        it for byte-identical delivery parity, exactly as the role path does.
        Idempotent on ``external_id=message_id`` (a transport retry collapses to
        one row).
        """
        record: dict[str, object] = {
            "external_id": message_id,
            "message_id": message_id,
            "thread_id": thread_id,
            "recipient_agent_id": recipient_agent_id,
            "recipient_agent_instance_id": recipient_agent_instance_id,
            "recipient_agent_session_id": recipient_agent_session_id,
            "sender_agent_id": sender_agent_id,
            "sender_agent_instance_id": sender_agent_instance_id,
            "sender_session_label": sender_session_label,
            "sender_bridge_id": sender_bridge_id,
            "content": _serialize_role_content(content),
            COL_LAST_EMITTED_AT: self._clock().isoformat(),
            COL_EMIT_COUNT: 1,
            COL_CONSUMED: False,
            COL_ESCALATED: False,
        }
        require_completed(
            self._state.upsert_state(
                _ROLE_NAMESPACE,
                {
                    "table": TABLE_AGENT_DIRECT_WAKE,
                    "record": record,
                    "conflict_columns": ["external_id"],
                },
            ),
            "upsert agent_direct_wake",
        )

    def rehome_owed_direct_wakes(
        self, *, agent_session_id: str, new_agent_instance_id: str,
    ) -> int:
        """Fork-1a re-home — re-point this session's owed direct rows on reconnect.

        The direct-wake sibling of ``refresh_role_binding_cas`` (REL-07): on
        reconnect/restart a recipient's ``agent_instance_id`` rotates but its
        ``agent_session_id`` is stable, so a predicated ``update_state`` keyed on
        the stable session id re-points every owed row this session is the
        RECIPIENT of, off the dead instance onto the just-registered successor —
        curing the REL-01 ``recipient_gone`` orphan class the RCA proved bites.
        Two moves (Architect ruling 2), returning their combined rows-affected:

        (a) **owed-and-live** rows (``consumed=false AND escalated=false``) — just
            re-point the drain fence; the successor bridge's drain adopts them.
        (b) **recipient_gone** rows (``consumed=false AND escalated=true AND
            reason=recipient_gone``) — the recipient PROVABLY returned (it just
            re-registered) and its terminality WAS the orphan bug, so re-point AND
            re-ENTER the re-emit stream (clear the terminal marks). ``cap_reached``
            rows are deliberately NOT revived: those emissions were genuinely
            spent against a live-but-deaf recipient (that is Root B, not an
            orphan) — reviving them would mask the deaf-wake signal.

        An empty session id is NOT an identity — fail closed (return ``0``, touch
        nothing) so a no-carrier recipient can never CAS-match a real row; a
        DIFFERENT session id matches nothing (the negative-adoption guard).
        """
        if not agent_session_id:
            return 0
        moved = require_updated(
            self._state.update_state(
                _ROLE_NAMESPACE,
                {
                    "table": TABLE_AGENT_DIRECT_WAKE,
                    "filters": {
                        "recipient_agent_session_id": agent_session_id,
                        COL_CONSUMED: False,
                        COL_ESCALATED: False,
                    },
                },
                {"recipient_agent_instance_id": new_agent_instance_id},
            ),
        )
        revived = require_updated(
            self._state.update_state(
                _ROLE_NAMESPACE,
                {
                    "table": TABLE_AGENT_DIRECT_WAKE,
                    "filters": {
                        "recipient_agent_session_id": agent_session_id,
                        COL_CONSUMED: False,
                        COL_ESCALATED: True,
                        COL_ESCALATION_REASON: ESCALATION_REASON_GONE,
                    },
                },
                {
                    "recipient_agent_instance_id": new_agent_instance_id,
                    COL_ESCALATED: False,
                    COL_ESCALATED_AT: None,
                    COL_ESCALATION_REASON: None,
                },
            ),
        )
        return moved + revived

    def list_owed_direct_for_instance(
        self,
        *,
        agent_instance_id: str,
        limit: int,
        now: datetime | None = None,
        re_emit_window_s: float = DEFAULT_RE_EMIT_WINDOW_S,
        cap: int = DEFAULT_RE_EMIT_CAP,
    ) -> list[dict[str, object]]:
        """Oldest-first drain page of owed direct-wake rows for this recipient.

        Owed = ``consumed=false AND escalated=false`` (equality) AND past the
        re-emit window AND under the cap (Python). The drain is fenced to the
        recipient instance's rows — the calling bridge cannot drain another
        recipient's outbox. On reconnect the recipient's instance id rotates, so
        owed rows are re-pointed to the successor by ``rehome_owed_direct_wakes``
        (Fork-1a) BEFORE this drain runs; the fence then naturally follows.
        """
        cutoff = now or self._clock()
        result = self._state.query_ordered(
            _ROLE_NAMESPACE,
            {
                "table": TABLE_AGENT_DIRECT_WAKE,
                "filters": {
                    "recipient_agent_instance_id": agent_instance_id,
                    COL_CONSUMED: False,
                    COL_ESCALATED: False,
                },
                "order_by": [("created_at", "asc"), ("id", "asc")],
                "limit": limit,
            },
        )
        return _owed_after_window_and_cap(
            _result_records(result),
            now=cutoff,
            re_emit_window_s=re_emit_window_s,
            cap=cap,
        )

    def mark_direct_emitted_for_instance(
        self, *, message_id: str, agent_instance_id: str,
    ) -> bool:
        """Record a re-emission of a direct-wake row (fenced to the recipient).

        Records ONLY if ``agent_instance_id`` is the row's FIXED recipient — a
        stray bridge cannot bump another instance's outbox. Sets
        ``last_emitted_at=now`` and ``emit_count += 1``. Returns ``True`` when
        recorded, ``False`` when the fence rejected (no such owed row for this
        instance).
        """
        row = self._read_owed_row(TABLE_AGENT_DIRECT_WAKE, message_id)
        if row is None:
            return False
        if str(row.get("recipient_agent_instance_id") or "") != agent_instance_id:
            return False
        require_completed(
            self._state.update_state(
                _ROLE_NAMESPACE,
                {
                    "table": TABLE_AGENT_DIRECT_WAKE,
                    "filters": {"external_id": message_id},
                },
                {
                    COL_EMIT_COUNT: _as_int(row.get(COL_EMIT_COUNT)) + 1,
                    COL_LAST_EMITTED_AT: self._clock().isoformat(),
                },
            ),
            "confirm agent_direct_wake emission",
        )
        return True

    def reconcile_direct_consumption(
        self,
        *,
        agent_instance_id: str,
        activity_at: datetime,
        prev_activity_at: datetime | None = None,
    ) -> list[str]:
        """Stamp ``consumed`` on direct rows this recipient entered a turn on.

        Direct rows are F3-immune (fixed recipient), so the fence is simply
        ``recipient_agent_instance_id == agent_instance_id``. Marks every
        un-consumed direct row emitted BEFORE ``activity_at`` that also landed in
        a turn-boundary quiet gap (QUIET-GAP; see :meth:`_stamp_consumed_rows`);
        returns the message_ids stamped.
        """
        result = self._state.query_state(
            _ROLE_NAMESPACE,
            {
                "table": TABLE_AGENT_DIRECT_WAKE,
                "filters": {
                    "recipient_agent_instance_id": agent_instance_id,
                    COL_CONSUMED: False,
                },
            },
        )
        return self._stamp_consumed_rows(
            TABLE_AGENT_DIRECT_WAKE,
            require_records(result),
            activity_at=activity_at,
            prev_activity_at=prev_activity_at,
        )

    def mark_direct_consumed_on_ack(
        self, *, message_id: str, recipient_agent_instance_id: str,
    ) -> bool:
        """Watcher events-ack consumption for ONE direct row.

        The direct sibling of :meth:`mark_role_consumed_on_ack`: the watcher's
        long-poll acked the queued wake event for this message, so it is
        provably surfaced in the watch output. Fenced to the row's FIXED
        recipient instance (the acking bridge's own binding) and predicated on
        ``consumed=false`` — a re-ack or a foreign message_id is a no-op.
        Returns True when a row was stamped.
        """
        return 0 < require_updated(
            self._state.update_state(
                _ROLE_NAMESPACE,
                {
                    "table": TABLE_AGENT_DIRECT_WAKE,
                    "filters": {
                        "external_id": message_id,
                        "recipient_agent_instance_id": recipient_agent_instance_id,
                        COL_CONSUMED: False,
                    },
                },
                {
                    COL_CONSUMED: True,
                    COL_CONSUMED_AT: self._clock().isoformat(),
                },
            ),
        )

    def list_escalatable_direct(
        self,
        *,
        now: datetime | None = None,
        cap: int = DEFAULT_RE_EMIT_CAP,
        re_emit_window_s: float = DEFAULT_RE_EMIT_WINDOW_S,
    ) -> list[dict[str, object]]:
        """Owed direct rows the server-side on_tick sweep should escalate.

        A row is escalatable when it has hit the emit CAP (a live recipient that
        stayed deaf through every re-emit → ``cap_reached``) OR when it has been
        owed past the cap-equivalent time ``cap * re_emit_window_s`` (a recipient
        whose bridge died so its drain never ran → ``recipient_gone``). Uncapped
        ``query_state`` (NOT ``query_ordered``'s 100-cap) so no owed row is
        silently skipped; the caller (reconciler) resolves the reason from the
        live registry and calls :meth:`mark_direct_escalated`.
        """
        cutoff = now or self._clock()
        result = self._state.query_state(
            _ROLE_NAMESPACE,
            {
                "table": TABLE_AGENT_DIRECT_WAKE,
                "filters": {COL_CONSUMED: False, COL_ESCALATED: False},
            },
        )
        return _escalatable(
            _result_records(result),
            now=cutoff,
            cap=cap,
            re_emit_window_s=re_emit_window_s,
        )

    def mark_direct_escalated(self, *, message_id: str, reason: str) -> None:
        """Stamp a direct row escalated (terminal) with the reason (audit)."""
        require_completed(
            self._state.update_state(
                _ROLE_NAMESPACE,
                {
                    "table": TABLE_AGENT_DIRECT_WAKE,
                    "filters": {"external_id": message_id},
                },
                {
                    COL_ESCALATED: True,
                    COL_ESCALATED_AT: self._clock().isoformat(),
                    COL_ESCALATION_REASON: reason,
                },
            ),
            "escalate agent_direct_wake",
        )

    def list_escalatable_role(
        self,
        *,
        now: datetime | None = None,
        cap: int = DEFAULT_RE_EMIT_CAP,
        re_emit_window_s: float = DEFAULT_RE_EMIT_WINDOW_S,
    ) -> list[dict[str, object]]:
        """Owed ROLE rows the server-side on_tick sweep should escalate (RIDER-1).

        The role sibling of :meth:`list_escalatable_direct`. Uncapped
        ``query_state`` over the un-consumed, un-escalated IMPORTANT role rows;
        the cap / cap-equivalent-time predicate is the shared :func:`_escalatable`.
        Without this a capped-unconsumed role row would sit in the owed set
        forever, clogging the drain's oldest limit-page and starving newer rows.
        """
        cutoff = now or self._clock()
        result = self._state.query_state(
            _ROLE_NAMESPACE,
            {
                "table": TABLE_AGENT_ROLE_MESSAGE,
                "filters": {
                    "important": True,
                    COL_CONSUMED: False,
                    COL_ESCALATED: False,
                },
            },
        )
        return _escalatable(
            _result_records(result),
            now=cutoff,
            cap=cap,
            re_emit_window_s=re_emit_window_s,
        )

    def mark_role_escalated(self, *, external_id: str, reason: str) -> None:
        """Stamp a role row escalated (terminal) — drops it from the owed drain."""
        require_completed(
            self._state.update_state(
                _ROLE_NAMESPACE,
                {
                    "table": TABLE_AGENT_ROLE_MESSAGE,
                    "filters": {"external_id": external_id},
                },
                {
                    COL_ESCALATED: True,
                    COL_ESCALATED_AT: self._clock().isoformat(),
                    COL_ESCALATION_REASON: reason,
                },
            ),
            "escalate agent_role_message",
        )

    def _stamp_consumed_rows(
        self,
        table: str,
        rows: list[dict[str, object]],
        *,
        activity_at: datetime,
        prev_activity_at: datetime | None,
    ) -> list[str]:
        """Mark ``consumed`` on each row whose emission STARTED the next turn.

        Two guards, both Python (the state filter expresses neither):

        1. ``last_emitted_at < activity_at`` — a turn that began before an
           emission cannot have surfaced it.
        2. ``last_emitted_at - prev_activity_at >= TURN_BOUNDARY_QUIET_S`` — the
           emission must have landed in a QUIET GAP long enough to be a turn
           boundary. Guard 1 alone was unsound in the direction that loses
           messages: a session already mid-turn issues its next model-initiated
           call within seconds, satisfying guard 1 and retiring the row from the
           owed set on its FIRST emit, unseen and never re-emitted. That made the
           silent-loss rate scale with how BUSY a session is — worst exactly for
           a coordinator, the session peers most need to reach. A row that fails
           guard 2 stays owed and re-emits (costing at most a visibly-marked
           duplicate), which is the §4.3 posture the contract always claimed:
           tolerate a redundant re-emit, never a false-positive loss.

        ``prev_activity_at`` of ``None`` means this is the session's FIRST
        model-initiated call, so the whole preceding session is the quiet gap and
        guard 2 passes. One ``update_state`` per row by ``external_id``
        (equality). Returns the external_ids stamped.
        """
        stamped: list[str] = []
        consumed_at = activity_at.isoformat()
        for row in rows:
            emitted = _parse_iso(row.get(COL_LAST_EMITTED_AT))
            if emitted is None or emitted >= activity_at:
                continue
            if not _emitted_after_turn_boundary(emitted, prev_activity_at):
                continue
            external_id = str(row.get("external_id") or "")
            if not external_id:
                continue
            require_updated(
                self._state.update_state(
                    _ROLE_NAMESPACE,
                    {"table": table, "filters": {"external_id": external_id}},
                    {COL_CONSUMED: True, COL_CONSUMED_AT: consumed_at},
                ),
            )
            stamped.append(external_id)
        return stamped

    def _read_owed_row(
        self, table: str, external_id: str,
    ) -> dict[str, object] | None:
        """Read one row by ``external_id`` from ``table`` (or ``None``)."""
        result = self._state.query_state(
            _ROLE_NAMESPACE,
            {"table": table, "filters": {"external_id": external_id}},
        )
        rows = require_records(result)
        return rows[0] if rows else None

    def list_silent_for_roles(
        self,
        *,
        agent_instance_id: str,
        include_important: bool,
        limit: int,
        role_after: str | None,
    ) -> tuple[tuple[PeerInboxEntry, ...], str | None]:
        """The role-inbox section — a global ``(created_at, id)`` k-way merge.

        Enumerates the roles ``agent_instance_id`` currently holds from
        ``agent_role_binding`` (NOT ``resolve_role``, which is per-name and
        cannot enumerate — Codex check #2), runs a per-role recent-first
        ``query_ordered`` over ``agent_role_message``, k-way merges into the
        global top-``limit``, and pages by an opaque scope-bound cursor.

        Default ``include_important=True`` is the catch-up/recovery view: it
        OMITS both the ``important`` and ``delivered`` filters so already-
        delivered IMPORTANT rows resurface (there is deliberately no
        ``core__agent_message`` projection to fall back on).
        ``include_important=False`` is an explicit silent-only status view.
        Returns ``((), None)`` for a holder with no roles. Re-readable +
        durable (rows are never consumed on read).
        """
        held_roles = self._enumerate_held_roles(agent_instance_id)
        if not held_roles:
            return (), None
        scope = RoleCursorScope(
            include_important=include_important,
            held_roles=tuple(held_roles),
        )
        after = self._decode_role_after(role_after, scope)
        per_role_records = [
            self._query_role_page(
                recipient_key=role,
                include_important=include_important,
                limit=limit,
                after=after,
            )
            for role in held_roles
        ]
        return build_role_section(per_role_records, scope=scope, limit=limit)

    def _enumerate_held_roles(self, agent_instance_id: str) -> list[str]:
        """Roles currently bound to ``agent_instance_id`` (single state read).

        Touches the plugin-owned ``agent_role_binding`` namespace — the state
        interface is namespace-per-call, so one ``peer_inbox`` legitimately
        spans the ``core`` envelope namespace and this binding namespace.
        """
        result = self._state.query_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                # §9 CUTOVER: the role-inbox enumeration reads the v4 table.
                "table": TABLE_ROLE_BINDING,
                "filters": {COL_AGENT_INSTANCE_ID: agent_instance_id},
            },
        )
        roles: list[str] = []
        for record in _result_records(result):
            role = record.get(COL_ROLE)
            if isinstance(role, str) and role:
                roles.append(role)
        return roles

    def _decode_role_after(
        self, role_after: str | None, scope: RoleCursorScope,
    ) -> tuple[object, ...] | None:
        """Decode the inbound ``role_after`` cursor against the current scope.

        ``None`` (no cursor) or a scope change (held-role set / visibility mode
        differs from issue time) → first page (``after=None``), so a
        newly-visible row is never silently skipped. A malformed/forged token
        fails closed as :class:`AgentRequestInvalidError`.
        """
        if role_after is None:
            return None
        try:
            decoded = decode_role_cursor(role_after, scope)
        except RoleCursorRejectedError as exc:
            raise AgentRequestInvalidError(str(exc)) from exc
        if decoded.outcome is RoleCursorOutcome.SCOPE_CHANGED:
            return None
        return (decoded.created_at, decoded.row_id)

    def _query_role_page(
        self,
        *,
        recipient_key: str,
        include_important: bool,
        limit: int,
        after: tuple[object, ...] | None,
    ) -> list[dict[str, object]]:
        """One per-role recent-first ``query_ordered`` page over the envelope."""
        filters: dict[str, object] = {
            "recipient_kind": RECIPIENT_KIND_ROLE,
            "recipient_key": recipient_key,
        }
        if not include_important:
            # Silent bucket only. include_important=True omits BOTH the
            # important and delivered filters (catch-up resurfaces delivered
            # IMPORTANT rows).
            filters["important"] = False
        query: dict[str, object] = {
            "table": TABLE_AGENT_ROLE_MESSAGE,
            "filters": filters,
            "order_by": [("created_at", "desc"), ("id", "desc")],
            "limit": limit,
        }
        if after is not None:
            query["after"] = after
        result = self._state.query_ordered(_ROLE_NAMESPACE, query)
        return _result_records(result)

    def close_thread(
        self, *, thread_id: str, bridge_id: str,
    ) -> AgentThreadClosed:
        self._require_enabled()
        thread = self._require_owned_thread(thread_id, bridge_id)
        if thread.status is ThreadStatus.CLOSED:
            return AgentThreadClosed(thread_id=thread.id, status=ThreadStatus.CLOSED)
        try:
            # Conditional close — refuse if a concurrent send raced
            # the thread into QUEUED/RUNNING between our snapshot
            # read and the write.  Without this guard the close
            # could overwrite QUEUED -> CLOSED while a turn is in
            # flight, stranding the run_turn action.
            updated = self._repo.conditional_update_thread(
                thread.id,
                ThreadStatusUpdate(
                    status=ThreadStatus.CLOSED,
                    set_closed_at=True,
                ),
                require_status_in=(
                    ThreadStatus.OPEN,
                    ThreadStatus.IDLE,
                    ThreadStatus.INTERRUPTED,
                    ThreadStatus.ERROR,
                ),
            )
        except RepositoryError as exc:
            # Status-guard failure means a turn raced in.
            raise AgentThreadRunningError(
                "thread has an active turn; agent_interrupt is not yet "
                "available — wait for the turn to land before closing.",
            ) from exc
        return AgentThreadClosed(thread_id=updated.id, status=updated.status)

    # ------------------------------------------------------------------
    # Runner entry point (called by plugin::agent_messaging_plugin::run_turn)
    # ------------------------------------------------------------------

    def execute_turn(
        self, *, thread_id: str, message_id: str,
    ) -> dict[str, object]:
        """Run one agent turn for the originator message ``message_id``.

        **Always returns a structured payload** — never raises.  The
        payload's ``status`` field indicates outcome
        (``idle`` / ``interrupted`` / ``error`` / ``not_found``).
        The platform's bridge_delivery_error builder only includes
        ``error_message`` / ``process_key`` / ``action_id`` /
        ``failed_arguments``; routing every outcome through
        bridge_delivery_result gives the consumer the full structured
        payload (including ``thread_id``, ``request_message_id``,
        stable error ``code``, persisted backend session id) regardless
        of whether the turn succeeded.
        """
        thread_or_error = self._load_runnable_thread(thread_id, message_id)
        if isinstance(thread_or_error, dict):
            return thread_or_error
        thread = thread_or_error

        running = self._transition_to_running(thread, message_id)
        if isinstance(running, dict):
            return running
        thread = running

        # From this point on, the thread is RUNNING — every failure
        # path must transition it to ERROR via _record_failure or the
        # thread is stranded (rejects future sends and close).
        invocation = self._invoke_turn(thread, message_id)
        if isinstance(invocation, dict):
            return invocation
        return self._finalize_turn_result(
            thread=thread,
            message_id=message_id,
            result=invocation,
        )

    def _load_runnable_thread(
        self, thread_id: str, message_id: str,
    ) -> AgentThreadRow | dict[str, object]:
        """Fetch ``thread_id`` and enforce the QUEUED/RUNNING run-guard.

        Returns an error payload (dict) for missing threads or
        out-of-state threads; otherwise returns the loaded row.  The
        run-guard prevents a stale/duplicated run_turn action from
        mutating a CLOSED, IDLE, ERROR, or INTERRUPTED thread
        mid-flight.  Failing here does NOT touch thread state.
        """
        thread = self._repo.get_thread(thread_id)
        if thread is None:
            return self._build_error_payload(
                thread_id=thread_id,
                request_message_id=message_id,
                code="agent_thread_not_found",
                message=f"thread {thread_id} not found",
                backend=None,
                response_message_id=None,
            )
        if thread.status not in {ThreadStatus.QUEUED, ThreadStatus.RUNNING}:
            return self._build_error_payload(
                thread_id=thread_id,
                request_message_id=message_id,
                code="agent_thread_run_guard",
                message=(
                    f"run_turn refusing to execute against thread "
                    f"{thread_id}: status is {thread.status.value!r} "
                    f"(expected 'queued' or 'running')"
                ),
                backend=thread.target_backend,
                response_message_id=None,
            )
        return thread

    def _transition_to_running(
        self, thread: AgentThreadRow, message_id: str,
    ) -> AgentThreadRow | dict[str, object]:
        """Move ``thread`` to RUNNING.

        Returns the updated row, or an error payload if the repository
        rejected the transition.  Failure here does NOT touch state.
        """
        try:
            return self._repo.update_thread(
                thread.id,
                ThreadStatusUpdate(status=ThreadStatus.RUNNING),
            )
        except RepositoryError as exc:
            return self._build_error_payload(
                thread_id=thread.id,
                request_message_id=message_id,
                code="agent_thread_transition_failed",
                message=str(exc),
                backend=thread.target_backend,
                response_message_id=None,
            )

    def _invoke_turn(
        self, thread: AgentThreadRow, message_id: str,
    ) -> ExecutionResult | dict[str, object]:
        """Look up the originator, assemble the prompt, invoke the backend.

        Returns the raw :class:`ExecutionResult` on success, or an
        error payload after compensating (transitions the thread to
        ERROR via ``_record_failure``).
        """
        try:
            history = self._repo.recent_messages(thread.id, limit=128)
            originator = next(
                (m for m in history if m.id == message_id), None,
            )
            if originator is None:
                err_msg = (
                    f"message {message_id} not found on thread {thread.id}"
                )
                self._record_failure(thread.id, message_id, err_msg)
                return self._build_error_payload(
                    thread_id=thread.id,
                    request_message_id=message_id,
                    code="agent_message_not_found",
                    message=err_msg,
                    backend=thread.target_backend,
                    response_message_id=None,
                )

            prompt = assemble_prompt(
                thread=thread,
                history=[m for m in history if m.id != message_id],
                current_request=originator.content,
            )
            # Persist the assembled prompt onto the originator message
            # metadata so it shows up in agent_messages output
            # (workbench doc §13).  Above max_message_bytes the helper
            # stores a SHA-256 hash instead of the full text.
            self._stamp_assembled_prompt(message_id, prompt.prompt)

            backend_resolution = self._router.resolve(thread.target_backend)
            execution_params = self._build_execution_params(
                prompt=prompt.prompt,
                working_directory=thread.working_directory,
                # Honor the per-message timeout the caller stamped onto
                # originator.metadata in _dispatch_turn (already clamped
                # to max_timeout_seconds).  Falls back to default.
                timeout_seconds=self._extract_timeout(originator),
            )
            return self._invoke_backend(
                backend_resolution.instance, execution_params,
            )
        except Exception as exc:  # noqa: BLE001 — every failure compensates
            self._record_failure(thread.id, message_id, str(exc))
            return self._build_error_payload(
                thread_id=thread.id,
                request_message_id=message_id,
                code=getattr(exc, "code", "agent_messaging_error"),
                message=str(exc),
                backend=thread.target_backend,
                response_message_id=None,
            )

    def _finalize_turn_result(
        self,
        *,
        thread: AgentThreadRow,
        message_id: str,
        result: ExecutionResult,
    ) -> dict[str, object]:
        """Map an :class:`ExecutionResult` to the bridge-delivered payload.

        Branches on (error / interrupted / success), persists the
        appropriate side-effect, and returns the structured payload.
        """
        if result.error:
            self._record_failure(thread.id, message_id, result.error)
            return self._build_error_payload(
                thread_id=thread.id,
                request_message_id=message_id,
                code="agent_execution_failed",
                message=result.error,
                backend=thread.target_backend,
                response_message_id=None,
            )

        if result.interrupted:
            interrupted_message = self._record_interrupted(
                thread_id=thread.id,
                request_message_id=message_id,
                backend=thread.target_backend,
                result=result,
            )
            return _build_terminal_payload(
                thread_id=thread.id,
                request_message_id=message_id,
                response_message_id=interrupted_message.id,
                status=ThreadStatus.INTERRUPTED,
                backend=thread.target_backend,
                result=result,
            )

        agent_message = self._record_success(
            thread_id=thread.id,
            request_message_id=message_id,
            backend=thread.target_backend,
            result=result,
        )
        return _build_terminal_payload(
            thread_id=thread.id,
            request_message_id=message_id,
            response_message_id=agent_message.id,
            status=ThreadStatus.IDLE,
            backend=thread.target_backend,
            result=result,
        )

    @staticmethod
    def _build_error_payload(
        *,
        thread_id: str,
        request_message_id: str,
        code: str,
        message: str,
        backend: str | None,
        response_message_id: str | None,
    ) -> dict[str, object]:
        """Structured failure payload delivered via bridge_delivery_result.

        Shape mirrors the success payload so consumers parse one schema
        and dispatch on ``status``.  ``code`` is a stable token suitable
        for client-side branching; ``message`` is human-readable.
        """
        return {
            "thread_id": thread_id,
            "request_message_id": request_message_id,
            "response_message_id": response_message_id,
            "status": ThreadStatus.ERROR.value,
            "backend": backend,
            "backend_session_id": None,
            "text": "",
            "interrupted": False,
            "interrupted_on": None,
            "artifacts": [],
            "metrics": {},
            "error": {
                "code": code,
                "message": message,
            },
        }

    def _stamp_assembled_prompt(self, message_id: str, prompt: str) -> None:
        """Write the assembled prompt onto the originator message metadata.

        Below ``max_message_bytes`` the verbatim text is stored.
        Above that bound, the SHA-256 hash is stored instead so the
        metadata column doesn't bloat unboundedly when a large
        transcript is included.
        """
        encoded = prompt.encode("utf-8")
        patch: dict[str, object] = {"assembled_prompt_chars": len(prompt)}
        if len(encoded) <= self._config.max_message_bytes:
            patch["assembled_prompt"] = prompt
        else:
            import hashlib  # noqa: PLC0415
            patch["assembled_prompt_sha256"] = hashlib.sha256(encoded).hexdigest()
        try:
            self._repo.merge_message_metadata(message_id, patch)
        except RepositoryError:
            # Best-effort audit stamp — never block the turn on it.
            return

    def _extract_timeout(self, originator: AgentMessageRow) -> int:
        """Pull ``timeout_seconds`` out of an originator message's metadata.

        Stamped by ``_dispatch_turn`` after clamping; absent for legacy
        rows or if the caller didn't pass one (in which case we fall
        back to the platform default).
        """
        raw = originator.metadata.get("timeout_seconds")
        if isinstance(raw, bool):  # bool is int subclass — exclude
            return self._config.default_timeout_seconds
        if isinstance(raw, int) and raw > 0:
            return raw
        if isinstance(raw, str) and raw.isdigit():
            value = int(raw)
            if value > 0:
                return value
        return self._config.default_timeout_seconds

    # ------------------------------------------------------------------
    # Internals — validation / lookup
    # ------------------------------------------------------------------

    def _require_enabled(self) -> None:
        if not self._config.enabled:
            raise AgentMessagingDisabledError(
                "agent_messaging is disabled in plugin config",
            )

    def _validate_backend(self, backend: str) -> tuple[str, str]:
        if not backend:
            raise AgentRequestInvalidError("backend is required")
        if backend not in self._config.allowed_backends:
            raise AgentBackendUnavailableError(
                f"backend {backend!r} is not in allowed_backends "
                f"({list(self._config.allowed_backends)})",
            )
        try:
            resolved = self._router.resolve(backend)
        except BackendResolutionError as exc:
            raise AgentBackendUnavailableError(str(exc)) from exc
        return resolved.backend, resolved.plugin_name

    def _validate_working_directory(self, raw: str | None) -> str | None:
        if raw is None:
            return None
        path = Path(raw).expanduser()
        try:
            resolved = path.resolve(strict=False)
        except OSError as exc:
            raise AgentRequestInvalidError(
                f"working_directory {raw!r} is invalid: {exc}",
            ) from exc
        roots = self._config.allowed_working_directory_roots
        if roots:
            if not any(_is_subpath(resolved, Path(r).expanduser().resolve())
                       for r in roots):
                raise AgentRequestInvalidError(
                    f"working_directory {raw!r} is not inside any "
                    f"allowed_working_directory_roots ({list(roots)})",
                )
        return str(resolved)

    def _create_peer_thread(self, request: PeerSendRequest) -> AgentThreadRow:
        """Snapshot session_labels + agent_instance_ids onto a new peer thread.

        Per 2026-05-31 Architect ruling §2: the 4 actor-snapshot columns
        and the title format are written ONCE at create_thread time and
        NEVER repeated on append_message. A later ``/rename`` on either
        endpoint does NOT propagate to historical rows.
        """
        sender_label = (
            request.sender_session_label or request.sender_agent_id
        )
        peer_label = (
            request.peer_session_label or request.peer_agent_id
        )
        return self._repo.create_thread(
            originator_type=OriginatorType.MCP_BRIDGE,
            originator_bridge_id=request.sender_bridge_id,
            originator_session_id=request.sender_bridge_id,
            target_backend=f"peer:{request.peer_agent_id}",
            target_plugin_name="agent_messaging_plugin",
            status=ThreadStatus.IDLE,
            title=f"peer: {sender_label} -> {peer_label}",
            metadata={
                "sender_agent_id": request.sender_agent_id,
                "peer_agent_id": request.peer_agent_id,
            },
            recipient_agent_instance_id=request.peer_agent_instance_id,
            recipient_agent_session_id=request.peer_agent_session_id,
            originator_session_label=(
                request.sender_session_label or None
            ),
            originator_agent_instance_id=(
                request.sender_agent_instance_id or None
            ),
            recipient_session_label=(
                request.peer_session_label or None
            ),
        )

    def _validate_content(self, content: MessageContent) -> None:
        if not content:
            raise AgentRequestInvalidError("content must contain at least one part")
        size = sum(
            len(part.text.encode("utf-8")) for part in content
        )
        if size > self._config.max_message_bytes:
            raise AgentRequestInvalidError(
                f"content exceeds max_message_bytes "
                f"({size} > {self._config.max_message_bytes})",
            )

    def _enforce_message_cap(self, thread_id: str) -> None:
        limit = self._config.max_thread_messages
        # Use a lightweight read; we only need to know if the cap is hit.
        recent = self._repo.recent_messages(thread_id, limit=limit + 1)
        if len(recent) >= limit:
            raise AgentRequestInvalidError(
                f"thread has reached max_thread_messages={limit}",
            )

    def _require_owned_thread(
        self, thread_id: str, bridge_id: str,
    ) -> AgentThreadRow:
        thread = self._repo.get_thread(thread_id)
        if thread is None:
            raise AgentThreadNotFoundError(f"thread {thread_id} not found")
        if thread.originator_bridge_id != bridge_id:
            raise AgentThreadUnauthorizedError(
                f"thread {thread_id} not owned by bridge {bridge_id}",
            )
        return thread

    @staticmethod
    def _require_open_for_send(thread: AgentThreadRow) -> None:
        if thread.status in {ThreadStatus.QUEUED, ThreadStatus.RUNNING}:
            raise AgentThreadBusyError(
                f"thread {thread.id} already has an active turn",
            )
        if thread.status in {ThreadStatus.CLOSED, ThreadStatus.ERROR}:
            raise AgentThreadClosedError(
                f"thread {thread.id} status is {thread.status.value!r}; "
                "send is not permitted",
            )
        # OPEN, IDLE, INTERRUPTED are sendable.  An interrupted turn
        # didn't kill the thread — the originator is free to send a
        # follow-up message that supersedes the partial response.

    # ------------------------------------------------------------------
    # Internals — turn dispatch
    # ------------------------------------------------------------------

    def _dispatch_turn(
        self,
        *,
        thread: AgentThreadRow,
        content: MessageContent,
        timeout_seconds: int | None,
    ) -> _PendingTurn:
        clamped_timeout = self._clamp_timeout(timeout_seconds)
        # Persist the originator message and queue the thread atomically.
        originator = self._repo.append_message(
            thread_id=thread.id,
            message=NewMessage(
                role=MessageRole.ORIGINATOR,
                kind=MessageKind.MESSAGE,
                content=content,
                metadata={
                    "timeout_seconds": clamped_timeout,
                },
            ),
            require_status_in=(
                ThreadStatus.OPEN,
                ThreadStatus.IDLE,
                ThreadStatus.INTERRUPTED,
            ),
            update=ThreadStatusUpdate(status=ThreadStatus.QUEUED),
        )

        # Once the originator message is persisted and the thread is in
        # QUEUED, every subsequent failure must roll the thread back to
        # ERROR — otherwise the thread is stranded (refuses sends and
        # close).  Any caller-visible exception below also persists a
        # system/error message so the failure is visible to ``agent_messages``.
        try:
            return self._submit_turn_action(
                thread=thread, originator=originator,
            )
        except Exception as exc:
            self._record_dispatch_failure(
                thread_id=thread.id,
                request_message_id=originator.id,
                error_message=f"dispatch failed: {exc}",
            )
            raise

    def _submit_turn_action(
        self,
        *,
        thread: AgentThreadRow,
        originator: AgentMessageRow,
    ) -> _PendingTurn:
        """Inner half of :meth:`_dispatch_turn` — does the failure-prone work.

        Split out so the surrounding ``try/except`` in ``_dispatch_turn``
        can compensate cleanly without re-running the cursor allocation.
        """
        session_id = thread.originator_session_id
        if not session_id:
            raise AgentMessagingError(
                f"thread {thread.id} has no originator_session_id; cannot dispatch",
            )

        trigger_data: dict[str, object] = {
            "source_namespace": self._delivery.plugin_namespace,
            "source": "agent_channel",
            "originator_type": OriginatorType.MCP_BRIDGE.value,
            "originator_bridge_id": thread.originator_bridge_id,
            "bridge_id": thread.originator_bridge_id,
            "session_id": session_id,
            "thread_id": thread.id,
            "message_id": originator.id,
            "bridge_plugin_namespace": self._delivery.plugin_namespace,
            "deliver_result_process_key": self._delivery.deliver_result_process_key,
            "deliver_error_process_key": self._delivery.deliver_error_process_key,
        }
        flow_id = self._flows.create_flow(
            session_id=session_id,
            trigger_type="agent_messaging_send",
            trigger_data=trigger_data,
            priority=5,
        )
        action_def: dict[str, object] = {
            "process_key": RUN_TURN_PROCESS_KEY,
            "arguments": {
                "thread_id": thread.id,
                "message_id": originator.id,
            },
            "notes": f"agent_messaging turn for {thread.id}",
            "session_id": session_id,
            "flow_id": flow_id,
            "result_processor_kind": ResultProcessorKind.BRIDGE_DELIVERY.value,
            "error_processor_kind": ErrorProcessorKind.BRIDGE_DELIVERY.value,
        }
        try:
            compilation_context = self._context.build_context(
                session_id=session_id, flow_id=flow_id,
            )
            action_id = self._actions.submit_action_definition(
                action_definition=action_def, context=compilation_context,
            )
        except Exception as exc:
            self._flows.update_flow_status(flow_id, "failed")
            raise AgentMessagingError(
                f"submit_action_definition failed: {exc}",
            ) from exc

        # Stamp the active action onto the thread now that we have an id.
        try:
            self._repo.update_thread(
                thread.id,
                ThreadStatusUpdate(
                    active_action_id=action_id,
                    active_flow_id=flow_id,
                ),
            )
        except RepositoryError as exc:
            raise AgentMessagingError(
                f"failed to stamp active action: {exc}",
            ) from exc

        return _PendingTurn(
            thread=thread,
            originator_message=originator,
            flow_id=flow_id,
            action_id=action_id,
        )

    def _record_dispatch_failure(
        self,
        *,
        thread_id: str,
        request_message_id: str,
        error_message: str,
    ) -> None:
        """Roll a QUEUED thread back to ERROR after dispatch fails.

        Mirrors :meth:`_record_failure` (the runner-side compensation),
        but is invoked from the synchronous send path before the action
        queue ever sees the action.  Failure to record the failure is
        swallowed — the original dispatch exception is what the caller
        sees, and stranded-thread cleanup falls back to operator action.
        """
        try:
            self._repo.append_message(
                thread_id=thread_id,
                message=NewMessage(
                    role=MessageRole.SYSTEM,
                    kind=MessageKind.ERROR,
                    content=[TextPart(type="text", text=error_message)],
                    error={
                        "request_message_id": request_message_id,
                        "message": error_message,
                        "phase": "dispatch",
                    },
                ),
                update=ThreadStatusUpdate(
                    status=ThreadStatus.ERROR,
                    clear_active_action=True,
                ),
            )
        except RepositoryError:
            return

    def _clamp_timeout(self, requested: int | None) -> int:
        default = self._config.default_timeout_seconds
        ceiling = self._config.max_timeout_seconds
        if requested is None:
            return default
        if requested < 1:
            raise AgentRequestInvalidError(
                "timeout_seconds must be a positive integer",
            )
        return min(requested, ceiling)

    # ------------------------------------------------------------------
    # Internals — backend invocation + result recording
    # ------------------------------------------------------------------

    @staticmethod
    def _build_execution_params(
        *,
        prompt: str,
        working_directory: str | None,
        timeout_seconds: int,
    ) -> ExecutionParams:
        # Imported lazily so this module does not couple to guarded_agent's
        # data model except where needed.
        from ananta.llm.guarded_agent.models import ExecutionParams

        return ExecutionParams(
            prompt=prompt,
            working_directory=working_directory,
            timeout_seconds=timeout_seconds,
        )

    @staticmethod
    def _invoke_backend(
        backend: object, params: ExecutionParams,
    ) -> ExecutionResult:
        # ``execute_agent`` on GuardedAgentInterface is async; the
        # action runner is sync.  Plugins may expose a paired
        # ``execute_agent_sync`` that handles the asyncio bridge — fall
        # back to scheduling the coroutine ourselves if not.
        from ananta.llm.guarded_agent.models import (  # noqa: PLC0415
            ExecutionResult as _ExecutionResult,
        )

        sync_call = getattr(backend, "execute_agent_sync", None)
        if callable(sync_call):
            result = sync_call(params)
        else:
            import asyncio  # noqa: PLC0415
            from collections.abc import Coroutine  # noqa: PLC0415

            async_call = getattr(backend, "execute_agent", None)
            if not callable(async_call):
                raise AgentMessagingError(
                    f"backend {type(backend).__name__} does not implement execute_agent",
                )
            coro = async_call(params)
            if not isinstance(coro, Coroutine):
                raise AgentMessagingError(
                    f"backend {type(backend).__name__}.execute_agent must "
                    f"return a coroutine; got {type(coro).__name__}",
                )
            result = asyncio.run(coro)
        if not isinstance(result, _ExecutionResult):
            raise AgentMessagingError(
                f"backend returned unexpected execution result type: {type(result).__name__}",
            )
        return result

    def _record_success(
        self,
        *,
        thread_id: str,
        request_message_id: str,
        backend: str,
        result: ExecutionResult,
    ) -> AgentMessageRow:
        text = result.text or ""
        agent_message = self._repo.append_message(
            thread_id=thread_id,
            message=NewMessage(
                role=MessageRole.AGENT,
                kind=MessageKind.RESULT,
                content=[TextPart(type="text", text=text)],
                action_id=None,
                backend_session_id=result.backend_session_id,
                metadata={
                    "request_message_id": request_message_id,
                    "backend": backend,
                    "metrics": dict(result.metrics or {}),
                },
            ),
            update=ThreadStatusUpdate(
                status=ThreadStatus.IDLE,
                backend_session_id=result.backend_session_id,
                clear_active_action=True,
            ),
        )
        return agent_message

    def _record_interrupted(
        self,
        *,
        thread_id: str,
        request_message_id: str,
        backend: str,
        result: ExecutionResult,
    ) -> AgentMessageRow:
        """Persist an interruption marker and transition to INTERRUPTED.

        Both Codex and Claude Code can return ``interrupted=True`` from
        ``execute_agent`` (watch-phrase trip, timeout, manual interrupt
        once that ships).  Without this branch the runner would record
        IDLE with a partial transcript and the originator would believe
        the turn ran to completion.
        """
        text = result.text or ""
        message = self._repo.append_message(
            thread_id=thread_id,
            message=NewMessage(
                role=MessageRole.SYSTEM,
                kind=MessageKind.STATUS,
                content=[TextPart(type="text", text=text or "(interrupted)")],
                action_id=None,
                backend_session_id=result.backend_session_id,
                metadata={
                    "request_message_id": request_message_id,
                    "backend": backend,
                    "interrupted": True,
                    "interrupted_on": result.interrupted_on,
                    "metrics": dict(result.metrics or {}),
                },
            ),
            update=ThreadStatusUpdate(
                status=ThreadStatus.INTERRUPTED,
                backend_session_id=result.backend_session_id,
                clear_active_action=True,
            ),
        )
        return message

    def _record_failure(
        self, thread_id: str, request_message_id: str, error_message: str,
    ) -> None:
        try:
            self._repo.append_message(
                thread_id=thread_id,
                message=NewMessage(
                    role=MessageRole.SYSTEM,
                    kind=MessageKind.ERROR,
                    content=[TextPart(type="text", text=error_message)],
                    error={
                        "request_message_id": request_message_id,
                        "message": error_message,
                    },
                ),
                update=ThreadStatusUpdate(
                    status=ThreadStatus.ERROR,
                    clear_active_action=True,
                ),
            )
        except RepositoryError:
            # The original failure is what matters; surface it after the
            # caller's ``raise``.  We deliberately swallow this secondary
            # error to keep the primary cause visible.
            return


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_subpath(candidate: Path, root: Path) -> bool:
    try:
        candidate.relative_to(root)
    except ValueError:
        return False
    return True


def _build_open_thread_metadata(
    request: OpenAgentThreadRequest,
) -> dict[str, object]:
    """Lift ``request.context`` into the thread-row metadata dict."""
    metadata: dict[str, object] = {}
    if request.context is None:
        return metadata
    if request.context.summary:
        metadata["context_summary"] = request.context.summary
    if request.context.tags:
        metadata["context_tags"] = list(request.context.tags)
    return metadata


def _build_terminal_payload(
    *,
    thread_id: str,
    request_message_id: str,
    response_message_id: str,
    status: ThreadStatus,
    backend: str,
    result: ExecutionResult,
) -> dict[str, object]:
    """Build the bridge-delivered payload for a non-error terminal turn.

    Used for both the IDLE (success) and INTERRUPTED branches — they
    share the same envelope shape, differing only in ``status``,
    ``interrupted``, and ``interrupted_on``.
    """
    return {
        "thread_id": thread_id,
        "request_message_id": request_message_id,
        "response_message_id": response_message_id,
        "status": status.value,
        "backend": backend,
        "backend_session_id": result.backend_session_id,
        "text": result.text,
        "interrupted": result.interrupted,
        "interrupted_on": result.interrupted_on if result.interrupted else None,
        "artifacts": [],
        "metrics": dict(result.metrics or {}),
        "error": None,
    }


__all__ = [
    "RUN_TURN_PROCESS_KEY",
    "AgentBackendUnavailableError",
    "AgentMessagingConfig",
    "AgentMessagingDisabledError",
    "AgentMessagingError",
    "AgentMessagingService",
    "AgentRequestInvalidError",
    "AgentThreadBusyError",
    "AgentThreadClosedError",
    "AgentThreadNotFoundError",
    "AgentThreadRunningError",
    "AgentThreadUnauthorizedError",
    "_BridgeDeliveryEndpoint",
    "role_message_external_id",
]
