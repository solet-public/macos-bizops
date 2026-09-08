"""Pair-completeness notifications for model-policy producer dispatches.

This rider owns only the policy-specific ten-minute pairing window.  The
general lifecycle sweep remains responsible for state transitions, deadlines,
and other notices; keeping this bounded policy in a separate module preserves
that sweep's readability and its independently measured maintenance score.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .peer_registry import PeerAmbiguousError, PeerSessionAmbiguousError, PeerUnreachableError
from .session_lifecycle_verbs import drive_on_delivery
from .steward_resolution import resolve_steward_binding

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

    from .bridge_sessions import BridgeSessionManager
    from .peer_registry import PeerRegistry
    from .session_sweep import NoticeLatch

logger = logging.getLogger(__name__)


def _pair_spawned_at(row: dict[str, Any]) -> datetime | None:
    from .session_sweep import _parse_iso

    return _parse_iso(row.get("created_at"))


def _pair_identity(row: dict[str, Any]) -> tuple[str, str, str, datetime] | None:
    pair_id = str(row.get("pair_id") or "")
    kind = str(row.get("dispatch_kind") or "")
    runtime = str(row.get("agent_runtime") or "")
    spawned_at = _pair_spawned_at(row)
    if not pair_id or not kind or not runtime or spawned_at is None:
        return None
    return pair_id, kind, runtime, spawned_at


def _matches_cross_vendor_pair(
    partner: dict[str, Any], *, pair_id: str, kind: str, candidate_runtime: str,
) -> bool:
    return (
        str(partner.get("dispatch_kind") or "") == kind
        and str(partner.get("pair_id") or "") == pair_id
        and str(partner.get("agent_runtime") or "") != candidate_runtime
    )


def _has_cross_vendor_pair(candidate: dict[str, Any], rows: list[dict[str, Any]]) -> bool:
    from .session_sweep import DISPATCH_POLICY_PAIR_WINDOW_S

    identity = _pair_identity(candidate)
    if identity is None:
        return False
    pair_id, kind, candidate_runtime, candidate_at = identity
    for partner in rows:
        if partner is candidate:
            continue
        if not _matches_cross_vendor_pair(
            partner, pair_id=pair_id, kind=kind, candidate_runtime=candidate_runtime,
        ):
            continue
        partner_at = _pair_spawned_at(partner)
        if partner_at is not None and abs(
            (partner_at - candidate_at).total_seconds(),
        ) <= DISPATCH_POLICY_PAIR_WINDOW_S:
            return True
    return False


def _notify_steward(
    *, state: StateManagementInterface, peer_registry: PeerRegistry,
    bridge_manager: BridgeSessionManager, row: dict[str, Any], clock: datetime,
) -> bool:
    from .session_sweep import DISPATCH_POLICY_PAIR_WINDOW_S, EVENT_DISPATCH_POLICY_UNPAIRED

    agent_instance_id = str(row.get("agent_instance_id") or "")
    spawner_instance_id = str(row.get("spawned_by_instance_id") or "")
    if not agent_instance_id or not spawner_instance_id:
        return False
    binding = resolve_steward_binding(
        state=state, peer_registry=peer_registry, spawner_instance_id=spawner_instance_id,
    )
    if binding is None:
        logger.warning("dispatch policy pair %s: steward %s is not resolvable", agent_instance_id, spawner_instance_id)
        return False
    prose = (
        f"dispatch_policy_unpaired: {agent_instance_id} (lane_id={row.get('lane_id')!r}, "
        f"dispatch_kind={row.get('dispatch_kind')!r}, pair_id={row.get('pair_id')!r}, "
        f"agent_runtime={row.get('agent_runtime')!r}) has no LIVE producer from the other "
        f"vendor within {DISPATCH_POLICY_PAIR_WINDOW_S:.0f}s of its spawn. Now is "
        f"{clock.isoformat()}. Dispatch the matching producer with the same pair_id; do not "
        "treat one producer as a completed diagnosis or design."
    )
    try:
        bridge_manager.append_event(
            binding.bridge_id, EVENT_DISPATCH_POLICY_UNPAIRED, prose,
            {"flow_id": f"dispatch-policy-unpaired-{agent_instance_id}"},
        )
    except (PeerAmbiguousError, PeerSessionAmbiguousError, PeerUnreachableError):
        logger.warning("dispatch policy pair %s notice append failed", agent_instance_id, exc_info=True)
        return False
    drive_on_delivery(
        state, recipient_agent_instance_id=spawner_instance_id,
        sender_label=EVENT_DISPATCH_POLICY_UNPAIRED,
    )
    return True


def _unpaired_candidate(
    row: dict[str, Any], rows: list[dict[str, Any]], clock: datetime,
) -> str | None:
    from .session_sweep import DISPATCH_POLICY_PAIR_WINDOW_S

    kind = str(row.get("dispatch_kind") or "")
    spawned_at = _pair_spawned_at(row)
    agent_instance_id = str(row.get("agent_instance_id") or "")
    if kind not in {"diagnose", "design"} or not agent_instance_id or spawned_at is None:
        return None
    if (clock - spawned_at).total_seconds() < DISPATCH_POLICY_PAIR_WINDOW_S:
        return None
    return None if _has_cross_vendor_pair(row, rows) else agent_instance_id


def sweep_unpaired_dispatch_policy(
    state: StateManagementInterface,
    *, now: datetime | None = None, peer_registry: PeerRegistry | None = None,
    bridge_manager: BridgeSessionManager | None = None, latch: NoticeLatch | None = None,
) -> int:
    """Notify once per active live diagnose/design row lacking a timely peer."""
    from .session_sweep import LIFECYCLE_LIVE, _latch_or_transient, _managed_sessions_in_state

    if peer_registry is None or bridge_manager is None:
        return 0
    clock = now or datetime.now(UTC)
    rows = _managed_sessions_in_state(state, LIFECYCLE_LIVE)
    gate = _latch_or_transient(latch)
    active: set[str] = set()
    sent = 0
    for row in rows:
        agent_instance_id = _unpaired_candidate(row, rows, clock)
        if agent_instance_id is None:
            continue
        active.add(agent_instance_id)
        if gate.suppressed(agent_instance_id):
            continue
        if _notify_steward(
            state=state, peer_registry=peer_registry, bridge_manager=bridge_manager,
            row=row, clock=clock,
        ):
            gate.record_sent(agent_instance_id)
            sent += 1
    gate.retain_active(active)
    return sent
