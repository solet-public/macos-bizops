"""Shared role-claim body — ONE implementation behind TWO transports.

``peer_claim_role`` is reachable two ways, and the difference between them is
the whole point of this module:

* ``/process/call`` → the ``peer_claim_role`` platform verb. MODEL_INITIATED.
  A ``/rename`` claim is a genuine model turn and MUST stamp
  ``last_model_activity_at``.
* ``{API_PREFIX}/{bridge_id}/peer/claim_role`` → the dedicated bridge route.
  INFRA. The forwarder's housekeeping claim (open / reconnect / steady-state
  re-assert) runs with no model turn behind it and MUST NOT stamp.

Before the split, the forwarder's housekeeping claim traversed the
MODEL_INITIATED ``/process/call`` route every ~176s forever, phantom-stamping
model activity with no model turn — which silently marked owed IMPORTANT wakes
to an idle session as consumed — and fired a ``bridge_delivery_result``
notification each tick. See the 2026-07-29 Architect ruling,
``workbench/2026-07-29_role_claim_route_classification_architect_ruling.md``.

WHY A SHARED HELPER RATHER THAN THE ``peer_send_by_name`` PRECEDENT. That
precedent reaches its synchronous response through ``_peer_send_by_name_impl``,
a parallel implementation of the verb in ``http_routes``. It is cheap there
because ``peer_send_by_name`` is nearly pure dispatch. It is NOT cheap here: a
claim terminates in the handover settle below, which owns semantics a duplicate
would have to reproduce exactly — ``refreshed`` surfacing as ``updated`` with no
wake, ``claimed``/``displaced`` notifying the displaced prior at its CURRENT
bridge plus the new-holder confirm, the non-serializable ``prior`` never
reaching a public result, and every declared ``return_value_schema`` property
being present. A duplicate that got any of those subtly wrong would fire phantom
handover wakes — the same defect class the split exists to remove. So the
notify + serialization contract is single-sourced here and both transports call
it. The Architect ruling ratifies this shape (§4, Condition 1 clarification).

:class:`RoleClaimOrigin` records WHICH transport a claim arrived on. It is
declarative only — this module never stamps and never infers model-ness from
it. Route classification remains the single source of truth for stamping and
lives in :mod:`route_activity`, keyed on path alone. The parameter exists so
both call sites state their classification out loud, and so whoever adds a
third transport has to answer the question rather than inherit an answer.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from ananta.llm.agent_messaging.models import TextPart
from ananta.llm.agent_messaging.role_binding import (
    HOLDER_KIND_INFERENCE_PROVIDER,
    HOLDER_KIND_SESSION,
)

from .bridge_sessions import BridgeNotFoundError, BridgeQueueFullError
from .constants import (
    SYSTEM_AGENT_ID,
    SYSTEM_ROLE_HANDOVER_ID,
    SYSTEM_ROLE_HANDOVER_LABEL,
)
from .peer_dispatch import NativeWakeError, dispatch_peer_send
from .peer_registry import (
    PeerAmbiguousError,
    PeerSessionAmbiguousError,
    PeerUnreachableError,
)
from .role_binding_store import (
    HolderClaim,
    claim_role_binding_v4,
    session_claim_requires_session_id,
    upsert_role_entity,
)
from .system_slots import SystemSlotClaimDecision, evaluate_system_slot_claim

if TYPE_CHECKING:  # pragma: no cover — type-only references
    from .bridge_sessions import BridgeSessionManager
    from .peer_registry import PeerRegistry

logger = logging.getLogger(__name__)


class RoleClaimOrigin(Enum):
    """Which transport a claim arrived on — declarative, never a stamp switch.

    ``MODEL_TURN`` is the ``/process/call`` verb (a real model turn, e.g.
    ``/rename``); ``INFRA`` is the bridge route the forwarder uses for
    housekeeping. Nothing in this module branches on it beyond logging: the
    stamping decision belongs to the route layer, which classifies on path
    alone and cannot be told otherwise by a caller.
    """

    MODEL_TURN = "model_turn"
    INFRA = "infra"


@dataclass(frozen=True)
class RoleClaimSuccess:
    """A landed claim, in the shape both transports must publish.

    :meth:`to_public` is the SINGLE definition of the public payload. Every
    property declared by the verb's ``return_value_schema`` must appear in it:
    ``ExecutionContext.store_result`` raises ``PlaceholderResolutionError`` on a
    declared-but-absent property, which fails the action AFTER the binding write
    has already landed — reporting a bogus failure for a claim that actually
    succeeded. Keeping one definition is what stops the two transports drifting
    into that trap independently.
    """

    action: str
    name: str
    agent_instance_id: str
    agent_session_id: str

    def to_public(self) -> dict[str, str]:
        """The claim outcome as published to callers of EITHER transport."""
        return {
            "action": self.action,
            "name": self.name,
            "agent_instance_id": self.agent_instance_id,
            "agent_session_id": self.agent_session_id,
        }


@dataclass(frozen=True)
class RoleClaimFailure:
    """A refused claim. Each transport renders this into its own error shape."""

    code: str
    message: str


RoleClaimResult = RoleClaimSuccess | RoleClaimFailure


def displaced_prose(name: str, new_agent_instance_id: str) -> str:
    """REL-04 displaced-holder notice. ``name`` is an opaque operator-defined role."""
    return (
        f"IMPORTANT: You have been displaced from role {name!r} by instance "
        f"{new_agent_instance_id}. You no longer hold this role — a role-addressed "
        f"message to {name!r} now reaches the new holder. Re-claim the role "
        f"(/rename) if this displacement was not intended."
    )


def new_holder_prose(name: str) -> str:
    """REL-04 new-holder confirmation. ``name`` is an opaque operator-defined role."""
    return (
        f"IMPORTANT: You now hold role {name!r}. Drain your role backlog with "
        f"peer_inbox(include_important=true) — role-addressed messages sent to "
        f"{name!r} while it was held by another session (or unclaimed) are waiting."
    )


def send_handover_notice(
    *,
    bridge_manager: BridgeSessionManager | None,
    peer_registry: PeerRegistry | None,
    agent_messaging_service: Any | None,
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
    if bridge_manager is None or peer_registry is None or agent_messaging_service is None:
        logger.warning(
            "REL-04 %s notice skipped (bridge not started): agi=%s",
            kind, peer_agent_instance_id,
        )
        return False
    try:
        dispatch_peer_send(
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
            agent_messaging_service=agent_messaging_service,
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


def is_genuine_displacement(
    prior: Any, new_agent_instance_id: str, new_agent_session_id: str,
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


def displaced_target(
    prior: Any, peer_registry: PeerRegistry | None,
) -> tuple[str, str]:
    """Route the displaced-holder notice to the prior holder's CURRENT bridge.

    The role binding records the ``agent_instance_id`` as of the CLAIM; by the
    time a reconnect displaces it, that instance has rotated. Resolve the
    prior's live binding by its stable session id; fall back to the recorded
    instance when it has no session id, no live binding, or an ambiguous one
    (best-effort — an undeliverable notice never gates a claim).
    """
    prior_sid = str(getattr(prior, "agent_session_id", "") or "")
    if prior_sid and peer_registry is not None:
        try:
            live = peer_registry.resolve_by_agent_session_id(prior_sid)
        except PeerSessionAmbiguousError as exc:
            logger.warning(
                "REL-04 displaced-notice: ambiguous session id %r — "
                "falling back to recorded instance: %s", prior_sid, exc,
            )
            live = None
        if live is not None:
            return live.agent_id, live.agent_instance_id
    return prior.agent_id, prior.agent_instance_id


def notify_role_handover(
    *,
    bridge_manager: BridgeSessionManager | None,
    peer_registry: PeerRegistry | None,
    agent_messaging_service: Any | None,
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
            name, getattr(prior, "holder_identity", {}), new_agent_id,
            new_agent_instance_id,
        )
    elif is_genuine_displacement(prior, new_agent_instance_id, new_agent_session_id):
        target_agent_id, target_instance_id = displaced_target(prior, peer_registry)
        send_handover_notice(
            bridge_manager=bridge_manager,
            peer_registry=peer_registry,
            agent_messaging_service=agent_messaging_service,
            peer_id=target_agent_id or new_agent_id,
            peer_agent_instance_id=target_instance_id,
            prose=displaced_prose(name, new_agent_instance_id),
            kind="displaced-holder",
        )
    send_handover_notice(
        bridge_manager=bridge_manager,
        peer_registry=peer_registry,
        agent_messaging_service=agent_messaging_service,
        peer_id=new_agent_id,
        peer_agent_instance_id=new_agent_instance_id,
        prose=new_holder_prose(name),
        kind="new-holder",
    )


def settle_role_handover(
    *,
    bridge_manager: BridgeSessionManager | None,
    peer_registry: PeerRegistry | None,
    agent_messaging_service: Any | None,
    name: str,
    agent_id: str,
    agent_instance_id: str,
    agent_session_id: str,
    outcome: dict[str, Any],
) -> RoleClaimSuccess:
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
    is NOT json-serializable, so it MUST NOT reach the public result (result
    persistence json.dumps would TypeError on a real displace — Codex BLOCKER-1).
    Returning a :class:`RoleClaimSuccess` is what structurally prevents that:
    ``prior`` has nowhere to ride along. ``agent_session_id`` is the RESOLVED id
    (the caller may omit it and have it sourced from ``peer_binding``), so echoing
    it is what tells the claimant which session id its role is now keyed on.
    """
    action = str(outcome.get("action") or "")
    resolved_name = str(outcome.get("name") or name)
    resolved_instance = str(outcome.get("agent_instance_id") or agent_instance_id)
    if action == "refreshed":
        # /rename refresh contract: an idempotent self-re-claim reports
        # ``updated`` and fires NO wake. This is the branch that keeps a
        # steady-state re-assert from waking its own session forever.
        return RoleClaimSuccess(
            action="updated",
            name=resolved_name,
            agent_instance_id=resolved_instance,
            agent_session_id=agent_session_id,
        )
    notify_role_handover(
        bridge_manager=bridge_manager,
        peer_registry=peer_registry,
        agent_messaging_service=agent_messaging_service,
        name=name,
        new_agent_id=agent_id,
        new_agent_instance_id=agent_instance_id,
        new_agent_session_id=agent_session_id,
        prior=outcome.get("prior"),
    )
    return RoleClaimSuccess(
        action=action,
        name=resolved_name,
        agent_instance_id=resolved_instance,
        agent_session_id=agent_session_id,
    )


def claim_role_for_session(
    *,
    origin: RoleClaimOrigin,
    name: str,
    agent_id: str,
    agent_instance_id: str,
    agent_session_id: str,
    session_label: str,
    state_service: Any | None,
    bridge_manager: BridgeSessionManager | None,
    peer_registry: PeerRegistry | None,
    agent_messaging_service: Any | None,
    call_context: Any | None,
) -> RoleClaimResult:
    """Claim-or-replace the ``agent_role_binding`` row for ``name`` (v10 #2.C).

    The single body behind both transports. ``origin`` is recorded, never acted
    on — see the module docstring.

    ``call_context`` MUST be the SERVER-BUILT context so the §6.1 reserved-
    keyspace gate cannot be forged. The verb lifts it out of ``state``; the
    bridge route passes ``None``, which is correct AND fail-closed: with no
    plugin principal every ``sys:`` name is refused, and the forwarder has no
    business claiming a system slot. A normal role is unaffected either way.
    """
    name = name.strip()
    agent_id = agent_id.strip()
    agent_instance_id = agent_instance_id.strip()
    agent_session_id = agent_session_id.strip()
    session_label = session_label or name
    if not name or not agent_id or not agent_instance_id:
        return RoleClaimFailure(
            code="missing_argument",
            message=(
                "peer_claim_role requires non-empty 'name', "
                "'agent_id', and 'agent_instance_id'."
            ),
        )
    # §6.1 reserved-keyspace gate: a general claim never assigns a SESSION-FILLED
    # system slot (that is the §D.9 auto-assignment lane), and a PLUGIN-OWNED slot
    # is claimable ONLY by its declared owner — verified against the SERVER-BUILT
    # principal, never spoofable caller input. A normal (non-sys:) role →
    # NOT_SYSTEM → proceeds.
    verdict = evaluate_system_slot_claim(name, call_context)
    if verdict.decision is SystemSlotClaimDecision.REJECT:
        return RoleClaimFailure(
            code="system_slot_claim_denied",
            message=verdict.reason,
        )
    if state_service is None:
        return RoleClaimFailure(
            code="state_service_unavailable",
            message="state_service is not bound on this homunculus.",
        )
    # REL-07(1): source the claimant's stable session id from its OWN live
    # peer_binding row when the claim args omit it (they always do). An empty
    # agent_session_id makes the reconnect CAS (keyed on it alone) unable to
    # re-point this role — the class fresh-C's REL-07 diagnostic surfaced.
    if not agent_session_id and peer_registry is not None:
        agent_session_id = peer_registry.agent_session_id_for_instance(
            agent_instance_id,
        )
    # carry-forward (c) [§4.5.3/§11]: a durable session claim MUST carry a stable
    # agent_session_id (the reconnect CAS + the §D.9 succession key on it). The
    # pre-cutover 'no worse than pre-fix' empty-allowed fallback DIES at the §9
    # cutover — reject an unsourceable session claim rather than write a dead binding.
    if session_claim_requires_session_id(HOLDER_KIND_SESSION, agent_session_id):
        return RoleClaimFailure(
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
    settled = settle_role_handover(
        bridge_manager=bridge_manager,
        peer_registry=peer_registry,
        agent_messaging_service=agent_messaging_service,
        name=name,
        agent_id=agent_id,
        agent_instance_id=agent_instance_id,
        agent_session_id=agent_session_id,
        outcome=outcome,
    )
    logger.debug(
        "role claim settled: name=%r action=%s origin=%s agi=%s",
        name, settled.action, origin.value, agent_instance_id,
    )
    return settled


__all__ = [
    "RoleClaimFailure",
    "RoleClaimOrigin",
    "RoleClaimResult",
    "RoleClaimSuccess",
    "claim_role_for_session",
    "displaced_prose",
    "displaced_target",
    "is_genuine_displacement",
    "new_holder_prose",
    "notify_role_handover",
    "send_handover_notice",
    "settle_role_handover",
]
