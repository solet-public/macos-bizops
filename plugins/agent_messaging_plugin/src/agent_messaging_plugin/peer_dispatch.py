"""Transport-neutral peer_send dispatch core.

Both the legacy bridge HTTP route (``/api/v1/bridge/{id}/peer/send``)
and the Streamable HTTP MCP transport's ``tools/call peer_send``
funnel through the **same** dispatch logic:

* resolve the recipient ``BridgeBinding`` via ``PeerRegistry.resolve``
  (raises :class:`PeerAmbiguousError` / :class:`PeerUnreachableError`),
* persist the message via ``agent_messaging_service.peer_send``,
* touch the sender bridge + both bindings so ``updated_at`` reflects
  the dispatch,
* detect the IMPORTANT marker and, when present, route through the
  recipient's native wake adapter if registered, else append a
  ``peer_message`` channel event on the recipient's bridge.

The shape of this logic is protocol semantics, not HTTP / JSON-RPC
plumbing — both call sites used to duplicate it.  This module is the
single source of truth; callers translate :class:`PeerSendOutcome`
and the raised exceptions into their own response types.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, Final

from ananta.llm.agent_messaging.models import PeerSendRequest
from ananta.llm.agent_messaging.schema import (
    META_KEY_DELIVERY_EXTERNAL_ID,
    META_KEY_RECIPIENT_KEY,
    META_KEY_RECIPIENT_KIND,
    RECIPIENT_KIND_ROLE,
)
from ananta.llm.agent_messaging.service import role_message_external_id

from .bridge_sessions import BridgeNotFoundError, BridgeQueueFullError
from .peer_registry import PeerAmbiguousError, PeerUnreachableError

if TYPE_CHECKING:
    from ananta.llm.agent_messaging.models import TextPart

    from .bridge_sessions import BridgeSessionManager
    from .peer_registry import BridgeBinding, PeerRegistry
    from .peer_role_management import ResolvedRole

logger = logging.getLogger(__name__)


# Channel event type used when delivering an IMPORTANT message via the
# recipient's bridge event queue (no native wake adapter registered).
# Imported by the existing http_routes module for the
# ``EVENT_PEER_MESSAGE`` re-export it still publishes.
EVENT_PEER_MESSAGE: Final[str] = "peer_message"


# Matches the leading "IMPORTANT" marker (with ":" or whitespace tail)
# that gates the wake-vs-silent dispatch path.  Stripped from the
# delivered prose before forwarding — the marker is protocol metadata.
IMPORTANT_MARKER_RE: Final[re.Pattern[str]] = re.compile(
    r"^\s*IMPORTANT[:\s]\s*", re.MULTILINE,
)


# Delivery discriminator literals.  Exported so callers can compare
# against named constants instead of string literals.
#
# REL-06 (honest-return relabel): the two live-delivery kinds name what the
# code can truthfully prove. The platform only ever QUEUES a turn-triggering
# event on the recipient's live bridge long-poll — it never observes the wake
# or the consumption. ``queued_wake`` rides a ``post_message`` envelope through
# a registered native adapter; ``queued_notification`` rides a ``peer_message``
# channel event. Emission to the client happens at the forwarder's next drain;
# whether it becomes a turn is client-side and NOT confirmed by this field —
# consumption is tracked separately (REL-05 direct-wake outbox).
DELIVERY_PERSISTED_SILENT: Final[str] = "persisted_silent"
DELIVERY_QUEUED_NOTIFICATION: Final[str] = "queued_notification"
DELIVERY_QUEUED_WAKE: Final[str] = "queued_wake"
# v10 Control #4: an IMPORTANT role message whose authoritative envelope was
# persisted (delivered=false) but whose current holder could not be reached
# (no live binding / wake failure / queue full). NOT a failure — the row sits
# durable and Control #5's repair loop re-delivers on the holder's next attach.
DELIVERY_QUEUED_FOR_REPLAY: Final[str] = "queued_for_replay"


@dataclass(frozen=True, slots=True)
class PeerSendOutcome:
    """Typed result of a peer_send dispatch.

    The fields are the union of every key the legacy bridge HTTP
    response and the streamable JSON-RPC tool result emit.  Callers
    pick whichever subset their transport surfaces.
    """

    thread_id: str
    message_id: str
    cursor: int
    delivered_to_agent_id: str
    delivered_to_agent_instance_id: str
    from_agent_id: str
    from_agent_instance_id: str
    delivery: str
    delivered_to_bridge_id: str

    def to_payload(self) -> dict[str, Any]:
        """Return the response shape both transports serialise."""
        return {
            "thread_id": self.thread_id,
            "message_id": self.message_id,
            "cursor": self.cursor,
            "delivered_to_agent_id": self.delivered_to_agent_id,
            "delivered_to_agent_instance_id": self.delivered_to_agent_instance_id,
            "from_agent_id": self.from_agent_id,
            "from_agent_instance_id": self.from_agent_instance_id,
            "delivery": self.delivery,
            "delivered_to_bridge_id": self.delivered_to_bridge_id,
        }


class NativeWakeError(Exception):
    """A registered native wake adapter raised mid-dispatch.

    The loop-prevention contract treats IMPORTANT delivery as a hard
    promise, so the adapter's failure surfaces to the caller as a
    stable error rather than a silent drop.  Carries the offending
    ``peer_agent_id`` for diagnostics + the originating exception
    via ``__cause__``.
    """

    def __init__(self, message: str, *, peer_agent_id: str) -> None:
        super().__init__(message)
        self.peer_agent_id: str = peer_agent_id


def dispatch_peer_send(
    *,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    agent_messaging_service: Any,
    sender_bridge_id: str,
    sender_agent_id: str,
    sender_agent_instance_id: str,
    sender_session_label: str,
    sender_parent_pid: int | None,
    peer_id: str,
    peer_agent_instance_id: str | None,
    content: list[TextPart],
) -> PeerSendOutcome:
    """Dispatch a peer_send and return the typed outcome.

    Raises:
        :class:`PeerAmbiguousError`: multiple instances of ``peer_id``
            registered and no ``peer_agent_instance_id`` hint given.
        :class:`PeerUnreachableError`: no binding matches the
            requested recipient.
        :class:`BridgeNotFoundError`: the recipient's bridge has been
            torn down between resolve and append.
        :class:`BridgeQueueFullError`: the recipient's event queue
            has hit its capacity.
        :class:`NativeWakeError`: a registered native wake adapter
            raised during IMPORTANT delivery.
    """
    recipient = peer_registry.resolve(peer_id, peer_agent_instance_id)
    prose = "\n".join(part.text for part in content)
    marker_match = IMPORTANT_MARKER_RE.match(prose)
    important = marker_match is not None
    result = agent_messaging_service.peer_send(
        PeerSendRequest(
            sender_bridge_id=sender_bridge_id,
            sender_agent_id=sender_agent_id,
            sender_agent_instance_id=sender_agent_instance_id,
            sender_session_label=sender_session_label,
            peer_agent_id=peer_id,
            peer_agent_instance_id=recipient.agent_instance_id,
            peer_session_label=recipient.session_label,
            # REL-08 read-side: stamp the recipient's stable session key onto the
            # peer thread at creation so its inbox survives instance rotation.
            peer_agent_session_id=recipient.agent_session_id,
            content=content,
            important=important,
        ),
    )
    # Bump activity timestamps for the sender bridge + both endpoint
    # bindings.  Postgres-trigger-style semantics: every dispatch
    # makes ``updated_at`` carry "last active" meaning that every
    # peer_list read surfaces.
    sender_bridge = bridge_manager.get(sender_bridge_id)
    if sender_bridge is not None:
        sender_bridge.touch()
    peer_registry.touch_binding(sender_agent_instance_id)
    peer_registry.touch_binding(recipient.agent_instance_id)
    if not important:
        return PeerSendOutcome(
            thread_id=str(result.thread_id),
            message_id=str(result.message_id),
            cursor=int(result.cursor),
            delivered_to_agent_id=peer_id,
            delivered_to_agent_instance_id=recipient.agent_instance_id,
            from_agent_id=sender_agent_id,
            from_agent_instance_id=sender_agent_instance_id,
            delivery=DELIVERY_PERSISTED_SILENT,
            delivered_to_bridge_id=recipient.bridge_id,
        )
    # ``marker_match`` is narrowed by ``important`` (truthy iff
    # match was found); the explicit assertion keeps the type
    # checker happy without a runtime cost.
    assert marker_match is not None
    delivered_prose = prose[marker_match.end():]
    adapter = peer_registry.wake_adapter_for(peer_id)
    if adapter is not None:
        try:
            delivered_to_bridge_id = adapter.wake(
                recipient_parent_pid=recipient.parent_pid,
                delivered_prose=delivered_prose,
                sender_agent_id=sender_agent_id,
                sender_agent_instance_id=sender_agent_instance_id,
                sender_session_label=sender_session_label,
                thread_id=result.thread_id,
                message_id=result.message_id,
            )
        except Exception as exc:  # noqa: BLE001 — adapter contract: any exception is failure
            logger.exception("native wake failed for %s", peer_id)
            raise NativeWakeError(
                f"native wake failed for {peer_id}: {exc}",
                peer_agent_id=peer_id,
            ) from exc
        delivery_kind = DELIVERY_QUEUED_WAKE
        delivered_bridge = str(delivered_to_bridge_id)
    else:
        meta = build_peer_message_meta(
            sender_agent_id=sender_agent_id,
            sender_agent_instance_id=sender_agent_instance_id,
            sender_session_label=sender_session_label,
            sender_parent_pid=sender_parent_pid,
            sender_bridge_id=sender_bridge_id,
            recipient_agent_id=recipient.agent_id,
            recipient_agent_instance_id=recipient.agent_instance_id,
            thread_id=result.thread_id,
            message_id=result.message_id,
            thread_cursor=result.cursor,
        )
        bridge_manager.append_event(
            recipient.bridge_id,
            EVENT_PEER_MESSAGE,
            delivered_prose,
            meta,
        )
        delivery_kind = DELIVERY_QUEUED_NOTIFICATION
        delivered_bridge = recipient.bridge_id
    # REL-05: insure this IMPORTANT direct send. The outbox row records the
    # ORIGINAL emission just performed (emit_count=1, last_emitted_at=now); if the
    # recipient session never enters a turn (Vector B), the recipient's repair
    # drain re-emits it (capped) until it provably enters context, then escalates
    # to this sender. Written AFTER a successful emission so a hard-failed wake
    # (NativeWakeError above) does not leave an insured-but-never-emitted row.
    # Silent sends never reach here (returned above) — the re-emit machinery is
    # IMPORTANT-only by contract.
    agent_messaging_service.persist_direct_wake(
        message_id=str(result.message_id),
        thread_id=str(result.thread_id),
        recipient_agent_id=recipient.agent_id,
        recipient_agent_instance_id=recipient.agent_instance_id,
        # Fork-1a: stamp the recipient's STABLE session key (from the resolved
        # live binding, never caller args) so a reconnect re-homes owed rows.
        recipient_agent_session_id=recipient.agent_session_id,
        sender_agent_id=sender_agent_id,
        sender_agent_instance_id=sender_agent_instance_id,
        sender_session_label=sender_session_label,
        sender_bridge_id=sender_bridge_id,
        content=content,
    )
    return PeerSendOutcome(
        thread_id=str(result.thread_id),
        message_id=str(result.message_id),
        cursor=int(result.cursor),
        delivered_to_agent_id=peer_id,
        delivered_to_agent_instance_id=recipient.agent_instance_id,
        from_agent_id=sender_agent_id,
        from_agent_instance_id=sender_agent_instance_id,
        delivery=delivery_kind,
        delivered_to_bridge_id=delivered_bridge,
    )


@dataclass(frozen=True, slots=True)
class RoleSendOutcome:
    """Typed result of a role-addressed (``peer_send_by_name``) dispatch.

    ``thread_id`` is the synthetic ``"role:{name}"`` handle (display only).
    ``delivery`` is one of ``persisted_silent`` (silent role message — inbox
    only), ``queued_wake`` / ``queued_notification`` (the role event was QUEUED
    on the live holder's bridge; ``delivered`` is flipped by the holder's forwarder
    on confirmed emission, NOT at send — v10 Q3-revised), or
    ``queued_for_replay`` (persisted but the holder was unreachable; the repair
    loop re-delivers).
    """

    thread_id: str
    message_id: str
    role: ResolvedRole
    delivery: str
    delivered_to_bridge_id: str

    def to_payload(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "message_id": self.message_id,
            "delivery": self.delivery,
            "delivered_to_bridge_id": self.delivered_to_bridge_id,
            "resolved_agent_id": self.role.agent_id,
            "resolved_agent_instance_id": self.role.agent_instance_id,
            "resolved_session_label": self.role.session_label,
        }


def _deliver_important_to_binding(
    *,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    recipient: BridgeBinding,
    delivered_prose: str,
    sender_agent_id: str,
    sender_agent_instance_id: str,
    sender_session_label: str,
    sender_parent_pid: int | None,
    sender_bridge_id: str,
    thread_id: str,
    message_id: str,
    thread_cursor: int,
    recipient_kind: str,
    recipient_key: str,
    delivery_external_id: str,
    reply_to_role: str = "",
) -> tuple[str, str]:
    """Deliver an IMPORTANT role message to a resolved recipient binding.

    Returns ``(delivery_kind, delivered_to_bridge_id)``: native wake adapter if
    one is registered for the recipient's ``agent_id`` (``queued_wake``),
    else a ``peer_message`` channel event on the recipient's bridge
    (``queued_notification``). Raises :class:`NativeWakeError` if a registered
    adapter fails — the loop-prevention contract treats IMPORTANT delivery as a
    hard promise.

    Role-only (called solely from :func:`dispatch_role_send`), so the v10
    Control #5 role-delivery meta keys (``recipient_kind`` / ``recipient_key`` /
    ``delivery_external_id``) ride the bridge event on BOTH paths — the
    ``queued_notification`` path stamps them on the ``append_event`` meta, the
    ``queued_wake`` path passes them via ``wake(delivery_meta=)`` (the
    native wake is the SAME bridge queue, NOT a direct push). The holder's bridge
    forwarder reads them off ``/events`` to recognise a role delivery and confirm
    ``delivered=true`` (the M7 flip). NEITHER path flips at send: the forwarder
    ``/peer/delivered`` is the SOLE delivered-authority for both (v10 Q3-revised,
    Codex BLOCKER-3).
    """
    adapter = peer_registry.wake_adapter_for(recipient.agent_id)
    if adapter is not None:
        try:
            delivered_to_bridge_id = adapter.wake(
                recipient_parent_pid=recipient.parent_pid,
                delivered_prose=delivered_prose,
                sender_agent_id=sender_agent_id,
                sender_agent_instance_id=sender_agent_instance_id,
                sender_session_label=sender_session_label,
                thread_id=thread_id,
                message_id=message_id,
                # REL-01 Fork 4: role sends carry the durable reply-to role so the
                # holder's envelope surfaces a role reply-to (two-way, reconnect-
                # surviving return leg). Empty for the direct-peer path.
                reply_to_role=reply_to_role,
                # v10 Control #5 / Q3-revised (Codex B3): the wake is the SAME
                # bridge queue as queued_notification — stamp the role keys so the
                # holder's forwarder confirms delivery (/peer/delivered), not a
                # send-flip.
                delivery_meta={
                    META_KEY_RECIPIENT_KIND: recipient_kind,
                    META_KEY_RECIPIENT_KEY: recipient_key,
                    META_KEY_DELIVERY_EXTERNAL_ID: delivery_external_id,
                },
            )
        except Exception as exc:  # noqa: BLE001 — adapter contract: any exception is failure
            logger.exception("native wake failed for %s", recipient.agent_id)
            raise NativeWakeError(
                f"native wake failed for {recipient.agent_id}: {exc}",
                peer_agent_id=recipient.agent_id,
            ) from exc
        return DELIVERY_QUEUED_WAKE, str(delivered_to_bridge_id)
    meta = build_peer_message_meta(
        sender_agent_id=sender_agent_id,
        sender_agent_instance_id=sender_agent_instance_id,
        sender_session_label=sender_session_label,
        sender_parent_pid=sender_parent_pid,
        sender_bridge_id=sender_bridge_id,
        recipient_agent_id=recipient.agent_id,
        recipient_agent_instance_id=recipient.agent_instance_id,
        thread_id=thread_id,
        message_id=message_id,
        thread_cursor=thread_cursor,
    )
    meta[META_KEY_RECIPIENT_KIND] = recipient_kind
    meta[META_KEY_RECIPIENT_KEY] = recipient_key
    meta[META_KEY_DELIVERY_EXTERNAL_ID] = delivery_external_id
    # REL-01 Fork 4 (Codex blocker): the no-adapter path serves Codex + streamable
    # recipients (no native wake adapter). Carry the reply-to on the PROSE so
    # two-way ROLE addressing works for them too — the SAME helper the native wake
    # envelope uses, so it is transport-agnostic and adds no role-coupling to the
    # forwarder. Empty ``reply_to_role`` yields the instance reply-to, matching the
    # native path.
    hinted_prose = f"{delivered_prose}\n\n" + build_wake_reply_hint(
        reply_to_role=reply_to_role,
        sender_agent_id=sender_agent_id,
        sender_agent_instance_id=sender_agent_instance_id,
        thread_id=thread_id,
        message_id=message_id,
    )
    bridge_manager.append_event(
        recipient.bridge_id, EVENT_PEER_MESSAGE, hinted_prose, meta,
    )
    return DELIVERY_QUEUED_NOTIFICATION, recipient.bridge_id


def dispatch_role_send(
    *,
    bridge_manager: BridgeSessionManager,
    peer_registry: PeerRegistry,
    agent_messaging_service: Any,
    role_name: str,
    role: ResolvedRole,
    sender_bridge_id: str,
    sender_agent_id: str,
    sender_agent_instance_id: str,
    sender_session_label: str,
    sender_parent_pid: int | None,
    content: list[TextPart],
    message_id: str,
    reply_to_role: str = "",
) -> RoleSendOutcome:
    """Persist-first role-addressed dispatch (v10 Controls #1/#3/#4).

    The caller has already existence-gated the role (Scope-decision C: an
    unknown role name is REJECTED before reaching here, so a typo can't seed a
    durable-but-undeliverable row). This function then:

    1. **Persist-first (#4.1/#1.1):** ONE authoritative ``upsert_state`` of the
       complete envelope (``delivered=false``), BEFORE resolving the live
       holder binding — so a role send never requires a live binding.
    2. **Silent → inbox-only:** a non-IMPORTANT role message is never
       auto-emitted (loop prevention); it is durable + re-readable via the
       role-inbox section.
    3. **Best-effort deliver (#4.2):** resolve the current holder's live
       binding and emit; on no-binding / wake failure / queue full →
       ``queued_for_replay`` (success — the row persists, Control #5 re-delivers).
       On success, NEITHER path flips ``delivered`` here at send (v10 Q3-revised,
       Codex BLOCKER-3): both the ``queued_notification`` channel event AND the
       ``queued_wake`` wake are merely QUEUED on the holder's bridge
       (the native wake is the same append_event queue, not a direct push), and
       both carry the Control #5 role keys. The holder's forwarder is the SOLE
       flip authority for both (POST /peer/delivered after confirmed emission),
       so an unconfirmed queued event stays ``delivered=false`` for the repair
       drain to re-deliver.
    """
    prose = "\n".join(part.text for part in content)
    marker_match = IMPORTANT_MARKER_RE.match(prose)
    important = marker_match is not None
    agent_messaging_service.persist_role_message(
        recipient_kind=RECIPIENT_KIND_ROLE,
        recipient_key=role_name,
        message_id=message_id,
        sender_agent_id=sender_agent_id,
        sender_agent_instance_id=sender_agent_instance_id,
        sender_session_label=sender_session_label,
        important=important,
        content=content,
    )
    thread_id = f"role:{role_name}"
    if not important:
        return RoleSendOutcome(
            thread_id=thread_id,
            message_id=message_id,
            role=role,
            delivery=DELIVERY_PERSISTED_SILENT,
            delivered_to_bridge_id="",
        )
    assert marker_match is not None  # narrowed by ``important``
    delivered_prose = prose[marker_match.end():]
    delivery_external_id = role_message_external_id(
        RECIPIENT_KIND_ROLE, role_name, message_id,
    )
    try:
        recipient = peer_registry.resolve(role.agent_id, role.agent_instance_id)
        delivery_kind, delivered_to_bridge_id = _deliver_important_to_binding(
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
            recipient=recipient,
            delivered_prose=delivered_prose,
            sender_agent_id=sender_agent_id,
            sender_agent_instance_id=sender_agent_instance_id,
            sender_session_label=sender_session_label,
            sender_parent_pid=sender_parent_pid,
            sender_bridge_id=sender_bridge_id,
            thread_id=thread_id,
            message_id=message_id,
            thread_cursor=0,
            recipient_kind=RECIPIENT_KIND_ROLE,
            recipient_key=role_name,
            delivery_external_id=delivery_external_id,
            reply_to_role=reply_to_role,
        )
    except (
        PeerUnreachableError,
        PeerAmbiguousError,
        BridgeNotFoundError,
        BridgeQueueFullError,
        NativeWakeError,
    ):
        # Holder unreachable — the envelope is durable; replay on next attach.
        return RoleSendOutcome(
            thread_id=thread_id,
            message_id=message_id,
            role=role,
            delivery=DELIVERY_QUEUED_FOR_REPLAY,
            delivered_to_bridge_id="",
        )
    # v10 Q3 — REVISED (Codex BLOCKER-3 / Architect 2026-06-19): NO send-time
    # flip for EITHER path. Native wake is NOT a direct push — the claude_code
    # adapter's wake() is the SAME ``manager.append_event`` bridge queue as the
    # queued_notification path (verified plugin.py:1726+), and a closed/reconnected bridge
    # drops queued events. Both paths therefore stamp the role keys on the
    # bridge EVENT and rely on the holder's forwarder POST /peer/delivered
    # (Control #5 M7) as the SOLE delivered-authority: a reconnect-before-drain
    # leaves ``delivered=false`` and the repair drain re-delivers, for BOTH
    # paths. This is STRONGER than the old optimistic send-flip (forwarder-
    # CONFIRMED at-least-once) and removes the takeover-between-wake-and-flip
    # suppression — the flip is always whoever currently holds + drains.
    return RoleSendOutcome(
        thread_id=thread_id,
        message_id=message_id,
        role=role,
        delivery=delivery_kind,
        delivered_to_bridge_id=delivered_to_bridge_id,
    )


def build_wake_reply_hint(
    *,
    reply_to_role: str,
    sender_agent_id: str,
    sender_agent_instance_id: str,
    thread_id: str,
    message_id: str,
) -> str:
    """The reply-to hint carried on an IMPORTANT delivery (REL-01 Fork 4).

    A role-addressed send (``reply_to_role`` set) surfaces a ROLE reply-to
    (``peer_send_by_name name=<role>``) so the return leg survives a holder
    reconnect — an instance reply-to churns on every reconnect, the original
    defect. A direct instance send keeps the same-connection instance reply-to.
    Shared by BOTH the native-wake envelope (``plugin.wake``) and the no-adapter
    channel-event path (``_deliver_important_to_binding`` below), so Codex /
    streamable recipients get the same two-way role reply-to — transport-agnostic,
    with no role-coupling in the forwarder. ``reply_to_role`` is treated as an
    opaque, operator-defined string (never enumerated / special-cased).
    """
    if reply_to_role:
        return (
            f"(reply via peer_send_by_name with name={reply_to_role}; "
            f'prefix prose with "IMPORTANT: " only if you need a response. '
            f"thread_id={thread_id}, message_id={message_id}.)"
        )
    return (
        f"(reply via peer_send with peer_id={sender_agent_id}, "
        f"peer_agent_instance_id={sender_agent_instance_id}; "
        f'prefix prose with "IMPORTANT: " only if you need a response. '
        f"thread_id={thread_id}, message_id={message_id}.)"
    )


def build_peer_message_meta(
    *,
    sender_agent_id: str,
    sender_agent_instance_id: str,
    sender_session_label: str,
    sender_parent_pid: int | None,
    sender_bridge_id: str,
    recipient_agent_id: str,
    recipient_agent_instance_id: str,
    thread_id: str,
    message_id: str,
    thread_cursor: int,
) -> dict[str, object]:
    """Construct the ``peer_message`` channel-event metadata dict."""
    return {
        "thread_id": thread_id,
        "message_id": message_id,
        "thread_cursor": thread_cursor,
        "from_agent_id": sender_agent_id,
        "from_agent_instance_id": sender_agent_instance_id,
        "from_session_label": sender_session_label,
        "from_parent_pid": sender_parent_pid,
        "to_agent_id": recipient_agent_id,
        "to_agent_instance_id": recipient_agent_instance_id,
        "from_bridge_id": sender_bridge_id,
        "important": True,
        "sent_at": datetime.now(UTC).isoformat(),
    }


__all__ = [
    "DELIVERY_QUEUED_NOTIFICATION",
    "DELIVERY_PERSISTED_SILENT",
    "DELIVERY_QUEUED_FOR_REPLAY",
    "DELIVERY_QUEUED_WAKE",
    "EVENT_PEER_MESSAGE",
    "IMPORTANT_MARKER_RE",
    "NativeWakeError",
    "PeerSendOutcome",
    "RoleSendOutcome",
    "build_peer_message_meta",
    "build_wake_reply_hint",
    "dispatch_peer_send",
    "dispatch_role_send",
]
