#!/usr/bin/env python3
"""Smoke: a role has no class and no legislation, and ordinary role binding still works.

A role was once stamped with an authority-carrying class by a
legislation act (``legislate_role``), and claim, release and retirement once
read it. Every reader went first, then the act, then the column and its
constants. Nothing replaces them: no grant, issuer or authorization verb.
What a role does have is unchanged: one name bound to one session, the
``sys:`` keyspace refused to general claims, a live holder refused retirement,
and an append-only retirement record naming who retired it.

Three legs, kept separate so a pass in one cannot cover for the other:

  (a) absence: the plugin declares no legislation or retirement-authorization
      process and no process takes a class input; the verb module and
      the binding store define none of the act's symbols; the role contract
      module defines no class constant or reserved-primary-name helper; the
      declared ``role`` table has no class column; no knowledge-base
      process document describes the act.
  (b) ordinary behaviour on the in-memory state the sibling smokes use: a first
      claim lands and a second session displaces it, a session holding one role
      is refused a second, ``sys:`` names are refused, a held role re-points to a
      new instance, a release vacates the name, and a registered caller retires
      an unheld role (appended, never rewritten) but not one a live session holds.
  (c) the upgrade, on private PostgreSQL: a ``role`` table installed with the
      predecessor's class column and holding a classed row and its
      binding is upgraded by the real schema manager with the plugin's current
      declaration. Afterwards ``information_schema.columns`` (read through the
      state interface's ``describe_schema``) shows no class column, the
      row and its binding are still there, and a new claim lands.

Leg (c) needs the private PostgreSQL fixture. Without it, (a) and (b) still run
and a failure there exits 1; otherwise the smoke exits 77, the runner's
disclosed SKIP, because (c) was not measured.

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/role_authority_smoke.py
    .venv/bin/python3 quality_gates/provision_test_pg.py --dir <new absolute dir> \
        --port <free non5432 port> --exec -- \
        .venv/bin/python3 plugins/agent_messaging_plugin/tests/role_authority_smoke.py
"""

from __future__ import annotations

import os
import sys
from dataclasses import replace
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from uuid import uuid4

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "postgres_state_management_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface
    from ananta.types.schema_types import SchemaDefinition

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging import role_binding as role_contract  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    HOLDER_KIND_SESSION,
    TABLE_ROLE,
    role_binding_external_id,
)
from ananta.llm.agent_messaging.state_results import require_completed, require_records  # noqa: E402
from ananta.types.column_types import ColumnType  # noqa: E402
from ananta.types.schema_types import ColumnDefinition  # noqa: E402

from agent_messaging_plugin import role_binding_store as store  # noqa: E402
from agent_messaging_plugin import role_claim as role_claim_module  # noqa: E402
from agent_messaging_plugin import schema as plugin_schema  # noqa: E402
from agent_messaging_plugin import session_lifecycle_verbs as lifecycle  # noqa: E402
from agent_messaging_plugin._role_refresh_evidence import RoleRefreshEvidence  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.retired_role_registry import (  # noqa: E402
    RetiredRoleRegistryError,
    RetireRoleCall,
    read_retired_role_registry,
    retire_role_for_caller,
    seed_retired_role_registry,
)
from agent_messaging_plugin.role_claim import (  # noqa: E402
    RoleClaimFailure,
    RoleClaimOrigin,
    RoleClaimResult,
    RoleClaimSuccess,
)
from quality_gates.private_test_pg import MANIFEST_ENV, ledger_fixture, skip_fixture  # noqa: E402

# The names the deleted act was known by: the process, its request type, the store
# writer and the conflict error. No retirement-authorization process replaces it.
_PROCESS = "legislate_role"
_REQUEST = "LegislateRoleRequest"
_WRITER = "legislate_role_class"
_CONFLICT = "RoleClassConflictError"
_NO_REPLACEMENT = ("authorize_role_retirement", "revoke_role_retirement")
_KB_PROCESSES = REPO_ROOT / "plugins" / "agent_messaging_plugin" / "knowledge_base" / "processes"

# The class column, its constants and the reserved-primary-name helper the class
# guarded minting with. Spelled here, not imported, because none of them exists.
_CLASS_COLUMN = "role_class"
_CLASS_SYMBOLS = (
    "COL_ROLE_CLASS",
    "ROLE_CLASSES",
    "ROLE_CLASS_DEFAULT",
    "ROLE_CLASS_PRIMARY",
    "ROLE_CLASS_PRINCIPAL",
    "ROLE_CLASS_PROJECT",
    "ROLE_CLASS_EPHEMERAL",
    "ROLE_CLASS_CHAT",
    "RESERVED_PRIMARY_NAME_SUFFIX",
    "is_reserved_primary_name",
)

# The predecessor's class column as it was declared: TEXT, NOT NULL, default
# 'project'. Only these fields shape DDL; a description does not.
_PREDECESSOR_CLASS_COLUMN = ColumnDefinition(
    type=ColumnType.TEXT,
    not_null=True,
    default="project",
    description="Predecessor role class column, declared before the class was removed.",
)
_PG_FIXTURE_KEY = "messaging_write"

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


def _declared_processes() -> dict[str, frozenset[str]]:
    """Every platform process the plugin class declares, with the names of its inputs."""
    declared: dict[str, frozenset[str]] = {}
    for klass in AgentMessagingPlugin.__mro__:
        for member in vars(klass).values():
            metadata: Any = getattr(member, "_platform_process_metadata", None)
            if metadata is not None:
                declared[str(metadata.name)] = frozenset(str(key) for key in metadata.parameters)
    return declared


def _claim(
    state: StateManagementInterface,
    *,
    name: str,
    agent_instance_id: str,
    agent_session_id: str,
) -> RoleClaimResult:
    return role_claim_module.claim_role_for_session(
        origin=RoleClaimOrigin.MODEL_TURN,
        name=name,
        agent_id="claude_code",
        agent_instance_id=agent_instance_id,
        agent_session_id=agent_session_id,
        session_label=name,
        state_service=state,
        bridge_manager=None,
        peer_registry=None,
        agent_messaging_service=None,
        call_context=None,
    )


def _holder(name: str, agent_instance_id: str, agent_session_id: str) -> store.HolderClaim:
    return store.HolderClaim(
        holder_kind=HOLDER_KIND_SESSION,
        holder_identity={"agent_id": "claude_code", "session_label": name},
        agent_instance_id=agent_instance_id,
        agent_session_id=agent_session_id,
        session_label=name,
    )


def test_the_act_is_absent() -> None:
    """(a) Nothing declares, defines, exports or documents the act, and nothing replaces it."""
    declared = _declared_processes()
    _check(
        {"peer_claim_role", "peer_release_role", "retire_role"} <= set(declared),
        "setup: the process table read holds the claim, release and retire processes it must keep",
    )
    _check(
        _PROCESS not in declared and not hasattr(AgentMessagingPlugin, _PROCESS),
        f"(a) the plugin declares no {_PROCESS} process and has no such method",
    )
    _check(
        not any(name in declared or hasattr(AgentMessagingPlugin, name) for name in _NO_REPLACEMENT),
        f"(a) no retirement-authorization process replaces it ({', '.join(_NO_REPLACEMENT)})",
    )
    _check(
        _PROCESS not in object.__new__(AgentMessagingPlugin).get_edge_process_definitions(),
        "(a) no processor-policy row names it",
    )
    _check(
        not any(hasattr(lifecycle, name) or name in lifecycle.__all__ for name in (_PROCESS, _REQUEST)),
        "(a) the lifecycle verbs define and export neither the verb nor its request type",
    )
    _check(
        not any(hasattr(store, name) or name in store.__all__ for name in (_WRITER, _CONFLICT)),
        "(a) the role binding store defines and exports neither the writer nor the conflict error",
    )
    _check(
        not (_KB_PROCESSES / f"{_PROCESS}.json").exists(),
        "(a) no knowledge-base process document describes the act",
    )


def _defines(name: str) -> bool:
    """True when the role contract or the plugin schema still defines or exports ``name``."""
    return hasattr(role_contract, name) or name in role_contract.__all__ or hasattr(plugin_schema, name)


def test_the_class_is_absent() -> None:
    """(a) No process input, contract symbol or declared column carries a role class."""
    _check(
        not any(_CLASS_COLUMN in inputs for inputs in _declared_processes().values()),
        f"(a) no process takes a {_CLASS_COLUMN} input, so no verb input path can stamp a class",
    )
    present = [name for name in _CLASS_SYMBOLS if _defines(name)]
    _check(
        not present,
        f"(a) the role contract and plugin schema define no class constant or reserved-primary-name helper (present: {present})",
    )
    columns = set(plugin_schema.get_role_schema().columns)
    _check(
        {"role", "origin"} <= columns and _CLASS_COLUMN not in columns,
        f"(a) the declared role table has no {_CLASS_COLUMN} column (declares {sorted(columns)})",
    )


def test_ordinary_claim_and_displacement() -> None:
    """(b) The claim path: a first claim, displacement, one role per session, the sys: keyspace."""
    state = _state()
    first = _claim(state, name="Zzz-Main", agent_instance_id="agi-a", agent_session_id="ases-a")
    _check(
        isinstance(first, RoleClaimSuccess) and first.action == "claimed",
        f"(b) a first claim of a <solet>-Main shape name is an ordinary 'claimed' (got {first!r})",
    )
    second = _claim(state, name="Zzz-Main", agent_instance_id="agi-b", agent_session_id="ases-b")
    _check(
        isinstance(second, RoleClaimSuccess) and second.action == "displaced",
        f"(b) a second, different session displaces the holder rather than coexisting (got {second!r})",
    )
    unrelated = _claim(state, name="Claude-Zeta", agent_instance_id="agi-c", agent_session_id="ases-c")
    _check(
        isinstance(unrelated, RoleClaimSuccess),
        f"setup: a third session claims an unrelated name (got {unrelated!r})",
    )
    second_role = _claim(state, name="Zzz-Main", agent_instance_id="agi-c", agent_session_id="ases-c")
    _check(
        isinstance(second_role, RoleClaimFailure) and second_role.code == "cardinality_conflict",
        f"(b) a session already holding a role is refused a second one (got {second_role!r})",
    )
    system = _claim(state, name="sys:autonomic", agent_instance_id="agi-sys", agent_session_id="ases-sys")
    _check(
        isinstance(system, RoleClaimFailure) and system.code == "system_slot_claim_denied",
        f"(b) a general claim of a sys: name is refused system_slot_claim_denied (got {system!r})",
    )


def test_ordinary_rebind_and_release() -> None:
    """(b) The binding store: a held role re-points to a new instance, a release vacates the name."""
    state = _state()
    store.claim_role_binding_v4(state, name="Architect", claim=_holder("Architect", "agi-old", "ases-r"))
    _check(
        store.resolve_role_binding(state, "Architect").agent_instance_id == "agi-old",
        "setup: the binding resolves to the claiming instance",
    )
    rebound = store.refresh_role_binding_cas(
        state,
        agent_session_id="ases-r",
        new_agent_instance_id="agi-new",
        evidence=RoleRefreshEvidence("agi-old", 123, 123),
    )
    _check(
        rebound == 1 and store.resolve_role_binding(state, "Architect").agent_instance_id == "agi-new",
        f"(b) a held role re-points to the session's new instance (rebound {rebound} binding)",
    )
    released = store.release_role_binding_v4(state, "Architect")
    vacant = False
    try:
        store.resolve_role_binding(state, "Architect")
    except store.RoleBindingVacantError:
        vacant = True
    _check(
        released == {"released": True, "name": "Architect"} and vacant,
        f"(b) a release vacates the name (got {released!r}, vacant={vacant})",
    )


def test_ordinary_retirement() -> None:
    """(b) Retirement: an unheld role retires, a live-held one is refused, rows are only appended."""
    state = _state()
    _check(
        seed_retired_role_registry(state) in {"seeded", "already_seeded"},
        "setup: the retired-role registry carries its seed row",
    )
    base = read_retired_role_registry(state).registry_version
    call = RetireRoleCall(
        caller_instance_id="agi-lane",
        name="Spare-Role",
        reason="fixture",
        authority="iss_fixture",
        successor="",
        retired_at="2026-11-19T00:00:00+00:00",
    )
    first = retire_role_for_caller(state, call, lambda _name: None)
    _check(
        first.role == "Spare-Role" and first.retired_by == "agi-lane; iss_fixture" and first.registry_version == base + 1,
        f"(b) a registered caller retires an unheld role; the row names the caller and the authority (got {first!r})",
    )
    refusal = ""
    try:
        retire_role_for_caller(state, replace(call, name="Held-Role"), lambda _name: "Held-Role (agi-held)")
    except RetiredRoleRegistryError as exc:
        refusal = exc.code
    _check(
        refusal == "role_held_live" and read_retired_role_registry(state).registry_version == base + 1,
        f"(b) a role a live session holds is refused role_held_live and nothing is written (got {refusal!r})",
    )
    second = retire_role_for_caller(state, replace(call, name="Second-Role"), lambda _name: None)
    roles = [row.role for row in read_retired_role_registry(state).roles]
    _check(
        second.registry_version == base + 2 and roles[-2:] == ["Spare-Role", "Second-Role"],
        f"(b) a second retirement is appended after the first, which stays (got {roles!r})",
    )


def _role_model_definition() -> SchemaDefinition:
    """The plugin's own declaration that carries the role table, exactly one of them."""
    plugin = object.__new__(AgentMessagingPlugin)
    carrying = [definition for definition in plugin.get_schema_definitions() if TABLE_ROLE in definition.tables]
    if len(carrying) != 1:
        raise AssertionError(f"expected one plugin schema definition carrying {TABLE_ROLE!r}, found {len(carrying)}")
    return carrying[0]


def _predecessor_definition(current: SchemaDefinition) -> SchemaDefinition:
    """The same declaration with the predecessor's class column on the role table."""
    role = current.tables[TABLE_ROLE]
    columns = {**role.columns, _CLASS_COLUMN: _PREDECESSOR_CLASS_COLUMN}
    return replace(current, tables={**current.tables, TABLE_ROLE: replace(role, columns=columns)})


def _physical_role_columns(state: StateManagementInterface) -> set[str]:
    """The role table's columns as information_schema.columns reports them."""
    described = require_completed(state.describe_schema(AGENT_ROLE_BINDING_NAMESPACE), "describe schema")
    tables = cast("dict[str, dict[str, dict[str, object]]]", described["tables"])
    return set(tables.get(f"{AGENT_ROLE_BINDING_NAMESPACE}__{TABLE_ROLE}", {}).get("columns", {}))


def _role_rows(state: StateManagementInterface, name: str) -> list[dict[str, object]]:
    return require_records(
        state.query_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": TABLE_ROLE, "filters": {"external_id": role_binding_external_id(name)}, "limit": 2},
        )
    )


def _seed_predecessor(state: StateManagementInterface, name: str) -> list[dict[str, object]]:
    """Write a classed role row and its binding into the predecessor table; return the row."""
    require_completed(
        state.upsert_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": TABLE_ROLE,
                "record": {"external_id": role_binding_external_id(name), "role": name, "origin": "user", _CLASS_COLUMN: "primary"},
                "conflict_columns": ["external_id"],
            },
        ),
        "seed a classed predecessor role row",
    )
    store.claim_role_binding_v4(state, name=name, claim=_holder(name, "agi-pg", "ases-pg"))
    rows = _role_rows(state, name)
    _check(
        _CLASS_COLUMN in _physical_role_columns(state) and [row.get(_CLASS_COLUMN) for row in rows] == ["primary"],
        f"setup: the predecessor role table physically has {_CLASS_COLUMN} and the seeded row carries 'primary'",
    )
    _check(
        store.resolve_role_binding(state, name).agent_instance_id == "agi-pg",
        "setup: the seeded role is bound before the upgrade",
    )
    return rows


def _check_upgraded(state: StateManagementInterface, name: str, rows_before: list[dict[str, object]]) -> None:
    """The class column is physically gone; the seeded row, its binding and new writes are fine."""
    physical = _physical_role_columns(state)
    _check(
        {"role", "origin"} <= physical and _CLASS_COLUMN not in physical,
        f"(c) after the upgrade information_schema.columns shows no {_CLASS_COLUMN} column on the role table (has {sorted(physical)})",
    )
    rows_after = _role_rows(state, name)
    _check(
        [(row.get("id"), row.get("role")) for row in rows_after] == [(row.get("id"), name) for row in rows_before],
        f"(c) the seeded role row survives the upgrade with its id (got {rows_after!r})",
    )
    _check(
        store.resolve_role_binding(state, name).agent_instance_id == "agi-pg",
        "(c) the seeded role is still bound to its session after the upgrade",
    )
    later = "Claude-After"
    store.upsert_role_entity(state, name=later)
    store.claim_role_binding_v4(state, name=later, claim=_holder(later, "agi-pg2", "ases-pg2"))
    _check(
        [row.get("role") for row in _role_rows(state, later)] == [later] and store.resolve_role_binding(state, later).agent_instance_id == "agi-pg2",
        "(c) a new role row and claim on the upgraded tables land and resolve",
    )


def test_predecessor_upgrade_on_private_postgres() -> None:
    """(c) The real schema manager drops the predecessor's class column and keeps the row and binding."""
    from ananta.services.schema_manager import SchemaManager

    current = _role_model_definition()
    with ledger_fixture(_PG_FIXTURE_KEY, schema="role_authority_test_" + uuid4().hex[:12]) as fixture:
        service = fixture.state
        state = cast("StateManagementInterface", service)
        before = SchemaManager(service, fixture.plugin)
        before.initialize_schemas([_predecessor_definition(current)])
        service.set_schema_manager(before)
        rows_before = _seed_predecessor(state, "Zzz-Main")

        after = SchemaManager(service, fixture.plugin)
        after.initialize_schemas([current])
        service.set_schema_manager(after)
        _check_upgraded(state, "Zzz-Main", rows_before)
    if not _failed:
        print(f"PRIVATE_PG_BODY {_PG_FIXTURE_KEY} role_authority_predecessor_upgrade")


def _summary() -> int:
    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


def main() -> int:
    test_the_act_is_absent()
    test_the_class_is_absent()
    test_ordinary_claim_and_displacement()
    test_ordinary_rebind_and_release()
    test_ordinary_retirement()
    if not os.environ.get(MANIFEST_ENV):
        if _summary():
            return 1
        return skip_fixture()
    test_predecessor_upgrade_on_private_postgres()
    return _summary()


if __name__ == "__main__":
    raise SystemExit(main())
