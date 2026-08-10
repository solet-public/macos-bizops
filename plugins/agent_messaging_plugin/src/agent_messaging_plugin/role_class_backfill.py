"""Fleet session-management Phase B, D1 (§3.1) one-shot backfill:
project the ``role_class`` default onto pre-Phase-B ``role`` rows, and
DETECT (never silently fix) any pre-existing cardinality violation the new
claim-gate invariant (§2: at most one named role per session) would newly
reject.

The additive ``role_class`` column lands with ``default='project'``
(schema.py), so the standardizer already writes that default onto every row
touched after the column exists. This backfill exists for the two things a
column default cannot do:

1. Explicitly stamp ``role_class`` on rows written BEFORE the column existed
   (belt-and-suspenders — the same ``message_important_backfill.py``
   precedent: an additive column's default is not retroactive on some
   backends/paths).
2. Walk the LIVE ``role_binding`` table and report every ``agent_instance_id``
   that currently holds more than one named role (excluding ``sys:*`` slots,
   which are exempt from the cardinality count — design §2). Dawn's ruling
   (arm-87976ca719, 2026-08-03) is explicit: the reconcile DETECTS AND
   REPORTS loudly for a pre-land operator cleanup pass; it never silently
   normalizes a multi-role holder down to one, because picking WHICH role to
   keep is an operator/stewardship decision, not a migration's to make. The
   AMEND-4b session-key row is lazy-created at each session's next claim —
   this backfill never manufactures one.

ONE-SHOT, durable-marker-gated (message_important_backfill.py precedent):
runs to completion exactly once, marker set ONLY after a successful full
pass (self-healing — a fault before the marker leaves it unset, next boot
re-runs).
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ananta.llm.agent_messaging.role_binding import (
    AGENT_ROLE_BINDING_NAMESPACE,
    COL_AGENT_INSTANCE_ID,
    COL_HOLDER_KIND,
    COL_ROLE,
    COL_ROLE_CLASS,
    HOLDER_KIND_SESSION,
    ROLE_CLASS_DEFAULT,
    TABLE_ROLE,
    TABLE_ROLE_BINDING,
    is_system_role,
)
from ananta.llm.agent_messaging.state_results import (
    require_completed,
    require_records,
    require_updated,
)

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

logger = logging.getLogger(__name__)

_COL_ID = "id"
_COL_IS_DELETED = "is_deleted"

# Durable one-shot marker (set ONLY after a successful full pass -> self-healing).
_BACKFILL_MARKER_KEY = "role_class_backfill_v1_complete"
_BACKFILL_MARKER_VALUE = "true"

STATUS_COMPLETED = "completed"
STATUS_ALREADY_DONE = "already_done"


def backfill_role_class(state: StateManagementInterface) -> dict[str, object]:
    """ONE-SHOT: stamp ``role_class`` default on pre-Phase-B rows; report
    (never fix) any pre-existing >1-named-role holder.

    Returns ``{"status", "stamped": [...role names...],
    "cardinality_violations": [{"agent_instance_id", "roles": [...]}]}``. A
    non-empty ``cardinality_violations`` is a LOUD signal for the pre-land
    operator cleanup pass named in the D1 ruling — it does not block the
    backfill itself (the ``role_class`` stamp and the cardinality report are
    independent concerns; a rename-aliases-adds-never-releases trap makes
    pre-existing multi-role holders plausible and worth surfacing every run
    until cleaned up, not just once — so THIS check is NOT marker-gated,
    only the stamp is).
    """
    stamped = _stamp_missing_role_class(state)
    violations = _detect_cardinality_violations(state)
    if violations:
        logger.warning(
            "role_class_backfill: %d agent_instance_id(s) hold more than one "
            "named role — pre-existing violation of the new §2 cardinality "
            "invariant, NOT auto-fixed (operator cleanup owed): %r",
            len(violations),
            violations,
        )
    if _backfill_already_complete(state):
        return {
            "status": STATUS_ALREADY_DONE,
            "stamped": [],
            "cardinality_violations": violations,
        }
    _mark_backfill_complete(state)
    if stamped:
        logger.info(
            "role_class_backfill: stamped default role_class=%r on %d "
            "pre-Phase-B role row(s)",
            ROLE_CLASS_DEFAULT,
            len(stamped),
        )
    return {
        "status": STATUS_COMPLETED,
        "stamped": stamped,
        "cardinality_violations": violations,
    }


def _stamp_missing_role_class(state: StateManagementInterface) -> list[str]:
    """Explicitly write ``role_class=ROLE_CLASS_DEFAULT`` on any ``role`` row
    where it is still unset — belt-and-suspenders alongside the column
    default. Idempotent: only rows missing the value are touched."""
    if _backfill_already_complete(state):
        return []
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_ROLE, "filters": {}},
    )
    stamped: list[str] = []
    for row in require_records(result):
        if row.get(COL_ROLE_CLASS):
            continue
        role_name = str(row.get(COL_ROLE) or "")
        require_updated(
            state.update_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {"table": TABLE_ROLE, "filters": {_COL_ID: row[_COL_ID]}},
                {COL_ROLE_CLASS: ROLE_CLASS_DEFAULT},
            ),
        )
        stamped.append(role_name)
    return stamped


def _detect_cardinality_violations(
    state: StateManagementInterface,
) -> list[dict[str, object]]:
    """Group live SESSION-holder ``role_binding`` rows by ``agent_instance_id``;
    report every instance holding more than one NAMED role. ``sys:*`` slots are
    slot machinery, not named roles, and are exempt from the count (design §2).
    """
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE_BINDING,
            "filters": {_COL_IS_DELETED: 0, COL_HOLDER_KIND: HOLDER_KIND_SESSION},
        },
    )
    roles_by_instance: dict[str, list[str]] = {}
    for row in require_records(result):
        role_name = str(row.get(COL_ROLE) or "")
        if not role_name or is_system_role(role_name):
            continue
        instance_id = str(row.get(COL_AGENT_INSTANCE_ID) or "")
        if not instance_id:
            continue
        roles_by_instance.setdefault(instance_id, []).append(role_name)
    return [
        {"agent_instance_id": instance_id, "roles": sorted(roles)}
        for instance_id, roles in sorted(roles_by_instance.items())
        if len(roles) > 1
    ]


def _backfill_already_complete(state: StateManagementInterface) -> bool:
    data = require_completed(
        state.get_key_value(AGENT_ROLE_BINDING_NAMESPACE, _BACKFILL_MARKER_KEY),
        "get role_class backfill marker",
    )
    return bool(data.get("found"))


def _mark_backfill_complete(state: StateManagementInterface) -> None:
    require_completed(
        state.set_key_value(
            AGENT_ROLE_BINDING_NAMESPACE, _BACKFILL_MARKER_KEY, _BACKFILL_MARKER_VALUE,
        ),
        "set role_class backfill marker",
    )


__all__ = ["STATUS_ALREADY_DONE", "STATUS_COMPLETED", "backfill_role_class"]
