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

import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
_ROOT = REPO_ROOT
sys.path.insert(0, str(_ROOT))
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402 - resolved at runtime by the sys.path.insert calls above; plugins/<X>/tests is not a pyright extraPath
from ananta.core.actions import _measured_action_lane as action_lane  # noqa: E402
from ananta.core.services.call_context import CallContext  # noqa: E402
from ananta.llm.agent_messaging.role_binding import AGENT_ROLE_BINDING_NAMESPACE  # noqa: E402
from ananta.services.store import Store, open_store  # noqa: E402

import agent_messaging_plugin.session_hosts as session_hosts  # noqa: E402
import agent_messaging_plugin.session_lifecycle_verbs as lifecycle  # noqa: E402
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
            label=instance,
            session_origin="registered" if host == "operator" else "spawned",
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
    # Give each session a host reference without giving it a role claim.
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


# Built from parts so a text search for the deleted name finds only live references.
_DELETED_VERB = "_".join(("retire", "completed", "lane", "sessions"))


def test_the_register_gated_lane_retirement_verb_is_gone() -> None:
    """The verb that retired a lane only on the external register's proof is deleted.

    Its pass condition (a register read of the lane's own Unit terminal) cannot
    be true on a solet without that register, so nothing may declare it: no
    platform process, no processor-policy row, no measured-action row, no
    knowledge-base process document, and no lifecycle verb.
    """
    declared = {metadata.name for member in vars(AgentMessagingPlugin).values() if (metadata := getattr(member, "_platform_process_metadata", None)) is not None}
    _check("retire_session" in declared, "setup: the table read holds the retire_session process it must keep")
    _check(_DELETED_VERB not in declared, "the plugin declares no platform process by that name")
    plugin = object.__new__(AgentMessagingPlugin)
    _check(_DELETED_VERB not in plugin.get_edge_process_definitions(), "no processor-policy row names it")
    _check(f"plugin::agent_messaging_plugin::{_DELETED_VERB}" not in action_lane.PROCESS_KEYS, "no measured-action lane row names it")
    _check(not hasattr(lifecycle, _DELETED_VERB) and _DELETED_VERB not in lifecycle.__all__, "the lifecycle verbs define and export nothing by that name")
    document = REPO_ROOT / "plugins" / "agent_messaging_plugin" / "knowledge_base" / "processes" / f"{_DELETED_VERB}.json"
    _check(not document.exists(), "no knowledge-base process document describes it")


def main() -> None:
    print("=== retire_session honest status smoke ===")
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
    test_the_register_gated_lane_retirement_verb_is_gone()
    test_collision_races()
    test_collision_full_set()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
