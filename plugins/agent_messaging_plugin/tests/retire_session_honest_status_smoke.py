#!/usr/bin/env python3
"""``retire_session`` / ``terminate_session`` say what they did NOT do (iss_7ee6fb98).

A lane launched by hand registers as ``host='operator'``: the operator driver
cannot stop a process it did not spawn, so ``terminate_session`` lands the
ledger transition and ``retire_session`` returned ``completed`` with no sign
that the process was still running and still listed by ``peer_list`` (20 lanes,
2026-09-30). The refusal to kill a process the ledger never spawned is
deliberate and unchanged; the defect was the silence. This smoke pins the
result: ``host_action`` (``terminated`` / ``none_available`` /
``not_attempted``) with the driver's own remedy, and, on the plugin verb, a
``not_done`` list plus the live peer registration. Real ledger fake, real
operator driver, a recording fake driver for the host that does terminate.
Hermetic.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
_ROOT = REPO_ROOT
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402  # pyright: ignore[reportMissingImports] - resolved at runtime by the sys.path.insert calls above; plugins/<X>/tests is not a pyright extraPath
from _register_unit_double import FIXTURE_REPOSITORY_ID, fixture_assignment  # noqa: E402  # pyright: ignore[reportMissingImports] - resolved at runtime by the sys.path.insert calls above; plugins/<X>/tests is not a pyright extraPath
from ananta.core.services.call_context import CallContext  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.services.store import Store, open_store  # noqa: E402
from session_lifecycle_transport_smoke import PacketLane, packet_lane  # noqa: E402  # pyright: ignore[reportMissingImports] - resolved at runtime by the sys.path.insert calls above; plugins/<X>/tests is not a pyright extraPath

import agent_messaging_plugin.session_hosts as session_hosts  # noqa: E402
import agent_messaging_plugin.session_lifecycle_verbs as lifecycle  # noqa: E402
from agent_messaging_plugin.lane_worktree_evidence import PsoletRetirementEvidenceReader  # noqa: E402
from agent_messaging_plugin.models import BridgeBinding  # noqa: E402
from agent_messaging_plugin.peer_registry import PeerRegistry  # noqa: E402
from agent_messaging_plugin.plugin import AgentMessagingPlugin  # noqa: E402
from agent_messaging_plugin.schema import (  # noqa: E402
    LIFECYCLE_LIVE,
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    PEER_BINDING_NAMESPACE,
    get_peer_binding_schema,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
    transition_lifecycle_state,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    VerbError,
    retire_completed_lane_sessions,
    retire_session,
    terminate_session,
)

_passed = 0
_failed: list[str] = []
_KILLING_HOST = "smoke-killing-host"
_WHO = "operator:none"


def _check(condition: object, label: str) -> None:
    global _passed
    if condition:
        _passed += 1
        print(f"  PASS  {label}")
        return
    _failed.append(label)
    print(f"  FAIL  {label}")


class _KillingDriver:
    """A host driver that really ends its host: records what it was asked to stop."""

    def __init__(self) -> None:
        self.terminated: list[str] = []

    def spawn(self, spec: object) -> str:
        del spec
        return "fake-ref"

    def alive(self, host_ref: str) -> bool:
        return host_ref not in self.terminated

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del grace_seconds
        self.terminated.append(host_ref)

    def driver_channel(self, host_ref: str) -> None:
        del host_ref

    def capability_report(self) -> dict[str, object]:
        return {}

    def verify_config(self) -> list[str]:
        return []


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def _live_row(state: StateManagementInterface, instance: str, host: str) -> None:
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id=instance,
            lane_id=f"lane-{instance}",
            assignment=fixture_assignment(),
            work_class="read_only",
            budget_line="b1",
            host=host,
        ),
    )
    transition_lifecycle_state(
        state,
        agent_instance_id=instance,
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE,
        directed_by=_WHO,
    )
    # No derivable lane worktree: adjudication is a no-op, as for the lanes in the report.
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": instance}},
        {"role_name": "", "local_name": "", "host_ref": f"ref-{instance}"},
    )


def test_operator_host_retire_reports_the_host_was_not_stopped() -> None:
    state = _state()
    _live_row(state, "agi-op", "operator")
    result = retire_session(state, agent_instance_id="agi-op", directed_by=_WHO)
    _check(result["already_retired"] is False, "the ledger transition still lands for an operator-hosted row")
    _check(read_managed_session(state, "agi-op")["lifecycle_state"] == LIFECYCLE_RETIRED, "the row is retired")
    _check(result.get("host") == "operator", "the result names the row's host")
    _check(result.get("host_action") == "none_available", "an operator-hosted retire reports host_action=none_available, not silence")
    _check("stop the process manually" in str(result.get("host_remedy")), "the driver's own remedy text is carried")


def test_terminating_host_reports_terminated() -> None:
    state = _state()
    driver = _KillingDriver()
    session_hosts._REGISTRY[_KILLING_HOST] = driver  # noqa: SLF001 -- test-only monkeypatch
    try:
        _live_row(state, "agi-kill", _KILLING_HOST)
        result = retire_session(state, agent_instance_id="agi-kill", directed_by=_WHO)
    finally:
        session_hosts._REGISTRY.pop(_KILLING_HOST, None)  # noqa: SLF001
    _check(result.get("host_action") == "terminated", "a driver that ended its host reports host_action=terminated")
    _check(result.get("host_remedy") is None, "a terminated host carries no remedy")
    _check(len(driver.terminated) == 1, "the driver was asked to terminate exactly once")


def test_repeat_and_redrive_do_not_claim_a_host_action() -> None:
    state = _state()
    _live_row(state, "agi-rep", "operator")
    first_terminate = terminate_session(state, agent_instance_id="agi-rep", directed_by=_WHO)
    _check(first_terminate.get("host_action") == "none_available", "terminate_session reports the same host_action")
    _check(first_terminate.get("host") == "operator", "terminate_session names the host")
    repeat_terminate = terminate_session(state, agent_instance_id="agi-rep", directed_by=_WHO)
    _check(
        repeat_terminate["already_terminal"] is True and repeat_terminate.get("host_action") == "not_attempted",
        "a repeat terminate_session did no host action and says so",
    )
    redrive = retire_session(state, agent_instance_id="agi-rep", directed_by=_WHO)
    _check(
        redrive["already_retired"] is False and redrive.get("host_action") == "not_attempted",
        "retiring an already-terminated row (re-drive) reports it did not attempt the host",
    )
    again = retire_session(state, agent_instance_id="agi-rep", directed_by=_WHO)
    _check(
        again["already_retired"] is True and again.get("host_action") == "not_attempted",
        "retiring an already-retired row reports it did not attempt the host",
    )
    _check(read_managed_session(state, "agi-rep")["lifecycle_state"] == LIFECYCLE_RETIRED, "the redrive still ends retired")


def test_unsupported_host_still_refuses() -> None:
    state = _state()
    _live_row(state, "agi-unk", "no-such-host")
    try:
        retire_session(state, agent_instance_id="agi-unk", directed_by=_WHO)
    except VerbError as exc:
        _check(exc.code == "unsupported_on_host", "a host with no driver still refuses loudly")
    else:
        _check(False, "a host with no driver still refuses loudly")
    _check(read_managed_session(state, "agi-unk")["lifecycle_state"] != LIFECYCLE_TERMINATED, "a refused retire leaves the ledger untouched")


def _registry_with(instance: str) -> PeerRegistry:
    store: Store = open_store(get_peer_binding_schema(), namespace=PEER_BINDING_NAMESPACE, backend="in_memory")
    registry = PeerRegistry(bindings_store=store)
    registry.register(
        BridgeBinding(
            bridge_id="agc-live",
            agent_id="claude_code",
            agent_instance_id=instance,
            session_label="Lane",
            parent_pid=63626,
            agent_session_id="ases-live",
        ),
    )
    return registry


def _plugin(state: StateManagementInterface, registry: PeerRegistry | None) -> AgentMessagingPlugin:
    plugin = object.__new__(AgentMessagingPlugin)
    plugin._get_state_service = lambda: state  # type: ignore[method-assign]
    plugin._peer_registry = registry  # noqa: SLF001
    return plugin


def _retire_through_plugin(plugin: AgentMessagingPlugin, instance: str) -> dict[str, Any]:
    result = plugin.retire_session(
        {"parameters": {"agent_instance_id": instance}},
        {"call_context": CallContext.for_operator()},
    )
    assert result["action_status"] == "completed", result
    return cast("dict[str, Any]", result["data"])


def test_plugin_verb_lists_what_was_not_done() -> None:
    state = _state()
    _live_row(state, "agi-reg", "operator")
    data = _retire_through_plugin(_plugin(state, _registry_with("agi-reg")), "agi-reg")
    not_done = data.get("not_done")
    _check(isinstance(not_done, list) and len(not_done) == 2, "an operator-hosted lane still registered reports both gaps")
    text = " | ".join(not_done) if isinstance(not_done, list) else ""
    _check("not stopped" in text and "stop the process manually" in text, "the host gap names the manual remedy")
    _check("peer_list" in text and "63626" in text, "the registration gap names the still-listed bridge pid")
    _check(
        data.get("peer_registration") == {"bridge_id": "agc-live", "parent_pid": 63626},
        "the live registration is returned as structured data",
    )


def test_plugin_verb_retire_after_an_earlier_teardown_lists_nothing_it_left_undone() -> None:
    """F2: a lane torn down by an earlier terminate (or the sweep) is not a false open item."""
    state = _state()
    session_hosts._REGISTRY[_KILLING_HOST] = _KillingDriver()  # noqa: SLF001 -- test-only monkeypatch
    try:
        _live_row(state, "agi-earlier", _KILLING_HOST)
        earlier = terminate_session(state, agent_instance_id="agi-earlier", directed_by=_WHO)
        data = _retire_through_plugin(_plugin(state, _registry_with("agi-someone-else")), "agi-earlier")
        again = _retire_through_plugin(_plugin(state, _registry_with("agi-someone-else")), "agi-earlier")
    finally:
        session_hosts._REGISTRY.pop(_KILLING_HOST, None)  # noqa: SLF001
    _check(earlier["host_action"] == "terminated", "setup: the earlier terminate really ended the host")
    _check(data["host_action"] == "not_attempted", "retire after a terminate reports it attempted no host action")
    _check(data["not_done"] == [], "observed-dead earlier teardown has no gaps")
    _check(
        again["already_retired"] is True and again["not_done"] == [],
        "retiring an already-retired, fully torn-down lane lists nothing",
    )


def test_plugin_verb_still_reports_a_registration_left_after_an_earlier_terminate() -> None:
    """The registration check is a measured fact of its own: it still reports after a terminate."""
    state = _state()
    _live_row(state, "agi-op-earlier", "operator")
    terminate_session(state, agent_instance_id="agi-op-earlier", directed_by=_WHO)
    data = _retire_through_plugin(_plugin(state, _registry_with("agi-op-earlier")), "agi-op-earlier")
    _check(
        len(data["not_done"]) == 2 and "still registered in peer_list" in data["not_done"][1],
        "earlier operator teardown preserves host and registration gaps",
    )


def test_plugin_verb_clean_teardown_has_nothing_left() -> None:
    state = _state()
    session_hosts._REGISTRY[_KILLING_HOST] = _KillingDriver()  # noqa: SLF001
    try:
        _live_row(state, "agi-clean", _KILLING_HOST)
        data = _retire_through_plugin(_plugin(state, _registry_with("agi-someone-else")), "agi-clean")
    finally:
        session_hosts._REGISTRY.pop(_KILLING_HOST, None)  # noqa: SLF001
    _check(data.get("not_done") == [], "a terminated host with no registration left reports not_done=[]")
    _check(data.get("peer_registration") is None, "no registration is reported when none is bound")


def test_plugin_verb_without_a_registry_says_it_did_not_look() -> None:
    state = _state()
    session_hosts._REGISTRY[_KILLING_HOST] = _KillingDriver()  # noqa: SLF001
    try:
        _live_row(state, "agi-noreg", _KILLING_HOST)
        data = _retire_through_plugin(_plugin(state, None), "agi-noreg")
    finally:
        session_hosts._REGISTRY.pop(_KILLING_HOST, None)  # noqa: SLF001
    _check(
        isinstance(data.get("not_done"), list) and any("registration was not checked" in g for g in data["not_done"]),
        "with no peer registry the verb says it did not check the registration, rather than claiming none",
    )


def test_prior_operator_outcome() -> None:
    state = _state()
    _live_row(state, "agi-prior", "operator")
    terminate_session(state, agent_instance_id="agi-prior", directed_by=_WHO)
    plugin = _plugin(state, _registry_with("agi-unrelated"))
    for _ in range(2):
        data = _retire_through_plugin(plugin, "agi-prior")
        prior = data.get("prior_host_outcome") or {}
        _check(prior.get("action") == "none_available", "M1 prior none_available survives")
        _check("stop the process manually" in str(prior.get("remedy")), "prior remedy survives")
        _check(data.get("host_action") == "not_attempted", "prior action is not a current action")
        _check(data.get("host_liveness") == "unknown", "operator liveness is unobservable")
        _check(data.get("teardown_complete") is False, "operator teardown remains incomplete")
        _check(any("not stopped" in gap for gap in data["not_done"]), "prior operator host gap survives")
        status = lifecycle.session_status(state, "agi-prior")
        _check(status.get("prior_host_outcome") == data.get("prior_host_outcome"), "terminal status reads the same prior attempt")
        _check(status.get("prior_host_reason") == data.get("prior_host_reason"), "terminal status retains immutable rationale")


class _ProbeDriver(_KillingDriver):
    def __init__(self, observation: str) -> None:
        super().__init__()
        self.observation = observation

    def alive(self, host_ref: str) -> bool:
        del host_ref
        if self.observation == "unknown":
            raise RuntimeError("fake observation unavailable")
        return self.observation == "alive"


class _RacingDriver(_ProbeDriver):
    def __init__(self, state: StateManagementInterface, field: str) -> None:
        super().__init__("alive")
        self.state = state
        self.field = field
        self.probes = 0

    def alive(self, host_ref: str) -> bool:
        self.probes += 1
        if self.probes != 1:
            return True
        updates: dict[str, Any] = {
            "last_heartbeat_at": "2026-10-20T01:00:00+00:00",
            "last_transition_at": "2026-10-20T01:00:01+00:00",
            "host_ref": host_ref + "-replacement",
            "host": "operator",
            "lifecycle_state": "idle",
        }
        self.state.update_state(
            AGENT_ROLE_BINDING_NAMESPACE,
            {"table": "managed_session", "filters": {"agent_instance_id": "agi-race"}},
            {self.field: updates[self.field]},
        )
        return False


def test_collision_races() -> None:
    for field in ("last_heartbeat_at", "last_transition_at", "host_ref", "host", "lifecycle_state"):
        state = _state()
        driver = _RacingDriver(state, field)
        session_hosts._REGISTRY[_KILLING_HOST] = driver  # noqa: SLF001
        try:
            _live_row(state, "agi-race", _KILLING_HOST)
            state.update_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {"table": "managed_session", "filters": {"agent_instance_id": "agi-race"}},
                {"local_name": "test-race", "last_heartbeat_at": None},
            )
            refused = False
            try:
                lifecycle._refuse_if_local_name_held(state, local_name="test-race", role_name="")  # noqa: SLF001
            except VerbError as exc:
                refused = exc.code == "local_name_already_held"
            _check(refused, f"M5 changed {field} preserves name collision")
            row = read_managed_session(state, "agi-race")
            _check(row["lifecycle_state"] != LIFECYCLE_TERMINATED, f"changed {field} never terminalized")
            _check(driver.terminated == [], "race repair never invokes terminate")
            if field != "host":
                _check(driver.probes == 2, "stale observation discarded and freshly probed")
        finally:
            session_hosts._REGISTRY.pop(_KILLING_HOST, None)  # noqa: SLF001


class _NewIncumbentDriver(_ProbeDriver):
    def __init__(self, state: StateManagementInterface, add_new: bool) -> None:
        super().__init__("dead")
        self.state = state
        self.add_new = add_new
        self.probed: list[str] = []

    def alive(self, host_ref: str) -> bool:
        self.probed.append(host_ref)
        if self.add_new and len(self.probed) == 1:
            _named_row(self.state, "agi-new", "test-many")
        return host_ref == "ref-agi-new"


def _named_row(state: StateManagementInterface, instance: str, name: str) -> None:
    _live_row(state, instance, _KILLING_HOST)
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": instance}},
        {"local_name": name},
    )


def test_collision_full_set() -> None:
    for add_new in (False, True):
        state = _state()
        driver = _NewIncumbentDriver(state, add_new)
        session_hosts._REGISTRY[_KILLING_HOST] = driver  # noqa: SLF001
        try:
            _named_row(state, "agi-old1", "test-many")
            _named_row(state, "agi-old2", "test-many")
            refused = False
            try:
                lifecycle._refuse_if_local_name_held(state, local_name="test-many", role_name="")  # noqa: SLF001
            except VerbError as exc:
                refused = exc.code == "local_name_already_held"
            _check(refused is add_new, "new incumbent requires fresh evaluation")
            _check(len(driver.probed) == (3 if add_new else 2), "whole same-name set probed including new row")
            _check(driver.terminated == [], "multiple ghost repair has zero kill calls")
        finally:
            session_hosts._REGISTRY.pop(_KILLING_HOST, None)  # noqa: SLF001


def test_observed_teardown() -> None:
    for observation in ("alive", "dead", "unknown"):
        for checked in (False, True):
            state = _state()
            driver = _ProbeDriver(observation)
            session_hosts._REGISTRY[_KILLING_HOST] = driver  # noqa: SLF001
            try:
                _live_row(state, "agi-observed", _KILLING_HOST)
                registry = _registry_with("agi-unrelated") if checked else None
                plugin = _plugin(state, registry)
                first = _retire_through_plugin(plugin, "agi-observed")
                repeat = _retire_through_plugin(plugin, "agi-observed")
                expected = observation == "dead" and checked
                _check(first.get("host_liveness") == observation, "native observation retained")
                _check(first.get("registry_checked") is checked, "registration check is explicit")
                _check(first.get("teardown_complete") is expected, f"teardown proof {observation}/{checked}")
                if observation == "alive":
                    _check(first.get("teardown_complete") is False, "M2 still-alive teardown false")
                if not checked:
                    _check(first.get("teardown_complete") is False, "M3 unchecked teardown false")
                _check(repeat.get("teardown_complete") is expected, "repeat reobserves proof")
                _check(len(driver.terminated) == 1, "M6 repeat makes zero additional host calls")
            finally:
                session_hosts._REGISTRY.pop(_KILLING_HOST, None)  # noqa: SLF001


def test_legacy_attempt_unknown() -> None:
    for reason in ("old human reason", '{"schema":"managed-session.host-attempt/v1","attempt":false}'):
        state = _state()
        _live_row(state, "agi-legacy", "operator")
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-legacy",
            from_state=LIFECYCLE_LIVE,
            to_state=LIFECYCLE_TERMINATED,
            directed_by=_WHO,
            reason=reason,
        )
        data = _retire_through_plugin(_plugin(state, _registry_with("agi-unrelated")), "agi-legacy")
        _check(data.get("prior_host_outcome") is None, "legacy rationale is not invented proof")
        _check(data.get("teardown_complete") is False, "legacy teardown unknown")
        _check(any("unknown" in gap for gap in data["not_done"]), "M4 unknown legacy remains in not_done")


def test_dead_name_repair() -> None:
    for observation in ("dead", "alive", "unknown"):
        state = _state()
        driver = _ProbeDriver(observation)
        session_hosts._REGISTRY[_KILLING_HOST] = driver  # noqa: SLF001
        try:
            _live_row(state, "agi-incumbent", _KILLING_HOST)
            state.update_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {"table": "managed_session", "filters": {"agent_instance_id": "agi-incumbent"}},
                {"local_name": "test-name"},
            )
            refused = False
            try:
                lifecycle._refuse_if_local_name_held(state, local_name="test-name", role_name="")  # noqa: SLF001
            except VerbError as exc:
                refused = exc.code == "local_name_already_held"
            _check(refused is (observation != "dead"), f"name guard observes {observation}")
            current = read_managed_session(state, "agi-incumbent")["lifecycle_state"]
            _check(current == (LIFECYCLE_TERMINATED if observation == "dead" else LIFECYCLE_LIVE), "only dead transitions")
            _check(driver.terminated == [], "collision repair never terminates a host")
        finally:
            session_hosts._REGISTRY.pop(_KILLING_HOST, None)  # noqa: SLF001


_CLI_FIXTURE = '''#!{executable}
"""A real psolet CLI on disk; the production reader parses exactly what this prints."""
import json
import sys

PAYLOAD = json.loads({payload})
argv = sys.argv[1:]
if argv[:3] == ["db", "repository", "resolve"]:
    print(json.dumps({{"repository_id": PAYLOAD["repository_id"]}}))
elif argv[:3] == ["db", "unit", "evidence"]:
    if PAYLOAD["evidence_exit"]:
        sys.stderr.write("project-solet: injected evidence failure")
        raise SystemExit(PAYLOAD["evidence_exit"])
    print(json.dumps(PAYLOAD["evidence"]))
elif argv[:2] in (["db", "candidate-revision"], ["db", "reference"]):
    print(json.dumps([]))
else:
    sys.stderr.write("unexpected command: " + " ".join(argv))
    raise SystemExit(2)
'''


def _psolet_cli(root: Path, name: str, payload: dict[str, Any]) -> Path:
    """Write one executable psolet CLI whose output the real reader will parse."""
    path = root / f"psolet-{name}"
    path.write_text(_CLI_FIXTURE.format(executable=sys.executable, payload=repr(json.dumps(payload))))
    path.chmod(0o755)
    return path


def _unit_evidence(agent_instance_id: str, unit_state: str) -> dict[str, Any]:
    """One ``db unit evidence`` payload in the shape the register really prints."""
    assignment = fixture_assignment()
    return {
        "unit_id": assignment.unit_id,
        "unit_key": assignment.unit_key,
        "repository_id": FIXTURE_REPOSITORY_ID,
        "owner": {"agent_instance_id": agent_instance_id},
        "current_state": {"to_state": unit_state, "is_deleted": False},
        "landings": {"rows": [], "truncated": False, "returned_count": 0},
    }


def _real_reader(root: Path, name: str, agent_instance_id: str, unit_state: str, *, evidence_exit: int = 0) -> PsoletRetirementEvidenceReader:
    """The production reader, bound to a real CLI on disk — never a mock or a stub."""
    payload = {
        "repository_id": FIXTURE_REPOSITORY_ID,
        "evidence": _unit_evidence(agent_instance_id, unit_state),
        "evidence_exit": evidence_exit,
    }
    return PsoletRetirementEvidenceReader(str(_psolet_cli(root, name, payload)), solet_name="retire-completed-smoke")


def _sweep(lane: PacketLane, reader: PsoletRetirementEvidenceReader | None) -> dict[str, Any]:
    """Drive the entry point over this lane's own row under its contained Git environment."""
    row = read_managed_session(lane.state, lane.identity["agent_instance_id"])
    with patch.dict("os.environ", lane.environment, clear=True):
        return retire_completed_lane_sessions(lane.state, [row], directed_by=_WHO, register=reader)


def test_a_provably_completed_unit_retires_its_lane_and_disposes_of_the_tree() -> None:
    """PASS PATH: a live row whose Unit is terminal reaches retired and loses its worktree.

    The register state is read by the real ``PsoletRetirementEvidenceReader``
    running a real executable CLI on disk, against a real provisioned lane
    worktree and a real managed_session row.
    """
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        lane = packet_lane(root, "completed", unique_state=False)
        instance = lane.identity["agent_instance_id"]
        _check(lane.lifecycle() == LIFECYCLE_LIVE and lane.lane.path.is_dir(), "setup: a real live lane with a real worktree")
        result = _sweep(lane, _real_reader(root, "completed", instance, "accepted"))
        _check(lane.lifecycle() == LIFECYCLE_RETIRED, "a provably completed Unit drives its row to retired")
        _check(not lane.lane.path.exists(), "retire_session's own proof-based step disposed of the lane worktree")
        _check(lane.disposition() == "removed", "the recorded worktree_disposition is present on the row")
        _check(result["advisories"] == [], "a proved row produces no advisory")
        record = result["retired"][0] if result["retired"] else {}
        _check(result["considered"] == 1 and len(result["retired"]) == 1, "the one considered row is reported once")
        _check(record.get("unit_state") == "accepted" and record.get("unit_id") == lane.identity["unit_id"], "the record names the exact Unit and the state that proved it")
        _check(record.get("worktree_disposition") == "removed" and record.get("lifecycle_state") == LIFECYCLE_RETIRED, "the record carries the read-back disposition and state")


def test_a_still_working_unit_leaves_the_row_and_its_worktree_alone() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        lane = packet_lane(root, "working", unique_state=False)
        instance = lane.identity["agent_instance_id"]
        result = _sweep(lane, _real_reader(root, "working", instance, "working"))
        _check(lane.lifecycle() == LIFECYCLE_LIVE, "a Unit still working leaves the row live")
        _check(lane.lane.path.is_dir(), "a Unit still working leaves the lane worktree in place")
        _check(result["retired"] == [], "no row is retired on a non-terminal Unit")
        _check(any("not terminal" in advisory and instance in advisory for advisory in result["advisories"]), "one advisory names the row and that the Unit is not terminal")


def test_a_register_read_that_did_not_complete_is_an_advisory_not_a_refusal() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        lane = packet_lane(root, "refused", unique_state=False)
        instance = lane.identity["agent_instance_id"]
        result = _sweep(lane, _real_reader(root, "refused", instance, "accepted", evidence_exit=3))
        _check(lane.lifecycle() == LIFECYCLE_LIVE and lane.lane.path.is_dir(), "an unreadable register read retires nothing")
        _check(any("register read did not complete" in advisory for advisory in result["advisories"]), "the refusal is recorded as an advisory")
        _check(
            any("unit_register_unavailable" in advisory and "exited 3" in advisory for advisory in result["advisories"]),
            "the advisory carries the production reader's own named refusal, proving its code parsed the real CLI",
        )
        _check(len(result["advisories"]) == 1 and result["retired"] == [], "the caller is not failed because one row could not be proved")


def test_an_unconfigured_reader_and_an_unbound_row_are_advisories() -> None:
    with tempfile.TemporaryDirectory() as raw:
        lane = packet_lane(Path(raw), "unconfigured", unique_state=False)
        result = _sweep(lane, None)
        _check(lane.lifecycle() == LIFECYCLE_LIVE, "no configured register reader retires nothing")
        _check(any("no register reader" in advisory for advisory in result["advisories"]), "an unconfigured reader is an advisory")
    state = _state()
    _live_row(state, "agi-unbound", "operator")
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-unbound"}},
        {"assignment": None, "assignment_sha256": None},
    )
    unbound = retire_completed_lane_sessions(state, [read_managed_session(state, "agi-unbound")], directed_by=_WHO, register=None)
    _check(read_managed_session(state, "agi-unbound")["lifecycle_state"] == LIFECYCLE_LIVE, "a row with no assigned Unit is never retired")
    _check(any("no registered assignment" in advisory for advisory in unbound["advisories"]), "a row with no assigned Unit is an advisory naming that")
    empty = retire_completed_lane_sessions(state, [{"lifecycle_state": LIFECYCLE_LIVE}], directed_by=_WHO, register=None)
    _check(empty["retired"] == [] and len(empty["advisories"]) == 1, "a row naming no session is an advisory, not a crash")


def test_driving_the_entry_point_twice_leaves_the_same_end_state() -> None:
    with tempfile.TemporaryDirectory() as raw:
        root = Path(raw)
        lane = packet_lane(root, "idempotent", unique_state=False)
        instance = lane.identity["agent_instance_id"]
        reader = _real_reader(root, "idempotent", instance, "completed")
        first = _sweep(lane, reader)
        after_first = lane.lifecycle()
        second = _sweep(lane, reader)
        _check(after_first == LIFECYCLE_RETIRED and lane.lifecycle() == LIFECYCLE_RETIRED, "a second drive leaves the same end state")
        _check(first["retired"][0]["already_retired"] is False, "the first drive did the work")
        _check(second["retired"][0]["already_retired"] is True, "the second drive is retire_session's own already-retired no-op")
        _check(second["retired"][0]["unit_state"] == "" and second["advisories"] == [], "an already-retired row costs no register read and raises no advisory")
        _check(lane.disposition() == "removed", "the recorded disposition survives the re-drive")


def main() -> None:
    print("=== retire_session honest status smoke ===")
    test_a_provably_completed_unit_retires_its_lane_and_disposes_of_the_tree()
    test_a_still_working_unit_leaves_the_row_and_its_worktree_alone()
    test_a_register_read_that_did_not_complete_is_an_advisory_not_a_refusal()
    test_an_unconfigured_reader_and_an_unbound_row_are_advisories()
    test_driving_the_entry_point_twice_leaves_the_same_end_state()
    test_operator_host_retire_reports_the_host_was_not_stopped()
    test_terminating_host_reports_terminated()
    test_repeat_and_redrive_do_not_claim_a_host_action()
    test_unsupported_host_still_refuses()
    test_plugin_verb_lists_what_was_not_done()
    test_plugin_verb_retire_after_an_earlier_teardown_lists_nothing_it_left_undone()
    test_plugin_verb_still_reports_a_registration_left_after_an_earlier_terminate()
    test_plugin_verb_clean_teardown_has_nothing_left()
    test_plugin_verb_without_a_registry_says_it_did_not_look()
    test_prior_operator_outcome()
    test_observed_teardown()
    test_legacy_attempt_unknown()
    test_dead_name_repair()
    test_collision_races()
    test_collision_full_set()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
