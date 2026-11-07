#!/usr/bin/env python3
"""A role ``peer_inbox`` read whose receipt refill runs out of scan budget returns the
unserved rows it already found, marked incomplete, instead of raising (iss_20b8758d).

The role section refills through acknowledged rows (``_query_visible_role_page``): it keeps
paging until it holds ``limit + 1`` visible rows. The scan is newest-first, so a role whose
newest row is unserved and which sits in front of more acknowledged rows than
``_ROLE_RECEIPT_REFILL_MAX_PAGES`` pages can cover used to hit the budget and RAISE, which the
role fault-domain boundary turned into ``role_section_status: "error"`` with no rows: the role
could not read its own mail. The read now stops at the budget and returns what it holds, with
``scan_truncated`` on the role's coverage and a continuation cursor, so the section never claims
"no older rows" for a scan that stopped early (rul_becb4bed: use the best value you have and say
so, never refuse).

Every check runs the REAL service, persist path and receipt row shape against the real-shape
state fake, through ``peer_inbox_action``.

Each check names the mutation that turns it red (the mutation sweep runs each one):

- raise again at the budget            -> ``_query_visible_role_page`` raises at the top of the loop
                                          (``the read does not raise`` checks)
- discard the rows found at the budget -> ``_query_visible_role_page`` returns ``[]`` at the budget
                                          (``the unserved row is returned`` checks)
- truncation reported False            -> ``_read_role_section`` builds coverage with
                                          ``scan_truncated=False`` (``coverage says the scan was
                                          truncated`` checks)
- section claims exhaustion            -> ``_read_role_section`` passes no truncated scan key to
                                          ``build_role_section`` (``a continuation cursor exists``
                                          checks)
- field dropped from the view          -> ``serialize_peer_inbox_page`` omits ``scan_truncated``
                                          (``the view carries scan_truncated`` check)

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/peer_inbox_role_refill_truncation_smoke.py
"""

from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "tests" / "llm" / "agent_messaging"))

import ananta.llm.agent_messaging.service as service_mod  # noqa: E402
from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_read_receipts import receipt_external_id  # noqa: E402
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    NAMESPACE as ROLE_NAMESPACE,
)
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    TABLE_ROLE_READ_RECEIPT,
)
from peer_inbox_horizon_both_sections_smoke import (  # noqa: E402
    _ROLE,
    _bind_role,
    _role_seconds,
    _service,
)
from peer_inbox_instance_exhaustion_smoke import _INSTANCE_ID, _SESSION_ID, _plugin, _seed  # noqa: E402
from peer_inbox_since_cursor_smoke import _at  # noqa: E402

_passed = 0
_failed: list[str] = []
_LIMIT = 5
_PAGE_ROWS = _LIMIT + 1  # one page of the refill scan is limit + 1 rows
_LOWERED_BUDGET = 3


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


def _write_role(state: RealShapeState, plugin: Any, message_id: str, *, second: int) -> None:
    """A role row written through the REAL persist path at a controlled clock."""
    state.now_iso = lambda: _at(second)
    _service(plugin).persist_role_message(
        recipient_kind="role", recipient_key=_ROLE, message_id=message_id,
        sender_agent_id="claude_code", sender_agent_instance_id="agi-lane",
        sender_session_label="Lane", important=True, content=[],
    )


def _acknowledge(state: RealShapeState, message_id: str) -> None:
    """A read receipt for the row ``message_id`` in the shape ``acknowledge_role_read_page`` writes."""
    row = next(
        r for r in state.rows(ROLE_NAMESPACE, "agent_role_message")
        if r.get("message_id") == message_id
    )
    row_id = str(row["id"])
    result = state.upsert_state(ROLE_NAMESPACE, {
        "table": TABLE_ROLE_READ_RECEIPT,
        "record": {
            "external_id": receipt_external_id(_ROLE, _INSTANCE_ID, row_id),
            "recipient_key": _ROLE, "agent_instance_id": _INSTANCE_ID,
            "role_row_id": row_id, "role_message_id": message_id,
            "role_created_at": str(row.get("created_at", "")),
            "acknowledged_at": (_at(0)),
        },
        "conflict_columns": ["external_id"], "on_conflict": "do_nothing",
    })
    assert result["action_status"] == "completed", result


def _world(
    *, acknowledged: int, unserved_deep: tuple[int, ...] = (),
) -> tuple[RealShapeState, Any]:
    """``acknowledged`` read rows (seconds 0..n-1), then one fresh unserved row ``arm-fresh``.

    ``unserved_deep`` are seconds, among the acknowledged ones, whose row is left UNSERVED
    (ids ``arm-deep<second>``), so a walk of the continuation cursor has older mail to find.
    """
    state = RealShapeState()
    _seed(state, count=2, threads=1)
    plugin = _plugin(state)
    _bind_role(state)
    for second in range(acknowledged):
        message_id = f"arm-deep{second}" if second in unserved_deep else f"arm-ack{second}"
        _write_role(state, plugin, message_id, second=second)
        if second not in unserved_deep:
            _acknowledge(state, message_id)
    _write_role(state, plugin, "arm-fresh", second=acknowledged)
    return state, plugin


def _read(plugin: Any, **params: object) -> dict[str, Any]:
    """A non-observer read, the one that applies read receipts and so refills."""
    result = plugin.peer_inbox_action(
        {"agent_session_id": _SESSION_ID, "observer": False, "limit": _LIMIT, **params}, {},
    )
    assert result["action_status"] == "completed", result
    return cast("dict[str, Any]", result["data"])


def _role_coverage(page: dict[str, Any]) -> dict[str, Any]:
    entries = page.get("role_read_coverage") or []
    return cast("dict[str, Any]", next((e for e in entries if e["role"] == _ROLE), {}))


def _walk(plugin: Any, first: dict[str, Any], *, max_pages: int) -> tuple[list[str], list[dict[str, Any]]]:
    """Follow ``next_role_cursor`` to the end; return every emitted id and every page."""
    pages = [first]
    seen = list(_role_seconds(first))
    page = first
    while page.get("next_role_cursor") and len(pages) < max_pages:
        page = _read(plugin, role_after=page["next_role_cursor"])
        pages.append(page)
        seen.extend(_role_seconds(page))
    return seen, pages


def test_budget_exhausted_returns_the_unserved_row() -> None:
    """The failing-first case: an unserved newest row in front of more acknowledged rows than the budget."""
    acknowledged = _LOWERED_BUDGET * _PAGE_ROWS * 2
    _, plugin = _world(acknowledged=acknowledged)
    original = service_mod._ROLE_RECEIPT_REFILL_MAX_PAGES
    service_mod._ROLE_RECEIPT_REFILL_MAX_PAGES = _LOWERED_BUDGET
    try:
        page = _read(plugin)
    finally:
        service_mod._ROLE_RECEIPT_REFILL_MAX_PAGES = original
    cover = _role_coverage(page)
    _check(page["role_section_status"] == "ok" and page["role_section_error"] is None,
           "the read does not raise: a spent refill budget leaves role_section_status ok, no role_section_error")
    _check(_role_seconds(page) == ["arm-fresh"],
           "the unserved row is returned: the fresh row in front of the acknowledged history is read")
    _check(cover.get("scan_truncated") is True,
           "coverage says the scan was truncated: scan_truncated is true on the role's coverage")
    _check(cover.get("receipt_hidden") == _LOWERED_BUDGET * _PAGE_ROWS - 1,
           "coverage still counts what the scan saw: every scanned acknowledged row is receipt_hidden")
    _check(bool(page["next_role_cursor"]),
           "a continuation cursor exists: the section does not claim there are no older rows")


def test_a_walk_of_the_cursor_finds_older_unserved_mail() -> None:
    """The cursor is a real continuation: following it reaches an unserved row behind the budget."""
    acknowledged = _LOWERED_BUDGET * _PAGE_ROWS * 3
    deep = _LOWERED_BUDGET * _PAGE_ROWS * 2 + 2
    _, plugin = _world(acknowledged=acknowledged, unserved_deep=(deep,))
    original = service_mod._ROLE_RECEIPT_REFILL_MAX_PAGES
    service_mod._ROLE_RECEIPT_REFILL_MAX_PAGES = _LOWERED_BUDGET
    try:
        first = _read(plugin)
        seen, pages = _walk(plugin, first, max_pages=20)
    finally:
        service_mod._ROLE_RECEIPT_REFILL_MAX_PAGES = original
    last = pages[-1]
    _check(seen == ["arm-fresh", f"arm-deep{deep}"],
           "a continuation cursor exists: walking next_role_cursor reaches the older unserved row, once, in order")
    _check(not last.get("next_role_cursor") and _role_coverage(last).get("scan_truncated") is not True,
           "the walk ends on a page that is not truncated and has no cursor: exhaustion is claimed only then")
    _check(all(p["role_section_status"] == "ok" for p in pages),
           "the read does not raise: no page of the walk is a role error")


def test_a_read_within_the_budget_is_unchanged() -> None:
    acknowledged = _PAGE_ROWS  # fewer pages than the lowered budget
    _, plugin = _world(acknowledged=acknowledged)
    original = service_mod._ROLE_RECEIPT_REFILL_MAX_PAGES
    service_mod._ROLE_RECEIPT_REFILL_MAX_PAGES = _LOWERED_BUDGET
    try:
        page = _read(plugin)
    finally:
        service_mod._ROLE_RECEIPT_REFILL_MAX_PAGES = original
    cover = _role_coverage(page)
    _check(_role_seconds(page) == ["arm-fresh"] and page["next_role_cursor"] is None
           and cover.get("scan_truncated") is not True and cover.get("receipt_hidden") == acknowledged,
           "control: a refill that ends inside the budget is exhausted (no cursor) and not marked truncated")
    _check("scan_truncated" not in cover,
           "control: a read that stays within the budget serializes exactly the coverage keys it did before")


def test_the_view_carries_scan_truncated() -> None:
    acknowledged = _LOWERED_BUDGET * _PAGE_ROWS * 2
    _, plugin = _world(acknowledged=acknowledged)
    original = service_mod._ROLE_RECEIPT_REFILL_MAX_PAGES
    service_mod._ROLE_RECEIPT_REFILL_MAX_PAGES = _LOWERED_BUDGET
    try:
        page = _read(plugin)
    finally:
        service_mod._ROLE_RECEIPT_REFILL_MAX_PAGES = original
    _check(_role_coverage(page).get("scan_truncated") is True,
           "the view carries scan_truncated: the serialized peer_inbox page names it on the role entry")


def test_the_real_budget_holds_the_same_shape() -> None:
    """The unmodified constant: more acknowledged rows than 100 pages can cover."""
    budget = service_mod._ROLE_RECEIPT_REFILL_MAX_PAGES
    acknowledged = budget * _PAGE_ROWS + _PAGE_ROWS
    started = time.monotonic()
    _, plugin = _world(acknowledged=acknowledged)
    page = _read(plugin)
    elapsed = time.monotonic() - started
    cover = _role_coverage(page)
    _check(page["role_section_status"] == "ok" and _role_seconds(page) == ["arm-fresh"]
           and cover.get("scan_truncated") is True and bool(page["next_role_cursor"]),
           f"the real constant ({budget} pages): the unserved row is read, truncated, with a cursor "
           f"({acknowledged} acknowledged rows, {elapsed:.1f}s)")


def main() -> int:
    print("=== peer_inbox role refill truncation smoke ===")
    test_budget_exhausted_returns_the_unserved_row()
    test_a_walk_of_the_cursor_finds_older_unserved_mail()
    test_a_read_within_the_budget_is_unchanged()
    test_the_view_carries_scan_truncated()
    test_the_real_budget_holds_the_same_shape()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
