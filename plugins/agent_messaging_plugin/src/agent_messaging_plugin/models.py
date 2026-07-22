"""Bridge session + event-queue runtime state for ``agent_messaging_plugin``.

A bridge is the durable handle an MCP client holds while it owns one or
more agent threads.  The bridge id appears in every URL and is the
authoritative ownership token; the platform session id is minted at
bridge open and persisted on every thread row so the bridge-delivery
contract validator finds the same session id on the action side.

Ported from the now-deleted ``agent_channel_plugin/models.py`` (2026-05-16)
during the bridge-consolidation work — see
``workbench/2026-05-16_codex_mcp_channel_and_inter_agent_outstanding_work.md``.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol


class NativeWakeAdapter(Protocol):
    """Protocol for plugins that surface a native MCP wake channel.

    The default ``peer_send`` delivery path enqueues a
    ``notifications/claude/channel`` event on the recipient's
    bridge.  Some MCP client transports do not auto-surface those
    notifications between turns and need an alternate "chat-style"
    wake path.  Plugins that own such an alternate path implement
    this Protocol and register themselves via
    ``AgentMessagingPlugin.register_native_wake_adapter``.

    With this plugin's consolidation (one plugin owning both the
    agent-to-agent surface and the Claude-Code IO surface),
    self-registration is the common case: the plugin registers an
    adapter for ``agent_id == "claude_code"`` that targets its own
    IO ``post_message`` path.  External plugins can still register
    adapters for other agent_ids if a future transport needs one.

    Native wake delivers via plain text (no ``meta`` field), so
    implementations MUST embed ``sender_agent_instance_id`` in the
    envelope so the receiver can construct a targeted reply.
    ``recipient_parent_pid`` lets the adapter pair with the matching
    sibling bridge inside the same OS process tree.
    """

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
        """Push prose into the agent's native MCP surface.

        Returns the receiver-side bridge id (for inclusion in the
        peer_send response's ``delivered_to_bridge_id`` field).

        ``reply_to_role`` (REL-01 Fork 4): the sender's DURABLE role for a
        role-addressed (``peer_send_by_name``) send. When non-empty the adapter
        MUST surface a role reply-to hint (``peer_send_by_name name=<role>``) so
        the return leg survives a holder reconnect. Empty for a direct instance
        ``peer_send`` (which keeps the same-connection instance reply-to).

        ``delivery_meta`` (v10 Control #5 / Q3-revised): extra bridge-event meta
        the adapter MUST merge onto the wake event. For a role-addressed send it
        carries the Control #5 role keys (``recipient_kind`` / ``recipient_key``
        / ``delivery_external_id``) so the holder's forwarder recognises the role
        delivery on ``/events`` and confirms it (``/peer/delivered``) — the wake
        is the SAME bridge queue as the queued_notification path, NOT a direct
        push, so the forwarder is the sole delivered-authority for both.
        ``None`` for a plain instance peer_send.

        Raises if the adapter cannot deliver — the loop-prevention
        contract treats IMPORTANT as a hard delivery promise, so
        silent drops are not acceptable.
        """
        ...


@dataclass(frozen=True, slots=True)
class BridgeBinding:
    """One entry in the per-agent_id peer registry.

    Multiple instances of the same ``agent_id`` (e.g., several
    concurrent Claude Code sessions) each register their own
    ``BridgeBinding``.  ``agent_instance_id`` is the durable routing
    key — the bridge subprocess generates it at startup and reuses
    it across reconnects, so the registry replaces (not duplicates)
    when the same instance re-registers with a new ``bridge_id``.

    Timestamps come from the backing :class:`Store`.  ``created_at``
    replaces the prior ``registered_at`` field (one-release deprecated
    alias still surfaced in ``peer_list`` responses).  ``updated_at``
    is bumped by every dispatch operation (``peer_send``,
    ``peer_inbox``, native wake) so it carries "last active"
    semantics that fall out of the canonical platform timestamp
    convention.  Construction-time bindings (those passed INTO
    ``PeerRegistry.register``) omit both timestamps; the store fills
    them on insert and the registry's read paths return rebuilt
    bindings carrying the persisted values.
    """

    bridge_id: str
    agent_id: str
    agent_instance_id: str
    session_label: str
    parent_pid: int | None
    created_at: str = ""
    updated_at: str = ""
    # S1 (agent_session_id splice): the STABLE per-logical-session key — survives
    # bridge reconnect / agent_instance_id rotation. Drives the reconnect
    # state-table self-refresh (peer_register → refresh_role_binding_cas) and
    # surfaces via current_identity. Empty for sessions launched without
    # HOMUNCULUS_AGENT_SESSION_ID exported (streamable / older clients) -> no self-refresh.
    agent_session_id: str = ""


@dataclass(slots=True)
class QueuedEvent:
    """One outbound channel event waiting for bridge consumption.

    ``content`` is plain English prose — the same text a human would
    read in a chat surface.  Structured fields (thread_id, sender,
    payload, etc.) live in ``meta`` so the MCP-side notification
    renderer can surface ``content`` directly without parsing.
    """

    cursor: int
    event_type: str
    content: str
    meta: dict[str, object] = field(default_factory=dict)
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )


@dataclass(slots=True)
class BridgeSessionState:
    """In-memory state for one active bridge.

    The event queue is mutated from at least two threads:

    * the action queue thread (calls ``append_event`` from
      ``deliver_result`` / ``deliver_error`` and from ``post_message``)
    * the FastAPI event loop thread (calls ``events_after`` while
      long-polling)

    A per-bridge lock around every queue mutation keeps appends and
    drains atomic — without it, an append concurrent with a drain can
    lose the event because ``events_after`` rebinds ``pending_events``
    via list comprehension.
    """

    bridge_id: str
    session_id: str
    # OS PID of the MCP host that spawned the bridge subprocess (e.g.
    # this Claude Code session, or this Codex session).  Live-routing
    # metadata only — used to pair sibling bridges inside the same OS
    # process tree.  Never persisted as identity.
    parent_pid: int | None = None
    # Durable per-bridge UUID generated by the bridge subprocess at
    # startup ("agi-<uuid>").  This is the routing key for
    # multi-instance addressing, persisted on peer threads, and
    # carried in peer_message notification meta + native-wake
    # envelopes so receivers can construct targeted replies.
    agent_instance_id: str | None = None
    # Mutable, non-unique human metadata supplied by the bridge at
    # registration ("codex on baroque-suite").  Never used as a
    # routing key — only for human-facing display in peer_list,
    # peer_message meta, and native-wake envelopes.
    session_label: str = ""
    # M5 §14.4: OAuth client_id that opened this bridge. Empty string
    # for legacy stdio bridges that never carried a bearer. Set at
    # bridge-establishment time from the validated BearerClaim.
    client_id: str = ""
    # M5 §14.4: per-session allowlist of process_keys this bridge may
    # invoke via process_call / process_search / process_schema.
    # Default is EMPTY_ALLOWLIST (fail-closed) — a bridge with no
    # resolved policy can call nothing. Populated by
    # BridgeSessionManager._resolve_session_policy at open time.
    process_export_allowlist: tuple[str, ...] = ()
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )
    last_seen_at: str = field(
        default_factory=lambda: datetime.now(UTC).isoformat(),
    )
    # REL-05: the last time this bridge invoked a MODEL-INITIATED route
    # (peer/send, process/*, agent/*, ... — NEVER a forwarder/infra
    # route: open, events, drain, delivered, register [F1], close, health). The
    # consumption reconciler reads it to decide whether an owed IMPORTANT send
    # entered a turn context. Distinct from ``last_seen_at`` (which every drain
    # long-poll bumps); a deaf session's forwarder keeps ``last_seen_at`` fresh
    # while this stays stale — that is exactly the Vector-B discriminator. Empty
    # until the first model-initiated route.
    last_model_activity_at: str = ""
    closed: bool = False
    next_event_id: int = 0
    pending_events: list[QueuedEvent] = field(default_factory=list)
    _events_lock: threading.Lock = field(default_factory=threading.Lock)

    def touch(self) -> None:
        self.last_seen_at = datetime.now(UTC).isoformat()

    def stamp_model_activity(self) -> str:
        """Record that a MODEL-INITIATED route just fired; return the timestamp.

        Called ONLY from the route-activity middleware for routes classified as
        model-initiated (never forwarder/infra). The returned ISO timestamp is
        mirrored to the durable ``peer_binding`` row so the server-side sweep can
        read it without the live session.
        """
        stamp = datetime.now(UTC).isoformat()
        self.last_model_activity_at = stamp
        return stamp

    def append_event(
        self,
        event_type: str,
        content: str,
        meta: dict[str, object] | None = None,
    ) -> QueuedEvent:
        with self._events_lock:
            event = QueuedEvent(
                cursor=self.next_event_id,
                event_type=event_type,
                content=content,
                meta=dict(meta) if meta else {},
            )
            self.pending_events.append(event)
            self.next_event_id += 1
            return event

    def events_after(self, after: int) -> list[QueuedEvent]:
        """Return undelivered events; drain those the client already saw."""
        with self._events_lock:
            self.pending_events = [
                e for e in self.pending_events if e.cursor > after
            ]
            return list(self.pending_events)

    def pending_event_count(self) -> int:
        with self._events_lock:
            return len(self.pending_events)


__all__ = [
    "BridgeBinding",
    "BridgeSessionState",
    "NativeWakeAdapter",
    "QueuedEvent",
]
