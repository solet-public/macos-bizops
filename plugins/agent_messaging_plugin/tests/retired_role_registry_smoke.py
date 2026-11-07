#!/usr/bin/env python3
"""The retired-role registry refuses retired names at every role transport (iss_d9c955a2).

Offline: a real-shape state fake, the real plugin verbs, the real HTTP route
helper, the real streamable tool and the real claim body. The fixture mirrors
the live defect: ``Register-Lead`` still has a (stale) role binding, so on base
a send to it resolves and is persisted as ``queued_for_replay`` — silently lost.

Checks, each named so a mutant names the check that kills it:

  R1  seed: the migration seed retires Register-Lead at version 1, idempotently.
  R2  send: peer_send_by_name to Register-Lead is refused with peer_role_retired,
      the message names the reason, and NO role message row is persisted.
  R3  send: a case-folded spelling is refused the same way.
  R4  send: a live role is unchanged (succeeds and persists one row).
  R5  claim: peer_claim_role of Register-Lead is refused; no binding is written.
  R6  HTTP and streamable send-by-name refuse Register-Lead with the same code.
  R7  version: retire_role of a second fixture role bumps registry_version 1 -> 2.
  R8  second role: a send to and a claim of the second role are refused, and the
      refusal names its successor.
  R9  verb: retire_role refuses a caller holding no primary/principal role and
      accepts a primary-class caller (version 2 -> 3).
  R10 read failure: a failed registry read raises, never an empty registry, and
      list_retired_roles returns a failure with no roles; an empty registry
      raises too.
  R11 live holder: retiring a role a live session holds is refused with nothing
      written, for the exact name AND for case, padding and full-width variants
      (the registry matches normalized names; bindings are keyed raw). Control:
      a role whose binding has no live holder retires.
  R12 registry fault: with the registry read failing, (a) peer_send_by_name,
      (b) the HTTP send-by-name route, (c) streamable peer_send_by_name,
      (d) deliver_job_completion and (e) peer_claim_role each refuse with
      retired_role_registry_unavailable and persist nothing. Each path has a
      control showing the same harness persists when the registry reads.
  R13 retire fails closed: (a) a role_binding walk that fails, (b) a malformed
      binding row whose normalized name matches the target, and (c) an
      unreadable authority read each make retire_role refuse with its named
      code (role_binding_unreadable / retire_role_authority_unreadable) and
      leave registry_version unchanged. Control: a malformed row that does NOT
      match the target does not block another retirement.

Mutants (applied by the lane's mutant runner, each red on the named check):

  M1 version not bumped (``+ 1`` dropped in ``retire_role``)          -> R7
  M2 refusal after persist (plugin check moved after dispatch)         -> R2
  M3 claim not checked (claim-body refusal removed)                    -> R5
  M4 empty list on read error (read error returns an empty registry)   -> R10
  M5 shared plugin refusal fails open on a registry error              -> R12a, R12d
  M6 peer_send_by_name alone ignores a registry-error refusal          -> R12a
  M7 HTTP route fails open on a registry error                         -> R12b
  M8 streamable send fails open on a registry error                    -> R12c
  M9 deliver_job_completion alone ignores a registry-error refusal     -> R12d
  M10 claim fails open on a registry error                             -> R12e
  M11 live-holder check back to a raw (case-sensitive) name compare    -> R11
  M12 retire_role swallows the role_binding_unreadable error           -> R13a, R13b
  M13 retire_role swallows the authority-unreadable error              -> R13c

Run:
    SOLET_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/retired_role_registry_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.interfaces.state_management_interface import (  # noqa: E402
    StateManagementInterface,
)
from ananta.llm.agent_messaging.repository import AgentMessagingRepository  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    TABLE_ROLE,
    TABLE_ROLE_BINDING,
)
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    NAMESPACE,
    TABLE_AGENT_ROLE_MESSAGE,
)
from ananta.llm.agent_messaging.service import AgentMessagingService  # noqa: E402
from ananta.services.store import open_store  # noqa: E402

from agent_messaging_plugin.bridge_sessions import BridgeSessionManager  # noqa: E402
from agent_messaging_plugin.http_routes import (  # noqa: E402
    PeerSendByNameBody,
    _peer_send_by_name_impl,
    _resolve_role_for_send,
)
from agent_messaging_plugin.mcp_streamable import dispatch as streamable_dispatch  # noqa: E402
from agent_messaging_plugin.mcp_streamable.auth import BearerClaim  # noqa: E402
from agent_messaging_plugin.mcp_streamable.dispatch import (  # noqa: E402
    DispatchContext,
    JsonRpcError,
)
from agent_messaging_plugin.mcp_streamable.session import (  # noqa: E402
    StreamableSessionManager,
)
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.retired_role_registry import (  # noqa: E402
    RetiredRoleRegistryError,
    RetireRoleRequest,
    read_retired_role_registry,
    retire_role,
    seed_retired_role_registry,
)
from agent_messaging_plugin.role_claim import (  # noqa: E402
    RoleClaimFailure,
    RoleClaimOrigin,
    claim_role_for_session,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)

_RETIRED = "Register-Lead"
_LIVE = "Coordinator"
_SECOND = "Old-Coordinator"
_MAIN = "Fixture-Main"
_MAIN_INSTANCE = "agi-main"
_HELD = "Git-Controller"
_HELD_INSTANCE = "agi-held"
_STALE = "Stale-Role"
_UNAVAILABLE = "retired_role_registry_unavailable"
_LANE_INSTANCE = "agi-lane"

_passed = 0
_failed: list[str] = []


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


class _EnabledConfig:
    enabled = True
    allowed_backends: tuple[str, ...] = ()
    max_message_bytes = 65_536


class _Orchestrator:
    def __init__(self, state: RealShapeState) -> None:
        self._state = state

    def get_service(self, name: str) -> object:
        return self._state if name == "state_service" else None


def _bridge_manager() -> BridgeSessionManager:
    return BridgeSessionManager(
        session_id_factory=lambda _solet_name: "ases-retired",
        idle_timeout_s=3600,
        max_pending_events=16,
        long_poll_timeout_s=1,
    )


def _peer_registry() -> PeerRegistry:
    store = open_store(
        get_peer_binding_schema(), namespace=PEER_BINDING_NAMESPACE, backend="in_memory",
    )
    return PeerRegistry(bindings_store=store)


def _seed_binding(state: RealShapeState, role: str, instance: str) -> None:
    state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_ROLE_BINDING).append(
        {
            "id": f"rbn-{role}",
            "external_id": f"role:{role}",
            "role": role,
            "holder_kind": "session",
            "agent_instance_id": instance,
            "agent_session_id": f"ases-{instance}",
            "holder_identity": {"agent_id": "claude_code", "session_label": role},
            "claim_epoch": 1,
            "claimed_at": "2026-09-05T23:55:42+00:00",
            "is_deleted": 0,
        },
    )


def _seed_role_class(state: RealShapeState, role: str, role_class: str) -> None:
    state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_ROLE).append(
        {"id": f"rol-{role}", "external_id": f"role:{role}", "role": role,
         "role_class": role_class, "is_deleted": 0},
    )


def _fixture() -> tuple[RealShapeState, Any]:
    state = RealShapeState()
    # The live defect's shape: the retired role still has a stale binding.
    _seed_binding(state, _RETIRED, "agi-d04925a7")
    _seed_binding(state, _LIVE, "agi-coordinator")
    _seed_binding(state, _MAIN, _MAIN_INSTANCE)
    _seed_binding(state, _HELD, _HELD_INSTANCE)
    _seed_binding(state, _STALE, "agi-stale")
    _seed_role_class(state, _MAIN, "primary")
    seed_retired_role_registry(cast(StateManagementInterface, state))
    plugin: Any = object.__new__(AgentMessagingPlugin)
    plugin.orchestrator_ref = _Orchestrator(state)
    plugin._bridge_manager = _bridge_manager()
    plugin._peer_registry = _peer_registry()
    plugin._service = AgentMessagingService(
        repository=AgentMessagingRepository(cast(StateManagementInterface, state)),
        state_service=cast(StateManagementInterface, state),
        config=cast(Any, _EnabledConfig()),
    )
    return state, plugin


def _make_live(plugin: Any, label: str, instance: str) -> str:
    """Register ``instance`` on a freshly opened bridge, so the liveness predicate sees it live."""
    bridge_id = str(plugin._bridge_manager.open(solet_name="test", parent_pid=None).bridge_id)
    plugin._peer_registry.register(
        BridgeBinding(
            bridge_id=bridge_id,
            agent_id="claude_code",
            agent_instance_id=instance,
            session_label=label,
            parent_pid=None,
            agent_session_id=f"ases-{instance}",
        ),
    )
    return bridge_id


def _guarded(call: Any) -> tuple[Any, str]:
    """Run one transport call; a crash is recorded, never allowed to end the smoke."""
    try:
        return call(), ""
    except JsonRpcError as exc:
        return None, str((exc.data or {}).get("code"))
    except Exception as exc:  # noqa: BLE001 — recorded as the check's own failure
        return None, f"raised {type(exc).__name__}: {exc}"


def _persisted(state: RealShapeState, role: str) -> int:
    return sum(
        1 for row in state.rows(NAMESPACE, TABLE_AGENT_ROLE_MESSAGE)
        if row.get("recipient_key") == role
    )


def _code(result: dict[str, Any]) -> str:
    error = result.get("error")
    return str(error.get("code")) if isinstance(error, dict) else ""


def _send(plugin: Any, name: str) -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        plugin.peer_send_by_name({"name": name, "content": "hello"}, {}),
    )


def _claim(state: RealShapeState, name: str) -> object:
    return claim_role_for_session(
        origin=RoleClaimOrigin.MODEL_TURN,
        name=name,
        agent_id="claude_code",
        agent_instance_id=_LANE_INSTANCE,
        agent_session_id=f"ases-{_LANE_INSTANCE}",
        session_label=name,
        state_service=state,
        bridge_manager=None,
        peer_registry=None,
        agent_messaging_service=None,
        call_context=None,
    )


def _binding_holder(state: RealShapeState, role: str) -> str:
    for row in state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_ROLE_BINDING):
        if row.get("role") == role and row.get("is_deleted") == 0:
            return str(row.get("agent_instance_id"))
    return ""


def test_seed() -> None:
    state, _plugin = _fixture()
    registry = read_retired_role_registry(cast(StateManagementInterface, state))
    _check(
        registry.registry_version == 1
        and [role.role for role in registry.roles] == [_RETIRED]
        and registry.roles[0].successor is None,
        "R1 seed: Register-Lead retired at registry_version 1 with no successor",
    )
    _check(
        seed_retired_role_registry(cast(StateManagementInterface, state)) == "already_seeded"
        and read_retired_role_registry(
            cast(StateManagementInterface, state),
        ).registry_version == 1,
        "R1 seed: a second seed run is a no-op",
    )


def test_send_refused_before_persist() -> None:
    state, plugin = _fixture()
    result = _send(plugin, _RETIRED)
    error = result.get("error")
    message = str(error.get("message")) if isinstance(error, dict) else ""
    _check(
        _code(result) == "peer_role_retired",
        f"R2 send: Register-Lead refused with peer_role_retired (got {_code(result)!r})",
    )
    _check(
        "triage is filed as project-solet register tickets" in message
        and "no successor" in message,
        "R2 send: the refusal names the reason and that there is no successor",
    )
    _check(
        _persisted(state, _RETIRED) == 0,
        f"R2 send: nothing persisted for Register-Lead (rows={_persisted(state, _RETIRED)})",
    )
    folded = _send(plugin, "register-LEAD")
    _check(
        _code(folded) == "peer_role_retired" and _persisted(state, "register-LEAD") == 0,
        "R3 send: a case-folded spelling is refused and not persisted",
    )


def test_live_role_unchanged() -> None:
    state, plugin = _fixture()
    result = _send(plugin, _LIVE)
    data = result.get("data")
    _check(
        result.get("action_status") == "completed"
        and isinstance(data, dict)
        and data.get("delivery") == "queued_for_replay"
        and _persisted(state, _LIVE) == 1,
        f"R4 send: a live role still sends and persists one row (got {result.get('action_status')!r})",
    )


def test_claim_refused() -> None:
    state, _plugin = _fixture()
    refused = _claim(state, _RETIRED)
    _check(
        isinstance(refused, RoleClaimFailure) and refused.code == "peer_role_retired",
        f"R5 claim: claiming Register-Lead is refused with peer_role_retired "
        f"(got {getattr(refused, 'code', refused)!r})",
    )
    _check(
        _binding_holder(state, _RETIRED) == "agi-d04925a7",
        "R5 claim: the refused claim wrote no binding",
    )
    control = _claim(state, "Fresh-Lane-Role")
    _check(
        not isinstance(control, RoleClaimFailure),
        "R5 claim: control — an unretired name still claims on this harness",
    )


def test_http_and_streamable_refuse() -> None:
    state, plugin = _fixture()
    response = _resolve_role_for_send(state, _RETIRED)
    body = getattr(response, "body", b"")
    _check(
        getattr(response, "status_code", None) == 410 and b"peer_role_retired" in body,
        "R6 HTTP: send_by_name route refuses Register-Lead with 410 peer_role_retired",
    )
    sessions = StreamableSessionManager(
        bridge_manager=_bridge_manager(), peer_registry=_peer_registry(),
    )
    session = sessions.allocate(
        BearerClaim(
            agent_id="claude_phone",
            agent_instance_id="agi-oauth",
            issued_at=datetime(2026, 10, 2, tzinfo=UTC),
            client_id="client-retired",
        ),
    )
    code = ""
    try:
        streamable_dispatch._tool_peer_send_by_name(
            {"name": _RETIRED, "content": "hello"},
            session=session,
            context=DispatchContext(
                bridge_manager=_bridge_manager(),
                peer_registry=_peer_registry(),
                platform_surface=cast(Any, object()),
                agent_messaging_service=plugin._service,
                state_service=cast(Any, state),
            ),
        )
    except JsonRpcError as exc:
        code = str((exc.data or {}).get("code"))
    _check(
        code == "peer_role_retired" and _persisted(state, _RETIRED) == 0,
        f"R6 streamable: peer_send_by_name refuses Register-Lead (got {code!r})",
    )


def test_second_role_and_version_bump() -> None:
    state, plugin = _fixture()
    retired = retire_role(
        cast(StateManagementInterface, state),
        RetireRoleRequest(
            role=_SECOND,
            reason="fixture: replaced by the Coordinator role",
            retired_by="smoke",
            retired_at="2026-10-02T00:00:00+00:00",
            successor=_LIVE,
        ),
    )
    registry = read_retired_role_registry(cast(StateManagementInterface, state))
    _check(
        retired.registry_version == 2 and registry.registry_version == 2
        and [role.role for role in registry.roles] == [_RETIRED, _SECOND],
        f"R7 version: retiring a second role bumps registry_version 1 -> "
        f"{registry.registry_version}",
    )
    result = _send(plugin, _SECOND)
    error = result.get("error")
    message = str(error.get("message")) if isinstance(error, dict) else ""
    _check(
        _code(result) == "peer_role_retired" and repr(_LIVE) in message
        and _persisted(state, _SECOND) == 0,
        "R8 second role: a send is refused, names the successor, persists nothing",
    )
    claim = _claim(state, _SECOND)
    _check(
        isinstance(claim, RoleClaimFailure) and claim.code == "peer_role_retired",
        "R8 second role: a claim is refused",
    )


def test_retire_verb_authorization() -> None:
    state, plugin = _fixture()
    params = {"name": "Spare-Role", "reason": "fixture", "authority": "iss_d9c955a2"}
    refused = plugin.retire_role(params, {"caller_attribution_instance_id": _LANE_INSTANCE})
    _check(
        _code(refused) == "retire_role_not_authorized",
        f"R9 verb: a caller with no primary/principal role is refused (got {_code(refused)!r})",
    )
    accepted = plugin.retire_role(params, {"caller_attribution_instance_id": _MAIN_INSTANCE})
    data = accepted.get("data")
    _check(
        isinstance(data, dict) and data.get("registry_version") == 2
        and str(data.get("retired_by", "")).startswith(_MAIN),
        f"R9 verb: a primary-class caller retires at the next version (got {data!r})",
    )


def test_read_failure_raises() -> None:
    state, plugin = _fixture()
    state.fail_next("query")
    raised = ""
    try:
        read_retired_role_registry(cast(StateManagementInterface, state))
    except RetiredRoleRegistryError as exc:
        raised = exc.message
    _check(
        "read failed" in raised,
        f"R10 read failure: a failed read raises a read error, never an empty registry "
        f"(got {raised!r})",
    )
    state.fail_next("query")
    listed = plugin.list_retired_roles({}, {})
    _check(
        listed.get("action_status") == "failed"
        and _code(listed) == "retired_role_registry_unavailable"
        and "roles" not in (listed.get("data") or {}),
        "R10 read failure: list_retired_roles fails rather than returning an empty list",
    )
    ok = plugin.list_retired_roles({}, {})
    _check(
        (ok.get("data") or {}).get("registry_version") == 1,
        "R10 read failure: control — the same verb reads version 1 once the fault clears",
    )
    empty = RealShapeState()
    raised_empty = False
    try:
        read_retired_role_registry(cast(StateManagementInterface, empty))
    except RetiredRoleRegistryError:
        raised_empty = True
    _check(raised_empty, "R10 read failure: an empty (unseeded) registry raises")


def _registry_size(state: RealShapeState) -> int:
    return len(read_retired_role_registry(cast(StateManagementInterface, state)).roles)


def test_live_holder_variants_refused() -> None:
    state, plugin = _fixture()
    _make_live(plugin, _HELD, _HELD_INSTANCE)
    main_caller = {"caller_attribution_instance_id": _MAIN_INSTANCE}
    for variant in (_HELD, "git-controller", "  GIT-CONTROLLER  ", "\uff27\uff49\uff54-Controller"):
        result = plugin.retire_role(
            {"name": variant, "reason": "fixture", "authority": "iss_d9c955a2"}, main_caller,
        )
        _check(
            _code(result) == "role_held_live" and _registry_size(state) == 1,
            f"R11 live holder: retiring {variant!r} while {_HELD!r} is held live is refused "
            f"role_held_live with nothing written (got {_code(result)!r})",
        )
    sent = _send(plugin, _HELD)
    _check(
        sent.get("action_status") == "completed",
        "R11 live holder: the live role still receives sends after the refused retirements",
    )
    stale = plugin.retire_role(
        {"name": _STALE, "reason": "fixture", "authority": "iss_d9c955a2"}, main_caller,
    )
    _check(
        (stale.get("data") or {}).get("registry_version") == 2,
        f"R11 live holder: control — a bound role with no live holder retires (got {_code(stale)!r})",
    )


def _fault_send(plugin: Any, state: RealShapeState) -> tuple[Any, str]:
    return _guarded(lambda: _send(plugin, _LIVE))


def _fault_http(plugin: Any, state: RealShapeState) -> tuple[Any, str]:
    bridge_id = _make_live(plugin, "Sender", "agi-sender")
    return _guarded(
        lambda: _peer_send_by_name_impl(
            bridge_id=bridge_id,
            body=PeerSendByNameBody(name=_LIVE, content="hello"),
            bridge_manager=plugin._bridge_manager,
            peer_registry=plugin._peer_registry,
            agent_messaging_service=plugin._service,
            state_service=state,
        ),
    )


def _fault_streamable(plugin: Any, state: RealShapeState) -> tuple[Any, str]:
    session = StreamableSessionManager(
        bridge_manager=_bridge_manager(), peer_registry=_peer_registry(),
    ).allocate(
        BearerClaim(
            agent_id="claude_phone",
            agent_instance_id="agi-oauth",
            issued_at=datetime(2026, 10, 2, tzinfo=UTC),
            client_id="client-retired",
        ),
    )
    return _guarded(
        lambda: streamable_dispatch._tool_peer_send_by_name(
            {"name": _LIVE, "content": "hello"},
            session=session,
            context=DispatchContext(
                bridge_manager=_bridge_manager(),
                peer_registry=_peer_registry(),
                platform_surface=cast(Any, object()),
                agent_messaging_service=plugin._service,
                state_service=cast(Any, state),
            ),
        ),
    )


def _fault_job(plugin: Any, state: RealShapeState) -> tuple[Any, str]:
    return _guarded(
        lambda: plugin.deliver_job_completion(
            {"name": _LIVE, "job_id": "job-fault", "status": "completed"}, {},
        ),
    )


def _outcome_code(result: Any, raised: str) -> str:
    """The refusal code a transport reported, however that transport reports it."""
    if raised:
        return raised
    if isinstance(result, dict):
        return _code(result)
    body = getattr(result, "body", b"")
    return _UNAVAILABLE if _UNAVAILABLE.encode() in body else f"http {getattr(result, 'status_code', '?')}"


def test_registry_fault_fails_closed() -> None:
    paths = (
        ("R12a peer_send_by_name", _fault_send),
        ("R12b HTTP send_by_name route", _fault_http),
        ("R12c streamable peer_send_by_name", _fault_streamable),
        ("R12d deliver_job_completion", _fault_job),
    )
    for label, run in paths:
        state, plugin = _fixture()
        state.fail_next("query")
        result, raised = run(plugin, state)
        code = _outcome_code(result, raised)
        _check(
            code == _UNAVAILABLE and _persisted(state, _LIVE) == 0,
            f"{label}: a registry read fault refuses {_UNAVAILABLE} and persists nothing "
            f"(got {code!r}, rows={_persisted(state, _LIVE)})",
        )
        control_state, control_plugin = _fixture()
        run(control_plugin, control_state)
        _check(
            _persisted(control_state, _LIVE) == 1,
            f"{label}: control — the same call persists one row when the registry reads",
        )
    state, _plugin = _fixture()
    state.fail_next("query")
    refused = _claim(state, "Fresh-Fault-Role")
    _check(
        isinstance(refused, RoleClaimFailure) and refused.code == _UNAVAILABLE
        and _binding_holder(state, "Fresh-Fault-Role") == "",
        f"R12e peer_claim_role: a registry read fault refuses {_UNAVAILABLE} and writes no "
        f"binding (got {getattr(refused, 'code', refused)!r})",
    )
    control_state, _control = _fixture()
    _check(
        not isinstance(_claim(control_state, "Fresh-Fault-Role"), RoleClaimFailure)
        and _binding_holder(control_state, "Fresh-Fault-Role") == _LANE_INSTANCE,
        "R12e peer_claim_role: control — the same claim binds when the registry reads",
    )


def _retire(plugin: Any, name: str) -> dict[str, Any]:
    return cast(
        "dict[str, Any]",
        plugin.retire_role(
            {"name": name, "reason": "fixture", "authority": "iss_d9c955a2"},
            {"caller_attribution_instance_id": _MAIN_INSTANCE},
        ),
    )


def _version(state: RealShapeState) -> int:
    return read_retired_role_registry(cast(StateManagementInterface, state)).registry_version


def test_retire_fails_closed_on_unreadable_state() -> None:
    state, plugin = _fixture()
    ordered = state.query_ordered

    def failing_binding_walk(namespace: str, query: dict[str, Any]) -> dict[str, Any]:
        if query.get("table") == TABLE_ROLE_BINDING:
            return {"action_status": "failed", "error": {"message": "injected walk failure"}, "data": {}}
        return cast("dict[str, Any]", ordered(namespace, query))

    state.query_ordered = failing_binding_walk  # type: ignore[method-assign]
    walk = _retire(plugin, _STALE)
    state.query_ordered = ordered  # type: ignore[method-assign]
    _check(
        _code(walk) == "role_binding_unreadable" and _version(state) == 1,
        f"R13a retire: a failing role_binding walk refuses role_binding_unreadable, "
        f"registry_version unchanged (got {_code(walk)!r}, v{_version(state)})",
    )

    state, plugin = _fixture()
    state.rows(AGENT_ROLE_BINDING_NAMESPACE, TABLE_ROLE_BINDING).append(
        {"id": "rbn-broken", "external_id": "role:Broken-Role", "role": "Broken-Role",
         "holder_kind": "session", "agent_instance_id": "agi-broken",
         "agent_session_id": "ases-agi-broken", "holder_identity": {},
         "claim_epoch": 1, "is_deleted": 0},
    )
    broken = _retire(plugin, "broken-role")
    _check(
        _code(broken) == "role_binding_unreadable" and _version(state) == 1,
        f"R13b retire: a malformed binding row matching the target (normalized) refuses "
        f"role_binding_unreadable, registry_version unchanged (got {_code(broken)!r})",
    )
    other = _retire(plugin, _STALE)
    _check(
        (other.get("data") or {}).get("registry_version") == 2,
        f"R13b retire: control — a malformed row that does not match the target does not "
        f"block another retirement (got {_code(other)!r})",
    )

    state, plugin = _fixture()
    state.fail_next("query")
    authority = _retire(plugin, _STALE)
    _check(
        _code(authority) == "retire_role_authority_unreadable" and _version(state) == 1,
        f"R13c retire: an unreadable authority read refuses retire_role_authority_unreadable, "
        f"registry_version unchanged (got {_code(authority)!r})",
    )


def main() -> int:
    for test in (
        test_seed,
        test_send_refused_before_persist,
        test_live_role_unchanged,
        test_claim_refused,
        test_http_and_streamable_refuse,
        test_second_role_and_version_bump,
        test_retire_verb_authorization,
        test_read_failure_raises,
        test_live_holder_variants_refused,
        test_registry_fault_fails_closed,
        test_retire_fails_closed_on_unreadable_state,
    ):
        print(f"\n{test.__name__}")
        test()
    if _failed:
        print(f"\nFAILED ({len(_failed)}): " + "; ".join(_failed))
        return 1
    print(f"\nPASS ({_passed} checks): retired-role registry smoke")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
