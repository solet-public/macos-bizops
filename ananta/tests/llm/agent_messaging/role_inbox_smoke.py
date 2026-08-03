#!/usr/bin/env python3
"""Unit smoke for the v10 role-inbox section (Control #1a) — no pytest, no DB.

Exercises the risky pure logic of sub-seam 3 against a faithful in-memory fake
state that REUSES the real ``ordered_query`` ordering/cursor semantics, so the
merge + cursor behave exactly as they will over postgres/rds:

  * opaque scope-bound cursor — round-trip, held-role-set change + visibility
    flip reset (no silent skip), malformed token fails closed;
  * global ``(created_at, id)`` k-way merge across MULTIPLE roles, including an
    equal-``created_at`` pair that STRADDLES the page boundary (the v9-B3 case)
    — every row exactly once across pages, correct global order, no skip/dup;
  * ``role_after`` page-2 reachability (the v9-B2 input-cursor threading);
  * delivered-IMPORTANT catch-up (``include_important`` omits important+
    delivered) vs explicit silent-only;
  * threadless ``PeerInboxEntry`` projection (targeted-reply fields present,
    ``message.id == message_id``, ``cursor == 0`` sentinel, content rebuilt);
  * empty-holder → ``((), None)``;
  * pull-surface boundary (design workbench/2026-08-02_pull_surface_boundary_design_claude_d.md):
    the floor excludes rows at/below a directly-seeded ``role_covered_mark``
    (nothing attests live in this suite, so the mark is seeded straight into
    the fake state — see design §5b.vi) and reports
    ``role_floor_applied``/a history cursor; echoing that history cursor back
    reveals the pre-mark backlog; the byte ceiling truncates a page of
    oversized entries SHORT of ``limit`` and still mints a real continuation
    cursor (proven by asserting the returned page is shorter than ``limit``,
    not just that a cursor was minted — a small-fixture byte-ceiling assertion
    would be vacuously green because the row cap binds first).

Run:
    .venv/bin/python3 ananta/tests/llm/agent_messaging/role_inbox_smoke.py
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))

from ananta.interfaces.state_management_interface import (  # noqa: E402
    StateManagementInterface,
)
from ananta.llm.agent_messaging.models import (  # noqa: E402
    PeerInboxRequest,
    RoleSectionStatus,
)
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    HOLDER_KIND_SESSION,
    TABLE_ROLE_BINDING,
    role_binding_external_id,
)
from ananta.llm.agent_messaging.role_cursor import (  # noqa: E402
    RoleCursorOutcome,
    RoleCursorRejectedError,
    RoleCursorScope,
    decode_role_cursor,
    encode_role_cursor,
)
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    NAMESPACE as ROLE_NAMESPACE,
)
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    TABLE_AGENT_ROLE_MESSAGE,
    TABLE_ROLE_COVERED_MARK,
)
from ananta.llm.agent_messaging.service import (  # noqa: E402
    AgentMessagingConfig,
    AgentMessagingService,
    AgentRequestInvalidError,
)
from ananta.services.state_service.ordered_query import (  # noqa: E402
    apply_ordered_query_in_memory,
    parse_ordered_query,
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


# ---------------------------------------------------------------------------
# Faithful in-memory fake state (reuses the real ordered_query semantics)
# ---------------------------------------------------------------------------


class _FakeState:
    """In-memory StateManagementInterface stand-in for the 4 verbs the role
    layer uses: ``upsert_state`` / ``query_state`` / ``query_ordered`` /
    ``update_state``. ``query_ordered`` delegates to the real
    ``apply_ordered_query_in_memory`` so ordering + cursor match production."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, list[dict[str, object]]]] = {}

    def _table(self, namespace: str, table: str) -> list[dict[str, object]]:
        return self._data.setdefault(namespace, {}).setdefault(table, [])

    def upsert_state(self, namespace: str, data: dict[str, object]) -> dict[str, Any]:
        table = cast(str, data["table"])
        record = dict(cast("dict[str, object]", data["record"]))
        conflict = cast("list[str]", data.get("conflict_columns", []))
        rows = self._table(namespace, table)
        for existing in rows:
            if all(existing.get(col) == record.get(col) for col in conflict):
                existing.update(record)
                return {"action_status": "completed", "data": {"result": {"upserted": 1}}}
        record.setdefault("id", f"{table}-{len(rows) + 1:03d}")
        rows.append(record)
        return {"action_status": "completed", "data": {"result": {"upserted": 1}}}

    def query_state(self, namespace: str, data: dict[str, object]) -> dict[str, Any]:
        table = cast(str, data["table"])
        filters = cast("dict[str, object]", data.get("filters", {}))
        rows = [
            dict(row)
            for row in self._table(namespace, table)
            if all(row.get(key) == value for key, value in filters.items())
        ]
        return {"action_status": "completed", "data": {"records": rows}}

    def query_ordered(self, namespace: str, data: dict[str, object]) -> dict[str, Any]:
        spec = parse_ordered_query(data)
        rows = self._table(namespace, cast(str, data["table"]))
        out = apply_ordered_query_in_memory(rows, spec)
        return {
            "action_status": "completed",
            "data": {"records": [dict(row) for row in out]},
        }

    def update_state(
        self, namespace: str, query: dict[str, object], updates: dict[str, object],
    ) -> dict[str, Any]:
        table = cast(str, query["table"])
        filters = cast("dict[str, object]", query.get("filters", {}))
        updated = 0
        for row in self._table(namespace, table):
            if all(row.get(key) == value for key, value in filters.items()):
                row.update(updates)
                updated += 1
        return {"action_status": "completed", "data": {"result": {"updated": updated}}}


def _make_service(state: _FakeState) -> AgentMessagingService:
    """Construct the service with stubs — only ``_state`` is exercised here."""
    return AgentMessagingService(
        repository=cast(Any, None),
        state_service=cast(StateManagementInterface, state),
        backend_router=cast(Any, None),
        flow_manager=cast(Any, None),
        action_factory=cast(Any, None),
        compilation_context_builder=cast(Any, None),
        bridge_delivery=cast(Any, None),
        config=AgentMessagingConfig(),
    )


_INSTANCE = "agi-holder"


def _seed_binding(state: _FakeState, role: str, instance: str = _INSTANCE) -> None:
    # §9 CUTOVER: the role-inbox held-roles enumeration (`_enumerate_held_roles`)
    # reads the v4 `role_binding` table (holder_kind='session'), NOT the legacy
    # `agent_role_binding`. Seed the v4 table so the enumeration resolves the role.
    state.upsert_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE_BINDING,
            "record": {
                "external_id": role_binding_external_id(role),
                "role": role,
                "holder_kind": HOLDER_KIND_SESSION,
                "agent_id": "claude_code",
                "agent_instance_id": instance,
                "agent_session_id": f"sess-{instance}",
                "session_label": role,
                "is_deleted": 0,
            },
            "conflict_columns": ["external_id"],
        },
    )


def _seed_role_msg(
    state: _FakeState,
    *,
    row_id: str,
    role: str,
    created_at: str,
    important: bool = False,
    delivered: bool = False,
    text: str = "hello",
) -> None:
    msg_id = f"msg-{row_id}"
    state.upsert_state(
        ROLE_NAMESPACE,
        {
            "table": TABLE_AGENT_ROLE_MESSAGE,
            "record": {
                "id": row_id,
                "external_id": f"role:{role}:{msg_id}",
                "recipient_kind": "role",
                "recipient_key": role,
                "message_id": msg_id,
                "sender_agent_id": "claude_code",
                "sender_agent_instance_id": "agi-sender",
                "sender_session_label": "Coordinator",
                "thread_id": f"role:{role}",
                "important": important,
                # REL-05 owed-delivery model: a row is owed while consumed=false
                # AND escalated=false; the re-emit window/cap read last_emitted_at
                # + emit_count. A delivered row IS consumed → excluded from drain.
                "consumed": delivered,
                "escalated": False,
                "emit_count": 0,
                "last_emitted_at": None,
                "content": [{"type": "text", "text": text}],
                "created_at": created_at,
                "is_deleted": 0,
            },
            "conflict_columns": ["external_id"],
        },
    )


def _seed_mark(
    state: _FakeState, *, role: str, covered_created_at: str, covered_id: str,
) -> None:
    """Seed a ``role_covered_mark`` row directly — nothing attests it live in
    this suite (design §5b.vi: the mark is inert until the verb is called;
    ``peer_mark_role_covered``'s own identity fence is smoke-tested
    separately at the plugin layer in
    ``plugins/agent_messaging_plugin/tests/peer_mark_role_covered_smoke.py``,
    where the write path is actually exercised)."""
    state.upsert_state(
        ROLE_NAMESPACE,
        {
            "table": TABLE_ROLE_COVERED_MARK,
            "record": {
                "external_id": f"role:{role}",
                "recipient_key": role,
                "covered_created_at": covered_created_at,
                "covered_id": covered_id,
                "covered_message_id": f"msg-{covered_id}",
                "attested_by_agent_instance_id": "agi-attester",
                "attested_by_agent_session_id": "sess-attester",
                "attested_by_session_label": "Claude-D",
                "attested_at": covered_created_at,
                "is_deleted": 0,
            },
            "conflict_columns": ["external_id"],
        },
    )


# ---------------------------------------------------------------------------
# 1. Cursor codec
# ---------------------------------------------------------------------------


def test_cursor_roundtrip_and_scope() -> None:
    scope = RoleCursorScope(include_important=False, held_roles=("Architect", "Coordinator"))
    token = encode_role_cursor(scope, created_at_iso="2026-06-19T09:00:00", row_id="arm-007")
    decoded = decode_role_cursor(token, scope)
    _check(
        decoded.outcome is RoleCursorOutcome.VALID
        and decoded.row_id == "arm-007"
        and decoded.created_at is not None
        and decoded.created_at.isoformat() == "2026-06-19T09:00:00",
        "cursor round-trips (VALID) with naive-UTC created_at + id",
    )

    # Role-set membership is order-insensitive (hash sorts) — same scope.
    reordered = RoleCursorScope(include_important=False, held_roles=("Coordinator", "Architect"))
    _check(
        decode_role_cursor(token, reordered).outcome is RoleCursorOutcome.VALID,
        "cursor scope is order-insensitive (sorted role set)",
    )

    # Held-role-set change → SCOPE_CHANGED (reset, no silent skip).
    grew = RoleCursorScope(include_important=False, held_roles=("Architect", "Coordinator", "Dusk"))
    _check(
        decode_role_cursor(token, grew).outcome is RoleCursorOutcome.SCOPE_CHANGED,
        "held-role-set change → SCOPE_CHANGED (cursor reset)",
    )

    # include_important flip → SCOPE_CHANGED.
    flipped = RoleCursorScope(include_important=True, held_roles=("Architect", "Coordinator"))
    _check(
        decode_role_cursor(token, flipped).outcome is RoleCursorOutcome.SCOPE_CHANGED,
        "include_important flip → SCOPE_CHANGED (cursor reset)",
    )


def test_cursor_fail_closed() -> None:
    scope = RoleCursorScope(include_important=False, held_roles=("Architect",))
    for bad, label in (
        ("not!base64!", "non-base64 token"),
        ("", "empty token"),
        (encode_role_cursor(scope, created_at_iso="nope", row_id="x"), "non-ISO created_at"),
    ):
        try:
            decode_role_cursor(bad, scope)
            _check(False, f"malformed cursor fails closed: {label}")
        except RoleCursorRejectedError:
            _check(True, f"malformed cursor fails closed: {label}")


# ---------------------------------------------------------------------------
# 2. Multi-role global k-way merge + page-boundary straddle (v9-B3 / B2)
# ---------------------------------------------------------------------------


def _seed_two_role_grid(state: _FakeState) -> None:
    _seed_binding(state, "R1")
    _seed_binding(state, "R2")
    # Equal created_at ACROSS roles at 09:00 (arm-001/002) and 09:02 (arm-004/005).
    _seed_role_msg(state, row_id="arm-001", role="R1", created_at="2026-06-19T09:00:00")
    _seed_role_msg(state, row_id="arm-002", role="R2", created_at="2026-06-19T09:00:00")
    _seed_role_msg(state, row_id="arm-003", role="R1", created_at="2026-06-19T09:01:00")
    _seed_role_msg(state, row_id="arm-004", role="R2", created_at="2026-06-19T09:02:00")
    _seed_role_msg(state, row_id="arm-005", role="R1", created_at="2026-06-19T09:02:00")
    _seed_role_msg(state, row_id="arm-006", role="R2", created_at="2026-06-19T09:03:00")


def _page_ids(entries: tuple[Any, ...]) -> list[str]:
    # The projected AgentMessageRow.id is the envelope message_id ("msg-arm-00N").
    return [e.message.id for e in entries]


def test_multirole_kway_merge_pagination() -> None:
    state = _FakeState()
    _seed_two_role_grid(state)
    service = _make_service(state)

    collected: list[str] = []
    cursor: str | None = None
    pages = 0
    while True:
        entries, cursor, _, _ = service.list_silent_for_roles(
            agent_instance_id=_INSTANCE,
            include_important=False,
            limit=2,
            role_after=cursor,
        )
        pages += 1
        collected.extend(_page_ids(entries))
        if cursor is None:
            break
        if pages > 10:  # loop guard
            break

    # Global (created_at,id)-desc order, every row exactly once, no skip/dup —
    # the 09:02 pair (arm-005,arm-004) STRADDLES the limit=2 page boundary.
    expected = ["msg-arm-006", "msg-arm-005", "msg-arm-004",
                "msg-arm-003", "msg-arm-002", "msg-arm-001"]
    _check(collected == expected, "multi-role k-way merge: global desc order, no skip/dup")
    _check(
        len(collected) == len(set(collected)),
        "multi-role k-way merge: every row exactly once across pages",
    )
    _check(pages >= 3, "multi-role k-way merge: role_after page-2+ reachable (>=3 pages)")


def test_held_role_set_change_resets() -> None:
    state = _FakeState()
    _seed_two_role_grid(state)
    service = _make_service(state)

    _, cursor, _, _ = service.list_silent_for_roles(
        agent_instance_id=_INSTANCE, include_important=False, limit=2, role_after=None,
    )
    _check(cursor is not None, "page 1 returns a next_role_cursor")

    # Holder ACQUIRES a third role mid-pagination, then replays the stale cursor.
    _seed_binding(state, "R3")
    _seed_role_msg(state, row_id="arm-009", role="R3", created_at="2026-06-19T09:05:00")
    entries, _, _, _ = service.list_silent_for_roles(
        agent_instance_id=_INSTANCE, include_important=False, limit=2, role_after=cursor,
    )
    # Scope changed → reset to page 1 of the NEW scope → newest row (R3 09:05)
    # surfaces, NOT silently skipped past the stale cursor.
    _check(
        _page_ids(entries)[0] == "msg-arm-009",
        "held-role-set change resets to page 1 (new role's row not skipped)",
    )


def test_malformed_role_after_rejected() -> None:
    state = _FakeState()
    _seed_two_role_grid(state)
    service = _make_service(state)
    try:
        service.list_silent_for_roles(
            agent_instance_id=_INSTANCE, include_important=False, limit=2,
            role_after="garbage!!",
        )
        _check(False, "malformed role_after → AgentRequestInvalidError (fail closed)")
    except AgentRequestInvalidError:
        _check(True, "malformed role_after → AgentRequestInvalidError (fail closed)")


# ---------------------------------------------------------------------------
# 3. Delivered-IMPORTANT catch-up + explicit silent-only
# ---------------------------------------------------------------------------


def test_delivered_important_catchup() -> None:
    state = _FakeState()
    _seed_binding(state, "R1")
    _seed_role_msg(state, row_id="arm-100", role="R1", created_at="2026-06-19T08:00:00")  # silent
    _seed_role_msg(
        state, row_id="arm-101", role="R1", created_at="2026-06-19T09:00:00",
        important=True, delivered=True,  # delivered IMPORTANT
    )
    service = _make_service(state)

    silent, _, _, _ = service.list_silent_for_roles(
        agent_instance_id=_INSTANCE, include_important=False, limit=10, role_after=None,
    )
    _check(
        _page_ids(silent) == ["msg-arm-100"],
        "include_important=False: delivered IMPORTANT excluded, silent only",
    )

    catchup, _, _, _ = service.list_silent_for_roles(
        agent_instance_id=_INSTANCE, include_important=True, limit=10, role_after=None,
    )
    _check(
        set(_page_ids(catchup)) == {"msg-arm-100", "msg-arm-101"},
        "include_important=True: delivered IMPORTANT resurfaces (catch-up)",
    )


# ---------------------------------------------------------------------------
# 4. Threadless projection (targeted-reply fields) + empty holder
# ---------------------------------------------------------------------------


def test_projection_fields() -> None:
    state = _FakeState()
    _seed_binding(state, "Architect")
    _seed_role_msg(
        state, row_id="arm-200", role="Architect", created_at="2026-06-19T09:00:00",
        text="ping",
    )
    service = _make_service(state)
    entries, _, _, _ = service.list_silent_for_roles(
        agent_instance_id=_INSTANCE, include_important=False, limit=10, role_after=None,
    )
    _check(len(entries) == 1, "projection: one entry returned")
    entry = entries[0]
    _check(
        entry.sender_agent_instance_id == "agi-sender"
        and entry.sender_agent_id == "claude_code"
        and entry.thread_id == "role:Architect",
        "projection: targeted-reply fields present (sender instance/id + thread handle)",
    )
    _check(
        entry.message.id == "msg-arm-200" and entry.message.cursor == 0,
        "projection: message.id == message_id, cursor == 0 sentinel",
    )
    _check(
        len(entry.message.content) == 1
        and entry.message.content[0].text == "ping",
        "projection: content rebuilt to typed parts",
    )


def test_empty_holder() -> None:
    state = _FakeState()
    service = _make_service(state)
    entries, cursor, floor_applied, history = service.list_silent_for_roles(
        agent_instance_id="agi-nobody", include_important=False, limit=10, role_after=None,
    )
    _check(
        entries == () and cursor is None and floor_applied is False and history is None,
        "holder with no roles → ((), None, False, None)",
    )


# ---------------------------------------------------------------------------
# 5. Repair-drain page (Control #5): oldest-first across held roles
# ---------------------------------------------------------------------------


def test_drain_page_oldest_first() -> None:
    state = _FakeState()
    _seed_binding(state, "R1")
    _seed_binding(state, "R2")
    # Undelivered IMPORTANT (drain candidates) interleaved across roles, plus a
    # delivered IMPORTANT and a silent row that must BOTH be excluded.
    _seed_role_msg(state, row_id="d1", role="R1", created_at="2026-06-19T08:00:00",
                   important=True, delivered=False)
    _seed_role_msg(state, row_id="d2", role="R2", created_at="2026-06-19T08:01:00",
                   important=True, delivered=False)
    _seed_role_msg(state, row_id="d3", role="R1", created_at="2026-06-19T08:02:00",
                   important=True, delivered=True)   # delivered → excluded
    _seed_role_msg(state, row_id="d4", role="R2", created_at="2026-06-19T08:03:00",
                   important=False, delivered=False)  # silent → excluded
    _seed_role_msg(state, row_id="d5", role="R1", created_at="2026-06-19T08:04:00",
                   important=True, delivered=False)
    service = _make_service(state)

    page = service.list_undelivered_for_instance(agent_instance_id=_INSTANCE, limit=10)
    ids = [str(r["message_id"]) for r in page]
    _check(
        ids == ["msg-d1", "msg-d2", "msg-d5"],
        "drain page: oldest-first across roles, excludes delivered + silent",
    )

    capped = service.list_undelivered_for_instance(agent_instance_id=_INSTANCE, limit=2)
    _check(
        [str(r["message_id"]) for r in capped] == ["msg-d1", "msg-d2"],
        "drain page: limit caps the oldest-first page",
    )

    empty = service.list_undelivered_for_instance(agent_instance_id="agi-nobody", limit=10)
    _check(empty == [], "drain page: holder with no roles → []")


# ---------------------------------------------------------------------------
# 6. Pull-surface boundary — the floor + the byte ceiling (design
#    workbench/2026-08-02_pull_surface_boundary_design_claude_d.md). The
#    attestation verb's identity fence / monotonic no-op / message_id lookup
#    are smoke-tested separately at the plugin layer, where the WRITE path
#    is actually exercised:
#    plugins/agent_messaging_plugin/tests/peer_mark_role_covered_smoke.py
# ---------------------------------------------------------------------------


def test_floor_excludes_covered_rows_and_reports_applied() -> None:
    state = _FakeState()
    _seed_binding(state, "R1")
    _seed_role_msg(state, row_id="f01", role="R1", created_at="2026-06-19T08:00:00")
    _seed_role_msg(state, row_id="f02", role="R1", created_at="2026-06-19T08:01:00")
    # Mark: covered THROUGH f02 — f01/f02 already seen, f03/f04 are new.
    _seed_mark(state, role="R1", covered_created_at="2026-06-19T08:01:00", covered_id="f02")
    _seed_role_msg(state, row_id="f03", role="R1", created_at="2026-06-19T08:02:00")
    _seed_role_msg(state, row_id="f04", role="R1", created_at="2026-06-19T08:03:00")
    service = _make_service(state)

    entries, cursor, floor_applied, history = service.list_silent_for_roles(
        agent_instance_id=_INSTANCE, include_important=False, limit=10, role_after=None,
    )
    _check(
        _page_ids(entries) == ["msg-f04", "msg-f03"],
        "floor: default drain returns only rows newer than the mark",
    )
    _check(cursor is None, "floor-stop: next_role_cursor is null (drain genuinely complete)")
    _check(floor_applied is True, "floor-stop: role_floor_applied reports true")
    _check(history is not None, "floor-stop: a history/resume cursor is minted")


def test_history_cursor_reveals_pre_mark_backlog() -> None:
    state = _FakeState()
    _seed_binding(state, "R1")
    _seed_role_msg(state, row_id="f01", role="R1", created_at="2026-06-19T08:00:00")
    _seed_role_msg(state, row_id="f02", role="R1", created_at="2026-06-19T08:01:00")
    _seed_mark(state, role="R1", covered_created_at="2026-06-19T08:01:00", covered_id="f02")
    _seed_role_msg(state, row_id="f03", role="R1", created_at="2026-06-19T08:02:00")
    service = _make_service(state)

    _, _, _, history = service.list_silent_for_roles(
        agent_instance_id=_INSTANCE, include_important=False, limit=10, role_after=None,
    )
    assert history is not None
    entries, _, floor_applied, _ = service.list_silent_for_roles(
        agent_instance_id=_INSTANCE, include_important=False, limit=10, role_after=history,
    )
    # Design §5b.vii disclosed edge: strictly OLDER than the mark, not the
    # mark's own row (f02) — that row was already delivered to whichever
    # session set the mark, since attestation only ever follows processing.
    _check(
        _page_ids(entries) == ["msg-f01"],
        "history cursor: echoing it back reveals rows strictly older than the mark",
    )
    _check(floor_applied is False, "history read: the floor did not apply (deliberate deep read)")


def test_history_token_wrong_scope_resets_and_refloors() -> None:
    """A history token replayed after the held-role set changed must NOT
    silently disable the floor forever — SCOPE_CHANGED resets to page 1 with
    the floor RE-ENABLED (role_cursor.decode_role_cursor's own contract)."""
    state = _FakeState()
    _seed_binding(state, "R1")
    _seed_role_msg(state, row_id="f01", role="R1", created_at="2026-06-19T08:00:00")
    _seed_role_msg(state, row_id="f02", role="R1", created_at="2026-06-19T08:01:00")
    _seed_mark(state, role="R1", covered_created_at="2026-06-19T08:01:00", covered_id="f02")
    _seed_role_msg(state, row_id="f03", role="R1", created_at="2026-06-19T08:02:00")
    service = _make_service(state)
    _, _, _, history = service.list_silent_for_roles(
        agent_instance_id=_INSTANCE, include_important=False, limit=10, role_after=None,
    )
    assert history is not None

    # Holder acquires a second role — held-role-set hash no longer matches
    # the token's issuing scope.
    _seed_binding(state, "R2")
    entries, _, floor_applied, _ = service.list_silent_for_roles(
        agent_instance_id=_INSTANCE, include_important=False, limit=10, role_after=history,
    )
    _check(
        _page_ids(entries) == ["msg-f03"],
        "history token + scope change: resets to page-1 of the NEW scope, floor re-applied",
    )
    _check(
        floor_applied is True,
        "scope-changed reset still floors R1's f01/f02 — the history token's "
        "floor-skip did NOT survive the reset (R1's mark still excludes them)",
    )


def test_floor_is_noop_without_a_mark() -> None:
    state = _FakeState()
    _seed_binding(state, "R1")
    _seed_role_msg(state, row_id="f01", role="R1", created_at="2026-06-19T08:00:00")
    service = _make_service(state)
    entries, cursor, floor_applied, history = service.list_silent_for_roles(
        agent_instance_id=_INSTANCE, include_important=False, limit=10, role_after=None,
    )
    _check(
        _page_ids(entries) == ["msg-f01"] and cursor is None
        and floor_applied is False and history is None,
        "no mark yet: today's behavior unchanged byte-for-byte (§12.3 fail-direction)",
    )


def test_byte_ceiling_truncates_short_of_the_row_limit() -> None:
    """A fixture of small entries would let the row cap bind first and never
    exercise the byte path — a vacuous-green risk flagged before this was
    written. Pad content so entries blow the 200 KB ceiling well before
    ``limit`` rows, and assert the returned page is SHORTER than ``limit``
    (the actual proof the byte path fired), not just that a cursor minted.
    """
    state = _FakeState()
    _seed_binding(state, "R1")
    big_text = "x" * 60_000  # ~60KB/entry serialized; 4 entries ≈ 240KB > 200KB ceiling
    for i in range(6):
        _seed_role_msg(
            state, row_id=f"b{i:02d}", role="R1",
            created_at=f"2026-06-19T08:{i:02d}:00", text=big_text,
        )
    service = _make_service(state)
    entries, cursor, floor_applied, history = service.list_silent_for_roles(
        agent_instance_id=_INSTANCE, include_important=False, limit=10, role_after=None,
    )
    _check(
        0 < len(entries) < 10,
        "byte ceiling: page truncated SHORT of the row limit (proves byte-stop fired)",
    )
    _check(cursor is not None, "byte-stop: a real continuation cursor is minted, never null")
    _check(floor_applied is False, "byte-stop: no mark involved, floor did not apply")
    _check(history is None, "byte-stop: no history cursor (that's the floor-stop branch only)")

    remaining, _, _, _ = service.list_silent_for_roles(
        agent_instance_id=_INSTANCE, include_important=False, limit=10, role_after=cursor,
    )
    _check(
        len(entries) + len(remaining) == 6,
        "byte-stop: continuing the walk reaches every row across pages, none dropped",
    )


def test_byte_ceiling_admits_at_least_one_oversized_entry() -> None:
    state = _FakeState()
    _seed_binding(state, "R1")
    huge_text = "x" * 250_000  # a single entry alone exceeds the 200KB ceiling
    _seed_role_msg(state, row_id="next", role="R1", created_at="2026-06-19T08:00:00")
    _seed_role_msg(
        state, row_id="huge", role="R1", created_at="2026-06-19T08:01:00", text=huge_text,
    )
    service = _make_service(state)
    entries, cursor, _, _ = service.list_silent_for_roles(
        agent_instance_id=_INSTANCE, include_important=False, limit=10, role_after=None,
    )
    _check(
        _page_ids(entries) == ["msg-huge"],
        "byte ceiling: a single over-ceiling entry still ships alone, newest first (R4)",
    )
    _check(cursor is not None, "byte ceiling: an over-ceiling single-entry page still mints a cursor")


# ---------------------------------------------------------------------------
# 9. Q1 role-section fault-domain boundary (peer_inbox._collect_role_section)
# ---------------------------------------------------------------------------


class _RaisingState(_FakeState):
    """A state whose held-role enumeration query raises — simulates a transient
    role-table read fault / table-absent deploy window."""

    def query_state(self, namespace: str, data: dict[str, object]) -> dict[str, Any]:
        msg = "simulated role-table read failure"
        raise RuntimeError(msg)


def _inbox_request(role_after: str | None = None) -> PeerInboxRequest:
    return PeerInboxRequest(
        recipient_agent_id="claude_code",
        recipient_agent_instance_id=_INSTANCE,
        role_after=role_after,
    )


def test_q1_boundary_isolates_query_failure() -> None:
    service = _make_service(_RaisingState())
    entries, cursor, floor_applied, history, status, error = (
        service._collect_role_section(_inbox_request())  # noqa: SLF001
    )
    _check(
        entries == () and cursor is None and status is RoleSectionStatus.ERROR
        and floor_applied is False and history is None,
        "Q1: role-query failure → empty role section + status=ERROR (not raised)",
    )
    _check(
        error is not None and "simulated role-table read failure" in error,
        "Q1: role_section_error carries the failure repr",
    )


def test_q1_boundary_malformed_cursor_isolated() -> None:
    # A client's garbage role_after must fail CLOSED to the role section only —
    # it must NOT propagate (which would deny the caller its instance messages).
    state = _FakeState()
    _seed_binding(state, "Architect")
    service = _make_service(state)
    _, _, _, _, status, error = service._collect_role_section(  # noqa: SLF001
        _inbox_request(role_after="not!a!valid!cursor"),
    )
    _check(
        status is RoleSectionStatus.ERROR and error is not None,
        "Q1: malformed role_after isolated to role section (ERROR, not propagated)",
    )


def test_q1_boundary_ok_passthrough() -> None:
    state = _FakeState()
    _seed_binding(state, "Architect")
    _seed_role_msg(state, row_id="q1", role="Architect", created_at="2026-06-19T08:00:00")
    service = _make_service(state)
    entries, _, _, _, status, error = (
        service._collect_role_section(_inbox_request())  # noqa: SLF001
    )
    _check(
        status is RoleSectionStatus.OK and error is None and len(entries) == 1,
        "Q1: healthy role section → status=OK, error=None, entries served",
    )


def main() -> int:
    print("=== v10 role-inbox section (Control #1a) unit smoke ===")
    test_cursor_roundtrip_and_scope()
    test_cursor_fail_closed()
    test_multirole_kway_merge_pagination()
    test_held_role_set_change_resets()
    test_malformed_role_after_rejected()
    test_delivered_important_catchup()
    test_projection_fields()
    test_empty_holder()
    test_drain_page_oldest_first()
    test_floor_excludes_covered_rows_and_reports_applied()
    test_history_cursor_reveals_pre_mark_backlog()
    test_history_token_wrong_scope_resets_and_refloors()
    test_floor_is_noop_without_a_mark()
    test_byte_ceiling_truncates_short_of_the_row_limit()
    test_byte_ceiling_admits_at_least_one_oversized_entry()
    test_q1_boundary_isolates_query_failure()
    test_q1_boundary_malformed_cursor_isolated()
    test_q1_boundary_ok_passthrough()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
