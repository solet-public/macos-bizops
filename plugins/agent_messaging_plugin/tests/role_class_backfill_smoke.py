#!/usr/bin/env python3
"""Unit smoke for the D1 ``role_class`` backfill (fleet session-management
Phase B, §3.1).

Verifies, against REAL ``ActionResult`` envelope shapes (flat
``data.records`` for reads, nested ``data.result.updated`` for the update,
``data.found`` for the key-value marker):

  - pre-Phase-B rows (``role_class`` unset) get stamped with the default;
  - rows that already carry a ``role_class`` are left untouched;
  - the stamp is marker-gated (one-shot, self-healing on an unset marker);
  - a pre-existing >1-named-role holder is DETECTED AND REPORTED, never
    silently normalized — and this check re-runs EVERY call (not marker
    gated), because a rename-aliases-adds-never-releases trap makes new
    violations plausible after the one-shot stamp has already fired;
  - ``sys:*`` slots are excluded from the cardinality count (design §2).

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/role_class_backfill_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

if TYPE_CHECKING:
    from ananta.core.domain.types import ActionResult
    from ananta.interfaces.state_management_interface import StateManagementInterface

from agent_messaging_plugin.role_class_backfill import (  # noqa: E402
    STATUS_ALREADY_DONE,
    STATUS_COMPLETED,
    backfill_role_class,
)

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _envelope(data: dict[str, Any]) -> ActionResult:
    return cast("ActionResult", {"action_status": "completed", "data": data})


class _FakeState:
    """Faithful fake over REAL ActionResult envelopes; implements only the
    verbs the backfill exercises (query_state / update_state / key-value)."""

    def __init__(
        self, role_rows: list[dict[str, Any]], binding_rows: list[dict[str, Any]],
    ) -> None:
        self.role_rows = role_rows
        self.binding_rows = binding_rows
        self.kv: dict[str, object] = {}
        self.role_updates: list[tuple[str, dict[str, object]]] = []

    def query_state(self, namespace: str, query: dict[str, object]) -> ActionResult:
        table = query.get("table")
        if table == "role":
            return _envelope({"records": list(self.role_rows)})
        if table == "role_binding":
            return _envelope({"records": list(self.binding_rows)})
        raise AssertionError(f"unexpected table {table!r}")

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object],
    ) -> ActionResult:
        filters = cast("dict[str, Any]", query.get("filters", {}))
        target = filters.get("id")
        affected = 0
        for row in self.role_rows:
            if row["id"] == target:
                row.update(updates)
                affected += 1
        self.role_updates.append((str(target), dict(updates)))
        return _envelope({"result": {"updated": affected}})

    def get_key_value(
        self, namespace: str, key: str, scope: str = "GLOBAL",
    ) -> ActionResult:
        return _envelope({"found": key in self.kv, "value": self.kv.get(key)})

    def set_key_value(
        self,
        namespace: str,
        key: str,
        value: object,
        scope: str = "GLOBAL",
        ttl: int | None = None,
    ) -> ActionResult:
        self.kv[key] = value
        return _envelope({})


def _role_row(rid: str, name: str, role_class: str | None) -> dict[str, Any]:
    row: dict[str, Any] = {"id": rid, "role": name}
    if role_class is not None:
        row["role_class"] = role_class
    return row


def _binding_row(
    name: str, *, agent_instance_id: str, holder_kind: str = "session",
) -> dict[str, Any]:
    return {
        "role": name,
        "agent_instance_id": agent_instance_id,
        "holder_kind": holder_kind,
        "is_deleted": 0,
    }


def main() -> int:
    role_rows = [
        _role_row("rol-_a", "Coordinator-Dawn", None),           # pre-Phase-B -> stamp
        _role_row("rol-_b", "Git-Controller", "principal"),      # already set -> leave
        _role_row("rol-_c", "Claude-C", None),                   # pre-Phase-B -> stamp
    ]
    binding_rows = [
        _binding_row("Claude-C", agent_instance_id="agi-1"),
        _binding_row("some-lane", agent_instance_id="agi-1"),    # agi-1 holds 2 -> violation
        _binding_row("Coordinator-Dawn", agent_instance_id="agi-2"),
        _binding_row("sys:autonomic", agent_instance_id="agi-2"),  # exempt slot, not a 2nd role
        _binding_row("provider-role", agent_instance_id="", holder_kind="inference_provider"),
    ]
    state = _FakeState(role_rows, binding_rows)
    repo_state = cast("StateManagementInterface", state)

    result = backfill_role_class(repo_state)
    stamped = sorted(cast("list[str]", result["stamped"]))
    _check(result["status"] == STATUS_COMPLETED, "first run -> status=completed")
    _check(
        stamped == ["Claude-C", "Coordinator-Dawn"],
        f"stamps ONLY rows missing role_class, leaves already-set rows "
        f"(stamped={stamped!r})",
    )
    _check(
        all(row.get("role_class") == "project" for row in role_rows if row["id"] != "rol-_b"),
        "stamped rows now carry role_class='project'",
    )
    _check(
        role_rows[1]["role_class"] == "principal",
        "already-set row's role_class is untouched",
    )

    violations = cast("list[dict[str, Any]]", result["cardinality_violations"])
    _check(len(violations) == 1, f"exactly one cardinality violation reported (got {violations!r})")
    if violations:
        _check(
            violations[0] == {"agent_instance_id": "agi-1", "roles": ["Claude-C", "some-lane"]},
            f"violation names the offending instance + BOTH roles, sys:* excluded "
            f"(got {violations[0]!r})",
        )

    state.role_updates.clear()
    again = backfill_role_class(repo_state)
    _check(again["status"] == STATUS_ALREADY_DONE, "second run -> already_done (marker-gated)")
    _check(again["stamped"] == [], "second run stamps nothing")
    _check(state.role_updates == [], "second run issues zero update_state calls (true no-op)")
    _check(
        len(cast("list[Any]", again["cardinality_violations"])) == 1,
        "cardinality detection re-runs even after the stamp is marker-done "
        "(NOT itself marker-gated — new violations can appear later)",
    )

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
