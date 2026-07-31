#!/usr/bin/env python3
"""Unit smoke for the GAP-2 agent_message ``important`` backfill (SQL-lockdown).

``backfill_message_important`` is a ONE-SHOT, durable-marker-gated projection of
the JSONB ``metadata.important`` flag onto the new first-class ``important``
column. This smoke verifies, against the REAL ActionResult envelope shapes (the
``state_results`` helpers are the production extraction path — flat
``data.records`` for reads, NESTED ``data.result.updated`` for the update,
``data.found`` for the key-value marker):

  - ONLY rows with truthy ``metadata.important`` AND a still-false column flip;
  - the flip uses ``update_state`` (no raw SQL), one call per row;
  - the durable marker gates a second run to a no-op (``already_done``);
  - dual-shape metadata (dict OR JSON-string) is accepted;
  - set-on-success-only is the only thing that suppresses a re-run (self-healing).

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/message_important_backfill_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))

from ananta.core.domain.types import ActionResult  # noqa: E402
from ananta.interfaces.state_management_interface import (  # noqa: E402
    StateManagementInterface,
)

from agent_messaging_plugin.message_important_backfill import (  # noqa: E402
    STATUS_ALREADY_DONE,
    STATUS_COMPLETED,
    backfill_message_important,
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
    """Faithful fake over the REAL ActionResult envelopes. Implements only the
    verbs the backfill exercises (query_state / update_state / key-value);
    duck-typed where a ``StateManagementInterface`` is expected."""

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.kv: dict[str, object] = {}
        self.updates: list[tuple[str, dict[str, object]]] = []

    def query_state(self, namespace: str, filters: dict[str, object]) -> ActionResult:
        # The backfill passes no row filter ({table, filters:{}}) -> all rows.
        return _envelope({"records": list(self.rows)})

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object],
    ) -> ActionResult:
        inner = cast("dict[str, Any]", query.get("filters", {}))
        target = inner.get("id")
        affected = 0
        for row in self.rows:
            if row["id"] == target:
                row.update(updates)
                affected += 1
        self.updates.append((str(target), dict(updates)))
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


def _row(mid: str, *, important_col: bool, metadata: Any) -> dict[str, Any]:
    return {"id": mid, "important": important_col, "metadata": metadata}


def main() -> int:
    rows = [
        _row("agm-_a", important_col=False, metadata={"important": True}),    # flip
        _row("agm-_b", important_col=False, metadata={"important": False}),   # leave
        _row("agm-_c", important_col=False, metadata={}),                     # leave (absent)
        _row("agm-_d", important_col=True, metadata={"important": True}),     # already projected
        _row("agm-_e", important_col=False, metadata='{"important": true}'),  # JSON-string -> flip
        _row("agm-_f", important_col=False, metadata="{}"),                   # JSON-string empty
    ]
    state = _FakeState(rows)
    repo_state = cast("StateManagementInterface", state)

    result = backfill_message_important(repo_state)
    flipped = sorted(cast("list[str]", result["updated"]))
    _check(result["status"] == STATUS_COMPLETED, "first run -> status=completed")
    _check(
        flipped == ["agm-_a", "agm-_e"],
        f"first run flips ONLY truthy-metadata + false-column rows (dict + "
        f"JSON-string); leaves false/absent/already-projected (flipped={flipped!r})",
    )
    update_ids = sorted(mid for mid, _ in state.updates)
    _check(
        update_ids == ["agm-_a", "agm-_e"],
        f"flip uses update_state, one call per flipped row (ids={update_ids!r})",
    )
    _check(
        all(upd == {"important": True} for _, upd in state.updates),
        "each flip sets important=True (no raw SQL)",
    )
    _check(
        state.rows[0]["important"] is True,
        "dict-metadata row now carries important=True on the column",
    )
    _check(
        state.rows[4]["important"] is True,
        "JSON-string-metadata row now carries important=True on the column",
    )

    state.updates.clear()
    again = backfill_message_important(repo_state)
    _check(
        again["status"] == STATUS_ALREADY_DONE,
        "second run -> already_done (marker-gated)",
    )
    _check(again["updated"] == [], "second run flips nothing")
    _check(state.updates == [], "second run issues zero update_state calls (true no-op)")

    fresh = _FakeState(
        [_row("agm-_z", important_col=False, metadata={"important": True})],
    )
    fresh_result = backfill_message_important(cast("StateManagementInterface", fresh))
    _check(
        fresh_result["status"] == STATUS_COMPLETED,
        "no marker -> runs (self-healing)",
    )
    _check(
        fresh_result["updated"] == ["agm-_z"],
        "set-on-success-only is the only re-run suppressor",
    )

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
