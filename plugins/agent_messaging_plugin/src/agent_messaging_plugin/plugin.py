"""``agent_messaging_plugin`` — consolidated bridge plugin.

This plugin wears three hats (see plugin.yaml for the headline summary):

1. **AgentMessagingServiceInterface** — durable ``core__agent_thread`` /
   ``core__agent_message`` schema host plus the ``run_turn`` EDGE
   process that drives a single agent conversation turn.
2. **IOInterfacePlugin** — ``start_interface`` / ``stop_interface`` /
   ``post_message`` / ``get_supported_capabilities``.  The homunculus
   delivers prose to Claude Code (or any MCP-connected peer) through this surface.
3. **Bridge service** — FastAPI HTTP API on a dynamically allocated
   port, peer registry with multi-instance routing, bridge sessions
   with long-poll event queues, native-wake adapter for
   ``agent_id="claude_code"``, and the bridge-delivery EDGE_SINK pair
   (``deliver_result`` / ``deliver_error``).

The plugin intentionally does NOT register through the service-binding
system (``ServiceName`` enum + ``service_bindings.json``).  Bound
ServiceProviders are skipped from the ``plugin::<name>::*`` registry
namespace by ``process_registry/builder.py::_should_skip_plugin``,
which would hide ``run_turn`` from ``submit_action_definition``.
``AgentMessagingServiceInterface`` is satisfied by structural
delegation; callers resolve us via
``plugin_manager.plugins["agent_messaging_plugin"]`` and call our
public methods directly.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import os
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final, Protocol

from ananta.core.actions.action_metadata import (
    ContextHandling,
    MergeErrorProcessorCustomizations,
    MergeResultProcessorCustomizations,
    ParameterMetadata,
    ParameterType,
    ReturnValueSchema,
    platform_process,
)
from ananta.core.domain.enums import ProcessorPolicyCategory
from ananta.core.domain.types import ActionResult, ErrorDetail
from ananta.core.plugins.plugin_base import ServicePlugin
from ananta.core.plugins.profile_manifest import load_manifest_plugin_set
from ananta.core.runtime import find_available_port, write_routerless_bridge_port_file
from ananta.error_handling import FrameworkError
from ananta.interfaces import IOInterfacePlugin
from ananta.interfaces.agent_messaging_service_interface import (
    AgentMessagingServiceInterface,
)
from ananta.interfaces.chat_interface_support import build_initial_vertex_action
from ananta.interfaces.edge_process_provider import (
    EdgeProcessDefinition,
    EdgeProcessProvider,
)
from ananta.interfaces.io_capabilities import IOCapability
from ananta.llm.agent_messaging.models import PeerInbox, TextPart
from ananta.llm.agent_messaging.repository import AgentMessagingRepository
from ananta.llm.agent_messaging.role_binding import (
    HOLDER_KIND_INFERENCE_PROVIDER,
    HOLDER_KIND_SESSION,
    SYS_AUTONOMIC_SLOT,
    is_system_role,
)
from ananta.llm.agent_messaging.routing import make_router
from ananta.llm.agent_messaging.schema import (
    get_agent_direct_wake_schema,
    get_agent_messaging_schema,
    get_agent_role_message_schema,
)
from ananta.llm.agent_messaging.service import (
    AgentMessagingConfig,
    AgentMessagingService,
    _BridgeDeliveryEndpoint,
)
from ananta.services.inference_service.completion_request_queue import (
    SERVE_SERVED,
    serve_completion_request,
)
from ananta.services.inference_service.completion_request_schema import (
    COL_CORRELATION as COL_ICR_CORRELATION,
)
from ananta.services.inference_service.completion_request_schema import (
    COL_MESSAGES as COL_ICR_MESSAGES,
)
from ananta.services.inference_service.completion_request_schema import (
    COL_PURPOSE as COL_ICR_PURPOSE,
)
from ananta.services.inference_service.completion_request_schema import (
    COL_REQUEST_ID as COL_ICR_REQUEST_ID,
)
from ananta.services.inference_service.completion_request_schema import (
    COL_RESUME_PROCESS_KEY as COL_ICR_RESUME_PROCESS_KEY,
)

from .autonomic_assignment import AutonomicAssignment
from .bridge_lifecycle import (
    BridgeLifecycleSweeper,
    purge_preboot_bindings,
    run_full_bridge_cleanup,
)
from .bridge_sessions import (
    BridgeNotFoundError,
    BridgeQueueFullError,
    BridgeSessionManager,
)
from .constants import PLUGIN_NAME
from .direct_wake_reconcile import DirectWakeReconciler
from .http_routes import register_routes
from .mcp_streamable import (
    BearerVerifier,
    StreamableSessionManager,
    build_streamable_router,
)
from .mcp_streamable.auth import HMAC_KEY_BYTE_LENGTH, PermissiveBearerVerifier
from .mcp_streamable.oauth import (
    DEFAULT_TOKEN_TTL_SECONDS,
    OAuthEndpoints,
    build_dynamic_oauth_router,
    build_endpoints,
    build_oauth_router,
)
from .mcp_streamable.router import STREAMABLE_ALIAS_PATH, STREAMABLE_PATH
from .message_important_backfill import backfill_message_important
from .peer_dispatch import (
    NativeWakeError,
    build_wake_reply_hint,
    dispatch_peer_send,
    dispatch_role_send,
)
from .peer_registry import (
    PeerAmbiguousError,
    PeerRegistry,
    PeerSessionAmbiguousError,
    PeerUnreachableError,
)
from .platform_surface import PlatformSurface
from .process_exposure import ProcessExportPolicy
from .role_binding_store import (
    HolderClaim,
    RoleBindingVacantError,
    claim_role_binding_v4,
    holds_role,
    release_role_binding_v4,
    resolve_role_binding,
    resolve_role_binding_v4,
    run_cutover_migration_at_readiness,
    session_claim_requires_session_id,
    upsert_role_entity,
)
from .role_message_consumed_backfill import backfill_role_message_consumed
from .route_activity import make_model_activity_middleware
from .schema import (
    get_agent_role_binding_schema_definition,
    get_peer_binding_schema_definition,
    get_role_model_schema_definition,
)
from .session_inference_provider import SessionInferenceProvider
from .system_slots import (
    SystemSlotClaimDecision,
    evaluate_system_slot_claim,
    validate_system_slot_declarations,
)

if TYPE_CHECKING:  # pragma: no cover — type-only references
    from collections.abc import Mapping

    from ananta.core.orchestration.interfaces import ISessionManager
    from ananta.core.orchestration.managers.flow_manager import FlowManager
    from ananta.llm.agent_messaging.models import (
        AgentMessageQueued,
        AgentMessagesPage,
        AgentThreadClosed,
        AgentThreadMessagesPage,
        AgentThreadOpened,
        AgentThreadsPage,
        AgentThreadStatus,
        ListAgentMessagesRequest,
        ListAgentThreadsRequest,
        OpenAgentThreadRequest,
        PeerInboxRequest,
        PeerSendRequest,
        PeerSendResult,
        ReadThreadMessagesRequest,
        SendAgentMessageRequest,
    )
    from ananta.types.schema_types import SchemaDefinition
    from fastapi import FastAPI

    from .models import BridgeSessionState

logger = logging.getLogger(__name__)
SYSTEM_AGENT_ID: Final[str] = "system"
SYSTEM_SCHEDULER_ID: Final[str] = "system:scheduler"
SYSTEM_ROLE_HANDOVER_ID: Final[str] = "system:role-handover"
SYSTEM_SCHEDULER_LABEL: Final[str] = "System (Scheduler)"
SYSTEM_ROLE_HANDOVER_LABEL: Final[str] = "System (role handover)"


class _UploadRouteAuth(Protocol):
    """Keyword-callable matching the chatgpt + claude_ai source plugins'
    ``AuthCheckProtocol``. Their AuthCheckProtocols define a single
    method ``__call__(self, authorization_header: str | None) -> object``;
    Pyright treats ``Callable[[str | None], object]`` as positional-only
    and rejects the assignment. Declaring the structural shape here keeps
    the local closure compatible with both source plugins without
    importing either at module load (they are profile-conditional).
    """

    def __call__(self, authorization_header: str | None) -> object: ...


# Bridge-delivery endpoint — the consolidated plugin owns the EDGE_SINK
# pair itself; bridge-bound flows route deliver_result / deliver_error
# back to this same plugin namespace.
_DELIVER_RESULT_PROCESS_KEY = f"plugin::{PLUGIN_NAME}::deliver_result"
_DELIVER_ERROR_PROCESS_KEY = f"plugin::{PLUGIN_NAME}::deliver_error"

# Bridge namespace error tokens for deliver_result / deliver_error.
_ERR_NO_ACTIVE_BRIDGE = "bridge.no_active_bridge"
_ERR_QUEUE_FULL = "bridge.queue_full"
_ERR_PROCESS_CALL_FAILED = "bridge.process_call_failed"

# Standard IO interface error tokens for post_message.
_ERR_SESSION_NOT_BOUND = "session_not_bound"
_ERR_VALIDATION = "ValidationError"

# Default per-message size guard used until config provider has run.
_DEFAULT_MAX_MESSAGE_CHARS = 120_000

# ◆R2 (Phase 5): bound on the disconnected-inference-instance tombstone.
# The tombstone records agent_instance_ids that HELD a SessionInferenceProvider
# earlier in this process lifetime but whose bridge has since dropped, so the
# vertex resolver can DEFER (never silent-Qwen) a flow explicitly bound to a
# now-disconnected roleless session, distinguishing it from a never-bound flow.
# LRU-bounded so a long-running process with churny reconnects can't leak.
# N1 (Rev-C ruling 2026-07-02): eviction past this cap is a DOCUMENTED,
# principled tradeoff — for a ROLELESS instance (no durable ◆R2 identity),
# "never silent-Qwen a bound session" and "never-bound/streamable MUST go
# DEFAULT" are irreconcilable under bounded memory. Role-bound flows are
# IMMUNE (the ◆R2 durable path never DEFAULTs). Eviction is made LOUD (see
# _clear_inference_providers_for_bridge) so the rare roleless-aged-out case
# is VISIBLE. 2048 (small strings) widens the practical roleless window.
_INFERENCE_TOMBSTONE_CAP = 2048

# How long we wait for the uvicorn server thread to acknowledge startup
# before assuming the bind silently failed.
_SERVER_START_TIMEOUT_S = 10.0
_SERVER_JOIN_TIMEOUT_S = 5.0

# The plugin whose presence in the active manifest means "this homunculus
# has a blue-green router" (D11 ruling R1). Must match
# macos_self_deployment_plugin.constants.PLUGIN_NAME — duplicated as a
# plain string rather than cross-plugin-imported (no plugin in this
# codebase imports another plugin's package directly).
_ROUTER_PLUGIN_NAME = "macos_self_deployment_plugin"

# Vault entry holding the HMAC secret that signs Streamable HTTP MCP
# bearer tokens (HS256). Generated on first homunculus boot if absent;
# never rotated except by explicit operator action (vault entry
# replacement + homunculus restart invalidates all outstanding tokens, which
# is the expected one-time disruption window). See
# ``workbench/2026-05-24_hmac_bearer_tokens_design.md`` §3.
#
# Scoped per master plan §3.3.1: <homunculus>.<plugin>.<credential>.
# Built at module-import time from HOMUNCULUS_NAME; fast-fails if unset.
# Per W-ADDRESS-BOOK-RENAME §A.2.4 path b — write under the scoped name
# directly so W-VAULT-CALLER-ENFORCE Tier 2 doesn't need a compat-mode
# entry for this row. The lazy-create path in `_load_or_create_bearer_hmac_key`
# below now writes under the scoped name on first streamable-MCP boot.
def _bearer_hmac_key_vault_name() -> str:
    name = os.environ.get("HOMUNCULUS_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "agent_messaging_plugin: HOMUNCULUS_NAME env var is required to "
            "resolve the scoped bearer_token_hmac_key vault entry name.",
        )
    return f"{name}.agent_messaging_plugin.bearer_token_hmac_key"


_BEARER_HMAC_KEY_VAULT_NAME = _bearer_hmac_key_vault_name()


def _load_or_create_bearer_hmac_key(vault: Any) -> bytes:
    """Return the homunculus's HMAC bearer-signing secret as raw bytes.

    Reads from the vault under :data:`_BEARER_HMAC_KEY_VAULT_NAME`;
    on first boot the entry is absent so we mint a fresh
    ``secrets.token_bytes(HMAC_KEY_BYTE_LENGTH)`` and persist its
    base64 encoding before returning. The value is base64-encoded in
    storage because the vault's ``store`` interface accepts a string.
    """
    retrieved = vault.retrieve(_BEARER_HMAC_KEY_VAULT_NAME)
    if isinstance(retrieved, dict) and retrieved.get("status") == "success":
        stored_value = retrieved.get("data", {}).get("value")
        if isinstance(stored_value, str) and stored_value:
            return base64.b64decode(stored_value)
    fresh = secrets.token_bytes(HMAC_KEY_BYTE_LENGTH)
    vault.store(
        _BEARER_HMAC_KEY_VAULT_NAME,
        base64.b64encode(fresh).decode("ascii"),
        tags=["bearer_token", "hmac", "task53"],
        metadata={
            "description": (
                "HMAC secret for Streamable HTTP MCP bearer-token signing "
                "(HS256). Replacing this value invalidates every outstanding "
                "bearer token; connected clients must re-authenticate."
            ),
            "byte_length": str(HMAC_KEY_BYTE_LENGTH),
            "algorithm": "HS256",
        },
    )
    return fresh


@dataclass(frozen=True, slots=True)
class _BridgeRuntimeConfig:
    """Bridge / IO surface runtime config.

    Kept separate from :class:`AgentMessagingConfig` (which is frozen and
    scoped to the agent-messaging service) so the bridge surface can read
    its own settings without forcing the service config to grow new
    fields it doesn't use.
    """

    host: str = "127.0.0.1"
    # Preferred bridge HTTP port. ``None`` (default) -> ``find_available_port``
    # returns an OS-assigned port via ``bind(0)``. Setting this to a
    # fixed value (via the ANANTA_PLUGIN_AGENT_MESSAGING_PLUGIN_PORT env
    # var or plugin yaml) pins the port — used by dry-run homunculus
    # deployments that need a stable host port mapping (8001:8000) for
    # first-boot orchestration. The port is in-process only: Slice 3 of
    # the bridge-port-routing design eliminated the per-color port file
    # in favor of cross-plugin lookup of ``self.bridge_port`` and direct
    # ``register_color`` calls against the router.
    port: int | None = None
    long_poll_timeout_seconds: int = 25
    bridge_idle_timeout_seconds: int = 3_600
    max_pending_events: int = 200
    max_message_chars: int = _DEFAULT_MAX_MESSAGE_CHARS
    # INF-01 §D.9 Trigger-2: grace window between a sys:autonomic holder's
    # bridge close and the succession check, so a reconnecting holder (whose
    # register re-points its bindings via the S2 self-refresh) is never
    # displaced by a mere reconnect gap. Must comfortably absorb a
    # fleet-wide bridge reconnect (observed reconnects are seconds).
    autonomic_grace_seconds: int = 120
    # REL-09: cadence of the bridge-lifecycle idle sweeper (the driver for
    # BridgeSessionManager.sweep_idle + the full per-bridge cleanup). The
    # idle THRESHOLD stays bridge_idle_timeout_seconds; this knob only
    # bounds detection latency past it.
    bridge_sweep_interval_seconds: int = 300
    # INF-02: serve window for autonomic-routed completion requests. A
    # pending request whose forward stamp is older than this without a
    # served/failed transition is re-queued by the serve-timeout sweep
    # (riding the bridge-lifecycle sweeper cadence above). Sized for a
    # frontier session mid-task: generous, but never forever.
    completion_serve_window_seconds: int = 900
    # INF-06 reliability: serve window for a forwarded Surface-1 action-decode
    # vertex. A 'forwarded' deferred_vertex row whose forward stamp is older than
    # this without the holder self-executing is re-driven by the forwarded
    # serve-timeout sweep. Holder turns legitimately run minutes (§2f) — a window
    # shorter than normal holder-turn latency re-drives every forward spuriously,
    # so this is generous; the attempts cap bounds the tail.
    forward_serve_window_seconds: int = 900
    # INF-06: monotone re-drive attempts cap for a forwarded vertex. At the cap
    # the row flips to the terminal 'failed' state (durable stall record + loud
    # log) instead of re-driving forever (§2g).
    forward_attempts_cap: int = 5
    # INF-06: age (seconds) after which a terminal 'failed' forwarded-vertex row
    # is hard-deleted by the GC sweep, so the durable stall records never grow
    # unbounded (§8-bis retention rider). Generous — the record stays readable
    # for well over a day of diagnosis before it is reaped.
    terminal_gc_after_seconds: int = 172_800
    # REL-05 (Q1): direct/role IMPORTANT re-emit window + cap. Window = the
    # minimum gap between emissions of one owed message (most sessions turn
    # within it); cap = total emissions (original + re-emits) before escalation.
    re_emit_window_seconds: int = 300
    re_emit_cap: int = 3
    # Streamable HTTP MCP transport — opt-in.  Off by default so the
    # laptop dev mode is unchanged; container deployments flip it on
    # via plugin yaml override and bind to 0.0.0.0:9000 for phone
    # connectivity (host port 9001 fronted by Caddy / mkcert TLS).
    streamable_enabled: bool = False
    streamable_host: str = "0.0.0.0"  # noqa: S104 — container bind, gated by streamable_enabled
    streamable_port: int = 9000
    streamable_allowed_origins: tuple[str, ...] = ()
    streamable_bearer_max_age_seconds: int = 300
    # OAuth 2.1 client_credentials surface for the streamable transport.
    # Required for claude.ai's custom-connector validator: when blank,
    # /.well-known/oauth-authorization-server returns 404 and claude.ai
    # gives up before issuing /oauth/token.  When set, the issuer URL
    # is echoed into the well-known docs verbatim — pin it to the
    # public hostname the connector is configured against.
    oauth_enabled: bool = False
    oauth_issuer_url: str = ""
    oauth_resource_aliases: tuple[str, ...] = ()
    # OAuth clients that act as the operator's management console.
    # These get a narrow read/search + role-dispatch process allowlist,
    # not blanket operator-equivalent authority.
    oauth_management_client_ids: tuple[str, ...] = ()
    # Access-token TTL for browser/hosted MCP clients; refresh-token
    # rotation covers longer-term reuse after the initial account link.
    oauth_token_ttl_seconds: int = DEFAULT_TOKEN_TTL_SECONDS
    # Authorization-code grant: TTL on the in-process auth-code cache.
    # Single-use, short-lived; matches RFC 6749 §4.1.2's "SHOULD <= 10
    # minutes" recommendation by default.
    oauth_auth_code_ttl_seconds: int = 600
    # Refresh-token TTL.  30 days = canonical OAuth 2.1 value; once
    # this elapses the client falls back to a full authorize round.
    oauth_refresh_token_ttl_seconds: int = 30 * 24 * 60 * 60
    # When enabled, the streamable router validates the bearer's ``aud``
    # claim against the canonical MCP URI derived from the issuer.
    # Disabling is for laptop dev mode where no canonical URL is pinned.
    oauth_require_audience: bool = True
    # When enabled, /oauth/token's authorization_code response includes
    # a refresh_token + the refresh_token grant is honoured.  Disable
    # to fall back to client_credentials + auth_code only.
    oauth_refresh_tokens_enabled: bool = True
    # CORS Origin allow-list for the streamable + OAuth endpoints.
    # ``https://claude.ai`` is required for the browser-driven custom
    # connector validator's preflight; add any other browser-driven
    # client domains here.
    streamable_cors_origins: tuple[str, ...] = ()
    # Operator-opt-in bypass: when True, replaces the bearer-token
    # verifier with a permissive variant that returns a synthetic
    # claim for every request. Use ONLY when an outer security
    # boundary (OpenAI tunnel-client + runtime API key, mTLS, or
    # explicit network isolation) is the auth gate. Default off —
    # opt-in opens an otherwise-closed surface. See
    # ``workbench/2026-06-06_openai_tunnel_client_setup.md`` for the
    # tunnel-as-security-boundary pattern. Eventual cleanup target:
    # remove this flag once ``bridge_hmac_key`` lazy creation lands
    # via the Tier 5 vault path (state-service consolidation campaign).
    streamable_no_auth: bool = False

    def get(self, key: str, default: object = None) -> object:
        """Provide a ``.get`` shim so :mod:`http_routes` can look up keys."""
        value = getattr(self, key, default)
        return value if value is not None else default


class AgentMessagingPlugin(
    ServicePlugin,
    IOInterfacePlugin,
    EdgeProcessProvider,
    AgentMessagingServiceInterface,
):
    """Consolidated bridge plugin (IO + bridge + agent messaging).

    Implements ``AgentMessagingServiceInterface`` by delegating to an
    underlying :class:`AgentMessagingService`; implements
    :class:`IOInterfacePlugin` directly via ``start_interface`` /
    ``stop_interface`` / ``post_message`` / ``get_supported_capabilities``.
    Exposes ``run_turn`` plus the bridge-delivery and IO EDGE_SINK
    processes through ``@platform_process`` decorators.

    NOTE: This plugin intentionally does NOT declare
    ``service_interfaces`` (the property would mark it as a
    ServiceProvider).  Bound ServiceProviders are skipped from the
    ``plugin::<name>::*`` registry namespace
    (process_registry/builder.py::_should_skip_plugin), which would
    hide ``run_turn`` from ``submit_action_definition``.  Instead,
    callers resolve us via
    ``plugin_manager.plugins["agent_messaging_plugin"]`` and use our
    public methods directly.
    """

    name: str = PLUGIN_NAME

    def __init__(self) -> None:
        super().__init__()
        self.name = PLUGIN_NAME
        self._service: AgentMessagingService | None = None
        self._services_started: bool = False
        # AgentMessagingServiceInterface injection (set via setter
        # pattern in startup_sequence).
        self._flow_manager: FlowManager | None = None
        self._compilation_context_builder: Any | None = None
        # IOInterfacePlugin injection.
        self._memory_service: Any | None = None
        self._session_manager: ISessionManager | None = None
        self._context_management_service: Any | None = None
        # VaultServiceProxy injected via set_vault_service (W-VAULT-INTERFACE-EXTEND
        # Phase D-2, 2026-06-07). Proxy is caller-bound to this plugin's name.
        self._vault_service: object | None = None
        # Bridge / IO runtime state — populated by start_interface.
        self._bridge_manager: BridgeSessionManager | None = None
        self._peer_registry: PeerRegistry | None = None
        self._platform_surface: PlatformSurface | None = None
        # D-IF7/D-IF8 sidecar: per-bridge SessionInferenceProvider keyed
        # by agent_instance_id. Populated post-success in the stdio
        # peer_register route when ``provides_inference=True``; cleared
        # post-success in close_bridge via ``PeerRegistry.list_by_bridge``.
        # Streamable peer_register paths DO NOT populate this sidecar per
        # v4 D-IF11 (scope-out for v1 — fallback to default_inference_plugin
        # via the wrapper's None-handling path).
        self._inference_providers: dict[str, SessionInferenceProvider] = {}
        self._inference_providers_lock: threading.Lock = threading.Lock()
        # ◆R2 tombstone (case 3b): agent_instance_ids that were bound to a
        # provider earlier in this lifetime but whose bridge has dropped.
        # Guarded by ``_inference_providers_lock`` (same critical section as
        # the sidecar it shadows). LRU-bounded via _INFERENCE_TOMBSTONE_CAP.
        self._inference_provider_tombstones: OrderedDict[str, None] = OrderedDict()
        # INF-01 sub-slice-2: the sys:autonomic auto-assignment lifecycle
        # (Trigger-1/2 hook bodies + manual-set + first-claim drain).
        # Built in start_interface once the bridge collaborators exist.
        self._autonomic_assignment: AutonomicAssignment | None = None
        # REL-09: the idle-sweep driver (sweep_idle had NO caller before) —
        # routes every expired bridge through unregister's full cleanup.
        self._bridge_sweeper: BridgeLifecycleSweeper | None = None
        # REL-05: the server-side escalation reconciler; rides the sweeper's
        # on_tick. Built in start_interface once the collaborators exist.
        self._direct_wake_reconciler: DirectWakeReconciler | None = None
        self._port: int | None = None
        self._host: str | None = None
        self._app: FastAPI | None = None
        self._server_thread: threading.Thread | None = None
        self._server_loop: asyncio.AbstractEventLoop | None = None
        self._server_started_event: threading.Event = threading.Event()
        self._service_started_at: str | None = None
        self._max_message_chars: int = _DEFAULT_MAX_MESSAGE_CHARS
        # Streamable HTTP MCP transport — separate uvicorn server bound
        # to a configurable host:port so the container can expose
        # 0.0.0.0:9000 to the phone while the bridge HTTP stays on
        # 127.0.0.1:<dyn> for local CLI subprocesses.
        self._streamable_session_manager: StreamableSessionManager | None = None
        self._streamable_server_thread: threading.Thread | None = None
        self._streamable_server_loop: asyncio.AbstractEventLoop | None = None
        self._streamable_server_started_event: threading.Event = threading.Event()
        self._streamable_host: str | None = None
        self._streamable_port: int | None = None
        # M4/M9 upload-route auth (chatgpt_export + claude_ai_export) shares
        # the streamable transport's BearerVerifier. Lazy because
        # _build_fastapi_app runs BEFORE _mount_streamable_transport; the
        # upload-route auth closure captures self and resolves the verifier
        # at request time (by which point startup is complete).
        self._streamable_bearer_verifier: BearerVerifier | None = None
        self._active: bool = True

    @property
    def bridge_port(self) -> int | None:
        """Bridge HTTP server port, or ``None`` before ``start_interface``.

        Cross-plugin discovery surface for Slice 2 of
        ``workbench/2026-06-05_bridge_port_routing_and_session_lifecycle_design.md``:
        ``macos_self_deployment_plugin``'s heartbeat reads this attribute
        via ``orchestrator_ref.plugin_manager.plugins`` to learn the
        child's actual bound port, replacing the file-mediated detour
        through ``<name>-<color>.bridge.port`` that pre-dated the
        spawn-path-guarantee invariant I2. Returns ``None`` until
        ``start_interface`` allocates and binds; callers must tolerate
        that initial-window absence rather than fail-loud.
        """
        return self._port

    # ------------------------------------------------------------------
    # Platform-injected setters
    # ------------------------------------------------------------------

    def set_flow_manager(self, flow_manager: Any) -> None:
        logger.info("%s set_flow_manager called", self.name)
        self._flow_manager = flow_manager

    def set_compilation_context_builder(self, compilation_context_builder: Any) -> None:
        logger.info("%s set_compilation_context_builder called", self.name)
        self._compilation_context_builder = compilation_context_builder

    def set_action_factory(self, action_factory: Any) -> None:
        logger.info("%s set_action_factory called", self.name)
        self.action_factory = action_factory

    def set_memory_service(self, memory_service: Any) -> None:
        logger.info("%s set_memory_service called", self.name)
        self._memory_service = memory_service

    def set_vault_service(self, vault_service: object) -> None:
        """Receive caller-bound VaultServiceProxy from lifecycle injection.

        W-VAULT-INTERFACE-EXTEND Phase D-2 (P0 Tier 1, 2026-06-07): the
        proxy was constructed by ``_inject_vault_service`` in
        ``startup_sequence.py`` with this plugin's name baked into its
        bound ``CallContext``. Do NOT acquire vault via
        ``orchestrator.get_service`` — the proxy is the only allowed
        handle.
        """
        logger.info("%s set_vault_service called", self.name)
        self._vault_service = vault_service

    # ------------------------------------------------------------------
    # VaultKeysProvider — W-PLUGIN-LAUNCH-KEYS (P0 Tier 2 sub-1, 2026-06-07)
    # ------------------------------------------------------------------

    def get_required_vault_keys(self) -> list[str]:
        """Scoped vault keys whose existence is required at readiness.

        The bearer_token_hmac_key is LAZY-CREATED on first streamable-
        MCP boot by ``_load_or_create_bearer_hmac_key``; not required
        at readiness — the plugin must load with no row present so the
        first boot can create it. Returns empty list per W-CLASSIFY
        §A.2.4 path (b) plus brief §3.5.
        """
        return []

    def get_declared_vault_keys(self) -> list[str]:
        """All scoped vault keys this plugin reads or writes.

        Per W-ADDRESS-BOOK-RENAME §A.2.4 the bearer_token_hmac_key now
        writes/reads under the scoped form built in
        ``_bearer_hmac_key_vault_name()``.
        """
        return [_BEARER_HMAC_KEY_VAULT_NAME]

    # ------------------------------------------------------------------
    # ServicePlugin lifecycle (no background workers; lazy build)
    # ------------------------------------------------------------------

    async def start_services(self) -> ActionResult:
        self._services_started = True
        self._service_started_at = _now_iso()
        return ActionResult(
            action_status="completed",
            data={
                "message": f"{self.name} services started",
                "started_at": self._service_started_at,
            },
            actions=[],
            error=None,
            timestamp=_now_iso(),
        )

    async def stop_services(self) -> ActionResult:
        self._services_started = False
        self._service = None
        self._service_started_at = None
        return ActionResult(
            action_status="completed",
            data={"message": f"{self.name} services stopped"},
            actions=[],
            error=None,
            timestamp=_now_iso(),
        )

    def set_active(self, active: bool) -> None:
        """L3 blue-green Slice D color-active gate (peer dispatch + inbox poll).

        The plugin runs FastAPI servers + per-request handlers, not a continuous
        tick loop. The active color is the one the router routes to; the
        inactive color should not normally receive peer_send / peer_inbox calls.
        This setter flips ``self._active`` so request handlers can refuse if
        they want to; ``peer_inbox`` short-circuits to an empty result and
        ``peer_send`` raises so a misrouted request fails fast and loud.
        """
        self._active = active

    def initialize(self, config: dict[str, object]) -> None:
        """Bind the config provider so plugin.yaml defaults take effect.

        Called by the platform during plugin discovery with the
        per-plugin config dict.  Without this override
        ``self.config_provider`` stays None and ``_build_config``
        falls back to hardcoded defaults instead of the yaml/JSON
        config.
        """
        from ananta.core.config.config_provider import ConfigProvider  # noqa: PLC0415
        self.config_provider = ConfigProvider(self.name, config)

    def prepare_for_readiness(self) -> None:
        """Validate dependencies available at readiness time.

        Flow / action-factory / compilation-context-builder arrive
        after readiness, so we only check the dependencies that must
        be present right now.  Service constructs lazily on first use.
        """
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref not injected",
            )
        if orchestrator.get_service("state_service") is None:
            raise RuntimeError(
                f"{self.name}: state_service unavailable at readiness",
            )
        if getattr(orchestrator, "plugin_manager", None) is None:
            raise RuntimeError(
                f"{self.name}: plugin_manager unavailable at readiness",
            )
        # §6.1: fail loud at readiness on a malformed system-slot declaration — the
        # platform's slot-constant registry must be well-formed before any claim or
        # gate reads it (a declaration bug is a startup-blocking error, not a
        # silently-tolerated state). The binding-STATE boot invariant (session-filled
        # LOUD-WARN / plugin-filled fail-boot) rides the INF-01 autonomic readiness
        # lane (§D.9); this is the declaration-INTEGRITY check that precedes it.
        validate_system_slot_declarations()
        # §9 CUTOVER migration — a PRE-SERVE HARD GATE. This readiness hook BLOCKS
        # until it returns; a parity failure RAISES (CutoverParityError) so green
        # refuses to serve while blue keeps serving — never a half-migrated live
        # table. One-shot marker-gated; the migrate→parity→[re-run]→flip loop IS the
        # §9 explicit-claim quiesce-equivalent. state_service confirmed available above.
        run_cutover_migration_at_readiness(
            orchestrator.get_service("state_service"),
            self._best_effort_memory_service(),
        )
        logger.info(
            "%s ready (service constructs lazily on first invocation)",
            self.name,
        )

    def _best_effort_memory_service(self) -> object | None:
        """The bound ``memory_service`` for the migration's best-effort role-entity
        ingest (§7), or ``None`` — the ingest is optional and NEVER gates readiness."""
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            return None
        get_service = getattr(orchestrator, "get_service", None)
        if not callable(get_service):
            return None
        try:
            return get_service("memory_service")
        except Exception:  # noqa: BLE001 — best-effort ingest must never gate readiness
            return None

    # ------------------------------------------------------------------
    # IOInterfacePlugin contract
    # ------------------------------------------------------------------

    def get_supported_capabilities(self) -> set[IOCapability]:
        return {IOCapability.TEXT}

    def get_edge_process_definitions(self) -> dict[str, EdgeProcessDefinition]:
        """Declare ``run_turn`` — the only EDGE process this plugin owns.

        ``run_turn`` is dispatched only by ``AgentMessagingService``
        (never by the inference model) and its result is bridge-delivered
        via ``ResultProcessorKind.BRIDGE_DELIVERY``.

        EDGE_SINK processes (``deliver_result``, ``deliver_error``,
        ``post_message``, ``start_interface``, ``stop_interface``) are
        NOT declared here — the platform's process registry builder
        filters this dict by ``ProcessorPolicyCategory.EDGE`` and will
        reject EDGE_SINK entries with "no @platform_process method
        with that name exists".  EDGE_SINK methods register themselves
        through their ``@platform_process`` decorators alone (see
        ``claude_code_channel_plugin`` for the same pattern).
        """
        return {
            "run_turn": EdgeProcessDefinition(
                name="run_turn",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "send_peer_message": EdgeProcessDefinition(
                name="send_peer_message",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "peer_send_by_name": EdgeProcessDefinition(
                name="peer_send_by_name",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "peer_claim_role": EdgeProcessDefinition(
                name="peer_claim_role",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "peer_release_role": EdgeProcessDefinition(
                name="peer_release_role",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "peer_holds_role": EdgeProcessDefinition(
                name="peer_holds_role",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            # INF-01 sub-slice-2 manual-set lane. Sensitivities mirror the
            # verb's ACTUAL return (action/name/agent_instance_id — the same
            # claim-outcome shape as peer_claim_role).
            "set_autonomic_slot": EdgeProcessDefinition(
                name="set_autonomic_slot",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            # INF-02 serve verb (holder → platform completion callback).
            # Sensitivities mirror the verb's ACTUAL return (status /
            # request_id / resume_process_key); not retryable — the serve
            # CAS is the idempotency gate, a retry would just report
            # already_served.
            "submit_autonomic_completion": EdgeProcessDefinition(
                name="submit_autonomic_completion",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
        }

    # ------------------------------------------------------------------
    # AgentMessagingServiceInterface — delegation
    # ------------------------------------------------------------------

    def open_thread(self, request: OpenAgentThreadRequest) -> AgentThreadOpened:
        return self._require_service().open_thread(request)

    def send_message(
        self, request: SendAgentMessageRequest,
    ) -> AgentMessageQueued:
        return self._require_service().send_message(request)

    def list_messages(
        self, request: ListAgentMessagesRequest,
    ) -> AgentMessagesPage:
        return self._require_service().list_messages(request)

    def list_threads(
        self, request: ListAgentThreadsRequest,
    ) -> AgentThreadsPage:
        return self._require_service().list_threads(request)

    def read_thread_messages(
        self, request: ReadThreadMessagesRequest,
    ) -> AgentThreadMessagesPage:
        return self._require_service().read_thread_messages(request)

    def get_status(
        self, *, thread_id: str, bridge_id: str,
    ) -> AgentThreadStatus:
        return self._require_service().get_status(
            thread_id=thread_id, bridge_id=bridge_id,
        )

    def close_thread(
        self, *, thread_id: str, bridge_id: str,
    ) -> AgentThreadClosed:
        return self._require_service().close_thread(
            thread_id=thread_id, bridge_id=bridge_id,
        )

    def peer_send(self, request: PeerSendRequest) -> PeerSendResult:
        if not self._active:
            raise RuntimeError(
                f"{self.name}: peer_send refused — this color is inactive "
                "(see LifecycleManaged.set_active). The router should route "
                "peer dispatch to the active color; a request landing here "
                "indicates a routing race or misconfiguration.",
            )
        return self._require_service().peer_send(request)

    def peer_inbox(self, request: PeerInboxRequest) -> PeerInbox:
        if not self._active:
            return PeerInbox(
                recipient_agent_id=request.recipient_agent_id,
                entries=(),
                next_after_created_at=None,
            )
        return self._require_service().peer_inbox(request)

    def get_schema_definitions(self) -> list[SchemaDefinition]:
        return [
            get_agent_messaging_schema(),
            get_agent_role_message_schema(),
            get_agent_direct_wake_schema(),
            get_peer_binding_schema_definition(),
            get_agent_role_binding_schema_definition(),
            get_role_model_schema_definition(),
        ]

    # ------------------------------------------------------------------
    # Peer messaging — send_peer_message (scheduler-callable)
    # ------------------------------------------------------------------

    @platform_process(
        name="send_peer_message",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "peer_id": ParameterMetadata(
                description="Agent ID of the recipient peer (e.g. 'claude_code')",
                required=True,
                type=ParameterType.STRING,
            ),
            "peer_agent_instance_id": ParameterMetadata(
                description=(
                    "Specific instance ID of the recipient peer. "
                    "Required when multiple instances of peer_id are registered."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "content": ParameterMetadata(
                description="Message text to deliver to the peer.",
                required=True,
                type=ParameterType.STRING,
            ),
            "important": ParameterMetadata(
                description=(
                    "When true, prepends 'IMPORTANT: ' to content so the platform "
                    "triggers a native wake on the recipient session."
                ),
                required=False,
                type=ParameterType.BOOLEAN,
            ),
        },
        output_type="object",
        output_description="Delivery outcome with thread and message identifiers",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Peer message delivery result",
            properties={
                "thread_id": ParameterMetadata(type=ParameterType.STRING),
                "message_id": ParameterMetadata(type=ParameterType.STRING),
                "delivery": ParameterMetadata(type=ParameterType.STRING),
                "delivered_to_agent_id": ParameterMetadata(type=ParameterType.STRING),
                "delivered_to_agent_instance_id": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def send_peer_message(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> dict[str, Any]:
        if self._peer_registry is None or self._bridge_manager is None:
            return _failure_result(
                code="bridge.not_running",
                message="Bridge not started — call start_interface first",
            )
        raw = params.get("parameters", params)
        peer_id = str(raw.get("peer_id", ""))
        peer_agent_instance_id = raw.get("peer_agent_instance_id") or None
        content_text = str(raw.get("content", ""))
        important = bool(raw.get("important", False))
        if important:
            content_text = f"IMPORTANT: {content_text}"
        content: list[TextPart] = [TextPart(type="text", text=content_text)]
        try:
            outcome = dispatch_peer_send(
                bridge_manager=self._bridge_manager,
                peer_registry=self._peer_registry,
                agent_messaging_service=self._require_service(),
                sender_bridge_id=SYSTEM_SCHEDULER_ID,
                sender_agent_id=SYSTEM_AGENT_ID,
                sender_agent_instance_id=SYSTEM_SCHEDULER_ID,
                sender_session_label=SYSTEM_SCHEDULER_LABEL,
                sender_parent_pid=None,
                peer_id=peer_id,
                peer_agent_instance_id=peer_agent_instance_id,
                content=content,
            )
        except (
            PeerAmbiguousError,
            PeerUnreachableError,
            BridgeNotFoundError,
            BridgeQueueFullError,
            NativeWakeError,
        ) as exc:
            return _failure_result(code="peer_send_failed", message=str(exc))
        return _success_result(data=outcome.to_payload())

    # ------------------------------------------------------------------
    # Peer addressing by role name — peer_send_by_name / peer_claim_role
    # / peer_release_role. v10 Control #2 made the ``agent_role_binding``
    # state table (StateManagementInterface) the sole resolution + CAS
    # authority, retiring the former address-book backing; see
    # ``role_binding_store`` and the v10 cutover note in
    # ``workbench/2026-05-29_address_book_driven_peer_addressing.md``.
    # ------------------------------------------------------------------

    @platform_process(
        name="peer_send_by_name",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "name": ParameterMetadata(
                description=(
                    "Role name registered in agent_role_binding "
                    "(e.g. 'Coordinator', 'Architect', 'Git-Controller')."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "content": ParameterMetadata(
                description=(
                    "Message text. Prefix with 'IMPORTANT: ' to trigger a "
                    "native wake on the resolved recipient (same loop-"
                    "prevention contract as peer_send)."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "Delivery outcome plus the resolved (agent_id, agent_instance_id, "
            "session_label) the name pointed at when the call was made."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="peer_send_by_name delivery + resolution result",
            properties={
                "thread_id": ParameterMetadata(type=ParameterType.STRING),
                "message_id": ParameterMetadata(type=ParameterType.STRING),
                "delivery": ParameterMetadata(type=ParameterType.STRING),
                "resolved_agent_id": ParameterMetadata(type=ParameterType.STRING),
                "resolved_agent_instance_id": ParameterMetadata(
                    type=ParameterType.STRING,
                ),
                "resolved_session_label": ParameterMetadata(
                    type=ParameterType.STRING,
                ),
            },
        ),
    )
    def peer_send_by_name(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Resolve ``name`` via the ``agent_role_binding`` table, then peer_send."""
        if self._peer_registry is None or self._bridge_manager is None:
            return _failure_result(
                code="bridge.not_running",
                message="Bridge not started — call start_interface first",
            )
        raw = params.get("parameters", params)
        name = str(raw.get("name", "")).strip()
        content_text = str(raw.get("content", ""))
        if not name:
            return _failure_result(
                code="missing_name",
                message="peer_send_by_name requires a non-empty role 'name'.",
            )
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        # v10 Control #2.C cutover: resolution authority is now the
        # agent_role_binding table (state-interface), not the address book. A
        # backfilled (UNCLAIMED) binding still resolves — the send then queues
        # for replay rather than rejecting.
        try:
            role = resolve_role_binding(state_service, name)
        except RoleBindingVacantError as exc:
            return _failure_result(code="peer_role_vacant", message=str(exc))
        if not role.agent_instance_id:
            return _failure_result(
                code="peer_role_malformed",
                message=(
                    f"agent_role_binding row for {name!r} is missing "
                    "'agent_instance_id'; re-claim via peer_claim_role."
                ),
            )
        content: list[TextPart] = [TextPart(type="text", text=content_text)]
        # v10 Control #4: persist-first role dispatch. The role row was just
        # existence-gated above (Scope-decision C: an unknown name is rejected
        # before any persist). dispatch_role_send writes the authoritative
        # envelope, then best-effort delivers to the current holder — an
        # offline/zombie holder yields ``queued_for_replay`` (success; the row
        # is durable and the repair loop re-delivers), never a hard failure.
        # ``message_id`` is minted ONCE per logical send (stable across any
        # transport retry, so the deterministic external_id stays idempotent).
        message_id = f"arm-{secrets.token_hex(16)}"
        # REL-01 Fork 4 (Control #3.2 realised): stamp the sender from the
        # caller's DURABLE role — lifted into ``state`` from the flow trigger_data
        # by ``ActionProcessor._lift_inference_vertex_identity`` — so a role reply
        # routes back to whoever holds the caller's role (reconnect-surviving),
        # closing the KB-08 §4 sender-stamping wart. Uses the originating
        # instance when present, then the system scheduler sentinel for scheduler sends.
        sender = _resolve_role_send_sender(state, state_service)
        outcome = dispatch_role_send(
            bridge_manager=self._bridge_manager,
            peer_registry=self._peer_registry,
            agent_messaging_service=self._require_service(),
            role_name=name,
            role=role,
            sender_bridge_id=sender.bridge_id,
            sender_agent_id=sender.agent_id,
            sender_agent_instance_id=sender.agent_instance_id,
            sender_session_label=sender.session_label,
            sender_parent_pid=None,
            reply_to_role=sender.reply_to_role,
            content=content,
            message_id=message_id,
        )
        return _success_result(data=outcome.to_payload())

    def _send_handover_notice(
        self,
        *,
        peer_id: str,
        peer_agent_instance_id: str,
        prose: str,
        kind: str,
    ) -> bool:
        """Best-effort IMPORTANT role-handover notice to a specific instance (REL-04).

        Persists durably + wakes when the recipient is live. NEVER raises — a role
        claim must not fail because a handover notice could not be delivered
        (displacement often happens BECAUSE the prior holder is dead). Returns
        ``True`` on delivery, ``False`` on best-effort failure (loud log).
        """
        if self._peer_registry is None or self._bridge_manager is None:
            logger.warning(
                "REL-04 %s notice skipped (bridge not started): agi=%s",
                kind, peer_agent_instance_id,
            )
            return False
        try:
            dispatch_peer_send(
                bridge_manager=self._bridge_manager,
                peer_registry=self._peer_registry,
                agent_messaging_service=self._require_service(),
                sender_bridge_id=SYSTEM_ROLE_HANDOVER_ID,
                sender_agent_id=SYSTEM_AGENT_ID,
                sender_agent_instance_id=SYSTEM_ROLE_HANDOVER_ID,
                sender_session_label=SYSTEM_ROLE_HANDOVER_LABEL,
                sender_parent_pid=None,
                peer_id=peer_id,
                peer_agent_instance_id=peer_agent_instance_id,
                content=[TextPart(type="text", text=prose)],
            )
        except (
            PeerAmbiguousError,
            PeerUnreachableError,
            BridgeNotFoundError,
            BridgeQueueFullError,
            NativeWakeError,
        ) as exc:
            logger.warning(
                "REL-04 %s notice undelivered (agi=%s): %s — role claim proceeds",
                kind, peer_agent_instance_id, exc,
            )
            return False
        return True

    def _is_genuine_displacement(
        self, prior: Any, new_agent_instance_id: str, new_agent_session_id: str,
    ) -> bool:
        """True iff ``prior`` is a DIFFERENT session than the new holder (REL-07(2)).

        Keys on the stable ``agent_session_id`` when both sides have one — an
        ``agent_instance_id`` rotates on reconnect, so the old instance-id check
        mistook a same-session re-claim for a displacement and double-woke it.
        Falls back to instance identity only for a legacy binding row that carries
        no session id.
        """
        if prior is None:
            return False
        prior_sid = str(getattr(prior, "agent_session_id", "") or "")
        if prior_sid and new_agent_session_id:
            return prior_sid != new_agent_session_id
        return bool(prior.agent_instance_id) and (
            prior.agent_instance_id != new_agent_instance_id
        )

    def _displaced_target(self, prior: Any) -> tuple[str, str]:
        """Route the displaced-holder notice to the prior holder's CURRENT bridge.

        The role binding records the ``agent_instance_id`` as of the CLAIM; by the
        time a reconnect displaces it, that instance has rotated. Resolve the
        prior's live binding by its stable session id; fall back to the recorded
        instance when it has no session id, no live binding, or an ambiguous one
        (best-effort — an undeliverable notice never gates a claim).
        """
        prior_sid = str(getattr(prior, "agent_session_id", "") or "")
        if prior_sid and self._peer_registry is not None:
            try:
                live = self._peer_registry.resolve_by_agent_session_id(prior_sid)
            except PeerSessionAmbiguousError as exc:
                logger.warning(
                    "REL-04 displaced-notice: ambiguous session id %r — "
                    "falling back to recorded instance: %s", prior_sid, exc,
                )
                live = None
            if live is not None:
                return live.agent_id, live.agent_instance_id
        return prior.agent_id, prior.agent_instance_id

    def _notify_role_handover(
        self,
        *,
        name: str,
        new_agent_id: str,
        new_agent_instance_id: str,
        new_agent_session_id: str,
        prior: Any,
    ) -> None:
        """Fire the REL-04/§5.4 handover notices for a GENUINE role claim.

        Notifies a displaced PRIOR holder — routed to its CURRENT bridge via its
        stable session id (§5.4), not the stale recorded instance — then confirms
        to the new holder so it drains any role backlog. Best-effort throughout;
        the claim already succeeded and never fails on a notify. The caller
        suppresses this entirely for an idempotent self-re-claim, so any prior seen
        here is a different session. ``name`` is opaque — never special-cased.

        §5.4 provider-transition rule: a displaced holder that is an
        ``inference_provider`` has NO wake target (a provider consumes no
        messages), so the transition is LOG-LOUD, never silent — an audit line
        instead of an undeliverable notice. The displaced-notice fires ONLY when
        the displaced holder is a session.
        """
        if prior is not None and (
            str(getattr(prior, "holder_kind", "") or "") == HOLDER_KIND_INFERENCE_PROVIDER
        ):
            logger.warning(
                "role %r handover: displaced holder was an inference_provider "
                "(identity=%s) — no wake target, logging the transition for audit "
                "(new holder %s/%s).",
                name, getattr(prior, "holder_identity", {}), new_agent_id, new_agent_instance_id,
            )
        elif self._is_genuine_displacement(
            prior, new_agent_instance_id, new_agent_session_id,
        ):
            target_agent_id, target_instance_id = self._displaced_target(prior)
            self._send_handover_notice(
                peer_id=target_agent_id or new_agent_id,
                peer_agent_instance_id=target_instance_id,
                prose=_displaced_prose(name, new_agent_instance_id),
                kind="displaced-holder",
            )
        self._send_handover_notice(
            peer_id=new_agent_id,
            peer_agent_instance_id=new_agent_instance_id,
            prose=_new_holder_prose(name),
            kind="new-holder",
        )

    def _claimant_session_id(self, agent_instance_id: str) -> str:
        """The claimant's stable session id from its live ``peer_binding`` (REL-07(1)).

        Claim args never carry ``agent_session_id``, and an empty column makes the
        reconnect CAS (keyed on it alone) unable to re-point the role. Sourced from
        the claimant's OWN registered binding; "" when the bridge is not started or
        the instance is unregistered (no worse than the pre-fix state).
        """
        if self._peer_registry is None:
            return ""
        return self._peer_registry.agent_session_id_for_instance(agent_instance_id)

    def _settle_role_handover(
        self,
        *,
        name: str,
        agent_id: str,
        agent_instance_id: str,
        agent_session_id: str,
        outcome: dict[str, Any],
    ) -> dict[str, Any]:
        """Consume ``claim_role_binding_v4``'s outcome (action + pre-CAS prior) + fire
        the REL-04/§5.4 handover notices (§9 CUTOVER — the v4 claim now decides
        self-re-claim vs displace).

        ``action='refreshed'`` = an idempotent self-re-claim → report
        ``action='updated'`` (the ``/rename`` refresh contract) and fire NO wake.
        ``'claimed'`` (fresh) / ``'displaced'`` (prior set) → notify: a displaced prior
        at its current bridge (§5.4) + the new-holder confirm. The new holder is always
        a SESSION here (the claimant), so carry-forward (d) [confirm-iff-session] is
        satisfied by construction — no kind gate needed.

        The v4 outcome carries ``prior`` (a ``ResolvedRole``) for the notify ONLY — it
        is NOT json-serializable, so it MUST NOT reach the public ActionResult (result
        persistence json.dumps would TypeError on a real displace — Codex BLOCKER-1).
        A plain, schema-shaped ``{action, name, agent_instance_id}`` is returned.
        """
        action = str(outcome.get("action") or "")
        public: dict[str, Any] = {
            "action": action,
            "name": outcome.get("name"),
            "agent_instance_id": outcome.get("agent_instance_id"),
        }
        if action == "refreshed":
            public["action"] = "updated"  # /rename refresh contract; no wake
            return public
        self._notify_role_handover(
            name=name,
            new_agent_id=agent_id,
            new_agent_instance_id=agent_instance_id,
            new_agent_session_id=agent_session_id,
            prior=outcome.get("prior"),
        )
        return public

    @platform_process(
        name="peer_claim_role",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "name": ParameterMetadata(
                description="Role name to claim (e.g. 'Coordinator').",
                required=True,
                type=ParameterType.STRING,
            ),
            "agent_id": ParameterMetadata(
                description="Claiming session's agent_id (e.g. 'claude_code').",
                required=True,
                type=ParameterType.STRING,
            ),
            "agent_instance_id": ParameterMetadata(
                description="Claiming session's agent_instance_id (agi-...).",
                required=True,
                type=ParameterType.STRING,
            ),
            "session_label": ParameterMetadata(
                description=(
                    "Display label as of the claim. May or may not match "
                    "``name``; both are stored separately."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "agent_role_binding claim outcome (v10): action='claimed', the "
            "role name, and the bound agent_instance_id."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="peer_claim_role outcome (v10 agent_role_binding claim)",
            properties={
                "action": ParameterMetadata(type=ParameterType.STRING),
                "name": ParameterMetadata(type=ParameterType.STRING),
                "agent_instance_id": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def peer_claim_role(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Claim-or-replace the ``agent_role_binding`` row for ``name`` (v10 #2.C).

        Caller-supplied identity (the ``/rename`` skill threads ``agent_id`` /
        ``agent_instance_id`` from the ``peer_register`` response, and
        ``agent_session_id`` from the same response when a carrier set it). A
        full-row upsert replaces any prior binding for the name in place —
        including a backfilled UNCLAIMED ``agent_session_id``.
        """
        raw = params.get("parameters", params)
        name = str(raw.get("name", "")).strip()
        agent_id = str(raw.get("agent_id", "")).strip()
        agent_instance_id = str(raw.get("agent_instance_id", "")).strip()
        agent_session_id = str(raw.get("agent_session_id", "")).strip()
        session_label = str(raw.get("session_label", "") or name)
        if not name or not agent_id or not agent_instance_id:
            return _failure_result(
                code="missing_argument",
                message=(
                    "peer_claim_role requires non-empty 'name', "
                    "'agent_id', and 'agent_instance_id'."
                ),
            )
        # §6.1 reserved-keyspace gate: the general peer_claim_role never assigns a
        # SESSION-FILLED system slot (that is the §D.9 auto-assignment lane), and a
        # PLUGIN-OWNED slot is claimable ONLY by its declared owner — verified
        # against the SERVER-BUILT principal lifted into `state`, never spoofable
        # caller `params`. A normal (non-sys:) role → NOT_SYSTEM → proceeds.
        verdict = evaluate_system_slot_claim(name, state.get("call_context"))
        if verdict.decision is SystemSlotClaimDecision.REJECT:
            return _failure_result(
                code="system_slot_claim_denied",
                message=verdict.reason,
            )
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        # REL-07(1): source the claimant's stable session id from its OWN live
        # peer_binding row when the claim args omit it (they always do). An empty
        # agent_session_id makes the reconnect CAS (keyed on it alone) unable to
        # re-point this role — the class fresh-C's REL-07 diagnostic surfaced.
        if not agent_session_id:
            agent_session_id = self._claimant_session_id(agent_instance_id)
        # carry-forward (c) [§4.5.3/§11]: a durable session claim MUST carry a stable
        # agent_session_id (the reconnect CAS + the §D.9 succession key on it). The
        # pre-cutover 'no worse than pre-fix' empty-allowed fallback DIES at the §9
        # cutover — reject an unsourceable session claim rather than write a dead binding.
        if session_claim_requires_session_id(HOLDER_KIND_SESSION, agent_session_id):
            return _failure_result(
                code="missing_session_id",
                message=(
                    "peer_claim_role requires a non-empty agent_session_id for a "
                    "session holder; the claimant's peer_binding carried none "
                    "(launch with the session-id carrier exported, or pass it)."
                ),
            )
        # §5.5 entity-first: upsert the role entity BEFORE the binding CAS (a lost CAS
        # then leaves at most a harmless orphan entity; resolve never reads it).
        upsert_role_entity(state_service, name=name)
        # §9 CUTOVER: the v4 predicated-CAS claim (claim / displace / self-reclaim in
        # one) returns action + the PRE-CAS displaced prior for the §5.4 notify.
        outcome = claim_role_binding_v4(
            state_service,
            name=name,
            claim=HolderClaim(
                holder_kind=HOLDER_KIND_SESSION,
                holder_identity={"agent_id": agent_id, "session_label": session_label},
                agent_instance_id=agent_instance_id,
                agent_session_id=agent_session_id,
                session_label=session_label,
            ),
        )
        outcome = self._settle_role_handover(
            name=name,
            agent_id=agent_id,
            agent_instance_id=agent_instance_id,
            agent_session_id=agent_session_id,
            outcome=outcome,
        )
        return _success_result(data=outcome)

    @platform_process(
        name="peer_release_role",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "name": ParameterMetadata(
                description="Role name to release (delete its agent_role_binding row).",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "agent_role_binding release outcome (v10): the released flag and "
            "the role name."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="peer_release_role outcome (v10 agent_role_binding release)",
            properties={
                "released": ParameterMetadata(type=ParameterType.BOOLEAN),
                "name": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def peer_release_role(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> dict[str, Any]:
        """Delete the ``agent_role_binding`` row for ``name`` (v10 #2.C)."""
        raw = params.get("parameters", params)
        name = str(raw.get("name", "")).strip()
        if not name:
            return _failure_result(
                code="missing_name",
                message="peer_release_role requires a non-empty role 'name'.",
            )
        # §6.1 no-vacant-release: a system slot (reserved 'sys:' keyspace) is only
        # ever RE-BOUND (a claim that atomically replaces the holder), never
        # released to vacant — a vacant system slot strands its capability (e.g.
        # the autonomic inference lane). Reject the release.
        if is_system_role(name):
            return _failure_result(
                code="system_slot_release_denied",
                message=(
                    f"system slot {name!r} cannot be released to vacant (reserved "
                    f"keyspace); a system slot is only ever re-bound, never released."
                ),
            )
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        # §9 CUTOVER: hard-delete the v4 role_binding row (no-tombstone §5.1).
        outcome = release_role_binding_v4(state_service, name)
        return _success_result(data=outcome)

    @platform_process(
        name="peer_holds_role",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "name": ParameterMetadata(
                description="Role name to re-check ownership of (e.g. 'Git-Controller').",
                required=True,
                type=ParameterType.STRING,
            ),
            "agent_instance_id": ParameterMetadata(
                description=(
                    "The caller's own agent_instance_id (agi-...). Its STABLE session "
                    "id is sourced server-side from the peer_binding — a caller-supplied "
                    "session id is never trusted."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "Act-time role-ownership re-check (§5.0): whether the caller's session "
            "still holds the role, plus its resolved stable session id."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="peer_holds_role outcome (§5.0 act-time ownership re-check)",
            properties={
                "holds": ParameterMetadata(type=ParameterType.BOOLEAN),
                "name": ParameterMetadata(type=ParameterType.STRING),
                "agent_session_id": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def peer_holds_role(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> dict[str, Any]:
        """READ-ONLY act-time ownership re-check (§5.0) — does the caller's session
        STILL hold ``name``?

        The §9 cutover makes this the Git-Controller Step-9.5 re-check: ``holds_role``
        is PULL-TRUTH over the v4 ``role_binding`` table (the prior notice-drain was a
        best-effort push signal). Sources the caller's STABLE session id from its OWN
        ``peer_binding`` row (by ``agent_instance_id`` — the REL-07 pattern), never a
        caller-supplied session id, then compares it to the live holder's. It NEVER
        writes: the anti-pattern is a self-re-claim (a WRITE that would STEAL the role
        back from a legitimate new holder) — this is a pure read.
        """
        raw = params.get("parameters", params)
        name = str(raw.get("name", "")).strip()
        agent_instance_id = str(raw.get("agent_instance_id", "")).strip()
        if not name or not agent_instance_id:
            return _failure_result(
                code="missing_argument",
                message="peer_holds_role requires non-empty 'name' and 'agent_instance_id'.",
            )
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        agent_session_id = self._claimant_session_id(agent_instance_id)
        holds = holds_role(state_service, name, agent_session_id)
        return _success_result(
            data={"holds": holds, "name": name, "agent_session_id": agent_session_id},
        )

    # dead post-S3; removed with the full AB-role-WRITE retirement follow-up
    def _get_address_book_service(self) -> Any:
        """Return the bound address_book_service, or ``None`` if unavailable."""
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            return None
        get_service = getattr(orchestrator, "get_service", None)
        if get_service is None:
            return None
        try:
            return get_service("address_book_service")
        except Exception:
            return None

    def _get_state_service(self) -> Any:
        """Return the bound state_service, or ``None`` if unavailable.

        The v10 Control #2.C resolution + claim authority — the role verbs
        (``peer_send_by_name`` / ``peer_claim_role`` / ``peer_release_role``)
        go through the ``agent_role_binding`` state table via this handle.
        """
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            return None
        get_service = getattr(orchestrator, "get_service", None)
        if get_service is None:
            return None
        try:
            return get_service("state_service")
        except Exception:
            return None

    def _run_startup_backfills(self) -> None:
        """Run the ONE-SHOT, durable-marker-gated GAP-2 ``agent_message``
        important-column projection off the load-bearing ``state_service``.

        (The Control #2 ``agent_role_binding`` legacy-role seed that used to run
        here was RETIRED at the §9 cutover — see the inline note below. It wrote the
        legacy table AFTER readiness set the v4-migration marker, which would strand
        rows out of v4 and break the parity-proof — Codex BLOCKER-2.)

        ``state_service`` is resolved DIRECTLY (not via the catch-all-None
        ``_get_*`` helpers) so a genuine lookup fault PROPAGATES instead of
        masquerading as "unbound" (Codex MAJOR-1); a partial/failed backfill leaves
        its marker unset so the next boot re-runs.
        """
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref not injected — cannot run the "
                "startup backfills",
            )
        state_service = orchestrator.get_service("state_service")
        if state_service is None:
            raise RuntimeError(
                f"{self.name}: state_service unbound — it is the load-bearing "
                "authority for the startup backfills; refusing to proceed",
            )
        # Control #2 B5: seed agent_role_binding from legacy address-book
        # RETIRED at the §9 cutover (slice-D): the legacy address-book →
        # agent_role_binding seed is GONE. It wrote the LEGACY table, but readiness
        # now runs the v4 migration (agent_role_binding → role_binding) + sets its
        # one-shot marker AHEAD of this startup step — a legacy write here would
        # strand rows out of v4 while the marker reads 'done' (parity never
        # re-converges), breaking the parity-proof the §9 quiesce-equivalent relies
        # on (Codex BLOCKER-2). v10 deliberately left the address book behind for
        # role resolution; a zombie legacy writer post-flip re-warms that retired
        # path, so it dies here. Roles are now claimed at runtime via peer_claim_role
        # into the v4 table; the migration copies any pre-existing agent_role_binding
        # rows forward.
        # GAP-2 SQL-lockdown: project metadata.important onto the new
        # core__agent_message.important column for pre-migration rows (after the
        # critical seed, so its unbounded read cannot defer the cutover).
        msg = backfill_message_important(state_service)
        msg_updated = msg.get("updated")
        logger.info(
            "%s: agent_message important backfill status=%s (%d row(s) flipped)",
            self.name,
            msg.get("status"),
            len(msg_updated) if isinstance(msg_updated, list) else 0,
        )
        # REL-05 F2: grandfather delivered role-message history so the new
        # consumption-gated drain predicate cannot flood-re-emit it. Rides this
        # same injected authenticated state_service (never opens its own
        # connection), so it is immune to the JOS-02 migration-credential class.
        consumed = backfill_role_message_consumed(state_service)
        consumed_updated = consumed.get("updated")
        logger.info(
            "%s: agent_role_message consumed backfill status=%s (%d row(s) "
            "grandfathered)",
            self.name,
            consumed.get("status"),
            len(consumed_updated) if isinstance(consumed_updated, list) else 0,
        )

    # ------------------------------------------------------------------
    # IO interface lifecycle — start_interface / stop_interface
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # D-IF7 / D-IF8 — SessionInferenceProvider sidecar (v4 §4-5)
    # ------------------------------------------------------------------

    def get_inference_provider(
        self, agent_instance_id: str,
    ) -> SessionInferenceProvider | None:
        """Public accessor for the per-bridge inference vertex sidecar.

        Returns the :class:`SessionInferenceProvider` registered for
        ``agent_instance_id``, or ``None`` when no provider is bound
        (streamable peers, stdio peers that did not pass
        ``provides_inference=True``, or peers that have since
        unregistered). The wrapper at
        ``inference_service/__init__.py`` falls back to the bound
        default plugin on ``None`` per v4 §7.

        INF-01 bridge-open gate (option b): the sidecar is cleared only on the
        UNREGISTER path (``_clear_inference_providers_for_bridge``), so an
        idle-swept / closed bridge leaves a STALE entry whose ``append_event``
        would raise ``BridgeNotFoundError`` (bridge popped from ``_bridges``).
        Cross-check the provider's bridge is OPEN before returning it, so the
        Phase-5 resolver DEFERs (never routes to a dead bridge) instead of
        erroring. REL-09 removes the stale entry at the source
        (``sweep_idle``/``close`` → the full unregister cleanup); this gate makes
        the resolver's PROVIDER verdict robust to swept holders meanwhile.
        """
        with self._inference_providers_lock:
            provider = self._inference_providers.get(agent_instance_id)
        if provider is None or self._bridge_manager is None:
            return provider
        bridge = self._bridge_manager.get(provider.bridge_id)
        if bridge is None or bridge.closed:
            return None
        return provider

    def get_autonomic_provider(self) -> SessionInferenceProvider | None:
        """Resolve the ``sys:autonomic`` system-slot holder's LIVE provider.

        INF-01 fault-edge: the DEFAULT verdict (the organism's own error/result
        turn, no per-flow vertex binding) routes to the frontier session holding
        ``sys:autonomic`` instead of the local default model. Resolution is
        v4-NATIVE — the ``role_binding`` table via
        :func:`resolve_role_binding_v4` — NOT ``resolve_role_to_instance``,
        which reads the LEGACY ``agent_role_binding`` table where the
        system-slot constant does not live (until the slice-D cutover). The
        holder's ``agent_instance_id`` then maps to the bridge-open-gated
        provider.

        Returns ``None`` when the slot is VACANT
        (:class:`RoleBindingVacantError`) or its holder has no live provider
        (bridge swept/closed — the gate above). The caller
        (``InferenceService._route_vertex``) then falls to the local default:
        vacant → LOCAL is the sub-slice-1 interim (nothing CLAIMS the slot until
        the sub-slice-2 auto-assignment lands, at which point the policy flips to
        vacant → DEFER — see that call site).
        """
        state = self._get_state_service()
        try:
            resolved = resolve_role_binding_v4(state, SYS_AUTONOMIC_SLOT)
        except RoleBindingVacantError:
            return None
        return self.get_inference_provider(resolved.agent_instance_id)

    def _has_live_inference_provider(self, agent_instance_id: str) -> bool:
        """Bridge-open-gated provider presence — the §D.9 candidate filter (D1)."""
        return self.get_inference_provider(agent_instance_id) is not None

    def _forward_completion_request(
        self, agent_instance_id: str, row: dict[str, object],
    ) -> None:
        """Carry one durable completion-request row to a holder's bridge.

        The ``forward_completion`` collaborator injected into
        :class:`AutonomicAssignment` — resolves the holder's live
        :class:`SessionInferenceProvider` and emits the typed
        ``inference_completion_request`` event. Raises on a missing
        provider or malformed row: the CALLER owns the stamp-clear
        (the row returns to the unassigned backlog, never lost).
        """
        import json

        provider = self.get_inference_provider(agent_instance_id)
        if provider is None:
            raise FrameworkError(
                f"completion forward: instance {agent_instance_id!r} has no "
                "live inference provider",
            )
        messages = json.loads(str(row.get(COL_ICR_MESSAGES) or "[]"))
        correlation = json.loads(str(row.get(COL_ICR_CORRELATION) or "{}"))
        provider.forward_completion_request(
            request_id=str(row.get(COL_ICR_REQUEST_ID) or ""),
            purpose=str(row.get(COL_ICR_PURPOSE) or ""),
            messages=messages,
            correlation=correlation,
        )

    def _resubmit_vertex(self, flow_id: str, method: str) -> bool:
        """SUB-05 RESUBMIT primitive — re-drive one un-consumed vertex flow.

        The ``resubmit_vertex`` collaborator injected into
        :class:`AutonomicAssignment` (INF-06 reliability). Re-enters the flow's
        owning session with a FRESH ``process_results`` initial vertex
        (:func:`build_initial_vertex_action` — observation removed, instructions
        emptied → a fresh decode of the session's CURRENT durable state, per
        Architect §2d/§6-bis; NEVER a replay of the recorded decode). ``method``
        is observability-only: whether the original forward was a
        ``process_results`` or ``process_error`` vertex, the only coherent
        re-entry is the same fresh initial vertex (re-entering ``process_error``
        WITHOUT its ephemeral error observation is incoherent; WITH it is the
        forbidden replay). The failure's consequence is already durable — the
        plan's ``[>]`` marker stays on the failed step and the failed action
        result is stored — so the fresh decode is not blind to it.

        Reuses the SAME ``flow_id`` so a re-forward / re-defer of the re-driven
        vertex upserts the SAME ``core__inference_deferred_vertex`` row (the
        clear-on-reentry site). Returns True iff the fresh vertex was submitted;
        NEVER raises (the sweep and drain are per-row fault-isolated) — a missing
        collaborator, an unknown flow, or a submit fault logs loud and returns
        False so the row stays durably queued for the next tick.
        """
        try:
            flow_manager = self._flow_manager
            orchestrator = getattr(self, "orchestrator_ref", None)
            action_factory = getattr(self, "action_factory", None)
            builder = self._compilation_context_builder
            if (
                flow_manager is None
                or orchestrator is None
                or action_factory is None
                or builder is None
            ):
                logger.warning(
                    "INF-06 RESUBMIT flow=%s (method=%s): collaborators not "
                    "injected — cannot re-drive; row stays queued.",
                    flow_id, method,
                )
                return False
            session_id = flow_manager.get_flow_session_id(flow_id)
            if not session_id:
                logger.warning(
                    "INF-06 RESUBMIT flow=%s (method=%s): no owning session "
                    "(flow unknown) — cannot re-drive; row stays queued.",
                    flow_id, method,
                )
                return False
            action_def = build_initial_vertex_action(
                session_id=session_id, flow_id=flow_id, orchestrator=orchestrator,
            )
            context = builder.build_context(session_id=session_id, flow_id=flow_id)
            action_factory.submit_action_definition(
                action_definition=action_def, context=context,
            )
            logger.info(
                "INF-06 RESUBMIT flow=%s session=%s method=%s: fresh vertex "
                "submitted (fresh decode of current durable state).",
                flow_id, session_id, method,
            )
        except Exception:  # noqa: BLE001 — per-row isolation: never abort the sweep/drain
            logger.exception(
                "INF-06 RESUBMIT flow=%s (method=%s) FAULTED — row stays "
                "durably queued for the next tick.",
                flow_id, method,
            )
            return False
        return True

    @platform_process(
        name="submit_autonomic_completion",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "request_id": ParameterMetadata(
                description=(
                    "The completion request id (icr-...) from the "
                    "inference_completion_request bridge event this serve "
                    "answers."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "text": ParameterMetadata(
                description=(
                    "The completion text the holder produced for the "
                    "request's messages payload."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "Serve outcome: the CAS verdict (served / already_served / "
            "already_failed / unknown_request) and the request id."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description=(
                "submit_autonomic_completion outcome (INF-02 serve verb)"
            ),
            properties={
                "status": ParameterMetadata(type=ParameterType.STRING),
                "request_id": ParameterMetadata(type=ParameterType.STRING),
                "resume_process_key": ParameterMetadata(
                    type=ParameterType.STRING,
                ),
            },
        ),
    )
    def submit_autonomic_completion(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> dict[str, Any]:
        """Serve one INF-02 completion request (holder → platform callback).

        CAS ``pending→served`` is the idempotency gate: exactly one serve
        wins and submits the consumer's resume continuation (returned in
        ``actions`` — the platform's Pattern-6a submission; the resume
        action_def carries NO result_processor, so its completion is
        terminal). A second serve reports ``already_served`` and submits
        nothing; an unknown request id is a typed rejection.

        Empty/whitespace ``text`` is rejected DELIBERATELY (Reviewer-A N2):
        an empty planning completion is degenerate — the row stays pending
        and the serve-timeout sweep re-forwards it. Do not relax this to
        accept ''.
        """
        raw = params.get("parameters", params)
        request_id = str(raw.get("request_id", "")).strip()
        text = str(raw.get("text", ""))
        if not request_id or not text.strip():
            return _failure_result(
                code="missing_argument",
                message=(
                    "submit_autonomic_completion requires non-empty "
                    "'request_id' and 'text'."
                ),
            )
        verdict, row = serve_completion_request(
            self._get_state_service(), request_id=request_id, result_text=text,
        )
        if verdict != SERVE_SERVED or row is None:
            return _failure_result(
                code=verdict,
                message=(
                    f"completion request {request_id!r} not served: {verdict}"
                ),
            )
        resume_action = _build_resume_action(row)
        self.logger.info(
            "completion request %s SERVED (purpose=%s) — submitting resume "
            "continuation %s",
            request_id,
            row.get(COL_ICR_PURPOSE),
            row.get(COL_ICR_RESUME_PROCESS_KEY),
        )
        return _success_result(
            data={
                "status": verdict,
                "request_id": request_id,
                "resume_process_key": str(
                    row.get(COL_ICR_RESUME_PROCESS_KEY) or "",
                ),
            },
            actions=[resume_action],
        )

    @platform_process(
        name="set_autonomic_slot",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description=(
                    "Target session's agent_instance_id (agi-...) to bind as "
                    "the sys:autonomic holder. Must be live and registered "
                    "with provides_inference=True."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "sys:autonomic manual-set outcome: the claim action and the "
            "bound agent_instance_id."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="set_autonomic_slot outcome (sys:autonomic manual override)",
            properties={
                "action": ParameterMetadata(type=ParameterType.STRING),
                "name": ParameterMetadata(type=ParameterType.STRING),
                "agent_instance_id": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def set_autonomic_slot(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> dict[str, Any]:
        """Manually bind ``sys:autonomic`` to a session (INF-01 §D.9 override).

        The SANCTIONED manual lane for the reserved slot: ``peer_claim_role``
        rejects ``sys:*`` for user-facing callers (§6.1), so operator/manual
        rebinding goes through here. Not principal-gated — any bridge session
        may re-point the slot (override-then-resume; the auto-assignment
        triggers keep it filled afterwards). Fails fast on a target that
        cannot serve the lane (no live inference provider).
        """
        raw = params.get("parameters", params)
        agent_instance_id = str(raw.get("agent_instance_id", "")).strip()
        if not agent_instance_id:
            return _failure_result(
                code="missing_argument",
                message="set_autonomic_slot requires a non-empty 'agent_instance_id'.",
            )
        assignment = self._autonomic_assignment
        if assignment is None:
            return _failure_result(
                code="bridge.not_started",
                message="Bridge interface is not running; no autonomic lifecycle.",
            )
        outcome = assignment.set_slot(agent_instance_id=agent_instance_id)
        if not outcome.get("success"):
            return _failure_result(
                code=str(outcome.get("code") or "set_autonomic_slot_failed"),
                message=str(outcome.get("message") or "set_autonomic_slot failed"),
            )
        return _success_result(data={
            "action": outcome.get("action"),
            "name": outcome.get("name"),
            "agent_instance_id": outcome.get("agent_instance_id"),
        })

    def resolve_role_to_instance(self, role: str) -> str | None:
        """◆R2 resolve-by-role: current instance holding ``role``, or ``None``.

        The durable ``agent_role_binding`` table (via ``peer_claim_role``) is
        the sole resolution authority — ``agent_instance_id`` is minted fresh
        per bridge launch, so the Phase-5 vertex resolver binds by role and
        maps role → current instance here at delivery time. Returns ``None``
        when the role has no binding (vacant / never claimed) or the bound
        row carries no instance id. This is the read-only reverse of the
        outbound tag write in ``platform_surface`` and reuses the same
        ``role_binding_store`` authority as ``peer_send_by_name`` — never a
        parallel resolution path.
        """
        if not role:
            return None
        state_service = self._get_state_service()
        if state_service is None:
            return None
        try:
            resolved = resolve_role_binding(state_service, role)
        except RoleBindingVacantError:
            return None
        return resolved.agent_instance_id or None

    def _register_inference_provider(
        self,
        *,
        bridge_id: str,
        agent_instance_id: str,
        agent_id: str,
        session_label: str | None,
    ) -> None:
        """Sidecar mutation: bind a provider for the (bridge, agent_instance) pair.

        Pop-then-insert semantics per v4 §4 — the post-register hook
        replaces any prior entry under the same ``agent_instance_id`` so
        a stale binding from a crashed predecessor cannot survive a
        legitimate re-register.
        """
        if self._bridge_manager is None:
            logger.warning(
                "_register_inference_provider called before start_interface; "
                "skipping for agent_instance_id=%s",
                agent_instance_id,
            )
            return
        provider = SessionInferenceProvider(
            bridge_id=bridge_id,
            agent_instance_id=agent_instance_id,
            agent_id=agent_id,
            session_label=session_label,
            bridge_manager=self._bridge_manager,
        )
        with self._inference_providers_lock:
            self._inference_providers.pop(agent_instance_id, None)
            self._inference_providers[agent_instance_id] = provider
            # Live again — drop any disconnected tombstone for this instance.
            self._inference_provider_tombstones.pop(agent_instance_id, None)

    def _clear_inference_providers_for_bridge(
        self,
        bridge_id: str,
    ) -> int:
        """Sidecar cleanup: drop every provider tied to ``bridge_id``.

        Caller MUST snapshot the per-bridge bindings via
        :meth:`PeerRegistry.list_by_bridge` BEFORE invoking
        :meth:`PeerRegistry.unregister`; this method walks that snapshot
        and removes any matching sidecar entry. Silent no-op for
        agent_instance_ids that have no provider (streamable peers,
        stdio peers that did not pass ``provides_inference=True``).
        """
        if self._peer_registry is None:
            return 0
        bindings = self._peer_registry.list_by_bridge(bridge_id)
        cleared = 0
        with self._inference_providers_lock:
            for binding in bindings:
                if self._inference_providers.pop(binding.agent_instance_id, None) is not None:
                    cleared += 1
                    # ◆R2 case 3b: tombstone the disconnected instance so the
                    # resolver DEFERs (not silent-Qwen) a flow explicitly bound
                    # to it. LRU-evict the oldest tombstone past the cap.
                    self._inference_provider_tombstones.pop(
                        binding.agent_instance_id, None,
                    )
                    self._inference_provider_tombstones[binding.agent_instance_id] = None
                    while len(self._inference_provider_tombstones) > _INFERENCE_TOMBSTONE_CAP:
                        evicted, _ = self._inference_provider_tombstones.popitem(last=False)
                        # N1 (Rev-C): LOUD eviction — a roleless bound instance
                        # aging out means its stale flows now route DEFAULT.
                        logger.warning(
                            "inference-provider tombstone evicted roleless bound "
                            "instance %s (cap=%d): its stale in-flight flows will "
                            "now route DEFAULT (default model), not DEFER. "
                            "Role-bound sessions are immune (◆R2 durable path). "
                            "Claim a role to get durable vertex protection.",
                            evicted,
                            _INFERENCE_TOMBSTONE_CAP,
                        )
        return cleared

    def _full_bridge_cleanup(self, bridge_id: str) -> int:
        """REL-09: the sweeper's per-bridge cleanup — identical to the close route.

        Sidecar clear (+ ◆R2 tombstone) → sys:autonomic Trigger-2 hook →
        registry unregister, all keyed on ``bridge_id``. Returns rows removed.
        """
        peer_registry = self._peer_registry
        if peer_registry is None:
            return 0
        return run_full_bridge_cleanup(
            bridge_id,
            inference_provider_clear=self._clear_inference_providers_for_bridge,
            autonomic_on_close=(
                self._autonomic_assignment.on_bridge_close
                if self._autonomic_assignment is not None else None
            ),
            unregister=peer_registry.unregister,
        )

    def _on_sweep_tick(self) -> None:
        """Composed REL-09 sweeper on_tick rider: INF-02 serve-timeout sweep +
        INF-06 forwarded-vertex re-drive + terminal-row GC + REL-05 deaf-wake
        escalation.

        Each rider is fault-isolated so one fault never skips the rest of the
        tick: the INF-02 sweep and the REL-05 reconciler can raise, so they are
        wrapped HERE (the sweeper's single outer guard would otherwise abort the
        tick before later riders run); the two INF-06 riders self-isolate
        (internal try/except → never raise, return counts) so they are called
        directly. Every rider runs every tick.
        """
        autonomic = self._autonomic_assignment
        if autonomic is not None:
            try:
                autonomic.completions.sweep_serve_timeouts()
            except Exception:  # noqa: BLE001 — one rider's fault must not skip the other
                logger.exception(
                    "serve-timeout sweep rider FAULTED; continuing to REL-05 "
                    "escalation",
                )
            # INF-06 reliability: re-drive forwarded vertices whose holder died /
            # timed out, then reap aged terminal 'failed' rows. Both self-isolate.
            autonomic.forwarded.sweep_serve_timeouts()
            autonomic.forwarded.gc_terminal_rows()
        reconciler = self._direct_wake_reconciler
        if reconciler is not None:
            try:
                reconciler.reconcile()
            except Exception:  # noqa: BLE001 — the reaper must survive a state fault, loudly
                logger.exception("REL-05 deaf-wake escalation FAULTED; sweeper continues")

    def was_inference_provider_bound(self, agent_instance_id: str) -> bool:
        """◆R2 case 3b: True if ``agent_instance_id`` held a provider earlier
        in this process lifetime but its bridge has since disconnected.

        Lets the vertex resolver distinguish an *explicitly-bound-but-absent*
        roleless session (→ DEFER, never silent-Qwen) from a never-bound /
        post-restart / streamable instance (→ DEFAULT).

        GOVERNING-RULE SCOPE (N1, Rev-C ruling 2026-07-02) — the precise
        limit of "never silent-Qwen anything explicitly bound this lifetime":
        - For ROLE-BOUND sessions the rule holds ABSOLUTELY — the ◆R2 durable
          ``agent_role_binding`` path (``resolve_role_to_instance``) never
          returns DEFAULT, so a role-bound flow always PROVIDER-or-DEFERs
          regardless of this tombstone.
        - For ROLELESS sessions the rule holds WITHIN tombstone capacity
          (``_INFERENCE_TOMBSTONE_CAP``): a roleless instance is LRU-evicted
          after that many subsequent inference-provider disconnects, after
          which its stale flows degrade to DEFAULT. This is the principled,
          leak-free tradeoff — a roleless session has no durable identity, and
          "never silent-Qwen a bound session" vs "never-bound/streamable MUST
          go DEFAULT" (R6 / D-IF11) are irreconcilable under bounded memory.
          Eviction is LOUD (WARNING) so the rare aged-out case is visible;
          claiming a role is the escape from the bound. Loud eviction is
          OBSERVABILITY, not a routing fix.
        """
        with self._inference_providers_lock:
            return agent_instance_id in self._inference_provider_tombstones

    def _router_is_declared(self) -> bool:
        """True if this homunculus's active manifest declares the router.

        D11 ruling R1: the "router present" predicate is MANIFEST-DECLARED,
        never runtime-probed (live plugin registry start-order is a race;
        port/pid probing is defensive and stale-file-prone). Both colors of
        a router topology see the identical declared set during a swap, so
        this predicate agrees for both — the load-bearing property that
        keeps router topology's "neither child ever writes" invariant intact.

        Fails loud (never guesses) when the declared set cannot be
        determined — an absent ``orchestrator_ref``/``APP_HOME`` or an
        absent manifest.yaml (``load_manifest_plugin_set`` returns
        ``None``, its "no gating" sentinel) means we cannot tell whether
        the router is in this homunculus's topology, and D11's routerless
        write path must never guess.
        """
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref not injected at start_interface "
                "— cannot resolve the D11 router-presence predicate",
            )
        app_home = getattr(orchestrator, "APP_HOME", None)
        if app_home is None:
            raise RuntimeError(
                f"{self.name}: orchestrator.APP_HOME unavailable at "
                "start_interface — cannot resolve the D11 router-presence "
                "predicate",
            )
        declared_plugins = load_manifest_plugin_set(app_home)
        if declared_plugins is None:
            raise RuntimeError(
                f"{self.name}: {app_home}/config/manifest.yaml is absent — "
                "the D11 router-presence predicate cannot be determined "
                "(never guessed). A homunculus without a manifest must "
                "still declare its topology before the bridge can decide "
                "whether it owns its own port-discovery file.",
            )
        return _ROUTER_PLUGIN_NAME in declared_plugins

    def _build_peer_registry(self) -> PeerRegistry:
        """Wire a :class:`PeerRegistry` over the platform persistent backend.

        PeerRegistry's persistent backend depends on a vault plugin having
        imported its ``postgres_backend`` module before this point (see
        peer_registry.py + 2026-06-01 reconnect-UX design §4). vault's
        ``prepare_for_readiness`` runs earlier, so the ``"postgres"``
        backend factory is registered by the time ``start_interface``
        fires. Raises ``RuntimeError`` if the orchestrator or
        ``state_service`` is missing — neither is recoverable mid-startup.
        """
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref not injected at start_interface",
            )
        state_service = orchestrator.get_service("state_service")
        if state_service is None:
            raise RuntimeError(
                f"{self.name}: state_service unavailable at start_interface — "
                "PeerRegistry persistence cannot initialize",
            )
        peer_registry = PeerRegistry(state_service=state_service)
        peer_registry.register_native_wake_adapter("claude_code", self)
        return peer_registry

    @platform_process(
        name="start_interface",
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={},
        output_type="object",
        output_description="Bridge API startup confirmation.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Bridge API host, port, and endpoint prefix.",
            properties={
                "host": ParameterMetadata(
                    type=ParameterType.STRING, required=True,
                ),
                "port": ParameterMetadata(
                    type=ParameterType.INTEGER, required=True,
                ),
                "bridge_url": ParameterMetadata(
                    type=ParameterType.STRING, required=True,
                ),
                "started_at": ParameterMetadata(
                    type=ParameterType.STRING, required=True,
                ),
            },
        ),
    )
    def start_interface(
        self,
        params: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> dict[str, Any]:
        if self._server_thread is not None and self._server_thread.is_alive():
            return _failure_result(
                code="bridge.already_running",
                message="Bridge API server is already running",
            )
        bridge_config = self._build_bridge_runtime_config()
        self._host = bridge_config.host
        self._max_message_chars = bridge_config.max_message_chars
        self._bridge_manager = BridgeSessionManager(
            session_id_factory=self._mint_session_id,
            idle_timeout_s=bridge_config.bridge_idle_timeout_seconds,
            max_pending_events=bridge_config.max_pending_events,
            long_poll_timeout_s=bridge_config.long_poll_timeout_seconds,
            # M5 §14.4: lazy resolver — vault + ledger may not be live at
            # bridge-construction time (startup ordering); the closure
            # looks them up at session-open time via orchestrator.
            policy_resolver=self._resolve_oauth_session_policy,
        )
        self._peer_registry = self._build_peer_registry()
        # REL-09 startup reconciliation: at boot ZERO live bridges exist, so
        # every persisted peer_binding row is a pre-restart zombie (SIGTERM
        # swaps never close bridges). Purge them; live sessions re-register
        # on reconnect within seconds.
        purge_preboot_bindings(self._peer_registry)
        # ONE-SHOT startup backfills (Control #2 agent_role_binding cutover seed
        # + GAP-2 agent_message important-column projection) BEFORE the FastAPI
        # surface comes up: every legacy role RESOLVES at cutover (a send queues
        # for replay instead of rejecting), and the silent peer-inbox stays
        # correct after the read-path cutover. Services are initialized by the
        # time start_interface runs, so they are resolvable here.
        self._run_startup_backfills()
        # INF-01 sub-slice-2: the sys:autonomic lifecycle rides the live
        # bridge collaborators; its Trigger-1/2 hooks are handed to
        # register_routes below (seam §b: this plugin owns the hook bodies).
        self._autonomic_assignment = AutonomicAssignment(
            state_service=self._get_state_service,
            list_active_bridges=self._bridge_manager.list_active,
            bindings_for_bridge=self._peer_registry.list_by_bridge,
            live_binding_for_session=self._peer_registry.resolve_by_agent_session_id,
            has_live_provider=self._has_live_inference_provider,
            send_notice=self._send_handover_notice,
            grace_seconds=bridge_config.autonomic_grace_seconds,
            forward_completion=self._forward_completion_request,
            serve_window_seconds=bridge_config.completion_serve_window_seconds,
            resubmit_vertex=self._resubmit_vertex,
            forward_serve_window_seconds=bridge_config.forward_serve_window_seconds,
            forward_attempts_cap=bridge_config.forward_attempts_cap,
            terminal_gc_after_seconds=bridge_config.terminal_gc_after_seconds,
        )
        # REL-05: the server-side escalation reconciler (rides the sweeper
        # on_tick below alongside the INF-02 serve-timeout sweep).
        self._direct_wake_reconciler = DirectWakeReconciler(
            service=self._require_service(),
            bridge_manager=self._bridge_manager,
            peer_registry=self._peer_registry,
            cap=bridge_config.re_emit_cap,
            re_emit_window_s=float(bridge_config.re_emit_window_seconds),
            clock=lambda: datetime.now(UTC),
        )
        # REL-09: drive the idle sweep — every expired bridge gets the SAME
        # full cleanup the close route runs (sidecar + tombstone + Trigger-2
        # + registry unregister), so swept and closed are indistinguishable.
        # INF-02 + REL-05 ride the same cadence via _on_sweep_tick: each tick
        # runs the completion serve-timeout sweep AND the deaf-wake escalation,
        # each fault-isolated so one failing does not skip the other.
        sweeper = BridgeLifecycleSweeper(
            bridge_manager=self._bridge_manager,
            cleanup=self._full_bridge_cleanup,
            interval_seconds=bridge_config.bridge_sweep_interval_seconds,
            on_tick=self._on_sweep_tick,
        )
        self._bridge_sweeper = sweeper
        sweeper.start()
        self._platform_surface = self._build_platform_surface(
            bridge_manager=self._bridge_manager,
            bridge_config=bridge_config,
        )
        self._app = self._build_fastapi_app(
            bridge_manager=self._bridge_manager,
            peer_registry=self._peer_registry,
            platform_surface=self._platform_surface,
            bridge_config=bridge_config,
        )
        if bridge_config.streamable_enabled:
            self._mount_streamable_transport(
                app=self._app,
                bridge_manager=self._bridge_manager,
                peer_registry=self._peer_registry,
                platform_surface=self._platform_surface,
                bridge_config=bridge_config,
            )
        # Bridge port is in-process only — no file write per Slice 3 of
        # the bridge-port-routing design. The macos_self_deployment_plugin
        # heartbeat reads the bound port from ``self.bridge_port`` via
        # cross-plugin lookup and passes it to ``router.register_color``.
        self._port = find_available_port(preferred=bridge_config.port)
        self._server_started_event.clear()
        self._server_thread = threading.Thread(
            target=self._run_server,
            name=f"{PLUGIN_NAME}-server",
            daemon=True,
        )
        self._server_thread.start()
        if not self._server_started_event.wait(timeout=_SERVER_START_TIMEOUT_S):
            self._shutdown_server()
            return _failure_result(
                code="bridge.startup_failed",
                message=(
                    f"Bridge API server did not signal startup within "
                    f"{_SERVER_START_TIMEOUT_S}s"
                ),
            )
        # D11 (workbench/2026-07-13_d11_bridge_port_discovery_routerless_ruling.md):
        # in router-less topology this plugin IS the bridge's front door, so
        # it is the sanctioned writer of its own discovery file — never in
        # router topology (R4), only after bind is confirmed above (R3),
        # rewritten on every start to self-heal port re-roll staleness (R3).
        if not self._router_is_declared():
            write_routerless_bridge_port_file(self._port)
        if bridge_config.streamable_enabled:
            streamable_failure = self._start_streamable_server(bridge_config)
            if streamable_failure is not None:
                self._shutdown_server()
                return streamable_failure
        started_at = _now_iso()
        bridge_url = f"http://{self._host}:{self._port}"
        result_data: dict[str, Any] = {
            "host": self._host,
            "port": self._port,
            "bridge_url": bridge_url,
            "started_at": started_at,
        }
        if bridge_config.streamable_enabled:
            result_data["streamable_url"] = (
                f"http://{self._streamable_host}:{self._streamable_port}"
                "/api/v1/mcp/streamable"
            )
        logger.info(
            "%s: bridge API started on %s:%s%s",
            self.name, self._host, self._port,
            (
                f" + streamable HTTP MCP on {self._streamable_host}:{self._streamable_port}"
                if bridge_config.streamable_enabled
                else ""
            ),
        )
        return _success_result(data=result_data)

    @platform_process(
        name="stop_interface",
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={},
        output_type="object",
        output_description="Bridge API shutdown confirmation.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Shutdown confirmation.",
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING, required=True,
                ),
            },
        ),
    )
    def stop_interface(
        self,
        params: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> dict[str, Any]:
        self._shutdown_server()
        return _success_result(data={"status": "stopped"})

    def _is_full_surface_ready(self) -> bool:
        """Are both uvicorn listeners bound and ready to serve?

        Used by ``/api/v1/bridge/health`` to gate the 200-vs-503 answer
        when streamable transport is enabled. The bridge uvicorn signals
        readiness via ``_server_started_event``; the streamable uvicorn
        signals via ``_streamable_server_started_event``. Cloud ALB
        target-group health checks probe ``/api/v1/bridge/health`` on
        the streamable port (9000); honest readiness means the smoke /
        connector that uses health=200 as the OAuth-ready gate doesn't
        race against an in-flight ``_start_streamable_server`` (see
        ``workbench/2026-06-12_aws_swap_smoke_run_report.md`` §3 Bug 2,
        iter 9 — vince's slow boot exposed the race for the first time).

        Only wired in when ``streamable_enabled=True``; local dev mode
        keeps the unconditional-200 contract because no streamable
        listener exists to race against.
        """
        return (
            self._server_started_event.is_set()
            and self._streamable_server_started_event.is_set()
        )

    # ------------------------------------------------------------------
    # IO interface — post_message
    # ------------------------------------------------------------------

    @platform_process(
        name="post_message",
        context_handling=ContextHandling.SESSION_AWARE,
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={
            "message": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="Message body to deliver to the bound MCP session.",
            ),
        },
        output_type="object",
        output_description="Message queueing confirmation.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.DICT,
            description="Contains message queueing confirmation.",
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="'queued' on success.",
                ),
            },
        ),
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=True,
        ),
        requires_result_processor=False,
    )
    def post_message(
        self, params: dict[str, Any], state: dict[str, Any],
    ) -> dict[str, Any]:
        # Text-only channel — silently strip attachment hints.
        params.pop("attachments", None)
        params.pop("job_result_ref", None)
        session_id = state.get("session_id") or params.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return _failure_result(
                code=_ERR_SESSION_NOT_BOUND,
                message="session_id missing from state",
            )
        manager = self._bridge_manager
        if manager is None:
            return _failure_result(
                code=_ERR_SESSION_NOT_BOUND,
                message="bridge interface is not started",
            )
        bridge = _find_bridge_by_session(manager, session_id)
        if bridge is None:
            return _failure_result(
                code=_ERR_SESSION_NOT_BOUND,
                message=f"No active bridge for session {session_id}",
            )
        message = _extract_message(params)
        if len(message) > self._max_message_chars:
            return _failure_result(
                code=_ERR_VALIDATION,
                message=f"Message exceeds {self._max_message_chars} char limit",
            )
        try:
            manager.append_event(
                bridge.bridge_id,
                "post_message",
                message,
                meta={
                    "flow_id": state.get("flow_id"),
                    "session_id": session_id,
                },
            )
        except BridgeNotFoundError:
            return _failure_result(
                code=_ERR_SESSION_NOT_BOUND,
                message=f"Bridge {bridge.bridge_id} is closed or missing",
            )
        except BridgeQueueFullError:
            return _failure_result(
                code="APIError",
                message="Bridge event queue is full",
            )
        if self._memory_service is not None:
            self._memory_service.store_interaction(
                session_id=session_id,
                source_namespace=PLUGIN_NAME,
                event_type="assistant_response",
                content=message,
                metadata={"source_namespace": PLUGIN_NAME},
            )
        return _success_result(data={"status": "queued"})

    # ------------------------------------------------------------------
    # Bridge delivery — deliver_result / deliver_error EDGE_SINK pair
    # ------------------------------------------------------------------

    @platform_process(
        name="deliver_result",
        context_handling=ContextHandling.SESSION_AWARE,
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={
            "result_payload": ParameterMetadata(
                type=ParameterType.DICT, required=True,
                description=(
                    "Raw structured result payload to deliver to the "
                    "originating bridge channel."
                ),
            ),
            "source_process_key": ParameterMetadata(
                type=ParameterType.STRING, required=True,
                description=(
                    "Process key of the action whose result is being "
                    "delivered (informational; surfaced to the MCP caller)."
                ),
            ),
            "bridge_id": ParameterMetadata(
                type=ParameterType.STRING, required=True,
                description="ID of the originating bridge session.",
            ),
        },
        output_type="object",
        output_description="Delivery confirmation.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.DICT,
            description="Contains delivery confirmation.",
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="'queued' on success.",
                ),
            },
        ),
        requires_result_processor=False,
    )
    def deliver_result(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        return self._deliver_payload(
            event_type="bridge_delivery_result",
            payload_key="result_payload",
            params=params,
            state=state,
        )

    @platform_process(
        name="deliver_error",
        context_handling=ContextHandling.SESSION_AWARE,
        processor_policy_category=ProcessorPolicyCategory.EDGE_SINK,
        parameters={
            "error_payload": ParameterMetadata(
                type=ParameterType.DICT, required=True,
                description=(
                    "Raw structured error payload to deliver to the "
                    "originating bridge channel."
                ),
            ),
            "source_process_key": ParameterMetadata(
                type=ParameterType.STRING, required=True,
                description=(
                    "Process key of the action whose failure is being "
                    "delivered."
                ),
            ),
            "bridge_id": ParameterMetadata(
                type=ParameterType.STRING, required=True,
                description="ID of the originating bridge session.",
            ),
        },
        output_type="object",
        output_description="Delivery confirmation.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.DICT,
            description="Contains delivery confirmation.",
            properties={
                "status": ParameterMetadata(
                    type=ParameterType.STRING,
                    description="'queued' on success.",
                ),
            },
        ),
        requires_result_processor=False,
    )
    def deliver_error(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        return self._deliver_payload(
            event_type="bridge_delivery_error",
            payload_key="error_payload",
            params=params,
            state=state,
        )

    # ------------------------------------------------------------------
    # run_turn — durable agent-messaging EDGE
    # ------------------------------------------------------------------

    @platform_process(
        name="run_turn",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        error_processor_customizations=MergeErrorProcessorCustomizations(
            retryable=False,
        ),
        parameters={
            "thread_id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="Agent thread the originator message belongs to.",
            ),
            "message_id": ParameterMetadata(
                type=ParameterType.STRING,
                required=True,
                description="Originator message id triggering this turn.",
            ),
        },
        output_type="object",
        output_description=(
            "Structured turn result; bridge-delivered to the originating "
            "agent_channel bridge."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description=(
                "Per-turn result payload — same shape on success, "
                "interruption, and error.  Discriminated by 'status'."
            ),
            properties={
                "thread_id": ParameterMetadata(
                    type=ParameterType.STRING, required=True,
                    description="Thread the turn belongs to.",
                ),
                "request_message_id": ParameterMetadata(
                    type=ParameterType.STRING, required=True,
                    description="Originator message that triggered the turn.",
                ),
                "response_message_id": ParameterMetadata(
                    type=ParameterType.STRING, required=False,
                    description=(
                        "Agent message id (success/interruption); null on "
                        "error paths where no agent row was persisted."
                    ),
                ),
                "status": ParameterMetadata(
                    type=ParameterType.STRING, required=True,
                    description="One of 'idle', 'interrupted', 'error'.",
                ),
                "backend": ParameterMetadata(
                    type=ParameterType.STRING, required=False,
                    description=(
                        "Resolved backend name; may be null if the failure "
                        "happened before backend resolution."
                    ),
                ),
                "backend_session_id": ParameterMetadata(
                    type=ParameterType.STRING, required=False,
                    description="Backend's internal session id, if any.",
                ),
                "text": ParameterMetadata(
                    type=ParameterType.STRING, required=True,
                    description=(
                        "Backend response text (empty string on error)."
                    ),
                ),
                "interrupted": ParameterMetadata(
                    type=ParameterType.BOOLEAN, required=True,
                    description="True iff backend reported interrupted=True.",
                ),
                "interrupted_on": ParameterMetadata(
                    type=ParameterType.STRING, required=False,
                    description=(
                        "Reason backend reported (e.g. 'timeout', "
                        "'watch_phrase'); null when not interrupted."
                    ),
                ),
                "artifacts": ParameterMetadata(
                    type=ParameterType.LIST, required=True,
                    description="Blob-ref artifact list; [] on error.",
                ),
                "metrics": ParameterMetadata(
                    type=ParameterType.OBJECT, required=True,
                    description="Backend metrics dict; {} on error.",
                ),
                "error": ParameterMetadata(
                    type=ParameterType.OBJECT, required=False,
                    description=(
                        "Structured failure info {code, message} when "
                        "status='error'; null on success/interruption."
                    ),
                ),
            },
        ),
    )
    def run_turn(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> dict[str, Any]:
        thread_id = str(params.get("thread_id") or "").strip()
        message_id = str(params.get("message_id") or "").strip()
        if not thread_id or not message_id:
            return _failure_result(
                code="agent_messaging.run_turn.missing_arguments",
                message="run_turn requires both thread_id and message_id",
            )
        # ``execute_turn`` always returns a structured payload (success
        # or failure shape, distinguished by ``payload.status``).  We
        # always return a *successful* ActionResult here so the bridge
        # dispatcher routes through ``deliver_result`` — the platform's
        # ``deliver_error`` builder strips most of the structured
        # context (thread_id / request_message_id / stable code), so
        # we keep failures inside the success channel.
        try:
            payload = self._require_service().execute_turn(
                thread_id=thread_id, message_id=message_id,
            )
        except Exception as exc:  # noqa: BLE001 — last-resort safety
            return _failure_result(
                code="agent_messaging.run_turn.unhandled",
                message=f"unhandled execute_turn failure: {exc}",
            )
        return _success_result(data=payload)

    # ------------------------------------------------------------------
    # NativeWakeAdapter — claude_code self-wake
    # ------------------------------------------------------------------

    def wake(
        self,
        *,
        recipient_parent_pid: int | None,
        delivered_prose: str,
        sender_agent_id: str,
        sender_agent_instance_id: str,
        sender_session_label: str,
        thread_id: str,
        message_id: str,
        reply_to_role: str = "",
        delivery_meta: Mapping[str, object] | None = None,
    ) -> str:
        """Implement NativeWakeAdapter for agent_id='claude_code'.

        Pairs the recipient's bridge by ``parent_pid`` and appends a
        post_message event carrying an envelope that embeds the sender's
        identity (native channel is text-only — no meta field rides
        along, so the receiver reconstructs targeted-reply args from the
        prose).  ``delivery_meta`` (v10 Control #5 / Q3-revised) is merged onto
        the bridge-event meta — for a role send it carries the role keys so the
        holder's forwarder confirms delivery (this wake is the SAME bridge queue
        as queued_notification, NOT a direct push). Raises on failure — the loop-prevention
        contract treats IMPORTANT delivery as a hard promise, so silent drops
        are not acceptable.
        """
        manager = self._bridge_manager
        registry = self._peer_registry
        if manager is None or registry is None:
            raise RuntimeError(
                "claude_code wake: bridge interface is not started",
            )
        if recipient_parent_pid is None:
            raise RuntimeError(
                "claude_code wake requires recipient parent_pid; the "
                "recipient bridge has no parent_pid (older client build)",
            )
        bridge = _find_claude_code_bridge_by_parent_pid(
            manager=manager,
            peer_registry=registry,
            parent_pid=recipient_parent_pid,
        )
        if bridge is None:
            raise RuntimeError(
                f"no open claude_code bridge with "
                f"parent_pid={recipient_parent_pid}",
            )
        label_segment = (
            f' "{sender_session_label}"' if sender_session_label else ""
        )
        reply_hint = build_wake_reply_hint(
            reply_to_role=reply_to_role,
            sender_agent_id=sender_agent_id,
            sender_agent_instance_id=sender_agent_instance_id,
            thread_id=thread_id,
            message_id=message_id,
        )
        envelope = (
            f"[peer:{sender_agent_id}{label_segment} "
            f"instance={sender_agent_instance_id}] "
            f"{delivered_prose}\n\n"
            f"{reply_hint}"
        )
        # Synthetic flow_id keeps the bridge subprocess emitting a
        # non-empty meta.flow_id on the resulting channel notification.
        meta: dict[str, object] = {
            "flow_id": f"peer-wake-{message_id}",
            "thread_id": thread_id,
            "message_id": message_id,
        }
        # v10 Control #5: a role send merges its role keys so the holder's
        # forwarder recognises the role delivery on /events and confirms it.
        if delivery_meta:
            meta.update(delivery_meta)
        manager.append_event(bridge.bridge_id, "post_message", envelope, meta=meta)
        manager.touch(bridge.bridge_id)
        return bridge.bridge_id

    # ------------------------------------------------------------------
    # Internals — service construction
    # ------------------------------------------------------------------

    def _require_service(self) -> AgentMessagingService:
        if self._service is None:
            self._service = self._build_service()
        return self._service

    def _build_service(self) -> AgentMessagingService:
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref not injected",
            )
        state_service = orchestrator.get_service("state_service")
        if state_service is None:
            raise RuntimeError(
                f"{self.name}: state_service unavailable",
            )
        flow_manager = self._flow_manager or orchestrator.get_service("flow_service")
        if flow_manager is None:
            raise RuntimeError(
                f"{self.name}: flow_service unavailable; cannot build service yet",
            )
        # PluginBase.set_action_factory stores into self.action_factory.
        action_factory = getattr(self, "action_factory", None)
        if action_factory is None:
            action_factory = getattr(orchestrator, "action_factory", None)
        if action_factory is None:
            raise RuntimeError(
                f"{self.name}: action_factory unavailable",
            )
        compilation_context_builder = (
            self._compilation_context_builder
            or getattr(orchestrator, "compilation_context_builder", None)
        )
        if compilation_context_builder is None:
            raise RuntimeError(
                f"{self.name}: compilation_context_builder unavailable",
            )
        plugin_manager = getattr(orchestrator, "plugin_manager", None)
        if plugin_manager is None:
            raise RuntimeError(
                f"{self.name}: plugin_manager unavailable",
            )
        config = self._build_config()
        repository = AgentMessagingRepository(state_service)
        router = make_router(plugin_manager)
        bridge = _BridgeDeliveryEndpoint(
            plugin_namespace=PLUGIN_NAME,
            deliver_result_process_key=_DELIVER_RESULT_PROCESS_KEY,
            deliver_error_process_key=_DELIVER_ERROR_PROCESS_KEY,
        )
        logger.info(
            "%s service constructed (allowed_backends=%s, max_message_bytes=%d)",
            self.name,
            list(config.allowed_backends),
            config.max_message_bytes,
        )
        return AgentMessagingService(
            repository=repository,
            state_service=state_service,
            backend_router=router,
            flow_manager=flow_manager,
            action_factory=action_factory,
            compilation_context_builder=compilation_context_builder,
            bridge_delivery=bridge,
            config=config,
        )

    def _build_platform_surface(
        self,
        *,
        bridge_manager: BridgeSessionManager,
        bridge_config: _BridgeRuntimeConfig,
    ) -> PlatformSurface:
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            raise RuntimeError(
                f"{self.name}: orchestrator_ref not injected",
            )
        action_factory = getattr(self, "action_factory", None) or getattr(
            orchestrator, "action_factory", None,
        )
        flow_manager = self._flow_manager or orchestrator.get_service(
            "flow_service",
        )
        compilation_context_builder = (
            self._compilation_context_builder
            or getattr(orchestrator, "compilation_context_builder", None)
        )
        process_registry = self._resolve_process_registry(orchestrator)
        export_policy = self._build_export_policy()
        return PlatformSurface(
            action_factory=action_factory,
            flow_manager=flow_manager,
            compilation_context_builder=compilation_context_builder,
            bridge_manager=bridge_manager,
            process_registry=process_registry,
            discovery_service=orchestrator.get_service("discovery_service"),
            state_service=orchestrator.get_service("state_service"),
            blob_storage_service=orchestrator.get_service(
                "blob_storage_service",
            ),
            memory_service=self._memory_service,
            plugin_manager=getattr(orchestrator, "plugin_manager", None),
            export_policy=export_policy,
            max_message_chars=bridge_config.max_message_chars,
        )

    @staticmethod
    def _resolve_process_registry(
        orchestrator: object,
    ) -> dict[str, object] | None:
        get_registry = getattr(orchestrator, "get_process_registry", None)
        if not callable(get_registry):
            return None
        registry = get_registry()
        return registry if isinstance(registry, dict) else None

    def _build_export_policy(self) -> ProcessExportPolicy:
        provider = getattr(self, "config_provider", None)
        if provider is None:
            self._populate_config_provider_from_orchestrator()
            provider = getattr(self, "config_provider", None)
        enabled = _as_bool(
            _provider_get(provider, "process_export_enabled"), True,
        )
        allow = _as_str_tuple(
            _provider_get(provider, "process_export_allow_patterns"),
            default=(),
        )
        deny = _as_str_tuple(
            _provider_get(provider, "process_export_deny_patterns"),
            default=(),
        )
        promote = _as_str_tuple(
            _provider_get(provider, "process_export_promote_patterns"),
            default=(),
        )
        max_promoted = _as_int(
            _provider_get(provider, "process_export_max_promoted_tools"),
            40,
        )
        return ProcessExportPolicy(
            enabled=enabled,
            allow_patterns=allow,
            deny_patterns=deny,
            promote_patterns=promote,
            max_promoted_tools=max_promoted,
        )

    def _mint_session_id(self, homunculus_name: str) -> str:  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
        if self._session_manager is None:
            raise RuntimeError(
                f"{self.name}: session_manager not injected; "
                "cannot mint session_id for bridge open",
            )
        session_id = self._session_manager.create_session(
            namespace=PLUGIN_NAME,
            context_type="bridge",
        )
        return session_id

    # ------------------------------------------------------------------
    # Internals — bridge-delivery shared body
    # ------------------------------------------------------------------

    def _deliver_payload(
        self,
        *,
        event_type: str,
        payload_key: str,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        manager = self._bridge_manager
        if manager is None:
            return _failure_result(
                code=_ERR_NO_ACTIVE_BRIDGE,
                message="bridge interface is not started",
            )
        bridge_id = str(params.get("bridge_id") or "")
        if not bridge_id:
            return _failure_result(
                code=_ERR_NO_ACTIVE_BRIDGE,
                message="bridge_id missing from delivery params",
            )
        payload = params.get(payload_key)
        if not isinstance(payload, dict):
            return _failure_result(
                code=_ERR_PROCESS_CALL_FAILED,
                message=(
                    f"{payload_key} must be a dict; got {type(payload).__name__}"
                ),
            )
        # ``QueuedEvent.content`` is a string field; structured payloads
        # are JSON-serialized so the MCP client can decode the
        # event_type-discriminated body on receipt.
        import json  # noqa: PLC0415 — keep heavy imports local to the path
        content_json = json.dumps(
            {
                "payload": dict(payload),
                "source_process_key": str(
                    params.get("source_process_key") or "",
                ),
            },
            default=str,
        )
        try:
            manager.append_event(
                bridge_id,
                event_type,
                content_json,
                meta={
                    "flow_id": state.get("flow_id"),
                    "session_id": state.get("session_id"),
                    "source_process_key": str(
                        params.get("source_process_key") or "",
                    ),
                },
            )
        except BridgeNotFoundError:
            return _failure_result(
                code=_ERR_NO_ACTIVE_BRIDGE,
                message=f"Bridge {bridge_id} is closed or missing",
            )
        except BridgeQueueFullError:
            return _failure_result(
                code=_ERR_QUEUE_FULL,
                message="Bridge event queue is full",
            )
        return _success_result(data={"status": "queued"})

    # ------------------------------------------------------------------
    # Internals — FastAPI server lifecycle
    # ------------------------------------------------------------------

    def _build_fastapi_app(
        self,
        *,
        bridge_manager: BridgeSessionManager,
        peer_registry: PeerRegistry,
        platform_surface: PlatformSurface,
        bridge_config: _BridgeRuntimeConfig,
    ) -> FastAPI:
        from fastapi import FastAPI  # noqa: PLC0415
        app = FastAPI(
            title="Homunculus Bridge API",
            version="1.0.0",
        )
        # REL-05: stamp last_model_activity_at on every MODEL-INITIATED bridge
        # route (never forwarder/infra — F1 keeps peer/register out) so the
        # consumption reconciler can tell a session that entered a turn from a
        # deaf one whose forwarder merely keeps polling.
        app.middleware("http")(
            make_model_activity_middleware(bridge_manager, peer_registry),
        )
        register_routes(
            app,
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
            platform_surface=platform_surface,
            agent_messaging_service=self._require_service(),
            config=bridge_config,
            state_service=self._get_state_service(),
            readiness_probe=(
                self._is_full_surface_ready
                if bridge_config.streamable_enabled
                else None
            ),
            # D-IF7 / D-IF8 sidecar wiring (v4 §4-5). Streamable transport
            # paths (mcp_streamable/{session,dispatch}.py) DO NOT receive
            # these callbacks per D-IF11 scope-out — streamable peers
            # fall back to default_inference_plugin via the wrapper's
            # None-handling path.
            inference_provider_register=self._register_inference_provider,
            inference_provider_clear=self._clear_inference_providers_for_bridge,
            # INF-01 sub-slice-2 (seam §b): Trigger-1 vacancy-fill/crash-heal
            # + Trigger-2 grace-delayed succession hook bodies. Streamable
            # paths stay scoped out with the sidecar (D-IF11).
            autonomic_on_register=(
                self._autonomic_assignment.on_register
                if self._autonomic_assignment is not None else None
            ),
            autonomic_on_close=(
                self._autonomic_assignment.on_bridge_close
                if self._autonomic_assignment is not None else None
            ),
        )
        # M5 §13.6 ONE-TIME EXCEPTION to the no-edits-to-god-file-plugins
        # Boy Scout rule: mount the session-ledger pairing routes umbrella
        # exported by session_shipper_bootstrap_plugin. ImportError fallback
        # is the explicit profile contract per §13.6 — profiles that don't
        # load the bootstrap plugin (e.g., minimal cloud test harnesses) skip
        # the mount entirely.
        try:
            from session_shipper_bootstrap_plugin.pairing_routes import (  # noqa: PLC0415
                make_pairing_ledger_facade,
                register_session_ledger_pairing_routes,
            )
        except ImportError:
            logger.info(
                "session_shipper_bootstrap_plugin not installed; skipping "
                "session-ledger pairing route mount (spec §13.6 profile contract)",
            )
            return app
        ledger_service = self._maybe_get_session_ledger_service()
        vault_registry = self._maybe_get_vault_oauth_registry()
        if ledger_service is None or vault_registry is None:
            logger.info(
                "session_ledger_service or vault_oauth_registry unavailable; "
                "skipping pairing route mount (will reattach next startup if both land)",
            )
            return app
        facade = make_pairing_ledger_facade(
            session_ledger_service=ledger_service,
            vault_oauth_registry=vault_registry,
        )
        register_session_ledger_pairing_routes(app, ledger=facade)
        # M5 §13.3: wire the service's operator-equivalent check to the
        # vault's is_operator_equivalent. This unlocks the §13.3 second
        # ownership-binding branch (operator_equivalent clients can
        # approve any pending deployment in addition to the initiator).
        ledger_service.set_operator_equivalent_check(
            vault_registry.is_operator_equivalent,
        )
        # M4 chatgpt-export + M9 claude_ai-export HTTP upload routes were
        # retired 2026-06-15 per the unified URL-walker design v3 §3.
        # The replacement is the per-plugin ``ingest_export`` EDGE verb
        # on chatgpt_export_session_source_plugin + claude_ai_export_session_source_plugin
        # invoked via ``process_call``. The session_shipper pairing routes
        # (different module, same function name) stay mounted above.
        return app

    def _oauth_client_is_operator_equivalent(self, client_id: str) -> bool:
        vault_registry = self._maybe_get_vault_oauth_registry()
        if vault_registry is None:
            return False
        try:
            return bool(vault_registry.is_operator_equivalent(client_id))
        except Exception:  # noqa: BLE001
            logger.exception(
                "policy resolver: is_operator_equivalent threw for client_id=%s",
                client_id,
            )
            return False

    def _oauth_client_is_management_client(self, client_id: str) -> bool:
        management_client_ids = set(
            self._build_bridge_runtime_config().oauth_management_client_ids,
        )
        return client_id in management_client_ids

    def _oauth_client_is_paired_shipper(self, client_id: str) -> bool:
        ledger = self._maybe_get_session_ledger_service()
        if ledger is None:
            return False
        try:
            repo = getattr(ledger, "_repository", None)
            if repo is None:
                return False
            return repo.get_deployment_by_oauth_client_id(client_id) is not None
        except Exception:  # noqa: BLE001
            logger.exception(
                "policy resolver: shipper deployment lookup threw for client_id=%s",
                client_id,
            )
            return False

    def _resolve_oauth_session_policy(self, claim: Any) -> tuple[str, ...]:
        """M5 §14.4 policy resolver. Closure captured by BridgeSessionManager.

        Returns one of:
        * ``_UNRESTRICTED`` — caller is operator_equivalent in the vault.
        * ``MANAGEMENT_ALLOWLIST`` — caller is a configured operator
          management client (for example ChatGPT over the secure MCP tunnel).
        * ``SHIPPER_ALLOWLIST`` — caller is a paired shipper (deployment
          row with matching oauth_client_id in pairing_status='paired').
        * ``EMPTY_ALLOWLIST`` — neither (fail-closed).

        Late binding: vault + session_ledger_service may not be live at
        BridgeSessionManager construction time (bridge starts via
        ``start_interface`` action, after the orchestrator wires the
        services). The closure looks them up at open-bridge time.
        """
        from .bridge_sessions import (  # noqa: PLC0415
            _UNRESTRICTED,
            EMPTY_ALLOWLIST,
            MANAGEMENT_ALLOWLIST,
            SHIPPER_ALLOWLIST,
        )

        client_id = getattr(claim, "client_id", "") or ""
        if not client_id:
            # Stdio bridges land here with empty client_id; the surface
            # short-circuits the policy check anyway (M5.B hot-fix), but
            # fail-closed at the resolver level is correct.
            return EMPTY_ALLOWLIST
        if self._oauth_client_is_operator_equivalent(client_id):
            return _UNRESTRICTED
        if self._oauth_client_is_management_client(client_id):
            return MANAGEMENT_ALLOWLIST
        if self._oauth_client_is_paired_shipper(client_id):
            return SHIPPER_ALLOWLIST
        return EMPTY_ALLOWLIST

    def _oauth_client_exists(self, client_id: str) -> bool:
        """M5 §14.3 BearerVerifier cross-check. True iff client is in the vault."""
        vault_registry = self._maybe_get_vault_oauth_registry()
        if vault_registry is None:
            # No registry means we can't validate; fail-closed by saying
            # the client doesn't exist. (Production: vault is always
            # bound before the bridge starts, so this path is dead.)
            return False
        try:
            return vault_registry.lookup_client(client_id) is not None
        except Exception:  # noqa: BLE001
            logger.exception(
                "client_exists_check: lookup_client threw for client_id=%s",
                client_id,
            )
            return False

    def _maybe_get_session_ledger_service(self) -> Any | None:
        """Reach the live SessionLedgerService via the orchestrator.

        Returns None if either the orchestrator_ref isn't injected yet
        or the ledger service hasn't been initialized (M1 ledger
        wiring runs in startup_sequence; bridge starts AFTER startup
        per starting_actions ordering, so this should always resolve
        in production).
        """
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            return None
        get_service = getattr(orchestrator, "get_service", None)
        if get_service is None:
            return None
        return get_service("session_ledger_service")

    def _maybe_get_blob_storage_service(self) -> Any | None:
        """Reach the live blob_storage_service via the orchestrator.

        Required by the M4 chatgpt_export + M9 claude_ai_export upload
        route facades — both call ``blob_storage_service.store_blob(...)``
        to persist the uploaded ZIP before registering a ledger source row.
        Same shape as :meth:`_maybe_get_session_ledger_service`; returns
        None when the orchestrator is not yet injected or the service
        binding is unbound (e.g., minimal cloud test harnesses without
        a blob-storage plugin loaded).
        """
        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            return None
        get_service = getattr(orchestrator, "get_service", None)
        if get_service is None:
            return None
        return get_service("blob_storage_service")

    def _make_upload_route_auth(
        self, bridge_config: _BridgeRuntimeConfig,
    ) -> _UploadRouteAuth:
        """Build the AuthCheckProtocol callable for M4/M9 upload routes.

        The wiring follows the streamable transport's pattern so the same
        operator-opt-in outer-boundary contract applies to the upload
        surface as to the MCP streamable surface:

        * ``streamable_enabled=False`` (no streamable listener exists) OR
          ``streamable_no_auth=True`` (operator-approved outer boundary) →
          no-op auth callback (returns None for any header). Matches the
          :class:`PermissiveBearerVerifier` semantics used inside
          :meth:`_mount_streamable_transport`; local-dev curl works.
        * Otherwise → production gate per M5 §13.6 docstring on the
          chatgpt routes module (``BearerVerifier + operator_equivalent``):
          verify the bearer token via the cached
          :class:`BearerVerifier`, then assert the claim's ``client_id``
          is flagged operator-equivalent in the vault OAuth registry.

        The closure resolves both dependencies lazily because
        ``_build_fastapi_app`` runs BEFORE ``_mount_streamable_transport``;
        the verifier is constructed and cached on ``self`` later in
        ``start_interface``. By the time a request actually hits an
        upload route, both are wired.

        Raises ``PermissionError`` on any failure — the upload-route
        handler catches every exception and maps it to HTTP 401.
        """
        if (
            not bridge_config.streamable_enabled
            or bridge_config.streamable_no_auth
        ):
            def _no_op(authorization_header: str | None) -> object:  # noqa: ARG001  # pyright: ignore[reportUnusedParameter]
                return None
            return _no_op

        def _verify(authorization_header: str | None) -> object:
            verifier = self._streamable_bearer_verifier
            vault_registry = self._maybe_get_vault_oauth_registry()
            if verifier is None or vault_registry is None:
                raise PermissionError(
                    "upload-route auth unavailable: bearer verifier or "
                    "vault OAuth registry not bound at request time",
                )
            claim = verifier.verify(authorization_header)
            client_id = getattr(claim, "client_id", "")
            if not vault_registry.is_operator_equivalent(client_id):
                raise PermissionError(
                    f"client_id {client_id!r} is not operator-equivalent; "
                    "upload routes require an operator-equivalent bearer "
                    "per M5 §13.6",
                )
            return claim

        return _verify

    def _maybe_get_vault_oauth_registry(self) -> Any | None:
        """Pull the vault plugin's VaultOAuthRegistry via the injected proxy.

        W-VAULT-INTERFACE-EXTEND Phase D-2: vault is no longer fetched
        via ``orchestrator.get_service`` — the injected
        ``VaultServiceProxy`` exposes ``_oauth_registry`` as a
        transitional property (removal target W-OAUTH-EXTRACT). Returns
        None when no vault is bound (e.g., mock-vault test profiles).
        """
        vault = self._vault_service
        if vault is None:
            return None
        return getattr(vault, "_oauth_registry", None)

    def _run_server(self) -> None:
        import uvicorn  # noqa: PLC0415
        app = self._app
        if app is None:
            logger.error("%s: FastAPI app not constructed", self.name)
            return
        self._server_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._server_loop)
        cfg = uvicorn.Config(
            app,
            host=self._host or "127.0.0.1",
            port=self._port or 0,
            log_level="warning",
            loop="asyncio",
        )
        server = uvicorn.Server(cfg)
        self._server_started_event.set()
        try:
            self._server_loop.run_until_complete(server.serve())
        except Exception:
            logger.exception("%s: bridge API server crashed", self.name)
        finally:
            self._server_loop.close()

    def _shutdown_server(self) -> None:
        # Stop the REL-09 idle sweeper before its collaborators go away.
        if self._bridge_sweeper is not None:
            self._bridge_sweeper.stop()
            self._bridge_sweeper = None
        # Cancel outstanding sys:autonomic grace timers before the bridge
        # collaborators they close over are torn down.
        if self._autonomic_assignment is not None:
            self._autonomic_assignment.cancel_all()
            self._autonomic_assignment = None
        # Tear streamable down first so SSE streams unblock before the
        # underlying BridgeSessionManager goes away.
        self._teardown_streamable_server()
        self._teardown_bridge_server()
        self._app = None
        self._bridge_manager = None
        self._peer_registry = None
        self._platform_surface = None

    def _teardown_streamable_server(self) -> None:
        if self._streamable_session_manager is not None:
            self._streamable_session_manager.close_all()
            self._streamable_session_manager = None
        streamable_loop = self._streamable_server_loop
        if streamable_loop is not None and streamable_loop.is_running():
            streamable_loop.call_soon_threadsafe(streamable_loop.stop)
        streamable_thread = self._streamable_server_thread
        if streamable_thread is not None and streamable_thread.is_alive():
            streamable_thread.join(timeout=_SERVER_JOIN_TIMEOUT_S)
        self._streamable_server_thread = None
        self._streamable_server_loop = None
        self._streamable_host = None
        self._streamable_port = None

    def _teardown_bridge_server(self) -> None:
        loop = self._server_loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(loop.stop)
        thread = self._server_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=_SERVER_JOIN_TIMEOUT_S)
        self._server_thread = None
        self._server_loop = None
        self._port = None

    # ------------------------------------------------------------------
    # Internals — config provider plumbing
    # ------------------------------------------------------------------

    def _populate_config_provider_from_orchestrator(self) -> None:
        """Re-bind the config provider against the live orchestrator.

        Plugin discovery may rebuild the plugins dict, leaving the
        active instance distinct from the one that received
        ``initialize(config)``.
        """
        from ananta.core.config.config_provider import ConfigProvider  # noqa: PLC0415

        orchestrator = getattr(self, "orchestrator_ref", None)
        if orchestrator is None:
            return
        config_manager = getattr(orchestrator, "config", None)
        if config_manager is None or not hasattr(
            config_manager, "get_plugin_config_provider",
        ):
            return
        provider = config_manager.get_plugin_config_provider(self.name)
        if isinstance(provider, ConfigProvider):
            self.config_provider = provider

    def _build_config(self) -> AgentMessagingConfig:
        provider = self._resolve_config_provider()
        return AgentMessagingConfig(
            enabled=_as_bool(_provider_get(provider, "enabled"), True),
            allowed_backends=_as_str_tuple(
                _provider_get(provider, "allowed_backends"),
                default=("codex", "claude_code"),
            ),
            allowed_working_directory_roots=_as_str_tuple(
                _provider_get(provider, "allowed_working_directory_roots"),
                default=(),
            ),
            max_message_bytes=_as_int(
                _provider_get(provider, "max_message_bytes"), 65_536,
            ),
            max_thread_messages=_as_int(
                _provider_get(provider, "max_thread_messages"), 1_000,
            ),
            default_timeout_seconds=_as_int(
                _provider_get(provider, "default_timeout_seconds"), 600,
            ),
            max_timeout_seconds=_as_int(
                _provider_get(provider, "max_timeout_seconds"), 1_800,
            ),
        )

    def _build_bridge_runtime_config(self) -> _BridgeRuntimeConfig:
        provider = self._resolve_config_provider()
        return _BridgeRuntimeConfig(
            host=_as_str(
                _provider_get(provider, "host"), "127.0.0.1",
            ),
            port=_as_optional_int(_provider_get(provider, "port")),
            long_poll_timeout_seconds=_as_int(
                _provider_get(provider, "long_poll_timeout_seconds"), 25,
            ),
            bridge_idle_timeout_seconds=_as_int(
                _provider_get(provider, "bridge_idle_timeout_seconds"),
                3_600,
            ),
            max_pending_events=_as_int(
                _provider_get(provider, "max_pending_events"), 200,
            ),
            max_message_chars=_as_int(
                _provider_get(provider, "max_message_chars"),
                _DEFAULT_MAX_MESSAGE_CHARS,
            ),
            autonomic_grace_seconds=_as_int(
                _provider_get(provider, "autonomic_grace_seconds"), 120,
            ),
            bridge_sweep_interval_seconds=_as_int(
                _provider_get(provider, "bridge_sweep_interval_seconds"), 300,
            ),
            completion_serve_window_seconds=_as_int(
                _provider_get(provider, "completion_serve_window_seconds"),
                900,
            ),
            forward_serve_window_seconds=_as_int(
                _provider_get(provider, "forward_serve_window_seconds"), 900,
            ),
            forward_attempts_cap=_as_int(
                _provider_get(provider, "forward_attempts_cap"), 5,
            ),
            terminal_gc_after_seconds=_as_int(
                _provider_get(provider, "terminal_gc_after_seconds"), 172_800,
            ),
            re_emit_window_seconds=_as_int(
                _provider_get(provider, "re_emit_window_seconds"), 300,
            ),
            re_emit_cap=_as_int(
                _provider_get(provider, "re_emit_cap"), 3,
            ),
            streamable_enabled=_as_bool(
                _provider_get(provider, "streamable_enabled"), False,
            ),
            streamable_host=_as_str(
                _provider_get(provider, "streamable_host"),  # noqa: S104
                "0.0.0.0",  # noqa: S104
            ),
            streamable_port=_as_int(
                _provider_get(provider, "streamable_port"), 9000,
            ),
            streamable_allowed_origins=_as_str_tuple(
                _provider_get(provider, "streamable_allowed_origins"),
                default=(),
            ),
            streamable_bearer_max_age_seconds=_as_int(
                _provider_get(provider, "streamable_bearer_max_age_seconds"),
                300,
            ),
            oauth_enabled=_as_bool(
                _provider_get(provider, "oauth_enabled"), False,
            ),
            oauth_issuer_url=_as_str(
                _provider_get(provider, "oauth_issuer_url"), "",
            ),
            oauth_resource_aliases=_as_str_tuple(
                _provider_get(provider, "oauth_resource_aliases"),
                default=(),
            ),
            oauth_management_client_ids=_as_str_tuple(
                _provider_get(provider, "oauth_management_client_ids"),
                default=(),
            ),
            oauth_token_ttl_seconds=_as_int(
                _provider_get(provider, "oauth_token_ttl_seconds"),
                DEFAULT_TOKEN_TTL_SECONDS,
            ),
            oauth_auth_code_ttl_seconds=_as_int(
                _provider_get(provider, "oauth_auth_code_ttl_seconds"),
                600,
            ),
            oauth_refresh_token_ttl_seconds=_as_int(
                _provider_get(provider, "oauth_refresh_token_ttl_seconds"),
                30 * 24 * 60 * 60,
            ),
            oauth_require_audience=_as_bool(
                _provider_get(provider, "oauth_require_audience"),
                True,
            ),
            oauth_refresh_tokens_enabled=_as_bool(
                _provider_get(provider, "oauth_refresh_tokens_enabled"),
                True,
            ),
            streamable_cors_origins=_as_str_tuple(
                _provider_get(provider, "streamable_cors_origins"),
                default=(),
            ),
            streamable_no_auth=_as_bool(
                _provider_get(provider, "streamable_no_auth"), False,
            ),
        )

    # ------------------------------------------------------------------
    # Internals — Streamable HTTP MCP transport
    # ------------------------------------------------------------------

    def _build_oauth_surface(
        self, bridge_config: _BridgeRuntimeConfig,
    ) -> tuple[OAuthEndpoints | None, str, tuple[str, ...]]:
        """Derive OAuth endpoints + resource-metadata URL + audiences.

        Returns ``(None, "", ())`` when OAuth is disabled or the
        issuer URL is unset; the bearer verifier and the streamable
        router both treat those as "OAuth not mounted".
        """
        if not (
            bridge_config.oauth_enabled and bridge_config.oauth_issuer_url
        ):
            return None, "", ()
        oauth_endpoints = build_endpoints(
            issuer=bridge_config.oauth_issuer_url,
            streamable_path=STREAMABLE_PATH,
        )
        resource_metadata_url = (
            oauth_endpoints.issuer
            + "/.well-known/oauth-protected-resource"
        )
        accepted_audiences: tuple[str, ...] = ()
        if bridge_config.oauth_require_audience:
            # The streamable router answers at both the primary path
            # and the alias; tokens whose ``aud`` claim matches either
            # canonical URI are accepted. /authorize + /token stamp
            # the primary URI; the alias exists for phone tokens
            # minted with --audience pointing at the alias path.
            accepted_audiences = (
                oauth_endpoints.resource,
                oauth_endpoints.issuer + STREAMABLE_ALIAS_PATH,
            )
        return oauth_endpoints, resource_metadata_url, accepted_audiences

    def _mount_streamable_transport(
        self,
        *,
        app: FastAPI,
        bridge_manager: BridgeSessionManager,
        peer_registry: PeerRegistry,
        platform_surface: PlatformSurface,
        bridge_config: _BridgeRuntimeConfig,
    ) -> None:
        """Register the Streamable HTTP router on ``app``.

        Owns the construction of the session manager + bearer
        verifier so the streamable router gets a fully-wired set of
        collaborators.  No-op if the vault plugin is unreachable —
        the streamable transport hard-fails on first request rather
        than silently degrading; the failure mode is clear from
        ``bearer.vault_unavailable`` in the error response body.
        """
        # An OAuth login surface is ALWAYS mounted now (static when an
        # issuer is pinned, dynamic origin-following otherwise — see
        # _mount_oauth_routers), and both variants offer refresh-token
        # rotation when it's enabled. So the vault must expose the
        # refresh-token methods whenever refresh tokens are enabled at
        # all, independent of streamable_no_auth.
        require_refresh = bridge_config.oauth_refresh_tokens_enabled
        vault = self._resolve_vault_plugin(
            require_refresh_token_methods=require_refresh,
        )
        oauth_endpoints, resource_metadata_url, accepted_audiences = (
            self._build_oauth_surface(bridge_config)
        )
        bearer_verifier, hmac_key = self._build_streamable_bearer_verifier(
            bridge_config=bridge_config,
            vault=vault,
            accepted_audiences=accepted_audiences,
        )
        # Cache the verifier so the M4/M9 upload-route auth closure can
        # share it (same audience binding, same client-exists check) per
        # the dispatch's lazy-resolver pattern.
        self._streamable_bearer_verifier = bearer_verifier
        # B1 Finding-B: wire the operator-equivalent propagation on the shared
        # platform surface — a VERIFIED operator_equivalent OAuth client keeps
        # operator authority (for_operator_equivalent) once the no-auth flip
        # lands. Reads the SAME vault ``is_operator_equivalent`` the policy
        # resolver + session-ledger use. Absent registry (mock-vault profiles)
        # → unwired → non-operator default (safe).
        oauth_registry = self._maybe_get_vault_oauth_registry()
        if oauth_registry is not None:
            platform_surface.set_operator_equivalent_check(
                oauth_registry.is_operator_equivalent,
            )
        session_manager = StreamableSessionManager(
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
        )
        self._streamable_session_manager = session_manager
        homunculus_name = _resolve_homunculus_name()
        router = build_streamable_router(
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
            platform_surface=platform_surface,
            agent_messaging_service=self._require_service(),
            state_service=self._get_state_service(),
            session_manager=session_manager,
            bearer_verifier=bearer_verifier,
            allowed_origins=bridge_config.streamable_allowed_origins,
            resource_metadata_url=resource_metadata_url,
            cors_origins=bridge_config.streamable_cors_origins,
            path_aliases=(STREAMABLE_ALIAS_PATH,),
            homunculus_name=homunculus_name,
        )
        app.include_router(router)
        self._mount_oauth_routers(
            app=app,
            bridge_config=bridge_config,
            vault=vault,
            hmac_key=hmac_key,
            oauth_endpoints=oauth_endpoints,
            accepted_audiences=accepted_audiences,
        )
        logger.info(
            "%s: Streamable HTTP MCP transport mounted at "
            "/api/v1/mcp/streamable (bearer max-age=%ds, dns-rebinding-origins=%s, "
            "cors-origins=%s, oauth=%s)",
            self.name,
            bridge_config.streamable_bearer_max_age_seconds,
            list(bridge_config.streamable_allowed_origins) or "any",
            list(bridge_config.streamable_cors_origins) or "none",
            "on" if bridge_config.oauth_enabled else "off",
        )

    def _build_streamable_bearer_verifier(
        self,
        *,
        bridge_config: _BridgeRuntimeConfig,
        vault: Any,
        accepted_audiences: tuple[str, ...],
    ) -> tuple[BearerVerifier, bytes]:
        """Build the bearer verifier + HMAC key for the streamable router.

        ``streamable_no_auth`` swaps in the permissive verifier (the
        outer boundary owns auth) but still provisions the HMAC key so
        the dynamic OAuth surface can mint tokens.
        """
        hmac_key = _load_or_create_bearer_hmac_key(vault)
        if bridge_config.streamable_no_auth:
            logger.warning(
                "streamable_no_auth=true: MCP streamable endpoint relies on "
                "an outer security boundary (tunnel-client + runtime API "
                "key, mTLS, or network isolation) for auth; per-request "
                "bearer enforcement is DISABLED. Ensure your outer boundary "
                "is active.",
            )
            return PermissiveBearerVerifier(), hmac_key
        verifier = BearerVerifier(
            hmac_key=hmac_key,
            max_age_seconds=bridge_config.streamable_bearer_max_age_seconds,
            accepted_audiences=accepted_audiences,
            # M5 §14.3: revoked-client cross-check via vault registry.
            # Lazy lookup keeps the verifier decoupled from vault wiring
            # ordering; bridge plugin already requires vault to construct
            # the verifier (HMAC key load earlier in this function), so
            # the registry is always available at first call time.
            client_exists_check=self._oauth_client_exists,
        )
        return verifier, hmac_key

    def _mount_oauth_routers(
        self,
        *,
        app: FastAPI,
        bridge_config: _BridgeRuntimeConfig,
        vault: Any,
        hmac_key: bytes,
        oauth_endpoints: OAuthEndpoints | None,
        accepted_audiences: tuple[str, ...],
    ) -> None:
        """Mount the OAuth 2.1 login surface — always exactly one variant.

        Static surface when a stable issuer is pinned
        (``oauth_endpoints`` present — cloud ALB); origin-following
        dynamic surface otherwise (local tunnel, ephemeral origin).

        This selection is INDEPENDENT of ``streamable_no_auth``: bearer
        *enforcement* (which verifier the streamable router uses) and the
        OAuth *login* surface (how a client obtains a bearer) are
        orthogonal concerns. A local tunnel deployment with enforcement
        ON still needs /authorize + /oauth/token so external clients
        (ChatGPT / claude.ai) can complete OAuth and mint a token the
        real verifier accepts. Gating the login surface on
        ``streamable_no_auth`` is what stranded the connector at a 404 on
        the enforcement cutover.
        """
        refresh_token_store = (
            vault if bridge_config.oauth_refresh_tokens_enabled else None
        )
        if oauth_endpoints is not None:
            oauth_router = build_oauth_router(
                endpoints=oauth_endpoints,
                client_store=vault,
                refresh_token_store=refresh_token_store,
                hmac_key=hmac_key,
                token_ttl_seconds=bridge_config.oauth_token_ttl_seconds,
                auth_code_ttl_seconds=bridge_config.oauth_auth_code_ttl_seconds,
                refresh_token_ttl_seconds=(
                    bridge_config.oauth_refresh_token_ttl_seconds
                ),
            )
            app.include_router(oauth_router)
            logger.info(
                "%s: OAuth 2.1 surface mounted (issuer=%s, "
                "token_ttl=%ds, auth_code_ttl=%ds, accepted_audiences=%s)",
                self.name,
                oauth_endpoints.issuer,
                bridge_config.oauth_token_ttl_seconds,
                bridge_config.oauth_auth_code_ttl_seconds,
                list(accepted_audiences) or "any",
            )
            return
        oauth_router = build_dynamic_oauth_router(
            streamable_path=STREAMABLE_PATH,
            client_store=vault,
            refresh_token_store=refresh_token_store,
            hmac_key=hmac_key,
            resource_aliases=bridge_config.oauth_resource_aliases,
            token_ttl_seconds=bridge_config.oauth_token_ttl_seconds,
            auth_code_ttl_seconds=bridge_config.oauth_auth_code_ttl_seconds,
            refresh_token_ttl_seconds=(
                bridge_config.oauth_refresh_token_ttl_seconds
            ),
        )
        app.include_router(oauth_router)
        logger.info(
            "%s: dynamic (origin-following) OAuth 2.1 login surface mounted "
            "(bearer_enforcement=%s)",
            self.name,
            "off" if bridge_config.streamable_no_auth else "on",
        )

    def _resolve_vault_plugin(
        self, *, require_refresh_token_methods: bool = False,
    ) -> Any:
        """Return the plugin bound to ``vault_service`` via the injected proxy.

        Reads ``self._vault_service`` (set by ``set_vault_service`` during
        lifecycle injection) so the profile's ``service_bindings`` decides
        which concrete plugin backs the interface (e.g.
        ``macos_vault_plugin`` for local, ``secrets_manager_vault_plugin``
        for cloud).  Hardcoding to ``macos_vault_plugin`` violated the
        Interface->Plugin rule and bypassed the cloud vault entirely
        (Task #31 §3.4).

        The streamable bearer-token verifier reads its HMAC secret
        from the vault via ``retrieve`` / ``store`` (Task #53 HS256
        migration); OAuth client lookup uses
        ``lookup_oauth_client`` / ``verify_oauth_client_credentials``.
        None of these are exposed on the LLM-visible registry; they
        are reachable only through this in-process handoff. Raises
        ``RuntimeError`` with a specific missing-method list if the
        bound plugin does not satisfy the required surface so
        misconfiguration fails fast at start-up rather than at the
        first phone request.

        When ``require_refresh_token_methods`` is True (the OAuth
        refresh-token rotation flag is enabled in bridge_config), the
        bound vault must additionally expose
        ``issue_oauth_refresh_token`` and
        ``consume_oauth_refresh_token``. Both default plugins do; the
        gate exists so a future vault implementation cannot be wired
        as the refresh-token store without satisfying that contract.
        """
        vault = self._vault_service
        if vault is None:
            raise RuntimeError(
                f"{self.name}: no plugin is bound to vault_service in "
                "the active profile's service_bindings; Streamable HTTP "
                "MCP transport requires a vault for bearer-token "
                "decryption + OAuth client lookup",
            )
        required_methods: list[str] = [
            "retrieve",
            "store",
            "lookup_oauth_client",
            "verify_oauth_client_credentials",
        ]
        if require_refresh_token_methods:
            required_methods.extend([
                "issue_oauth_refresh_token",
                "consume_oauth_refresh_token",
            ])
        missing = [m for m in required_methods if not hasattr(vault, m)]
        if missing:
            plugin_name = getattr(vault, "name", type(vault).__name__)
            raise RuntimeError(
                f"{self.name}: vault_service binding {plugin_name!r} is "
                f"missing required structural methods: {missing}. "
                "Streamable HTTP MCP transport requires HMAC key "
                "storage (retrieve/store) + OAuth client lookup"
                + (" + refresh-token rotation"
                   if require_refresh_token_methods else "")
                + ". Bind a different plugin via service_bindings or "
                "extend the current one with the missing methods.",
            )
        return vault

    def _start_streamable_server(
        self, bridge_config: _BridgeRuntimeConfig,
    ) -> dict[str, Any] | None:
        """Start the streamable HTTP listener; return failure dict on error."""
        self._streamable_host = bridge_config.streamable_host
        self._streamable_port = bridge_config.streamable_port
        self._streamable_server_started_event.clear()
        self._streamable_server_thread = threading.Thread(
            target=self._run_streamable_server,
            name=f"{PLUGIN_NAME}-streamable-server",
            daemon=True,
        )
        self._streamable_server_thread.start()
        if not self._streamable_server_started_event.wait(
            timeout=_SERVER_START_TIMEOUT_S,
        ):
            return _failure_result(
                code="bridge.streamable_startup_failed",
                message=(
                    f"Streamable HTTP server did not signal startup "
                    f"within {_SERVER_START_TIMEOUT_S}s"
                ),
            )
        return None

    def _run_streamable_server(self) -> None:
        """Uvicorn entry point for the streamable HTTP listener thread."""
        import uvicorn  # noqa: PLC0415
        app = self._app
        if app is None:
            logger.error(
                "%s: streamable server thread started without FastAPI app",
                self.name,
            )
            return
        self._streamable_server_loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._streamable_server_loop)
        cfg = uvicorn.Config(
            app,
            host=self._streamable_host or "0.0.0.0",  # noqa: S104
            port=self._streamable_port or 9000,
            log_level="warning",
            loop="asyncio",
        )
        server = uvicorn.Server(cfg)
        self._streamable_server_started_event.set()
        try:
            self._streamable_server_loop.run_until_complete(server.serve())
        except Exception:
            logger.exception(
                "%s: streamable HTTP server crashed", self.name,
            )
        finally:
            self._streamable_server_loop.close()

    def _resolve_config_provider(self) -> object:
        provider = getattr(self, "config_provider", None)
        if provider is None:
            self._populate_config_provider_from_orchestrator()
            provider = getattr(self, "config_provider", None)
        return provider


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _success_result(
    *,
    data: dict[str, object],
    actions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a successful ``ActionResult`` dict with all required keys.

    Returns a plain ``dict[str, Any]`` (rather than the
    :class:`ActionResult` TypedDict) so this helper is assignable to
    both the IO interface contract (``dict[str, Any]``) and the
    EDGE/EDGE_SINK ``ActionResult`` callers — the platform validator
    cares about the runtime keys, not the static type. ``actions`` ride
    the poller's Pattern-6a returned-action submission (the INF-02 serve
    verb's resume continuation).
    """
    return {
        "action_status": "completed",
        "data": data,
        "actions": [] if actions is None else actions,
        "error": None,
        "timestamp": _now_iso(),
    }


def _build_resume_action(row: dict[str, object]) -> dict[str, Any]:
    """Build the served request's resume continuation action_def.

    The row's ``resume_process_key`` names the consumer's re-entry verb
    (this plugin stays consumer-agnostic); arguments are platform-owned —
    just the ``request_id``, the consumer re-reads its correlation and the
    served text from the durable row. The def carries NO result_processor,
    so its completion is terminal (no spurious inference turn); the
    consumer's own returned actions continue the flow (Pattern 6a).
    """
    import json

    resume_key = str(row.get(COL_ICR_RESUME_PROCESS_KEY) or "")
    segments = resume_key.split("::")
    if len(segments) != 3 or not all(segments):
        raise FrameworkError(
            f"completion request {row.get(COL_ICR_REQUEST_ID)!r} carries a "
            f"malformed resume_process_key {resume_key!r} "
            "(expected provider_type::provider::function_name)",
        )
    provider_type, provider, function_name = segments
    request_id = str(row.get(COL_ICR_REQUEST_ID) or "")
    action_def: dict[str, Any] = {
        "name": f"resume_completion_{function_name}",
        "description": (
            f"Resume {provider}::{function_name} with the served completion "
            f"for request {request_id}"
        ),
        "process": {
            "provider_type": provider_type,
            "provider": provider,
            "function_name": function_name,
        },
        "arguments": {"request_id": request_id},
    }
    correlation = json.loads(str(row.get(COL_ICR_CORRELATION) or "{}"))
    context_id = correlation.get("context_id") if isinstance(correlation, dict) else None
    if isinstance(context_id, str) and context_id:
        action_def["context_id"] = context_id
    return action_def


def _failure_result(*, code: str, message: str) -> dict[str, Any]:
    """Build a failed ``ActionResult`` dict with all required keys."""
    timestamp = _now_iso()
    error: ErrorDetail = {
        "type": "agent_messaging_error",
        "code": code,
        "message": message,
        "details": {},
        "severity": "error",
        "timestamp": timestamp,
    }
    return {
        "action_status": "failed",
        "data": {},
        "actions": [],
        "error": error,
        "timestamp": timestamp,
    }


def _extract_message(params: dict[str, Any]) -> str:
    raw = params.get("message")
    return str(raw) if raw is not None else ""


def _find_bridge_by_session(
    manager: BridgeSessionManager, session_id: str,
) -> BridgeSessionState | None:
    for bridge in manager.list_active():
        if bridge.session_id == session_id:
            return bridge
    return None


def _find_claude_code_bridge_by_parent_pid(
    *,
    manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    parent_pid: int,
) -> BridgeSessionState | None:
    """Locate the open ``claude_code`` bridge bound to ``parent_pid``.

    Walks the peer registry to find every binding registered under
    ``agent_id="claude_code"`` whose live bridge has the matching
    ``parent_pid``.  On multiple matches (reconnect leak), prefer the
    most recently created bridge — the older one is almost certainly
    stale.
    """
    bindings = peer_registry.list_agent_ids().get("claude_code", [])
    matches: list[BridgeSessionState] = []
    for binding in bindings:
        bridge = manager.get(binding.bridge_id)
        if bridge is None or bridge.closed:
            continue
        if bridge.parent_pid != parent_pid:
            continue
        matches.append(bridge)
    if not matches:
        return None
    return max(matches, key=lambda b: b.created_at)


@dataclass(frozen=True, slots=True)
class _RoleSendSender:
    """Sender identity for a role-addressed (``peer_send_by_name``) dispatch.

    ``reply_to_role`` is the load-bearing field: when non-empty the recipient's
    envelope surfaces a role reply-to (``peer_send_by_name name=<role>``) so the
    return leg is durable (reconnect-surviving), closing the KB-08 §4 wart. The
    other fields are sender provenance (envelope display + persisted message).
    """

    agent_id: str
    agent_instance_id: str
    session_label: str
    bridge_id: str
    reply_to_role: str


def _str_field(value: object) -> str:
    """Return ``value`` if it is a non-empty string, else ``""``."""
    return value if isinstance(value, str) and value else ""


def _sender_from_role(
    role_name: str, origin_instance: str, state_service: Any,
) -> _RoleSendSender:
    """Sender identity for a role-stamped send: role reply-to + best-effort provenance.

    The role NAME is the durable reply-to address (survives a holder reconnect).
    The current binding supplies honest sender provenance when resolvable;
    resolution is best-effort (degrade-silent) so a provenance fault never breaks
    the send — ``reply_to_role`` is set regardless, so two-way still works.
    """
    agent_id, instance, label = SYSTEM_AGENT_ID, origin_instance, role_name
    try:
        binding = resolve_role_binding(state_service, role_name)
    except Exception:  # noqa: BLE001 — provenance is best-effort; never break the send
        binding = None
    if binding is not None:
        agent_id = binding.agent_id or SYSTEM_AGENT_ID
        instance = binding.agent_instance_id or origin_instance
        label = binding.session_label or role_name
    return _RoleSendSender(
        agent_id=agent_id,
        agent_instance_id=instance,
        session_label=label,
        bridge_id=SYSTEM_SCHEDULER_ID,
        reply_to_role=role_name,
    )


def _resolve_role_send_sender(
    state: dict[str, Any], state_service: Any,
) -> _RoleSendSender:
    """REL-01 Fork 4 resolution ladder for a role-addressed send's sender identity.

    Prefers the caller's DURABLE role (lifted into ``state`` from the flow
    trigger_data by ``ActionProcessor._lift_inference_vertex_identity``) so a role
    reply routes back to whoever holds it, surviving a holder reconnect. Ladder:

      1. role present → :func:`_sender_from_role` (role reply-to + provenance).
      2. else originating agent_instance_id present → fire-and-forget by instance,
         honestly labelled, no reply-to-role.
      3. else → genuine scheduler-originated send → the system scheduler sentinel
         (pre-REL-01 behaviour, now reached ONLY when no caller identity rode the
         flow, e.g. scheduler / heartbeat-originated sends).
    """
    role_name = _str_field(state.get("inference_vertex_role"))
    origin_instance = _str_field(state.get("inference_vertex_session_id"))
    if role_name:
        return _sender_from_role(role_name, origin_instance, state_service)
    if origin_instance:
        return _RoleSendSender(
            agent_id=SYSTEM_AGENT_ID,
            agent_instance_id=origin_instance,
            session_label="",
            bridge_id=SYSTEM_SCHEDULER_ID,
            reply_to_role="",
        )
    return _RoleSendSender(
        agent_id=SYSTEM_AGENT_ID,
        agent_instance_id=SYSTEM_SCHEDULER_ID,
        session_label=SYSTEM_SCHEDULER_LABEL,
        bridge_id=SYSTEM_SCHEDULER_ID,
        reply_to_role="",
    )


def _displaced_prose(name: str, new_agent_instance_id: str) -> str:
    """REL-04 displaced-holder notice. ``name`` is an opaque operator-defined role."""
    return (
        f"IMPORTANT: You have been displaced from role {name!r} by instance "
        f"{new_agent_instance_id}. You no longer hold this role — a role-addressed "
        f"message to {name!r} now reaches the new holder. Re-claim the role "
        f"(/rename) if this displacement was not intended."
    )


def _new_holder_prose(name: str) -> str:
    """REL-04 new-holder confirmation. ``name`` is an opaque operator-defined role."""
    return (
        f"IMPORTANT: You now hold role {name!r}. Drain your role backlog with "
        f"peer_inbox(include_important=true) — role-addressed messages sent to "
        f"{name!r} while it was held by another session (or unclaimed) are waiting."
    )


def _provider_get(provider: object, key: str) -> object | None:
    """Read ``key`` from a ConfigProvider-shaped object.

    Returns ``None`` when the provider is absent, has no ``.get`` method,
    or the key is missing. Callers wrap with ``_as_int`` / ``_as_bool`` /
    ``_as_str`` / ``_as_str_tuple`` which carry their own typed defaults.

    The third positional ``default`` parameter was dropped 2026-05-30 as
    part of the plugin-config-defaults unification: yaml's ``config:``
    block now lands as the lowest merge layer in
    ``ConfigManager.get_plugin_config``, so a hardcoded default at the
    ``_provider_get`` callsite duplicates the yaml entry and creates a
    drift surface (see Plugin Authoring Traps §10).
    """
    if provider is None:
        return None
    getter = getattr(provider, "get", None)
    if not callable(getter):
        return None
    return getter(key)


def _as_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    return default


def _as_int(value: object, default: int) -> int:
    if isinstance(value, bool):  # bool is a subclass of int — treat as default
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return default


def _as_optional_int(value: object) -> int | None:
    """Like :func:`_as_int` but returns ``None`` when no usable value was given.

    Used for fields whose semantics differ between "unset" (dynamic
    behavior) and "set to N" (explicit override).
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value)
    return None


def _as_str(value: object, default: str) -> str:
    if isinstance(value, str) and value:
        return value
    return default


def _as_str_tuple(
    value: object, *, default: tuple[str, ...] = (),
) -> tuple[str, ...]:
    if value is None:
        return default
    if isinstance(value, (list, tuple)):
        items = tuple(str(v) for v in value)
        return items or default
    return (str(value),)


def _resolve_homunculus_name() -> str:
    """Return the homunculus identity from ``$HOMUNCULUS_NAME``.

    Single source of truth across the platform: every plugin that
    surfaces a deployment label to external clients reads the same
    env var the bootstrap script sets.  Empty string when unset (laptop
    dev mode); downstream callers apply their own fallback.
    """
    import os  # noqa: PLC0415 — kept local so the import is greppable here
    return os.environ.get("HOMUNCULUS_NAME", "").strip()


__all__ = ["AgentMessagingPlugin"]
