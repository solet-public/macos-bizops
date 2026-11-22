#!/usr/bin/env python3
"""Session limits: the store, its lifecycle and the room function, offline over in-memory state.

One test method per check C0-C10. The state is in memory; no test creates a file.
"""

from __future__ import annotations

import sys
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(_ROOT / "ananta/src"))
sys.path.insert(0, str(_ROOT / "plugins/agent_messaging_plugin/src"))

from _real_state_fake import _STANDARDIZER_COLUMNS, RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE as NS  # noqa: E402
from ananta.types.column_types import ColumnType  # noqa: E402
from ananta.types.schema_standardizer import SchemaStandardizer  # noqa: E402
from session_origin_backfill_smoke import seed_legacy  # noqa: E402

from agent_messaging_plugin import session_limits as limits  # noqa: E402
from agent_messaging_plugin.schema import TABLE_MANAGED_SESSION as SESSIONS  # noqa: E402
from agent_messaging_plugin.schema import TABLE_MODEL_CAPABILITY_CELL as CELLS  # noqa: E402
from agent_messaging_plugin.schema import get_managed_session_schema, get_model_capability_cell_schema  # noqa: E402
from agent_messaging_plugin.session_origin_backfill import backfill_session_origins  # noqa: E402

NOW = datetime(2026, 11, 15, 12, 0, tzinfo=UTC)
ACTOR = "ases-operator"
BASE = {"selector": {"runtime": "codex"}, "sessions": 1, "words": "one codex", "reason": "fixture"}


def typed(state: RealShapeState) -> StateManagementInterface:
    return cast(StateManagementInterface, state)


def at(second: int) -> datetime:
    """A strictly later moment per call, so created_at order is the order of the writes."""
    return NOW + timedelta(seconds=second)


def fixture() -> RealShapeState:
    state = RealShapeState()
    tables = (limits.get_session_limit_schema_definition().tables[limits.TABLE_SESSION_LIMIT], get_managed_session_schema(), get_model_capability_cell_schema())
    for table in tables:
        state._enforced_columns[table.table_name] = frozenset(table.columns) | _STANDARDIZER_COLUMNS
    return state


def write(state: RealShapeState, table: str, record: dict[str, Any]) -> None:
    result = state.write_state(NS, {"table": table, "record": record})
    if result["action_status"] != "completed":
        raise AssertionError(f"fixture write to {table} was refused: {result!r}")


def session_row(instance: str, *, runtime: str | None = "codex", model: str = "gpt-5.6-luna", effort: str = "medium", lifecycle: str = "live") -> dict[str, Any]:
    return {"agent_instance_id": instance, "agent_runtime": runtime, "model": model, "effort": effort, "lifecycle_state": lifecycle, "session_origin": "spawned", "host": "tmux"}


def add_session(state: RealShapeState, instance: str, **fields: Any) -> None:
    write(state, SESSIONS, session_row(instance, **fields))


def add_cell(state: RealShapeState, runtime: str, model: str, effort: str, provider: str) -> None:
    write(state, CELLS, {"provider": provider, "runtime": runtime, "model": model, "effort": effort, "acceptance": "accepted", "cost_per_task_usd": 1.0, "enabled": True})


def candidate(runtime: str | None = "codex", model: str = "gpt-6-sol", effort: str = "medium") -> dict[str, Any]:
    return {"agent_runtime": runtime, "model": model, "effort": effort}


def put(state: RealShapeState, selector: dict[str, str], sessions: int, words: str, *, second: int, **extra: Any) -> str:
    params = {"selector": selector, "sessions": sessions, "words": words, "reason": "fixture", **extra}
    return str(limits.set_limit(typed(state), params, actor=ACTOR, now=at(second))["limit"]["limit_id"])


def room(state: RealShapeState, who: dict[str, Any], now: datetime = NOW) -> limits.RoomAnswer:
    return limits.read_room(typed(state), who, now)


def refused_by(answer: limits.RoomAnswer) -> list[str]:
    return [refusal.limit.limit_id for refusal in answer.refusals]


def correct(state: RealShapeState, limit_id: str, expected: int, *, second: int, actor: str = ACTOR, **changes: Any) -> dict[str, Any]:
    params = {"action": "correct", "limit_id": limit_id, "expected_revision": expected, **BASE, **changes}
    return limits.set_limit(typed(state), params, actor=actor, now=at(second))


def c4_fixture() -> tuple[RealShapeState, str, str]:
    """A: codex 5. B: gpt-6-sol 1. One live codex gpt-6-sol session."""
    state = fixture()
    first = put(state, {"runtime": "codex"}, 5, "five codex", second=1)
    second = put(state, {"model": "gpt-6-sol"}, 1, "one sol", second=2)
    add_session(state, "agi-sol", model="gpt-6-sol")
    return state, first, second


class SessionLimitsSmoke(unittest.TestCase):
    def test_c0_the_module_is_this_worktrees_and_its_schema_is_valid(self) -> None:
        self.assertTrue(Path(limits.__file__).resolve().is_relative_to(_ROOT), limits.__file__)
        definition = limits.get_session_limit_schema_definition()
        self.assertEqual(definition.validate(), [])
        standardized = SchemaStandardizer().standardize_schema(definition)
        table = definition.tables[limits.TABLE_SESSION_LIMIT]
        self.assertEqual((limits.TABLE_SESSION_LIMIT, definition.namespace, definition.version, set(definition.tables), table.id_prefix), ("session_limit", NS, "1.0.0", {"session_limit"}, "slm"))
        self.assertEqual(set(standardized.tables), {"session_limit"})
        self.assertEqual({name for name, column in table.columns.items() if column.not_null}, {"limit_id", "revision", "selector", "sessions", "words", "state", "reason", "set_by"})
        expected_types = {"revision": ColumnType.INTEGER, "sessions": ColumnType.INTEGER, "selector": ColumnType.JSON}
        for name, column in table.columns.items():
            self.assertEqual(column.type, expected_types.get(name, ColumnType.TEXT), name)
        self.assertEqual({(tuple(index.columns), index.unique) for index in table.indexes}, {(("limit_id", "revision"), True), (("state",), False)})

    def test_c1_two_codex_sessions_fill_a_codex_limit_of_two(self) -> None:
        state = fixture()
        limit_id = put(state, {"runtime": "codex"}, 2, "two codex at once", second=1)
        add_session(state, "agi-b")
        add_session(state, "agi-a")
        answer = room(state, candidate("codex"))
        self.assertFalse(answer.room)
        self.assertEqual(len(answer.refusals), 1)
        refusal = answer.refusals[0]
        self.assertEqual((refusal.limit.limit_id, refusal.limit.words, refusal.limit.sessions, refusal.count, refusal.in_use), (limit_id, "two codex at once", 2, 2, ("agi-a", "agi-b")))
        expected = {"limit_id": limit_id, "revision": 1, "selector": {"runtime": "codex"}, "sessions": 2, "words": "two codex at once", "ends_at": None, "count": 2, "in_use": ["agi-a", "agi-b"]}
        self.assertEqual(answer.to_dict(), {"room": False, "refusals": [expected]})
        self.assertTrue(room(state, candidate("claude_code", "claude-sonnet-5-5", "xhigh")).room)

    def test_c2_a_limit_of_zero_for_everything_refuses_every_candidate_with_no_rows(self) -> None:
        state = fixture()
        limit_id = put(state, {}, 0, "start nothing new", second=1)
        for who in (candidate("codex"), candidate("claude_code", "claude-sonnet-5-5", "xhigh"), candidate(None, "claude-sonnet-5-5", "xhigh")):
            answer = room(state, who)
            self.assertFalse(answer.room)
            self.assertEqual(refused_by(answer), [limit_id])
            self.assertEqual((answer.refusals[0].count, answer.refusals[0].in_use), (0, ()))
        self.assertEqual(state.rows(NS, SESSIONS), [])

    def test_c3_a_lapsed_limit_does_not_apply_and_reads_as_lapsed(self) -> None:
        past = fixture()
        add_session(past, "agi-a")
        made = limits.set_limit(typed(past), {**BASE, "ends_at": (NOW - timedelta(seconds=1)).isoformat()}, actor=ACTOR, now=NOW)
        self.assertEqual(len(made["warnings"]), 1)
        self.assertTrue(room(past, candidate("codex")).room)
        self.assertEqual([view["lapsed"] for view in limits.list_limits(typed(past), now=NOW)["limits"]], [True])
        future = fixture()
        add_session(future, "agi-a")
        limit_id = str(limits.set_limit(typed(future), {**BASE, "ends_at": (NOW + timedelta(hours=1)).isoformat()}, actor=ACTOR, now=NOW)["limit"]["limit_id"])
        self.assertEqual(refused_by(room(future, candidate("codex"))), [limit_id])
        self.assertEqual([view["lapsed"] for view in limits.list_limits(typed(future), now=NOW)["limits"]], [False])
        self.assertTrue(room(future, candidate("codex"), NOW + timedelta(hours=1)).room)

    def test_c4_every_matching_limit_must_hold_in_the_order_they_were_written(self) -> None:
        state, first, second = c4_fixture()
        self.assertEqual(refused_by(room(state, candidate("codex", "gpt-6-sol"))), [second])
        self.assertTrue(room(state, candidate("codex", "gpt-5.6-luna")).room)
        correct(state, first, 1, second=3, sessions=1, words="one codex")
        answer = room(state, candidate("codex", "gpt-6-sol"))
        self.assertEqual((refused_by(answer), [refusal.limit.revision for refusal in answer.refusals]), ([second, first], [1, 2]))
        self.assertEqual(refused_by(room(state, candidate("codex", "gpt-5.6-luna"))), [first])

    def test_c5_only_live_sessions_the_solet_started_are_counted(self) -> None:
        state = fixture()
        seed_legacy(typed(state), with_default=True)
        self.assertEqual(backfill_session_origins(typed(state))["updated"], 5)
        rows = {row["agent_instance_id"]: row for row in state.rows(NS, SESSIONS)}
        for identity, origin in (("old-spawn", "spawned"), ("old-registration", "registered"), ("old-canary", "synthetic")):
            row = rows[identity]
            self.assertEqual((row["session_origin"], row["host"], row["workspace"], row["spawned_by_instance_id"]), (origin, "tmux", "", ""))
        put(state, {}, 2, "two spawned sessions", second=1)
        answer = room(state, candidate())
        self.assertFalse(answer.room)
        self.assertEqual((answer.refusals[0].count, answer.refusals[0].in_use), (2, ("old-generic-spawn", "old-spawn")))

    def test_c6_every_non_terminal_lifecycle_state_is_counted(self) -> None:
        self.assertEqual(limits.LIVE_STATES, ("spawning", "live", "idle", "overdue", "parked"))
        state = fixture()
        for index, lifecycle in enumerate(limits.LIVE_STATES):
            add_session(state, f"agi-{index}", lifecycle=lifecycle)
        add_session(state, "agi-terminated", lifecycle="terminated")
        add_session(state, "agi-retired", lifecycle="retired")
        put(state, {"runtime": "codex"}, 5, "five codex", second=1)
        refusal = room(state, candidate("codex")).refusals[0]
        self.assertEqual((refusal.count, refusal.in_use), (5, ("agi-0", "agi-1", "agi-2", "agi-3", "agi-4")))

    def test_c7_provider_model_family_and_the_default_runtime_are_selectors(self) -> None:
        providers = {("claude_code", "claude-sonnet-5-5", "xhigh"): "anthropic"}
        self.assertEqual(limits.selector_view({"agent_runtime": None, "model": "claude-sonnet-5-5", "effort": "xhigh"}, providers), {"runtime": "claude_code", "model": "claude-sonnet-5-5", "effort": "xhigh", "provider": "anthropic"})
        self.assertEqual(limits.selector_view({"agent_runtime": "codex", "model": "gpt-6-sol", "effort": ""}, providers), {"runtime": "codex", "model": "gpt-6-sol"})
        by_provider = fixture()
        add_cell(by_provider, "codex", "gpt-6-sol", "medium", "openai")
        add_session(by_provider, "agi-cell", model="gpt-6-sol")
        add_session(by_provider, "agi-no-cell", model="gpt-9-none")
        put(by_provider, {"provider": "openai"}, 1, "one openai", second=1)
        self.assertEqual(room(by_provider, candidate("codex", "gpt-6-sol")).refusals[0].in_use, ("agi-cell",))
        self.assertTrue(room(by_provider, candidate("codex", "gpt-9-none")).room)
        by_family = fixture()
        add_session(by_family, "agi-sol", model="gpt-6-sol")
        add_session(by_family, "agi-luna", model="gpt-5.6-luna")
        put(by_family, {"model_family_prefix": "gpt-6"}, 1, "one gpt-6", second=1)
        self.assertEqual(room(by_family, candidate("codex", "gpt-6-sol")).refusals[0].in_use, ("agi-sol",))
        self.assertTrue(room(by_family, candidate("codex", "gpt-5.6-luna")).room)
        by_default = fixture()
        add_session(by_default, "agi-default", runtime=None, model="claude-sonnet-5-5", effort="xhigh")
        put(by_default, {"runtime": "claude_code"}, 1, "one claude_code", second=1)
        for runtime in ("claude_code", None):
            self.assertEqual(room(by_default, candidate(runtime, "claude-sonnet-5-5", "xhigh")).refusals[0].in_use, ("agi-default",))

    def test_c8_set_correct_and_retire_keep_every_revision(self) -> None:
        state = fixture()
        ends, later = (NOW + timedelta(hours=1)).isoformat(), (NOW + timedelta(hours=2)).isoformat()
        made = limits.set_limit(typed(state), {**BASE, "ends_at": ends}, actor="ases-a", now=at(1))
        limit_id = made["limit"]["limit_id"]
        self.assertTrue(limit_id.startswith("lim-"))
        self.assertEqual((made["limit"]["revision"], made["limit"]["state"], made["limit"]["set_by"], made["limit"]["set_at"], made["warnings"]), (1, "active", "ases-a", at(1), []))
        self.assertEqual([view["limit_id"] for view in made["limits"]], [limit_id])
        self.assertTrue(room(state, candidate("codex")).room)
        fixed = correct(state, limit_id, 1, second=2, actor="ases-b", sessions=0, words="none", reason="stop", ends_at=later)
        self.assertEqual((fixed["limit"]["revision"], fixed["limit"]["sessions"], fixed["limit"]["ends_at"], fixed["limit"]["lapsed"]), (2, 0, later, False))
        self.assertEqual([view["revision"] for view in fixed["limits"]], [2])
        self.assertEqual(refused_by(room(state, candidate("codex"))), [limit_id])
        for stale in (lambda: correct(state, limit_id, 1, second=3), lambda: limits.set_limit(typed(state), {"action": "retire", "limit_id": limit_id, "expected_revision": 1, "reason": "late"}, actor=ACTOR, now=at(3))):
            with self.assertRaises(limits.LimitError) as caught:
                stale()
            self.assertEqual(caught.exception.code, "stale_limit")
        retired = limits.set_limit(typed(state), {"action": "retire", "limit_id": limit_id, "expected_revision": 2, "reason": "done"}, actor="ases-c", now=at(4))
        self.assertEqual((retired["limit"]["revision"], retired["limit"]["state"], retired["limit"]["ended_by"], retired["limit"]["ended_reason"], retired["limit"]["ended_at"], retired["limits"]), (2, "retired", "ases-c", "done", at(4).isoformat(), []))
        with self.assertRaises(limits.LimitError) as caught:
            limits.set_limit(typed(state), {"action": "retire", "limit_id": limit_id, "expected_revision": 2, "reason": "again"}, actor=ACTOR, now=at(5))
        self.assertEqual(caught.exception.code, "stale_limit")
        self.assertTrue(room(state, candidate("codex")).room)
        listing = limits.list_limits(typed(state), now=at(6))
        self.assertEqual((listing["limits"], listing["now"]), ([], at(6).isoformat()))
        newest, oldest = listing["history"]
        self.assertEqual((newest["revision"], newest["state"], oldest["revision"], oldest["state"]), (2, "retired", 1, "replaced"))
        self.assertEqual((oldest["set_by"], oldest["ended_by"], oldest["ended_reason"], newest["set_by"], newest["ended_by"]), ("ases-a", "ases-b", "stop", "ases-b", "ases-c"))
        self.assertEqual(len(state.rows(NS, limits.TABLE_SESSION_LIMIT)), 2)
        self.assertEqual([view["revision"] for view in limits.list_limits(typed(state), history_limit=1, now=at(6))["history"]], [2])
        cleared = fixture()
        offset = limits.set_limit(typed(cleared), {**BASE, "ends_at": "2026-11-15T16:00:00+02:00"}, actor=ACTOR, now=at(1))
        self.assertEqual((offset["limit"]["ends_at"], offset["limit"]["lapsed"]), ("2026-11-15T14:00:00+00:00", False))
        omitted = correct(cleared, offset["limit"]["limit_id"], 1, second=2)
        self.assertEqual((omitted["limit"]["revision"], omitted["limit"]["ends_at"], omitted["limit"]["lapsed"]), (2, None, False))
        kept = correct(cleared, offset["limit"]["limit_id"], 2, second=3, ends_at="2026-11-15T14:00:00+00:00")
        self.assertEqual((kept["limit"]["revision"], kept["limit"]["ends_at"]), (3, "2026-11-15T14:00:00+00:00"))

    def test_c9_bad_input_is_parameter_invalid_and_writes_nothing(self) -> None:
        state = fixture()
        without_selector = {key: value for key, value in BASE.items() if key != "selector"}
        cases: dict[str, dict[str, Any]] = {
            "asker selector": {**BASE, "selector": {"asker": "x"}},
            "project selector": {**BASE, "selector": {"project": "x"}},
            "blank selector value": {**BASE, "selector": {"runtime": " "}},
            "missing selector": without_selector,
            "negative sessions": {**BASE, "sessions": -1},
            "bool sessions": {**BASE, "sessions": True},
            "float sessions": {**BASE, "sessions": 1.5},
            "blank words": {**BASE, "words": "  "},
            "blank reason": {**BASE, "reason": ""},
            "naive ends_at": {**BASE, "ends_at": "2026-11-16T00:00:00"},
            "unparseable ends_at": {**BASE, "ends_at": "tomorrow"},
            "number ends_at": {**BASE, "ends_at": 5},
            "action delete": {**BASE, "action": "delete"},
            "correct without limit_id": {**BASE, "action": "correct", "expected_revision": 1},
            "correct without expected_revision": {**BASE, "action": "correct", "limit_id": "lim-x"},
            "correct with bool expected_revision": {**BASE, "action": "correct", "limit_id": "lim-x", "expected_revision": True},
            "retire with revision 0": {"action": "retire", "limit_id": "lim-x", "expected_revision": 0, "reason": "done"},
            "retire without reason": {"action": "retire", "limit_id": "lim-x", "expected_revision": 1},
        }
        for name, params in cases.items():
            with self.subTest(name), self.assertRaises(limits.LimitError) as caught:
                limits.set_limit(typed(state), params, actor=ACTOR, now=NOW)
            self.assertEqual(caught.exception.code, "parameter_invalid", name)
        for bad in (0, 101, True, "5"):
            with self.subTest(history_limit=bad), self.assertRaises(limits.LimitError) as caught:
                limits.list_limits(typed(state), history_limit=bad, now=NOW)
            self.assertEqual(caught.exception.code, "parameter_invalid")
        with self.assertRaises(limits.LimitError) as caught:
            correct(state, "lim-not-there", 1, second=1)
        self.assertEqual(caught.exception.code, "stale_limit")
        self.assertEqual(state.rows(NS, limits.TABLE_SESSION_LIMIT), [])
        past = (NOW - timedelta(hours=1)).isoformat()
        kept = limits.set_limit(typed(state), {**BASE, "ends_at": past}, actor=ACTOR, now=NOW)
        self.assertEqual((len(kept["warnings"]), kept["limit"]["ends_at"], kept["limit"]["lapsed"]), (1, past, True))
        self.assertEqual(limits.set_limit(typed(state), BASE, actor=ACTOR, now=at(1))["warnings"], [])

    def test_c10_room_for_over_the_rows_equals_read_room(self) -> None:
        state, first, _ = c4_fixture()
        correct(state, first, 1, second=3, sessions=1, words="one codex")
        views = limits.list_limits(typed(state), now=NOW)["limits"]
        active = [limits.SessionLimit(view["limit_id"], view["revision"], view["selector"], view["sessions"], view["words"], None) for view in views]
        providers = {(row["runtime"], row["model"], row["effort"]): row["provider"] for row in state.rows(NS, CELLS)}
        for who in (candidate("codex", "gpt-6-sol"), candidate("codex", "gpt-5.6-luna"), candidate("claude_code", "claude-sonnet-5-5", "xhigh")):
            self.assertEqual(limits.room_for(who, active, state.rows(NS, SESSIONS), providers, NOW).to_dict(), room(state, who).to_dict())


if __name__ == "__main__":
    unittest.main()
