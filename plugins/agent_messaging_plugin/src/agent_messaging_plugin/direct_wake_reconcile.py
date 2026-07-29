"""REL-05 server-side escalation reconciler (rides the REL-09 sweeper on_tick).

The client-side repair drain re-emits owed IMPORTANT sends until they are
consumed; this reconciler is the SERVER-side terminal step for the ones that
never will be. It does NOT emit — it only ESCALATES an owed wake (BOTH direct
AND role rows — RIDER-1 generalized it from direct-only) that has either hit the
emit cap without the route-specific consumption acknowledgement
(``cap_reached``) or been owed past the cap-equivalent time after its recipient
route disappeared (``recipient_gone``): stamp the row terminal and notify the
SENDER's live bridge — the party with context to inspect operational state — or
a loud server log if the sender's bridge is gone. Neither reason proves that the
recipient failed to see or act on the message; MCP consumption is a model-activity
proxy, while watcher consumption is an explicit cursor acknowledgement.

RIDER-1 (role terminal-clear): before role rows could escalate, a capped-
unconsumed role IMPORTANT went DORMANT (consumed=false, emit_count=cap) and never
left the ``consumed=false`` owed set — so ≥cap dormant rows for one role filled
the oldest limit-page of the drain and STARVED genuinely-owed newer rows behind
them. Escalating a role row flips ``escalated`` so it drops from the drain's
equality filter (kills the starvation) and fires the sender terminal signal
(kills the silent-fail) — exactly as direct rows already did. The class name
stays ``DirectWakeReconciler`` (Rev-A named it as the thing to generalize).

Loop-prevention (S3): the escalation is a ``post_message`` channel event on the
sender's bridge, NEVER a ``peer_send``, so it cannot re-enter the peer-send path
(no send→wake→send cycle by construction). It rides the existing REL-09 sweeper
``on_tick`` hook (the INF-02 serve-timeout-sweep precedent), so it needs no new
thread.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ananta.llm.agent_messaging.schema import (
    ESCALATION_REASON_CAP,
    ESCALATION_REASON_GONE,
)

from .models import WATCH_AGENT_INSTANCE_PREFIX
from .peer_registry import PeerAmbiguousError, PeerUnreachableError

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from ananta.llm.agent_messaging.service import AgentMessagingService

    from .bridge_sessions import BridgeSessionManager
    from .peer_registry import PeerRegistry

logger = logging.getLogger(__name__)

# The escalation rides a plain post_message channel event on the sender's bridge
# — deliberately NOT a peer_send (loop prevention). "post_message" is the same
# event type the reconnect announcement uses.
ESCALATION_EVENT_TYPE = "post_message"


def _as_int(value: object) -> int:
    """A state-row integer cell coerced to ``int`` (0 for a missing/other cell)."""
    return value if isinstance(value, int) else 0


def _consumption_acknowledgement(row: dict[str, object]) -> str:
    """Name the route-specific consumption signal the platform did not observe."""
    recipient_instance = str(
        row.get("emitted_to_agent_instance_id")
        or row.get("recipient_agent_instance_id")
        or "",
    )
    if recipient_instance.startswith(WATCH_AGENT_INSTANCE_PREFIX):
        return "watcher delivery acknowledgement"
    return "qualifying model-activity consumption acknowledgement"


class DirectWakeReconciler:
    """Escalate owed direct-wake rows past the cap / time bound (on_tick rider)."""

    def __init__(
        self,
        *,
        service: AgentMessagingService,
        bridge_manager: BridgeSessionManager,
        peer_registry: PeerRegistry,
        cap: int,
        re_emit_window_s: float,
        clock: Callable[[], datetime],
    ) -> None:
        self._service = service
        self._bridge_manager = bridge_manager
        self._peer_registry = peer_registry
        self._cap = cap
        self._re_emit_window_s = re_emit_window_s
        self._clock = clock

    def reconcile(self) -> int:
        """One escalation pass over BOTH owed kinds; return the count escalated.

        RIDER-1 generalized this from direct-only to cover role rows too — a
        capped-unconsumed ROLE IMPORTANT otherwise went dormant and starved the
        drain (never leaving the ``consumed=false`` owed set). Both kinds share
        the same escalation ACTION (stamp terminal → drop from the drain → notify
        the sender); only the id key + recipient label differ. A fault propagates
        to the sweeper's guarded ``on_tick`` wrapper (logged loud; the reaper
        survives), so this stays a straight-line pass.
        """
        now = self._clock()
        total = self._escalate_direct(now) + self._escalate_role(now)
        if total:
            logger.info(
                "REL-05/RIDER-1: escalated %d unconsumed wake(s)", total,
            )
        return total

    def _escalate_direct(self, now: datetime) -> int:
        """Escalate owed DIRECT rows past cap / time (id = message_id)."""
        escalated = 0
        for row in self._service.list_escalatable_direct(
            now=now, cap=self._cap, re_emit_window_s=self._re_emit_window_s,
        ):
            message_id = str(row.get("message_id") or "")
            if not message_id:
                continue
            reason = self._reason(row)
            self._service.mark_direct_escalated(message_id=message_id, reason=reason)
            self._notify_sender(
                row,
                recipient=str(row.get("recipient_agent_id") or ""),
                message_id=message_id,
                reason=reason,
            )
            escalated += 1
        return escalated

    def _escalate_role(self, now: datetime) -> int:
        """Escalate owed ROLE rows past cap / time (id = external_id) — RIDER-1."""
        escalated = 0
        for row in self._service.list_escalatable_role(
            now=now, cap=self._cap, re_emit_window_s=self._re_emit_window_s,
        ):
            external_id = str(row.get("external_id") or "")
            message_id = str(row.get("message_id") or "")
            if not external_id or not message_id:
                continue
            reason = self._reason(row)
            self._service.mark_role_escalated(external_id=external_id, reason=reason)
            self._notify_sender(
                row,
                recipient=f"role {row.get('recipient_key') or ''}",
                message_id=message_id,
                reason=reason,
            )
            escalated += 1
        return escalated

    def _reason(self, row: dict[str, object]) -> str:
        """Classify exhausted emission cap vs a disappeared recipient route."""
        return (
            ESCALATION_REASON_CAP
            if _as_int(row.get("emit_count")) >= self._cap
            else ESCALATION_REASON_GONE
        )

    def _notify_sender(
        self,
        row: dict[str, object],
        *,
        recipient: str,
        message_id: str,
        reason: str,
    ) -> None:
        """Append a deaf-wake-escalation channel event on the sender's live bridge."""
        emit_count = _as_int(row.get("emit_count"))
        created_at = str(row.get("created_at") or "")
        acknowledgement = _consumption_acknowledgement(row)
        if reason == ESCALATION_REASON_CAP:
            observation = (
                f"The platform observed no {acknowledgement} before the "
                "emission cap."
            )
        else:
            observation = (
                "The recipient's registered route disappeared before the "
                f"platform observed a {acknowledgement}."
            )
        prose = (
            f"deaf_wake_escalation: your IMPORTANT to {recipient} "
            f"(message_id={message_id}, sent {created_at}, emitted {emit_count}x) "
            f"has no recorded consumption acknowledgement ({reason}). {observation} "
            "This does not prove the recipient failed to see or act on the message; "
            "inspect operational state before resending or using another route."
        )
        bridge_id = self._resolve_sender_bridge(row)
        if bridge_id is None:
            logger.warning("deaf_wake_escalation (sender bridge gone): %s", prose)
            return
        meta: dict[str, object] = {"flow_id": f"deaf-wake-escalation-{message_id}"}
        try:
            self._bridge_manager.append_event(
                bridge_id, ESCALATION_EVENT_TYPE, prose, meta,
            )
        except Exception:  # noqa: BLE001 — best-effort notify; the row is already terminal
            logger.warning(
                "deaf_wake_escalation append failed: %s", prose, exc_info=True,
            )

    def _resolve_sender_bridge(self, row: dict[str, object]) -> str | None:
        """The sender's CURRENT live bridge id, or ``None`` if unresolvable.

        Resolves via the live registry (the recorded ``sender_bridge_id`` is
        stale after a sender reconnect). Best-effort: a sender whose
        agent_instance_id has rotated resolves to nothing → loud log, no crash.
        """
        sender_agent_id = str(row.get("sender_agent_id") or "")
        sender_instance = str(row.get("sender_agent_instance_id") or "")
        if not sender_agent_id or not sender_instance:
            return None
        try:
            binding = self._peer_registry.resolve(sender_agent_id, sender_instance)
        except (PeerUnreachableError, PeerAmbiguousError):
            return None
        return binding.bridge_id


__all__ = [
    "ESCALATION_EVENT_TYPE",
    "ESCALATION_REASON_CAP",
    "ESCALATION_REASON_GONE",
    "DirectWakeReconciler",
]
