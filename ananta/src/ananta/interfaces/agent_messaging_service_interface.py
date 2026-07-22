"""Agent Messaging Service Interface.

Durable thread/message persistence for inter-agent calls.  Implemented by
``agent_messaging_plugin`` and consumed by its own bridge HTTP surface
(and any future originator: REST, plans, schedules, Discord, ...).

This interface defines the public *shape* of the service.  It is NOT
wired into the ``service_bindings.json`` system — see ``ServiceName``
in ``ananta/src/ananta/core/orchestration/service_bindings.py`` for the
canonical bound-service registry.  Binding ``agent_messaging_plugin``
as a ServiceProvider would cause the platform's process registry to
skip its ``plugin::*::*`` namespace
(``process_registry/builder.py::_should_skip_plugin``), which would
hide ``plugin::agent_messaging_plugin::run_turn`` from
``submit_action_definition`` — and ``run_turn`` is exactly what the
service must dispatch on every turn.

Consumption pattern (used internally by the plugin's bridge surface):

    instance = orchestrator.plugin_manager.plugins["agent_messaging_plugin"]
    # ``instance`` implements this interface by structural typing.

Plugins implementing this interface must:

1. NOT declare ``service_interfaces`` (would mark them as ServiceProvider).
2. NOT be added to ``config/service_bindings.json``.
3. Apply the ownership and policy checks described in
   ``workbench/2026-05-10_codex_inter_agent_messaging.md``
   (each method takes the calling ``bridge_id`` and rejects requests that
   don't own the named thread).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar

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
    PeerInbox,
    PeerInboxRequest,
    PeerSendRequest,
    PeerSendResult,
    ReadThreadMessagesRequest,
    SendAgentMessageRequest,
)


class AgentMessagingServiceInterface(ABC):
    """Public surface of the agent-messaging service.

    Every method takes the calling bridge id and enforces strict
    bridge-bound ownership in the first slice.  ``agent_interrupt`` is
    deferred (the underlying ``GuardedAgentInterface`` cannot surface a
    backend session id mid-execution; see workbench doc §1, §18).
    """

    INTERFACE_VERSION: ClassVar[str] = "1.0.0"

    @abstractmethod
    def open_thread(self, request: OpenAgentThreadRequest) -> AgentThreadOpened:
        """Create a durable thread and optionally start the first turn.

        If ``request.initial_message`` is omitted the thread is created in
        ``ThreadStatus.OPEN`` with no action submitted.  Otherwise the
        originator message is persisted and a ``run_turn`` EDGE action is
        submitted; the thread transitions to ``ThreadStatus.QUEUED``.
        """
        ...

    @abstractmethod
    def send_message(
        self, request: SendAgentMessageRequest,
    ) -> AgentMessageQueued:
        """Append an originator message and schedule the next agent turn.

        Fails with a structured ``agent_thread_busy`` error if the thread
        already has an active turn.  Fails with ``agent_thread_closed`` if
        the thread is closed.  Fails with ``agent_thread_unauthorized`` if
        the calling bridge does not own the thread.
        """
        ...

    @abstractmethod
    def list_messages(
        self, request: ListAgentMessagesRequest,
    ) -> AgentMessagesPage:
        """Return messages with cursor strictly greater than ``after_cursor``.

        Pagination controls count, not content size.  Inbound writes that
        exceed ``max_message_bytes`` are rejected at write time, never
        truncated at read time.
        """
        ...

    @abstractmethod
    def list_threads(
        self, request: ListAgentThreadsRequest,
    ) -> AgentThreadsPage:
        """Globally enumerate threads, cursor-paginated by ``(created_at, id)``.

        The GAP-5/D1 owning-service read verb: consumers (e.g. the session
        ledger source) project the whole thread set through this interface
        instead of reading ``core__agent_thread`` directly. UNLIKE the other
        methods this is NOT bridge-ownership-scoped — it is a global substrate
        read consumed only by structural typing (not exposed on the bridge
        HTTP surface), so it takes no ``bridge_id``. Ascending by the tie-safe
        composite ``(created_at, id)``; ``request.after_cursor`` is the opaque
        token of the previous page's last row (echoed back from ``next_cursor``,
        fail-closed on a malformed token). ``include_deleted=False`` excludes
        soft-deleted threads. Not a discoverable ``@service_interface_process``
        (mirrors ``list_messages``).
        """
        ...

    @abstractmethod
    def read_thread_messages(
        self, request: ReadThreadMessagesRequest,
    ) -> AgentThreadMessagesPage:
        """Read ONE thread's messages cursor-paginated, UNSCOPED (GAP-5/D1).

        The unscoped counterpart to ``list_messages``: same per-thread,
        int-cursor (``cursor`` strictly greater than ``after_cursor``) read, but
        WITHOUT the bridge-ownership gate — it takes no ``bridge_id`` because the
        only consumer (the session-ledger projection) does not own the threads
        it reads. Consumed only by structural typing; not on the bridge HTTP
        surface, not a discoverable ``@service_interface_process``. Reuses the
        same repository read as ``list_messages``; returns a minimal page (no
        thread ``status`` — the owned-thread fetch is intentionally skipped).
        """
        ...

    @abstractmethod
    def get_status(self, *, thread_id: str, bridge_id: str) -> AgentThreadStatus:
        """Return current thread status plus the active action if any."""
        ...

    @abstractmethod
    def peer_inbox(self, request: PeerInboxRequest) -> PeerInbox:
        """Return originator peer messages addressed to ``recipient_agent_id``.

        Surface for receivers to pull silently-persisted peer
        messages (those sent without the IMPORTANT marker, which did
        not fire a notification).
        """
        ...

    @abstractmethod
    def peer_send(self, request: PeerSendRequest) -> PeerSendResult:
        """Persist a peer-message to the (sender, peer) thread.

        Returns the structured row plus a delivery hint.  The
        actual cross-bridge event enqueue happens in the plugin's
        own bridge surface after this method returns — the
        service keeps the bridge layer out of its dependencies.
        """
        ...

    @abstractmethod
    def close_thread(
        self, *, thread_id: str, bridge_id: str,
    ) -> AgentThreadClosed:
        """Mark the thread closed.

        Refuses to close threads in ``ThreadStatus.QUEUED`` or
        ``ThreadStatus.RUNNING`` until ``agent_interrupt`` ships
        (workbench doc §1, §18).
        """
        ...
