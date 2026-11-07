#!/usr/bin/env python3
"""Offline smoke for accepted-but-unfired authorization obligations.

A successor session with no memory of the seat that accepted a standing
authorization must find it through the public verbs, read its current and
withdrawn predicates, and never be able to fire, revive or rewrite it outside
the lifecycle. Runs against ``RealShapeState`` (real provider ActionResult
envelopes and a rolling-back transaction); the private-PostgreSQL proof of
the same contract is ``accepted_authorization_pg_smoke.py``.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/accepted_authorization_smoke.py
"""

from __future__ import annotations

import copy
import json
import sys
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from itertools import count
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(_ROOT / "ananta" / "src"))
sys.path.insert(0, str(_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.core.actions.action_factory import ActionFactory  # noqa: E402
from ananta.error_handling import FrameworkError  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402

import agent_messaging_plugin  # noqa: E402
from agent_messaging_plugin import accepted_authorization_verbs as verbs  # noqa: E402
from agent_messaging_plugin.held_authorization_store import record_held_authorization, retire_held_authorization  # noqa: E402
from agent_messaging_plugin.register_unit_client import VerbError  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    TABLE_ACCEPTED_AUTHORIZATION,
    TABLE_ACCEPTED_AUTHORIZATION_EVENT,
    get_accepted_authorization_schema_definition,
)

if TYPE_CHECKING:
    from agent_messaging_plugin.plugin import AgentMessagingPlugin

PREDICATE_A = "clone URL published + sealed sha recorded + repository suffix _local_bizops_921c694a0"
PREDICATE_B = "clone URL published + sealed sha recorded + repository suffix _a6e684b28"
PREDICATE_C = "clone URL published + sealed sha recorded + repository suffix _973ffefe6"
EVIDENCE = "clone https://example.invalid/fixture sealed sha 9f3e"
PLUGIN_KEY = "plugin::agent_messaging_plugin::"
PROCESSES = _ROOT / "plugins/agent_messaging_plugin/knowledge_base/processes"
VERBS = ("record_accepted_authorization", "change_accepted_authorization", "get_authorization_history", "list_held_authorizations")
_STANDARD_COLUMNS = frozenset({"id", "external_id", "namespace", "created_at", "updated_at", "created_by", "updated_by", "name", "is_deleted"})

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


def _state() -> RealShapeState:
    """A fake whose ``created_at`` stamps follow insertion order."""
    state = RealShapeState()
    clock = count(1)
    state.now_iso = lambda: f"2026-10-18T12:00:{next(clock):06d}"
    return state


def _plugin_class() -> type[AgentMessagingPlugin]:
    """The plugin class, imported on first use: its module reads SOLET_NAME at
    import, which a private-PostgreSQL capture scrubs, and the PG smoke
    reuses this module's probes without ever building a plugin."""
    from agent_messaging_plugin.plugin import AgentMessagingPlugin

    return AgentMessagingPlugin


def _plugin(state: object) -> AgentMessagingPlugin:
    plugin = object.__new__(_plugin_class())
    plugin._get_state_service = lambda: state  # type: ignore[method-assign]  # noqa: SLF001
    return plugin


def _call(state: object, verb: str, **params: Any) -> dict[str, Any]:
    """One public process call on a fresh plugin: no session memory survives."""
    result: dict[str, Any] = getattr(_plugin(state), verb)(params, {})
    return result


def _data(result: dict[str, Any]) -> dict[str, Any]:
    if result["action_status"] != "completed":
        raise AssertionError(f"expected completed, got {result['error']}")
    data: dict[str, Any] = result["data"]
    return data


def _code(result: dict[str, Any]) -> str | None:
    return None if result["action_status"] == "completed" else str(result["error"]["code"])


def _codes(*results: dict[str, Any]) -> tuple[str | None, ...]:
    return tuple(_code(result) for result in results)


def _facts(rows: list[dict[str, Any]], *fields: str) -> list[tuple[Any, ...]]:
    """The named fields of each row, in order: one comparable value per list."""
    return [tuple(row.get(field) for field in fields) for row in rows]


def _rows(state: RealShapeState, table: str) -> list[dict[str, Any]]:
    return copy.deepcopy(state.rows(AGENT_ROLE_BINDING_NAMESPACE, table))


def _tables(state: RealShapeState) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Projection and event rows, for proving a refused write wrote nothing."""
    return _rows(state, TABLE_ACCEPTED_AUTHORIZATION), _rows(state, TABLE_ACCEPTED_AUTHORIZATION_EVENT)


def _operations(state: RealShapeState) -> list[str]:
    return [str(event["operation"]) for event in _rows(state, TABLE_ACCEPTED_AUTHORIZATION_EVENT)]


def _record(state: object, message: str = "arm-A", predicate: str = PREDICATE_A, **extra: Any) -> dict[str, Any]:
    params: dict[str, Any] = {
        "requesting_peer": "Coordinator-Main",
        "owed_by_role": "Git-Controller",
        "branch_or_request_ref": "seed-clone/fixture",
        "authorizing_message_id": message,
        "act": "seed-clone",
        "predicate": predicate,
        "reason": "standing authorization accepted at intake; fires on publication",
        "actor": "Git-Controller",
        **extra,
    }
    return _data(_call(state, "record_accepted_authorization", **params))


def _change(state: object, entry_id: str, version: int, operation: str, **extra: Any) -> dict[str, Any]:
    params: dict[str, Any] = {"entry_id": entry_id, "expected_version": version, "operation": operation, "reason": f"{operation} by test", "actor": "Coordinator-Main", **extra}
    return _call(state, "change_accepted_authorization", **params)


def _corrected_to_c(state: RealShapeState) -> str:
    """Record A, then reissue B and C as corrections; returns the entry id."""
    entry_id = str(_record(state)["entry_id"])
    _data(_change(state, entry_id, 1, "correct", predicate=PREDICATE_B, authorizing_message_id="arm-B"))
    _data(_change(state, entry_id, 2, "correct", predicate=PREDICATE_C, authorizing_message_id="arm-C"))
    return entry_id


def _history(state: object, **selector: Any) -> dict[str, Any]:
    return _data(_call(state, "get_authorization_history", **selector))


def _listed(state: object, **params: Any) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = _data(_call(state, "list_held_authorizations", **params))["entries"]
    return entries


class _ProbedTransaction:
    """One transaction with a stale read, a read barrier, a failing table or CAS counts."""

    def __init__(self, txn: Any, probe: SimpleNamespace) -> None:
        self._txn = txn
        self._probe = probe

    def query_state(self, namespace: str, query: dict[str, Any]) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = self._txn.query_state(namespace, query)
        if self._probe.read_barrier is not None:
            self._probe.read_barrier.wait()
        if self._probe.stale_row is not None and query["table"] == TABLE_ACCEPTED_AUTHORIZATION:
            return [dict(self._probe.stale_row)]
        return rows

    def update_state(self, namespace: str, query: dict[str, Any], updates: dict[str, Any]) -> int:
        matched: int = self._txn.update_state(namespace, query, updates)
        self._probe.cas_matches.append(matched)
        return matched

    def write_state(self, namespace: str, data: dict[str, Any]) -> str:
        if data["table"] == self._probe.fail_table:
            self._probe.failed_writes += 1
            raise RuntimeError("injected append failure")
        written: str = self._txn.write_state(namespace, data)
        return written


class _ProbedState:
    """A state (the fake, or a real state service) whose transactions run
    through :class:`_ProbedTransaction`; ``read_barrier`` holds each read
    until every racing writer has read."""

    def __init__(self, inner: Any, *, stale_row: dict[str, Any] | None = None, fail_table: str | None = None, read_barrier: Any = None) -> None:
        self._inner = inner
        self.probe = SimpleNamespace(stale_row=stale_row, fail_table=fail_table, cas_matches=[], failed_writes=0, read_barrier=read_barrier)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    @contextmanager
    def transactional(self) -> Iterator[_ProbedTransaction]:
        with self._inner.transactional() as txn:
            yield _ProbedTransaction(txn, self.probe)


def _refused(action: Callable[[], object]) -> tuple[str | None, str | None]:
    """(VerbError code, other exception type) of ``action``; (None, None) if it ran."""
    try:
        action()
    except VerbError as exc:
        return exc.code, None
    except Exception as exc:  # noqa: BLE001 - recorded, the named assertion decides
        return None, type(exc).__name__
    return None, None


def test_pending_visible() -> None:
    """A successor plugin with no memory of the accepting seat sees the obligation."""
    state = _state()
    record: Any = getattr(_plugin(state), "record_accepted_authorization", None)
    _check(callable(record), "record_accepted_authorization_is_public")
    if callable(record):
        recorded = record(
            {
                "requesting_peer": "Coordinator-Main",
                "owed_by_role": "Git-Controller",
                "branch_or_request_ref": "seed-clone/fixture",
                "authorizing_message_id": "arm-A",
                "act": "seed-clone",
                "predicate": PREDICATE_A,
                "reason": "standing authorization accepted at intake; fires on publication",
                "actor": "Git-Controller",
            },
            {},
        )
        _check(recorded["action_status"] == "completed", "record_accepted_authorization_completes")
    successor = _plugin(state)
    entries = successor.list_held_authorizations({"owed_by_role": "Git-Controller"}, {})["data"]["entries"]
    _check(
        _facts(entries, "entry_kind", "state", "authorizing_message_id", "predicate") == [("accepted", "accepted", "arm-A", PREDICATE_A)],
        "accepted_unfired_visible_to_successor",
    )


def test_record_shape_and_validation() -> None:
    """Record writes version 1 plus one record event, and refuses blanks."""
    state = _state()
    recorded = _record(state, notes="re-issued three times; watch the suffix")
    _check(
        (recorded["status"], recorded["state"], recorded["version"], recorded["entry"]["notes"]) == ("recorded", "accepted", 1, "re-issued three times; watch the suffix"),
        "record_starts_accepted_at_version_one",
    )
    events = _rows(state, TABLE_ACCEPTED_AUTHORIZATION_EVENT)
    _check(
        [(e["operation"], e["before_snapshot"], e["authorizing_message_id"], e["changes"].get("predicate")) for e in events] == [("record", None, "arm-A", "set")],
        "record_appends_one_record_event",
    )
    base = {"requesting_peer": "Coordinator-Main", "owed_by_role": "Git-Controller", "branch_or_request_ref": "x", "authorizing_message_id": "arm-X", "act": "seed-clone", "reason": "r", "actor": "a"}
    missing = _call(state, "record_accepted_authorization", **base, predicate="   ")
    blank_notes = _call(state, "record_accepted_authorization", **base, predicate="p", notes=" ")
    _check(
        (*_codes(missing, blank_notes), len(_rows(state, TABLE_ACCEPTED_AUTHORIZATION))) == ("missing_argument", "invalid_argument", 1),
        "record_refuses_blank_required_and_blank_notes",
    )
    declared = get_accepted_authorization_schema_definition().tables
    _check(
        all(set(row) - _STANDARD_COLUMNS <= set(schema.columns) for table, schema in declared.items() for row in _rows(state, table)),
        "stored_columns_are_declared",
    )


def test_correction_history() -> None:
    """A -> B -> C keeps A and B in history and exposes only C as current."""
    state = _state()
    entry_id = _corrected_to_c(state)
    history = _history(state, entry_id=entry_id)
    _check(
        ([event["after"]["predicate"] for event in history["events"]], [event["before"]["predicate"] for event in history["events"][1:]]) == ([PREDICATE_A, PREDICATE_B, PREDICATE_C], [PREDICATE_A, PREDICATE_B]),
        "correction_retains_old_predicates",
    )
    _check(
        _facts(history["current"], "predicate", "authorizing_message_id", "version") == [(PREDICATE_C, "arm-C", 3)],
        "only_latest_predicate_is_current",
    )
    _check(_facts(_listed(state), "predicate") == [(PREDICATE_C,)], "successor_list_shows_only_current_predicate")
    by_old = _history(state, authorizing_message_id="arm-A")
    supersessions = [(e["operation"], (e["before"] or {}).get("authorizing_message_id"), e["after"]["authorizing_message_id"], e["actor"], e["reason"]) for e in by_old["events"]]
    _check(
        (by_old["entry_ids"], supersessions[1:2], by_old["current"][0]["predicate"]) == ([entry_id], [("correct", "arm-A", "arm-B", "Coordinator-Main", "correct by test")], PREDICATE_C),
        "old_message_id_finds_its_withdrawal_and_current_predicate",
    )
    by_middle = _history(state, authorizing_message_id="arm-B")
    by_current = _history(state, authorizing_message_id="arm-C")
    unknown = _history(state, authorizing_message_id="arm-never-recorded")
    _check(
        (by_middle["entry_ids"], by_current["entry_ids"], by_current["count"], unknown["entry_ids"], unknown["events"], unknown["next_cursor"]) == ([entry_id], [entry_id], 3, [], [], None),
        "lookup_by_old_and_current_message_ids",
    )
    unnamed = _change(state, entry_id, 3, "correct", predicate="a fourth predicate without its authorizing message")
    _check(_code(unnamed) == "authorizing_message_required", "predicate_correction_names_its_authorizing_message")
    neither = _call(state, "get_authorization_history")
    both = _call(state, "get_authorization_history", entry_id=entry_id, authorizing_message_id="arm-A")
    _check(_codes(neither, both) == ("invalid_argument", "invalid_argument"), "history_selector_is_exactly_one")


def test_notes_lifecycle() -> None:
    """Notes are set, corrected and cleared as three distinct recorded changes."""
    state = _state()
    entry_id = str(_record(state)["entry_id"])
    set_notes = _data(_change(state, entry_id, 1, "correct", notes="first note"))
    corrected = _data(_change(state, entry_id, 2, "correct", notes="second note"))
    cleared = _data(_change(state, entry_id, 3, "correct", clear_notes=True))
    changes = [event["changes"] for event in _rows(state, TABLE_ACCEPTED_AUTHORIZATION_EVENT)[1:]]
    _check(
        ((set_notes["entry"]["notes"], corrected["entry"]["notes"], cleared["entry"]["notes"]), changes) == (("first note", "second note", None), [{"notes": "set"}, {"notes": "correct"}, {"notes": "clear"}]),
        "notes_set_correct_clear_are_distinct_events",
    )
    again = _change(state, entry_id, 4, "correct", clear_notes=True)
    both = _change(state, entry_id, 4, "correct", notes="x", clear_notes=True)
    blank = _change(state, entry_id, 4, "correct", predicate="")
    _check(_codes(again, both, blank) == ("no_change", "invalid_argument", "invalid_argument"), "notes_noop_conflict_and_blank_required_refused")


def test_stale_and_duplicate() -> None:
    """A stale writer, before or at the compare-and-set, and a replayed or
    duplicate terminal change all write nothing."""
    state = _state()
    entry_id = str(_record(state)["entry_id"])
    stale_row = copy.deepcopy(_rows(state, TABLE_ACCEPTED_AUTHORIZATION)[0])
    _data(_change(state, entry_id, 1, "correct", predicate=PREDICATE_B, authorizing_message_id="arm-B"))
    baseline = _tables(state)
    sequential = _change(state, entry_id, 1, "correct", notes="written from a stale read")
    _check((_code(sequential), _tables(state) == baseline) == ("stale_version", True), "stale_version_refused_before_write")
    probed = _ProbedState(state, stale_row=stale_row)
    params = {"entry_id": entry_id, "expected_version": 1, "operation": "correct", "reason": "raced", "actor": "late-writer", "notes": "lost race"}
    refusal = _refused(lambda: verbs.change_accepted_authorization(probed, params))  # type: ignore[arg-type]
    print(f"        reach: stale read version 1, CAS matches={probed.probe.cas_matches}, refusal={refusal}")
    _check((refusal, probed.probe.cas_matches, _tables(state) == baseline) == (("stale_version", None), [0], True), "stale_write_preserves_projection_and_history")
    fired = _data(_change(state, entry_id, 2, "fire", predicate=PREDICATE_B, evidence=EVIDENCE))
    after_fire = _tables(state)
    replay = _change(state, entry_id, 2, "fire", predicate=PREDICATE_B, evidence="replayed")
    second = _change(state, entry_id, 3, "fire", predicate=PREDICATE_B, evidence="second fire")
    _check(
        (fired["state"], *_codes(replay, second), _tables(state) == after_fire, len(after_fire[1])) == ("fired", "stale_version", "invalid_transition", True, 3),
        "duplicate_terminal_operation_writes_nothing",
    )


def test_fire_rules() -> None:
    """Only the current predicate fires; firing records evidence and is terminal."""
    state = _state()
    entry_id = _corrected_to_c(state)
    baseline = _tables(state)
    withdrawn = [_change(state, entry_id, 3, "fire", predicate=predicate, evidence="fired against a withdrawn predicate") for predicate in (PREDICATE_A, PREDICATE_B)]
    _check((_codes(*withdrawn), _tables(state) == baseline) == (("predicate_mismatch", "predicate_mismatch"), True), "withdrawn_predicate_cannot_fire")
    no_evidence = _change(state, entry_id, 3, "fire", predicate=PREDICATE_C)
    foreign = _change(state, entry_id, 3, "fire", predicate=PREDICATE_C, evidence="e", act="something else")
    _check((_codes(no_evidence, foreign), _tables(state) == baseline) == (("missing_argument", "invalid_argument"), True), "fire_needs_evidence_and_takes_no_rewrite")
    entry = _data(_change(state, entry_id, 3, "fire", predicate=PREDICATE_C, evidence=EVIDENCE))["entry"]
    _check(_facts([entry], "state", "terminal_evidence", "terminal_actor", "predicate") == [("fired", EVIDENCE, "Coordinator-Main", PREDICATE_C)], "current_predicate_fire_records_supplied_evidence")
    _check(
        (_listed(state), _facts(_listed(state, include_retired=True), "state"), _operations(state)) == ([], [("fired",)], ["record", "correct", "correct", "fire"]),
        "fired_leaves_pending_list_and_stays_in_history",
    )
    touched = {table for (_namespace, table), rows in state._rows.items() if rows}  # noqa: SLF001 - every table holding rows
    _check(touched <= {TABLE_ACCEPTED_AUTHORIZATION, TABLE_ACCEPTED_AUTHORIZATION_EVENT}, "fire_is_bookkeeping_only")
    reopened = _change(state, entry_id, 4, "reopen", authorizing_message_id="arm-D", predicate=PREDICATE_C)
    rewrite = _change(state, entry_id, 4, "correct", predicate=PREDICATE_A, authorizing_message_id="arm-D")
    _check((_codes(reopened, rewrite), len(_operations(state))) == (("invalid_transition", "invalid_transition"), 4), "fired_is_terminal")


def test_withdraw_retire_reopen() -> None:
    """Withdrawn and retired entries keep correctable metadata and reopen only
    on a fresh authorizing message."""
    state = _state()
    entry_id = str(_record(state)["entry_id"])
    withdrawn = _data(_change(state, entry_id, 1, "withdraw", evidence="arm-W withdrew it"))
    again = _change(state, entry_id, 2, "withdraw")
    _check((withdrawn["state"], withdrawn["entry"]["terminal_reason"], _code(again)) == ("withdrawn", "withdraw by test", "invalid_transition"), "withdraw_needs_accepted_and_stamps_terminal")
    metadata = _data(_change(state, entry_id, 2, "correct", owed_by_role="Git-Controller-2"))
    authority = _change(state, entry_id, 3, "correct", predicate=PREDICATE_B, authorizing_message_id="arm-B")
    _check((metadata["state"], metadata["entry"]["owed_by_role"], _code(authority)) == ("withdrawn", "Git-Controller-2", "invalid_transition"), "terminal_metadata_correctable_authority_not")
    stale_message = _change(state, entry_id, 3, "reopen", authorizing_message_id="arm-A", predicate=PREDICATE_B)
    entry = _data(_change(state, entry_id, 3, "reopen", authorizing_message_id="arm-R", predicate=PREDICATE_B))["entry"]
    _check(
        (_code(stale_message), *_facts([entry], "state", "authorizing_message_id", "predicate", "terminal_reason")[0]) == ("authorization_not_fresh", "accepted", "arm-R", PREDICATE_B, None),
        "reopen_needs_a_fresh_authorization",
    )
    retired = _data(_change(state, entry_id, 4, "retire"))
    revived = _data(_change(state, entry_id, 5, "reopen", authorizing_message_id="arm-S", predicate=PREDICATE_C, act="seed-clone-v2"))
    _check(
        (retired["state"], revived["entry"]["act"], _operations(state)) == ("retired", "seed-clone-v2", ["record", "withdraw", "correct", "reopen", "retire", "reopen"]),
        "retire_and_reopen_append_history",
    )


def test_event_failure_rollback() -> None:
    """A failed event append rolls the projection back, for change and record."""
    state = _state()
    entry_id = str(_record(state)["entry_id"])
    baseline = _tables(state)
    probed = _ProbedState(state, fail_table=TABLE_ACCEPTED_AUTHORIZATION_EVENT)
    params = {"entry_id": entry_id, "expected_version": 1, "operation": "fire", "reason": "r", "actor": "a", "predicate": PREDICATE_A, "evidence": "e"}
    refusal = _refused(lambda: verbs.change_accepted_authorization(probed, params))  # type: ignore[arg-type]
    print(f"        reach: CAS matches={probed.probe.cas_matches}, failed event writes={probed.probe.failed_writes}, raised={refusal}")
    _check((probed.probe.cas_matches, probed.probe.failed_writes, refusal, _tables(state) == baseline) == ([1], 1, (None, "RuntimeError"), True), "event_failure_rolls_back_projection")
    record_params = {"requesting_peer": "p", "owed_by_role": "Git-Controller", "branch_or_request_ref": "b", "authorizing_message_id": "arm-F", "act": "seed-clone", "predicate": "q", "reason": "r", "actor": "a"}
    refusal = _refused(lambda: verbs.record_accepted_authorization(probed, record_params))  # type: ignore[arg-type]
    _check((refusal, _tables(state) == baseline) == ((None, "RuntimeError"), True), "record_event_failure_rolls_back_projection")


def test_filters_and_legacy_refusals() -> None:
    """Filters compose across both kinds; refusal rows and their retire path
    behave exactly as before and never touch an accepted entry."""
    state = _state()
    _record(state, owed_by_role="Git-Controller", requesting_peer="lane-a")
    _record(state, owed_by_role="Git-Controller", requesting_peer="lane-b")
    _record(state, owed_by_role="Coordinator", requesting_peer="lane-a")
    both = _listed(state, owed_by_role="Git-Controller", requesting_peer="lane-a")
    role = _listed(state, owed_by_role="Git-Controller")
    _check((_facts(both, "owed_by_role", "requesting_peer"), len(role)) == ([("Git-Controller", "lane-a")], 2), "accepted_filters_compose")
    refusal_id = record_held_authorization(state, requesting_peer="lane-a", owed_by_role="Git-Controller", branch_or_request_ref="feature/a", reason="citation only")  # type: ignore[arg-type]
    listed = _listed(state, owed_by_role="Git-Controller", requesting_peer="lane-a")
    _check(
        _facts(listed, "entry_kind", "entry_id", "reason", "retired_at", "state", "predicate") == [("refusal", refusal_id, "citation only", None, None, None), ("accepted", listed[-1]["entry_id"], None, None, "accepted", PREDICATE_A)],
        "refusal_rows_keep_their_shape_and_kind",
    )
    before = _tables(state)
    misdirected = retire_held_authorization(state, entry_id=str(listed[-1]["entry_id"]), retired_reason="authorized", retired_by="Git-Controller", retired_at="2026-10-18T12:00:00+00:00")  # type: ignore[arg-type]
    _check((misdirected, _tables(state) == before) == (False, True), "refusal_retire_cannot_retire_accepted")
    retire = {"entry_id": refusal_id, "retired_by": "Git-Controller", "retired_at": "2026-10-18T12:00:00+00:00"}
    retired = _call(state, "retire_held_authorization", **retire, retired_reason="authorized")
    twice = _call(state, "retire_held_authorization", **retire, retired_reason="superseded")
    refusals = [entry for entry in _listed(state, requesting_peer="lane-a", include_retired=True) if entry["entry_kind"] == "refusal"]
    _check((_codes(retired, twice), _facts(refusals, "retired_reason")) == ((None, "entry_not_found_or_already_retired"), [("authorized",)]), "legacy_refusal_double_retire_unchanged")


def _pages(state: object, limit: int | None = None, **filters: Any) -> list[list[str]]:
    """Every page of the list, as entry ids, following ``next_cursor``."""
    pages: list[list[str]] = []
    cursor: str | None = None
    while len(pages) <= 200:
        params: dict[str, Any] = {**filters, **({} if limit is None else {"limit": limit}), **({} if cursor is None else {"cursor": cursor})}
        page = _data(_call(state, "list_held_authorizations", **params))
        pages.append([str(entry["entry_id"]) for entry in page["entries"]])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    return pages


def _history_revisions(state: object, entry_id: str, limit: int) -> list[int]:
    seen: list[int] = []
    cursor: str | None = None
    while len(seen) <= 200:
        page = _history(state, entry_id=entry_id, limit=limit, **({} if cursor is None else {"cursor": cursor}))
        seen.extend(int(event["revision"]) for event in page["events"])
        cursor = page["next_cursor"]
        if cursor is None:
            break
    return seen


def _refusals(state: RealShapeState, total: int) -> list[str]:
    return [record_held_authorization(state, requesting_peer=f"lane-{index}", owed_by_role="Git-Controller", branch_or_request_ref=f"b{index}", reason="r") for index in range(total)]  # type: ignore[arg-type]


def test_pagination() -> None:
    """Pages cover refusals then accepted entries exactly once, continue past
    one page, stay tie-safe, and validate their arguments."""
    state = _state()
    refusals = _refusals(state, 3)
    accepted = [str(_record(state, message=f"arm-{index}")["entry_id"]) for index in range(4)]
    pages = _pages(state, limit=2)
    _check(([len(page) for page in pages], sum(pages, [])) == ([2, 2, 2, 1], [*refusals, *accepted]), "combined_pages_cover_both_domains_once")
    boundary = _state()
    _refusals(boundary, 2)
    _record(boundary)
    first = _data(_call(boundary, "list_held_authorizations", limit=2))
    _check(([len(page) for page in _pages(boundary, limit=2)], first["next_cursor"]) == ([2, 1], "accepted:"), "refusals_filling_a_page_hand_over_to_accepted")
    tied = _state()
    tied.now_iso = lambda: "2026-10-18T12:00:00"
    tied_ids = sorted(str(_record(tied, message=f"arm-{index}")["entry_id"]) for index in range(101))
    default_pages = _pages(tied)
    _check(([len(page) for page in default_pages], sum(default_pages, [])) == ([100, 1], tied_ids), "default_page_continues_past_one_hundred_tie_safe")
    bad = _codes(_call(state, "list_held_authorizations", limit=101), _call(state, "list_held_authorizations", cursor="refusal:hau_missing"), _call(state, "list_held_authorizations", cursor="elsewhere:x"))
    _check(bad == ("invalid_argument", "invalid_cursor", "invalid_cursor"), "page_arguments_validated")
    for index in range(4):
        _data(_change(state, accepted[0], index + 1, "correct", notes=f"note {index}"))
    _check(_history_revisions(state, accepted[0], 2) == [1, 2, 3, 4, 5], "history_pages_in_revision_order")


def _process_factory(method: Any, key: str) -> ActionFactory:
    metadata = method._platform_process_metadata
    return ActionFactory(
        process_registry={"processes": {key: {"parameters": {name: parameter.to_dict() for name, parameter in metadata.parameters.items()}}}},
        template_engine=SimpleNamespace(resolve_templates=lambda action_def, context: action_def),
        state_service=SimpleNamespace(generate_unique_string=lambda **_kwargs: {"action_status": "completed", "data": {"random_string": "accepted-smoke"}}),
        action_event_recorder=SimpleNamespace(store_action_event=lambda _action: "ae-accepted-smoke"),
    )


def _bridge_accepts(verb: str, arguments: dict[str, Any]) -> bool:
    key = PLUGIN_KEY + verb
    _process_factory(getattr(_plugin_class(), verb), key).submit_action_definition(
        {"process_key": key, "arguments": arguments, "flow_id": "flow-accepted-smoke", "result_processor_kind": None, "error_processor_kind": None},
    )
    return True


def _bridge_refuses_unknown(verb: str) -> bool:
    try:
        _bridge_accepts(verb, {"xyzzy": "value"})
    except FrameworkError as exc:
        return exc.error_code == "action.unknown_arguments"
    return False


def _bridge_arguments() -> dict[str, dict[str, Any]]:
    """Every declared parameter of every lifecycle verb, filled once."""
    return {
        "record_accepted_authorization": {"requesting_peer": "p", "owed_by_role": "r", "branch_or_request_ref": "b", "authorizing_message_id": "m", "act": "a", "predicate": "q", "reason": "x", "actor": "y", "notes": "n"},
        "change_accepted_authorization": {
            "entry_id": "aau_x",
            "expected_version": 1,
            "operation": "correct",
            "reason": "x",
            "actor": "y",
            "predicate": "q",
            "act": "a",
            "authorizing_message_id": "m",
            "requesting_peer": "p",
            "owed_by_role": "r",
            "branch_or_request_ref": "b",
            "notes": "n",
            "clear_notes": False,
            "evidence": "e",
        },
        "get_authorization_history": {"entry_id": "aau_x", "authorizing_message_id": "m", "limit": 5, "cursor": "aae_x"},
        "list_held_authorizations": {"owed_by_role": "r", "requesting_peer": "p", "include_retired": True, "limit": 5, "cursor": "accepted:"},
    }


def test_public_surface() -> None:
    """Bridge parameters, edge retry policy, process documents and return
    schemas all match the implemented verbs."""
    arguments = _bridge_arguments()
    _check(all(_bridge_accepts(verb, arguments[verb]) and _bridge_refuses_unknown(verb) for verb in VERBS), "bridge_registers_every_lifecycle_parameter")
    plugin_class = _plugin_class()
    edges = plugin_class.get_edge_process_definitions(object.__new__(plugin_class))
    _check([edges[verb].error_processor_template_customizations.retryable for verb in VERBS] == [False, False, True, True], "edge_definitions_never_replay_writes")
    documents = [json.loads((PROCESSES / f"{verb}.json").read_text(encoding="utf-8")) for verb in VERBS]
    _check([(doc["process_key"], doc["processor_policy_category"]) for doc in documents] == [(PLUGIN_KEY + verb, "edge") for verb in VERBS], "process_documents_match_registered_keys")
    state = _state()
    recorded = _call(state, "record_accepted_authorization", **arguments["record_accepted_authorization"])
    entry_id = _data(recorded)["entry_id"]
    returned = [recorded, _change(state, entry_id, 1, "correct", notes="m2"), _call(state, "get_authorization_history", entry_id=entry_id), _call(state, "list_held_authorizations")]
    declared = [set(getattr(plugin_class, verb)._platform_process_metadata.return_value_schema.properties) for verb in VERBS]
    _check([set(_data(result)) for result in returned] == declared, "return_schemas_match_returned_keys")
    _check(Path(str(agent_messaging_plugin.__file__)).resolve().is_relative_to(_ROOT), "lane_source_provenance")


def main() -> int:
    test_pending_visible()
    test_record_shape_and_validation()
    test_correction_history()
    test_notes_lifecycle()
    test_stale_and_duplicate()
    test_fire_rules()
    test_withdraw_retire_reopen()
    test_event_failure_rollback()
    test_filters_and_legacy_refusals()
    test_pagination()
    test_public_surface()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
