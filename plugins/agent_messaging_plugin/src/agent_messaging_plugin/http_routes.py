# pyright: reportUnusedFunction=false
"""FastAPI route registration for the merged bridge plugin.

Wires every ``/api/v1/bridge/*`` route in the consolidated plan's
route table onto a single ``FastAPI`` app, delegating to the layered
collaborators that own each concern:

* :class:`BridgeSessionManager` — bridge lifecycle + long-poll events.
* :class:`PeerRegistry` — peer bindings + native wake adapters.
* :class:`PlatformSurface` — process_* / download.
* ``agent_messaging_service`` — durable thread/message persistence.

Handlers stay deliberately thin: validate the request shape, delegate,
serialize.  Every collaborator-specific exception maps to a single
HTTP status code so MCP clients see a stable contract irrespective of
which subsystem failed.

Ported from ``agent_channel_plugin.plugin`` and
``claude_code_channel_plugin.plugin`` during the bridge-consolidation
work — see
``workbench/2026-05-16_codex_mcp_channel_and_inter_agent_outstanding_work.md``
sub-phase 2e for the route table and dispatch semantics.
"""

from __future__ import annotations

import dataclasses
import logging
import re
from datetime import datetime as _dt
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Callable

from ananta.llm.agent_messaging.models import (
    ListAgentMessagesRequest,
    OpenAgentThreadRequest,
    PeerInboxRequest,
    SendAgentMessageRequest,
    TextPart,
)
from ananta.llm.agent_messaging.schema import (
    META_KEY_DELIVERY_EXTERNAL_ID,
    RECIPIENT_KIND_ROLE,
    ROLE_THREAD_PREFIX,
)
from ananta.llm.agent_messaging.service import (
    AgentMessagingError,
    role_message_external_id,
)
from ananta.llm.agent_messaging.state_results import StateOperationError
from fastapi import Body, FastAPI
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

from .bridge_lifecycle import run_full_bridge_cleanup
from .bridge_sessions import (
    BridgeNotFoundError,
    BridgeQueueFullError,
    BridgeSessionManager,
)
from .peer_dispatch import (
    EVENT_PEER_MESSAGE,
    EVENT_POST_MESSAGE,
    IMPORTANT_MARKER_RE,
    NativeWakeError,
    dispatch_peer_send,
)
from .peer_registry import (
    PeerAmbiguousError,
    PeerRegistry,
    PeerUnreachableError,
)
from .platform_surface import BridgeError, PlatformSurface
from .role_binding_store import (
    UNCLAIMED_SESSION_ID,
    list_roles_for_agent_instance,
    refresh_role_binding_cas,
)

if TYPE_CHECKING:
    from .models import BridgeBinding, QueuedEvent

logger = logging.getLogger(__name__)


# Bridge-event types that carry an IMPORTANT peer/role delivery (the native
# wake rides ``post_message``; the no-adapter path rides ``peer_message``).
# The watcher-ack consumption reconcile only inspects these.
_WATCHER_DELIVERY_EVENT_TYPES: Final[frozenset[str]] = frozenset(
    {EVENT_POST_MESSAGE, EVENT_PEER_MESSAGE},
)


API_PREFIX: Final[str] = "/api/v1/bridge"

_AGENT_ID_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9._-]{1,64}")

# Map BridgeError.code → HTTP status.  Anything not in this table maps
# to 400, matching the platform_surface convention of "bad input"
# being the default failure mode.
_BRIDGE_ERROR_STATUS: Final[dict[str, int]] = {
    "bridge.no_active_bridge": 404,
    "bridge.action_result_not_found": 404,
    "bridge.blob_not_found": 404,
    "bridge.blob_storage_unavailable": 503,
    "bridge.state_service_unavailable": 503,
    "bridge.discovery_unavailable": 503,
    "bridge.dependencies_not_ready": 503,
    "bridge.process_export_disabled": 503,
    "bridge.process_call_failed": 500,
    "bridge.attachment_missing": 400,
    "bridge.invalid_process_key": 400,
    "bridge.process_not_allowed": 403,
}


# ---------------------------------------------------------------------------
# Inline request models — kept here so the wire contract is greppable from
# one place and so the handlers stay declarative.
# ---------------------------------------------------------------------------


class OpenBridgeBody(BaseModel):
    """Optional parent_pid for sibling-bridge pairing inside one OS tree."""

    parent_pid: int | None = None


class ProcessSearchBody(BaseModel):
    query: str
    max_results: int = 10


class ProcessSchemaBody(BaseModel):
    process_key: str


class ProcessCallBody(BaseModel):
    process_key: str
    arguments: dict[str, Any]
    reason: str | None = None


class PeerRegisterBody(BaseModel):
    agent_id: str
    agent_instance_id: str
    session_label: str = ""
    parent_pid: int | None = None
    # S1 (agent_session_id splice): the STABLE per-logical-session key the client
    # already sends (mcp_bridge/__main__.py + forwarder.py). The model previously
    # DROPPED it silently; it is now stored on the BridgeBinding and drives the
    # reconnect state-table self-refresh (S2). Empty when the launcher did not
    # export HOMUNCULUS_AGENT_SESSION_ID -> self-refresh disabled (S1.5 loud log).
    agent_session_id: str = ""
    # D-IF7: opt-in flag for the per-bridge SessionInferenceProvider
    # sidecar (v4 §4). When True, the post-register hook binds an
    # inference vertex for this bridge so the wrapper at
    # ``inference_service/__init__.py`` can route process_error +
    # process_results through the calling coding-agent session.
    # Defaults to False so non-coding-agent peers (older Codex sessions,
    # MCP-only consumers) remain unaffected.
    provides_inference: bool = False


class PeerSendBody(BaseModel):
    # User-facing field is ``peer_id`` (matches every MCP tool schema and
    # the legacy Node bridges' contract).  The underlying service layer
    # calls the same value ``peer_agent_id`` — translation happens at
    # the service-call boundary inside _peer_send_impl.
    peer_id: str
    peer_agent_instance_id: str | None = None
    content: list[dict[str, Any]] = Field(default_factory=list)


class PeerDrainBody(BaseModel):
    # v10 Control #5: the repair loop POSTs this to fetch the oldest page of
    # IMPORTANT role messages owed to the roles the calling bridge holds.
    limit: int = 50


class PeerDeliveredBody(BaseModel):
    # v10 Control #5: the repair loop / live emit path POSTs this after a
    # successful emit to flip ``delivered=true``. ``recipient_key`` is echoed
    # from the drain row and ownership-fenced server-side (the caller must
    # currently hold that role).
    external_id: str
    recipient_key: str


class PeerDeliveredDirectBody(BaseModel):
    # REL-05: the repair loop POSTs this after a successful re-emit of a
    # direct-wake row to record the emission (emit_count += 1, last_emitted_at).
    # ``message_id`` is echoed from the drain row and recipient-fenced
    # server-side (the caller must be the row's fixed recipient instance).
    message_id: str


class ThreadOpenBody(BaseModel):
    backend: str
    working_directory: str | None = None
    title: str | None = None
    context: dict[str, Any] | None = None
    initial_message: dict[str, Any] | None = None


class ThreadSendBody(BaseModel):
    content: list[dict[str, Any]]
    response_mode: str = "async"
    timeout_seconds: int | None = None


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_routes(
    app: FastAPI,
    *,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    platform_surface: PlatformSurface,
    agent_messaging_service: Any,
    config: Any,
    state_service: Any | None = None,
    readiness_probe: Callable[[], bool] | None = None,
    inference_provider_register: Callable[..., None] | None = None,
    inference_provider_clear: Callable[[str], int] | None = None,
    autonomic_on_register: Callable[..., str] | None = None,
    autonomic_on_close: Callable[[str], str] | None = None,
) -> None:
    """Attach every ``/api/v1/bridge/*`` route onto ``app``.

    All collaborators are passed by keyword so the call site is the
    single source of truth for dependency wiring; nothing in this
    module reaches back into a plugin instance.

    On ``peer/register`` the route re-points every role this session holds in the
    ``agent_role_binding`` state table to the reconnected ``agent_instance_id``
    (``_state_table_self_refresh`` → ``refresh_role_binding_cas``, keyed on the
    stable ``agent_session_id``). This is the reconnect-survival path (S1–S3
    splice) that replaced the retired address-book self-refresh.

    ``readiness_probe`` is an optional zero-arg callable returning True
    once every load-bearing surface is serving (bridge uvicorn +, when
    enabled, the streamable HTTP MCP listener). When provided and
    returning False, ``/api/v1/bridge/health`` answers ``503 starting``
    so external probes (cloud ALB target-group health check, cross-host
    smokes, monitoring) gate on the full surface rather than just the
    bridge uvicorn being bound. Omitting the parameter preserves the
    legacy unconditional-200 contract for local dev / tests that don't
    enable the streamable transport. See iter-9 finding in
    ``workbench/2026-06-12_aws_swap_smoke_run_report.md`` §3 Bug 2.
    """
    long_poll_timeout_s = _config_int(config, "long_poll_timeout_seconds", 25)
    # REL-05 re-emit window + cap (Q1: plugin-config-surfaced, constants
    # defaults). Read via getattr — ``config`` is the _BridgeRuntimeConfig
    # dataclass (no ``.get``), so _config_int would only ever yield the default.
    re_emit_window_s = float(getattr(config, "re_emit_window_seconds", 300))
    re_emit_cap = int(getattr(config, "re_emit_cap", 3))

    _register_bridge_lifecycle_routes(
        app,
        bridge_manager=bridge_manager,
        peer_registry=peer_registry,
        agent_messaging_service=agent_messaging_service,
        long_poll_timeout_s=long_poll_timeout_s,
        inference_provider_clear=inference_provider_clear,
        autonomic_on_close=autonomic_on_close,
    )
    _register_platform_surface_routes(
        app, platform_surface=platform_surface, bridge_manager=bridge_manager,
    )
    _register_peer_routes(
        app,
        bridge_manager=bridge_manager,
        peer_registry=peer_registry,
        agent_messaging_service=agent_messaging_service,
        state_service=state_service,
        inference_provider_register=inference_provider_register,
        autonomic_on_register=autonomic_on_register,
        re_emit_window_s=re_emit_window_s,
        re_emit_cap=re_emit_cap,
    )
    _register_agent_thread_routes(
        app,
        bridge_manager=bridge_manager,
        agent_messaging_service=agent_messaging_service,
    )

    @app.get(f"{API_PREFIX}/health")
    async def health() -> JSONResponse:
        if readiness_probe is not None and not readiness_probe():
            return JSONResponse(
                content={"status": "starting"}, status_code=503,
            )
        return JSONResponse(content={"status": "healthy"}, status_code=200)


# ---------------------------------------------------------------------------
# Bridge lifecycle: open / close / events
# ---------------------------------------------------------------------------


def _register_bridge_lifecycle_routes(
    app: FastAPI,
    *,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    agent_messaging_service: Any,
    long_poll_timeout_s: int,
    inference_provider_clear: Callable[[str], int] | None = None,
    autonomic_on_close: Callable[[str], str] | None = None,
) -> None:
    @app.post(f"{API_PREFIX}/open")
    async def open_bridge(
        body: OpenBridgeBody = Body(default_factory=OpenBridgeBody),  # noqa: B008
    ) -> JSONResponse:
        # Bridge open is the one operation that needs the homunculus
        # name — the session_id_factory bound onto the manager closes
        # over it, so the handler doesn't need it explicitly.
        bridge = bridge_manager.open(
            homunculus_name="", parent_pid=body.parent_pid,
        )
        return JSONResponse(
            content={
                "bridge_id": bridge.bridge_id,
                "session_id": bridge.session_id,
                "long_poll_timeout_seconds": long_poll_timeout_s,
            },
            status_code=200,
        )

    @app.post(f"{API_PREFIX}/{{bridge_id}}/close")
    async def close_bridge(
        bridge_id: str,
        body: dict[str, Any] = Body(default_factory=dict),  # noqa: B008,ARG001
    ) -> JSONResponse:
        # REL-09: the SAME full cleanup the idle sweeper runs — sidecar
        # clear + tombstone (D-IF7, pre-unregister so list_by_bridge sees
        # the bindings), the sys:autonomic Trigger-2 hook (INF-01 §D.9,
        # pre-unregister for the same reason), then the registry
        # unregister. Runs BEFORE bridge close so a concurrent peer_send
        # racing the close cannot resolve to a dead bridge; without the
        # unregister, open/close cycles accumulate zombie bindings and
        # peer_send eventually hits peer_ambiguous.
        run_full_bridge_cleanup(
            bridge_id,
            inference_provider_clear=inference_provider_clear,
            autonomic_on_close=autonomic_on_close,
            unregister=peer_registry.unregister,
        )
        bridge_manager.close(bridge_id)
        return JSONResponse(content={"status": "closed"}, status_code=200)

    @app.get(f"{API_PREFIX}/{{bridge_id}}/events")
    async def events_bridge(
        bridge_id: str, after: int = -1,
    ) -> JSONResponse:
        try:
            acked, events = await bridge_manager.events_after(
                bridge_id, after, timeout_s=long_poll_timeout_s,
            )
        except BridgeNotFoundError:
            return _bridge_not_found(bridge_id)
        # An actively long-polling registered client (the no-MCP watcher) is
        # alive — bump its binding so "last active" liveness agrees with the
        # delivery path, same as peer_inbox does. Without this, a watcher that
        # only ever long-polls looks inactive to binding-liveness consumers
        # while its bridge keeps answering, one half of the persisted_silent
        # black hole (Dax Part 13).
        binding = _lookup_binding_for_bridge(peer_registry, bridge_id)
        if binding is not None:
            peer_registry.touch_binding(binding.agent_instance_id)
            # Dax Part 14: a watcher's cursor ack proves the acked events
            # streamed into its watch output — the pull equivalent of entering
            # a turn. Retire the acked deliveries' re-emit/escalation insurance
            # so an armed watcher never escalates recipient_gone. MCP-transport
            # bridges confirm via /peer/drain instead — never here (their
            # forwarder drains events without the model having read them).
            if binding.is_watcher and acked:
                _consume_watcher_acked_events(
                    agent_messaging_service, binding, acked,
                )
        next_cursor = events[-1].cursor if events else after
        return JSONResponse(
            content={
                "events": [dataclasses.asdict(e) for e in events],
                "next_cursor": next_cursor,
            },
            status_code=200,
        )


# ---------------------------------------------------------------------------
# Platform surface: process_* / download
# ---------------------------------------------------------------------------


def _register_platform_surface_routes(
    app: FastAPI,
    *,
    platform_surface: PlatformSurface,
    bridge_manager: BridgeSessionManager,
) -> None:
    @app.post(f"{API_PREFIX}/{{bridge_id}}/process/search")
    async def process_search_route(
        bridge_id: str, body: ProcessSearchBody,
    ) -> JSONResponse:
        # M5 §14.7: pass bridge_id through so process_search applies the
        # bridge session's per-session allowlist on top of global policy.
        try:
            payload = platform_surface.process_search(
                query=body.query, max_results=body.max_results,
                bridge_id=bridge_id,
            )
        except BridgeError as exc:
            return _bridge_error_response(exc)
        return JSONResponse(content=payload, status_code=200)

    @app.post(f"{API_PREFIX}/{{bridge_id}}/process/schema")
    async def process_schema_route(
        bridge_id: str, body: ProcessSchemaBody,
    ) -> JSONResponse:
        # M5 §14.7: pass bridge_id so the schema lookup is gated by the
        # bridge session's allowlist before the discovery call runs.
        try:
            payload = platform_surface.process_schema(
                process_key=body.process_key, bridge_id=bridge_id,
            )
        except BridgeError as exc:
            return _bridge_error_response(exc)
        return JSONResponse(content=payload, status_code=200)

    @app.post(f"{API_PREFIX}/{{bridge_id}}/process/call")
    async def process_call_route(
        bridge_id: str, body: ProcessCallBody,
    ) -> JSONResponse:
        bridge = bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            return _bridge_not_found(bridge_id)
        trigger_data: dict[str, Any] = {
            "bridge_id": bridge_id,
            "session_id": bridge.session_id,
        }
        if body.reason is not None:
            trigger_data["reason"] = body.reason
        try:
            payload = platform_surface.process_call(
                process_key=body.process_key,
                arguments=body.arguments,
                trigger_data=trigger_data,
            )
        except BridgeError as exc:
            return _bridge_error_response(exc)
        return JSONResponse(content=payload, status_code=200)

    @app.get(f"{API_PREFIX}/{{bridge_id}}/process/result/{{action_id}}")
    async def process_result_route(
        bridge_id: str, action_id: str,
    ) -> JSONResponse:
        _ = bridge_id
        try:
            payload = platform_surface.process_result(action_id=action_id)
        except BridgeError as exc:
            return _bridge_error_response(exc)
        return JSONResponse(content=payload, status_code=200)

    @app.get(f"{API_PREFIX}/{{bridge_id}}/download/{{blob_id}}")
    async def download_route(
        bridge_id: str, blob_id: str,
    ) -> Response:
        _ = bridge_id
        try:
            blob = platform_surface.download(blob_id=blob_id)
        except BridgeError as exc:
            return _bridge_error_response(exc)
        return Response(
            content=blob.content,
            media_type=blob.mime_type,
            headers={
                "Content-Disposition": (
                    f'attachment; filename="{blob.filename}"'
                ),
            },
        )


# ---------------------------------------------------------------------------
# Peer routes: register / list / send / inbox
# ---------------------------------------------------------------------------


def _state_table_self_refresh(
    state_service: Any | None,
    *,
    agent_session_id: str,
    new_agent_instance_id: str,
) -> str:
    """Reconnect self-refresh: re-point every role this session holds in the
    ``agent_role_binding`` STATE TABLE to the rotated ``agent_instance_id`` (S2).

    Replaces the retired address-book self-refresh. The state table is the SOLE
    resolution authority (v10 Control #2.C), so re-pointing it is what actually
    keeps role wakes reaching a reconnected holder (whose ``agent_instance_id``
    rotated). Filtered on the stable ``agent_session_id`` — one CAS re-points ALL
    held roles. NEVER raises: a self-refresh fault is loud but the bridge
    registration MUST still succeed (200).

    Tokens: ``rerouted:<n>`` (re-pointed n held roles) / ``no_roles`` (session
    holds none) / ``no_session_key`` (S1.5: launched without a stable
    HOMUNCULUS_AGENT_SESSION_ID -> self-refresh disabled, LOUD) / ``no_state_service`` /
    ``error`` (a state fault — logged loud with traceback, registration kept).
    """
    if not agent_session_id or agent_session_id == UNCLAIMED_SESSION_ID:
        logger.warning(
            "peer/register: no stable agent_session_id (%r) — this session's roles "
            "will NOT survive reconnect (state-table self-refresh disabled). Launch "
            "with HOMUNCULUS_AGENT_SESSION_ID exported to enable reconnect survival.",
            agent_session_id,
        )
        return "no_session_key"
    if state_service is None:
        logger.warning(
            "peer/register: state_service unbound — role self-refresh skipped "
            "(session %r)", agent_session_id,
        )
        return "no_state_service"
    try:
        rerouted = refresh_role_binding_cas(
            state_service,
            agent_session_id=agent_session_id,
            new_agent_instance_id=new_agent_instance_id,
        )
    except Exception:  # noqa: BLE001 — a self-refresh fault is loud-but-non-fatal; the bridge MUST still register
        # Systemic tradeoff (one-shot-at-register, NO retry): during a reconnect
        # STORM a state fault brings the cohort up 200 with roles NOT re-pointed —
        # they strand until the next reconnect / re-claim. Accepted + LOUD (this
        # error log fires per fault); a retry/queue is a separate hardening call.
        logger.exception(
            "peer/register: state-table role self-refresh FAULTED (session %r, new "
            "agi=%s); registration kept 200 but held roles were NOT re-pointed and "
            "will strand until re-claim", agent_session_id, new_agent_instance_id,
        )
        return "error"
    if rerouted >= 1:
        logger.info(
            "peer/register: re-pointed %d role(s) to agent_instance_id=%s on "
            "reconnect (session %r)", rerouted, new_agent_instance_id,
            agent_session_id,
        )
        return f"rerouted:{rerouted}"
    return "no_roles"


def _direct_wake_self_refresh(
    agent_messaging_service: Any | None,
    *,
    agent_session_id: str,
    new_agent_instance_id: str,
) -> str:
    """Reconnect self-refresh for the DIRECT-WAKE outbox (Fork-1a).

    The direct-send sibling of :func:`_state_table_self_refresh`: on reconnect a
    recipient's ``agent_instance_id`` rotates, so re-home every owed direct-wake
    row this session is the RECIPIENT of onto the just-registered successor
    (``rehome_owed_direct_wakes``, keyed on the stable ``agent_session_id``) —
    curing the REL-01 ``recipient_gone`` orphan class the RCA proved bites, and
    re-entering the ``recipient_gone`` rows whose terminality WAS the orphan bug.
    Same one-shot-at-register, loud-but-non-fatal posture as the role
    self-refresh: a fault brings the bridge up 200 with rows NOT re-homed (they
    strand until the next reconnect); NO retry queue (Architect ruling 2).

    Tokens: ``rehomed:<n>`` / ``no_owed`` / ``no_session_key`` / ``no_service`` /
    ``error``.
    """
    if not agent_session_id or agent_session_id == UNCLAIMED_SESSION_ID:
        return "no_session_key"
    if agent_messaging_service is None:
        return "no_service"
    try:
        rehomed = agent_messaging_service.rehome_owed_direct_wakes(
            agent_session_id=agent_session_id,
            new_agent_instance_id=new_agent_instance_id,
        )
    except Exception:  # noqa: BLE001 — loud-but-non-fatal; the bridge MUST still register
        logger.exception(
            "peer/register: direct-wake re-home FAULTED (session %r, new agi=%s); "
            "registration kept 200 but owed direct rows were NOT re-homed and will "
            "strand until the next reconnect",
            agent_session_id, new_agent_instance_id,
        )
        return "error"
    if rehomed >= 1:
        logger.info(
            "peer/register: re-homed %d owed direct-wake row(s) to "
            "agent_instance_id=%s on reconnect (session %r)",
            rehomed, new_agent_instance_id, agent_session_id,
        )
        return f"rehomed:{rehomed}"
    return "no_owed"


def _register_peer_routes(
    app: FastAPI,
    *,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    agent_messaging_service: Any,
    state_service: Any | None = None,
    inference_provider_register: Callable[..., None] | None = None,
    autonomic_on_register: Callable[..., str] | None = None,
    re_emit_window_s: float = 300.0,
    re_emit_cap: int = 3,
) -> None:
    @app.post(f"{API_PREFIX}/{{bridge_id}}/peer/register")
    async def peer_register_route(
        bridge_id: str, body: PeerRegisterBody,
    ) -> JSONResponse:
        bridge = bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            return _bridge_not_found(bridge_id)
        agent_id = body.agent_id.strip()
        if not _AGENT_ID_RE.fullmatch(agent_id):
            return _validation_error(
                "invalid_agent_id",
                "agent_id must match [A-Za-z0-9._-]{1,64}",
            )
        agent_instance_id = body.agent_instance_id.strip()
        if not agent_instance_id:
            return _validation_error(
                "missing_agent_instance_id",
                "agent_instance_id is required",
            )
        bridge.agent_instance_id = agent_instance_id
        if body.parent_pid is not None:
            bridge.parent_pid = body.parent_pid
        # Import here to avoid a circular import at module load time;
        # models is pulled in via TYPE_CHECKING for the type hints.
        from .models import BridgeBinding  # noqa: PLC0415
        binding = BridgeBinding(
            bridge_id=bridge_id,
            agent_id=agent_id,
            agent_instance_id=agent_instance_id,
            session_label=body.session_label,
            parent_pid=body.parent_pid,
            agent_session_id=body.agent_session_id,
        )
        # ``register`` returns the EFFECTIVE label — the preserve-on-empty
        # path (2026-06-01 §4.2) restores a stored label when the incoming
        # one is empty (auto-reconnect's stale subprocess cache). The
        # response + the bridge.session_label cache MUST reflect that
        # restored value, otherwise the peer-side reconnect announcement
        # interpolates the wrong (empty) label.
        effective_label = peer_registry.register(binding)
        bridge.session_label = effective_label
        # D-IF7 sidecar populate (v4 §4) — bind the per-bridge inference
        # vertex AFTER peer_registry.register succeeds so the wrapper can
        # resolve a provider for this agent_instance_id on its next
        # process_error / process_results call. Best-effort try/except —
        # sidecar bind failures must not block the registration response.
        if inference_provider_register is not None and body.provides_inference:
            try:
                inference_provider_register(
                    bridge_id=bridge_id,
                    agent_instance_id=agent_instance_id,
                    agent_id=agent_id,
                    session_label=effective_label,
                )
            except Exception:  # noqa: BLE001 — sidecar populate is best-effort
                logger.warning(
                    "inference_provider_register raised for bridge %s "
                    "agent_instance_id=%s; registration kept; provider sidecar "
                    "WILL be missing for this peer",
                    bridge_id, agent_instance_id, exc_info=True,
                )
        self_refresh_action = _state_table_self_refresh(
            state_service,
            agent_session_id=body.agent_session_id,
            new_agent_instance_id=agent_instance_id,
        )
        # Fork-1a: re-home this session's owed DIRECT-wake rows off the rotated
        # instance onto the successor (the direct-send sibling of the role
        # self-refresh above; same loud-but-non-fatal one-shot posture).
        direct_rehome_action = _direct_wake_self_refresh(
            agent_messaging_service,
            agent_session_id=body.agent_session_id,
            new_agent_instance_id=agent_instance_id,
        )
        # INF-01 Trigger-1 (§D.9): fill a vacant / dead-holder sys:autonomic
        # slot with the just-registered session. Runs AFTER the sidecar
        # populate (a provider must exist for the claim to serve) and AFTER
        # the S2 self-refresh (a reconnecting holder re-points its binding
        # first, so it reads back as live-held → no-op). Best-effort: the
        # registration response never fails on lifecycle policy.
        autonomic_action = "disabled"
        if autonomic_on_register is not None:
            try:
                autonomic_action = autonomic_on_register(
                    agent_id=agent_id,
                    agent_instance_id=agent_instance_id,
                    agent_session_id=body.agent_session_id,
                    session_label=effective_label,
                    provides_inference=body.provides_inference,
                )
            except Exception:  # noqa: BLE001 — lifecycle policy never blocks a registration
                logger.warning(
                    "autonomic_on_register raised for bridge %s agi=%s; "
                    "registration kept",
                    bridge_id, agent_instance_id, exc_info=True,
                )
                autonomic_action = "error"
        return JSONResponse(
            content={
                "agent_id": agent_id,
                "agent_instance_id": agent_instance_id,
                "session_label": effective_label,
                "parent_pid": body.parent_pid,
                "bridge_id": bridge_id,
                "status": "registered",
                "self_refresh": self_refresh_action,
                "direct_rehome": direct_rehome_action,
                "autonomic": autonomic_action,
            },
            status_code=200,
        )

    @app.get(f"{API_PREFIX}/{{bridge_id}}/peer/list")
    async def peer_list_route(bridge_id: str) -> JSONResponse:
        _ = bridge_id
        snapshot = peer_registry.list_agent_ids()
        instances: dict[str, list[dict[str, object]]] = {
            agent_id: [
                {
                    "agent_instance_id": b.agent_instance_id,
                    "session_label": b.session_label,
                    "parent_pid": b.parent_pid,
                    # ``registered_at`` is a deprecated alias for
                    # ``created_at`` — kept for one release so older
                    # MCP clients keep working.  New consumers should
                    # read ``created_at`` + ``updated_at``.
                    "registered_at": b.created_at,
                    "created_at": b.created_at,
                    "updated_at": b.updated_at,
                }
                for b in bindings
            ]
            for agent_id, bindings in snapshot.items()
        }
        return JSONResponse(
            content={
                "agent_ids": sorted(snapshot.keys()),
                "instances": instances,
            },
            status_code=200,
        )

    @app.get(f"{API_PREFIX}/{{bridge_id}}/current_identity")
    async def current_identity_route(bridge_id: str) -> JSONResponse:
        bridge = bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            return _bridge_not_found(bridge_id)
        binding = _lookup_binding_for_bridge(peer_registry, bridge_id)
        if binding is None:
            return _validation_error(
                "identity_not_registered",
                "this bridge has not registered an agent_id; "
                "POST /peer/register first",
            )
        roles_or_error = _read_roles_held(
            state_service,
            agent_instance_id=binding.agent_instance_id,
        )
        if isinstance(roles_or_error, JSONResponse):
            return roles_or_error
        return JSONResponse(
            content={
                "transport": "bridge_http",
                "homunculus_name": "",
                "agent_id": binding.agent_id,
                "agent_instance_id": binding.agent_instance_id,
                "agent_session_id": binding.agent_session_id,
                "session_label": binding.session_label,
                "bridge_id": bridge_id,
                "mcp_session_id": "",
                "roles_held": roles_or_error,
                "identity_trust": "bridge_registered",
                "streamable_no_auth": False,
            },
            status_code=200,
        )

    @app.post(f"{API_PREFIX}/{{bridge_id}}/peer/send")
    async def peer_send_route(
        bridge_id: str, body: PeerSendBody,
    ) -> JSONResponse:
        return _peer_send_impl(
            bridge_id=bridge_id,
            body=body,
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
            agent_messaging_service=agent_messaging_service,
        )

    @app.get(f"{API_PREFIX}/{{bridge_id}}/peer/inbox")
    async def peer_inbox_route(
        bridge_id: str,
        after: str | None = None,
        limit: int = 50,
        include_important: bool = False,
        role_after: str | None = None,
    ) -> JSONResponse:
        bridge = bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            return _bridge_not_found(bridge_id)
        sender_binding = _lookup_binding_for_bridge(peer_registry, bridge_id)
        if sender_binding is None:
            return _validation_error(
                "identity_not_registered",
                "this bridge has not registered an agent_id; "
                "POST /peer/register first",
            )
        try:
            after_dt = _parse_iso_after(after)
        except ValueError as exc:
            return _validation_error("invalid_after", str(exc))
        try:
            page = agent_messaging_service.peer_inbox(
                PeerInboxRequest(
                    recipient_agent_id=sender_binding.agent_id,
                    recipient_agent_instance_id=sender_binding.agent_instance_id,
                    recipient_agent_session_id=sender_binding.agent_session_id,
                    after_created_at=after_dt,
                    limit=max(1, min(limit, 100)),
                    include_important=include_important,
                    # Opaque role-section cursor; the service validates it and
                    # raises AgentMessagingError on a malformed/forged token
                    # (caught below → error response). Fail-closed by design.
                    role_after=role_after,
                ),
            )
        except AgentMessagingError as exc:
            return _agent_messaging_error_response(exc)
        # The bridge calling peer_inbox is alive — bump its binding so
        # peer_list shows recent activity even if no peer_send has run.
        peer_registry.touch_binding(sender_binding.agent_instance_id)
        # Dax Part 14: the watch client's arm-time catch-up drain prints every
        # returned entry into the watch output — those exact rows are surfaced,
        # so their re-emit/escalation insurance retires here. Watcher-only:
        # an MCP session's consumption authority stays the /peer/drain
        # reconcile.
        if sender_binding.is_watcher and include_important:
            _consume_watcher_inbox_page(
                agent_messaging_service, sender_binding, page,
            )
        return JSONResponse(
            content=_serialize_peer_inbox(page, sender_binding),
            status_code=200,
        )

    @app.post(f"{API_PREFIX}/{{bridge_id}}/peer/drain")
    async def peer_drain_route(
        bridge_id: str, body: PeerDrainBody,
    ) -> JSONResponse:
        # v10 Control #5 + REL-05: return the oldest page of un-CONSUMED IMPORTANT
        # ROLE messages owed to the roles this bridge holds, PLUS the owed DIRECT
        # rows for this instance. The binding is derived server-side from
        # ``bridge_id`` (NOT caller-supplied). Before listing, RECONCILE
        # consumption: this bridge's in-memory ``last_model_activity_at`` marks
        # consumed any owed row emitted before that turn (so the active session's
        # rows drop out of the owed set). A deaf session has a stale/empty stamp →
        # nothing consumed → its owed rows re-emit (the Vector-B insurance).
        bridge = bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            return _bridge_not_found(bridge_id)
        sender_binding = _lookup_binding_for_bridge(peer_registry, bridge_id)
        if sender_binding is None:
            return _validation_error(
                "identity_not_registered",
                "this bridge has not registered an agent_id; "
                "POST /peer/register first",
            )
        agent_instance_id = sender_binding.agent_instance_id
        limit = max(1, min(body.limit, 100))
        activity_at = _parse_iso_after(bridge.last_model_activity_at or None)
        try:
            if activity_at is not None:
                agent_messaging_service.reconcile_role_consumption(
                    agent_instance_id=agent_instance_id, activity_at=activity_at,
                )
                agent_messaging_service.reconcile_direct_consumption(
                    agent_instance_id=agent_instance_id, activity_at=activity_at,
                )
            rows = agent_messaging_service.list_undelivered_for_instance(
                agent_instance_id=agent_instance_id,
                limit=limit,
                re_emit_window_s=re_emit_window_s,
                cap=re_emit_cap,
            )
            direct_rows = agent_messaging_service.list_owed_direct_for_instance(
                agent_instance_id=agent_instance_id,
                limit=limit,
                re_emit_window_s=re_emit_window_s,
                cap=re_emit_cap,
            )
        except AgentMessagingError as exc:
            return _agent_messaging_error_response(exc)
        peer_registry.touch_binding(agent_instance_id)
        return JSONResponse(
            content={
                "undelivered": [_serialize_role_drain_row(r) for r in rows],
                "undelivered_direct": [
                    _serialize_direct_wake_row(r) for r in direct_rows
                ],
                # N3: the forwarder marks a re-emit [re-emit n/cap ...] — cap
                # rides the envelope so the client never hard-codes it.
                "re_emit_cap": re_emit_cap,
            },
            status_code=200,
        )

    @app.post(f"{API_PREFIX}/{{bridge_id}}/peer/delivered")
    async def peer_delivered_route(
        bridge_id: str, body: PeerDeliveredBody,
    ) -> JSONResponse:
        # v10 Control #5: flip ``delivered=true`` after a successful emit.
        # Idempotent + ownership-fenced (a displaced holder can't mark
        # deliveries) — ``flagged=false`` if the caller no longer holds the role.
        # REL-05: also records the emit bookkeeping (emit_count/last_emitted_at +
        # F3 emitted_to instance) inside the fenced confirm.
        bridge = bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            return _bridge_not_found(bridge_id)
        sender_binding = _lookup_binding_for_bridge(peer_registry, bridge_id)
        if sender_binding is None:
            return _validation_error(
                "identity_not_registered",
                "this bridge has not registered an agent_id; "
                "POST /peer/register first",
            )
        try:
            flagged = agent_messaging_service.mark_delivered_for_instance(
                external_id=body.external_id,
                recipient_key=body.recipient_key,
                agent_instance_id=sender_binding.agent_instance_id,
            )
        except AgentMessagingError as exc:
            return _agent_messaging_error_response(exc)
        return JSONResponse(content={"flagged": flagged}, status_code=200)

    @app.post(f"{API_PREFIX}/{{bridge_id}}/peer/delivered_direct")
    async def peer_delivered_direct_route(
        bridge_id: str, body: PeerDeliveredDirectBody,
    ) -> JSONResponse:
        # REL-05: record a re-emission of a direct-wake row after a successful
        # emit. Recipient-fenced (only the row's fixed recipient instance can
        # bump it) — ``flagged=false`` if the caller is not that recipient.
        bridge = bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            return _bridge_not_found(bridge_id)
        sender_binding = _lookup_binding_for_bridge(peer_registry, bridge_id)
        if sender_binding is None:
            return _validation_error(
                "identity_not_registered",
                "this bridge has not registered an agent_id; "
                "POST /peer/register first",
            )
        try:
            flagged = agent_messaging_service.mark_direct_emitted_for_instance(
                message_id=body.message_id,
                agent_instance_id=sender_binding.agent_instance_id,
            )
        except AgentMessagingError as exc:
            return _agent_messaging_error_response(exc)
        return JSONResponse(content={"flagged": flagged}, status_code=200)


# ---------------------------------------------------------------------------
# Agent thread routes: open / send / messages / status / close
# ---------------------------------------------------------------------------


def _register_agent_thread_routes(
    app: FastAPI,
    *,
    bridge_manager: BridgeSessionManager,
    agent_messaging_service: Any,
) -> None:
    @app.post(f"{API_PREFIX}/{{bridge_id}}/agent/thread/open")
    async def agent_thread_open_route(
        bridge_id: str, body: ThreadOpenBody,
    ) -> JSONResponse:
        bridge = bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            return _bridge_not_found(bridge_id)
        try:
            request = OpenAgentThreadRequest(
                bridge_id=bridge_id,
                session_id=bridge.session_id,
                backend=body.backend,
                working_directory=body.working_directory,
                title=body.title,
                context=_parse_context(body.context),
                initial_message=_parse_initial_message(body.initial_message),
            )
            opened = agent_messaging_service.open_thread(request)
        except AgentMessagingError as exc:
            return _agent_messaging_error_response(exc)
        return JSONResponse(
            content={
                "thread_id": opened.thread_id,
                "message_id": opened.message_id,
                "action_id": opened.action_id,
                "flow_id": opened.flow_id,
                "status": opened.status.value,
            },
            status_code=200,
        )

    @app.post(f"{API_PREFIX}/{{bridge_id}}/agent/{{thread_id}}/send")
    async def agent_send_route(
        bridge_id: str, thread_id: str, body: ThreadSendBody,
    ) -> JSONResponse:
        bridge = bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            return _bridge_not_found(bridge_id)
        try:
            request = SendAgentMessageRequest(
                bridge_id=bridge_id,
                thread_id=thread_id,
                content=_parse_text_parts(body.content),
                response_mode=body.response_mode,
                timeout_seconds=body.timeout_seconds,
            )
            queued = agent_messaging_service.send_message(request)
        except AgentMessagingError as exc:
            return _agent_messaging_error_response(exc)
        return JSONResponse(
            content={
                "thread_id": queued.thread_id,
                "message_id": queued.message_id,
                "action_id": queued.action_id,
                "flow_id": queued.flow_id,
                "status": queued.status.value,
            },
            status_code=200,
        )

    @app.get(f"{API_PREFIX}/{{bridge_id}}/agent/{{thread_id}}/messages")
    async def agent_messages_route(
        bridge_id: str,
        thread_id: str,
        after_cursor: int = 0,
        limit: int = 50,
    ) -> JSONResponse:
        bridge = bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            return _bridge_not_found(bridge_id)
        try:
            page = agent_messaging_service.list_messages(
                ListAgentMessagesRequest(
                    bridge_id=bridge_id,
                    thread_id=thread_id,
                    after_cursor=after_cursor,
                    limit=limit,
                ),
            )
        except AgentMessagingError as exc:
            return _agent_messaging_error_response(exc)
        return JSONResponse(
            content=_serialize_messages_page(page),
            status_code=200,
        )

    @app.get(f"{API_PREFIX}/{{bridge_id}}/agent/{{thread_id}}/status")
    async def agent_status_route(
        bridge_id: str, thread_id: str,
    ) -> JSONResponse:
        bridge = bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            return _bridge_not_found(bridge_id)
        try:
            status = agent_messaging_service.get_status(
                thread_id=thread_id, bridge_id=bridge_id,
            )
        except AgentMessagingError as exc:
            return _agent_messaging_error_response(exc)
        return JSONResponse(
            content=_serialize_thread_status(status),
            status_code=200,
        )

    @app.post(f"{API_PREFIX}/{{bridge_id}}/agent/{{thread_id}}/close")
    async def agent_close_route(
        bridge_id: str,
        thread_id: str,
        body: dict[str, Any] = Body(default_factory=dict),  # noqa: B008,ARG001
    ) -> JSONResponse:
        bridge = bridge_manager.get(bridge_id)
        if bridge is None or bridge.closed:
            return _bridge_not_found(bridge_id)
        try:
            closed = agent_messaging_service.close_thread(
                thread_id=thread_id, bridge_id=bridge_id,
            )
        except AgentMessagingError as exc:
            return _agent_messaging_error_response(exc)
        return JSONResponse(
            content={
                "thread_id": closed.thread_id,
                "status": closed.status.value,
            },
            status_code=200,
        )


# ---------------------------------------------------------------------------
# peer/send dispatch — the IMPORTANT-marker semantics live here so the
# route handler stays a thin wrapper.
# ---------------------------------------------------------------------------


def _peer_send_impl(
    *,
    bridge_id: str,
    body: PeerSendBody,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    agent_messaging_service: Any,
) -> JSONResponse:
    """Thin route adapter — validate inputs, delegate to :mod:`peer_dispatch`.

    Routing-table membership (bridge existence, sender binding) is the
    HTTP-layer concern: if a request hits ``/bridge/<bridge_id>/peer/send``
    and the bridge is closed, that's a 404 specific to this transport.
    The IMPORTANT-marker semantics + wake-vs-channel dispatch live in
    :func:`agent_messaging_plugin.peer_dispatch.dispatch_peer_send`,
    shared with the streamable transport.
    """
    bridge = bridge_manager.get(bridge_id)
    if bridge is None or bridge.closed:
        return _bridge_not_found(bridge_id)
    sender_binding = _lookup_binding_for_bridge(peer_registry, bridge_id)
    if sender_binding is None:
        return _validation_error(
            "identity_not_registered",
            "this bridge has not registered an agent_id; "
            "POST /peer/register first",
        )
    try:
        content = _parse_text_parts(body.content)
    except AgentMessagingError as exc:
        return _agent_messaging_error_response(exc)
    try:
        outcome = dispatch_peer_send(
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
            agent_messaging_service=agent_messaging_service,
            sender_bridge_id=bridge_id,
            sender_agent_id=sender_binding.agent_id,
            sender_agent_instance_id=sender_binding.agent_instance_id,
            sender_session_label=sender_binding.session_label,
            sender_parent_pid=sender_binding.parent_pid,
            peer_id=body.peer_id,
            peer_agent_instance_id=body.peer_agent_instance_id,
            content=content,
        )
    except PeerAmbiguousError as exc:
        return JSONResponse(
            content={
                "code": "peer_ambiguous",
                "message": str(exc),
                "peer_agent_id": exc.peer_agent_id,
                "candidate_instance_ids": exc.candidate_instance_ids,
                "candidate_session_labels": exc.candidate_session_labels,
            },
            status_code=400,
        )
    except PeerUnreachableError as exc:
        return JSONResponse(
            content={"code": "peer_unreachable", "message": str(exc)},
            status_code=404,
        )
    except BridgeNotFoundError as exc:
        return JSONResponse(
            content={
                "code": "peer_unreachable",
                "message": (
                    f"recipient bridge is no longer registered: {exc}"
                ),
            },
            status_code=404,
        )
    except BridgeQueueFullError:
        return JSONResponse(
            content={
                "code": "peer_queue_full",
                "message": (
                    f"recipient {body.peer_id} event queue is full"
                ),
            },
            status_code=503,
        )
    except NativeWakeError as exc:
        return JSONResponse(
            content={"code": "native_wake_failed", "message": str(exc)},
            status_code=502,
        )
    except AgentMessagingError as exc:
        return _agent_messaging_error_response(exc)
    return JSONResponse(content=outcome.to_payload(), status_code=200)


# ---------------------------------------------------------------------------
# Helpers — parsing
# ---------------------------------------------------------------------------


def _parse_text_parts(value: list[dict[str, Any]]) -> list[TextPart]:
    if not value:
        raise AgentMessagingError(
            "content must be a non-empty list of text parts",
        )
    parts: list[TextPart] = []
    for raw in value:
        kind = str(raw.get("type") or "text")
        if kind != "text":
            raise AgentMessagingError(
                f"content part type {kind!r} is not supported",
            )
        parts.append(TextPart(type="text", text=str(raw.get("text") or "")))
    return parts


def _parse_context(value: dict[str, Any] | None) -> Any | None:
    if value is None:
        return None
    from ananta.llm.agent_messaging.models import (  # noqa: PLC0415
        AgentThreadContext,
    )
    summary = _optional_str(value.get("summary"))
    tags_raw = value.get("tags")
    if tags_raw is None:
        tags: tuple[str, ...] = ()
    elif isinstance(tags_raw, list | tuple):
        tags = tuple(str(t) for t in tags_raw)
    else:
        raise AgentMessagingError("context.tags must be a list")
    return AgentThreadContext(summary=summary, tags=tags)


def _parse_initial_message(value: dict[str, Any] | None) -> Any | None:
    if value is None:
        return None
    from ananta.llm.agent_messaging.models import (  # noqa: PLC0415
        InitialMessage,
    )
    content = _parse_text_parts(_coerce_content_list(value.get("content")))
    response_mode = str(value.get("response_mode") or "async")
    timeout_raw = value.get("timeout_seconds")
    timeout_seconds = timeout_raw if isinstance(timeout_raw, int) else None
    return InitialMessage(
        content=content,
        response_mode=response_mode,
        timeout_seconds=timeout_seconds,
    )


def _coerce_content_list(value: object) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise AgentMessagingError(
            "initial_message.content must be a list of text parts",
        )
    return [v for v in value if isinstance(v, dict)]


def _parse_iso_after(value: str | None) -> _dt | None:
    if not value:
        return None
    try:
        return _dt.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"after must be ISO-8601 datetime: {exc}") from exc




# ---------------------------------------------------------------------------
# Helpers — serialization
# ---------------------------------------------------------------------------


def _serialize_messages_page(page: Any) -> dict[str, Any]:
    return {
        "thread_id": page.thread_id,
        "messages": [_serialize_message(m) for m in page.messages],
        "next_cursor": page.next_cursor,
        "status": page.status.value,
    }


def _serialize_message(message: Any) -> dict[str, Any]:
    return {
        "id": message.id,
        "cursor": message.cursor,
        "role": message.role.value,
        "kind": message.kind.value,
        "content": [
            {"type": p.type, "text": p.text} for p in message.content
        ],
        "action_id": message.action_id,
        "backend_session_id": message.backend_session_id,
        "error": message.error,
        "artifacts": [dataclasses.asdict(a) for a in message.artifacts],
        "metadata": message.metadata,
        "created_at": message.created_at.isoformat(),
    }


def _serialize_thread_status(status: Any) -> dict[str, Any]:
    return {
        "thread_id": status.thread_id,
        "status": status.status.value,
        "backend": status.backend,
        "last_message_cursor": status.last_message_cursor,
        "updated_at": status.updated_at.isoformat(),
        "active_action_id": status.active_action_id,
        "active_flow_id": status.active_flow_id,
        "backend_session_id": status.backend_session_id,
    }


def _serialize_peer_inbox_entry(entry: Any) -> dict[str, Any]:
    return {
        "thread_id": entry.thread_id,
        "sender_agent_id": entry.sender_agent_id,
        "sender_agent_instance_id": entry.sender_agent_instance_id,
        "sender_session_label": entry.sender_session_label,
        "message": _serialize_message(entry.message),
    }


def _serialize_peer_inbox(page: Any, sender_binding: BridgeBinding) -> dict[str, Any]:
    # The role section (role_entries + next_role_cursor) is emitted ADDITIVELY;
    # the instance section keys are byte-for-byte unchanged. role_section_status
    # / role_section_error carry the v10 Q1 fault-domain outcome so a caller can
    # tell an empty role section (no role messages) from a failed one.
    return {
        "recipient_agent_id": page.recipient_agent_id,
        "recipient_agent_instance_id": sender_binding.agent_instance_id,
        "entries": [_serialize_peer_inbox_entry(entry) for entry in page.entries],
        "next_after_created_at": (
            page.next_after_created_at.isoformat()
            if page.next_after_created_at is not None
            else None
        ),
        "role_entries": [
            _serialize_peer_inbox_entry(entry) for entry in page.role_entries
        ],
        "next_role_cursor": page.next_role_cursor,
        "role_section_status": page.role_section_status.value,
        "role_section_error": page.role_section_error,
    }


def _role_drain_prose(content_raw: object) -> str:
    """Join stored role-message parts to prose, stripping the IMPORTANT marker.

    ``persist_role_message`` stores the ORIGINAL content (the IMPORTANT marker
    still embedded in the first part). The live wake path delivers
    ``prose[marker_match.end():]`` (marker stripped). The repair drain MUST
    deliver the SAME marker-stripped prose so a re-delivered message reads
    identically to a live one — no leading "IMPORTANT" artifact, byte-for-byte
    delivery parity. The content round-trips from the JSONB column as a native
    list of ``{"type","text"}`` dicts (psycopg deserialises JSONB → objects).
    """
    if not isinstance(content_raw, list):
        return ""
    prose = "\n".join(
        str(part.get("text") or "")
        for part in content_raw
        if isinstance(part, dict)
    )
    marker_match = IMPORTANT_MARKER_RE.match(prose)
    if marker_match is not None:
        return prose[marker_match.end():]
    return prose


def _serialize_role_drain_row(row: dict[str, Any]) -> dict[str, Any]:
    """Project an undelivered role envelope row to the drain wire shape.

    Carries exactly what the forwarder's repair loop needs to emit the message
    and flip its flag: the deterministic ``external_id`` + ``recipient_key``
    (echoed back to ``POST /peer/delivered``, ownership-fenced), the
    ``message_id`` + sender provenance (the targeted-reply meta on the native
    wake), the synthetic thread handle, and the marker-stripped content prose
    (delivery parity with the live wake path — see :func:`_role_drain_prose`).
    REL-05: ``emit_count`` + ``created_at`` ride along so a re-emit can be marked
    ``[re-emit n/cap ... originally sent <created_at>]`` (N3).
    """
    return {
        "external_id": row.get("external_id"),
        "recipient_key": row.get("recipient_key"),
        "message_id": row.get("message_id"),
        "sender_agent_id": row.get("sender_agent_id"),
        "sender_agent_instance_id": row.get("sender_agent_instance_id"),
        "sender_session_label": row.get("sender_session_label"),
        "thread_id": row.get("thread_id"),
        "important": bool(row.get("important", False)),
        "emit_count": int(row.get("emit_count") or 0),
        "created_at": _iso_or_empty(row.get("created_at")),
        "content": _role_drain_prose(row.get("content")),
    }


def _serialize_direct_wake_row(row: dict[str, Any]) -> dict[str, Any]:
    """Project an owed direct-wake row to the drain wire shape (REL-05).

    Mirrors :func:`_serialize_role_drain_row` for direct sends: the
    ``message_id`` (echoed back to ``POST /peer/delivered_direct``,
    recipient-fenced), the sender provenance for the targeted-reply meta, the
    thread handle, the marker-stripped content, and ``emit_count`` +
    ``created_at`` for the N3 re-emit marker.
    """
    return {
        "message_id": row.get("message_id"),
        "thread_id": row.get("thread_id"),
        "sender_agent_id": row.get("sender_agent_id"),
        "sender_agent_instance_id": row.get("sender_agent_instance_id"),
        "sender_session_label": row.get("sender_session_label"),
        "emit_count": int(row.get("emit_count") or 0),
        "created_at": _iso_or_empty(row.get("created_at")),
        "content": _role_drain_prose(row.get("content")),
    }


def _iso_or_empty(value: object) -> str:
    """A stored timestamp cell as a string (empty for a missing/non-string cell)."""
    return value if isinstance(value, str) else ""


# ---------------------------------------------------------------------------
# Helpers — error responses
# ---------------------------------------------------------------------------


def _bridge_error_response(exc: BridgeError) -> JSONResponse:
    status = _BRIDGE_ERROR_STATUS.get(exc.code, 400)
    return JSONResponse(
        content={"code": exc.code, "message": exc.message},
        status_code=status,
    )


def _agent_messaging_error_response(exc: AgentMessagingError) -> JSONResponse:
    return JSONResponse(
        content={"code": exc.code, "message": str(exc)},
        status_code=exc.http_status,
    )


def _bridge_not_found(bridge_id: str) -> JSONResponse:
    return JSONResponse(
        content={
            "code": "bridge_not_found",
            "message": f"bridge {bridge_id} not found or closed",
        },
        status_code=404,
    )


def _validation_error(code: str, message: str) -> JSONResponse:
    return JSONResponse(
        content={"code": code, "message": message},
        status_code=400,
    )


def _state_unavailable(message: str) -> JSONResponse:
    return _bridge_error_response(
        BridgeError("bridge.state_service_unavailable", message),
    )


def _read_roles_held(
    state_service: Any | None, *, agent_instance_id: str,
) -> list[str] | JSONResponse:
    if state_service is None:
        return _state_unavailable(
            "state_service is not bound; cannot read agent_role_binding.",
        )
    try:
        return list_roles_for_agent_instance(state_service, agent_instance_id)
    except StateOperationError as exc:
        return _state_unavailable(str(exc))


# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------


def _lookup_binding_for_bridge(
    peer_registry: PeerRegistry, bridge_id: str,
) -> BridgeBinding | None:
    """Find the BridgeBinding registered for ``bridge_id`` (linear scan)."""
    for bindings in peer_registry.list_agent_ids().values():
        for binding in bindings:
            if binding.bridge_id == bridge_id:
                return binding
    return None


def _consume_watcher_acked_events(
    agent_messaging_service: Any,
    binding: BridgeBinding,
    acked: list[QueuedEvent],
) -> None:
    """Stamp watcher-acked IMPORTANT deliveries consumed (Dax Part 14).

    Role deliveries are recognised by the Control #5 ``delivery_external_id``
    meta key (stamped on both the native-wake and channel-event transports);
    direct wakes by their ``message_id`` meta. Both marks are predicated
    (``consumed=false``) and the direct mark is fenced to the acking binding's
    own instance, so a re-ack or a non-delivery event is a no-op.
    """
    for event in acked:
        if event.event_type not in _WATCHER_DELIVERY_EVENT_TYPES:
            continue
        external_id = str(event.meta.get(META_KEY_DELIVERY_EXTERNAL_ID) or "")
        if external_id:
            agent_messaging_service.mark_role_consumed_on_ack(
                external_id=external_id,
            )
            continue
        message_id = str(event.meta.get("message_id") or "")
        if message_id:
            agent_messaging_service.mark_direct_consumed_on_ack(
                message_id=message_id,
                recipient_agent_instance_id=binding.agent_instance_id,
            )


def _consume_watcher_inbox_page(
    agent_messaging_service: Any,
    binding: BridgeBinding,
    page: Any,
) -> None:
    """Stamp watcher catch-up-drained IMPORTANT rows consumed (Dax Part 14).

    The instance section's entries map to direct-wake rows by ``message_id``
    (silent messages have no outbox row — the fenced mark is a no-op). Role
    entries recover their role name from the synthetic ``role:`` thread handle
    and re-derive the deterministic delivery external_id; the predicated mark
    skips silent and already-consumed rows.
    """
    for entry in page.entries:
        agent_messaging_service.mark_direct_consumed_on_ack(
            message_id=str(entry.message.id),
            recipient_agent_instance_id=binding.agent_instance_id,
        )
    for entry in page.role_entries:
        role_name = str(entry.thread_id).removeprefix(ROLE_THREAD_PREFIX)
        agent_messaging_service.mark_role_consumed_on_ack(
            external_id=role_message_external_id(
                RECIPIENT_KIND_ROLE, role_name, str(entry.message.id),
            ),
        )


def _config_int(config: Any, key: str, default: int) -> int:
    if config is None:
        return default
    getter = getattr(config, "get", None)
    if not callable(getter):
        return default
    value = getter(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        return default
    return int(value)


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


__all__ = [
    "API_PREFIX",
    "EVENT_PEER_MESSAGE",
    "register_routes",
]
