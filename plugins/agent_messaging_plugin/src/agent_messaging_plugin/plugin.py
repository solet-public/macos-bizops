"""``agent_messaging_plugin`` — consolidated bridge plugin.

This plugin wears three hats (see plugin.yaml for the headline summary):

1. **AgentMessagingServiceInterface** — durable ``core__agent_thread`` /
   ``core__agent_message`` schema host for peer messaging and the
   session-ledger's unscoped thread/message reads.
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
which would hide ``send_peer_message`` / ``peer_send_by_name`` / the
session-lifecycle EDGE processes from ``submit_action_definition``.
``AgentMessagingServiceInterface`` is satisfied by structural
delegation; callers resolve us via
``plugin_manager.plugins["agent_messaging_plugin"]`` and call our
public methods directly.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import logging
import os
import secrets
import threading
from collections import OrderedDict
from dataclasses import dataclass, field, replace
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
from ananta.core.domain.enums import ActionStatus, ProcessorPolicyCategory
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
from ananta.llm.agent_messaging.models import PeerInbox, PeerInboxRequest, TextPart
from ananta.llm.agent_messaging.repository import AgentMessagingRepository
from ananta.llm.agent_messaging.role_binding import (
    SYS_AUTONOMIC_SLOT,
    is_system_role,
)
from ananta.llm.agent_messaging.schema import (
    get_agent_direct_wake_schema,
    get_agent_messaging_schema,
    get_agent_role_message_schema,
    get_role_covered_mark_schema,
)
from ananta.llm.agent_messaging.service import (
    AgentMessagingConfig,
    AgentMessagingError,
    AgentMessagingService,
    AgentRequestInvalidError,
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
    DEFAULT_BINDING_LIVENESS_WINDOW_S,
    BridgeNotFoundError,
    BridgeQueueFullError,
    BridgeSessionManager,
)
from .budget_report import build_budget_report as lifecycle_build_budget_report
from .choreography_verbs import (
    ACTION_GENERATE_CURATION_REPORT,
    ACTION_RESTART_SESSION,
    ACTION_ROTATE_SESSION,
    JOB_STATUS_COMPLETED,
    JOB_STATUS_ERROR,
    JOB_STATUS_PROCESSING,
    JOB_STATUS_QUEUED,
    PROVIDER_PLUGIN_NAME,
    GenerateCurationReportDispatchRequest,
    RestartSessionDispatchRequest,
    RotateSessionDispatchRequest,
    dispatch_generate_curation_report,
    dispatch_restart_session,
    dispatch_rotate_session,
)
from .choreography_verbs import (
    check_choreography_job_status as lifecycle_check_choreography_job_status,
)
from .constants import (
    PLUGIN_NAME,
    SYSTEM_AGENT_ID,
)
from .context_status_verbs import report_context_status as lifecycle_report_context_status
from .context_status_verbs import session_context_status as lifecycle_session_context_status
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
from .memory_curation_verbs import (
    build_curation_report,
    build_fact_index,
    origin_tag,
    resolve_memory_id_by_slug,
    slug_to_slot_tag,
)
from .message_important_backfill import backfill_message_important
from .peer_dispatch import (
    EVENT_POST_MESSAGE,
    NativeWakeError,
    build_wake_reply_hint,
    dispatch_peer_send,
    dispatch_role_send,
)
from .peer_inbox_view import serialize_peer_inbox_page
from .peer_list_view import serialize_peer_list
from .peer_registry import (
    PeerAmbiguousError,
    PeerRegistry,
    PeerSessionAmbiguousError,
    PeerUnreachableError,
)
from .platform_surface import PlatformSurface
from .process_exposure import ProcessExportPolicy
from .role_binding_store import (
    RoleBindingMalformedError,
    RoleBindingVacantError,
    holds_role,
    release_role_binding_v4,
    resolve_role_binding,
    resolve_role_binding_v4,
    run_cutover_migration_at_readiness,
    sole_role_for_reply_address,
)
from .role_claim import (
    RoleClaimFailure,
    RoleClaimOrigin,
    claim_role_for_session,
    send_handover_notice,
)
from .role_class_backfill import backfill_role_class
from .role_message_consumed_backfill import backfill_role_message_consumed
from .route_activity import make_model_activity_middleware
from .schema import (
    get_agent_role_binding_schema_definition,
    get_peer_binding_schema_definition,
    get_role_model_schema_definition,
    get_session_lifecycle_schema_definition,
)
from .session_claude_mapping_ingest import (
    detect_hook_absent_sessions as lifecycle_detect_hook_absent_sessions,
)
from .session_claude_mapping_ingest import (
    drain_session_claude_mapping_spool as lifecycle_drain_session_claude_mapping_spool,
)
from .session_claude_mapping_store import (
    list_session_claude_mappings as lifecycle_list_session_claude_mappings,
)
from .session_inference_provider import SessionInferenceProvider
from .session_lifecycle_store import format_directed_by
from .session_lifecycle_store import resolve_lane_charter as lifecycle_resolve_lane_charter
from .session_lifecycle_verbs import (
    ArmSessionDependencyRequest,
    CaptureLaneCharterRequest,
    LegislateRoleRequest,
    SpawnSessionRequest,
    VerbError,
)
from .session_lifecycle_verbs import arm_session_dependency as lifecycle_arm_session_dependency
from .session_lifecycle_verbs import capture_lane_charter as lifecycle_capture_lane_charter
from .session_lifecycle_verbs import clear_session as lifecycle_clear_session
from .session_lifecycle_verbs import compact_session as lifecycle_compact_session
from .session_lifecycle_verbs import drive_session as lifecycle_drive_session
from .session_lifecycle_verbs import legislate_role as lifecycle_legislate_role
from .session_lifecycle_verbs import list_sessions as lifecycle_list_sessions
from .session_lifecycle_verbs import report_alive as lifecycle_report_alive
from .session_lifecycle_verbs import retire_session as lifecycle_retire_session
from .session_lifecycle_verbs import session_status as lifecycle_session_status
from .session_lifecycle_verbs import spawn_session as lifecycle_spawn_session
from .session_lifecycle_verbs import terminate_session as lifecycle_terminate_session
from .session_role_claim_store import delete_session_role_claim_if_still_holds
from .session_sweep import (
    SessionRoleClaimPruner,
    sweep_deadline_dependencies,
    sweep_lane_closed_dependencies,
    sweep_overdue_sessions,
)
from .system_slots import (
    validate_system_slot_declarations,
)

if TYPE_CHECKING:  # pragma: no cover — type-only references
    from collections.abc import Mapping

    from ananta.core.orchestration.interfaces import ISessionManager
    from ananta.core.orchestration.managers.flow_manager import FlowManager
    from ananta.core.state.async_job_manager import AsyncJobManager
    from ananta.llm.agent_messaging.models import (
        AgentThreadMessagesPage,
        AgentThreadsPage,
        ListAgentThreadsRequest,
        PeerSendRequest,
        PeerSendResult,
        ReadThreadMessagesRequest,
    )
    from ananta.types.schema_types import SchemaDefinition
    from fastapi import FastAPI

    from .models import BridgeBinding, BridgeSessionState

logger = logging.getLogger(__name__)
SYSTEM_SCHEDULER_ID: Final[str] = "system:scheduler"
SYSTEM_SCHEDULER_LABEL: Final[str] = "System (Scheduler)"
# peer_inbox page size. Deliberately far below the route's 50: a freshly
# /clear'd Coordinator-Dawn measured a 422,513-character page at 50 instance +
# 50 role entries on 2026-08-01 — roughly 4KB per entry, because an entry
# carries the whole message. ``limit`` bounds the COUNT, so bytes are the
# caller's arithmetic, not the platform's promise: 5 is a page a session can
# read and still act on, and the two cursors exist to fetch the rest.
PEER_INBOX_DEFAULT_LIMIT: Final[int] = 5
PEER_INBOX_MIN_LIMIT: Final[int] = 1
# Parity with the /peer/inbox route's own clamp — one ceiling, both surfaces.
PEER_INBOX_MAX_LIMIT: Final[int] = 100


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
def _coerce_takeover(raw: object) -> bool:
    """Coerce the ``takeover`` parameter FAIL-CLOSED.

    Only a real ``True`` or the exact string ``"true"`` (case-insensitively,
    trimmed) authorizes displacing a live holder. Everything else — including
    the strings ``"false"`` and ``"0"``, ``None``, and any non-boolean type —
    is ``False``.

    A plain truthiness test would be wrong here in the one direction that
    matters: this parameter crosses a JSON transport, so a caller sending
    ``"false"`` as a STRING would take the role, which is the exact opposite of
    what they asked for, on the exact parameter whose purpose is that it must be
    deliberate. Refusing an intended takeover costs one retry with a clear
    message; performing an unintended one silently moves another session's
    deliveries.
    """
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.strip().lower() == "true"
    return False


def _bearer_hmac_key_vault_name() -> str:
    name = os.environ.get("HOMUNCULUS_NAME", "").strip()
    if not name:
        raise RuntimeError(
            "agent_messaging_plugin: HOMUNCULUS_NAME env var is required to "
            "resolve the scoped bearer_token_hmac_key vault entry name.",
        )
    return f"{name}.agent_messaging_plugin.bearer_token_hmac_key"


_BEARER_HMAC_KEY_VAULT_NAME = _bearer_hmac_key_vault_name()


class VaultEnvelopeError(RuntimeError):
    """Raised by :func:`_vault_retrieve_value` when a vault ``retrieve``
    call returns anything other than a well-formed hit or a well-formed
    miss — a REAL vault error (``action_status == "error"``, e.g. keychain
    unavailable) or an unrecognized envelope shape. This is deliberately
    NOT swallowed into "key absent": a caller treating a malformed/error
    envelope as a miss is exactly the vault-read envelope bug this seam
    exists to close (Dax Part 36 §36.2) — a weird envelope must never
    silently trigger a read-or-create caller's mint-and-store path."""


def _vault_retrieve_value(vault: Any, name: str) -> str | None:
    """Single seam for every vault ``retrieve`` consumer in this plugin.

    Keys on the REAL ``macos_vault_plugin`` ``ActionResult`` envelope
    (``plugin.py::_success``/``_not_found``): ``action_status`` is
    ``"completed"`` for BOTH a hit and a genuine miss — the vault never
    returns a top-level ``"status"`` key, so a caller keyed on that (the
    §36.2 bug) never recognizes a hit and re-mints on every read. A hit
    and a miss are distinguished by ``data`` shape instead: a hit carries
    a present, non-empty string ``data["value"]``; a well-formed miss
    carries ``data["found"] is False`` (``_not_found``'s exact shape,
    no ``"value"`` key at all).

    Returns the stored string value on a well-formed hit, ``None`` on a
    well-formed miss, and raises :class:`VaultEnvelopeError` on anything
    else — fast-fail, no silent fallback.
    """
    retrieved = vault.retrieve(name)
    if not isinstance(retrieved, dict):
        raise VaultEnvelopeError(
            f"vault.retrieve({name!r}) returned "
            f"{type(retrieved).__name__}, not a dict envelope",
        )
    if retrieved.get("action_status") != ActionStatus.COMPLETED.value:
        raise VaultEnvelopeError(
            f"vault.retrieve({name!r}) did not complete: "
            f"action_status={retrieved.get('action_status')!r}, "
            f"error={retrieved.get('error')!r}",
        )
    data = retrieved.get("data")
    if not isinstance(data, dict):
        raise VaultEnvelopeError(
            f"vault.retrieve({name!r}) returned action_status="
            f"'completed' with a non-dict data payload: {data!r}",
        )
    value = data.get("value")
    if isinstance(value, str) and value:
        return value
    if data.get("found") is False:
        return None
    raise VaultEnvelopeError(
        f"vault.retrieve({name!r}) returned action_status='completed' "
        "with an unrecognized data shape (neither a hit with a "
        f"non-empty 'value' nor a well-formed miss with found=False): {data!r}",
    )


def _load_or_create_bearer_hmac_key(vault: Any) -> bytes:
    """Return the homunculus's HMAC bearer-signing secret as raw bytes.

    Reads from the vault under :data:`_BEARER_HMAC_KEY_VAULT_NAME` via
    :func:`_vault_retrieve_value`; on first boot the entry is absent so
    we mint a fresh ``secrets.token_bytes(HMAC_KEY_BYTE_LENGTH)`` and
    persist its base64 encoding before returning. The value is
    base64-encoded in storage because the vault's ``store`` interface
    accepts a string.
    """
    stored_value = _vault_retrieve_value(vault, _BEARER_HMAC_KEY_VAULT_NAME)
    if stored_value is not None:
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
    # WS-2a W3 / WS-2e §4.3.2 — ONE knob, TWO consumers. A binding counts as
    # LIVE iff it resolves to a bridge whose ``last_seen_at`` is within this
    # window. Both transports long-poll continuously (the events poll holds
    # ~25s server-side and the client re-polls immediately), so a live
    # session's bridge never lags more than ~30s: 90 is >3x the worst-case
    # healthy gap and far under the 3_600s idle sweep, which makes staleness a
    # clean discriminator rather than a heuristic.
    #
    # Consumer 1 (here): dispatch refuses to report ``queued_watcher`` against
    # a bridge nobody is polling — a SIGKILLed watcher leaves its server-side
    # session alive, so ``append_event`` succeeds and the label lies for up to
    # the full idle sweep (~65 min).
    # Consumer 2 (pending operator sign-off): the duplicate-role claim gate.
    binding_liveness_window_seconds: int = DEFAULT_BINDING_LIVENESS_WINDOW_S
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


@dataclass(frozen=True, slots=True)
class _SessionLifecyclePolicyConfig:
    """Fleet session-management Phase B, §6 L3 rule 1 policy config.

    Kept separate from :class:`AgentMessagingConfig` for the same reason as
    :class:`_BridgeRuntimeConfig`: a distinct concern (spawn-time defaults for
    the L1 verb surface) reads its own settings without forcing the
    core agent-messaging service config to grow fields it doesn't use.

    ``work_class_defaults`` is operator-editable policy DATA (``plugin.yaml``'s
    ``config:`` block), not a code default: "cheapest capable model per
    work_class" is a values-laden business call this module does not make
    unilaterally (the same posture ``FLEET_HEADLESS_PERMISSION_MODE`` already
    takes for permission mode). Empty (the shipped default) means
    ``spawn_session`` behaves exactly as it did before this config existed —
    an unconfigured work_class leaves ``model``/``effort`` at whatever the
    caller passed (usually empty).

    ``work_class_tool_allowlists`` is the §6 permission-mode design's
    (2026-08-03) spawn-time tool allowlist, consumed by
    ``headless_adapter.py``'s PreToolUse gate
    (``.claude/hooks/headless_tool_allowlist_gate.py``). Operator ruling,
    same day, effective now ("we don't have any restrictions now"): shipped
    empty means the gate is UNARMED by default (``headless_adapter.py``'s
    ``_spawn_env`` only sets the hook's env var when an allowlist is
    actually non-empty) — the mechanism stays landed as shelf capability,
    armed per-``work_class`` whenever usage data argues for it, not
    exercised by default.

    ``headless_permission_mode`` is declared config (not a process env var —
    a config value is as declared as an env var, no LaunchAgent edit needed
    to change it). Shipped default ``"bypassPermissions"`` (flipped from
    ``"default"`` — D2 finding: Claude Code's own ``"default"`` interactive-
    approval mode leaves an unattended spawn with EMPTY effective grants,
    since ``--setting-sources project`` excludes every allowlist and no
    human exists to approve a prompt). Per the same operator ruling, no
    value (including ``"bypassPermissions"``) is rejected here — the knob is
    fail-closed only when it resolves to NOTHING at all
    (``headless_adapter.py.verify_config()``'s separate, unconditional
    floor), which is operational sanity, not a restriction.

    ``default_fleet_transport`` is the fleet-watch-transport-migration
    lane's single declared default-transport knob (phase 2 slice 2), the
    ONE configuration point the operator's verbatim charter's "easy to
    change later" clause names. Shipped default ``"watch"`` — the charter's
    own instruction that non-MCP must be the fleet's PRIMARY transport now,
    MCP retained as backup/chat-class only. Landed as shelf capability
    ahead of its consumers (same posture ``work_class_tool_allowlists``
    shipped in before ``headless_adapter.py`` read it): phase-2 slice 1
    wires the host adapters to read this value when building spawn env: it
    is not yet consumed by any spawn path as of this slice.
    """

    work_class_defaults: Mapping[str, Mapping[str, str]] = field(default_factory=dict)
    work_class_tool_allowlists: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    headless_permission_mode: str = ""
    default_fleet_transport: str = ""


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
    Exposes ``send_peer_message``, the session-lifecycle EDGE processes,
    and the bridge-delivery and IO EDGE_SINK processes through
    ``@platform_process`` decorators.

    NOTE: This plugin intentionally does NOT declare
    ``service_interfaces`` (the property would mark it as a
    ServiceProvider).  Bound ServiceProviders are skipped from the
    ``plugin::<name>::*`` registry namespace
    (process_registry/builder.py::_should_skip_plugin), which would
    hide those EDGE processes from ``submit_action_definition``.
    Instead, callers resolve us via
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
        self._session_role_claim_pruner: SessionRoleClaimPruner | None = None
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
        # maintenance-verbs M1 choreography jobs (rotate_session/restart_session,
        # D0.3-ratified deferred-completion shape). AsyncJobManager is lazily
        # pulled from orchestrator_ref (comfyui_image_generation_plugin's own
        # `_try_acquire_job_manager` precedent — there is no generic per-plugin
        # push-injection for it), not pushed at boot. Single dedicated worker
        # thread per the architect-pass constraint: FlowManager._sequence_cache
        # is an unlocked shared dict hit on every action submission, and the
        # comfyui pattern is only race-free because it runs exactly one
        # background worker — this thread must stay single and serialized,
        # never spawn a second concurrent choreography worker.
        self._async_job_manager: AsyncJobManager | None = None
        self._choreography_stop_event: threading.Event = threading.Event()
        self._choreography_worker_thread: threading.Thread | None = None

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

    def set_async_job_manager(self, async_job_manager: AsyncJobManager) -> None:
        """Accept an AsyncJobManager if ever pushed generically (no injection
        loop calls this today — see ``_try_acquire_async_job_manager`` for
        the actual lazy-pull path, mirroring
        ``comfyui_image_generation_plugin``'s identical precedent)."""
        logger.info("%s set_async_job_manager called", self.name)
        self._async_job_manager = async_job_manager

    def _try_acquire_async_job_manager(self) -> AsyncJobManager | None:
        """Lazily pull ``AsyncJobManager`` from ``orchestrator_ref`` the first
        time it's needed — the same pattern
        ``comfyui_image_generation_plugin._try_acquire_job_manager`` uses,
        verified at source (2026-08-09): there is no generic per-plugin
        push-injection for this service, only the attribute sitting on the
        orchestrator once platform boot wires it."""
        if self._async_job_manager is not None:
            return self._async_job_manager
        if not self.orchestrator_ref:
            return None
        job_manager = getattr(self.orchestrator_ref, "async_job_manager", None)
        if job_manager:
            self.set_async_job_manager(job_manager)
        return self._async_job_manager

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
        self._start_choreography_worker()
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
        # D1 §5: terminate every tracked headless worker so a graceful
        # shutdown/restart never leaves an orphaned Claude Code process
        # burning tokens with nothing tracking it (start_new_session=True
        # detaches it from this process's own group on purpose).
        from .session_hosts import shutdown_all_drivers  # noqa: PLC0415
        shutdown_all_drivers()
        self._stop_choreography_worker()
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

    # ------------------------------------------------------------------
    # maintenance-verbs M1 choreography worker (rotate_session/restart_session)
    # ------------------------------------------------------------------

    _CHOREOGRAPHY_POLL_INTERVAL_SECONDS = 2.0
    _CHOREOGRAPHY_DIRECTED_BY = "agent_messaging_plugin.choreography_worker"

    def _start_choreography_worker(self) -> None:
        """Start the SINGLE dedicated choreography worker thread — mirrors
        ``comfyui_image_generation_plugin``'s ``_worker_thread`` lifecycle
        exactly (started in ``start_services``, joined with a timeout in
        ``stop_services``). Deliberately ONE thread, never a pool: the
        architect-pass constraint (2026-08-09) is that
        ``FlowManager._sequence_cache`` is an unlocked shared dict hit on
        every action submission, and single-worker execution is what keeps
        this safe without fixing that race — do not parallelize this loop."""
        self._choreography_stop_event.clear()
        self._choreography_worker_thread = threading.Thread(
            target=self._choreography_worker_loop,
            name=f"{PLUGIN_NAME}-choreography-worker",
            daemon=True,
        )
        self._choreography_worker_thread.start()

    def _stop_choreography_worker(self) -> None:
        self._choreography_stop_event.set()
        thread = self._choreography_worker_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=30.0)
            if thread.is_alive():
                logger.error("%s choreography worker did not stop within timeout", self.name)
        self._choreography_worker_thread = None

    def _next_queued_choreography_job(
        self, job_manager: AsyncJobManager, provider_name: str,
    ) -> dict[str, Any] | None:
        """The oldest queued job for one provider_name, or ``None``. Split out
        of :func:`_choreography_worker_loop` to keep it a straight-line
        dispatcher (radon cc)."""
        jobs_result = job_manager.list_jobs(
            status=JOB_STATUS_QUEUED, provider_name=provider_name, limit=1,
            order_by="created_at ASC",
        )
        jobs = (jobs_result.get("data") or {}).get("jobs", [])
        if not isinstance(jobs, list) or not jobs:
            return None
        job = jobs[0]
        return job if isinstance(job, dict) else None

    def _poll_and_process_one_choreography_job(
        self, job_manager: AsyncJobManager, state_service: Any,
    ) -> None:
        """One tick's worth of work: check rotate, restart, then
        curation-report's queue for a single oldest job and process it —
        split out of :func:`_choreography_worker_loop` to keep it a
        straight-line dispatcher (radon cc)."""
        for provider_name in (
            f"{PROVIDER_PLUGIN_NAME}.{ACTION_ROTATE_SESSION}",
            f"{PROVIDER_PLUGIN_NAME}.{ACTION_RESTART_SESSION}",
            f"{PROVIDER_PLUGIN_NAME}.{ACTION_GENERATE_CURATION_REPORT}",
        ):
            if self._choreography_stop_event.is_set():
                return
            job = self._next_queued_choreography_job(job_manager, provider_name)
            if job is not None:
                self._process_choreography_job(job, job_manager, state_service)

    def _choreography_worker_loop(self) -> None:
        """Poll for queued rotate_session/restart_session jobs and process
        them ONE AT A TIME, serially — never concurrently (see the
        single-worker constraint on ``_start_choreography_worker``). Modeled
        directly on ``comfyui_image_generation_plugin._worker_loop``: a
        top-level ``except Exception`` per tick so one bad job can never kill
        the loop, and a wait-based poll interval rather than a hot spin."""
        while not self._choreography_stop_event.is_set():
            try:
                job_manager = self._try_acquire_async_job_manager()
                state_service = self._get_state_service()
                if job_manager is not None and state_service is not None:
                    self._poll_and_process_one_choreography_job(job_manager, state_service)
            except Exception:
                logger.exception("%s choreography worker loop error", self.name)
            self._choreography_stop_event.wait(self._CHOREOGRAPHY_POLL_INTERVAL_SECONDS)
        logger.debug("%s choreography worker loop exited", self.name)

    def _update_choreography_progress(
        self, job_manager: AsyncJobManager, job_id: str, *, progress_percent: int, leg: str,
    ) -> None:
        """One ``update_status`` call per choreography leg, per the D0.3-ratified
        shape. Logs the leg name for operator observability — the job ledger's
        own ``progress_percent`` is the only durable per-leg signal
        ``AsyncJobManager`` exposes; there is no free-text leg-name column."""
        logger.info("%s choreography job %s: leg=%s", self.name, job_id, leg)
        job_manager.update_job(
            job_id, {"status": JOB_STATUS_PROCESSING, "progress_percent": progress_percent},
        )

    def _complete_choreography_job(
        self, job_manager: AsyncJobManager, job_id: str, result: dict[str, Any],
    ) -> None:
        job_manager.update_job(job_id, {"status": JOB_STATUS_COMPLETED, "result": result})

    def _fail_choreography_job(
        self, job_manager: AsyncJobManager, job_id: str, code: str, message: str,
    ) -> None:
        job_manager.update_job(
            job_id, {"status": JOB_STATUS_ERROR, "error": {"code": code, "message": message}},
        )

    def _resolve_choreography_job_request(
        self, job: dict[str, Any], job_manager: AsyncJobManager,
    ) -> tuple[str, str, dict[str, Any]] | None:
        """``(job_id, provider_name, request_data)``, or ``None`` after
        already failing the job itself — split out of
        :func:`_process_choreography_job` to keep it a straight-line
        dispatcher (radon cc). The caller only needs to check for ``None``;
        every failure path here has already reached a terminal job status."""
        job_id = str(job.get("id") or "")
        provider_name = str(job.get("provider_name") or "")
        if not job_id or not provider_name:
            logger.error("%s choreography job missing id/provider_name: %r", self.name, job)
            return None
        payload_result = job_manager.get_job_payload(job_id, "request")
        if payload_result.get("action_status") != "completed":
            self._fail_choreography_job(
                job_manager, job_id, "request_payload_missing",
                "could not read the job's own request payload",
            )
            return None
        payload_data = payload_result.get("data")
        request_data = payload_data.get("payload") if isinstance(payload_data, dict) else None
        if not isinstance(request_data, dict):
            self._fail_choreography_job(
                job_manager, job_id, "request_payload_invalid",
                "job request payload was not an object",
            )
            return None
        return job_id, provider_name, request_data

    def _process_choreography_job(
        self, job: dict[str, Any], job_manager: AsyncJobManager, state_service: Any,
    ) -> None:
        """Dispatch one queued job to the rotate/restart/curation-report
        runner by ``provider_name`` suffix, and guarantee it reaches a
        TERMINAL status — every exception path here ends in
        ``_fail_choreography_job``, never a job left stranded at
        ``processing`` (the D0.3 doc's own named crash/reap gap is a
        platform-level absence this function must not add to by letting an
        exception escape uncaught)."""
        resolved = self._resolve_choreography_job_request(job, job_manager)
        if resolved is None:
            return
        job_id, provider_name, request_data = resolved
        try:
            if provider_name.endswith(f".{ACTION_ROTATE_SESSION}"):
                self._run_rotate_session_job(job_id, request_data, job_manager, state_service)
            elif provider_name.endswith(f".{ACTION_RESTART_SESSION}"):
                self._run_restart_session_job(job_id, request_data, job_manager, state_service)
            elif provider_name.endswith(f".{ACTION_GENERATE_CURATION_REPORT}"):
                self._run_generate_curation_report_job(job_id, request_data, job_manager)
            else:
                self._fail_choreography_job(
                    job_manager, job_id, "unknown_action",
                    f"unrecognized provider_name {provider_name!r}",
                )
        except VerbError as exc:
            logger.error(
                "%s choreography job %s failed: code=%s message=%s",
                self.name, job_id, exc.code, exc.message,
            )
            self._fail_choreography_job(job_manager, job_id, exc.code, exc.message)
        except Exception as exc:  # noqa: BLE001 — a job must reach a terminal status, never strand
            logger.exception("%s choreography job %s crashed", self.name, job_id)
            self._fail_choreography_job(job_manager, job_id, "internal_error", str(exc))

    # 2026-08-10 fix: measured live in gsuite-async's first production
    # rotation (job-2ns5on395r9xz) — the real post-clear first-turn latency
    # in a loaded production lane was ~77s from drive_session, and the new
    # claude_session_id was captured only ~6.7s after the OLD 60s window's
    # deadline had already declared verify_timeout on an otherwise-healthy
    # rotation (session_claude_mapping rows: drive_session leg logged
    # 2026-08-10T03:48:54.824Z, new id captured_at 2026-08-10T03:50:11.977Z).
    # A healthy rotation reporting as an error is a false negative any
    # job-status-driven automation would be misled by (the plausibility-
    # fence-below-the-plausible-range class). Raised with real margin, not
    # tuned tightly to this one sample.
    _ROTATE_VERIFY_MAX_WAIT_SECONDS = 300.0
    _ROTATE_VERIFY_POLL_INTERVAL_SECONDS = 5.0
    _RESTART_VERIFY_MAX_WAIT_SECONDS = 90.0
    _RESTART_VERIFY_POLL_INTERVAL_SECONDS = 5.0

    def _check_for_new_claude_session(
        self, state_service: Any, agent_instance_id: str, existing_ids: set[str],
    ) -> list[str]:
        """One point-in-time check for a ``claude_session_id`` outside
        ``existing_ids`` — split out of :func:`_wait_for_new_claude_session`
        so the poll loop and its post-deadline final re-check share exactly
        one query+diff, never two copies to drift."""
        current_ids = {
            str(m.get("claude_session_id") or "")
            for m in lifecycle_list_session_claude_mappings(state_service, agent_instance_id)
        }
        new_ids = current_ids - existing_ids
        new_ids.discard("")
        return sorted(new_ids)

    def _wait_for_new_claude_session(
        self, state_service: Any, agent_instance_id: str, existing_ids: set[str],
        max_wait_seconds: float, poll_interval_seconds: float,
    ) -> list[str]:
        """Poll ``list_session_claude_mappings`` until a ``claude_session_id``
        outside ``existing_ids`` appears, or the deadline passes — plus ONE
        final check immediately after the deadline, closing the narrow race
        where the id lands in the gap between the last poll and the
        deadline rather than genuinely never arriving. A NEW id appearing is
        a positive, mechanically-checked observation that a fresh session
        generation actually started (the SessionStart hook fired) — the
        ARMED-vs-FIRED distinction the M0 design names explicitly, not a
        bare status re-read or a fixed sleep."""
        deadline = datetime.now(UTC).timestamp() + max_wait_seconds
        while (
            datetime.now(UTC).timestamp() < deadline
            and not self._choreography_stop_event.is_set()
        ):
            new_ids = self._check_for_new_claude_session(
                state_service, agent_instance_id, existing_ids,
            )
            if new_ids:
                return new_ids
            self._choreography_stop_event.wait(poll_interval_seconds)
        return self._check_for_new_claude_session(state_service, agent_instance_id, existing_ids)

    def _wait_for_role_claim(self, role_name: str, agent_instance_id: str) -> bool:
        """Poll ``peer_holds_role`` (called as a plain method — ``@platform_process``
        is a metadata-only decorator, verified at source, so this executes
        identically to a dispatched call) until the new session claims
        ``role_name``, or the deadline passes."""
        deadline = datetime.now(UTC).timestamp() + self._RESTART_VERIFY_MAX_WAIT_SECONDS
        while (
            datetime.now(UTC).timestamp() < deadline
            and not self._choreography_stop_event.is_set()
        ):
            result = self.peer_holds_role(
                {"parameters": {"name": role_name, "agent_instance_id": agent_instance_id}}, {},
            )
            if result.get("action_status") == "completed":
                data = result.get("data")
                if isinstance(data, dict) and data.get("holds") is True:
                    return True
            self._choreography_stop_event.wait(self._RESTART_VERIFY_POLL_INTERVAL_SECONDS)
        return False

    def _run_rotate_session_job(
        self, job_id: str, request_data: dict[str, Any], job_manager: AsyncJobManager,
        state_service: Any,
    ) -> None:
        """§2.1 choreography, run OFF the dispatch path: resolve -> durable
        pickup -> clear -> drive -> verify. Every VerbError raised by a
        composed lifecycle verb propagates to :func:`_process_choreography_job`,
        which fails the job with that verb's own code/message — no
        catch-and-continue here."""
        agent_instance_id = _str_field(request_data.get("agent_instance_id"))
        role_name = _str_field(request_data.get("role_name"))
        pickup_text = _str_field(request_data.get("pickup_text"))
        park_first = bool(request_data.get("park_first", False))

        self._update_choreography_progress(
            job_manager, job_id, progress_percent=10, leg="resolve_ledger_row",
        )
        lifecycle_session_status(state_service, agent_instance_id)

        existing_ids = {
            str(m.get("claude_session_id") or "")
            for m in lifecycle_list_session_claude_mappings(state_service, agent_instance_id)
        }

        self._update_choreography_progress(
            job_manager, job_id, progress_percent=25, leg="durable_pickup_dispatch",
        )
        send_result = self.peer_send_by_name(
            {"parameters": {"name": role_name, "content": pickup_text}}, {},
        )
        if send_result.get("action_status") != "completed":
            raise VerbError(
                "pickup_dispatch_failed",
                f"peer_send_by_name to role {role_name!r} did not complete cleanly: "
                f"{send_result!r}",
            )

        self._update_choreography_progress(
            job_manager, job_id, progress_percent=45, leg="clear_session",
        )
        lifecycle_clear_session(
            state_service, agent_instance_id=agent_instance_id, park=park_first,
            directed_by=self._CHOREOGRAPHY_DIRECTED_BY,
        )

        self._update_choreography_progress(
            job_manager, job_id, progress_percent=65, leg="drive_session",
        )
        lifecycle_drive_session(
            state_service, agent_instance_id=agent_instance_id, text=pickup_text,
            directed_by=self._CHOREOGRAPHY_DIRECTED_BY,
        )

        self._update_choreography_progress(job_manager, job_id, progress_percent=85, leg="verify")
        new_ids = self._wait_for_new_claude_session(
            state_service, agent_instance_id, existing_ids,
            self._ROTATE_VERIFY_MAX_WAIT_SECONDS, self._ROTATE_VERIFY_POLL_INTERVAL_SECONDS,
        )
        if not new_ids:
            raise VerbError(
                "verify_timeout",
                f"no new claude_session_id observed for {agent_instance_id!r} within "
                f"{self._ROTATE_VERIFY_MAX_WAIT_SECONDS}s of drive_session — the turn "
                "may not have started (ARMED ≠ FIRED).",
            )
        self._complete_choreography_job(
            job_manager, job_id, {"turn_observed": True, "new_claude_session_ids": new_ids},
        )

    def _build_restart_spawn_params(
        self, old_row: dict[str, Any], role_class: str, lane_id: str, role_name: str,
    ) -> dict[str, Any]:
        """Carry the old ledger row's dispatch config forward into the fresh
        spawn's raw params — split out of :func:`_run_restart_session_job` to
        keep it a straight-line dispatcher (radon cc: each field extraction's
        own truthiness check lives here, not stacked onto the caller's
        count). Feeds :func:`_spawn_session_request_from_params`, the SAME
        raw-params builder ``spawn_session()`` itself uses (2026-08-10 fix:
        this path previously built a ``SpawnSessionRequest`` directly and
        skipped every policy-resolution step spawn_session() runs —
        permission_mode/allowed_tools/transport are not columns on
        managed_session, so they were silently lost every restart; routing
        through the shared params+policy path closes that class of drift for
        good, not just this one field)."""
        return {
            "role_class": role_class,
            "lane_id": lane_id,
            "brief_ref": _str_field(old_row.get("brief_ref")),
            "work_class": _str_field(old_row.get("work_class")),
            "budget_line": _str_field(old_row.get("budget_line")),
            "role_name": role_name,
            "host": _str_field(old_row.get("host")),
            "visibility": _str_field(old_row.get("visibility")),
            "model": _str_field(old_row.get("model")),
            "effort": _str_field(old_row.get("effort")),
            "spawned_by_role": self._CHOREOGRAPHY_DIRECTED_BY,
        }

    def _run_restart_session_job(
        self, job_id: str, request_data: dict[str, Any], job_manager: AsyncJobManager,
        state_service: Any,
    ) -> None:
        """§2.2 choreography, run OFF the dispatch path: capture -> terminate
        -> spawn -> (conditional) role-reclaim drive -> verify. Per the M0
        design's own gap finding, the role-reclaim drive defaults to ALWAYS
        firing unless a lane charter is already on file (option (b), coordinator-
        seat ruled default) — never trusts the automatic first turn alone to carry
        the role-claim instruction."""
        old_agent_instance_id = _str_field(request_data.get("agent_instance_id"))
        role_name = _str_field(request_data.get("role_name"))
        role_class = _str_field(request_data.get("role_class"))
        grace_seconds_raw = request_data.get("grace_seconds")
        grace_seconds = grace_seconds_raw if isinstance(grace_seconds_raw, int) else 30

        self._update_choreography_progress(
            job_manager, job_id, progress_percent=10, leg="capture_old_row",
        )
        old_row = lifecycle_session_status(state_service, old_agent_instance_id)
        lane_id = _str_field(old_row.get("lane_id"))

        self._update_choreography_progress(
            job_manager, job_id, progress_percent=25, leg="terminate_session",
        )
        lifecycle_terminate_session(
            state_service, agent_instance_id=old_agent_instance_id,
            directed_by=self._CHOREOGRAPHY_DIRECTED_BY, grace_seconds=grace_seconds,
        )

        self._update_choreography_progress(
            job_manager, job_id, progress_percent=45, leg="spawn_session",
        )
        raw_params = self._build_restart_spawn_params(old_row, role_class, lane_id, role_name)
        spawn_req = _spawn_session_request_from_params(raw_params, self._CHOREOGRAPHY_DIRECTED_BY)
        spawn_req = _apply_spawn_session_policy(spawn_req, self._build_session_lifecycle_policy_config())
        spawn_result = lifecycle_spawn_session(state_service, spawn_req)
        new_agent_instance_id = str(spawn_result.get("agent_instance_id") or "")
        if not new_agent_instance_id:
            raise VerbError(
                "spawn_failed", "restart_session: spawn_session returned no agent_instance_id.",
            )

        self._update_choreography_progress(
            job_manager, job_id, progress_percent=65, leg="role_reclaim_drive",
        )
        charter = lifecycle_resolve_lane_charter(state_service, lane_id) if lane_id else None
        role_reclaim_driven = charter is None
        if role_reclaim_driven:
            lifecycle_drive_session(
                state_service, agent_instance_id=new_agent_instance_id,
                text=(
                    f"claim role '{role_name}' via the rename skill / arm a watch "
                    f"process for it — this is a restart continuing lane {lane_id!r}, "
                    "not a fresh unbriefed spawn."
                ),
                directed_by=self._CHOREOGRAPHY_DIRECTED_BY,
            )

        self._update_choreography_progress(job_manager, job_id, progress_percent=85, leg="verify")
        holds = self._wait_for_role_claim(role_name, new_agent_instance_id)
        if not holds:
            raise VerbError(
                "verify_timeout",
                f"new session {new_agent_instance_id!r} never claimed role "
                f"{role_name!r} within {self._RESTART_VERIFY_MAX_WAIT_SECONDS}s of "
                "spawn (claim circle not broken — ARMED ≠ FIRED).",
            )
        self._complete_choreography_job(
            job_manager, job_id,
            {
                "old_agent_instance_id": old_agent_instance_id,
                "new_agent_instance_id": new_agent_instance_id,
                "role_reclaim_driven": role_reclaim_driven,
                "role_reclaim_verified": True,
            },
        )

    def _run_generate_curation_report_job(
        self, job_id: str, request_data: dict[str, Any], job_manager: AsyncJobManager,
    ) -> None:
        """M2.2 choreography, run OFF the dispatch path: fetch this origin's
        memory records once, build the fact index, rank the caller-supplied
        head lines. Raises ``VerbError`` (``memory_service_unavailable``,
        ``homunculus_name_unset``, ``memory_fetch_failed``) on any precondition
        this job cannot proceed without — propagates to
        :func:`_process_choreography_job`, which fails the job with that
        code/message, same contract as rotate/restart."""
        head_lines_raw = request_data.get("head_lines")
        head_lines = [str(x) for x in head_lines_raw] if isinstance(head_lines_raw, list) else []
        bottom_n_raw = request_data.get("bottom_n")
        bottom_n = bottom_n_raw if isinstance(bottom_n_raw, int) else 10
        byte_budget_raw = request_data.get("byte_budget")
        byte_budget = byte_budget_raw if isinstance(byte_budget_raw, int) else 17_000
        line_budget_raw = request_data.get("line_budget")
        line_budget = line_budget_raw if isinstance(line_budget_raw, int) else 132

        self._update_choreography_progress(
            job_manager, job_id, progress_percent=25, leg="fetch_memory_records",
        )
        if self._memory_service is None:
            raise VerbError(
                "memory_service_unavailable", "memory_service is not bound on this homunculus.",
            )
        homunculus_name = _resolve_homunculus_name_for_memory_tags()
        if not homunculus_name:
            raise VerbError(
                "homunculus_name_unset",
                "Could not resolve a homunculus name to scope the memory fetch to this "
                "origin -- HOMUNCULUS_NAME is unset, root_manifest.yaml is unreadable or "
                "still carries its unwritten placeholder, and CLAUDE_PROJECT_DIR is unset "
                "(the final fallback needs it too).",
            )
        fetch_result = self._memory_service.get_memories_by_tag(tag=origin_tag(homunculus_name))
        records = fetch_result.get("memories") if isinstance(fetch_result, dict) else None
        if not isinstance(records, list):
            raise VerbError(
                "memory_fetch_failed",
                f"get_memories_by_tag returned no usable 'memories' list: {fetch_result!r}",
            )

        self._update_choreography_progress(
            job_manager, job_id, progress_percent=65, leg="build_index_and_rank",
        )
        fact_index = build_fact_index(records, homunculus_name)
        report = build_curation_report(
            head_lines, fact_index,
            bottom_n=bottom_n, byte_budget=byte_budget, line_budget=line_budget,
        )
        self._complete_choreography_job(job_manager, job_id, report)

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
        """Declare the EDGE processes this plugin owns.

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
            "spawn_session": EdgeProcessDefinition(
                name="spawn_session",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "legislate_role": EdgeProcessDefinition(
                name="legislate_role",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "capture_lane_charter": EdgeProcessDefinition(
                name="capture_lane_charter",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    # A fresh INSERT, not idempotent on conflict -- an
                    # automatic retry after an uncertain result would write
                    # a SECOND charter row, which resolve_lane_charter would
                    # then treat as the superseding one. Never safe to retry
                    # blind.
                    retryable=False,
                ),
            ),
            "arm_session_dependency": EdgeProcessDefinition(
                name="arm_session_dependency",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    # A fresh INSERT, not idempotent on conflict (unlike
                    # legislate_role's on_conflict=do_nothing) -- an
                    # automatic retry after an uncertain result would arm a
                    # SECOND edge for the same condition, never safe.
                    retryable=False,
                ),
            ),
            "list_sessions": EdgeProcessDefinition(
                name="list_sessions",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "budget_report": EdgeProcessDefinition(
                name="budget_report",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    # Read-only (issues no writes) -- always safe to retry.
                    retryable=True,
                ),
            ),
            "drain_session_claude_mapping_spool": EdgeProcessDefinition(
                name="drain_session_claude_mapping_spool",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    # Idempotent by construction (upsert on the spool
                    # filename's own conflict triple) -- an automatic retry
                    # after an uncertain result is always safe, unlike
                    # arm_session_dependency's fresh-INSERT-only sibling above.
                    retryable=True,
                ),
            ),
            # usage-capture-attribution D2 follow-on (2026-08-06, workbench
            # 2026-08-06_usage_capture_attribution_findings_usage-capture-impl.md):
            # a read-only listing verb over session_claude_mapping, so a
            # future budget_report diagnosis can read the mapping table
            # directly instead of inferring its contents (as this lane's own
            # D1/D2 had to).
            "list_session_claude_mappings": EdgeProcessDefinition(
                name="list_session_claude_mappings",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    # Read-only (issues no writes) -- always safe to retry.
                    retryable=True,
                ),
            ),
            "session_status": EdgeProcessDefinition(
                name="session_status",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "clear_session": EdgeProcessDefinition(
                name="clear_session",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "compact_session": EdgeProcessDefinition(
                name="compact_session",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "drive_session": EdgeProcessDefinition(
                name="drive_session",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "terminate_session": EdgeProcessDefinition(
                name="terminate_session",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "retire_session": EdgeProcessDefinition(
                name="retire_session",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "report_alive": EdgeProcessDefinition(
                name="report_alive",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            # maintenance-verbs M1 (workbench
            # 2026-08-09_maintenance_verbs_m0_design_mverbs-impl.md §2.3).
            # Retryable: an overwrite upsert of the caller's OWN latest
            # snapshot is idempotent-on-repeat by construction (same row,
            # same conflict key) — a retry after a transient fault can never
            # double-record or corrupt an earlier value.
            "report_context_status": EdgeProcessDefinition(
                name="report_context_status",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # Read-only (issues no writes) -- always safe to retry.
            "session_context_status": EdgeProcessDefinition(
                name="session_context_status",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # maintenance-verbs M1, D0.3-ratified deferred-completion shape.
            # NOT retryable: a repeat call creates a SECOND choreography job
            # (AsyncJobManager.create_job mints a fresh job_id every call, no
            # idempotency key) -- a naive retry after a transient dispatch
            # fault would double-drive the same worker. The caller re-checks
            # via check_choreography_job_status before ever re-dispatching.
            "rotate_session": EdgeProcessDefinition(
                name="rotate_session",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            "restart_session": EdgeProcessDefinition(
                name="restart_session",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            # Read-only (issues no writes) -- always safe to retry. Also
            # serves generate_curation_report's job family -- it is a
            # generic AsyncJobManager job-row reader, not scoped to
            # rotate/restart specifically (verified at source).
            "check_choreography_job_status": EdgeProcessDefinition(
                name="check_choreography_job_status",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # M2.2, same D0.3 dispatch shape as rotate/restart_session above.
            # NOT retryable for the identical reason: a repeat call mints a
            # SECOND job (no idempotency key on create_job), double-queuing
            # the same report -- the caller re-checks via
            # check_choreography_job_status before ever re-dispatching.
            "generate_curation_report": EdgeProcessDefinition(
                name="generate_curation_report",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            # Idempotent in effect (a second reinforce on the same slug just
            # adds another retrieval timestamp) but NOT marked retryable --
            # a naive retry after a transient dispatch fault would still
            # double-reinforce the target memory, over-counting
            # retrieval_count for a citation that only happened once. Mirrors
            # peer_claim_role's own "side effect, so don't auto-retry" stance.
            "reinforce_by_slug": EdgeProcessDefinition(
                name="reinforce_by_slug",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=False,
                ),
            ),
            # Pull-surface boundary (design §2). Retryable: the write is
            # monotonic (an attestation at or below the stored mark is a
            # no-op), so a retry after a transient fault re-attests the same
            # value harmlessly rather than double-advancing anything.
            "peer_mark_role_covered": EdgeProcessDefinition(
                name="peer_mark_role_covered",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # Pull receive verb. Retryable: it is a pure
            # read whose only write is the liveness touch, so a repeat is
            # harmless and a transient state-read fault is worth re-running.
            "peer_inbox": EdgeProcessDefinition(
                name="peer_inbox",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
                ),
            ),
            # Peer-enumeration asymmetry close (WS-1a pattern, operator-
            # prompted 2026-08-02): a no-MCP session could read its own mail
            # via peer_inbox but had no way to see who else was live.
            # Retryable: a pure, unfiltered registry snapshot with no write
            # at all, so a repeat after a transient state-read fault is
            # always harmless.
            "peer_list": EdgeProcessDefinition(
                name="peer_list",
                result_processor_template_customizations=MergeResultProcessorCustomizations(
                ),
                error_processor_template_customizations=MergeErrorProcessorCustomizations(
                    retryable=True,
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

    def list_threads(
        self, request: ListAgentThreadsRequest,
    ) -> AgentThreadsPage:
        return self._require_service().list_threads(request)

    def read_thread_messages(
        self, request: ReadThreadMessagesRequest,
    ) -> AgentThreadMessagesPage:
        return self._require_service().read_thread_messages(request)

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
            get_role_covered_mark_schema(),
            get_peer_binding_schema_definition(),
            get_agent_role_binding_schema_definition(),
            get_role_model_schema_definition(),
            get_session_lifecycle_schema_definition(),
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
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Instance-addressed peer send, stamped with the CALLER's identity.

        §34.6: this verb used to hardcode the ``system`` / ``system:scheduler``
        / ``System (Scheduler)`` sentinel, so a message sent through it arrived
        unattributable no matter which transport the caller used — including a
        registered MCP session, whose identity was already sitting unread in
        ``state``. It now resolves the sender through the same ladder
        ``peer_send_by_name`` uses (:func:`_resolve_role_send_sender`), which
        reads only SERVER-STAMPED state keys; the sentinel remains the honest
        answer for a genuinely scheduler-originated send.

        ``sender_bridge_id`` deliberately stays :data:`SYSTEM_SCHEDULER_ID`:
        peer threads are keyed on ``(sender_bridge_id, peer_instance)``, so
        substituting the caller's live (or one-shot) bridge id would fork a new
        thread per send. Only the identity triple changes — never the key.
        """
        if self._peer_registry is None or self._bridge_manager is None:
            return _failure_result(
                code="bridge.not_running",
                message="Bridge not started — call start_interface first",
            )
        raw = params.get("parameters", params)
        peer_id = str(raw.get("peer_id", ""))
        peer_agent_instance_id = raw.get("peer_agent_instance_id") or None
        content_text = str(raw.get("content", ""))
        content: list[TextPart] = [TextPart(type="text", text=content_text)]
        # Unchanged from before this lane: state_service may be None (not yet
        # bound at bootstrap) and _resolve_role_send_sender already degrades
        # gracefully on that — never hard-fail this verb over it. The new
        # dispatch_peer_send param accepts None for exactly this case
        # (drive_on_delivery is best-effort and no-ops without one).
        state_service = self._get_state_service()
        sender = _resolve_role_send_sender(state, state_service)
        try:
            outcome = dispatch_peer_send(
                bridge_manager=self._bridge_manager,
                peer_registry=self._peer_registry,
                agent_messaging_service=self._require_service(),
                state_service=state_service,
                sender_bridge_id=SYSTEM_SCHEDULER_ID,
                sender_agent_id=sender.agent_id,
                sender_agent_instance_id=sender.agent_instance_id,
                sender_session_label=sender.session_label,
                sender_parent_pid=None,
                peer_id=peer_id,
                peer_agent_instance_id=peer_agent_instance_id,
                content=content,
                # WS-2c V4: resolved from the SENDER's registered instance, not
                # from ``sender.reply_to_role`` — the ladder's role rung takes
                # ``sorted(roles)[0]``, which is fine as a flow tag but would
                # misroute a multi-role sender's replies (DEF-3).
                reply_to_role=sole_role_for_reply_address(
                    self._get_state_service(), sender.agent_instance_id,
                ),
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
                    "Message text. Delivery is a transport property, not a "
                    "sender-declared one (A4, 2026-08-04): every send is "
                    "delivery-attempted against the resolved recipient's "
                    "live binding, waking it if a native adapter is "
                    "registered. A leading 'IMPORTANT:' is stripped as "
                    "input hygiene only; it no longer changes delivery."
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
            state_service=state_service,
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
        """Bind this plugin's bridge collaborators to the shared REL-04 sender.

        Kept as a bound method because ``AutonomicAssignment`` takes it as its
        ``send_notice`` callable. The behaviour lives in :mod:`role_claim` so the
        verb, the bridge route, and the autonomic lane all emit the same notice.
        """
        return send_handover_notice(
            bridge_manager=self._bridge_manager,
            peer_registry=self._peer_registry,
            agent_messaging_service=self._handover_service(),
            state_service=self._get_state_service(),
            peer_id=peer_id,
            peer_agent_instance_id=peer_agent_instance_id,
            prose=prose,
            kind=kind,
        )

    def _handover_service(self) -> Any:
        """The messaging service the REL-04 notices dispatch through, or ``None``.

        ``_require_service`` builds the service on first use, so the raw
        ``_service`` attribute is not a substitute for it. Only built when the
        bridge collaborators exist: without them a notice cannot be dispatched
        at all, and building the service would be wasted work that can raise on
        a plugin whose orchestrator is not injected.
        """
        if self._bridge_manager is None or self._peer_registry is None:
            return None
        return self._require_service()

    def _claimant_session_id(self, agent_instance_id: str) -> str:
        """A session's stable id from its live ``peer_binding`` row (REL-07(1)).

        ``peer_holds_role`` reads it here rather than trusting a caller-supplied
        session id — the whole point of that verb is that it compares PULL-TRUTH.
        The claim path sources the same value inside
        :func:`role_claim.claim_role_for_session`, from the same registry.
        Returns ``""`` when the bridge is not started or the instance is
        unregistered.
        """
        if self._peer_registry is None:
            return ""
        return self._peer_registry.agent_session_id_for_instance(agent_instance_id)

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
            "agent_session_id": ParameterMetadata(
                description=(
                    "Stable logical session id for reconnect-safe role binding. "
                    "When omitted, the plugin sources it from the claimant's "
                    "live peer_binding row."
                ),
                required=False,
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
            "takeover": ParameterMetadata(
                description=(
                    "Explicitly take the role from a LIVE holder, displacing it. "
                    "Default false, in which case a live holder is refused with "
                    "``role_held_live`` and the claim does nothing. This is the "
                    "escape hatch that refusal's message names: it exists so a "
                    "deliberate, operator-confirmed handover is possible while an "
                    "accidental one still fails. It authorizes THIS claim only and "
                    "is never persisted. With no live holder it is a silent no-op — "
                    "an ordinary claim — so callers never have to pre-check "
                    "liveness and race their own answer."
                ),
                required=False,
                type=ParameterType.BOOLEAN,
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
                "agent_session_id": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def peer_claim_role(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Claim-or-replace the ``agent_role_binding`` row for ``name`` (v10 #2.C).

        The MODEL_INITIATED transport for a claim: reached through
        ``/process/call``, so a genuine model turn (the ``/rename`` skill) stamps
        ``last_model_activity_at`` as it should. The forwarder's housekeeping
        claim deliberately does NOT come here — it uses the INFRA
        ``peer/claim_role`` bridge route, which shares this body via
        :func:`role_claim.claim_role_for_session` but is classified so it never
        stamps. Splitting the transports is what separates "the model claimed a
        role" from "the bridge re-asserted its binding"; see the module docstring
        of :mod:`role_claim`.

        Caller-supplied identity (the ``/rename`` skill threads ``agent_id`` /
        ``agent_instance_id`` from the ``peer_register`` response, and
        ``agent_session_id`` from the same response when a carrier set it). A
        full-row upsert replaces any prior binding for the name in place —
        including a backfilled UNCLAIMED ``agent_session_id``.
        """
        raw = params.get("parameters", params)
        result = claim_role_for_session(
            origin=RoleClaimOrigin.MODEL_TURN,
            name=str(raw.get("name", "")),
            agent_id=str(raw.get("agent_id", "")),
            agent_instance_id=str(raw.get("agent_instance_id", "")),
            agent_session_id=str(raw.get("agent_session_id", "")),
            session_label=str(raw.get("session_label", "")),
            # Explicit escape hatch for a LIVE holder (§4.3.3a). bool() rather
            # than a truthiness test on the raw value: the transport hands JSON,
            # so a caller sending the STRING "false" would otherwise take the
            # role — the opposite of what they asked for, on the one parameter
            # whose whole purpose is that it must be deliberate.
            takeover=_coerce_takeover(raw.get("takeover")),
            state_service=self._get_state_service(),
            bridge_manager=self._bridge_manager,
            peer_registry=self._peer_registry,
            # NOT ``self._service`` — that attribute is lazily populated, and
            # ``_require_service`` is what BUILDS it on first use. Reading the
            # raw attribute would hand the claim body ``None`` whenever nothing
            # had happened to construct the service yet, and the handover notices
            # would then be skipped with a "bridge not started" log while the
            # claim reported success: a displaced holder never told it lost the
            # role. Guarded exactly as ``_send_handover_notice`` guards it — with
            # no bridge collaborators there is nothing to notify anyway, so
            # building the service would be pointless work that can raise.
            agent_messaging_service=self._handover_service(),
            # SERVER-BUILT context, lifted into ``state`` by the action processor
            # — never read from caller ``params``, so slot ownership cannot be
            # forged.
            call_context=state.get("call_context"),
        )
        if isinstance(result, RoleClaimFailure):
            return _failure_result(code=result.code, message=result.message)
        return _success_result(data=dict(result.to_public()))

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
        # Fleet session-management Phase B, D1 (§2 rule 3, Architect ratification
        # #2): capture the PRE-release holder's agent_session_id so the
        # session_role_claim row can be pruned AFTER the binding release —
        # binding-release strictly precedes the session-key-row delete (never
        # the reverse: row-first + a crash between lets a fresh INSERT for a
        # new role slip past the still-standing old binding, a double-claim).
        prior_session_id = ""
        with contextlib.suppress(RoleBindingVacantError, RoleBindingMalformedError):
            prior_session_id = resolve_role_binding_v4(state_service, name).agent_session_id
        # §9 CUTOVER: hard-delete the v4 role_binding row (no-tombstone §5.1).
        outcome = release_role_binding_v4(state_service, name)
        if prior_session_id and not is_system_role(name):
            delete_session_role_claim_if_still_holds(
                state_service, agent_session_id=prior_session_id, expected_held_role=name,
            )
        return _success_result(data=outcome)

    @platform_process(
        name="spawn_session",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "role_class": ParameterMetadata(
                description=(
                    "ephemeral | project | principal (§2 taxonomy; primary/chat "
                    "are never spawn-assigned)."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "lane_id": ParameterMetadata(
                description="The lane this session is spawned for (provenance).",
                required=True,
                type=ParameterType.STRING,
            ),
            "brief_ref": ParameterMetadata(
                description="Workbench path or dispatch id backing the spawn (provenance).",
                required=True,
                type=ParameterType.STRING,
            ),
            "work_class": ParameterMetadata(
                description="read_only | analysis_deliverable | production_mutation.",
                required=True,
                type=ParameterType.STRING,
            ),
            "budget_line": ParameterMetadata(
                description="The token-budget ledger key this spawn rolls up to.",
                required=True,
                type=ParameterType.STRING,
            ),
            "role_name": ParameterMetadata(
                description=(
                    "Named role to fill on boot (project: may mint; principal: "
                    "fill-never-mint, must already be legislated). Empty for ephemeral."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "host": ParameterMetadata(
                description=(
                    "Per-spawn host override (tmux | headless | operator). Empty "
                    "falls to FLEET_SESSION_HOST env, then the platform default."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "visibility": ParameterMetadata(
                description="visible | headless (spawn-time operator/primary parameter).",
                required=False,
                type=ParameterType.STRING,
            ),
            "model": ParameterMetadata(
                description="Dispatch model override.", required=False, type=ParameterType.STRING,
            ),
            "effort": ParameterMetadata(
                description="Dispatch effort override.", required=False, type=ParameterType.STRING,
            ),
            "allowed_tools": ParameterMetadata(
                description=(
                    "Explicit tool-name allowlist override for the headless "
                    "PreToolUse gate (§6 permission-mode ruling, 2026-08-03). "
                    "Omitted -> resolved from plugin.yaml's per-work_class "
                    "work_class_tool_allowlists; unconfigured -> empty (the "
                    "spawn is still gated, just with nothing extra allowed)."
                ),
                required=False,
                type=ParameterType.LIST,
            ),
            "permission_mode": ParameterMetadata(
                description=(
                    "Explicit --permission-mode override for the headless host "
                    "driver (§6 permission-mode design, 2026-08-03). Omitted -> "
                    "resolved from plugin.yaml's headless_permission_mode. No "
                    "value is rejected (operator ruling, 2026-08-03: 'we don't "
                    "have any restrictions now'); the driver still refuses if "
                    "this and the config both resolve to nothing at all."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "report_by_seconds": ParameterMetadata(
                description="Initial report-or-die deadline, in seconds from spawn.",
                required=False,
                type=ParameterType.INTEGER,
            ),
            "ttl_seconds": ParameterMetadata(
                description="Optional hard TTL, in seconds from spawn.",
                required=False,
                type=ParameterType.INTEGER,
            ),
            "spawned_by_instance_id": ParameterMetadata(
                description="Lineage: the spawning session's own agent_instance_id.",
                required=False,
                type=ParameterType.STRING,
            ),
            "spawned_by_role": ParameterMetadata(
                description="Lineage: the spawning session's role name at spawn time, if any.",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "spawn_session outcome: the new session's identity + host dispatch result."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="spawn_session outcome (D1 §4)",
            properties={
                "agent_instance_id": ParameterMetadata(type=ParameterType.STRING),
                "host": ParameterMetadata(type=ParameterType.STRING),
                "host_ref": ParameterMetadata(type=ParameterType.STRING),
                "lifecycle_state": ParameterMetadata(type=ParameterType.STRING),
                "first_turn_source": ParameterMetadata(
                    description="charter | fallback — which text was driven as turn 1 "
                    "(phase 2 slice 6).",
                    type=ParameterType.STRING,
                ),
                "first_turn_delivered": ParameterMetadata(
                    description="Whether the first-turn send succeeded. False never blocks "
                    "the spawn itself; the failure is logged separately.",
                    type=ParameterType.BOOLEAN,
                ),
                "first_turn_error": ParameterMetadata(
                    description="Non-empty error detail when first_turn_delivered is False; "
                    "empty on success.",
                    type=ParameterType.STRING,
                ),
            },
        ),
    )
    def spawn_session(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """§4 ``spawn_session`` — validate, write the ledger row (spawning,
        BEFORE host dispatch), dispatch through the resolved host driver.

        ``operator`` (degenerate, cannot spawn), ``headless`` (D1, the
        registered default), and ``tmux`` (D2) all ship registered — see
        ``session_hosts.py`` for the current registry. A call with
        ``host="operator"`` (or an unconfigured ``headless``/``tmux``
        environment) still ends in ``host_cannot_spawn``, with the specific
        remedies in the error; an undeclared/typo'd host name ends in
        ``host_mechanism_missing``.
        """
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        req = _spawn_session_request_from_params(raw, format_directed_by(state.get("call_context")))
        req = _apply_spawn_session_policy(req, self._build_session_lifecycle_policy_config())
        try:
            result = lifecycle_spawn_session(state_service, req)
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="legislate_role",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "name": ParameterMetadata(
                description=(
                    "The role name to legislate (e.g. 'Coordinator-Main' for a "
                    "role_class='primary' seat)."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "role_class": ParameterMetadata(
                description=(
                    "primary | principal — the ONE two-value taxonomy this "
                    "governance act may assign; project/ephemeral/chat are "
                    "minted, never legislated."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "brief_ref": ParameterMetadata(
                description=(
                    "Workbench path, dispatch id, or ruling reference "
                    "authorizing this act (provenance — mirrors spawn_session's "
                    "brief_ref)."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="legislate_role outcome: the legislated name + role_class.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="legislate_role outcome (D4 Part B item 1)",
            properties={
                "action": ParameterMetadata(type=ParameterType.STRING),
                "name": ParameterMetadata(type=ParameterType.STRING),
                "role_class": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def legislate_role(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """D4 Part B item 1 — governance-act creation of a ``role`` row with
        an authority-carrying ``role_class`` (``primary``/``principal``)
        stamped at birth. The ONE sanctioned path outside ``peer_claim_role``
        (§3.1 Q1: claim-time is enforce-by-class, never class-assignment).

        ``directed_by`` is server-built from ``call_context`` via
        ``format_directed_by`` — the SAME provenance convention
        ``spawn_session`` uses, never caller-supplied.
        """
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        req = LegislateRoleRequest(
            name=str(raw.get("name") or ""),
            role_class=str(raw.get("role_class") or ""),
            brief_ref=str(raw.get("brief_ref") or ""),
            directed_by=format_directed_by(state.get("call_context")),
        )
        try:
            result = lifecycle_legislate_role(state_service, req)
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="capture_lane_charter",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "lane_id": ParameterMetadata(
                description="The lane this charter founds.",
                required=True,
                type=ParameterType.STRING,
            ),
            "charter_text": ParameterMetadata(
                description=(
                    "The operator's verbatim founding words, captured byte-exact. "
                    "Driven unmodified as a spawned worker's literal first turn "
                    "(spawn_session resolves the LATEST captured row for the "
                    "worker's lane_id)."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "captured_at": ParameterMetadata(
                description=(
                    "ISO-8601 timestamp of when the operator spoke these words in "
                    "the seat conversation — NOT the row-write time."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "brief_ref": ParameterMetadata(
                description="Workbench path or dispatch id this charter accompanies.",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="capture_lane_charter outcome: the newly written charter row.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="capture_lane_charter outcome (phase 2 slice 6)",
            properties={
                "lane_id": ParameterMetadata(type=ParameterType.STRING),
                "charter_text": ParameterMetadata(type=ParameterType.STRING),
                "brief_ref": ParameterMetadata(type=ParameterType.STRING),
                "captured_at": ParameterMetadata(type=ParameterType.STRING),
                "directed_by": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def capture_lane_charter(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """Phase 2 slice 6, design check-in ruling item 3(a) — the seat-
        invoked governance act that writes a ``lane_charter`` row.
        Insert-only: calling this again for the same ``lane_id`` supersedes
        by recency, it never edits a prior charter's text in place.

        ``directed_by`` is server-built from ``call_context`` via
        ``format_directed_by`` — the SAME provenance convention
        ``spawn_session``/``legislate_role`` use, never caller-supplied.
        """
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        req = CaptureLaneCharterRequest(
            lane_id=str(raw.get("lane_id") or ""),
            charter_text=str(raw.get("charter_text") or ""),
            captured_at=str(raw.get("captured_at") or ""),
            brief_ref=str(raw.get("brief_ref") or ""),
            directed_by=format_directed_by(state.get("call_context")),
        )
        try:
            result = lifecycle_capture_lane_charter(state_service, req)
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="arm_session_dependency",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "waiter_instance_id": ParameterMetadata(
                description=(
                    "The waiting session's agent_instance_id. Required — v1 is "
                    "session-scoped ONLY; lane-scoped arming is unsupported by "
                    "construction (there is no waiter_lane_id parameter)."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "condition_kind": ParameterMetadata(
                description="lane_closed | session_terminal | deadline.",
                required=True,
                type=ParameterType.STRING,
            ),
            "condition_ref": ParameterMetadata(
                description=(
                    "The condition_kind's referent: a lane_id (lane_closed), "
                    "an agent_instance_id (session_terminal), or an ISO-8601 "
                    "timestamp (deadline). Shape-checked per kind."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "arm_session_dependency outcome (drive-on-delivery lane rider) — "
            "the armed wake edge."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="arm_session_dependency outcome",
            properties={
                "waiter_instance_id": ParameterMetadata(type=ParameterType.STRING),
                "condition_kind": ParameterMetadata(type=ParameterType.STRING),
                "condition_ref": ParameterMetadata(type=ParameterType.STRING),
                "armed": ParameterMetadata(type=ParameterType.BOOLEAN),
            },
        ),
    )
    def arm_session_dependency(
        self, params: dict[str, Any], state: dict[str, Any],
    ) -> dict[str, Any]:
        """Rider verb (drive-on-delivery lane, slice 2, 2026-08-04) — the
        FIRST caller of the D1 ``session_dependency`` wake-edge machinery.
        See :func:`session_lifecycle_verbs.arm_session_dependency` for the
        full contract (session-scoped only, no waiter-existence check,
        per-kind ``condition_ref`` shape validation)."""
        del state
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        req = ArmSessionDependencyRequest(
            waiter_instance_id=str(raw.get("waiter_instance_id") or ""),
            condition_kind=str(raw.get("condition_kind") or ""),
            condition_ref=str(raw.get("condition_ref") or ""),
        )
        try:
            result = lifecycle_arm_session_dependency(state_service, req)
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="drain_session_claude_mapping_spool",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        output_type="object",
        output_description=(
            "T1 usage-capture lane — drains the SessionStart hook's "
            "file-per-firing spool into session_claude_mapping."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="drain_session_claude_mapping_spool outcome",
            properties={
                "files_seen": ParameterMetadata(type=ParameterType.INTEGER),
                "upserted": ParameterMetadata(type=ParameterType.INTEGER),
                "skipped_malformed": ParameterMetadata(type=ParameterType.INTEGER),
            },
        ),
    )
    def drain_session_claude_mapping_spool(
        self, params: dict[str, Any], state: dict[str, Any],
    ) -> dict[str, Any]:
        """T1 usage-capture lane (ruling 2026-08-05) — testable/on-demand
        entry point for :func:`session_claude_mapping_ingest.
        drain_session_claude_mapping_spool`; the SAME function is also
        called directly from ``_run_session_lifecycle_sweep`` (the sweep-tick
        wiring the ruling requires — a verb nobody calls is bound-in-name-only)."""
        del params, state
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        result = lifecycle_drain_session_claude_mapping_spool(state_service)
        return _success_result(data=result)

    @platform_process(
        name="list_sessions",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "lane_id": ParameterMetadata(
                description="Filter to one lane_id.", required=False, type=ParameterType.STRING,
            ),
            "work_class": ParameterMetadata(
                description="Filter to one work_class.", required=False, type=ParameterType.STRING,
            ),
            "host": ParameterMetadata(
                description="Filter to one host.", required=False, type=ParameterType.STRING,
            ),
            "lifecycle_state": ParameterMetadata(
                description="Filter to one lifecycle_state.",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="§4 list_sessions — the ONE fleet list.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="list_sessions outcome",
            properties={
                "sessions": ParameterMetadata(type=ParameterType.LIST),
            },
        ),
    )
    def list_sessions(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        """§4 ``list_sessions`` — the ONE fleet list (operator-managed rows
        included by construction, via the normal registration path)."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        filters = {
            key: str(raw[key])
            for key in ("lane_id", "work_class", "host", "lifecycle_state")
            if raw.get(key)
        }
        return _success_result(data=lifecycle_list_sessions(state_service, filters or None))

    @platform_process(
        name="budget_report",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "lane_id": ParameterMetadata(
                description="Filter to one lane_id.", required=False, type=ParameterType.STRING,
            ),
            "budget_line": ParameterMetadata(
                description="Filter to one budget_line.",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "T1 S3 -- per-budget_line token-usage rollup, joining managed_session/"
            "session_claude_mapping against session_ledger's session/event tables."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="budget_report outcome",
            properties={
                "budget_lines": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "One entry per distinct budget_line among matching "
                        "managed_session rows. Each entry: budget_line (str), "
                        "sessions_covered (int, contributed >=1 usage-bearing "
                        "session_ledger event), sessions_uncovered (int, no "
                        "mapping row or no ledger usage events -- the S2c "
                        "absence-detection population), as_of (ISO-8601 str or "
                        "null -- the latest event_at among included usage "
                        "events; null when sessions_covered is 0), usage "
                        "(dict[str, number] -- per-field sums of whatever "
                        "numeric keys actually appear in the vendor's verbatim "
                        "usage_json, no fixed schema), and by_model (dict "
                        "keyed on managed_session.model, empty string for "
                        "unset, each value the same "
                        "sessions_covered/sessions_uncovered/as_of/usage shape "
                        "scoped to that model)."
                    ),
                ),
            },
        ),
    )
    def budget_report(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        """T1 S3 -- read-only token-usage rollup per budget_line. See
        ``budget_report.py``'s module docstring for the join mechanism (the
        first cross-plugin state read against session_ledger's own tables)
        and the seat's three S3 design rails (staleness marker, coverage
        disclosure, compile-time schema coupling)."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        result = lifecycle_build_budget_report(
            state_service,
            lane_id=str(raw.get("lane_id") or ""),
            budget_line=str(raw.get("budget_line") or ""),
        )
        return _success_result(data=result)

    @platform_process(
        name="list_session_claude_mappings",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The managed_session whose mapping rows to list.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "usage-capture-attribution D2 follow-on -- every live "
            "session_claude_mapping row observed for one agent_instance_id."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="list_session_claude_mappings outcome",
            properties={
                "mappings": ParameterMetadata(
                    type=ParameterType.LIST,
                    description=(
                        "Every live session_claude_mapping row for the given "
                        "agent_instance_id, oldest-observation-order NOT "
                        "guaranteed (callers needing order sort by "
                        "captured_at themselves). Each row: agent_instance_id "
                        "(str), claude_session_id (str, the Claude Code "
                        "session_id this firing captured), captured_at "
                        "(ISO-8601 str, the hook payload's own timestamp), "
                        "capture_source (str: hook:startup | hook:clear | "
                        "hook:resume | init_event), plus the standard "
                        "state-layer row fields (id, namespace, created_at, "
                        "updated_at, created_by, updated_by, name, "
                        "is_deleted, external_id) every state-managed table "
                        "carries. An empty list means no mapping has EVER "
                        "been observed for this worker (SessionStart hook "
                        "never fired, or the worker predates the capture "
                        "landing) -- not an error."
                    ),
                ),
            },
        ),
    )
    def list_session_claude_mappings(
        self, params: dict[str, Any], state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """usage-capture-attribution D2 follow-on (workbench
        2026-08-06_usage_capture_attribution_findings_usage-capture-impl.md)
        -- a read-only listing verb over session_claude_mapping, named
        during that lane's D1 diagnosis as the missing piece that forced
        inference instead of measurement. Read-only, issues no writes;
        thin wrapper over the same store-layer function budget_report.py
        already uses internally."""
        raw = params.get("parameters", params)
        agent_instance_id = str(raw.get("agent_instance_id") or "")
        if not agent_instance_id:
            return _failure_result(
                code="missing_agent_instance_id",
                message="list_session_claude_mappings requires a non-empty agent_instance_id.",
            )
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        mappings = lifecycle_list_session_claude_mappings(state_service, agent_instance_id)
        return _success_result(data={"mappings": mappings})

    @platform_process(
        name="session_status",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The managed_session to look up.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="§4 session_status — the ledger row.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="The live managed_session row.",
            properties={
                "agent_instance_id": ParameterMetadata(type=ParameterType.STRING),
                "agent_session_id": ParameterMetadata(type=ParameterType.STRING),
                "agent_id": ParameterMetadata(type=ParameterType.STRING),
                "spawned_by_instance_id": ParameterMetadata(type=ParameterType.STRING),
                "spawned_by_role": ParameterMetadata(type=ParameterType.STRING),
                "lane_id": ParameterMetadata(type=ParameterType.STRING),
                "brief_ref": ParameterMetadata(type=ParameterType.STRING),
                "model": ParameterMetadata(type=ParameterType.STRING),
                "effort": ParameterMetadata(type=ParameterType.STRING),
                "work_class": ParameterMetadata(type=ParameterType.STRING),
                "budget_line": ParameterMetadata(type=ParameterType.STRING),
                "visibility": ParameterMetadata(type=ParameterType.STRING),
                "host": ParameterMetadata(type=ParameterType.STRING),
                "host_ref": ParameterMetadata(type=ParameterType.STRING),
                "capability_report": ParameterMetadata(type=ParameterType.OBJECT),
                "report_by": ParameterMetadata(type=ParameterType.STRING),
                "expires_at": ParameterMetadata(type=ParameterType.STRING),
                "lifecycle_state": ParameterMetadata(type=ParameterType.STRING),
                "last_transition_at": ParameterMetadata(type=ParameterType.STRING),
                "directed_by": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def session_status(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        """§4 ``session_status`` — the ledger row. (Host-liveness enrichment
        is deferred to whichever caller has the host driver registry; this
        verb's contract is the ledger truth, always available.)"""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        try:
            row = lifecycle_session_status(state_service, str(raw.get("agent_instance_id", "")))
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=row)

    @platform_process(
        name="clear_session",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The managed_session to clear.",
                required=True,
                type=ParameterType.STRING,
            ),
            "park": ParameterMetadata(
                description=(
                    "When true, also drives live/idle/overdue -> parked "
                    "(L3 rule 2, steward direction) after the clear is sent."
                ),
                required=False,
                type=ParameterType.BOOLEAN,
            ),
        },
        output_type="object",
        output_description="§4 clear_session (AMEND 5b) — context hygiene via the driver channel.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="clear_session outcome",
            properties={
                "lifecycle_state": ParameterMetadata(type=ParameterType.STRING),
                "parked": ParameterMetadata(type=ParameterType.BOOLEAN),
            },
        ),
    )
    def clear_session(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """§4 ``clear_session`` (AMEND 5b) — fire-and-forget ``/clear`` over
        the resolved host driver's channel; ``park=True`` additionally
        drives the row to ``parked`` (the only writer of that edge)."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        try:
            result = lifecycle_clear_session(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                park=bool(raw.get("park", False)),
                directed_by=format_directed_by(state.get("call_context")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="compact_session",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The managed_session to compact.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "§4 compact_session (AMEND 5b) — context hygiene via the driver channel."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="compact_session outcome",
            properties={
                "lifecycle_state": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def compact_session(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:  # noqa: ARG002
        """§4 ``compact_session`` (AMEND 5b) — fire-and-forget ``/compact``
        over the driver channel; no park mode, no lifecycle transition."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        try:
            result = lifecycle_compact_session(
                state_service, agent_instance_id=str(raw.get("agent_instance_id", "")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="drive_session",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The managed_session to dispatch the work turn into.",
                required=True,
                type=ParameterType.STRING,
            ),
            "text": ParameterMetadata(
                description=(
                    "The work turn to send over the driver channel — a "
                    "self-contained dispatch (brief text or a workbench "
                    "brief pointer plus instructions). Must be non-empty."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "drive_session (D2-window rider) — work dispatch via the driver channel."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="drive_session outcome",
            properties={
                "lifecycle_state": ParameterMetadata(type=ParameterType.STRING),
                "unparked": ParameterMetadata(type=ParameterType.BOOLEAN),
            },
        ),
    )
    def drive_session(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """``drive_session`` (D2-window rider, 2026-08-04) — fire-and-forget
        work dispatch over the resolved host driver's channel; owns the §3.2
        ``parked -> live`` edge and re-arms ``report_by`` on every dispatch."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        try:
            result = lifecycle_drive_session(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                text=str(raw.get("text", "")),
                directed_by=format_directed_by(state.get("call_context")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="terminate_session",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The managed_session to terminate.",
                required=True,
                type=ParameterType.STRING,
            ),
            "grace_seconds": ParameterMetadata(
                description=(
                    "Seconds to wait for a graceful host-level stop before "
                    "SIGKILL. Default 30."
                ),
                required=False,
                type=ParameterType.INTEGER,
            ),
        },
        output_type="object",
        output_description=(
            "§4 terminate_session — graceful stop -> kill after grace -> "
            "ledger -> terminated. Also fires + best-effort delivers any "
            "armed session_terminal dependency edges waiting on this "
            "session, on both the transition and already-terminal paths."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="terminate_session outcome",
            properties={
                "already_terminal": ParameterMetadata(type=ParameterType.BOOLEAN),
                "lifecycle_state": ParameterMetadata(type=ParameterType.STRING),
                "session_terminal_edges_fired": ParameterMetadata(type=ParameterType.INTEGER),
            },
        ),
    )
    def terminate_session(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """§4 ``terminate_session`` — resolves the row's host driver and
        calls its real ``terminate()`` (driver-level stop, SIGKILL after
        grace) BEFORE the ledger write, so the ledger never claims
        ``terminated`` over a process still running. ``host='operator'``
        rows (degenerate driver, never spawned by us) still land the ledger
        transition — see ``session_lifecycle_verbs.terminate_session``'s
        docstring for why that's not a silent degradation. Idempotent on an
        already-terminal row."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        grace_raw = raw.get("grace_seconds")
        try:
            result = lifecycle_terminate_session(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                directed_by=format_directed_by(state.get("call_context")),
                **({"grace_seconds": int(grace_raw)} if grace_raw is not None else {}),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="retire_session",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The managed_session to retire (the lane-landing verb).",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "§4 retire_session — terminate + release + fire dependency edges + retired."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="retire_session outcome",
            properties={
                "already_retired": ParameterMetadata(type=ParameterType.BOOLEAN),
                "dependencies_fired": ParameterMetadata(type=ParameterType.INTEGER),
            },
        ),
    )
    def retire_session(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """§4 ``retire_session`` — the lane-landing verb; four idempotent
        steps, re-drivable by construction (session_lifecycle_verbs module
        docstring)."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        try:
            result = lifecycle_retire_session(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                directed_by=format_directed_by(state.get("call_context")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="report_alive",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The reporting managed_session.",
                required=True,
                type=ParameterType.STRING,
            ),
            "status": ParameterMetadata(
                description="working | idle.", required=True, type=ParameterType.STRING,
            ),
            "status_note": ParameterMetadata(
                description="Optional free-text note, recorded on the audit trail.",
                required=False,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description="§4 report_alive — re-arms report_by; status drives live<->idle.",
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="report_alive outcome",
            properties={
                "lifecycle_state": ParameterMetadata(type=ParameterType.STRING),
                "recovered": ParameterMetadata(type=ParameterType.BOOLEAN),
            },
        ),
    )
    def report_alive(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """§4 ``report_alive`` — re-arms ``report_by``; a late report from
        ``overdue`` recovers and sets ``recovered=True``."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        try:
            result = lifecycle_report_alive(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                status=str(raw.get("status", "")),
                status_note=str(raw.get("status_note", "") or ""),
                directed_by=format_directed_by(state.get("call_context")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="rotate_session",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The ledger agent_instance_id to rotate (from list_sessions — "
                "NEVER a peer_list/role-thread watch id).",
                required=True,
                type=ParameterType.STRING,
            ),
            "role_name": ParameterMetadata(
                description="The durable role this ledger row currently holds — used for "
                "the durable pickup dispatch (peer_send_by_name).",
                required=True,
                type=ParameterType.STRING,
            ),
            "pickup_text": ParameterMetadata(
                description="Pickup pointer driven as the post-clear turn (e.g. pointing "
                "at the worker's own handoff note + inbox).",
                required=True,
                type=ParameterType.STRING,
            ),
            "park_first": ParameterMetadata(
                description="Pass through to clear_session's park flag.",
                required=False,
                type=ParameterType.BOOLEAN,
            ),
        },
        output_type="object",
        output_description=(
            "maintenance-verbs M1, D0.3-ratified deferred-completion shape — dispatches "
            "a rotate_session choreography job and returns immediately; poll "
            "check_choreography_job_status for the outcome."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="rotate_session dispatch outcome",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING),
                "status": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def rotate_session(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """§2.1 ``rotate_session`` — ms-scale dispatch only (D0.3 mechanic 1);
        the actual clear/drive/verify choreography runs in the plugin's own
        single dedicated background worker (never inline in this handler)."""
        raw = params.get("parameters", params)
        job_manager = self._try_acquire_async_job_manager()
        if job_manager is None:
            return _failure_result(
                code="async_job_manager_unavailable",
                message="AsyncJobManager is not available on this homunculus.",
            )
        req = RotateSessionDispatchRequest(
            agent_instance_id=str(raw.get("agent_instance_id", "")),
            role_name=str(raw.get("role_name", "")),
            pickup_text=str(raw.get("pickup_text", "")),
            park_first=bool(raw.get("park_first", False)),
        )
        try:
            result = dispatch_rotate_session(job_manager, req, state)
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="restart_session",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The dying worker's ledger agent_instance_id.",
                required=True,
                type=ParameterType.STRING,
            ),
            "role_name": ParameterMetadata(
                description="The durable role the fresh spawn must reclaim.",
                required=True,
                type=ParameterType.STRING,
            ),
            "role_class": ParameterMetadata(
                description="The role_class to spawn under (managed_session carries no "
                "role_class column of its own — required from the caller).",
                required=True,
                type=ParameterType.STRING,
            ),
            "grace_seconds": ParameterMetadata(
                description="Pass through to terminate_session's grace_seconds.",
                required=False,
                type=ParameterType.INTEGER,
            ),
        },
        output_type="object",
        output_description=(
            "maintenance-verbs M1, D0.3-ratified deferred-completion shape — dispatches "
            "a restart_session choreography job and returns immediately; poll "
            "check_choreography_job_status for the outcome."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="restart_session dispatch outcome",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING),
                "status": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def restart_session(self, params: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        """§2.2 ``restart_session`` — ms-scale dispatch only (D0.3 mechanic 1);
        the actual terminate/spawn/role-reclaim/verify choreography runs in
        the plugin's own single dedicated background worker."""
        raw = params.get("parameters", params)
        job_manager = self._try_acquire_async_job_manager()
        if job_manager is None:
            return _failure_result(
                code="async_job_manager_unavailable",
                message="AsyncJobManager is not available on this homunculus.",
            )
        req = RestartSessionDispatchRequest(
            agent_instance_id=str(raw.get("agent_instance_id", "")),
            role_name=str(raw.get("role_name", "")),
            role_class=str(raw.get("role_class", "")),
            grace_seconds=int(raw.get("grace_seconds", 30) or 30),
        )
        try:
            result = dispatch_restart_session(job_manager, req, state)
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="check_choreography_job_status",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "job_id": ParameterMetadata(
                description="The job_id returned by rotate_session/restart_session.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "maintenance-verbs M1 — the caller-side polling answer for a "
            "rotate_session/restart_session job (the check_generation_status "
            "precedent); these jobs configure no completion_handlers, so this "
            "poll is the only way a direct caller learns the outcome."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="Choreography job status",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING),
                "status": ParameterMetadata(type=ParameterType.STRING),
                "progress_percent": ParameterMetadata(type=ParameterType.INTEGER),
                "result": ParameterMetadata(type=ParameterType.OBJECT),
                "error": ParameterMetadata(type=ParameterType.OBJECT),
            },
        ),
    )
    def check_choreography_job_status(
        self, params: dict[str, Any], state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """Read-only poll of a rotate_session/restart_session/
        generate_curation_report job's ledger row + terminal payload, if
        any — a generic ``AsyncJobManager`` job-row reader, not scoped to
        any one action name."""
        raw = params.get("parameters", params)
        job_manager = self._try_acquire_async_job_manager()
        if job_manager is None:
            return _failure_result(
                code="async_job_manager_unavailable",
                message="AsyncJobManager is not available on this homunculus.",
            )
        try:
            result = lifecycle_check_choreography_job_status(
                job_manager, str(raw.get("job_id", "")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="generate_curation_report",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "head_lines": ParameterMetadata(
                description="The current curated head's lines, already split by the "
                "caller (index_render.split_head's output) — this plugin cannot import "
                "that local-CLI-only module itself.",
                required=True,
                type=ParameterType.LIST,
            ),
            "bottom_n": ParameterMetadata(
                description="How many lowest-activation demotion candidates to return.",
                required=False,
                type=ParameterType.INTEGER,
            ),
            "byte_budget": ParameterMetadata(
                description="The head's byte budget (index_render.DEFAULT_BYTE_BUDGET, "
                "17000 as of M2.2 — kept in sync by convention until M2.3's index-"
                "manifest record removes the need for this duplication).",
                required=False,
                type=ParameterType.INTEGER,
            ),
            "line_budget": ParameterMetadata(
                description="The head's line budget (index_render.DEFAULT_LINE_BUDGET, "
                "132 as of M2.2 — same sync caveat as byte_budget).",
                required=False,
                type=ParameterType.INTEGER,
            ),
        },
        output_type="object",
        output_description=(
            "maintenance-verbs M2.2, D0.3-ratified deferred-completion shape — dispatches "
            "an activation-ranked curation-report job and returns immediately; poll "
            "check_choreography_job_status for the outcome."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="generate_curation_report dispatch outcome",
            properties={
                "job_id": ParameterMetadata(type=ParameterType.STRING),
                "status": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def generate_curation_report(
        self, params: dict[str, Any], state: dict[str, Any],
    ) -> dict[str, Any]:
        """M2.2 ``generate_curation_report`` — ms-scale dispatch only (D0.3
        mechanic 1, same shape as rotate/restart_session); the actual
        memory_service query + ranking runs in the plugin's own single
        dedicated background worker (never inline in this handler)."""
        raw = params.get("parameters", params)
        job_manager = self._try_acquire_async_job_manager()
        if job_manager is None:
            return _failure_result(
                code="async_job_manager_unavailable",
                message="AsyncJobManager is not available on this homunculus.",
            )
        head_lines_raw = raw.get("head_lines")
        head_lines = tuple(str(x) for x in head_lines_raw) if isinstance(head_lines_raw, list) else ()
        req = GenerateCurationReportDispatchRequest(
            head_lines=head_lines,
            bottom_n=int(raw.get("bottom_n") or 10),
            byte_budget=int(raw.get("byte_budget") or 17_000),
            line_budget=int(raw.get("line_budget") or 132),
        )
        try:
            result = dispatch_generate_curation_report(job_manager, req, state)
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="reinforce_by_slug",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "slug": ParameterMetadata(
                description="The memory fact's slug — its local file name minus '.md' "
                "(e.g. 'feedback_operator_delegates_routine_operations_end_to_end'). "
                "Resolved to the canonical memory_id server-side via the fact's own "
                "slot tag; never pass a memory_id here.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "maintenance-verbs M2.2 — resolve a memory fact's slug to its canonical "
            "memory_id via the slot tag convention, then reinforce it (ACT-R activation "
            "boost). Use when a fact is actually applied — cited in an incident, invoked "
            "in a review — never on a schedule or automatically."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="reinforce_by_slug outcome",
            properties={
                "memory_id": ParameterMetadata(type=ParameterType.STRING),
                "slug": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def reinforce_by_slug(
        self, params: dict[str, Any], state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """M2.2 cite->reinforce wiring: resolves ``slug`` to a ``memory_id``
        via ``get_memories_by_tag`` on the fact's own slot tag (never a local
        export file — this plugin calls the injected ``memory_service``
        directly, the same dependency-injection seam ``store_interaction``
        already uses elsewhere in this file), then reinforces it. This verb
        IS the wiring the charter asks for; WHEN to call it (citation
        detection) stays a human/agent judgment call this slice, not an
        automated hook — disclosed, not silently assumed."""
        raw = params.get("parameters", params)
        slug = str(raw.get("slug", "")).strip()
        if not slug:
            return _failure_result(
                code="missing_argument", message="reinforce_by_slug requires a non-empty slug.",
            )
        if self._memory_service is None:
            return _failure_result(
                code="memory_service_unavailable",
                message="memory_service is not bound on this homunculus.",
            )
        homunculus_name = _resolve_homunculus_name_for_memory_tags()
        if not homunculus_name:
            return _failure_result(
                code="homunculus_name_unset",
                message="Could not resolve a homunculus name to resolve a slug's slot tag "
                "-- HOMUNCULUS_NAME is unset, root_manifest.yaml is unreadable or still "
                "carries its unwritten placeholder, and CLAUDE_PROJECT_DIR is unset (the "
                "final fallback needs it too).",
            )
        tag = slug_to_slot_tag(homunculus_name, slug)
        lookup = self._memory_service.get_memories_by_tag(tag=tag)
        matches = lookup.get("memories") if isinstance(lookup, dict) else None
        try:
            memory_id = resolve_memory_id_by_slug(matches if isinstance(matches, list) else [], slug)
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        self._memory_service.reinforce(memory_id=memory_id)
        return _success_result(data={"memory_id": memory_id, "slug": slug})

    @platform_process(
        name="report_context_status",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The reporting session's own id (ledger id for a worker; "
                "its own AGENT_INSTANCE_ID for the seat).",
                required=True,
                type=ParameterType.STRING,
            ),
            "claude_session_id": ParameterMetadata(
                description="The Claude Code session_id this snapshot was measured against.",
                required=True,
                type=ParameterType.STRING,
            ),
            "model": ParameterMetadata(
                description="Transcript message.model at measurement time.",
                required=True,
                type=ParameterType.STRING,
            ),
            "current_tokens": ParameterMetadata(
                description="input+cache_creation+cache_read tokens from the most recent turn.",
                required=True,
                type=ParameterType.INTEGER,
            ),
            "ceiling": ParameterMetadata(
                description="rotation_thresholds.resolve_ceiling(model) at measurement time.",
                required=True,
                type=ParameterType.INTEGER,
            ),
            "measured_at": ParameterMetadata(
                description="When the reporting hook computed this snapshot (ISO timestamp).",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "maintenance-verbs M1 — overwrite the caller's own latest "
            "context-status snapshot (shape (a) cache write)."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="report_context_status outcome",
            properties={
                "status": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def report_context_status(
        self, params: dict[str, Any], state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """maintenance-verbs M1 — plain state upsert of a measurement the
        CALLER already took client-side; this handler does no file/subprocess
        I/O of its own (born-async-clean, no D0.3 dependency — sanctioned
        ms-scale state work)."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        try:
            result = lifecycle_report_context_status(
                state_service,
                agent_instance_id=str(raw.get("agent_instance_id", "")),
                claude_session_id=str(raw.get("claude_session_id", "")),
                model=str(raw.get("model", "")),
                current_tokens=int(raw.get("current_tokens", 0) or 0),
                ceiling=int(raw.get("ceiling", 0) or 0),
                measured_at=str(raw.get("measured_at", "")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

    @platform_process(
        name="session_context_status",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_instance_id": ParameterMetadata(
                description="The session to read the cached context-status snapshot for.",
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "maintenance-verbs M1 — the cached context-window occupancy for "
            "one session; resolved=False (never a raised error) when no "
            "report has landed for it yet."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="session_context_status outcome",
            properties={
                "resolved": ParameterMetadata(type=ParameterType.BOOLEAN),
                "resolution_error": ParameterMetadata(type=ParameterType.STRING),
                "agent_instance_id": ParameterMetadata(type=ParameterType.STRING),
                "claude_session_id": ParameterMetadata(type=ParameterType.STRING),
                "model": ParameterMetadata(type=ParameterType.STRING),
                "current_tokens": ParameterMetadata(type=ParameterType.INTEGER),
                "ceiling": ParameterMetadata(type=ParameterType.INTEGER),
                "fraction": ParameterMetadata(type=ParameterType.FLOAT),
                "per_prompt_carriage_estimate_tokens": ParameterMetadata(
                    type=ParameterType.INTEGER,
                ),
                "rotation_due": ParameterMetadata(type=ParameterType.BOOLEAN),
                "measured_at": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def session_context_status(
        self, params: dict[str, Any], state: dict[str, Any],  # noqa: ARG002
    ) -> dict[str, Any]:
        """maintenance-verbs M1 — trivial state read of the cached snapshot
        `report_context_status` writes; this handler never reads a
        transcript or resolves a path itself."""
        raw = params.get("parameters", params)
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        try:
            result = lifecycle_session_context_status(
                state_service, agent_instance_id=str(raw.get("agent_instance_id", "")),
            )
        except VerbError as exc:
            return _failure_result(code=exc.code, message=exc.message)
        return _success_result(data=result)

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
            "still holds the role, its resolved stable session id, and whether the "
            "role's CURRENT holder has a delivery route attached."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="peer_holds_role outcome (§5.0 act-time ownership re-check)",
            properties={
                "holds": ParameterMetadata(type=ParameterType.BOOLEAN),
                "name": ParameterMetadata(type=ParameterType.STRING),
                "agent_session_id": ParameterMetadata(type=ParameterType.STRING),
                "delivery_route_attached": ParameterMetadata(
                    type=ParameterType.BOOLEAN,
                ),
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
            data={
                "holds": holds,
                "name": name,
                "agent_session_id": agent_session_id,
                "delivery_route_attached": self._role_delivery_route_attached(
                    state_service, name,
                ),
            },
        )

    @platform_process(
        name="peer_mark_role_covered",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "name": ParameterMetadata(
                description="Role name to advance the covered mark for.",
                required=True,
                type=ParameterType.STRING,
            ),
            "message_id": ParameterMetadata(
                description=(
                    "The arm-... message_id of the NEWEST role message this "
                    "session has processed for 'name'. The server looks this "
                    "row up and attests ITS OWN (created_at, id) — a caller "
                    "can only ever name a pair that corresponds to a row "
                    "that exists (pull-surface boundary design §2)."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
        },
        output_type="object",
        output_description=(
            "The stored role_covered_mark after this attestation — the "
            "PRE-EXISTING mark unchanged on a monotonic no-op."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description="peer_mark_role_covered outcome (design §2).",
            properties={
                "recipient_key": ParameterMetadata(type=ParameterType.STRING),
                "covered_created_at": ParameterMetadata(type=ParameterType.STRING),
                "covered_id": ParameterMetadata(type=ParameterType.STRING),
                "covered_message_id": ParameterMetadata(type=ParameterType.STRING),
                "attested_at": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def peer_mark_role_covered(
        self,
        params: dict[str, Any],
        state: dict[str, Any],
    ) -> dict[str, Any]:
        """Attest ``name`` covered through ``message_id`` (design §2, R1).

        **R1 — REGISTERED-ROUTE-ONLY, no exceptions.** This is a WRITE whose
        wrong advance is silent loss for the NEXT holder (the strong class,
        per Architect's ruling — the same shape as the measured
        ``peer_claim_role`` instance-id finding). Unlike ``peer_holds_role``
        (a READ, whose mistake-cost falls on the caller), this verb takes NO
        caller-supplied identity argument at all, ever — not even one
        resolved server-side. Identity is sourced EXCLUSIVELY from
        ``state["inference_vertex_session_id"]``: the calling BRIDGE's own
        ``agent_instance_id``, stamped by ``ActionProcessor
        ._lift_inference_vertex_identity`` ONLY for a call dispatched through
        a registered bridge's ``process_call`` (``PlatformSurface
        ._build_process_call_trigger_data``). A caller arriving over an
        unregistered route — a one-shot ``homunculus call`` from the local
        CLI, which stamps the DIFFERENT ``caller_attribution_*`` family
        instead (§34.6) — is refused loud with ``unregistered_route``. That
        family is deliberately NEVER consulted here, even as a fallback.

        Role ownership is re-checked LIVE, at attestation time (not claim
        time) — a displaced prior holder attesting after being displaced
        could otherwise advance the mark past mail the NEW holder never saw,
        the identical silent-loss shape the watch-spool mark was ruled out
        for.
        """
        raw = params.get("parameters", params)
        name = str(raw.get("name", "")).strip()
        message_id = str(raw.get("message_id", "")).strip()
        if not name or not message_id:
            return _failure_result(
                code="missing_argument",
                message="peer_mark_role_covered requires non-empty 'name' and 'message_id'.",
            )
        caller_instance_id = str(state.get("inference_vertex_session_id") or "").strip()
        if not caller_instance_id:
            return _failure_result(
                code="unregistered_route",
                message=(
                    "peer_mark_role_covered requires a call dispatched through "
                    "a registered bridge's process_call; a one-shot homunculus "
                    "call carries no registered-route identity to attest with."
                ),
            )
        state_service = self._get_state_service()
        if state_service is None:
            return _failure_result(
                code="state_service_unavailable",
                message="state_service is not bound on this homunculus.",
            )
        agent_session_id = self._claimant_session_id(caller_instance_id)
        if not agent_session_id:
            return _failure_result(
                code="identity_not_registered",
                message=f"no live peer_binding for instance {caller_instance_id!r}.",
            )
        if not holds_role(state_service, name, agent_session_id):
            return _failure_result(
                code="peer_role_not_held",
                message=(
                    f"the calling session does not currently hold role {name!r} "
                    "— re-check ownership before attesting."
                ),
            )
        try:
            binding = resolve_role_binding(state_service, name)
            session_label = binding.session_label
        except Exception:  # noqa: BLE001 — audit field only, best-effort
            session_label = ""
        service = self._require_service()
        try:
            mark = service.mark_role_covered(
                recipient_key=name,
                message_id=message_id,
                attested_by_agent_instance_id=caller_instance_id,
                attested_by_agent_session_id=agent_session_id,
                attested_by_session_label=session_label,
            )
        except AgentRequestInvalidError as exc:
            return _failure_result(code="role_message_not_found", message=str(exc))
        return _success_result(
            data={
                "recipient_key": mark.recipient_key,
                "covered_created_at": mark.covered_created_at,
                "covered_id": mark.covered_id,
                "covered_message_id": mark.covered_message_id,
                "attested_at": mark.attested_at,
            },
        )

    def _role_delivery_route_attached(
        self, state_service: Any, name: str,
    ) -> bool:
        """Does the role's CURRENT holder have a live bridge bound right now?

        A role binding outlives the session that claimed it, so
        ``holds=True`` can be reported for a role whose holder has no receiver
        left — the claim is durable, the route is not. This measures the route:
        the holder's stable session id resolves to a ``peer_binding`` row, and
        that row's bridge is open (an MCP bridge session or an armed ``watch``
        long-poll — both are the same kind of attachment here).

        Named for what it measures. NOT ``receiving``: on MCP transport a route
        can be attached while no waker ever fires, so a truthful name is the
        narrow one. False is also the honest answer for a vacant role and for a
        holder whose binding is gone.

        **Total by construction.** Every fault this lookup can raise —
        a duplicate binding for one session id, a malformed role row — is
        answered ``False`` rather than propagated. ``peer_holds_role`` is
        Git-Controller's Step-9.5 pre-commit ownership re-check: ``holds`` is
        the safety answer and must survive anything the route lookup does. An
        additive truth-in-reporting field that can convert that boolean into an
        exception would be a regression wearing an addition's clothes.

        Caveat, stated rather than engineered away: this reads the role binding
        a second time (``holds_role`` read it first), so a displacement landing
        between the two reads would report ``holds`` for one holder and the
        route of another. Fixing that would mean re-implementing ``holds_role``
        inline, and changing the Step-9.5 safety computation to improve an
        advisory field is the wrong trade. The window is one state read wide.
        """
        if self._peer_registry is None or self._bridge_manager is None:
            return False
        try:
            resolved = resolve_role_binding(state_service, name)
            binding = self._peer_registry.resolve_by_agent_session_id(
                resolved.agent_session_id,
            )
        except (
            RoleBindingVacantError,
            RoleBindingMalformedError,
            PeerSessionAmbiguousError,
        ):
            return False
        if binding is None:
            return False
        bridge = self._bridge_manager.get(binding.bridge_id)
        return bridge is not None and not bridge.closed

    @platform_process(
        name="peer_inbox",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={
            "agent_session_id": ParameterMetadata(
                description=(
                    "The caller's OWN stable session id (ases-...), exported by "
                    "the fleet launcher as $AGENT_SESSION_ID and echoed by "
                    "peer_register / current_identity / the watcher's armed line. "
                    "The agent_id and agent_instance_id whose mail is read are "
                    "resolved server-side from this session's live peer_binding "
                    "row — a caller cannot name someone else's inbox."
                ),
                required=True,
                type=ParameterType.STRING,
            ),
            "after": ParameterMetadata(
                description=(
                    "Instance-section cursor ONLY: an ISO-8601 timestamp, echo "
                    "back the previous page's next_after_created_at. It does "
                    "NOT page the role section — that is 'role_after'."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "role_after": ParameterMetadata(
                description=(
                    "Role-section cursor ONLY: the opaque token from the "
                    "previous page's next_role_cursor, echoed back verbatim. "
                    "Independent of 'after'; the two are never mixed. A "
                    "malformed token fails the role section closed."
                ),
                required=False,
                type=ParameterType.STRING,
            ),
            "limit": ParameterMetadata(
                description=(
                    "Maximum entries per section, clamped to 1..100. Default 5 "
                    "— entries carry full message content (~4KB each measured "
                    "2026-08-01), so this is a page size, not a backlog size: "
                    "page with the two cursors instead of raising it."
                ),
                required=False,
                type=ParameterType.INTEGER,
                default=PEER_INBOX_DEFAULT_LIMIT,
            ),
        },
        output_type="object",
        output_description=(
            "One page of the caller's peer inbox: an instance section (entries "
            "+ next_after_created_at) and an independently-cursored role "
            "section (role_entries + next_role_cursor + its fault-domain "
            "status), plus the resolved recipient identity. Pull-surface "
            "boundary (design §5): role_floor_applied is True when the "
            "default drain's mark-bounded floor removed already-covered "
            "rows this call; role_history_cursor, populated only on a "
            "genuine floor-stop, is a deliberate-deep-read token."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description=(
                "peer_inbox page — the exact shape emitted by "
                "peer_inbox_view.serialize_peer_inbox_page (the same payload the "
                "localhost /peer/inbox route and the MCP peer_inbox tool return)"
            ),
            properties={
                "recipient_agent_id": ParameterMetadata(type=ParameterType.STRING),
                "recipient_agent_instance_id": ParameterMetadata(
                    type=ParameterType.STRING,
                ),
                "entries": ParameterMetadata(type=ParameterType.LIST),
                "next_after_created_at": ParameterMetadata(
                    type=ParameterType.STRING,
                ),
                "role_entries": ParameterMetadata(type=ParameterType.LIST),
                "next_role_cursor": ParameterMetadata(type=ParameterType.STRING),
                "role_section_status": ParameterMetadata(type=ParameterType.STRING),
                "role_section_error": ParameterMetadata(type=ParameterType.STRING),
                "role_floor_applied": ParameterMetadata(type=ParameterType.BOOLEAN),
                "role_history_cursor": ParameterMetadata(type=ParameterType.STRING),
            },
        ),
    )
    def peer_inbox_action(
        self,
        params: dict[str, Any],
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> dict[str, Any]:
        """PULL receive path — read this session's own peer mail on demand.

        Named ``peer_inbox_action``, not ``peer_inbox``: this plugin already
        carries the typed ``AgentMessagingServiceInterface.peer_inbox``
        delegation, so this is the platform's documented split-verb shape —
        ``ActionProcessor._execute_plugin_method`` resolves ``peer_inbox`` from
        the process key and then prefers the ``<verb>_action`` wrapper when it
        carries the decorator's ``_platform_process_metadata`` marker. The
        process key stays ``plugin::agent_messaging_plugin::peer_inbox``.

        Before this verb the ONLY read of the durable inbox was
        the ``GET .../peer/inbox`` bridge route, whose identity comes from the
        CALLING bridge's peer registration. ``homunculus call`` opens a fresh,
        unregistered bridge, so a no-MCP session had no pull path at all —
        streaming ``homunculus watch`` was the only receive, and a session
        without a live watcher simply could not read its backlog.

        Identity is therefore an explicit argument, but a caller may only name
        its OWN session: ``agent_session_id`` is looked up in ``peer_binding``
        and the recipient triple is taken from that row (the same three fields
        the route reads off its own binding). An unknown or duplicated session
        id is a loud error — never a silent read of an empty inbox.

        Deliberately NOT done here: this read does not retire the re-emit /
        escalation insurance on the rows it returns (the watcher long-poll ack
        and the MCP ``/peer/drain`` reconcile remain the two consumption
        authorities). Retiring insurance on a read that might not reach a model
        turn risks destroying content, which is the worse failure; a session
        that drains here may still see the same IMPORTANT rows re-emitted.
        """
        if not self._active or self._peer_registry is None:
            return _failure_result(
                code="bridge.not_running",
                message=(
                    "The agent messaging bridge is not active on this "
                    "homunculus, so no inbox can be read. This is NOT an empty "
                    "inbox — start the interface and retry."
                ),
            )
        raw = params.get("parameters", params)
        agent_session_id = str(raw.get("agent_session_id", "")).strip()
        if not agent_session_id:
            return _failure_result(
                code="missing_argument",
                message=(
                    "peer_inbox requires the caller's own non-empty "
                    "'agent_session_id' (the launcher exports it as "
                    "$AGENT_SESSION_ID)."
                ),
            )
        try:
            binding = self._peer_registry.resolve_by_agent_session_id(
                agent_session_id,
            )
        except PeerSessionAmbiguousError as exc:
            return _failure_result(
                code="peer_session_ambiguous",
                message=str(exc),
            )
        if binding is None:
            return _failure_result(
                code="identity_not_registered",
                message=(
                    f"no live peer_binding for agent_session_id "
                    f"{agent_session_id!r}. This usually means this session's "
                    f"watcher or bridge is no longer registered — re-arm it "
                    f"('<homunculus> watch --role <role>', or peer_register "
                    f"over MCP) and retry. Read this as 'the reader is "
                    f"unknown', never as 'the reader has no mail': the "
                    f"messages are durable and still waiting. A wrong "
                    f"agent_session_id produces this same error, so check the "
                    f"value came from $AGENT_SESSION_ID and not a stale note."
                ),
            )
        try:
            request = _build_peer_inbox_request(raw, binding)
        except ValueError as exc:
            return _failure_result(code="invalid_after", message=str(exc))
        try:
            page = self._require_service().peer_inbox(request)
        except AgentMessagingError as exc:
            return _failure_result(
                code="peer_inbox_rejected",
                message=str(exc),
            )
        # The caller proved liveness by reading; keep "last active" in step with
        # the delivery path, exactly as the /peer/inbox route does.
        self._peer_registry.touch_binding(binding.agent_instance_id)
        return _success_result(
            data=serialize_peer_inbox_page(page, binding.agent_instance_id),
        )

    @platform_process(
        name="peer_list",
        processor_policy_category=ProcessorPolicyCategory.EDGE,
        parameters={},
        output_type="object",
        output_description=(
            "A snapshot of every live peer registered on this homunculus: "
            "the sorted list of distinct agent_ids present, and per agent_id "
            "the list of its live instances (agent_instance_id, "
            "session_label, parent_pid, registered_at, created_at, "
            "updated_at)."
        ),
        return_value_schema=ReturnValueSchema(
            type=ParameterType.OBJECT,
            description=(
                "peer_list snapshot — the exact shape emitted by "
                "peer_list_view.serialize_peer_list (the same payload the "
                "localhost /peer/list route and the MCP peer_list tool "
                "return)"
            ),
            properties={
                "agent_ids": ParameterMetadata(type=ParameterType.LIST),
                "instances": ParameterMetadata(type=ParameterType.DICT),
            },
        ),
    )
    def peer_list(
        self,
        params: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
        state: dict[str, Any],  # noqa: ARG002  # pyright: ignore[reportUnusedParameter]
    ) -> dict[str, Any]:
        """No-MCP peer enumeration — closes the peer-enumeration asymmetry.

        WS-1a's ``peer_inbox`` gave a no-MCP session (``homunculus call``, no
        registered bridge) a way to read its OWN mail. It left a companion
        gap open: that same session had no way to see who ELSE was live —
        ``peer_list`` existed only as an MCP-bridge HTTP route and its
        Streamable/stdio MCP mirrors, all of which resolve identity from the
        CALLING bridge's registration, something ``homunculus call`` never
        has. This verb needs no such resolution: it is a global, unfiltered
        registry snapshot, identical for every caller regardless of identity
        — there is nothing to scope BY, so unlike ``peer_inbox`` it takes no
        arguments and does no per-caller lookup.

        No fencing beyond "reached this homunculus at all": localhost is the
        existing trust boundary for enumerating peers on this MCP surface
        (the pre-existing route and tool have never required more), and this
        verb decides that explicitly rather than inheriting it silently —
        see ``peer_list_view``'s module docstring for the field-set decision
        that keeps the same boundary from silently widening (``bridge_id``
        and ``agent_session_id`` stay unexposed, exactly as on the two
        pre-existing surfaces).
        """
        if not self._active or self._peer_registry is None:
            return _failure_result(
                code="bridge.not_running",
                message=(
                    "The agent messaging bridge is not active on this "
                    "homunculus, so no peer registry can be read. This is "
                    "NOT an empty registry — start the interface and retry."
                ),
            )
        snapshot = self._peer_registry.list_agent_ids()
        return _success_result(data=serialize_peer_list(snapshot))

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
        # Fleet session-management Phase B, D1 (§3.1): stamp role_class on
        # pre-Phase-B role rows + report (never auto-fix) any pre-existing
        # >1-named-role holder for the operator cleanup pass (Dawn ruling).
        role_class = backfill_role_class(state_service)
        role_class_stamped = role_class.get("stamped")
        role_class_violations = role_class.get("cardinality_violations")
        logger.info(
            "%s: role_class backfill status=%s (%d row(s) stamped, %d "
            "cardinality violation(s) reported)",
            self.name,
            role_class.get("status"),
            len(role_class_stamped) if isinstance(role_class_stamped, list) else 0,
            len(role_class_violations) if isinstance(role_class_violations, list) else 0,
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

        D2-window ruling (2026-08-04, pulled forward on measured runaway —
        ~25 redeliveries/hr into the seat vs the ~4/hr the earmark accepted):
        a PEER-SESSION holder has no protocol surface for raw vertex turns
        (the serve verb needs an ``icr-`` completion request id these lack),
        so while a peer session holds the slot the VERTEX lane always answers
        ``None`` — ``resolve_autonomic``'s existing ``None`` → DEFER flip then
        lands the turn in the durable no-loss queue instead of destroying it
        against a session that cannot act. Completion-request forwarding
        (``_forward_completion_request`` → ``get_inference_provider``) is a
        DIFFERENT path and still reaches peer-session holders — that class IS
        servable. Every provider this plugin can mint today is a peer session
        (``SessionInferenceProvider``), so the guard is unconditional here;
        the ad hoc inference capability that will properly own this slot
        registers a different provider kind and reworks this accessor when it
        lands (its designed home, per the 08-03 sitting).

        Returns ``None`` when the slot is VACANT
        (:class:`RoleBindingVacantError`) — the sub-slice-2 vacancy → DEFER
        flip, unchanged — and now also for a HELD slot, per the ruling above.
        """
        state = self._get_state_service()
        try:
            resolved = resolve_role_binding_v4(state, SYS_AUTONOMIC_SLOT)
        except RoleBindingVacantError:
            return None
        logger.info(
            "sys:autonomic vertex turn: slot held by peer session agi=%s — "
            "vertex lane DEFERs to the durable queue (peer sessions cannot "
            "serve raw vertex turns; D2-window ruling 2026-08-04)",
            resolved.agent_instance_id,
        )
        return None

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
        INF-06 forwarded-vertex re-drive + terminal-row GC + D1 session sweep.

        Each rider is fault-isolated so one fault never skips the rest of the
        tick: the INF-02 sweep can raise, so it is wrapped HERE (the sweeper's
        single outer guard would otherwise abort the tick before later riders
        run); the two INF-06 riders self-isolate (internal try/except → never
        raise, return counts) so they are called directly. Every rider runs
        every tick.

        A4 (2026-08-04): the REL-05 deaf-wake escalation rider (DirectWakeReconciler)
        retired here — sweep_overdue_sessions + _notify_steward_of_overdue
        (session_sweep.py, D1) is its sole successor, keyed off the recipient's
        own report_by promise instead of a message-level heuristic.
        """
        autonomic = self._autonomic_assignment
        if autonomic is not None:
            try:
                autonomic.completions.sweep_serve_timeouts()
            except Exception:  # noqa: BLE001 — one rider's fault must not skip the other
                logger.exception(
                    "serve-timeout sweep rider FAULTED; continuing",
                )
            # INF-06 reliability: re-drive forwarded vertices whose holder died /
            # timed out, then reap aged terminal 'failed' rows. Both self-isolate.
            autonomic.forwarded.sweep_serve_timeouts()
            autonomic.forwarded.gc_terminal_rows()
        try:
            self._run_session_lifecycle_sweep()
        except Exception:  # noqa: BLE001 — one rider's fault must not skip the others
            logger.exception("D1 session-lifecycle sweep FAULTED; sweeper continues")

    def _run_session_lifecycle_sweep(self) -> None:
        """D1 platform sweep rider (§3.4/§6 rule 3, Architect ratification #3):
        marks overdue sessions (+ best-effort steward notice, D2-lane-tail
        follow-up #3), fires+delivers armed 'deadline'/'lane_closed'
        dependency edges, and prunes stale session_role_claim rows. See
        ``session_sweep.py``'s module docstring for the exact scope boundary
        ('session_terminal' firing stays retire_session's job)."""
        state_service = self._get_state_service()
        # peer_registry/bridge_manager are OPTIONAL on this call (unlike the
        # two dependency sweeps below, which require them and are gated by
        # the `if` below) -- sweep_overdue_sessions must still mark overdue
        # rows on an early-boot tick before the bridge service is up; it
        # just skips the notify step internally when either is None.
        overdue = sweep_overdue_sessions(
            state_service, peer_registry=self._peer_registry, bridge_manager=self._bridge_manager,
        )
        if overdue:
            logger.info("D1 sweep: marked %d session(s) overdue", overdue)
        _run_session_claude_mapping_riders(state_service)
        if self._peer_registry is not None and self._bridge_manager is not None:
            fired = sweep_deadline_dependencies(
                state_service,
                peer_registry=self._peer_registry,
                bridge_manager=self._bridge_manager,
            )
            if fired:
                logger.info("D1 sweep: fired %d 'deadline' dependency edge(s)", fired)
            lane_fired = sweep_lane_closed_dependencies(
                state_service,
                peer_registry=self._peer_registry,
                bridge_manager=self._bridge_manager,
            )
            if lane_fired:
                logger.info("D1 sweep: fired %d 'lane_closed' dependency edge(s)", lane_fired)
            if self._session_role_claim_pruner is not None:
                pruned = self._session_role_claim_pruner.sweep(
                    state_service, peer_registry=self._peer_registry,
                )
                if pruned:
                    logger.info("D1 sweep: pruned %d stale session_role_claim row(s)", pruned)

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
            binding_liveness_window_s=bridge_config.binding_liveness_window_seconds,
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
        # D1 §3.4/§2 rule 4: the session-role-claim staleness pruner (Architect
        # ratification #3) rides the on_tick cadence — stateful (grace-window
        # tracking), so it is constructed once and held.
        self._session_role_claim_pruner = SessionRoleClaimPruner(
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
                    description=(
                        "'queued' when appended, or 'dropped_bridge_gone' "
                        "when the originating bridge has already closed."
                    ),
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
                    description=(
                        "'queued' when appended, or 'dropped_bridge_gone' "
                        "when the originating bridge has already closed."
                    ),
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
        sender_agent_session_id: str = "",
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
            # A2: the sender's stable session key, so a reply still resolves once
            # the instance id in this hint has rotated.
            sender_agent_session_id=sender_agent_session_id,
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
        manager.append_event(
            bridge.bridge_id, EVENT_POST_MESSAGE, envelope, meta=meta,
        )
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
        config = self._build_config()
        repository = AgentMessagingRepository(state_service)
        logger.info(
            "%s service constructed (allowed_backends=%s, max_message_bytes=%d)",
            self.name,
            list(config.allowed_backends),
            config.max_message_bytes,
        )
        return AgentMessagingService(
            repository=repository,
            state_service=state_service,
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
            # §34.6: the surface resolves an unregistered caller's attribution
            # key against the SAME registry peer_send_by_name routes over.
            # Built at _build_peer_registry, before this call site.
            peer_registry=self._peer_registry,
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
            # A bridge-delivery action is terminal by contract. Once the
            # originating bridge is gone there is no caller left to receive
            # either this payload or an inference-formatted explanation.
            # Returning a failure here used to route the EDGE_SINK through
            # process_error, assign it to sys:autonomic, and mint a durable
            # forwarded vertex that INF-06 re-drove forever. Record the
            # irreversible transport drop loudly, then complete terminally
            # with zero continuation actions.
            logger.warning(
                "%s: dropping %s for closed or missing bridge %s "
                "(source_process_key=%s)",
                self.name,
                event_type,
                bridge_id,
                str(params.get("source_process_key") or ""),
            )
            return _success_result(data={"status": "dropped_bridge_gone"})
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

    def _build_session_lifecycle_policy_config(self) -> _SessionLifecyclePolicyConfig:
        provider = self._resolve_config_provider()
        return _SessionLifecyclePolicyConfig(
            work_class_defaults=_as_work_class_defaults(
                _provider_get(provider, "work_class_defaults"),
            ),
            work_class_tool_allowlists=_as_work_class_tool_allowlists(
                _provider_get(provider, "work_class_tool_allowlists"),
            ),
            headless_permission_mode=_as_str(
                _provider_get(provider, "headless_permission_mode"), "bypassPermissions",
            ),
            default_fleet_transport=_as_str(
                _provider_get(provider, "default_fleet_transport"), "watch",
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


def _run_session_claude_mapping_riders(state_service: Any) -> None:
    """Split out of ``_run_session_lifecycle_sweep`` to keep it under the
    radon cc threshold (mirrors ``session_sweep.py``'s own
    ``_mark_one_overdue`` split and ``_spawn_session_request_from_params``
    above -- same "thin dispatch" rationale) -- the two T1 usage-capture
    riders that share the same no-peer_registry/no-bridge_manager-dependency
    call-site pattern as the overdue marking: ``drain_session_claude_mapping_spool``
    (ruling 2026-08-05, Q1(d)) and ``detect_hook_absent_sessions`` (S2c,
    named follow-up) both run every tick regardless of bridge availability.
    A verb nobody calls is bound-in-name-only. A module-level function
    (not a method) -- it touches no instance state, only ``state_service``."""
    drain_result = lifecycle_drain_session_claude_mapping_spool(state_service)
    if drain_result["upserted"] or drain_result["skipped_malformed"]:
        logger.info(
            "D1 sweep: session_claude_mapping spool drain -- files_seen=%d "
            "upserted=%d skipped_malformed=%d",
            drain_result["files_seen"], drain_result["upserted"],
            drain_result["skipped_malformed"],
        )
    # S2c: detects a genuinely BROKEN SessionStart hook installation (as
    # opposed to the cross-check's not-yet-fired case, which is not an error).
    hook_absent = lifecycle_detect_hook_absent_sessions(state_service)
    if hook_absent:
        logger.warning("D1 sweep: hook-absence detected for %d session(s)", hook_absent)


def _spawn_dispatch_overrides_from_params(raw: dict[str, Any]) -> dict[str, Any]:
    """The dispatch-override subset of ``spawn_session``'s params (host,
    model, effort, allowed_tools, permission_mode) — split out of
    ``_spawn_session_request_from_params`` to keep it under the radon cc
    threshold; these five fields share one purpose (per-spawn dispatch
    overrides), so grouping them is a real seam, not an arbitrary split."""
    return {
        "host": (str(raw["host"]) if raw.get("host") else None),
        "model": str(raw.get("model", "") or ""),
        "effort": str(raw.get("effort", "") or ""),
        "allowed_tools": _as_str_tuple(raw.get("allowed_tools"), default=()),
        "permission_mode": str(raw.get("permission_mode", "") or ""),
    }


def _spawn_session_request_from_params(
    raw: dict[str, Any], directed_by: str,
) -> SpawnSessionRequest:
    """Build the ``spawn_session`` verb's typed request from raw transport
    params. Split out of the ``spawn_session`` method so the transport shim
    stays a thin dispatch (radon cc)."""
    return SpawnSessionRequest(
        role_class=str(raw.get("role_class", "")),
        lane_id=str(raw.get("lane_id", "")),
        brief_ref=str(raw.get("brief_ref", "")),
        work_class=str(raw.get("work_class", "")),
        budget_line=str(raw.get("budget_line", "")),
        role_name=str(raw.get("role_name", "") or ""),
        visibility=str(raw.get("visibility", "") or ""),
        report_by_seconds=int(raw.get("report_by_seconds") or 0),
        ttl_seconds=int(raw.get("ttl_seconds") or 0),
        spawned_by_instance_id=str(raw.get("spawned_by_instance_id", "") or ""),
        spawned_by_role=str(raw.get("spawned_by_role", "") or ""),
        directed_by=directed_by,
        **_spawn_dispatch_overrides_from_params(raw),
    )


def _apply_work_class_defaults(
    req: SpawnSessionRequest, defaults: Mapping[str, Mapping[str, str]],
) -> SpawnSessionRequest:
    """§6 L3 rule 1 — fill an OMITTED ``model``/``effort`` from the
    operator-configured per-``work_class`` default (``plugin.yaml``'s
    ``work_class_defaults`` block). Never overrides a value the caller
    explicitly passed; an unconfigured ``work_class`` (the shipped default —
    empty block) leaves both fields exactly as ``spawn_session`` received
    them, i.e. today's behavior."""
    entry = defaults.get(req.work_class)
    if not entry:
        return req
    model = req.model or str(entry.get("model", ""))
    effort = req.effort or str(entry.get("effort", ""))
    if model == req.model and effort == req.effort:
        return req
    return replace(req, model=model, effort=effort)


def _apply_tool_allowlist(
    req: SpawnSessionRequest, allowlists: Mapping[str, tuple[str, ...]],
) -> SpawnSessionRequest:
    """§6 permission-mode ruling (2026-08-03) — fill an OMITTED
    ``allowed_tools`` from the operator-configured per-``work_class``
    allowlist (``plugin.yaml``'s ``work_class_tool_allowlists`` block).
    Never overrides an explicit caller value. An unconfigured ``work_class``
    resolves to the shipped-empty default (``()``) — the spawn is STILL
    gated (``headless_adapter.py`` always injects the PreToolUse hook),
    just with nothing extra allowed, never an open default."""
    if req.allowed_tools:
        return req
    configured = allowlists.get(req.work_class)
    if not configured:
        return req
    return replace(req, allowed_tools=configured)


def _resolve_permission_mode(req: SpawnSessionRequest, policy_mode: str) -> SpawnSessionRequest:
    """§6 permission-mode design — fill an OMITTED ``permission_mode`` from
    ``policy_mode`` (``plugin.yaml``'s ``headless_permission_mode``), never
    overriding an explicit caller value. Per the 2026-08-03 operator ruling
    ("we don't have any restrictions now"), no resolved value is rejected
    here — ``headless_adapter.py.verify_config()`` still refuses a spawn if
    this and the config both resolve to nothing at all, which is
    operational sanity (the process needs SOME argv value), not a
    restriction on which value."""
    if req.permission_mode:
        return req
    return replace(req, permission_mode=policy_mode)


def _resolve_transport(req: SpawnSessionRequest, policy_transport: str) -> SpawnSessionRequest:
    """fleet-watch-transport-migration phase 2 slice 1 (2026-08-06) — fill
    an OMITTED ``transport`` from ``policy_transport`` (``plugin.yaml``'s
    ``default_fleet_transport``), never overriding an explicit caller
    value. Mirrors :func:`_resolve_permission_mode` exactly: the same
    fill-never-override shape, one field later."""
    if req.transport:
        return req
    return replace(req, transport=policy_transport)


def _apply_spawn_session_policy(
    req: SpawnSessionRequest, policy: _SessionLifecyclePolicyConfig,
) -> SpawnSessionRequest:
    """The four spawn-config-resolution steps every ``spawn_session``
    dispatch must run — model/effort defaults, tool allowlist, permission
    mode, transport — factored out so ``spawn_session()`` (the API path)
    and the ``restart_session`` choreography (the background-worker path)
    share exactly ONE resolution path. Before this (2026-08-10), the
    choreography built its own ``SpawnSessionRequest`` directly and skipped
    all four steps; ``permission_mode``/``allowed_tools``/``transport`` are
    not columns on ``managed_session``, so restarting a worker silently lost
    them every time (measured live: ``host_cannot_spawn`` — no permission
    mode configured — on the fresh spawn). A fifth policy step added here
    now reaches both callers automatically instead of needing to be copied
    into a second, easily-forgotten call site."""
    if not req.model or not req.effort:
        req = _apply_work_class_defaults(req, policy.work_class_defaults)
    req = _apply_tool_allowlist(req, policy.work_class_tool_allowlists)
    req = _resolve_permission_mode(req, policy.headless_permission_mode)
    req = _resolve_transport(req, policy.default_fleet_transport)
    return req


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


def _build_peer_inbox_request(
    raw: dict[str, Any], binding: BridgeBinding,
) -> PeerInboxRequest:
    """Coerce caller args + the resolved binding into one ``PeerInboxRequest``.

    The recipient triple comes from ``binding`` and never from ``raw`` — a
    caller names only its own session, and the identity it reads with is the one
    the registry holds for that session. The two cursors are read independently
    and neither ever feeds the other. Raises ``ValueError`` for a malformed
    ``after``: a broken cursor means the caller's paging is wrong, and silently
    restarting from page one would turn that into an unbounded re-read.
    """
    after_raw = raw.get("after")
    try:
        after_created_at = (
            datetime.fromisoformat(str(after_raw))
            if after_raw not in (None, "")
            else None
        )
    except ValueError as exc:
        message = (
            f"'after' must be an ISO-8601 datetime (the previous page's "
            f"next_after_created_at): {exc}"
        )
        raise ValueError(message) from exc
    role_after_raw = raw.get("role_after")
    return PeerInboxRequest(
        recipient_agent_id=binding.agent_id,
        recipient_agent_instance_id=binding.agent_instance_id,
        recipient_agent_session_id=binding.agent_session_id,
        after_created_at=after_created_at,
        limit=_clamp_peer_inbox_limit(raw.get("limit")),
        # A4 (2026-08-04): the silent/important split at send time is
        # retired, so the catch-up view is the only meaningful one — never
        # read from the caller. This closes the hatch the same way Amendment
        # 3 closes send_peer_message's: the schema entry AND the
        # read-and-branch code both go, not just one.
        include_important=True,
        role_after=(
            str(role_after_raw) if role_after_raw not in (None, "") else None
        ),
    )


def _clamp_peer_inbox_limit(raw: object) -> int:
    """Coerce a caller's ``limit`` to the supported page size.

    Absent or non-numeric → the modest default (the flood guard is what makes
    an unqualified ``peer_inbox`` call safe to advertise). Out-of-range values
    clamp rather than error: the caller asked for "as much as you'll give me",
    and a page size is not a correctness argument — unlike ``after`` /
    ``role_after``, where a malformed value means the caller's paging is broken
    and must fail loud.
    """
    if isinstance(raw, bool) or not isinstance(raw, int):
        return PEER_INBOX_DEFAULT_LIMIT
    return max(PEER_INBOX_MIN_LIMIT, min(raw, PEER_INBOX_MAX_LIMIT))


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
    role_name: str,
    origin_instance: str,
    state_service: Any,
    *,
    fallback_agent_id: str = SYSTEM_AGENT_ID,
    fallback_label: str = "",
) -> _RoleSendSender:
    """Sender identity for a role-stamped send: role reply-to + best-effort provenance.

    The role NAME is the durable reply-to address (survives a holder reconnect).
    The current binding supplies honest sender provenance when resolvable;
    resolution is best-effort (degrade-silent) so a provenance fault never breaks
    the send — ``reply_to_role`` is set regardless, so two-way still works.

    The fallbacks matter on the §34.6 attribution rung: there the caller's
    ``agent_id`` and label were ALREADY resolved out of the peer registry, so
    degrading them to the ``system`` sentinel when the separate role-binding
    read faults would throw away identity we hold. Callers with no better
    material keep the pre-existing defaults.
    """
    agent_id = fallback_agent_id or SYSTEM_AGENT_ID
    instance = origin_instance
    label = fallback_label or role_name
    try:
        binding = resolve_role_binding(state_service, role_name)
    except Exception:  # noqa: BLE001 — provenance is best-effort; never break the send
        binding = None
    if binding is not None:
        agent_id = binding.agent_id or agent_id
        instance = binding.agent_instance_id or origin_instance
        label = binding.session_label or label
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
      3. else CALLER ATTRIBUTION present (§34.6) → an unregistered caller — the
         local CLI — whose opaque session key the SERVER already resolved
         against the peer registry in
         ``PlatformSurface._resolve_caller_attribution``. Its role rung is
         preferred over its instance rung for the same reconnect-survival
         reason as rung 1. Nothing here is caller-asserted: an unresolvable
         key arrives all-empty and falls through to rung 4.
      4. else → genuine scheduler-originated send → the system scheduler sentinel
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
    attributed_role = _str_field(state.get("caller_attribution_role"))
    attributed_instance = _str_field(state.get("caller_attribution_instance_id"))
    if attributed_role:
        return _sender_from_role(
            attributed_role,
            attributed_instance,
            state_service,
            fallback_agent_id=_str_field(state.get("caller_attribution_agent_id")),
            fallback_label=_str_field(state.get("caller_attribution_label")),
        )
    if attributed_instance:
        return _RoleSendSender(
            agent_id=(
                _str_field(state.get("caller_attribution_agent_id"))
                or SYSTEM_AGENT_ID
            ),
            agent_instance_id=attributed_instance,
            session_label=_str_field(state.get("caller_attribution_label")),
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


def _as_work_class_defaults(value: object) -> dict[str, dict[str, str]]:
    """Coerce ``plugin.yaml``'s ``work_class_defaults`` block (§6 L3 rule 1)
    into ``{work_class: {"model": ..., "effort": ...}}``. Tolerant of a
    malformed entry (skips it, logs, keeps the rest) rather than failing the
    whole config build over one operator typo — matches ``_as_bool``/
    ``_as_int``'s "fall back rather than crash" posture. Absent/wrong-typed
    input returns ``{}``, which is exactly today's behavior (no defaults
    applied, spawn_session leaves model/effort as the caller passed them)."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for work_class, entry in value.items():
        if not isinstance(work_class, str) or not isinstance(entry, dict):
            logger.warning(
                "Skipping malformed work_class_defaults entry for %r: not a mapping",
                work_class,
            )
            continue
        result[work_class] = {
            str(k): str(v) for k, v in entry.items() if k in {"model", "effort"}
        }
    return result


def _as_work_class_tool_allowlists(value: object) -> dict[str, tuple[str, ...]]:
    """Coerce ``plugin.yaml``'s ``work_class_tool_allowlists`` block (§6
    permission-mode ruling, 2026-08-03) into ``{work_class: (tool_name, ...)}``.
    Tolerant of a malformed entry (skips it, logs, keeps the rest) — same
    posture as ``_as_work_class_defaults``. Absent/wrong-typed input returns
    ``{}``: every headless spawn is STILL gated (the hook is always injected,
    per ``headless_adapter.py``'s ``_spawn_env``), just with an empty
    allowlist — never an open default."""
    if not isinstance(value, dict):
        return {}
    result: dict[str, tuple[str, ...]] = {}
    for work_class, entry in value.items():
        if not isinstance(work_class, str) or not isinstance(entry, (list, tuple)):
            logger.warning(
                "Skipping malformed work_class_tool_allowlists entry for %r: not a list",
                work_class,
            )
            continue
        result[work_class] = tuple(str(t) for t in entry)
    return result


def _resolve_homunculus_name() -> str:
    """Return the homunculus identity from ``$HOMUNCULUS_NAME``.

    Single source of truth across the platform: every plugin that
    surfaces a deployment label to external clients reads the same
    env var the bootstrap script sets.  Empty string when unset (laptop
    dev mode); downstream callers apply their own fallback.
    """
    import os  # noqa: PLC0415 — kept local so the import is greppable here
    return os.environ.get("HOMUNCULUS_NAME", "").strip()


# R4 seed-packaging audit, Package B (2026-08-10): root_manifest.yaml's own
# unwritten placeholder for `homunculus_name:` -- the midwife rewrites this
# field ONLY at genesis, so a raw/pre-genesis checkout keeps this exact
# literal, which rung 2 below must skip rather than treat as a real name.
_ROOT_MANIFEST_PLACEHOLDER = "homunculus"


def _resolve_homunculus_name_for_memory_tags() -> str:
    """Three-rung origin-resolution ladder for memory-tag scoping ONLY.

    SEPARATE from :func:`_resolve_homunculus_name` by deliberate ruling
    (coordinator seat, arm-8491e1ba, 2026-08-10): ``_resolve_homunculus_name`` has
    an unrelated third caller (the MCP streamable router's own identity
    label) whose consumers were never traced, so it stays byte-identical
    here -- containment over elegance on mint night. This function is
    called ONLY from the two M2.2 memory-tag verbs
    (``generate_curation_report``/``reinforce_by_slug``); unifying the two
    resolvers, after tracing the router-identity string's actual
    consumers, is a named post-mint backlog item, not this change.

    The SAME ladder as the hooks' own ``_journal.homunculus_name()``
    (``.claude/hooks/memory_passthrough/_journal.py`` and its vendored
    plugin copy), a parity test asserts the two agree on OUTPUT across a
    matrix of env/file/dirname combinations -- never on implementation
    approach, since this side runs inside the venv (real ``yaml.safe_load``)
    while the hooks' side runs outside it (a minimal regex line-scan,
    since PyYAML is a venv-only dependency there, measured 2026-08-10).

    1. ``HOMUNCULUS_NAME`` env var, if set.
    2. ``root_manifest.yaml``'s own ``homunculus_name:`` field, placeholder-
       skipped.
    3. ``CLAUDE_PROJECT_DIR``-basename -- :func:`_resolve_homunculus_name`'s
       existing sole behavior, preserved here as the final fallback.

    Empty string only when every rung is exhausted (no env var AND no
    resolvable ``CLAUDE_PROJECT_DIR``) -- callers apply their own
    fail-loud contract on an empty result, same as the existing function.
    """
    import os  # noqa: PLC0415 — kept local, mirrors _resolve_homunculus_name's own style

    import yaml  # noqa: PLC0415

    env_name = os.environ.get("HOMUNCULUS_NAME", "").strip()
    if env_name:
        return env_name
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR", "").strip()
    if not project_dir:
        return ""
    try:
        with open(os.path.join(project_dir, "root_manifest.yaml"), encoding="utf-8") as handle:
            data = yaml.safe_load(handle)
        candidate = str((data or {}).get("homunculus_name", "")).strip()
        if candidate and candidate != _ROOT_MANIFEST_PLACEHOLDER:
            return candidate
    except (OSError, yaml.YAMLError):
        pass  # unreadable, absent, or malformed -- fall through, never raise
    return os.path.basename(os.path.normpath(os.path.abspath(project_dir)))


__all__ = ["AgentMessagingPlugin"]
