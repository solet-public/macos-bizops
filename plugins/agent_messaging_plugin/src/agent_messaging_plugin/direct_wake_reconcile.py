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

Deaf-wake Guard-1 severity fix (Architect-ruled): ``cap_reached`` is measured by
Guard 1 in ``AgentMessagingService._stamp_consumed_rows`` — a LATER model-
initiated platform call from the recipient. Local-only tool work (Bash/Edit/Read,
no bridge call) never advances that stamp, so a session at maximum effort looks
identical to a dead one, and the escalation this module sends reads as a
confident "not consumed" when it only ever measured "no bridge call since
emission." The registry's binding presence, by contrast, is refreshed by an
always-on ~200s heartbeat independent of model activity — the one signal that
CAN see a session doing local work. So: heartbeat GATES THE NOTIFICATION'S
SEVERITY, never the row's terminal state. Heartbeat-as-consumption was
considered and REJECTED — wiring it into the drain/consumed predicate would
silently retire rows this module never confirmed were read, trading a false
alarm for a false silence. ``recipient_gone`` stays alarm-class unconditionally:
a registered route disappearing is a stronger and different claim than a stale
activity stamp, and a heartbeat check cannot soften it. See :meth:`_severity`.
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

# Notification severity — heartbeat-gated (see module docstring + _severity).
# Never written to the row; never read back by any drain/escalation filter.
NOTICE_SEVERITY_INFO = "info"
NOTICE_SEVERITY_ALARM = "alarm"


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
            severity = self._severity(row, reason, "recipient_agent_session_id")
            self._service.mark_direct_escalated(message_id=message_id, reason=reason)
            self._notify_sender(
                row,
                recipient=str(row.get("recipient_agent_id") or ""),
                message_id=message_id,
                reason=reason,
                severity=severity,
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
            severity = self._severity(row, reason, "emitted_to_agent_session_id")
            self._service.mark_role_escalated(external_id=external_id, reason=reason)
            self._notify_sender(
                row,
                recipient=f"role {row.get('recipient_key') or ''}",
                message_id=message_id,
                reason=reason,
                severity=severity,
            )
            escalated += 1
        return escalated

    def _reason(self, row: dict[str, object]) -> str:
        """Classify exhausted emission cap vs a disappeared recipient route.

        Purely a state-column classification — unaffected by, and never
        affecting, :meth:`_severity` below. This is the value written to
        ``escalation_reason`` and is load-bearing for the re-home CAS
        (``rehome_owed_role_wakes``/``rehome_owed_direct_wakes``), which reads
        it back to decide whether a reconnect may revive the row.
        """
        return (
            ESCALATION_REASON_CAP
            if _as_int(row.get("emit_count")) >= self._cap
            else ESCALATION_REASON_GONE
        )

    def _severity(
        self, row: dict[str, object], reason: str, session_id_field: str,
    ) -> str:
        """Heartbeat gates the NOTIFICATION's severity — never the escalation itself.

        Only ``cap_reached`` can downgrade to :data:`NOTICE_SEVERITY_INFO`: the
        registry binding is refreshed by an always-on ~200s heartbeat,
        independent of model activity, so it is the one signal that can tell
        "busy with local work" apart from "actually deaf" — exactly what Guard 1
        (a later model-initiated platform call) cannot see. ``recipient_gone``
        stays :data:`NOTICE_SEVERITY_ALARM` unconditionally: a registered route
        disappearing is a stronger, different claim than a heartbeat check bears
        on.

        This return value feeds ONLY :meth:`_notify_sender`'s prose/tag. It is
        never written to the row, never read by ``mark_*_escalated`` (which
        already ran by the time this fires), and never enters the drain filter
        — the row terminates on cap exactly as RIDER-1 requires either way.
        """
        if reason != ESCALATION_REASON_CAP:
            return NOTICE_SEVERITY_ALARM
        session_id = str(row.get(session_id_field) or "")
        return (
            NOTICE_SEVERITY_INFO
            if self._recipient_alive(session_id)
            else NOTICE_SEVERITY_ALARM
        )

    def _recipient_alive(self, session_id: str) -> bool:
        """True iff the registry holds a live binding under this session id.

        Any doubt — an empty id (older client, pre-migration row) or an
        ambiguous match (``PeerSessionAmbiguousError`` IS-A
        ``PeerUnreachableError``) — resolves to NOT alive, keeping today's
        alarm-class behavior rather than guessing a downgrade this module
        cannot actually confirm.
        """
        if not session_id:
            return False
        try:
            binding = self._peer_registry.resolve_by_agent_session_id(session_id)
        except PeerUnreachableError:
            return False
        return binding is not None

    def _notify_sender(
        self,
        row: dict[str, object],
        *,
        recipient: str,
        message_id: str,
        reason: str,
        severity: str,
    ) -> None:
        """Append a deaf-wake channel event on the sender's live bridge.

        ``severity`` only changes this notification's tag and tone (see
        :meth:`_severity`) — the row is already terminal by the time this
        runs, and ``reason`` is unaffected either way. Meta stays the
        canonical ``{flow_id}``-only shape: the Claude Code MCP transport
        drops the whole event past a handful of meta keys, so severity MUST
        travel in the prose, the only receiver-visible channel there.
        """
        emit_count = _as_int(row.get("emit_count"))
        created_at = str(row.get("created_at") or "")
        acknowledgement = _consumption_acknowledgement(row)
        header = (
            f"your IMPORTANT to {recipient} (message_id={message_id}, "
            f"sent {created_at}, emitted {emit_count}x)"
        )
        if reason == ESCALATION_REASON_CAP and acknowledgement != (
            "watcher delivery acknowledgement"
        ):
            # H2 prose-narrowing (its own defect, shipped alongside the
            # severity gate): name exactly what Guard 1 measures — a LATER
            # model-initiated platform call — never "no consumption
            # acknowledgement". Local-only tool work (Bash/Edit/Read) stamps
            # nothing on this signal, so it cannot see a session working.
            body = (
                f"{header} has no model-initiated platform call from this "
                "session since emission — local-only work is invisible to "
                "this signal."
            )
        elif reason == ESCALATION_REASON_CAP:
            body = (
                f"{header} has no recorded consumption acknowledgement "
                f"({reason}). The platform observed no {acknowledgement} "
                "before the emission cap."
            )
        else:
            body = (
                f"{header} has no recorded consumption acknowledgement "
                f"({reason}). The recipient's registered route disappeared "
                f"before the platform observed a {acknowledgement}."
            )
        if severity == NOTICE_SEVERITY_INFO:
            tag = "deaf_wake_notice (severity=info)"
            tail = (
                "The recipient's registry presence is current — an "
                "always-on heartbeat independent of model activity — so "
                "this reads as local-only work, not a lost message. No "
                "action needed on this alone; inspect operational state if "
                "you have other evidence the recipient is stuck."
            )
        else:
            tag = "deaf_wake_escalation (severity=alarm)"
            tail = (
                "This does not prove the recipient failed to see or act on "
                "the message; inspect operational state before resending "
                "or using another route."
            )
        prose = f"{tag}: {body} {tail}"
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
    "NOTICE_SEVERITY_ALARM",
    "NOTICE_SEVERITY_INFO",
    "DirectWakeReconciler",
]
