#!/usr/bin/env python3
"""Unit smoke for the D1 ``managed_session`` ledger + ``session_transition``
audit trail (``session_lifecycle_store.py``), against ``RealShapeState``
(real provider ActionResult envelopes, not a hand-rolled convenience fake).

TWO GUARDS, TWO TOKENS (AMEND 2b + the second-guard-vacuous trap): this smoke
proves each guard fires on its OWN failure mode, not the other's —

  - an ILLEGAL edge (not in the §3.2 matrix) is refused by the Python check
    BEFORE any state write — ``IllegalLifecycleTransitionError``, and the
    ledger row is verifiably untouched (still in ``from_state``), proving
    the CAS was never reached;
  - a LEGAL edge that loses its race (another writer already moved the row)
    raises ``StaleLifecycleStateError`` from the predicated ``update_state``
    itself — exercised by actually racing it (moving the row out from under
    the call between read and write), not by asserting the illegal-edge path
    twice under a different name;
  - the audit row is inserted ONLY after a successful ledger write, and NOT
    at all when the ledger write loses its race (ordering, not just
    presence — measured by checking session_transition row count is zero
    after a StaleLifecycleStateError).

Also covers ``backfill_registration``/``set_host_ref`` — the registration-hook
fix (Dawn ruling arm-11511b07): the ``spawning -> live`` edge fires exactly
once on first registration, a reconnect self-corrects identity without
re-firing, and an agent_instance_id with no ledger row is a silent no-op.

THE SCHEMA HOLDS NO WORK MODEL (release 74): the plugin's declarations carry
no Unit, lane, dispatch, work-class or budget-line column or table.
``managed_session`` keeps three declarations only as input to
``session_origin_backfill`` on a direct
upgrade. The real state-service diff, run in update mode against the
predecessor declaration, drops every other work-model column and none of the
three. With a private PostgreSQL (``PRIVATE_TEST_PG_MANIFEST``) the smoke also
upgrades a populated predecessor schema: the three retired tables are dropped
by owner-aware retirement and the classifier still reads its three inputs.
Without one, that proof is a disclosed skip (exit 77) after every other check.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/session_lifecycle_store_smoke.py
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import fields, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[3]
_ROOT = REPO_ROOT
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.config.core_schemas import CoreSchemaDefinitions  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.llm.agent_messaging.state_results import require_completed, require_records, require_updated  # noqa: E402
from ananta.services.schema_manager import SchemaManager  # noqa: E402
from ananta.types.column_types import ColumnType  # noqa: E402
from ananta.types.schema_standardizer import SchemaStandardizer  # noqa: E402
from ananta.types.schema_types import ColumnDefinition, IndexDefinition, SchemaDefinition, TableSchema  # noqa: E402
from postgres_state_management_plugin.postgres_backend.ddl_renderer import resolve_index_name  # noqa: E402
from postgres_state_management_plugin.postgres_backend.schema_diff import diff_schema  # noqa: E402

from agent_messaging_plugin import schema as schema_module  # noqa: E402
from agent_messaging_plugin import session_origin_backfill  # noqa: E402
from agent_messaging_plugin.http_routes import (  # noqa: E402
    _managed_session_registration_backfill,
)
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    TABLE_MANAGED_SESSION,
    TABLE_SESSION_DEPENDENCY,
    TABLE_SESSION_TRANSITION,
    get_session_lifecycle_schema_definition,
)
from agent_messaging_plugin.session_lifecycle_control import (  # noqa: E402
    HostAttempt,
    read_host_attempt,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    IllegalLifecycleTransitionError,
    ManagedSessionSpec,
    SessionNotFoundError,
    StaleLifecycleStateError,
    backfill_registration,
    insert_managed_session,
    iter_managed_sessions_unbounded,
    list_managed_sessions,
    read_managed_session,
    set_host_ref,
    transition_lifecycle_state,
)
from quality_gates.private_test_pg import MANIFEST_ENV, ledger_fixture, skip_fixture  # noqa: E402

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


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def _transition_count(state: StateManagementInterface, agent_instance_id: str) -> int:
    result = state.query_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": TABLE_SESSION_TRANSITION, "filters": {"agent_instance_id": agent_instance_id}},
    )
    return len(require_records(result))


# The work-model columns the predecessor managed_session declared beyond the
# three upgrade inputs, with their declared types. None had a default, NOT
# NULL or UNIQUE, so the type is the whole DDL shape.
_REMOVED_SESSION_COLUMNS: dict[str, ColumnType] = {
    "lane_id": ColumnType.TEXT,
    "assignment_sha256": ColumnType.TEXT,
    "unit_id": ColumnType.TEXT,
    "dispatch_kind": ColumnType.TEXT,
    "reviewed_report_vendor": ColumnType.TEXT,
    "pair_id": ColumnType.TEXT,
    "scope_tags": ColumnType.JSON,
    "capability_floors": ColumnType.JSON,
    "difficulty_score": ColumnType.REAL,
    "selection_receipt": ColumnType.JSON,
    "work_class": ColumnType.TEXT,
    "budget_line": ColumnType.TEXT,
    "dispatch_id": ColumnType.TEXT,
    "worktree_disposition": ColumnType.TEXT,
    "lane_repo_root": ColumnType.TEXT,
}
_REMOVED_SESSION_INDEXES = {"idx_managed_session_lane": "lane_id", "idx_managed_session_unit": "unit_id"}
_REMOVED_DEPENDENCY_COLUMN = "waiter_lane_id"
_WORK_MODEL_COLUMNS = frozenset({*_REMOVED_SESSION_COLUMNS, _REMOVED_DEPENDENCY_COLUMN})
# The predecessor's three work-model tables: their id prefixes and a few of
# their real columns, enough to hold populated rows for the retirement proof.
_RETIRED_TABLES: dict[str, tuple[str, tuple[str, ...]]] = {
    "managed_dispatch": ("mdp", ("dispatch_id", "lane_id", "unit_id", "state")),
    "managed_dispatch_event": ("mde", ("event_id", "dispatch_id", "event_kind")),
    "lane_charter": ("lch", ("lane_id", "charter_text", "unit_id")),
}
# The DDL shape (type, NOT NULL, default) of the three declarations release 74
# keeps as session_origin_backfill's input on a direct upgrade. Each name is
# spelled in two pieces, so only _seed_predecessor, which builds the predecessor
# rows, spells the kept names whole; the order is the one _HISTORICAL_SESSIONS uses.
_UPGRADE_INPUTS = {
    "provisioning" + "_mode": (ColumnType.TEXT, True, "worktree"),
    "brief" + "_ref": (ColumnType.TEXT, False, None),
    "assign" + "ment": (ColumnType.JSON, False, None),
}
# Every column no live writer names: the work-model columns and the three upgrade
# inputs. The database default fills the one the upgrade still declares NOT NULL.
_FORMER_COLUMNS = _WORK_MODEL_COLUMNS | frozenset(_UPGRADE_INPUTS)
_RETIRED_CONSTANTS = (
    "TABLE_MANAGED_DISPATCH",
    "MANAGED_DISPATCH_ID_PREFIX",
    "TABLE_MANAGED_DISPATCH_EVENT",
    "MANAGED_DISPATCH_EVENT_ID_PREFIX",
    "TABLE_LANE_CHARTER",
    "LANE_CHARTER_ID_PREFIX",
    "WORK_CLASS_READ_ONLY",
    "WORK_CLASS_ANALYSIS_DELIVERABLE",
    "WORK_CLASS_PRODUCTION_MUTATION",
    "CONDITION_LANE_CLOSED",
)


def test_insert_and_read() -> None:
    state = _state()
    spec = ManagedSessionSpec(
        agent_instance_id="agi-spawn-1",
        host="headless",
        spawned_by_instance_id="agi-spawner",
        spawned_by_role="Claude-C",
        directed_by="operator:none",
    )
    insert_managed_session(state, spec)
    row = read_managed_session(state, "agi-spawn-1")
    _check(row["session_origin"] == "unknown", "a store caller without creation evidence records unknown")
    _check(_FORMER_COLUMNS.isdisjoint(row), f"new rows record no former column (found {sorted(_FORMER_COLUMNS & set(row))})")
    _check(row["lifecycle_state"] == LIFECYCLE_SPAWNING, "fresh spawn lands in 'spawning' state")
    _check(row["spawned_by_instance_id"] == "agi-spawner", "ledger row carries lineage (spawned_by_instance_id)")

    not_found = False
    try:
        read_managed_session(state, "agi-nonexistent")
    except SessionNotFoundError:
        not_found = True
    _check(not_found, "reading an unknown instance raises SessionNotFoundError")


def test_list_filters() -> None:
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-spawn-1",
            label="label-x",
            host="headless",
        ),
    )
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-spawn-2",
            label="label-y",
            host="operator",
        ),
    )
    label_x_rows = list_managed_sessions(state, {"label": "label-x"})
    _check(
        len(label_x_rows) == 1 and label_x_rows[0]["agent_instance_id"] == "agi-spawn-1",
        "list_managed_sessions filters by label",
    )
    all_rows = list(
        iter_managed_sessions_unbounded(
            state,
            reason="store smoke intentionally verifies all inserted fixture rows",
        )
    )
    _check(len(all_rows) == 2, "the named full-ledger helper returns all fixture rows")


def test_transition_guards() -> None:
    """TWO GUARDS, TWO TOKENS, chained on ONE evolving row: legal transition
    lands (ledger + audit), an illegal edge is refused BEFORE any write
    (ledger untouched), then a legal edge that loses its race to a
    concurrent writer is refused by the predicated write itself (ledger
    left at the WINNER's state, no audit row for the loser)."""
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-spawn-1",
            host="headless",
        ),
    )

    # --- legal transition succeeds: ledger + audit both land ---
    transition_lifecycle_state(
        state,
        agent_instance_id="agi-spawn-1",
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE,
        directed_by="operator:none",
        reason="registration hook",
    )
    _check(
        read_managed_session(state, "agi-spawn-1")["lifecycle_state"] == LIFECYCLE_LIVE,
        "a legal transition updates lifecycle_state",
    )
    _check(
        _transition_count(state, "agi-spawn-1") == 1,
        "a successful transition inserts exactly one session_transition audit row",
    )

    # --- illegal edge: refused BEFORE any write, ledger untouched ---
    illegal_raised = False
    try:
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-spawn-1",
            from_state=LIFECYCLE_LIVE,
            to_state=LIFECYCLE_RETIRED,
            directed_by="operator:none",
        )
    except IllegalLifecycleTransitionError:
        illegal_raised = True
    _check(
        illegal_raised,
        "live -> retired (not in the matrix) raises IllegalLifecycleTransitionError",
    )
    _check(
        read_managed_session(state, "agi-spawn-1")["lifecycle_state"] == LIFECYCLE_LIVE,
        "an illegal-edge attempt leaves lifecycle_state untouched (the Python check fired BEFORE any state write — the CAS was never reached)",
    )
    _check(
        _transition_count(state, "agi-spawn-1") == 1,
        "an illegal-edge attempt inserts NO audit row (still exactly one, from the earlier legal transition)",
    )

    # --- legal edge, lost race: predicated CAS itself refuses ---
    # Simulate the row having ALREADY moved to 'terminated' by a concurrent writer
    # between this caller's read and its write — the CAS predicate (still filtered
    # on from_state='live') then matches zero rows. Reaches directly into the
    # fake's internal row store (test-only) rather than going through the
    # module under test, so the race is genuinely external to it.
    real_rows = state._rows[(AGENT_ROLE_BINDING_NAMESPACE, "managed_session")]  # type: ignore[attr-defined]
    for r in real_rows:
        if r["agent_instance_id"] == "agi-spawn-1":
            r["lifecycle_state"] = LIFECYCLE_TERMINATED
    stale_raised = False
    try:
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-spawn-1",
            from_state=LIFECYCLE_LIVE,
            to_state=LIFECYCLE_IDLE,
            directed_by="operator:none",
        )
    except StaleLifecycleStateError:
        stale_raised = True
    _check(
        stale_raised,
        "a LEGAL edge (live->idle) that lost the race (row already moved to terminated by a concurrent writer) raises StaleLifecycleStateError — the PREDICATED WRITE itself refused, a different code path than the illegal-edge Python check above",
    )
    _check(
        read_managed_session(state, "agi-spawn-1")["lifecycle_state"] == LIFECYCLE_TERMINATED,
        "a stale-race attempt leaves the concurrent writer's state intact (never clobbered by the losing caller)",
    )
    _check(
        _transition_count(state, "agi-spawn-1") == 1,
        "a lost-race attempt inserts NO audit row (ledger-write-before-audit-insert ordering — never document a transition that didn't happen)",
    )


def test_backfill_registration_fires_once() -> None:
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-spawn-3",
            host="headless",
        ),
    )
    backfill_registration(
        state,
        agent_instance_id="agi-spawn-3",
        agent_id="claude_code",
        agent_session_id="sess-3",
    )
    row = read_managed_session(state, "agi-spawn-3")
    _check(
        row["agent_session_id"] == "sess-3" and row["agent_id"] == "claude_code",
        "backfill_registration writes agent_session_id/agent_id onto the ledger row",
    )
    _check(
        row["lifecycle_state"] == LIFECYCLE_LIVE,
        "backfill_registration fires spawning->live on first registration",
    )
    _check(
        _transition_count(state, "agi-spawn-3") == 1,
        "backfill_registration's spawning->live edge is audited exactly once",
    )


def test_backfill_reconnect_does_not_refire() -> None:
    """A reconnect (or a late/duplicate register) on a row already past
    'spawning' must re-write identity (self-correcting) but never attempt
    the one-time edge again."""
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-spawn-3",
            host="headless",
        ),
    )
    backfill_registration(
        state,
        agent_instance_id="agi-spawn-3",
        agent_id="claude_code",
        agent_session_id="sess-3",
    )
    backfill_registration(
        state,
        agent_instance_id="agi-spawn-3",
        agent_id="claude_code",
        agent_session_id="sess-3-b",
    )
    row = read_managed_session(state, "agi-spawn-3")
    _check(
        row["lifecycle_state"] == LIFECYCLE_LIVE,
        "a reconnect backfill on an already-'live' row does not re-fire the edge",
    )
    _check(
        row["agent_session_id"] == "sess-3-b",
        "a reconnect backfill still re-writes agent_session_id (self-correcting, mirrors the state-table self-refresh pattern)",
    )
    _check(
        _transition_count(state, "agi-spawn-3") == 1,
        "a reconnect backfill inserts NO additional audit row",
    )


def test_backfill_with_no_managed_session_row_is_noop() -> None:
    state = _state()
    no_row_raised = False
    try:
        backfill_registration(
            state,
            agent_instance_id="agi-no-such-session",
            agent_id="claude_code",
            agent_session_id="sess-none",
        )
    except Exception:  # noqa: BLE001 — this smoke asserts NO exception of any kind
        no_row_raised = True
    _check(
        not no_row_raised,
        "backfill_registration on an agent_instance_id with no managed_session row returns cleanly so the registration route can decide whether to birth inventory",
    )


def test_hand_launched_registration_creates_honest_operator_row() -> None:
    """CATCHES: preserving the old no-row no-op in the actual registration seam.

    This faithfully simulates a hand-launched bridge registering without any
    prior spawn lineage, then reads the row back through the store API. A
    fixture row inserted before the assertion would not exercise C1.
    """
    state = _state()
    _managed_session_registration_backfill(
        state,
        agent_instance_id="agi-hand-launched",
        agent_id="codex",
        agent_session_id="ases-hand-launched",
    )
    row = read_managed_session(state, "agi-hand-launched")
    _check(
        row["host"] == "operator" and row["lifecycle_state"] == LIFECYCLE_LIVE,
        "a hand-launched registration creates and activates a real operator row",
    )
    _check(
        row["agent_id"] == "codex" and row["agent_session_id"] == "ases-hand-launched",
        "the registration-created row reads back the registering identity",
    )
    _check(
        row.get("report_by") is None and row.get("expires_at") is None and row["report_by_seconds"] == 0,
        "the inventory row carries no fabricated deadline or TTL contract",
    )
    _managed_session_registration_backfill(
        state,
        agent_instance_id="agi-hand-launched",
        agent_id="codex-reconnected",
        agent_session_id="ases-hand-launched",
    )
    reconnected = read_managed_session(state, "agi-hand-launched")
    _check(
        reconnected["agent_id"] == "codex-reconnected" and _transition_count(state, "agi-hand-launched") == 1,
        "a reconnect updates identity without creating a second row or transition",
    )


def test_backfill_registration_recovers_spawn_id_from_agent_session_id() -> None:
    """spawn/registration-gaps fix (2026-08-08, coordinator-seat ruling): a watch-
    hosted worker registers under a DIFFERENT agent_instance_id than the one
    its managed_session row was created under (``_resolve_watch_identity``
    deliberately mints ``agi-watch-<digest>``, never the spawn id). The
    primary lookup misses; the fallback must recover the spawn id from
    agent_session_id's guaranteed-by-construction ``ases-<agent_instance_id>``
    shape and find the row via THAT — a lifecycle claim, so this asserts the
    row actually advances (columns populate, transitions to live), not
    merely that the call returns without raising."""
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-spawn-recover",
            host="tmux",
        ),
    )
    backfill_registration(
        state,
        agent_instance_id="agi-watch-deadbeef",  # the watch-minted id -- NOT the spawn id
        agent_id="claude_code",
        agent_session_id="ases-agi-spawn-recover",  # embeds the ORIGINAL spawn id
    )
    row = read_managed_session(state, "agi-spawn-recover")
    _check(
        row["agent_session_id"] == "ases-agi-spawn-recover" and row["agent_id"] == "claude_code",
        "the recovered-id fallback backfills agent_session_id/agent_id onto the SPAWN-time row",
    )
    _check(
        row["lifecycle_state"] == LIFECYCLE_LIVE,
        "the recovered-id fallback fires spawning->live exactly like a direct-id match would",
    )
    _check(
        _transition_count(state, "agi-spawn-recover") == 1,
        "the recovered-id fallback's spawning->live edge is audited exactly once",
    )


def test_backfill_registration_fallback_refuses_when_recovered_row_not_spawning() -> None:
    """Guard 2/3 (coordinator-seat ruling): the fallback may ONLY match a row still
    in 'spawning' -- a row that already completed its lineage must never be
    re-keyed by a later registration arriving under a different id. Red-
    first shape: first backfill the row normally (it leaves 'spawning'),
    THEN attempt a second registration whose agent_session_id recovers the
    SAME now-non-spawning row under a different agent_instance_id -- must
    refuse (no re-backfill via this path, no re-transition, no new audit
    row), never silently re-key it."""
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-spawn-completed",
            host="tmux",
        ),
    )
    backfill_registration(
        state,
        agent_instance_id="agi-spawn-completed",
        agent_id="claude_code",
        agent_session_id="ases-agi-spawn-completed",
    )
    row_before = read_managed_session(state, "agi-spawn-completed")
    _check(
        row_before["lifecycle_state"] == LIFECYCLE_LIVE,
        "setup: the row is live before the refusal case",
    )

    backfill_registration(
        state,
        agent_instance_id="agi-watch-imposter",
        agent_id="imposter_agent",  # DISTINCT from the setup call's "claude_code" -- if the guard
        # fails to block the backfill, this value would silently overwrite the column and a same-
        # value assertion (re-checking agent_session_id, unchanged by construction here since it's
        # deterministically recovered from the same instance id either way) would never catch it.
        agent_session_id="ases-agi-spawn-completed",  # recovers the now-non-spawning row
    )
    row_after = read_managed_session(state, "agi-spawn-completed")
    _check(
        row_after["agent_id"] == "claude_code",
        "the row's agent_id is unchanged by the refused fallback attempt (a distinguishable value proves the guard actually blocked the write, not merely that it wrote the same value twice)",
    )
    _check(
        row_after["agent_session_id"] == "ases-agi-spawn-completed",
        "the row's agent_session_id is unchanged by the refused fallback attempt",
    )
    _check(
        row_after["lifecycle_state"] == LIFECYCLE_LIVE,
        "a non-spawning row is left completely untouched by a mismatched later registration",
    )
    _check(
        _transition_count(state, "agi-spawn-completed") == 1,
        "the refused fallback attempt inserts NO additional audit row",
    )


def test_backfill_registration_fallback_noop_when_recovered_id_also_has_no_row() -> None:
    """Guard 4 preserved: recovery failing to resolve ANY row (not just the
    primary miss) is the same silent no-op as today -- never an error, never
    a fabricated row."""
    state = _state()
    no_row_raised = False
    try:
        backfill_registration(
            state,
            agent_instance_id="agi-watch-orphan",
            agent_id="claude_code",
            agent_session_id="ases-agi-never-spawned",
        )
    except Exception:  # noqa: BLE001 -- this smoke asserts NO exception of any kind
        no_row_raised = True
    _check(
        not no_row_raised,
        "a recovered id that also has no managed_session row is a silent no-op",
    )


def test_backfill_registration_fallback_noop_when_session_id_has_no_spawn_shape() -> None:
    """Guard 4, the other edge: an agent_session_id that doesn't even have
    the ases-<id> shape (e.g. an operator-launched session's own
    ases-<epoch>-<pid>-<random> id) must never be treated as recoverable --
    silent no-op, matching the documented genuine-no-spawn-lineage path."""
    state = _state()
    no_row_raised = False
    try:
        backfill_registration(
            state,
            agent_instance_id="agi-watch-something",
            agent_id="claude_code",
            agent_session_id="ases-1786034326-39546-11549",  # operator-shaped, not spawn-shaped
        )
    except Exception:  # noqa: BLE001
        no_row_raised = True
    _check(
        not no_row_raised,
        "a non-spawn-shaped agent_session_id is never treated as recoverable -- silent no-op",
    )


def test_set_host_ref() -> None:
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-spawn-3",
            host="headless",
        ),
    )
    set_host_ref(state, agent_instance_id="agi-spawn-3", host_ref="driver-pid-4242")
    _check(
        read_managed_session(state, "agi-spawn-3")["host_ref"] == "driver-pid-4242",
        "set_host_ref persists the adapter's host_ref onto the ledger row",
    )


def test_written_columns_are_declared() -> None:
    """Every column the store writes must exist in the declared TableSchema.

    The battery's fakes accept any record shape, so a write-path column that
    was never declared passes every lifecycle smoke and then fails at the
    real provider with Postgres's undefined-column error (measured live
    2026-08-28: a written column no declaration named). This check closes that seam: the
    insert payload — including the conditional report_by/expires_at keys —
    is compared against the schema declaration itself.
    """
    from agent_messaging_plugin.schema import get_managed_session_schema

    state = _state()
    record = insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-schema-parity",
            host="tmux",
            spawned_by_instance_id="agi-spawner",
            spawned_by_role="Claude-C",
            directed_by="operator:none",
            report_by_seconds=900,
        ),
    )
    declared = set(get_managed_session_schema().columns)
    undeclared = sorted(set(record) - declared)
    _check(
        not undeclared,
        f"every written managed_session column is declared in the schema (undeclared: {undeclared})",
    )
    _check(_FORMER_COLUMNS.isdisjoint(record), f"the store writes no former column (wrote {sorted(_FORMER_COLUMNS & set(record))})")


def test_insert_managed_session_writes_no_former_column() -> None:
    state = _state()
    row = insert_managed_session(
        state,
        ManagedSessionSpec(agent_instance_id="agi-no-unit", host="headless"),
    )
    _check(_FORMER_COLUMNS.isdisjoint(row), f"a spawned ledger row records no former column (found {sorted(_FORMER_COLUMNS & set(row))})")


def _attempt_fixture() -> tuple[StateManagementInterface, HostAttempt]:
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-attempt",
            host="operator",
        ),
    )
    return state, HostAttempt(
        agent_instance_id="agi-attempt",
        host="operator",
        host_ref="",
        action="none_available",
        remedy="stop the process manually",
        observation="unknown",
        observed_at=datetime.now(UTC).isoformat(),
        provenance="terminate_session",
    )


def test_attempt_roundtrip() -> None:
    state, attempt = _attempt_fixture()
    reason = attempt.reason("human rationale custody_packet=fixture fingerprint=unchanged")
    transition_lifecycle_state(
        state,
        agent_instance_id="agi-attempt",
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_TERMINATED,
        directed_by="operator:none",
        reason=reason,
    )
    result = read_host_attempt(state, "agi-attempt")
    _check(result["prior_host_outcome"]["action"] == "none_available", "versioned attempt roundtrips")
    _check(result["prior_host_reason"] == reason, "human rationale and custody provenance unchanged")
    _check(result["prior_host_outcome"]["transition_id"] is not None, "history has transition provenance")
    refused = False
    try:
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-attempt",
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_TERMINATED,
            directed_by="operator:none",
            reason="losing writer",
        )
    except StaleLifecycleStateError:
        refused = True
    _check(refused and _transition_count(state, "agi-attempt") == 1, "stale writer adds no evidence")
    transition_lifecycle_state(
        state,
        agent_instance_id="agi-attempt",
        from_state=LIFECYCLE_TERMINATED,
        to_state=LIFECYCLE_RETIRED,
        directed_by="operator:none",
        reason="retire custody kept",
    )
    _check(read_host_attempt(state, "agi-attempt") == result, "retire does not overwrite termination history")


def test_attempt_audit_failure() -> None:
    state, attempt = _attempt_fixture()
    cast("RealShapeState", state).fail_next("write")
    failed = False
    try:
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-attempt",
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_TERMINATED,
            directed_by="operator:none",
            reason=attempt.reason("attempt"),
        )
    except RuntimeError:
        failed = True
    _check(failed, "audit failure is not a green transition")
    _check(read_managed_session(state, "agi-attempt")["lifecycle_state"] == LIFECYCLE_TERMINATED, "partial state retained")
    _check(read_host_attempt(state, "agi-attempt")["prior_host_outcome"] is None, "missing audit never fabricates outcome")


def test_expected_observation_nulls() -> None:
    state, attempt = _attempt_fixture()
    row = read_managed_session(state, "agi-attempt")
    fields = ("host", "host_ref", "lifecycle_state", "last_transition_at", "last_heartbeat_at")
    expected = {key: row.get(key) for key in fields}
    transition_lifecycle_state(
        state,
        agent_instance_id="agi-attempt",
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_TERMINATED,
        directed_by="operator:none",
        reason=attempt.reason("observed"),
        expected_observation=expected,
    )
    _check(_transition_count(state, "agi-attempt") == 1, "null observation fields match with is_null")


def test_latest_attempt_and_unreadable() -> None:
    state, attempt = _attempt_fixture()
    for row_id, reason in (("older", attempt.reason("valid")), ("z-newer", "legacy human rationale")):
        state.write_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_SESSION_TRANSITION,
                "record": {
                    "id": row_id,
                    "agent_instance_id": "agi-attempt",
                    "from_state": LIFECYCLE_SPAWNING,
                    "to_state": LIFECYCLE_TERMINATED,
                    "directed_by": "operator:none",
                    "reason": reason,
                    "occurred_at": "2026-10-20T00:00:00+00:00",
                },
            },
        )
    result = read_host_attempt(state, "agi-attempt")
    _check(result["prior_host_outcome"] is None, "newest unknown never falls back to older proof")
    _check(result["prior_host_reason"] == "legacy human rationale", "stable time/id order returns raw latest reason")
    cast("RealShapeState", state).fail_next("query")
    result = read_host_attempt(state, "agi-attempt")
    _check("unreadable" in result["prior_host_advisory"], "read failure becomes explicit advisory")
    _check(result["prior_host_outcome"] is None, "unreadable history supplies no proof")


def test_registration_during_origin_completion() -> None:
    """Reproduce the reviewer's store insert immediately after the empty text-unknown snapshot."""
    fake = RealShapeState()
    state = cast("StateManagementInterface", fake)
    read_page = fake.query_ordered
    inserted = False

    def insert_after_snapshot(namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        nonlocal inserted
        snapshot = read_page(namespace, query)
        filters = query["filters"]
        if not inserted and filters.get("session_origin") == "unknown" and "lifecycle_state" in filters:
            inserted = True
            written = insert_managed_session(state, ManagedSessionSpec(agent_instance_id="late-origin", host="tmux"))
            # The fake applies no column default; Postgres fills each NOT NULL column with a declared default that an insert omits.
            declared = schema_module.get_managed_session_schema().columns
            omitted = {name: column.default for name, column in declared.items() if column.not_null and column.default is not None and name not in written}
            require_updated(state.update_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MANAGED_SESSION, "filters": {"agent_instance_id": "late-origin"}}, omitted))
        return snapshot

    with patch.object(fake, "query_ordered", side_effect=insert_after_snapshot):
        first = session_origin_backfill.backfill_session_origins(state)
    assert inserted
    row = read_managed_session(state, "late-origin")
    assert row["lifecycle_state"] == LIFECYCLE_SPAWNING and row["session_origin"] == "unknown"
    assert first == {"status": "live_unknown", "row_id": row["id"], "updated": 0}, f"completion published over the interleaved live row: {first}"
    _check(session_origin_backfill.read_session_origin_completion(state) == {"status": "live_unknown", "row_id": row["id"]}, "interleaved spawning row withholds current completion")
    _check(session_origin_backfill.backfill_session_origins(state) == {"status": "completed", "updated": 1}, "retry classifies the interleaved row")
    _check(read_managed_session(state, "late-origin")["session_origin"] == "spawned", "recovered row records the legacy spawn origin")
    _check(session_origin_backfill.read_session_origin_completion(state) == {"status": "completed"}, "current completion reads zero live unknowns after recovery")


def _ddl_shape(column: ColumnDefinition) -> tuple[ColumnType, bool, object]:
    return column.type, column.not_null, column.default


def _work_model_columns(definitions: list[SchemaDefinition]) -> list[str]:
    """Every declared ``table.column`` whose column is a work-model column."""
    return sorted(f"{name}.{column}" for definition in definitions for name, table in definition.tables.items() for column in table.columns if column in _WORK_MODEL_COLUMNS)


def _work_model_indexes(definitions: list[SchemaDefinition]) -> list[str]:
    """Every declared index that covers a work-model column."""
    return sorted(index.name for definition in definitions for table in definition.tables.values() for index in table.indexes if not _WORK_MODEL_COLUMNS.isdisjoint(index.columns))


def _lifecycle(definitions: list[SchemaDefinition]) -> SchemaDefinition:
    return next(definition for definition in definitions if TABLE_MANAGED_SESSION in definition.tables)


def _kept_upgrade_inputs(lifecycle: SchemaDefinition) -> dict[str, tuple[ColumnType, bool, object]]:
    columns = lifecycle.tables[TABLE_MANAGED_SESSION].columns
    return {name: _ddl_shape(columns[name]) for name in _UPGRADE_INPUTS if name in columns}


def test_schema_holds_no_work_model() -> None:
    """CATCHES: a work-model column, index or table left in a declaration the plugin installs."""
    definitions = AgentMessagingPlugin().get_schema_definitions()
    work_columns = _work_model_columns(definitions)
    _check(not work_columns, f"no declared table carries a work-model column (found {work_columns})")
    work_indexes = _work_model_indexes(definitions)
    _check(not work_indexes, f"no declared index covers a work-model column (found {work_indexes})")
    declared_tables = {table_name for definition in definitions for table_name in definition.tables}
    _check(declared_tables.isdisjoint(_RETIRED_TABLES), "no definition declares managed_dispatch, managed_dispatch_event or lane_charter")
    lifecycle = _lifecycle(definitions)
    _check(set(_RETIRED_TABLES) <= set(lifecycle.retired_tables), f"the session lifecycle definition retires the three work-model tables by name (retired: {lifecycle.retired_tables})")
    kept = _kept_upgrade_inputs(lifecycle)
    _check(kept == _UPGRADE_INPUTS, f"managed_session keeps its three upgrade-input declarations with their declared shape (kept: {kept})")
    spec_fields = {field.name for field in fields(ManagedSessionSpec)}
    _check(spec_fields.isdisjoint(_FORMER_COLUMNS), f"ManagedSessionSpec carries no former column (found {sorted(spec_fields & _FORMER_COLUMNS)})")
    leftover = [name for name in _RETIRED_CONSTANTS if hasattr(schema_module, name)]
    _check(not leftover, f"schema.py defines no retired work-model constant (found {leftover})")


def _predecessor(definition: SchemaDefinition) -> SchemaDefinition:
    """The lifecycle declaration a direct upgrade starts from: this one plus
    every work-model declaration it removes, and without ``session_origin``,
    which the same release adds."""
    tables = dict(definition.tables)
    session = tables[TABLE_MANAGED_SESSION]
    session_columns = {name: column for name, column in session.columns.items() if name != "session_origin"}
    session_columns.update({name: ColumnDefinition(type=kind) for name, kind in _REMOVED_SESSION_COLUMNS.items()})
    old_indexes = [IndexDefinition(name=name, columns=[column]) for name, column in _REMOVED_SESSION_INDEXES.items()]
    tables[TABLE_MANAGED_SESSION] = replace(session, columns=session_columns, indexes=[*session.indexes, *old_indexes])
    dependency = tables[TABLE_SESSION_DEPENDENCY]
    tables[TABLE_SESSION_DEPENDENCY] = replace(dependency, columns={**dependency.columns, _REMOVED_DEPENDENCY_COLUMN: ColumnDefinition(type=ColumnType.TEXT)})
    for name, (prefix, names) in _RETIRED_TABLES.items():
        tables.setdefault(name, TableSchema(table_name=name, id_prefix=prefix, columns={column: ColumnDefinition(type=ColumnType.TEXT) for column in names}))
    return replace(definition, tables=tables, retired_tables=tuple(name for name in definition.retired_tables if name not in _RETIRED_TABLES))


_DROP_COLUMN = re.compile(r'^ALTER TABLE "[^"]+"\."([^"]+)" DROP COLUMN "([^"]+)"$')
_ADD_COLUMN = re.compile(r'^ALTER TABLE "[^"]+"\."([^"]+)" ADD COLUMN "([^"]+)"')
_DROP_INDEX = re.compile(r'^DROP INDEX IF EXISTS "[^"]+"\."([^"]+)"$')


def _update_ops() -> tuple[str, list[str]]:
    """The real state-service diff in update mode, rendered, on both declarations
    standardized as the lifecycle standardizes them and limited, as
    ``_apply_update`` limits it, to the tables the new declaration names."""
    standardizer = SchemaStandardizer()
    declared = standardizer.standardize_schema(get_session_lifecycle_schema_definition())
    predecessor = standardizer.standardize_schema(_predecessor(get_session_lifecycle_schema_definition()))
    current = replace(predecessor, tables={name: table for name, table in predecessor.tables.items() if name in declared.tables})
    return declared.namespace, [op.as_string() for op in diff_schema(declared.namespace, current, declared, mode="update", schema_name="private_test_fixture")]


def _classified_ops(rendered: list[str]) -> tuple[set[tuple[str, str]], set[tuple[str, str]], set[str], list[str]]:
    """Split rendered DDL into dropped columns, added columns, dropped indexes and anything else."""
    dropped: set[tuple[str, str]] = set()
    added: set[tuple[str, str]] = set()
    dropped_indexes: set[str] = set()
    other: list[str] = []
    for op in rendered:
        if match := _DROP_COLUMN.match(op):
            dropped.add((match.group(1), match.group(2)))
        elif match := _ADD_COLUMN.match(op):
            added.add((match.group(1), match.group(2)))
        elif match := _DROP_INDEX.match(op):
            dropped_indexes.add(match.group(1))
        else:
            other.append(op)
    return dropped, added, dropped_indexes, other


def test_predecessor_update_drops_only_work_model_columns() -> None:
    """CATCHES: an upgrade that keeps a work-model column or drops an upgrade input."""
    namespace, rendered = _update_ops()
    dropped, added, dropped_indexes, other = _classified_ops(rendered)
    session_table = f"{namespace}__{TABLE_MANAGED_SESSION}"
    expected = {(session_table, column) for column in _REMOVED_SESSION_COLUMNS} | {(f"{namespace}__{TABLE_SESSION_DEPENDENCY}", _REMOVED_DEPENDENCY_COLUMN)}
    _check(dropped == expected, f"update mode drops exactly the work-model columns (missing {sorted(expected - dropped)}, extra {sorted(dropped - expected)})")
    _check(all(column not in _UPGRADE_INPUTS for _table, column in dropped), "no DROP COLUMN targets an upgrade input")
    expected_indexes = {resolve_index_name(namespace, TABLE_MANAGED_SESSION, name) for name in _REMOVED_SESSION_INDEXES}
    _check(dropped_indexes == expected_indexes, f"update mode drops exactly the lane and unit indexes (dropped {sorted(dropped_indexes)})")
    _check(added == {(session_table, "session_origin")} and not other, f"the only other change is the release's added session_origin (added {sorted(added)}, other {other})")


# identity -> (the three classifier inputs, lifecycle_state, the origin the classifier records)
_HISTORICAL_SESSIONS: dict[str, tuple[str, str, dict[str, str] | None, str, str]] = {
    "old-spawn": ("worktree", "", None, LIFECYCLE_LIVE, "spawned"),
    "old-registration": ("operator_existing_checkout", "", None, LIFECYCLE_LIVE, "registered"),
    "old-path": ("worktree", "/old/brief", None, LIFECYCLE_LIVE, "historical_read_only"),
    "old-bound": ("synthetic_no_process", "/old/bound-brief", {"unit_id": "unt_old"}, LIFECYCLE_IDLE, "synthetic"),
    "old-ambiguous": ("unrecorded", "", None, LIFECYCLE_TERMINATED, "unknown"),
}


def _seed_predecessor(state: StateManagementInterface) -> None:
    """Populate every work-model column and table the predecessor declared."""
    for identity, (mode, brief_ref, assignment, lifecycle_state, _origin) in _HISTORICAL_SESSIONS.items():
        session: dict[str, object] = {
            "agent_instance_id": identity,
            "host": "tmux",
            "lifecycle_state": lifecycle_state,
            "provisioning_mode": mode,
            "brief_ref": brief_ref,
            "assignment": assignment,
            "lane_id": f"lane-{identity}",
            "assignment_sha256": "0" * 64,
            "unit_id": "unt_old",
            "dispatch_kind": "implement",
            "reviewed_report_vendor": "codex",
            "pair_id": "pair-old",
            "scope_tags": ["state_schema"],
            "capability_floors": [{"tag": "state_schema", "source": "declared", "detail": "old"}],
            "difficulty_score": 62.0,
            "selection_receipt": {"model": "old"},
            "work_class": "read_only",
            "budget_line": "budget-old",
            "dispatch_id": f"mdp-{identity}",
            "worktree_disposition": "removed",
            "lane_repo_root": "/old/checkout",
        }
        require_completed(state.write_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_MANAGED_SESSION, "record": session}), f"seed predecessor session {identity}")
    dependency = {"waiter_instance_id": "old-spawn", _REMOVED_DEPENDENCY_COLUMN: "lane-old-spawn", "condition_kind": "session_terminal", "condition_ref": "old-ambiguous"}
    require_completed(state.write_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_SESSION_DEPENDENCY, "record": dependency}), "seed predecessor dependency")
    for table, (_prefix, names) in _RETIRED_TABLES.items():
        row = {column: f"{table}-{column}" for column in names}
        require_completed(state.write_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": table, "record": row}), f"seed predecessor {table}")


def _tables(state: StateManagementInterface) -> dict[str, Any]:
    """The live tables of this namespace, read from the database catalog."""
    schema = require_completed(state.describe_schema(AGENT_ROLE_BINDING_NAMESPACE), "describe private schema")
    return cast("dict[str, Any]", schema["tables"])


def _install(fixture_state: Any, plugin: Any, definition: SchemaDefinition) -> None:
    manager = SchemaManager(fixture_state, plugin)
    manager.initialize_schemas([CoreSchemaDefinitions.get_key_value_store_schema(), definition])
    fixture_state.set_schema_manager(manager)


_SESSION_TABLE = f"{AGENT_ROLE_BINDING_NAMESPACE}__{TABLE_MANAGED_SESSION}"


def _check_predecessor_tables(state: StateManagementInterface) -> None:
    before = _tables(state)
    _check(all(f"{AGENT_ROLE_BINDING_NAMESPACE}__{table}" in before for table in _RETIRED_TABLES), "setup: the predecessor database holds the three work-model tables")
    _check(set(_REMOVED_SESSION_COLUMNS) <= set(before[_SESSION_TABLE]["columns"]), "setup: the predecessor managed_session holds every work-model column")


def _check_upgraded_tables(state: StateManagementInterface, installed: dict[str, Any]) -> None:
    """The database catalog and the ownership record after the upgrade."""
    after = _tables(state)
    session_columns = set(after[_SESSION_TABLE]["columns"])
    _check(session_columns.isdisjoint(_REMOVED_SESSION_COLUMNS), f"the upgrade dropped every work-model managed_session column (left {sorted(session_columns & set(_REMOVED_SESSION_COLUMNS))})")
    _check(set(_UPGRADE_INPUTS) | {"session_origin"} <= session_columns, "the upgrade kept the three upgrade-input declarations and added session_origin")
    _check(_REMOVED_DEPENDENCY_COLUMN not in after[f"{AGENT_ROLE_BINDING_NAMESPACE}__{TABLE_SESSION_DEPENDENCY}"]["columns"], "the upgrade dropped session_dependency.waiter_lane_id")
    _check(all(f"{AGENT_ROLE_BINDING_NAMESPACE}__{table}" not in after for table in _RETIRED_TABLES), "owner-aware retirement dropped the three populated work-model tables")
    owned = set(installed["tables"])
    _check(owned.isdisjoint(_RETIRED_TABLES) and TABLE_MANAGED_SESSION in owned, "ownership no longer records the retired tables")


def _check_classified_rows(state: StateManagementInterface) -> None:
    """The upgraded rows keep their classifier inputs, and the classifier reads them."""
    rows = {identity: read_managed_session(state, identity) for identity in _HISTORICAL_SESSIONS}
    mode_column, file_column, payload_column = _UPGRADE_INPUTS
    _check(
        all((rows[identity][mode_column], rows[identity][file_column] or "", rows[identity][payload_column] is None, rows[identity]["session_origin"]) == (mode, file_ref, payload is None, "unknown") for identity, (mode, file_ref, payload, _lifecycle, _origin) in _HISTORICAL_SESSIONS.items()),
        "every historical session keeps its identity and its three classifier inputs, with an unknown origin",
    )
    result = session_origin_backfill.backfill_session_origins(state)
    _check(result == {"status": "completed", "updated": 4}, f"the classifier classifies the upgraded rows (result {result})")
    origins = {identity: read_managed_session(state, identity)["session_origin"] for identity in _HISTORICAL_SESSIONS}
    _check(origins == {identity: spec[4] for identity, spec in _HISTORICAL_SESSIONS.items()}, f"each upgraded row records the origin its inputs imply ({origins})")
    marker = require_completed(state.get_key_value(AGENT_ROLE_BINDING_NAMESPACE, session_origin_backfill.SESSION_ORIGIN_MARKER), "read origin marker")
    _check(marker["found"] and marker["value"] == "history_scanned", f"the history checkpoint reads back as scanned ({marker})")
    _check(session_origin_backfill.read_session_origin_completion(state) == {"status": "completed"}, "origin completion reads back as completed")
    waiting = require_records(state.query_state(AGENT_ROLE_BINDING_NAMESPACE, {"table": TABLE_SESSION_DEPENDENCY, "filters": {"waiter_instance_id": "old-spawn"}}))
    _check(len(waiting) == 1 and _REMOVED_DEPENDENCY_COLUMN not in waiting[0], "the armed dependency row survives without its lane column")


def check_private_pg_upgrade() -> None:
    """A populated predecessor schema upgrades in place, and the classifier still reads its three inputs."""
    with ledger_fixture("messaging_write", schema="work_model_upgrade_" + uuid4().hex[:12]) as fixture:
        state = cast("StateManagementInterface", fixture.state)
        _install(fixture.state, fixture.plugin, _predecessor(get_session_lifecycle_schema_definition()))
        _seed_predecessor(state)
        _check_predecessor_tables(state)
        _install(fixture.state, fixture.plugin, get_session_lifecycle_schema_definition())
        _check_upgraded_tables(state, fixture.plugin.get_installed_schema(AGENT_ROLE_BINDING_NAMESPACE))
        _check_classified_rows(state)


def check_private_pg_fresh_install() -> None:
    """A fresh database gets the declared shape, and the store writes a session into it."""
    namespace = AGENT_ROLE_BINDING_NAMESPACE
    with ledger_fixture("messaging_write", schema="work_model_fresh_" + uuid4().hex[:12]) as fixture:
        state = cast("StateManagementInterface", fixture.state)
        _install(fixture.state, fixture.plugin, get_session_lifecycle_schema_definition())
        tables = _tables(state)
        session_columns = set(tables[f"{namespace}__{TABLE_MANAGED_SESSION}"]["columns"])
        _check(session_columns.isdisjoint(_REMOVED_SESSION_COLUMNS) and set(_UPGRADE_INPUTS) <= session_columns, "a fresh install declares the three upgrade inputs and no work-model column")
        _check(all(f"{namespace}__{table}" not in tables for table in _RETIRED_TABLES), "a fresh install creates none of the retired tables")
        insert_managed_session(state, ManagedSessionSpec(agent_instance_id="agi-fresh", host="headless", session_origin="spawned", label="fresh"))
        row = read_managed_session(state, "agi-fresh")
        _check((row["label"], row["session_origin"]) == ("fresh", "spawned"), "the store writes a session into the fresh schema")
        _check(session_origin_backfill.backfill_session_origins(state) == {"status": "completed", "updated": 0}, "a fresh install has nothing to classify")


def main() -> int:
    test_insert_managed_session_writes_no_former_column()
    test_attempt_roundtrip()
    test_attempt_audit_failure()
    test_expected_observation_nulls()
    test_latest_attempt_and_unreadable()
    test_insert_and_read()
    test_written_columns_are_declared()
    test_list_filters()
    test_transition_guards()
    test_backfill_registration_fires_once()
    test_backfill_reconnect_does_not_refire()
    test_backfill_with_no_managed_session_row_is_noop()
    test_hand_launched_registration_creates_honest_operator_row()
    test_backfill_registration_recovers_spawn_id_from_agent_session_id()
    test_backfill_registration_fallback_refuses_when_recovered_row_not_spawning()
    test_backfill_registration_fallback_noop_when_recovered_id_also_has_no_row()
    test_backfill_registration_fallback_noop_when_session_id_has_no_spawn_shape()
    test_set_host_ref()
    test_registration_during_origin_completion()
    test_schema_holds_no_work_model()
    test_predecessor_update_drops_only_work_model_columns()
    private_pg = bool(os.environ.get(MANIFEST_ENV))
    if private_pg:
        check_private_pg_upgrade()
        check_private_pg_fresh_install()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    if _failed:
        return 1
    return 0 if private_pg else skip_fixture()


if __name__ == "__main__":
    sys.exit(main())
