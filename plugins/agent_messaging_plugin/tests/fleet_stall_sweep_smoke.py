#!/usr/bin/env python3
"""Private-PostgreSQL proof of the work-keyed, runtime-neutral fleet stall sweep.

Run only under the shipped provisioner
(``project-solet/scripts/provision_test_pg.py --exec``); a missing fixture
exits 77 (UNMEASURED), which is never a pass.  The managed-session ledger is a
REAL ledger: a ``PostgresStatePlugin`` applies the real schema through its own
declarative lifecycle, and every row is written by the production writer
``insert_managed_session`` / ``transition_lifecycle_state``, never hand-built.
This module never opens a driver connection or executes SQL.

The register port is REAL as well.  ``PsoletFleetStallReader`` is constructed
on an executable CLI fixture written to disk, invoked as a subprocess by the
reader's own inherited ``_run``, and its stdout parsed by the production
``read_ready_owned_work`` / ``read_produced_work`` with their own code,
including the ``who-has-work`` cursor loop and the named refusals.  Nothing
here monkeypatches ``list_managed_sessions``, stubs a connection, mocks the
reader or hands the predicate a built fact dict: each of those would certify
the shape of the test rather than the behaviour of the sweep.

The fixture's JSON is the shape MEASURED live from the register on
2026-11-03 (``psolet db unit who-has-work --limit 2`` and ``psolet db
activity produced-work --actor <agi id> --since <iso>``), so the production
parser is exercised against the real contract.

Acceptance covered here:

(a) a Codex-only live fleet with work ready and owned and nothing produced in
    the window reports ``stalled`` -- and a Claude-only and a mixed fleet
    report the identical verdict, which is what makes runtime neutrality
    measured rather than asserted;
(b) the same fleet with produced work inside the window reports
    ``not_stalled``, so the signal is output and not liveness;
(c) a fleet with nothing ready and owned reports ``not_stalled`` however idle
    its sessions look;
(d) the sweep's own last completed run is readable afterwards, with its age
    and an explicit silence advisory once that age passes the threshold;
(e) no session-acting call is reachable from the sweep: its module imports and
    its whole call graph are audited for drive/clear/compact/terminate/spawn.

Plus the two the close-out names: a REFUSED register read (verdict
``unmeasured``, never a manufactured stall) and an EMPTY fleet.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/fleet_stall_sweep_smoke.py
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import shlex
import stat
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(_ROOT))
for _source in (
    _ROOT / "ananta/src",
    _ROOT / "plugins/agent_messaging_plugin/src",
    _ROOT / "plugins/postgres_state_management_plugin/src",
    Path(__file__).resolve().parent,
):
    sys.path.insert(0, str(_source))

import postgres_state_management_plugin  # noqa: E402
from ananta.config.core_schemas import CoreSchemaDefinitions  # noqa: E402
from ananta.interfaces.state_management_interface import StateManagementInterface  # noqa: E402
from ananta.services.plugin_schema_service.serialization import to_json  # noqa: E402
from ananta.services.state_service import StateService  # noqa: E402
from postgres_state_management_plugin.plugin import PostgresStatePlugin  # noqa: E402

import agent_messaging_plugin  # noqa: E402
import ananta  # noqa: E402
from agent_messaging_plugin.fleet_stall_sweep import (  # noqa: E402
    SILENCE_THRESHOLD_SECONDS,
    VERDICT_NOT_STALLED,
    VERDICT_STALLED,
    VERDICT_UNMEASURED,
    PsoletFleetStallReader,
    clear_completed_run,
    read_completed_run,
    read_fleet_sessions,
    read_sweep_status,
    run_fleet_stall_sweep,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_LIVE,
    LIFECYCLE_SPAWNING,
    get_session_lifecycle_schema_definition,
)
from agent_messaging_plugin.session_hosts import (  # noqa: E402
    AGENT_RUNTIME_CLAUDE_CODE,
    AGENT_RUNTIME_CODEX,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    transition_lifecycle_state,
)

_passed = 0
_failed: list[str] = []

#: The module whose call graph (e)'s audit walks, and the actions it must not reach.
_SWEEP_SOURCE = _ROOT / "plugins/agent_messaging_plugin/src/agent_messaging_plugin/fleet_stall_sweep.py"
_SESSION_ACTING_NAMES = frozenset(
    {
        "drive_session",
        "clear_session",
        "compact_session",
        "terminate_session",
        "spawn_session",
        "run_rotation",
        "send_text",
        "submit",
        "inject",
        "retire_session",
        "park_session",
        "report_teardown",
        "dispatch_managed_work",
        "provision_role_session",
        "write_to_pane",
    }
)
_SESSION_ACTING_MODULES = frozenset(
    {
        "seat_rotation_helper",
        "seat_idle_watcher",
        "session_lifecycle_control",
        "session_sweep",
        "tmux_driver",
        "iterm2_driver",
        "session_launch_jobs",
        "session_launch_adapters",
        "host_drivers",
        "peer_dispatch",
        "driver_texts",
    }
)


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


def _private_dsn() -> dict[str, str]:
    raw = os.environ.get("PROJECT_SOLET_DESK_TEST_DSN")
    if not raw:
        print("UNMEASURED: private PostgreSQL fixture is missing; run under project-solet/scripts/provision_test_pg.py --exec")
        raise SystemExit(77)
    dsn = dict(word.split("=", 1) for word in shlex.split(raw))
    assert dsn.get("host") == "127.0.0.1" and int(dsn.get("port", "5432")) != 5432, "private_fixture_required"
    assert dsn.get("dbname") == "project_solet_desk_test", "private_fixture_required"
    for module in (ananta, agent_messaging_plugin, postgres_state_management_plugin):
        _check(Path(str(module.__file__)).resolve().is_relative_to(_ROOT), "lane_source_provenance_" + module.__name__)
    return dsn


class Fixture:
    """One real state service over one private schema holding the real ledger."""

    def __init__(self) -> None:
        self.owner: PostgresStatePlugin | None = None
        self.state: StateManagementInterface | None = None

    def open(self) -> StateManagementInterface:
        dsn = _private_dsn()
        physical_schema = "fleet_stall_sweep_test_" + uuid4().hex[:12]
        lifecycle = get_session_lifecycle_schema_definition()
        owner = PostgresStatePlugin()
        self.owner = owner
        # The three SimpleNamespace collaborators are the private-fixture
        # wiring the existing PostgreSQL smokes in this tree use: the vault
        # hands back the fixture password, the plugin manager hands back this
        # one owner, and the schema manager hands back the one installed
        # definition. They are not stand-ins for the code under test -- the
        # state plugin, the state service and the production writers are all
        # real -- so each is cast at its own boundary rather than widened.
        owner.set_vault_service(
            cast(
                Any,
                SimpleNamespace(retrieve=lambda **_kw: {"action_status": "completed", "data": {"value": dsn.get("password", "")}}),
            )
        )
        config: dict[str, object] = {
            "host": dsn["host"],
            "port": int(dsn["port"]),
            "database": dsn["dbname"],
            "user": dsn["user"],
            "pg_schema": physical_schema,
            "pool_size": 3,
        }
        with patch.dict(os.environ, {"SOLET_NAME": "fleet-stall-sweep-fixture"}):
            owner._initialize(config)  # noqa: SLF001
        _check(asyncio.run(owner.start_services()).get("action_status") == "completed", "private_state_owner_started")
        key_values = CoreSchemaDefinitions.get_key_value_store_schema()
        for definition in (lifecycle, key_values):
            owner.install_plugin_schema(definition.namespace, to_json(definition))
        state = StateService(
            plugin_manager=cast(Any, SimpleNamespace(get_plugin=lambda _name, current=owner: current)),
            state_plugin_name="postgres_state_management_plugin",
        )
        state.set_schema_manager(cast(Any, SimpleNamespace(get_schema=lambda _namespace: lifecycle)))
        self.state = state
        # One cast at the boundary: StateService implements the interface
        # structurally but does not declare it, and the production code this
        # smoke drives is typed on the interface. Casting here keeps every
        # call below honestly typed instead of scattering per-call ignores.
        return cast(StateManagementInterface, state)

    def close(self) -> None:
        if self.owner is not None:
            asyncio.run(self.owner.stop_services())


def _seed_session(state: StateManagementInterface, runtime: str, *, live: bool) -> str:
    """One real ledger row, written by the production writer, in a live state."""
    instance = f"agi-{uuid4().hex}"
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=instance,
            lane_id=f"lane-{runtime}-{instance[-8:]}",
            assignment=None,
            work_class="maintenance",
            budget_line="fleet_stall_sweep_fixture",
            host="synthetic",
            provisioning_mode="synthetic_no_process",
            agent_runtime=runtime,
            role_name=f"{runtime}-fixture",
        ),
    )
    if live:
        transition_lifecycle_state(
            state,
            agent_instance_id=instance,
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="fleet_stall_sweep_smoke",
            reason="fixture session reaches live",
        )
    return instance


# --- the executable register CLI fixture ------------------------------------
# Real argv parsing, real exit codes, real JSON on stdout. The production
# reader runs this as a subprocess and parses what it prints.
_CLI_TEMPLATE = """#!/usr/bin/env python3
import json, sys

PAYLOAD = json.loads({payload!r})
argv = sys.argv[1:]
if argv[:3] == ["db", "unit", "who-has-work"]:
    cursor = argv[argv.index("--after") + 1] if "--after" in argv else ""
    pages = PAYLOAD["who_has_work"]
    if pages.get("refuse"):
        sys.stderr.write("project-solet: register unavailable: connection refused\\n")
        sys.exit(2)
    page = pages["pages"].get(cursor)
    if page is None:
        sys.stderr.write("unexpected cursor %r\\n" % cursor)
        sys.exit(3)
    print(json.dumps(page))
    sys.exit(0)
if argv[:3] == ["db", "activity", "produced-work"]:
    actor = argv[argv.index("--actor") + 1]
    since = argv[argv.index("--since") + 1]
    produced = PAYLOAD["produced"]
    if produced.get("refuse"):
        sys.stderr.write("project-solet: produced-work read failed\\n")
        sys.exit(2)
    rows = [r for r in produced["rows"].get(actor, []) if r["occurred_at"] >= since]
    print(json.dumps(rows))
    sys.exit(0)
sys.stderr.write("fixture does not implement %r\\n" % (argv,))
sys.exit(64)
"""


def _write_cli(directory: Path, payload: dict[str, Any]) -> str:
    path = directory / f"psolet_fixture_{uuid4().hex[:8]}"
    path.write_text(_CLI_TEMPLATE.format(payload=json.dumps(payload)), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def _unit_row(unit_id: str, actor: str, coverage: str = "active-owned") -> dict[str, Any]:
    """The MEASURED who-has-work row shape, 2026-11-03."""
    return {
        "actor": actor,
        "brief_document_revision_id": "drv_00000000-0000-0000-0000-000000000000",
        "changes": [],
        "coverage": coverage,
        "kind": "fix",
        "state": "working",
        "state_observed_at": "2026-11-02T12:46:54.801353-07:00",
        "ttl_expires_at": None,
        "ttl_policy": "never_expires",
        "unit_id": unit_id,
        "unit_key": f"fixture-{unit_id[-6:]}",
    }


def _who_page(rows: list[dict[str, Any]], *, truncated: bool = False, next_cursor: str = "") -> dict[str, Any]:
    """The MEASURED who-has-work envelope shape, 2026-11-03."""
    return {
        "consistency": "current lifecycle evaluated on each page; not a snapshot",
        "effective_limit": 100,
        "has_more": truncated,
        "next_cursor": next_cursor,
        "order": "created_at, id ascending",
        "returned_count": len(rows),
        "rows": rows,
        "stale_after_seconds": 86400,
        "total": len(rows),
        "total_count": len(rows),
        "truncated": truncated,
    }


def _produced_row(unit_id: str, occurred_at: str, kind: str = "unit_state_event") -> dict[str, Any]:
    """The MEASURED produced-work row shape, 2026-11-03."""
    return {"event_kind": kind, "id": f"use_{uuid4().hex}", "issue_id": None, "occurred_at": occurred_at, "unit_id": unit_id}


def _reader(cli_path: str) -> PsoletFleetStallReader:
    return PsoletFleetStallReader(cli_path, solet_name="fleet-stall-sweep-fixture", timeout_seconds=30.0)


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """Every string Constant that is a module, class or function docstring."""
    found: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            first = body[0] if body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant) and isinstance(first.value.value, str):
                found.add(id(first.value))
    return found


def _imported_modules(tree: ast.AST) -> set[str]:
    """Every module and name the source imports, dots and leading dots stripped."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names.add((node.module or "").lstrip("."))
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
    return names


def _referenced_names(tree: ast.AST) -> set[str]:
    """Every called name and every attribute the source touches."""
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def _executable_strings(tree: ast.AST) -> set[str]:
    """Every string literal that is NOT a docstring.

    Docstrings in the sweep module are prose ABOUT the Claude-only surfaces it
    deliberately does not touch, so a raw text grep reds on the module's own
    explanation of itself. This reads executable strings only.
    """
    docstrings = _docstring_nodes(tree)
    return {node.value for node in ast.walk(tree) if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docstrings}


# --- (e) the no-action audit ------------------------------------------------
def _session_acting_audit() -> None:
    """Walk the sweep module's AST: no session-acting import, call or attribute."""
    tree = ast.parse(_SWEEP_SOURCE.read_text(encoding="utf-8"))
    imported = _imported_modules(tree)
    referenced = _referenced_names(tree)
    offending_modules = {name for name in imported if name.split(".")[-1] in _SESSION_ACTING_MODULES}
    _check(not offending_modules, f"no_session_acting_import ({sorted(offending_modules)})")
    offending_calls = referenced & _SESSION_ACTING_NAMES
    _check(not offending_calls, f"no_session_acting_call ({sorted(offending_calls)})")
    _check("subprocess" not in imported, "sweep_opens_no_subprocess_of_its_own")
    searchable = referenced | _executable_strings(tree)
    for forbidden in (".claude/sessions", "claude_session_id", "statusUpdatedAt", "sessionId"):
        hits = sorted(text for text in searchable if forbidden in text)
        _check(not hits, f"sweep_never_reads_{forbidden.strip('.').replace('/', '_')} ({hits})")


# --- the fleet-neutrality legs ---------------------------------------------
def _verdict(state: StateManagementInterface, cli: str, *, now: datetime, window: int = 1800, trigger: str = "manual") -> dict[str, Any]:
    return run_fleet_stall_sweep(state, _reader(cli), now=now, trigger=trigger, window_seconds=window)


_RUNTIME_LEGS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("codex_only", (AGENT_RUNTIME_CODEX, AGENT_RUNTIME_CODEX)),
    ("claude_only", (AGENT_RUNTIME_CLAUDE_CODE, AGENT_RUNTIME_CLAUDE_CODE)),
    ("mixed", (AGENT_RUNTIME_CODEX, AGENT_RUNTIME_CLAUDE_CODE)),
)


def _runtime_neutrality(state: StateManagementInterface, workspace: Path, now: datetime, actor: str) -> None:
    """(a) the same stall verdict for a Codex-only, Claude-only and mixed fleet.

    Each leg opens its OWN Fixture, so each runs against a private schema whose
    ledger holds only that leg's own runtimes. One shared ledger would leave the
    Codex rows of the first leg in place for the second, which would then not be
    a Claude-only fleet at all -- the neutrality claim would rest on the leg's
    name rather than on what the ledger holds. The verdict has to come out
    identical three times over three genuinely different real ledgers, and each
    leg asserts the composition of its own.
    """
    stalled_payload = {
        "who_has_work": {"pages": {"": _who_page([_unit_row("unt_fixture_stall", actor)])}},
        "produced": {"rows": {}},
    }
    cli = _write_cli(workspace, stalled_payload)
    for label, runtimes in _RUNTIME_LEGS:
        leg = Fixture()
        try:
            leg_state = leg.open()
            _check(read_fleet_sessions(leg_state) == [], f"a_{label}_starts_on_an_empty_ledger")
            for runtime in runtimes:
                _seed_session(leg_state, runtime, live=True)
            sessions = read_fleet_sessions(leg_state)
            report = _verdict(leg_state, cli, now=now)
            _check(report["verdict"] == VERDICT_STALLED, f"a_{label}_fleet_reports_stalled")
            _check(report["advisories"] == [], f"a_{label}_fleet_verdict_is_measured")
            _check(report["ready_and_owned_count"] == 1 and report["produced_count"] == 0, f"a_{label}_counts")
            observed = {session.agent_runtime for session in sessions}
            _check(observed == set(runtimes), f"a_{label}_ledger_holds_only_its_own_runtimes")
            _check(len(sessions) == len(runtimes), f"a_{label}_ledger_holds_only_its_own_rows")
            expected_counts = {runtime: runtimes.count(runtime) for runtime in set(runtimes)}
            _check(report["runtime_counts"] == expected_counts, f"a_{label}_runtime_counts_reported")
        finally:
            leg.close()
    # Every later leg reads the shared ledger, so it carries real rows of more
    # than one agent_runtime for them, as acceptance (a) requires of the
    # pass-path reads.
    for runtime in (AGENT_RUNTIME_CODEX, AGENT_RUNTIME_CLAUDE_CODE):
        _seed_session(state, runtime, live=True)
    _check(
        {AGENT_RUNTIME_CODEX, AGENT_RUNTIME_CLAUDE_CODE} == {session.agent_runtime for session in read_fleet_sessions(state)},
        "a_ledger_holds_more_than_one_agent_runtime",
    )


def _produced_inside_window(state: StateManagementInterface, workspace: Path, now: datetime, actor: str) -> None:
    """(b) output, not liveness: produced work inside the window is no stall."""
    inside = _iso(now - timedelta(seconds=300))
    outside = _iso(now - timedelta(seconds=7200))
    cli = _write_cli(
        workspace,
        {
            "who_has_work": {"pages": {"": _who_page([_unit_row("unt_fixture_moving", actor)])}},
            "produced": {"rows": {actor: [_produced_row("unt_fixture_moving", inside)]}},
        },
    )
    report = _verdict(state, cli, now=now)
    _check(report["verdict"] == VERDICT_NOT_STALLED, "b_produced_inside_window_is_not_stalled")
    _check(report["produced_count"] == 1, "b_produced_row_was_parsed_by_production_code")
    _check(report["produced"][0]["event_kind"] == "unit_state_event", "b_produced_row_fields_survive_the_parser")
    stale = _write_cli(
        workspace,
        {
            "who_has_work": {"pages": {"": _who_page([_unit_row("unt_fixture_moving", actor)])}},
            "produced": {"rows": {actor: [_produced_row("unt_fixture_moving", outside)]}},
        },
    )
    older = _verdict(state, stale, now=now)
    _check(older["verdict"] == VERDICT_STALLED, "b_produced_outside_window_is_still_a_stall")


def _nothing_ready(state: StateManagementInterface, workspace: Path, now: datetime, actor: str) -> None:
    """(c) an idle fleet with no work to do is not a stall."""
    cli = _write_cli(
        workspace,
        {
            "who_has_work": {"pages": {"": _who_page([_unit_row("unt_fixture_done", actor, coverage="stale")])}},
            "produced": {"rows": {}},
        },
    )
    report = _verdict(state, cli, now=now)
    _check(report["verdict"] == VERDICT_NOT_STALLED, "c_nothing_ready_and_owned_is_not_stalled")
    _check(report["ready_and_owned_count"] == 0, "c_non_active_owned_coverage_is_not_ready_work")
    _check(report["owning_actors"] == [], "c_no_produced_work_read_is_keyed_without_owners")


def _paged_ready_work(state: StateManagementInterface, workspace: Path, now: datetime, actor: str) -> None:
    """The production cursor loop: a truncated page is followed, not truncated."""
    cli = _write_cli(
        workspace,
        {
            "who_has_work": {
                "pages": {
                    "": _who_page([_unit_row("unt_page_one", actor)], truncated=True, next_cursor="cursor-2"),
                    "cursor-2": _who_page([_unit_row("unt_page_two", actor)]),
                }
            },
            "produced": {"rows": {}},
        },
    )
    report = _verdict(state, cli, now=now)
    _check(report["ready_and_owned_count"] == 2, "who_has_work_cursor_is_followed_to_the_end")
    _check(report["verdict"] == VERDICT_STALLED, "paged_ready_work_still_classifies")


def _refused_reads(state: StateManagementInterface, workspace: Path, now: datetime, actor: str) -> None:
    """A refused register read is an advisory and NO verdict, never a stall."""
    ready_refused = _write_cli(workspace, {"who_has_work": {"refuse": True}, "produced": {"rows": {}}})
    report = _verdict(state, ready_refused, now=now)
    _check(report["verdict"] == VERDICT_UNMEASURED, "refused_ready_read_yields_no_verdict")
    _check(any("ready-and-owned" in note for note in report["advisories"]), "refused_ready_read_names_itself")
    _check(report["ready_and_owned_count"] == 0 and report["produced_count"] == 0, "refused_read_invents_no_counts")

    produced_refused = _write_cli(
        workspace,
        {
            "who_has_work": {"pages": {"": _who_page([_unit_row("unt_unreadable", actor)])}},
            "produced": {"refuse": True},
        },
    )
    second = _verdict(state, produced_refused, now=now)
    _check(second["verdict"] == VERDICT_UNMEASURED, "refused_produced_read_yields_no_verdict")
    _check(any("produced-work" in note for note in second["advisories"]), "refused_produced_read_names_itself")
    _check(second["verdict"] != VERDICT_STALLED, "unreadable_register_is_never_read_as_nothing_produced")

    malformed = _write_cli(workspace, {"who_has_work": {"pages": {"": {"rows": "not-a-list"}}}, "produced": {"rows": {}}})
    third = _verdict(state, malformed, now=now)
    _check(third["verdict"] == VERDICT_UNMEASURED, "malformed_register_output_yields_no_verdict")


def _empty_fleet(state: StateManagementInterface, workspace: Path, now: datetime, actor: str) -> None:
    """An empty fleet never changes the verdict: the predicate is output, not population."""
    cli = _write_cli(
        workspace,
        {
            "who_has_work": {"pages": {"": _who_page([_unit_row("unt_orphaned", actor)])}},
            "produced": {"rows": {}},
        },
    )
    empty = Fixture()
    try:
        empty_state = empty.open()
        _check(read_fleet_sessions(empty_state) == [], "empty_fleet_ledger_is_really_empty")
        report = run_fleet_stall_sweep(empty_state, _reader(cli), now=now, trigger="manual")
        _check(report["session_count"] == 0, "empty_fleet_session_count_is_zero")
        _check(report["verdict"] == VERDICT_STALLED, "empty_fleet_with_owned_work_is_still_a_stall")
        _check(report["runtime_counts"] == {}, "empty_fleet_reports_no_runtimes")
    finally:
        empty.close()


def _own_liveness(state: StateManagementInterface, workspace: Path, now: datetime, actor: str) -> None:
    """(d) the sweep's own last completed run is readable, with its silence."""
    cli = _write_cli(
        workspace,
        {
            "who_has_work": {"pages": {"": _who_page([_unit_row("unt_liveness", actor)])}},
            "produced": {"rows": {}},
        },
    )
    clear_completed_run(state)
    fresh = read_sweep_status(state, now=now)
    _check(fresh["last_completed_run"] is None, "d_cleared_run_reads_back_as_never_recorded")
    _check(fresh["advisories"] and not fresh["silent"], "d_never_recorded_is_an_advisory_not_silence")

    report = _verdict(state, cli, now=now, trigger="cron")
    recorded = read_completed_run(state)
    _check(isinstance(recorded, dict) and recorded["verdict"] == report["verdict"], "d_completed_run_is_published")
    _check(recorded is not None and recorded["trigger"] == "cron", "d_published_run_names_its_trigger")

    status = read_sweep_status(state, now=now)
    _check(status["silent"] is False, "d_a_just_completed_run_is_not_silent")
    _check((status["seconds_since_last_completed_run"] or 0.0) < 5.0, "d_age_of_a_fresh_run_is_near_zero")

    later = now + timedelta(seconds=SILENCE_THRESHOLD_SECONDS + 60)
    silent = read_sweep_status(state, now=later)
    _check(silent["silent"] is True, "d_silence_past_the_threshold_is_reported")
    _check(any("silence threshold" in note for note in silent["advisories"]), "d_silence_names_itself")
    _check(silent["last_completed_run"] == recorded, "d_the_stale_verdict_is_still_readable_beside_the_silence")

    advisory_cli = _write_cli(workspace, {"who_has_work": {"refuse": True}, "produced": {"rows": {}}})
    _verdict(state, advisory_cli, now=now + timedelta(seconds=1))
    advisory_run = read_completed_run(state)
    _check(
        advisory_run is not None and advisory_run["verdict"] == VERDICT_UNMEASURED,
        "d_an_advisory_run_publishes_itself_too",
    )


def _run_record_lifecycle(state: StateManagementInterface, workspace: Path, now: datetime, actor: str) -> None:
    """The published run row's whole lifecycle: create, read, correct, retire.

    The terminal action is the one half a column-only create path would leave
    out, so it is measured here against the real key-value store rather than
    asserted: what it removes, what it reports when there is nothing to
    remove, and that the record really is gone afterwards.
    """
    cli = _write_cli(
        workspace,
        {
            "who_has_work": {"pages": {"": _who_page([_unit_row("unt_lifecycle", actor)])}},
            "produced": {"rows": {}},
        },
    )
    created = _verdict(state, cli, now=now, trigger="manual")
    _check(read_completed_run(state) is not None, "lifecycle_create_then_read")

    corrected = _verdict(state, cli, now=now + timedelta(seconds=2), trigger="cron")
    after = read_completed_run(state)
    _check(after is not None and after["trigger"] == "cron", "lifecycle_next_run_corrects_the_record")
    _check(
        created["completed_at"] != corrected["completed_at"] and after is not None and after["completed_at"] == corrected["completed_at"],
        "lifecycle_retention_is_one_latest_row",
    )

    retired = clear_completed_run(state)
    _check(retired["cleared"] is True, "lifecycle_terminal_action_reports_what_it_cleared")
    _check(retired["cleared_run"] == after, "lifecycle_terminal_action_returns_the_removed_record")
    _check(retired["advisories"] == [], "lifecycle_clearing_a_present_record_raises_no_advisory")
    _check(read_completed_run(state) is None, "lifecycle_record_is_really_gone_after_the_terminal_action")

    repeated = clear_completed_run(state)
    _check(repeated["cleared"] is False, "lifecycle_clearing_an_absent_record_is_not_an_error")
    _check(repeated["cleared_run"] is None, "lifecycle_absent_record_returns_nothing")
    _check(bool(repeated["advisories"]), "lifecycle_nothing_to_clear_is_an_advisory")

    reborn = _verdict(state, cli, now=now + timedelta(seconds=4), trigger="cron")
    _check(read_completed_run(state) is not None, "lifecycle_next_fire_republishes_after_a_clear")
    _check(reborn["verdict"] == created["verdict"], "lifecycle_a_clear_does_not_change_the_verdict")


def main() -> int:
    fixture = Fixture()
    now = datetime.now(UTC)
    actor = f"agi-{uuid4().hex}"
    try:
        state = fixture.open()
        with tempfile.TemporaryDirectory(prefix="fleet_stall_sweep_cli_") as raw:
            workspace = Path(raw)
            _session_acting_audit()
            _runtime_neutrality(state, workspace, now, actor)
            _produced_inside_window(state, workspace, now, actor)
            _nothing_ready(state, workspace, now, actor)
            _paged_ready_work(state, workspace, now, actor)
            _refused_reads(state, workspace, now, actor)
            _empty_fleet(state, workspace, now, actor)
            _own_liveness(state, workspace, now, actor)
            _run_record_lifecycle(state, workspace, now, actor)
    finally:
        fixture.close()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    for label in _failed:
        print(f"  FAILED: {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
