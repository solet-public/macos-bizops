"""JSON-RPC dispatch for the Streamable HTTP MCP transport.

Pure synchronous logic: a single ``dispatch_request`` entry point
takes a parsed JSON-RPC request envelope and the platform
collaborators, returns either:

* ``JsonRpcResponse`` — a JSON-RPC response envelope (or ``None`` for
  notifications that intentionally have no response).
* ``JsonRpcError`` (raised) — a JSON-RPC error response envelope.

The router layer translates these into HTTP responses (200 with JSON,
202 Accepted, or HTTP error).  There is **no FastAPI here**: dispatch
stays pure Python so the unit shape is exercised by the smoke client,
not by HTTP plumbing.

Tool calls map 1:1 to the same collaborator methods the
``/api/v1/bridge/*`` routes use — :class:`PlatformSurface` for
``process_*`` / ``download``, ``agent_messaging_service``
for ``agent_*`` and ``peer_*``, :class:`PeerRegistry` for routing
table reads.  Errors raised by collaborators are mapped to JSON-RPC
errors with codes drawn from the MCP / JSON-RPC standard ranges
(``-32602`` invalid params, ``-32603`` internal error, ``-32000`` to
``-32099`` server-defined).

The IMPORTANT-marker semantics and the wake-vs-channel dispatch live
in :mod:`agent_messaging_plugin.peer_dispatch` — both this dispatcher
and the legacy bridge HTTP route call into the same shared
:func:`dispatch_peer_send`.
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING, Any, Final

from ananta.llm.agent_messaging.models import (
    ListAgentMessagesRequest,
    OpenAgentThreadRequest,
    PeerInboxRequest,
    SendAgentMessageRequest,
    TextPart,
)
from ananta.llm.agent_messaging.service import AgentMessagingError
from ananta.llm.agent_messaging.state_results import StateOperationError

from ..bridge_sessions import BridgeNotFoundError, BridgeQueueFullError
from ..constants import TUNNEL_PASSTHROUGH_SENTINEL
from ..peer_dispatch import (
    IMPORTANT_MARKER_RE,
    NativeWakeError,
    dispatch_peer_send,
    dispatch_role_send,
)
from ..peer_registry import PeerAmbiguousError, PeerUnreachableError
from ..platform_surface import BridgeError
from ..role_binding_store import (
    RoleBindingVacantError,
    list_roles_for_agent_instance,
    resolve_role_binding,
)
from .tools import (
    SERVER_VERSION,
    SUPPORTED_PROTOCOL_VERSION,
    TOOLS,
    build_server_instructions,
    build_server_name,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from ..bridge_sessions import BridgeSessionManager
    from ..peer_registry import PeerRegistry
    from ..platform_surface import PlatformSurface
    from .session import StreamableSession

logger = logging.getLogger(__name__)


# JSON-RPC + MCP error codes used by this dispatcher.  Standard JSON-RPC
# codes (-32700 .. -32603) cover protocol-level failures; the
# server-defined range (-32000 .. -32099) is reserved for application
# errors that do not fit the standard codes.  PARSE_ERROR and
# INTERNAL_ERROR are public so the router can raise them on JSON parse
# failures and unhandled exceptions respectively.
PARSE_ERROR: Final[int] = -32700
INTERNAL_ERROR: Final[int] = -32603
_INVALID_REQUEST: Final[int] = -32600
_METHOD_NOT_FOUND: Final[int] = -32601
_INVALID_PARAMS: Final[int] = -32602
_TOOL_CALL_FAILED: Final[int] = -32000  # server-defined: tool invocation error
_BRIDGE_NOT_FOUND: Final[int] = -32001  # server-defined: synthetic bridge gone
_PEER_AMBIGUOUS: Final[int] = -32002
_PEER_UNREACHABLE: Final[int] = -32003
_IMPORTANT_ROLE_DEDUPE_WINDOW_SECONDS: Final[float] = 120.0
_IMPORTANT_ROLE_DEDUPE_LOCK = threading.Lock()
_IMPORTANT_ROLE_DEDUPE: dict[tuple[str, str, str], float] = {}

# ---------------------------------------------------------------------
# JSON-RPC envelope types.
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class JsonRpcRequest:
    """Parsed JSON-RPC request envelope."""

    method: str
    params: dict[str, Any]
    id: int | str | None  # noqa: A003 — JSON-RPC field name


@dataclass(frozen=True, slots=True)
class JsonRpcResponse:
    """JSON-RPC response envelope (success path)."""

    id: int | str | None  # noqa: A003 — JSON-RPC field name
    result: dict[str, Any]

    def to_wire(self) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": self.id, "result": self.result}


class JsonRpcError(Exception):
    """JSON-RPC error response envelope (raised, then serialised by router)."""

    def __init__(
        self,
        code: int,
        message: str,
        *,
        request_id: int | str | None = None,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code: int = code
        self.message: str = message
        self.request_id: int | str | None = request_id
        self.data: dict[str, Any] | None = data

    def to_wire(self) -> dict[str, Any]:
        error_obj: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.data is not None:
            error_obj["data"] = self.data
        return {"jsonrpc": "2.0", "id": self.request_id, "error": error_obj}


# ---------------------------------------------------------------------
# Collaborator bundle — passed once at router build time, then carried
# through dispatch calls.  Keeps the dispatcher signature compact while
# making the test seam obvious.
# ---------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DispatchContext:
    """Platform collaborators the dispatcher uses to satisfy tool calls.

    ``homunculus_name`` is the identity the platform was booted with
    (``$HOMUNCULUS_NAME``); it flows into the ``initialize`` response's
    ``serverInfo.name`` + ``instructions`` so MCP clients surface the
    actual deployment identity rather than a generic label.
    Empty string = no homunculus identity available; the tools-layer
    fallback kicks in.
    """

    bridge_manager: BridgeSessionManager
    peer_registry: PeerRegistry
    platform_surface: PlatformSurface
    agent_messaging_service: Any  # AgentMessagingServiceInterface — Any avoids cycle
    state_service: Any | None = None
    homunculus_name: str = ""


# ---------------------------------------------------------------------
# Public entry point.
# ---------------------------------------------------------------------


def parse_envelope(raw: dict[str, Any]) -> JsonRpcRequest:
    """Validate JSON-RPC envelope shape; raise :class:`JsonRpcError` on failure."""
    if raw.get("jsonrpc") != "2.0":
        raise JsonRpcError(
            _INVALID_REQUEST,
            "jsonrpc field must be '2.0'",
            request_id=raw.get("id"),
        )
    method = raw.get("method")
    if not isinstance(method, str) or not method:
        raise JsonRpcError(
            _INVALID_REQUEST,
            "method must be a non-empty string",
            request_id=raw.get("id"),
        )
    params = raw.get("params") or {}
    if not isinstance(params, dict):
        raise JsonRpcError(
            _INVALID_PARAMS,
            "params must be an object",
            request_id=raw.get("id"),
        )
    return JsonRpcRequest(method=method, params=params, id=raw.get("id"))


def dispatch_request(
    request: JsonRpcRequest,
    *,
    session: StreamableSession | None,
    context: DispatchContext,
) -> JsonRpcResponse | None:
    """Route one JSON-RPC request to the matching handler.

    Returns:
        :class:`JsonRpcResponse` for requests carrying an id.
        ``None`` for notifications (no id, no response per JSON-RPC spec).

    Raises:
        :class:`JsonRpcError` on any failure; the router translates to
        a JSON-RPC error response.
    """
    if request.method == "initialize":
        return _handle_initialize(request, homunculus_name=context.homunculus_name)
    if request.method.startswith("notifications/"):
        # Notifications have no id and expect no response.  The router
        # returns 202 Accepted at the HTTP layer.  We still validate
        # the params object shape so a malformed notification is
        # surfaced as an HTTP error.
        return None
    if session is None:
        raise JsonRpcError(
            _INVALID_REQUEST,
            "Mcp-Session-Id header is required after initialize",
            request_id=request.id,
        )
    if request.method == "tools/list":
        return _handle_tools_list(request)
    if request.method == "tools/call":
        return _handle_tools_call(request, session=session, context=context)
    raise JsonRpcError(
        _METHOD_NOT_FOUND,
        f"method {request.method!r} is not implemented by this server",
        request_id=request.id,
    )


# ---------------------------------------------------------------------
# initialize / tools.list / tools.call handlers.
# ---------------------------------------------------------------------


def _handle_initialize(
    request: JsonRpcRequest, *, homunculus_name: str,
) -> JsonRpcResponse:
    """Return InitializeResult; the router allocates the session header."""
    client_protocol = str(request.params.get("protocolVersion") or "")
    # The spec lets us echo whichever version we support; pick the one
    # the client asked for if it matches our supported set, else fall
    # back to ours so the client can decide whether to retry.
    protocol_version = (
        client_protocol
        if client_protocol == SUPPORTED_PROTOCOL_VERSION
        else SUPPORTED_PROTOCOL_VERSION
    )
    return JsonRpcResponse(
        id=request.id,
        result={
            "protocolVersion": protocol_version,
            "serverInfo": {
                "name": build_server_name(homunculus_name),
                "version": SERVER_VERSION,
            },
            "capabilities": {
                "tools": {"listChanged": False},
                # The notifications/claude/channel pseudo-capability is
                # the same one the stdio bridge advertises via
                # experimental_capabilities, kept here so a single
                # phone-side renderer handles both transports.
                "experimental": {"claude/channel": {}},
            },
            "instructions": build_server_instructions(homunculus_name),
        },
    )


def _handle_tools_list(request: JsonRpcRequest) -> JsonRpcResponse:
    return JsonRpcResponse(id=request.id, result={"tools": list(TOOLS)})


def _handle_tools_call(
    request: JsonRpcRequest,
    *,
    session: StreamableSession,
    context: DispatchContext,
) -> JsonRpcResponse:
    """Dispatch one ``tools/call`` to the matching tool implementation.

    Validation + exception-mapping are factored into helpers so this
    orchestrator stays radon-cc A: validate, look up handler, invoke,
    wrap. The handler invocation funnels every collaborator-specific
    exception through :func:`_invoke_tool_handler`, which maps each
    one to its canonical JSON-RPC error code.
    """
    name, arguments = _parse_tools_call_params(request)
    handler = _TOOL_HANDLERS.get(name)
    if handler is None:
        raise JsonRpcError(
            _METHOD_NOT_FOUND,
            f"tool {name!r} is not implemented by this server",
            request_id=request.id,
        )
    payload = _invoke_tool_handler(
        handler=handler,
        name=name,
        arguments=arguments,
        session=session,
        context=context,
        request_id=request.id,
    )
    return JsonRpcResponse(
        id=request.id,
        result={
            "structuredContent": payload,
            "content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False)}],
            "isError": False,
        },
    )


def _parse_tools_call_params(
    request: JsonRpcRequest,
) -> tuple[str, dict[str, Any]]:
    """Validate ``tools/call`` params; return ``(name, arguments)``.

    Raises :class:`JsonRpcError` (-32602 invalid_params) when the
    request shape doesn't match the MCP ``tools/call`` contract.
    """
    name = request.params.get("name")
    arguments = request.params.get("arguments") or {}
    if not isinstance(name, str) or not name:
        raise JsonRpcError(
            _INVALID_PARAMS,
            "tools/call.name must be a non-empty string",
            request_id=request.id,
        )
    if not isinstance(arguments, dict):
        raise JsonRpcError(
            _INVALID_PARAMS,
            "tools/call.arguments must be an object",
            request_id=request.id,
        )
    return name, arguments


def _invoke_tool_handler(
    *,
    handler: Callable[..., dict[str, Any]],
    name: str,
    arguments: dict[str, Any],
    session: StreamableSession,
    context: DispatchContext,
    request_id: int | str | None,
) -> dict[str, Any]:
    """Run ``handler`` and translate collaborator exceptions to JSON-RPC errors.

    Each collaborator (BridgeError / AgentMessaging / peer-registry /
    bridge-sessions / native-wake) maps to a single JSON-RPC error code
    so MCP clients see a stable contract regardless of which subsystem
    failed. Pure pass-through on success.
    """
    try:
        return handler(arguments, session=session, context=context)
    except JsonRpcError as exc:
        if exc.request_id is None:
            raise JsonRpcError(
                exc.code,
                exc.message,
                request_id=request_id,
                data=exc.data,
            ) from exc
        raise
    except BridgeError as exc:
        raise _bridge_error_to_jsonrpc(exc, request_id=request_id) from exc
    except AgentMessagingError as exc:
        raise JsonRpcError(
            _TOOL_CALL_FAILED,
            str(exc),
            request_id=request_id,
            data={"code": exc.code, "tool": name},
        ) from exc
    except PeerAmbiguousError as exc:
        raise JsonRpcError(
            _PEER_AMBIGUOUS,
            str(exc),
            request_id=request_id,
            data={
                "code": "peer_ambiguous",
                "candidate_instance_ids": exc.candidate_instance_ids,
                "candidate_session_labels": exc.candidate_session_labels,
            },
        ) from exc
    except PeerUnreachableError as exc:
        raise JsonRpcError(
            _PEER_UNREACHABLE,
            str(exc),
            request_id=request_id,
            data={"code": "peer_unreachable"},
        ) from exc
    except BridgeNotFoundError as exc:
        raise JsonRpcError(
            _BRIDGE_NOT_FOUND,
            str(exc),
            request_id=request_id,
            data={"code": "bridge_not_found"},
        ) from exc
    except BridgeQueueFullError as exc:
        raise JsonRpcError(
            _TOOL_CALL_FAILED,
            f"recipient event queue is full: {exc}",
            request_id=request_id,
            data={"code": "peer_queue_full"},
        ) from exc
    except NativeWakeError as exc:
        raise JsonRpcError(
            _TOOL_CALL_FAILED,
            str(exc),
            request_id=request_id,
            data={"code": "native_wake_failed", "peer_agent_id": exc.peer_agent_id},
        ) from exc


def _bridge_error_to_jsonrpc(
    exc: BridgeError, *, request_id: int | str | None,
) -> JsonRpcError:
    """Map a :class:`BridgeError` to an MCP-shaped JSON-RPC error."""
    return JsonRpcError(
        _TOOL_CALL_FAILED,
        exc.message,
        request_id=request_id,
        data={"code": exc.code},
    )


# ---------------------------------------------------------------------
# Tool handlers — one per MCP tool, signature
# ``(arguments, *, session, context) -> dict``.
#
# All return value shapes mirror the stdio bridge's ``Forwarder``
# methods (forwarder.py).  Anything that would have been a 4xx HTTP
# response from the bridge surface becomes a raised exception caught
# above and turned into a JSON-RPC error.
# ---------------------------------------------------------------------


def _tool_current_identity(
    arguments: dict[str, Any],  # noqa: ARG001 — uniform handler signature
    *,
    session: StreamableSession,
    context: DispatchContext,
) -> dict[str, Any]:
    return {
        "transport": "streamable_http",
        "homunculus_name": context.homunculus_name,
        "agent_id": session.agent_id,
        "agent_instance_id": session.agent_instance_id,
        "agent_session_id": session.agent_session_id,
        "session_label": session.session_label,
        "bridge_id": session.bridge_id,
        "mcp_session_id": session.mcp_session_id,
        "roles_held": _read_roles_held(context, session.agent_instance_id),
        "identity_trust": _identity_trust(session),
        "streamable_no_auth": _is_no_auth_sentinel(session),
    }


def _tool_download(
    arguments: dict[str, Any],
    *,
    session: StreamableSession,  # noqa: ARG001 — uniform handler signature
    context: DispatchContext,
) -> dict[str, Any]:
    blob_id = arguments.get("blob_id")
    output_path = arguments.get("output_path")
    if not isinstance(blob_id, str) or not blob_id:
        raise JsonRpcError(_INVALID_PARAMS, "download.blob_id must be a non-empty string")
    if not isinstance(output_path, str) or not output_path:
        raise JsonRpcError(
            _INVALID_PARAMS,
            "download.output_path must be a non-empty string",
        )
    blob = context.platform_surface.download(blob_id=blob_id)
    target = Path(output_path)
    target.write_bytes(blob.content)
    return {
        "status": "downloaded",
        "filename": blob.filename,
        "size": len(blob.content),
        "path": output_path,
    }


def _tool_process_search(
    arguments: dict[str, Any],
    *,
    session: StreamableSession,
    context: DispatchContext,
) -> dict[str, Any]:
    query = arguments.get("query")
    max_results = arguments.get("max_results", 10)
    if not isinstance(query, str) or not query.strip():
        raise JsonRpcError(
            _INVALID_PARAMS, "process_search.query must be a non-empty string",
        )
    if not isinstance(max_results, int) or max_results <= 0:
        raise JsonRpcError(
            _INVALID_PARAMS,
            "process_search.max_results must be a positive integer",
        )
    return context.platform_surface.process_search(
        query=query, max_results=max_results, bridge_id=session.bridge_id,
    )


def _tool_process_schema(
    arguments: dict[str, Any],
    *,
    session: StreamableSession,
    context: DispatchContext,
) -> dict[str, Any]:
    process_key = arguments.get("process_key")
    if not isinstance(process_key, str) or not process_key:
        raise JsonRpcError(
            _INVALID_PARAMS, "process_schema.process_key must be a non-empty string",
        )
    return context.platform_surface.process_schema(
        process_key=process_key, bridge_id=session.bridge_id,
    )


def _tool_process_call(
    arguments: dict[str, Any],
    *,
    session: StreamableSession,
    context: DispatchContext,
) -> dict[str, Any]:
    process_key = arguments.get("process_key")
    call_args = arguments.get("arguments") or {}
    reason = arguments.get("reason")
    if not isinstance(process_key, str) or not process_key:
        raise JsonRpcError(
            _INVALID_PARAMS, "process_call.process_key must be a non-empty string",
        )
    if not isinstance(call_args, dict):
        raise JsonRpcError(
            _INVALID_PARAMS, "process_call.arguments must be an object",
        )
    trigger_data: dict[str, Any] = {
        "bridge_id": session.bridge_id,
        "session_id": session.session_id,
    }
    if isinstance(reason, str) and reason:
        trigger_data["reason"] = reason
    return context.platform_surface.process_call(
        process_key=process_key,
        arguments=call_args,
        trigger_data=trigger_data,
        deliver_to_bridge=False,
    )


def _tool_process_result(
    arguments: dict[str, Any],
    *,
    session: StreamableSession,  # noqa: ARG001
    context: DispatchContext,
) -> dict[str, Any]:
    action_id = arguments.get("action_id")
    if not isinstance(action_id, str) or not action_id:
        raise JsonRpcError(
            _INVALID_PARAMS, "process_result.action_id must be a non-empty string",
        )
    return context.platform_surface.process_result(action_id=action_id)


def _tool_peer_list(
    arguments: dict[str, Any],  # noqa: ARG001 — uniform handler signature
    *,
    session: StreamableSession,  # noqa: ARG001
    context: DispatchContext,
) -> dict[str, Any]:
    snapshot = context.peer_registry.list_agent_ids()
    instances: dict[str, list[dict[str, object]]] = {
        agent_id: [
            {
                "agent_instance_id": b.agent_instance_id,
                "session_label": b.session_label,
                "parent_pid": b.parent_pid,
                "registered_at": b.created_at,
                "created_at": b.created_at,
                "updated_at": b.updated_at,
            }
            for b in bindings
        ]
        for agent_id, bindings in snapshot.items()
    }
    return {
        "agent_ids": sorted(snapshot.keys()),
        "instances": instances,
    }


def _tool_peer_register(
    arguments: dict[str, Any],
    *,
    session: StreamableSession,
    context: DispatchContext,
) -> dict[str, Any]:
    """Relabel the session's existing peer binding.

    The session's ``agent_instance_id`` is fixed by the bearer-token
    claim, so this call cannot mint a new instance — it can only
    rename ``session_label`` or change ``agent_id`` (rare, but kept
    for parity with the stdio tool).  The cross-bucket sweep inside
    :meth:`PeerRegistry.register` handles the rebinding cleanly.
    """
    new_agent_id = arguments.get("agent_id")
    if not isinstance(new_agent_id, str) or not new_agent_id:
        raise JsonRpcError(
            _INVALID_PARAMS, "peer_register.agent_id must be a non-empty string",
        )
    new_label = arguments.get("session_label")
    label = new_label if isinstance(new_label, str) else session.session_label
    from ..models import BridgeBinding  # noqa: PLC0415 — break import cycle
    binding = BridgeBinding(
        bridge_id=session.bridge_id,
        agent_id=new_agent_id,
        agent_instance_id=session.agent_instance_id,
        session_label=label,
        parent_pid=None,
    )
    context.peer_registry.register(binding)
    session.agent_id = new_agent_id
    session.session_label = label
    session.binding = binding
    return {
        "agent_id": new_agent_id,
        "agent_instance_id": session.agent_instance_id,
        "session_label": label,
        "parent_pid": None,
        "bridge_id": session.bridge_id,
        "status": "registered",
    }


def _tool_peer_send(
    arguments: dict[str, Any],
    *,
    session: StreamableSession,
    context: DispatchContext,
) -> dict[str, Any]:
    """Dispatch a peer_send from the streamable session.

    Validation lives here (JSON-RPC argument shape); the
    IMPORTANT-marker semantics and wake-vs-channel routing live in
    :func:`agent_messaging_plugin.peer_dispatch.dispatch_peer_send`,
    shared with the legacy bridge HTTP route.  The dispatcher's
    typed exceptions (``PeerAmbiguousError`` / ``PeerUnreachableError``
    / ``BridgeNotFoundError`` / ``BridgeQueueFullError`` /
    ``NativeWakeError``) propagate up to ``_handle_tools_call`` and
    map to MCP-shaped JSON-RPC errors there.
    """
    peer_id = arguments.get("peer_id")
    if not isinstance(peer_id, str) or not peer_id:
        raise JsonRpcError(_INVALID_PARAMS, "peer_send.peer_id must be a non-empty string")
    peer_agent_instance_id = arguments.get("peer_agent_instance_id")
    if peer_agent_instance_id is not None and not isinstance(
        peer_agent_instance_id, str,
    ):
        raise JsonRpcError(
            _INVALID_PARAMS,
            "peer_send.peer_agent_instance_id must be a string when present",
        )
    content_raw = arguments.get("content") or []
    if not isinstance(content_raw, list) or not content_raw:
        raise JsonRpcError(
            _INVALID_PARAMS, "peer_send.content must be a non-empty list",
        )
    content = _parse_text_parts(content_raw)
    outcome = dispatch_peer_send(
        bridge_manager=context.bridge_manager,
        peer_registry=context.peer_registry,
        agent_messaging_service=context.agent_messaging_service,
        sender_bridge_id=session.bridge_id,
        sender_agent_id=session.agent_id,
        sender_agent_instance_id=session.agent_instance_id,
        sender_session_label=session.session_label,
        sender_parent_pid=None,
        peer_id=peer_id,
        peer_agent_instance_id=peer_agent_instance_id,
        content=content,
    )
    return outcome.to_payload()


def _tool_peer_send_by_name(
    arguments: dict[str, Any],
    *,
    session: StreamableSession,
    context: DispatchContext,
) -> dict[str, Any]:
    name = arguments.get("name")
    content_text = arguments.get("content")
    if not isinstance(name, str) or not name.strip():
        raise JsonRpcError(
            _INVALID_PARAMS, "peer_send_by_name.name must be a non-empty string",
        )
    if not isinstance(content_text, str) or not content_text:
        raise JsonRpcError(
            _INVALID_PARAMS, "peer_send_by_name.content must be a non-empty string",
        )
    role_name = name.strip()
    state_service = context.state_service
    if state_service is None:
        raise JsonRpcError(
            _TOOL_CALL_FAILED,
            "state_service is not bound; cannot resolve role bindings.",
            data={"code": "bridge.state_service_unavailable"},
        )
    try:
        role = resolve_role_binding(state_service, role_name)
    except RoleBindingVacantError as exc:
        raise JsonRpcError(
            _PEER_UNREACHABLE,
            str(exc),
            data={"code": "peer_role_vacant", "name": role_name},
        ) from exc
    except StateOperationError as exc:
        raise JsonRpcError(
            _TOOL_CALL_FAILED,
            str(exc),
            data={"code": "bridge.state_service_unavailable"},
        ) from exc
    if not role.agent_instance_id:
        raise JsonRpcError(
            _TOOL_CALL_FAILED,
            (
                f"role binding for {role_name!r} is missing "
                "agent_instance_id; re-claim the role first"
            ),
            data={"code": "peer_role_malformed", "name": role_name},
        )

    content = [TextPart(type="text", text=content_text)]
    duplicate_payload = _recent_important_role_duplicate(
        session=session,
        role_name=role_name,
        content_text=content_text,
        role_payload=role,
    )
    if duplicate_payload is not None:
        return duplicate_payload

    outcome = dispatch_role_send(
        bridge_manager=context.bridge_manager,
        peer_registry=context.peer_registry,
        agent_messaging_service=context.agent_messaging_service,
        role_name=role_name,
        role=role,
        sender_bridge_id=session.bridge_id,
        sender_agent_id=session.agent_id,
        sender_agent_instance_id=session.agent_instance_id,
        sender_session_label=session.session_label,
        sender_parent_pid=None,
        content=content,
        message_id=f"arm-{secrets.token_hex(16)}",
        reply_to_role="",
    )
    return outcome.to_payload()


def _tool_peer_inbox(
    arguments: dict[str, Any],
    *,
    session: StreamableSession,
    context: DispatchContext,
) -> dict[str, Any]:
    after_raw = arguments.get("after")
    after_dt: datetime | None = None
    if isinstance(after_raw, str) and after_raw:
        try:
            after_dt = datetime.fromisoformat(after_raw.replace("Z", "+00:00"))
        except ValueError as exc:
            raise JsonRpcError(
                _INVALID_PARAMS,
                f"peer_inbox.after is not a valid ISO-8601 datetime: {exc}",
            ) from exc
    limit_raw = arguments.get("limit", 50)
    if not isinstance(limit_raw, int) or limit_raw <= 0:
        raise JsonRpcError(
            _INVALID_PARAMS, "peer_inbox.limit must be a positive integer",
        )
    include_important = bool(arguments.get("include_important", True))
    role_after_raw = arguments.get("role_after")
    if role_after_raw is not None and not isinstance(role_after_raw, str):
        raise JsonRpcError(
            _INVALID_PARAMS, "peer_inbox.role_after must be a string",
        )
    page = context.agent_messaging_service.peer_inbox(
        PeerInboxRequest(
            recipient_agent_id=session.agent_id,
            recipient_agent_instance_id=session.agent_instance_id,
            after_created_at=after_dt,
            limit=max(1, min(limit_raw, 100)),
            include_important=include_important,
            # Opaque role-section cursor; the service validates + fails closed
            # on a malformed token (→ AgentMessagingError → JsonRpcError).
            role_after=role_after_raw,
        ),
    )
    context.peer_registry.touch_binding(session.agent_instance_id)
    return _serialize_peer_inbox_page(page, session.agent_instance_id)


def _tool_agent_thread_open(
    arguments: dict[str, Any],
    *,
    session: StreamableSession,
    context: DispatchContext,
) -> dict[str, Any]:
    backend = arguments.get("backend")
    if not isinstance(backend, str) or not backend:
        raise JsonRpcError(
            _INVALID_PARAMS, "agent_thread_open.backend must be a non-empty string",
        )
    request = OpenAgentThreadRequest(
        bridge_id=session.bridge_id,
        session_id=session.session_id,
        backend=backend,
        working_directory=_optional_str(arguments.get("working_directory")),
        title=_optional_str(arguments.get("title")),
        context=_parse_thread_context(arguments.get("context")),
        initial_message=_parse_initial_message(arguments.get("initial_message")),
    )
    opened = context.agent_messaging_service.open_thread(request)
    return {
        "thread_id": opened.thread_id,
        "message_id": opened.message_id,
        "action_id": opened.action_id,
        "flow_id": opened.flow_id,
        "status": opened.status.value,
    }


def _tool_agent_send(
    arguments: dict[str, Any],
    *,
    session: StreamableSession,
    context: DispatchContext,
) -> dict[str, Any]:
    thread_id = arguments.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise JsonRpcError(
            _INVALID_PARAMS, "agent_send.thread_id must be a non-empty string",
        )
    content_raw = arguments.get("content") or []
    if not isinstance(content_raw, list) or not content_raw:
        raise JsonRpcError(
            _INVALID_PARAMS, "agent_send.content must be a non-empty list",
        )
    request = SendAgentMessageRequest(
        bridge_id=session.bridge_id,
        thread_id=thread_id,
        content=_parse_text_parts(content_raw),
        response_mode=str(arguments.get("response_mode") or "async"),
        timeout_seconds=(
            arguments.get("timeout_seconds")
            if isinstance(arguments.get("timeout_seconds"), int)
            else None
        ),
    )
    queued = context.agent_messaging_service.send_message(request)
    return {
        "thread_id": queued.thread_id,
        "message_id": queued.message_id,
        "action_id": queued.action_id,
        "flow_id": queued.flow_id,
        "status": queued.status.value,
    }


def _tool_agent_messages(
    arguments: dict[str, Any],
    *,
    session: StreamableSession,
    context: DispatchContext,
) -> dict[str, Any]:
    thread_id = arguments.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise JsonRpcError(
            _INVALID_PARAMS, "agent_messages.thread_id must be a non-empty string",
        )
    after_cursor_raw = arguments.get("after_cursor", 0)
    if not isinstance(after_cursor_raw, int) or after_cursor_raw < 0:
        raise JsonRpcError(
            _INVALID_PARAMS,
            "agent_messages.after_cursor must be a non-negative integer",
        )
    limit_raw = arguments.get("limit", 50)
    if not isinstance(limit_raw, int) or limit_raw <= 0:
        raise JsonRpcError(
            _INVALID_PARAMS, "agent_messages.limit must be a positive integer",
        )
    page = context.agent_messaging_service.list_messages(
        ListAgentMessagesRequest(
            bridge_id=session.bridge_id,
            thread_id=thread_id,
            after_cursor=after_cursor_raw,
            limit=limit_raw,
        ),
    )
    return _serialize_messages_page(page)


def _tool_agent_status(
    arguments: dict[str, Any],
    *,
    session: StreamableSession,
    context: DispatchContext,
) -> dict[str, Any]:
    thread_id = arguments.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise JsonRpcError(
            _INVALID_PARAMS, "agent_status.thread_id must be a non-empty string",
        )
    status = context.agent_messaging_service.get_status(
        thread_id=thread_id, bridge_id=session.bridge_id,
    )
    return _serialize_thread_status(status)


def _tool_agent_close(
    arguments: dict[str, Any],
    *,
    session: StreamableSession,
    context: DispatchContext,
) -> dict[str, Any]:
    thread_id = arguments.get("thread_id")
    if not isinstance(thread_id, str) or not thread_id:
        raise JsonRpcError(
            _INVALID_PARAMS, "agent_close.thread_id must be a non-empty string",
        )
    closed = context.agent_messaging_service.close_thread(
        thread_id=thread_id, bridge_id=session.bridge_id,
    )
    return {
        "thread_id": closed.thread_id,
        "status": closed.status.value,
    }


_TOOL_HANDLERS: Final[dict[str, Any]] = {
    "current_identity": _tool_current_identity,
    "download": _tool_download,
    "process_search": _tool_process_search,
    "process_schema": _tool_process_schema,
    "process_call": _tool_process_call,
    "process_result": _tool_process_result,
    "peer_list": _tool_peer_list,
    "peer_register": _tool_peer_register,
    "peer_send": _tool_peer_send,
    "peer_send_by_name": _tool_peer_send_by_name,
    "peer_inbox": _tool_peer_inbox,
    "agent_thread_open": _tool_agent_thread_open,
    "agent_send": _tool_agent_send,
    "agent_messages": _tool_agent_messages,
    "agent_status": _tool_agent_status,
    "agent_close": _tool_agent_close,
}


def _recent_important_role_duplicate(
    *,
    session: StreamableSession,
    role_name: str,
    content_text: str,
    role_payload: Any,
) -> dict[str, Any] | None:
    if IMPORTANT_MARKER_RE.match(content_text) is None:
        return None
    now = monotonic()
    cutoff = now - _IMPORTANT_ROLE_DEDUPE_WINDOW_SECONDS
    key = (session.agent_instance_id, role_name, content_text)
    with _IMPORTANT_ROLE_DEDUPE_LOCK:
        stale = [
            cached_key
            for cached_key, seen_at in _IMPORTANT_ROLE_DEDUPE.items()
            if seen_at < cutoff
        ]
        for cached_key in stale:
            _IMPORTANT_ROLE_DEDUPE.pop(cached_key, None)
        if key in _IMPORTANT_ROLE_DEDUPE:
            return {
                "thread_id": f"role:{role_name}",
                "message_id": "",
                "delivery": "deduplicated_recent_important",
                "delivered_to_bridge_id": "",
                "resolved_agent_id": role_payload.agent_id,
                "resolved_agent_instance_id": role_payload.agent_instance_id,
                "resolved_session_label": role_payload.session_label,
                "deduplicated": True,
            }
        _IMPORTANT_ROLE_DEDUPE[key] = now
    return None


# ---------------------------------------------------------------------
# Helpers — parsing / serialisation shared with the HTTP route module's
# serialisation conventions.
# ---------------------------------------------------------------------


def _read_roles_held(
    context: DispatchContext, agent_instance_id: str,
) -> list[str]:
    state_service = context.state_service
    if state_service is None:
        raise JsonRpcError(
            _TOOL_CALL_FAILED,
            "state_service is not bound; cannot read agent_role_binding.",
            data={"code": "bridge.state_service_unavailable"},
        )
    try:
        return list_roles_for_agent_instance(state_service, agent_instance_id)
    except StateOperationError as exc:
        raise JsonRpcError(
            _TOOL_CALL_FAILED,
            str(exc),
            data={"code": "bridge.state_service_unavailable"},
        ) from exc


def _identity_trust(session: StreamableSession) -> str:
    if _is_no_auth_sentinel(session):
        return "outer_boundary_only"
    return "bearer_verified"


def _is_no_auth_sentinel(session: StreamableSession) -> bool:
    return (
        session.agent_id == TUNNEL_PASSTHROUGH_SENTINEL
        and session.agent_instance_id == TUNNEL_PASSTHROUGH_SENTINEL
        and session.session_label == "streamable_no_auth"
    )


def _parse_text_parts(parts: list[Any]) -> list[TextPart]:
    out: list[TextPart] = []
    for raw in parts:
        if not isinstance(raw, dict):
            raise JsonRpcError(
                _INVALID_PARAMS, "content parts must be objects",
            )
        kind = raw.get("type") or "text"
        if kind != "text":
            raise JsonRpcError(
                _INVALID_PARAMS,
                f"content part type {kind!r} is not supported",
            )
        text = raw.get("text")
        if not isinstance(text, str):
            raise JsonRpcError(
                _INVALID_PARAMS, "content part text must be a string",
            )
        out.append(TextPart(type="text", text=text))
    if not out:
        raise JsonRpcError(_INVALID_PARAMS, "content must be a non-empty list")
    return out


def _parse_thread_context(value: Any) -> Any | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise JsonRpcError(_INVALID_PARAMS, "context must be an object")
    from ananta.llm.agent_messaging.models import (  # noqa: PLC0415
        AgentThreadContext,
    )
    summary = value.get("summary")
    summary_str = summary if isinstance(summary, str) else None
    tags_raw = value.get("tags") or ()
    if not isinstance(tags_raw, list | tuple):
        raise JsonRpcError(_INVALID_PARAMS, "context.tags must be a list")
    tags = tuple(str(t) for t in tags_raw)
    return AgentThreadContext(summary=summary_str, tags=tags)


def _parse_initial_message(value: Any) -> Any | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise JsonRpcError(
            _INVALID_PARAMS, "initial_message must be an object",
        )
    from ananta.llm.agent_messaging.models import InitialMessage  # noqa: PLC0415
    content_raw = value.get("content")
    if not isinstance(content_raw, list):
        raise JsonRpcError(
            _INVALID_PARAMS, "initial_message.content must be a list",
        )
    content = _parse_text_parts(content_raw)
    response_mode = str(value.get("response_mode") or "async")
    timeout_raw = value.get("timeout_seconds")
    timeout_seconds = timeout_raw if isinstance(timeout_raw, int) else None
    return InitialMessage(
        content=content,
        response_mode=response_mode,
        timeout_seconds=timeout_seconds,
    )


def _serialize_messages_page(page: Any) -> dict[str, Any]:
    return {
        "thread_id": page.thread_id,
        "messages": [_serialize_message(m) for m in page.messages],
        "next_cursor": page.next_cursor,
        "status": page.status.value,
    }


def _serialize_message(message: Any) -> dict[str, Any]:
    import dataclasses  # noqa: PLC0415 — local heavy import
    return {
        "id": message.id,
        "cursor": message.cursor,
        "role": message.role.value,
        "kind": message.kind.value,
        "content": [{"type": p.type, "text": p.text} for p in message.content],
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


def _serialize_peer_inbox_page(
    page: Any, recipient_agent_instance_id: str,
) -> dict[str, Any]:
    # The role section (role_entries + next_role_cursor) is emitted ADDITIVELY;
    # the instance section keys are byte-for-byte unchanged. role_section_status
    # / role_section_error carry the v10 Q1 fault-domain outcome so a caller can
    # tell an empty role section (no role messages) from a failed one.
    return {
        "recipient_agent_id": page.recipient_agent_id,
        "recipient_agent_instance_id": recipient_agent_instance_id,
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


def _optional_str(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


__all__ = [
    "INTERNAL_ERROR",
    "PARSE_ERROR",
    "DispatchContext",
    "JsonRpcError",
    "JsonRpcRequest",
    "JsonRpcResponse",
    "dispatch_request",
    "parse_envelope",
]
