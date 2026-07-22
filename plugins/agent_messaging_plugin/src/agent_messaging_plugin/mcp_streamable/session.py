"""Streamable HTTP MCP session state machine.

A Streamable HTTP session is identified by the ``Mcp-Session-Id``
header allocated on the ``initialize`` response.  Each session owns a
**synthetic** :class:`BridgeSessionState` — opened the same way the
stdio bridge open route opens one — plus a peer-registry binding for
the agent_id/agent_instance_id carried in the bearer-token claim.
That lets every other piece of the platform treat a streamable
session as a first-class peer: ``peer_send`` targeting the phone's
``agent_instance_id`` deposits an event in the synthetic bridge's
queue, and the SSE notification stream picks it up off the same
``BridgeSessionManager`` event source the stdio long-poll uses.

Lifecycle:

* ``allocate`` — at ``initialize`` time.  Generates a fresh
  ``Mcp-Session-Id`` (cryptographically secure URL-safe token), opens
  a synthetic bridge, registers the peer binding, indexes the session
  by both ids.
* ``get`` — by ``Mcp-Session-Id`` header.  Bumps ``last_seen_at`` and
  touches the underlying bridge so the idle sweep does not expire it
  while the phone is interacting.
* ``close`` — explicit DELETE from the client OR idle expiry.
  Unregisters the peer binding (so subsequent ``peer_send`` calls
  fail with ``peer_unreachable``), closes the synthetic bridge (which
  wakes any in-flight SSE poll), and drops the session entry.

Threading: session-table mutation is guarded by a single ``Lock``.
``BridgeSessionManager`` and ``PeerRegistry`` carry their own internal
locks, so cross-mutex ordering matters only at allocate / close.  We
hold the session lock first and the bridge/registry locks second; no
opposite-direction path exists.
"""

from __future__ import annotations

import logging
import secrets
import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Final

from ..models import BridgeBinding

if TYPE_CHECKING:
    from ..bridge_sessions import BridgeSessionManager
    from ..peer_registry import PeerRegistry
    from .auth import BearerClaim

logger = logging.getLogger(__name__)


# Length of the Mcp-Session-Id (bytes pre-base64).  256 bits is overkill
# for an ephemeral session token but matches the spec's "cryptographically
# secure UUID, JWT, or hash" wording without ambiguity.
_SESSION_ID_BYTES: Final[int] = 32

# Visible-ASCII session ID character set (the spec requires 0x21–0x7E).
# urlsafe_b64 stays inside that range — `-` (0x2D), `_` (0x5F), `=` (0x3D)
# all qualify.
_SESSION_ID_PREFIX: Final[str] = "mcp-"

# Default human session_label for phone bindings; the bearer claim can
# override it.  Kept short so peer_list output stays readable.
_DEFAULT_SESSION_LABEL: Final[str] = "phone via streamable HTTP"


@dataclass(slots=True)
class StreamableSession:
    """One live Streamable HTTP MCP session.

    Holds the durable handles needed by the JSON-RPC dispatcher and
    the SSE notification stream:

    * ``mcp_session_id`` — outward-facing handle echoed by the client
      on every HTTP request.
    * ``bridge_id`` — synthetic ``agc-<hex>`` registered with
      :class:`BridgeSessionManager`.  Owns the event queue.
    * ``binding`` — the peer-registry entry registered for the
      ``agent_id``/``agent_instance_id`` from the bearer claim.
    * ``sse_cursor`` — next event cursor the SSE GET stream should
      poll for.  ``-1`` means "deliver any event in the queue
      (catch-up)".  Updated in-place by the SSE multiplexer.
    """

    mcp_session_id: str
    bridge_id: str
    session_id: str
    agent_id: str
    agent_instance_id: str
    session_label: str
    binding: BridgeBinding
    client_info: dict[str, object] = field(default_factory=dict)
    protocol_version: str = ""
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_seen_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    sse_cursor: int = -1

    def touch(self) -> None:
        """Bump ``last_seen_at``; called on every successful request."""
        self.last_seen_at = datetime.now(UTC)


class StreamableSessionManager:
    """Owns the in-memory ``Mcp-Session-Id`` → :class:`StreamableSession` table.

    Constructor takes the platform collaborators it needs to allocate
    and tear down the synthetic bridge + peer binding.  No FastAPI /
    HTTP knowledge here — the router layer translates between HTTP
    headers and these calls.
    """

    def __init__(
        self,
        *,
        bridge_manager: BridgeSessionManager,
        peer_registry: PeerRegistry,
    ) -> None:
        self._bridge_manager = bridge_manager
        self._peer_registry = peer_registry
        self._sessions: dict[str, StreamableSession] = {}
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def allocate(
        self,
        claim: BearerClaim,
        *,
        client_info: dict[str, object] | None = None,
        protocol_version: str = "",
    ) -> StreamableSession:
        """Open a fresh session bound to ``claim``.

        M5 §14.4 round-2 fix: pass the validated bearer claim through
        to ``BridgeSessionManager.open_bridge`` so the bridge carries
        ``client_id`` + ``process_export_allowlist`` at establishment.
        Previously the claim was discarded and the bridge defaulted to
        an empty identity, breaking per-session policy enforcement.

        The synthetic bridge id flows into the peer registry as the
        binding's ``bridge_id``, so subsequent ``peer_send`` calls
        resolve recipient → binding → bridge event queue exactly the
        same way they resolve a stdio-bridge peer.
        """
        bridge = self._bridge_manager.open_bridge(claim, parent_pid=None)
        session_label = claim.session_label or _DEFAULT_SESSION_LABEL
        binding = BridgeBinding(
            bridge_id=bridge.bridge_id,
            agent_id=claim.agent_id,
            agent_instance_id=claim.agent_instance_id,
            session_label=session_label,
            parent_pid=None,
        )
        # open_bridge already pre-marks the bridge with agent_instance_id +
        # session_label from the claim (so legacy IO peer lookups read
        # the right identity), so no manual rebind is needed here.
        self._peer_registry.register(binding)
        session = StreamableSession(
            mcp_session_id=_mint_session_id(),
            bridge_id=bridge.bridge_id,
            session_id=bridge.session_id,
            agent_id=claim.agent_id,
            agent_instance_id=claim.agent_instance_id,
            session_label=session_label,
            binding=binding,
            client_info=dict(client_info or {}),
            protocol_version=protocol_version,
        )
        with self._lock:
            self._sessions[session.mcp_session_id] = session
        logger.info(
            "streamable session allocated: mcp_session_id=%s bridge_id=%s "
            "agent_id=%s agent_instance_id=%s",
            session.mcp_session_id,
            session.bridge_id,
            session.agent_id,
            session.agent_instance_id,
        )
        return session

    def get(self, mcp_session_id: str | None) -> StreamableSession | None:
        """Look a session up by header value; bump ``last_seen_at`` on hit."""
        if not mcp_session_id:
            return None
        with self._lock:
            session = self._sessions.get(mcp_session_id)
        if session is None:
            return None
        session.touch()
        # Touching the underlying bridge keeps the idle sweep from
        # expiring the synthetic bridge while the phone is interacting
        # — without this, an idle SSE stream can be reaped mid-flight.
        bridge = self._bridge_manager.get(session.bridge_id)
        if bridge is not None and not bridge.closed:
            bridge.touch()
        return session

    def close(self, mcp_session_id: str) -> bool:
        """Tear down a session; return False if no session matched."""
        with self._lock:
            session = self._sessions.pop(mcp_session_id, None)
        if session is None:
            return False
        # Unregister the peer binding before closing the bridge so a
        # concurrent peer_send racing the close fails fast on the
        # binding lookup rather than depositing into a doomed queue.
        self._peer_registry.unregister(session.bridge_id)
        self._bridge_manager.close(session.bridge_id)
        logger.info(
            "streamable session closed: mcp_session_id=%s bridge_id=%s",
            session.mcp_session_id,
            session.bridge_id,
        )
        return True

    def close_all(self) -> int:
        """Tear down every session; called from plugin shutdown."""
        with self._lock:
            session_ids = list(self._sessions.keys())
        return sum(1 for sid in session_ids if self.close(sid))


def _mint_session_id() -> str:
    """Generate an ``mcp-<urlsafe-b64>`` id; ASCII-visible per MCP spec."""
    return _SESSION_ID_PREFIX + secrets.token_urlsafe(_SESSION_ID_BYTES)


__all__ = ["StreamableSession", "StreamableSessionManager"]
