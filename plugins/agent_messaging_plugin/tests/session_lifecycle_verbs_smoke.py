#!/usr/bin/env python3
"""Unit smoke for the L1 verb bodies (``session_lifecycle_verbs.py``):
``spawn_session``, ``list_sessions``, ``session_status``, ``clear_session``,
``compact_session``, ``drive_session``, ``terminate_session``,
``retire_session``, ``report_alive``. Against ``RealShapeState`` (real
provider ActionResult envelopes).

``clear_session``/``compact_session`` are tested against a trivial FAKE
``HostDriver``/``DriverChannel`` pair, monkeypatched into
``session_hosts._REGISTRY`` under a test-only host name — isolates the
VERB's own logic (terminal-state guard, park transition, error-token
mapping) from the real ``headless`` driver's subprocess machinery, which
has its own dedicated smoke (``headless_adapter_smoke.py``).

Run:
    .venv/bin/python3 plugins/agent_messaging_plugin/tests/session_lifecycle_verbs_smoke.py
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO_ROOT / "ananta" / "src"))
sys.path.insert(0, str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

if TYPE_CHECKING:
    from ananta.interfaces.state_management_interface import StateManagementInterface

from _real_state_fake import RealShapeState  # noqa: E402
from ananta.llm.agent_messaging.role_binding import (  # noqa: E402
    AGENT_ROLE_BINDING_NAMESPACE,
    TABLE_ROLE_BINDING,
    role_binding_external_id,
)
from ananta.llm.agent_messaging.state_results import require_records  # noqa: E402

import agent_messaging_plugin.session_hosts as session_hosts  # noqa: E402
from agent_messaging_plugin.headless_adapter import (  # noqa: E402
    _WORKER_INJECTED_HOOK_FILENAMES,
    HeadlessHostDriver,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    CONDITION_SESSION_TERMINAL,
    LIFECYCLE_IDLE,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    list_managed_sessions,
    read_managed_session,
    set_host_ref,
    transition_lifecycle_state,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    DEFAULT_REPORT_BY_SECONDS,
    ArmSessionDependencyRequest,
    SpawnSessionRequest,
    VerbError,
    arm_session_dependency,
    clear_session,
    compact_session,
    drive_session,
    list_sessions,
    report_alive,
    retire_session,
    session_status,
    terminate_session,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    spawn_session as native_spawn_session,
)
from agent_messaging_plugin.session_role_claim_store import (  # noqa: E402
    CardinalityGatedClaim,
    read_session_role_claim,
    win_cardinality_gate,
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


def _state() -> StateManagementInterface:
    return cast("StateManagementInterface", RealShapeState())


def spawn_session(state: StateManagementInterface, req: SpawnSessionRequest) -> dict[str, Any]:
    return native_spawn_session(state, req)


_WORKSPACE = tempfile.mkdtemp(prefix="lifecycle-verbs-smoke-workspace-")


def _spawn_req(**overrides: object) -> SpawnSessionRequest:
    """A generic request on the operator host; each case names what it varies."""
    base: dict[str, object] = {
        "model": "fixture-model",
        "effort": "high",
        "workspace": _WORKSPACE,
        "first_message": "fixture first message",
        "host": "operator",
        "directed_by": "operator:none",
    }
    base.update(overrides)
    return SpawnSessionRequest(**base)  # type: ignore[arg-type]


def test_spawn_errors() -> None:
    state = _state()
    for kwargs, code in [
        ({"model": ""}, "generic_spawn_field_required"),
        ({"effort": " "}, "generic_spawn_field_required"),
        ({"first_message": ""}, "generic_spawn_field_required"),
        ({"workspace": ""}, "generic_workspace_invalid"),
        ({"workspace": "relative/dir"}, "generic_workspace_invalid"),
        ({"workspace": "/nonexistent/lifecycle-verbs-smoke"}, "generic_workspace_invalid"),
    ]:
        raised_code = None
        try:
            spawn_session(state, _spawn_req(**kwargs))
        except VerbError as exc:
            raised_code = exc.code
        _check(raised_code == code, f"spawn_session({kwargs}) -> {code} (got {raised_code!r})")
    _check(list_sessions(state, {"host": "operator"})["sessions"] == [], "an admission refusal writes no ledger row")

    # host_mechanism_missing: "screen" is explicitly documented unsupported
    # (skeleton §2) and never gets a driver -- "tmux" no longer works for
    # this case now that D2 registered it (session_lifecycle_verbs_smoke.py
    # must track the registry, not assume any one name stays unregistered).
    missing = None
    try:
        spawn_session(state, _spawn_req(host="screen"))
    except VerbError as exc:
        missing = exc.code
    _check(
        missing == "host_mechanism_missing",
        "spawn_session(host='screen') -> host_mechanism_missing",
    )

    # headless and tmux (D2) are BOTH registered now, but fail closed
    # (host_cannot_spawn) in an unconfigured environment (no
    # SOLET_NAME / permission mode / .mcp.json) -- config remedies,
    # never a silent bypass-permissions default.
    headless_cannot_spawn = None
    os.environ.pop("FLEET_HEADLESS_PERMISSION_MODE", None)
    try:
        spawn_session(state, _spawn_req(host="headless", label="label-headless-cfg"))
    except VerbError as exc:
        headless_cannot_spawn = exc.code
    _check(
        headless_cannot_spawn == "host_cannot_spawn",
        f"spawn_session(host='headless') in an unconfigured environment -> host_cannot_spawn (got {headless_cannot_spawn!r})",
    )

    tmux_cannot_spawn = None
    try:
        spawn_session(state, _spawn_req(host="tmux", label="label-tmux-cfg"))
    except VerbError as exc:
        tmux_cannot_spawn = exc.code
    _check(
        tmux_cannot_spawn == "host_cannot_spawn",
        f"spawn_session(host='tmux') in an unconfigured environment -> host_cannot_spawn (got {tmux_cannot_spawn!r})",
    )

    # host_cannot_spawn: operator is degenerate, and the ledger row is left
    # terminated (half-failed-spawn cleanup), not stuck in 'spawning'.
    cannot_spawn = None
    try:
        spawn_session(state, _spawn_req(host="operator"))
    except VerbError as exc:
        cannot_spawn = exc.code
    _check(
        cannot_spawn == "host_cannot_spawn",
        "spawn_session(host='operator') -> host_cannot_spawn",
    )
    rows = list_sessions(state, {"host": "operator"})["sessions"]
    _check(
        len(rows) == 1 and rows[0]["lifecycle_state"] == LIFECYCLE_TERMINATED,
        "a known-doomed dispatch (host_cannot_spawn) transitions the ledger row straight to terminated, not left stuck in 'spawning'",
    )
    headless_rows = list_managed_sessions(state, {"label": "label-headless-cfg"})
    _check(
        len(headless_rows) == 1 and headless_rows[0]["end_reason"] == "failed",
        "a host that could not start the session ends its record with end_reason failed",
    )


def test_local_name_defaults_to_the_label_then_the_instance_id() -> None:
    """The local name is the caller's explicit local_name, else the label, else the instance id."""
    state = _state()
    _install_fake_hosts()
    try:
        labelled = spawn_session(state, _spawn_req(host=_TEST_HOST, label="label-named"))
        unlabelled = spawn_session(state, _spawn_req(host=_TEST_HOST))
        explicit = spawn_session(state, _spawn_req(host=_TEST_HOST, label="label-ignored", local_name="explicit-name"))
        names = {key: read_managed_session(state, str(result["agent_instance_id"]))["local_name"] for key, result in (("labelled", labelled), ("unlabelled", unlabelled), ("explicit", explicit))}
        _check(names["labelled"] == "label-named", "a spawn with a label and no local_name is named for the label")
        _check(names["unlabelled"] == unlabelled["agent_instance_id"], "a spawn with neither is named for its instance id")
        _check(names["explicit"] == "explicit-name", "an explicit local_name wins over the label")
    finally:
        _remove_fake_hosts()


def test_spawn_refuses_second_session_under_a_live_local_name() -> None:
    """W6 OPERATOR RULING: refused loudly, never a suffix, never an eviction."""
    state = _state()
    _install_fake_hosts()
    try:
        spawn_session(
            state,
            _spawn_req(
                host=_TEST_HOST,
                local_name="Git-Controller",
            ),
        )
        code, message = None, ""
        try:
            spawn_session(
                state,
                _spawn_req(
                    host=_TEST_HOST,
                    local_name="Git-Controller",
                ),
            )
        except VerbError as exc:
            code, message = exc.code, exc.message
        _check(code == "local_name_already_held", f"the second spawn is refused (got {code!r})")
        _check(
            "Git-Controller" in message and "terminate_session" in message,
            "the refusal names the contested name AND the verb that frees it",
        )
        _check("agi-" in message, "and identifies the incumbent by agent_instance_id")
    finally:
        _remove_fake_hosts()


def test_spawn_allows_replacement_after_incumbent_terminated() -> None:
    """The other half of terminate-then-spawn: the refusal must not strand the
    name. This is also why the gate keys on a NON-TERMINAL row — a crashed
    worker swept to 'terminated' frees its name with no operator in the loop,
    the same crash-succession posture the claim path keeps for dead holders."""
    state = _state()
    _install_fake_hosts()
    try:
        first = spawn_session(
            state,
            _spawn_req(
                host=_TEST_HOST,
                local_name="Git-Controller",
            ),
        )
        terminate_session(
            state,
            agent_instance_id=first["agent_instance_id"],
            directed_by="operator:none",
        )
        replaced = spawn_session(
            state,
            _spawn_req(
                host=_TEST_HOST,
                local_name="Git-Controller",
            ),
        )
        _check(
            replaced["agent_instance_id"] != first["agent_instance_id"],
            "once the incumbent is terminated the replacement spawns normally",
        )
    finally:
        _remove_fake_hosts()


def test_spawn_does_not_claim_the_role_binding() -> None:
    """W6 OPERATOR RULING: spawning must NOT claim the durable binding —
    claiming adds a name and never releases the incumbent's, so an automatic
    claim at spawn would evict whoever holds the role."""
    state = _state()
    _install_fake_hosts()
    try:
        result = spawn_session(
            state,
            _spawn_req(
                host=_TEST_HOST,
                local_name="Git-Controller",
            ),
        )
        rows = require_records(
            state.query_state(
                AGENT_ROLE_BINDING_NAMESPACE,
                {
                    "table": TABLE_ROLE_BINDING,
                    "filters": {"external_id": role_binding_external_id("Git-Controller")},
                },
            ),
        )
        _check(rows == [], "no role_binding row exists after the spawn -- nothing was claimed")
        row = read_managed_session(state, result["agent_instance_id"])
        _check(
            row.get("local_name") == "Git-Controller",
            "the ledger records local_name, which is what the worker is actually named",
        )
        _check((row["session_origin"], row["spawned_by_instance_id"]) == ("spawned", ""), "a generic spawn with no caller records its origin and empty lineage")
    finally:
        _remove_fake_hosts()


def test_list_and_status() -> None:
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-x",
            host="operator",
            session_origin="registered",
            workspace="/tmp/session-status-workspace",
        ),
    )
    private_fields = {"coordination_state", "dispatch_id", "brief_ref", "assignment", "provisioning_mode"}
    row = session_status(state, "agi-x")
    _check(row["lifecycle_state"] == LIFECYCLE_SPAWNING, "session_status returns the ledger row")
    _check(private_fields.isdisjoint(row), "session status omits private migration inputs and dispatch projections")

    not_found = None
    try:
        session_status(state, "agi-nonexistent")
    except VerbError as exc:
        not_found = exc.code
    _check(not_found == "session_not_found", "session_status(unknown) -> session_not_found")

    listed = list_sessions(state, {"host": "operator"})["sessions"]
    _check(len(listed) == 1, "list_sessions filters by host")
    _check(private_fields.isdisjoint(listed[0]), "the session roster omits private migration inputs and dispatch projections")
    _check(all((item["session_origin"], item["workspace"]) == ("registered", "/tmp/session-status-workspace") for item in (row, listed[0])), "status and roster preserve the session's origin and workspace")
    transition_lifecycle_state(
        state,
        agent_instance_id="agi-x",
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_TERMINATED,
        directed_by="test",
    )
    terminal = session_status(state, "agi-x")
    _check(terminal["ledger_state"] == LIFECYCLE_TERMINATED and private_fields.isdisjoint(terminal), "terminal status reports the recorded lifecycle without private inputs or dispatch projections")


_TEST_HOST = "test-driver-host"
_TEST_HOST_NO_CHANNEL = "test-driver-host-no-channel"
_TEST_HOST_RUNTIME_ROUTING = "test-driver-runtime-routing"


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


class _FakeVerifyingChannel(_FakeChannel):
    def verify_driven(self, text: str) -> bool:
        return text in self.sent


class _FakeParkInterruptingVerifyingChannel(_FakeVerifyingChannel):
    def __init__(self) -> None:
        super().__init__()
        self.interrupts = 0

    def interrupt_park(self) -> str:
        self.interrupts += 1
        return "interrupted_park"


class _FakeDriverWithChannel:
    def __init__(self) -> None:
        self.channel = _FakeChannel()

    def spawn(self, spec: object) -> str:
        del spec
        return "fake-host-ref"

    def alive(self, host_ref: str) -> bool:
        del host_ref
        return True

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del host_ref, grace_seconds

    def driver_channel(self, host_ref: str) -> _FakeChannel:
        del host_ref
        return self.channel

    def capability_report(self) -> dict[str, object]:
        return {}

    def verify_config(self) -> list[str]:
        return []


class _FakeDriverNoChannel:
    def spawn(self, spec: object) -> str:
        del spec
        return "fake-host-ref"

    def alive(self, host_ref: str) -> bool:
        del host_ref
        return True

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del host_ref, grace_seconds

    def driver_channel(self, host_ref: str) -> None:
        del host_ref
        return None

    def capability_report(self) -> dict[str, object]:
        return {}

    def verify_config(self) -> list[str]:
        return []


def _install_fake_hosts() -> None:
    session_hosts._REGISTRY[_TEST_HOST] = _FakeDriverWithChannel()  # noqa: SLF001 -- test-only monkeypatch  # pyright: ignore[reportArgumentType]
    session_hosts._REGISTRY[_TEST_HOST_NO_CHANNEL] = _FakeDriverNoChannel()  # noqa: SLF001


def _remove_fake_hosts() -> None:
    session_hosts._REGISTRY.pop(_TEST_HOST, None)  # noqa: SLF001
    session_hosts._REGISTRY.pop(_TEST_HOST_NO_CHANNEL, None)  # noqa: SLF001


def test_omitted_host_resolves_to_tmux_before_spawn() -> None:
    """A raw spawn with no host must select tmux before it allocates a lane."""
    state = _state()
    key = (session_hosts.DEFAULT_AGENT_RUNTIME, "tmux")
    original_driver = session_hosts._REGISTRY[key]  # noqa: SLF001 -- test seam
    previous_env_host = os.environ.pop("FLEET_SESSION_HOST", None)
    fake_driver = _FakeDriverWithChannel()
    session_hosts._REGISTRY[key] = fake_driver  # noqa: SLF001 -- test seam  # pyright: ignore[reportArgumentType]
    try:
        result = spawn_session(
            state,
            _spawn_req(
                host=None,
                visibility="tmux",
                label="label-omitted-host-default",
            ),
        )
        _check(
            result["host"] == "tmux",
            "spawn_session(host omitted) resolves to tmux before any host side effect",
        )
        _check(
            bool(fake_driver.channel.sent),
            "the tmux-resolved fake driver receives the bootstrap turn",
        )
        terminate_session(
            state,
            agent_instance_id=str(result["agent_instance_id"]),
            directed_by="operator:none",
        )
    finally:
        session_hosts._REGISTRY[key] = original_driver  # noqa: SLF001 -- test seam
        if previous_env_host is None:
            os.environ.pop("FLEET_SESSION_HOST", None)
        else:
            os.environ["FLEET_SESSION_HOST"] = previous_env_host


def test_visibility_host_consistency() -> None:
    """Visibility must describe the resolved spawn host, not just the row."""
    state = _state()
    cases = (("visible", "headless"), ("tmux", "headless"), ("headless", "tmux"))
    for visibility, host in cases:
        code = _spawn_error_code(
            state,
            _spawn_req(
                host=host,
                visibility=visibility,
                label=f"label-visibility-{visibility}-{host}",
            ),
        )
        _check(
            code == "visibility_host_mismatch",
            f"visibility={visibility!r} with host={host!r} refuses before spawn",
        )
    aligned = _spawn_error_code(
        state,
        _spawn_req(host="tmux", visibility="visible", label="label-visible-tmux-cfg"),
    )
    _check(
        aligned == "host_cannot_spawn",
        "visibility='visible' with host='tmux' clears consistency validation",
    )
    previous_env_host = os.environ.pop("FLEET_SESSION_HOST", None)
    try:
        omitted_headless = _spawn_error_code(
            state,
            _spawn_req(host=None, visibility="headless", label="label-omitted-headless"),
        )
    finally:
        if previous_env_host is None:
            os.environ.pop("FLEET_SESSION_HOST", None)
        else:
            os.environ["FLEET_SESSION_HOST"] = previous_env_host
    _check(
        omitted_headless == "visibility_host_mismatch",
        "host omission cannot turn an asserted headless visibility into a tmux spawn",
    )


def _spawn_error_code(state: StateManagementInterface, req: SpawnSessionRequest) -> str | None:
    try:
        spawn_session(state, req)
    except VerbError as exc:
        return exc.code
    return None


# -- session_terminal fire+deliver (2026-08-04, acceptance Test C fix slice) --

_TEST_HOST_RAISING = "test-driver-host-raising-channel"


class _FakeChannelRaising:
    def send(self, text: str) -> None:
        del text
        raise RuntimeError("driver channel exploded")


class _FakeDriverRaisingChannel:
    def spawn(self, spec: object) -> str:
        del spec
        return "fake-host-ref"

    def alive(self, host_ref: str) -> bool:
        del host_ref
        return True

    def terminate(self, host_ref: str, grace_seconds: int) -> None:
        del host_ref, grace_seconds

    def driver_channel(self, host_ref: str) -> _FakeChannelRaising:
        del host_ref
        return _FakeChannelRaising()

    def capability_report(self) -> dict[str, object]:
        return {}

    def verify_config(self) -> list[str]:
        return []


def _arm_session_terminal(
    state: StateManagementInterface,
    *,
    waiter_instance_id: str,
    dying_instance_id: str,
) -> None:
    armed = arm_session_dependency(
        state,
        ArmSessionDependencyRequest(
            waiter_instance_id=waiter_instance_id,
            condition_kind=CONDITION_SESSION_TERMINAL,
            condition_ref=dying_instance_id,
        ),
    )
    _check(armed.get("armed") is True, "the session_terminal edge armed cleanly via the real verb")


def test_terminate_fires_and_delivers_session_terminal_edge() -> None:
    """Coordinator-seat ruling (1), 2026-08-04: terminate_session now OWNS firing +
    best-effort delivering armed session_terminal edges. Named failing
    mutation: removing the drive_on_delivery call inside
    _fire_session_terminal_dependencies reds ONLY the channel assertion
    below (session_terminal_edges_fired stays 1) -- proving fire and
    deliver are independently exercised, not one assertion covering both."""
    _install_fake_hosts()
    try:
        state = _state()
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-dying-1",
                host="operator",
            ),
        )
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-dying-1",
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="operator:none",
        )
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-waiter-1",
                host=_TEST_HOST,
                session_origin="spawned",
            ),
        )
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-waiter-1",
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="operator:none",
        )
        _arm_session_terminal(
            state,
            waiter_instance_id="agi-waiter-1",
            dying_instance_id="agi-dying-1",
        )
        driver = session_hosts._REGISTRY[_TEST_HOST]  # noqa: SLF001

        result = terminate_session(state, agent_instance_id="agi-dying-1", directed_by="operator:none")
        _check(result["already_terminal"] is False, "the dying session transitions to terminated")
        _check(
            result.get("session_terminal_edges_fired") == 1,
            f"terminate_session reports exactly 1 edge fired (got {result})",
        )
        _check(
            len(driver.channel.sent) == 1 and "drain peer_inbox" in driver.channel.sent[0],
            f"the waiter's driver channel receives exactly one drive-on-delivery notice (got {driver.channel.sent})",
        )
    finally:
        _remove_fake_hosts()


def test_retire_composes_terminate_no_double_delivery() -> None:
    """Coordinator-seat ruling (2): retire_session composes terminate_session as its
    OWN first step and must not ALSO fire/deliver a second time. Named
    failing mutation: restoring retire_session's own separate
    _fire_session_terminal_dependencies call (the pre-fix design) would
    double the channel.sent count (edges already fired-guarded server-side,
    so it would not double-count `dependencies_fired`, but a NEW edge armed
    between the two calls in the old design could have double-delivered --
    this test pins the single-call-site invariant directly)."""
    _install_fake_hosts()
    try:
        state = _state()
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-dying-2",
                host="operator",
            ),
        )
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-dying-2",
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="operator:none",
        )
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-waiter-2",
                host=_TEST_HOST,
                session_origin="spawned",
            ),
        )
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-waiter-2",
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="operator:none",
        )
        _arm_session_terminal(
            state,
            waiter_instance_id="agi-waiter-2",
            dying_instance_id="agi-dying-2",
        )
        driver = session_hosts._REGISTRY[_TEST_HOST]  # noqa: SLF001

        result = retire_session(state, agent_instance_id="agi-dying-2", directed_by="operator:none")
        _check(
            result["already_retired"] is False and result["dependencies_fired"] == 1 and result["host_action"] == "none_available",
            f"retire_session reports exactly 1 edge fired via composition, and that the operator-hosted process was not stopped (got {result})",
        )
        _check(
            len(driver.channel.sent) == 1,
            f"the waiter's channel receives exactly ONE notice, not two, under composition (got {driver.channel.sent})",
        )
    finally:
        _remove_fake_hosts()


def test_terminate_delivery_fault_is_contained() -> None:
    """Position 4 (containment, carried from slice 1): a raising driver
    channel must never fail terminate_session itself -- the fire (ledger
    write) already succeeded before delivery was even attempted. Named
    failing mutation: removing drive_on_delivery's own internal try/except
    (session_lifecycle_verbs.py) would let this RuntimeError propagate and
    red this entire test with an unhandled exception."""
    session_hosts._REGISTRY[_TEST_HOST_RAISING] = _FakeDriverRaisingChannel()  # noqa: SLF001  # pyright: ignore[reportArgumentType]
    try:
        state = _state()
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-dying-3",
                host="operator",
            ),
        )
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-dying-3",
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="operator:none",
        )
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-waiter-3",
                host=_TEST_HOST_RAISING,
                session_origin="spawned",
            ),
        )
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-waiter-3",
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="operator:none",
        )
        _arm_session_terminal(
            state,
            waiter_instance_id="agi-waiter-3",
            dying_instance_id="agi-dying-3",
        )
        raised = False
        result: dict[str, Any] = {}
        try:
            result = terminate_session(state, agent_instance_id="agi-dying-3", directed_by="operator:none")
        except Exception:  # noqa: BLE001 -- the whole point is it must NOT raise
            raised = True
        _check(not raised, "a raising waiter driver channel does not propagate out of terminate_session")
        _check(
            result.get("already_terminal") is False and result.get("session_terminal_edges_fired") == 1,
            f"terminate_session still succeeds and still reports the edge fired (got {result})",
        )
    finally:
        session_hosts._REGISTRY.pop(_TEST_HOST_RAISING, None)  # noqa: SLF001


def test_already_terminal_catches_orphaned_edge() -> None:
    """Coordinator-seat ruling (3), included: a repeat terminate_session call on an
    already-terminal row still sweeps for an edge armed AFTER the session
    died -- the success path only runs once per transition and could never
    catch this orphan class otherwise. Named failing mutation: removing the
    firing call from the already_terminal branch reds this test's
    session_terminal_edges_fired/channel assertions while leaving the
    already_terminal=True assertion green (proving the two are
    independently exercised)."""
    _install_fake_hosts()
    try:
        state = _state()
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-dying-4",
                host="operator",
            ),
        )
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-dying-4",
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="operator:none",
        )
        # Terminate FIRST, with no edge armed yet -- nothing to fire.
        first = terminate_session(state, agent_instance_id="agi-dying-4", directed_by="operator:none")
        _check(
            first["already_terminal"] is False and first.get("session_terminal_edges_fired") == 0,
            f"the first terminate call fires nothing -- no edge was armed yet (got {first})",
        )

        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-waiter-4",
                host=_TEST_HOST,
                session_origin="spawned",
            ),
        )
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-waiter-4",
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="operator:none",
        )
        # Arm the edge AFTER the target already died -- the orphan case.
        _arm_session_terminal(
            state,
            waiter_instance_id="agi-waiter-4",
            dying_instance_id="agi-dying-4",
        )
        driver = session_hosts._REGISTRY[_TEST_HOST]  # noqa: SLF001

        second = terminate_session(state, agent_instance_id="agi-dying-4", directed_by="operator:none")
        _check(second["already_terminal"] is True, "the repeat call still reports already_terminal")
        _check(
            second.get("session_terminal_edges_fired") == 1,
            f"the repeat call catches the orphaned edge (got {second})",
        )
        _check(
            len(driver.channel.sent) == 1,
            f"the orphaned edge's waiter is still notified (got {driver.channel.sent})",
        )
    finally:
        _remove_fake_hosts()


# -- spawn_session first-turn (phase 2 slice 6) --


def test_spawn_session_first_turn_preserves_agent_runtime() -> None:
    """Regression for the live 2026-08-22 Codex spawn failure.

    The spawn half resolved ``(agent_runtime='codex', host='headless')`` and
    created a real Codex app-server, but the immediate first-turn half rebuilt
    the driver lookup without ``agent_runtime``.  That silently selected the
    default Claude driver, which could not recognize the Codex host reference,
    and returned ``first_turn_delivered=False`` over a live stranded worker.

    The two runtime drivers deliberately disagree here: only the Codex driver
    has a channel.  Dropping runtime propagation therefore makes this fixture
    red without any real process or model turn.
    """
    state = _state()
    codex_key = (session_hosts.AGENT_RUNTIME_CODEX, _TEST_HOST_RUNTIME_ROUTING)
    claude_key = (session_hosts.AGENT_RUNTIME_CLAUDE_CODE, _TEST_HOST_RUNTIME_ROUTING)
    prior_codex = session_hosts._REGISTRY.get(codex_key)  # noqa: SLF001
    prior_claude = session_hosts._REGISTRY.get(claude_key)  # noqa: SLF001
    codex_driver = _FakeDriverWithChannel()
    session_hosts._REGISTRY[codex_key] = codex_driver  # noqa: SLF001  # pyright: ignore[reportArgumentType]
    session_hosts._REGISTRY[claude_key] = _FakeDriverNoChannel()  # noqa: SLF001
    try:
        result = spawn_session(
            state,
            _spawn_req(
                host=_TEST_HOST_RUNTIME_ROUTING,
                agent_runtime=session_hosts.AGENT_RUNTIME_CODEX,
                label="label-codex-runtime-first-turn",
            ),
        )
        _check(
            result["first_turn_delivered"] is True,
            "Codex spawn resolves the first-turn channel through the Codex runtime driver",
        )
        _check(
            len(codex_driver.channel.sent) == 1,
            "the first turn reaches the same runtime-specific driver that performed spawn",
        )
    finally:
        if prior_codex is None:
            session_hosts._REGISTRY.pop(codex_key, None)  # noqa: SLF001
        else:
            session_hosts._REGISTRY[codex_key] = prior_codex  # noqa: SLF001
        if prior_claude is None:
            session_hosts._REGISTRY.pop(claude_key, None)  # noqa: SLF001
        else:
            session_hosts._REGISTRY[claude_key] = prior_claude  # noqa: SLF001


def test_spawn_session_first_turn_failure_is_visible_not_blocking() -> None:
    """Ordering-ruling guard (b): a first-turn delivery failure must be
    VISIBLE (surfaced in the spawn result) and must NEVER block the spawn
    itself — the row still lands, just with first_turn_delivered=False."""
    state = _state()
    _install_fake_hosts()
    try:
        result = spawn_session(
            state,
            _spawn_req(host=_TEST_HOST_NO_CHANNEL, label="label-first-turn-no-channel"),
        )
        _check(
            result["first_turn_delivered"] is False,
            "a host with no driver channel reports first_turn_delivered=False",
        )
        _check(
            result["first_turn_error"] != "",
            f"RED-vs-GREEN: the failure is surfaced in the spawn result, never silent (got first_turn_error={result['first_turn_error']!r})",
        )
        _check(
            result["lifecycle_state"] == LIFECYCLE_SPAWNING,
            "the spawn itself is NOT blocked by a first-turn delivery failure",
        )
    finally:
        _remove_fake_hosts()


def test_spawn_session_first_turn_send_raising_is_contained() -> None:
    state = _state()
    session_hosts._REGISTRY[_TEST_HOST_RAISING] = _FakeDriverRaisingChannel()  # noqa: SLF001  # pyright: ignore[reportArgumentType]
    try:
        result = spawn_session(
            state,
            _spawn_req(host=_TEST_HOST_RAISING, label="label-first-turn-raising"),
        )
        _check(
            result["first_turn_delivered"] is False,
            "a driver channel that raises on send() reports first_turn_delivered=False",
        )
        _check(
            "driver channel exploded" in result["first_turn_error"],
            f"the underlying exception text is surfaced (got {result['first_turn_error']!r})",
        )
        _check(
            result["lifecycle_state"] == LIFECYCLE_SPAWNING,
            "the spawn itself is NOT blocked by a raising driver channel",
        )
    finally:
        session_hosts._REGISTRY.pop(_TEST_HOST_RAISING, None)  # noqa: SLF001


def test_clear_session_sends_and_can_park() -> None:
    _install_fake_hosts()
    try:
        state = _state()
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-clear-1",
                host=_TEST_HOST,
                session_origin="spawned",
            ),
        )
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-clear-1",
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="operator:none",
        )
        driver = session_hosts._REGISTRY[_TEST_HOST]  # noqa: SLF001

        result = clear_session(
            state,
            agent_instance_id="agi-clear-1",
            park=False,
            directed_by="operator:none",
        )
        _check(driver.channel.sent == ["/clear"], "clear_session sends '/clear' over the channel")
        _check(
            result
            == {
                "lifecycle_state": LIFECYCLE_LIVE,
                "parked": False,
                "dispatched": True,
                "cleared": None,
                "clear_verification": "unsupported_on_driver",
            },
            "clear_session(park=False) does not transition lifecycle_state",
        )
        # GAU-09: this fixture's channel has no read-back surface, so the
        # verb must say the effect is UNVERIFIED rather than imply success.
        # Asserted as a whole-dict equality on purpose -- a field silently
        # dropped from the envelope is exactly the regression that would put
        # the "success means cleared" reading back.
        _check(
            result["cleared"] is None,
            "GAU-09: a channel with no read-back surface never claims cleared=True",
        )
        _check(
            read_managed_session(state, "agi-clear-1")["lifecycle_state"] == LIFECYCLE_LIVE,
            "the ledger row is still 'live' after a non-parking clear",
        )

        parked_result = clear_session(
            state,
            agent_instance_id="agi-clear-1",
            park=True,
            directed_by="operator:none",
        )
        _check(
            driver.channel.sent == ["/clear", "/clear"],
            "clear_session(park=True) still sends '/clear' before parking",
        )
        _check(
            parked_result
            == {
                "lifecycle_state": LIFECYCLE_PARKED,
                "parked": True,
                "dispatched": True,
                "cleared": None,
                "clear_verification": "unsupported_on_driver",
            },
            "clear_session(park=True) drives live -> parked",
        )
        _check(
            read_managed_session(state, "agi-clear-1")["lifecycle_state"] == LIFECYCLE_PARKED,
            "the ledger row is now 'parked'",
        )
    finally:
        _remove_fake_hosts()


def test_compact_session_sends_no_park() -> None:
    _install_fake_hosts()
    try:
        state = _state()
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-compact-1",
                host=_TEST_HOST,
                session_origin="spawned",
            ),
        )
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-compact-1",
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="operator:none",
        )
        driver = session_hosts._REGISTRY[_TEST_HOST]  # noqa: SLF001
        result = compact_session(state, agent_instance_id="agi-compact-1")
        _check(
            driver.channel.sent == ["/compact"],
            "compact_session sends '/compact' over the channel",
        )
        _check(
            result == {"lifecycle_state": LIFECYCLE_LIVE},
            "compact_session never parks (no lifecycle transition)",
        )
    finally:
        _remove_fake_hosts()


def test_drive_session_dispatches_and_unparks() -> None:
    """Green legs + their named failing mutations: removing the
    ``channel.send(text)`` call reds "sends the text"; removing the
    parked->live transition reds "drives parked -> live"; removing the
    confirmed-drive guard from ``_rearm_report_by`` reds "unverified drive
    preserves the reporting deadline"."""
    _install_fake_hosts()
    try:
        state = _state()
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-drive-1",
                host=_TEST_HOST,
                session_origin="spawned",
            ),
        )
        driver = session_hosts._REGISTRY[_TEST_HOST]  # noqa: SLF001

        # Dispatch-at-spawn: a 'spawning' row is drivable (the brief can be
        # sent the moment spawn_session returns); the registration hook
        # still owns the spawning->live edge, so no transition here.
        report_by_before_first = read_managed_session(state, "agi-drive-1").get("report_by")
        spawning_result = drive_session(
            state,
            agent_instance_id="agi-drive-1",
            text="Brief: build the tmux driver.",
            directed_by="steward:none",
        )
        _check(
            driver.channel.sent == ["Brief: build the tmux driver."],
            "drive_session sends the text over the channel",
        )
        _check(
            spawning_result
            == {
                "lifecycle_state": LIFECYCLE_SPAWNING,
                "unparked": False,
                "dispatched": True,
                "submitted": None,
                "drive_verification": "unsupported_on_driver",
                "drive_on_delivery_detail": None,
            },
            "drive_session on a 'spawning' row dispatches without a transition",
        )
        report_by_after_first = read_managed_session(state, "agi-drive-1").get("report_by")
        _check(
            report_by_after_first == report_by_before_first,
            "an unsupported-on-driver dispatch preserves the reporting deadline",
        )

        # parked -> live: the §3.2 "new dispatch through the driver channel"
        # edge, owned by this verb alone.
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-drive-1",
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="operator:none",
        )
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-drive-1",
            from_state=LIFECYCLE_LIVE,
            to_state=LIFECYCLE_PARKED,
            directed_by="steward:none",
        )
        driver.channel = _FakeParkInterruptingVerifyingChannel()
        report_by_before_confirmed = read_managed_session(state, "agi-drive-1").get("report_by")
        unpark_result = drive_session(
            state,
            agent_instance_id="agi-drive-1",
            text="Follow-up: land it.",
            directed_by="steward:none",
        )
        _check(
            driver.channel.sent == ["Follow-up: land it."],
            "a second drive_session sends the follow-up text",
        )
        _check(
            driver.channel.interrupts == 1,
            "a parked drive interrupts its declared parked-pane channel before sending text",
        )
        _check(
            unpark_result.get("drive_on_delivery_detail") == "interrupted_park",
            "a parked drive reports the measured interrupt recovery detail",
        )
        _check(
            unpark_result
            == {
                "lifecycle_state": LIFECYCLE_LIVE,
                "unparked": True,
                "dispatched": True,
                "submitted": True,
                "drive_verification": "confirmed",
                "drive_on_delivery_detail": "interrupted_park",
            },
            "drive_session drives parked -> live (unparked=True)",
        )
        _check(
            read_managed_session(state, "agi-drive-1")["lifecycle_state"] == LIFECYCLE_LIVE,
            "the ledger row is 'live' after driving a parked session",
        )
        confirmed_row = read_managed_session(state, "agi-drive-1")
        _check(
            confirmed_row.get("report_by") != report_by_before_confirmed and confirmed_row.get("report_by_source") == "confirmed_drive",
            "only a confirmed drive re-arms report_by with confirmed-drive provenance",
        )
    finally:
        _remove_fake_hosts()


def test_drive_session_errors() -> None:
    _install_fake_hosts()
    try:
        state = _state()

        not_found = None
        try:
            drive_session(
                state,
                agent_instance_id="agi-nonexistent",
                text="work",
                directed_by="x",
            )
        except VerbError as exc:
            not_found = exc.code
        _check(not_found == "session_not_found", "drive_session(unknown) -> session_not_found")

        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-drive-err",
                host=_TEST_HOST,
            ),
        )
        driver = session_hosts._REGISTRY[_TEST_HOST]  # noqa: SLF001
        empty = None
        try:
            drive_session(state, agent_instance_id="agi-drive-err", text="  ", directed_by="x")
        except VerbError as exc:
            empty = exc.code
        _check(empty == "empty_text", "drive_session(blank text) -> empty_text")
        _check(
            driver.channel.sent == [],
            "a refused empty dispatch never touches the channel",
        )

        transition_lifecycle_state(
            state,
            agent_instance_id="agi-drive-err",
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_TERMINATED,
            directed_by="operator:none",
        )
        conflict = None
        try:
            drive_session(state, agent_instance_id="agi-drive-err", text="work", directed_by="x")
        except VerbError as exc:
            conflict = exc.code
        _check(
            conflict == "lifecycle_state_conflict",
            "drive_session on a terminated row -> lifecycle_state_conflict",
        )

        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-drive-nochan",
                host=_TEST_HOST_NO_CHANNEL,
                session_origin="spawned",
            ),
        )
        unsupported = None
        try:
            drive_session(
                state,
                agent_instance_id="agi-drive-nochan",
                text="work",
                directed_by="x",
            )
        except VerbError as exc:
            unsupported = exc.code
        _check(
            unsupported == "unsupported_on_host",
            "drive_session on a host with no driver channel -> unsupported_on_host",
        )
    finally:
        _remove_fake_hosts()


def test_clear_and_compact_errors() -> None:
    _install_fake_hosts()
    try:
        state = _state()

        not_found = None
        try:
            clear_session(state, agent_instance_id="agi-nonexistent", park=False, directed_by="x")
        except VerbError as exc:
            not_found = exc.code
        _check(not_found == "session_not_found", "clear_session(unknown) -> session_not_found")

        not_found_compact = None
        try:
            compact_session(state, agent_instance_id="agi-nonexistent")
        except VerbError as exc:
            not_found_compact = exc.code
        _check(
            not_found_compact == "session_not_found",
            "compact_session(unknown) -> session_not_found",
        )

        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-terminal",
                host=_TEST_HOST,
            ),
        )
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-terminal",
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_TERMINATED,
            directed_by="operator:none",
        )
        conflict = None
        try:
            clear_session(state, agent_instance_id="agi-terminal", park=False, directed_by="x")
        except VerbError as exc:
            conflict = exc.code
        _check(
            conflict == "lifecycle_state_conflict",
            "clear_session on a terminated row -> lifecycle_state_conflict",
        )
        compact_conflict = None
        try:
            compact_session(state, agent_instance_id="agi-terminal")
        except VerbError as exc:
            compact_conflict = exc.code
        _check(
            compact_conflict == "lifecycle_state_conflict",
            "compact_session on a terminated row -> lifecycle_state_conflict",
        )

        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-no-channel",
                host=_TEST_HOST_NO_CHANNEL,
                session_origin="spawned",
            ),
        )
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-no-channel",
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="operator:none",
        )
        unsupported = None
        try:
            clear_session(state, agent_instance_id="agi-no-channel", park=False, directed_by="x")
        except VerbError as exc:
            unsupported = exc.code
        _check(
            unsupported == "unsupported_on_host",
            "clear_session on a host with no driver channel -> unsupported_on_host",
        )

        unsupported_operator = None
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-operator",
                host="operator",
                session_origin="registered",
            ),
        )
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-operator",
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="operator:none",
        )
        try:
            compact_session(state, agent_instance_id="agi-operator")
        except VerbError as exc:
            unsupported_operator = exc.code
        _check(
            unsupported_operator == "unsupported_on_host",
            "compact_session on the real degenerate 'operator' driver -> unsupported_on_host",
        )
    finally:
        _remove_fake_hosts()


def test_terminate_idempotent() -> None:
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-t",
            host="operator",
        ),
    )
    transition_lifecycle_state(
        state,
        agent_instance_id="agi-t",
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE,
        directed_by="operator:none",
    )
    first = terminate_session(state, agent_instance_id="agi-t", directed_by="operator:none")
    _check(first["already_terminal"] is False, "first terminate_session lands the transition")
    second = terminate_session(state, agent_instance_id="agi-t", directed_by="operator:none")
    _check(
        second["already_terminal"] is True,
        "a second terminate_session is idempotent (already_terminal)",
    )

    not_found = None
    try:
        terminate_session(state, agent_instance_id="agi-none", directed_by="operator:none")
    except VerbError as exc:
        not_found = exc.code
    _check(not_found == "session_not_found", "terminate_session(unknown) -> session_not_found")

    # Pin the operator-host decision explicitly (2026-08-03/04 Dawn ruling):
    # every normal peer-registered fleet session is 'operator'-hosted and
    # was never dispatched through spawn_session, so the degenerate
    # driver's terminate() always raises HostCannotSpawnError ("I didn't
    # spawn this, I can't kill it") -- that MUST be tolerated as "no host
    # action available", not propagated as a verb failure, or no
    # operator-hosted row could ever reach 'terminated' (wedging
    # session_sweep.sweep_lane_closed_dependencies for any lane touched by
    # a non-headless session). test_terminate_idempotent above already
    # exercises this path (host="operator") and passing IS the pin; this
    # assertion just makes the "why" explicit for a future reader.
    _check(
        first["lifecycle_state"] == LIFECYCLE_TERMINATED and not first["already_terminal"],
        "an operator-hosted row still reaches 'terminated' without the verb raising -- the degenerate driver's HostCannotSpawnError is tolerated, not propagated",
    )


def _real_short_lived_popen_fn(*_a: object, **_k: object) -> subprocess.Popen[str]:
    """Ignores the incoming cmd/env entirely -- spawns a real, harmless,
    short-lived child so the test exercises actual OS-level kill mechanics
    without ever invoking the real ``claude`` binary (mirrors
    ``headless_adapter_smoke.py``'s pattern)."""
    return subprocess.Popen(  # noqa: S603 -- fixed harmless argv, test-only
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def test_terminate_session_kills_the_real_headless_process() -> None:
    """2026-08-03/04 Dawn ruling, on the live e2e's finding: retire_session
    returned 'retired' over a process that kept running 12+ seconds later.
    terminate_session (session_lifecycle_verbs.py) now resolves the row's
    host driver and calls its REAL terminate() before the ledger write --
    this asserts the actual OS process dies, not just that the verb
    returns ok (a green here must fail if the driver.terminate() call is
    ever dropped again -- 'a green lies four ways', name the failing
    mutation). Swaps a real HeadlessHostDriver (harmless popen_fn, no real
    claude binary) into session_hosts._REGISTRY['headless'] for the
    duration of the test."""
    with tempfile.TemporaryDirectory() as tmp:
        mcp_config = Path(tmp) / ".mcp.json"
        mcp_config.write_text("{}")
        # R4 Package C (2026-08-10): populate rung 1 so the worker-hook
        # resolution ladder resolves -- matching a real dev checkout's own
        # shape, same fixture pattern as headless_adapter_smoke.py's own.
        hooks_dir = Path(tmp) / ".claude" / "hooks"
        hooks_dir.mkdir(parents=True, exist_ok=True)
        for _hook_name in _WORKER_INJECTED_HOOK_FILENAMES:
            (hooks_dir / _hook_name).write_text("#!/usr/bin/env python3\n")
        real_driver = HeadlessHostDriver(
            claude_bin=sys.executable,
            solet_name="testhom",
            permission_mode="bypassPermissions",
            mcp_config_path=mcp_config,
            cwd=Path(tmp),
            popen_fn=_real_short_lived_popen_fn,
        )
        prior = session_hosts._REGISTRY.get("headless")  # noqa: SLF001 -- test-only monkeypatch
        session_hosts._REGISTRY["headless"] = real_driver  # noqa: SLF001
        try:
            state = _state()
            agent_instance_id = "agi-real-reap"
            insert_managed_session(
                state,
                ManagedSessionSpec(
                    agent_instance_id=agent_instance_id,
                    host="headless",
                ),
            )
            transition_lifecycle_state(
                state,
                agent_instance_id=agent_instance_id,
                from_state=LIFECYCLE_SPAWNING,
                to_state=LIFECYCLE_LIVE,
                directed_by="operator:none",
            )
            host_ref = real_driver.spawn(
                {"agent_instance_id": agent_instance_id},
            )
            set_host_ref(state, agent_instance_id=agent_instance_id, host_ref=host_ref)
            _check(
                real_driver.alive(host_ref),
                "precondition: the real spawned process is alive before terminate_session",
            )

            result = terminate_session(
                state,
                agent_instance_id=agent_instance_id,
                directed_by="operator:none",
                grace_seconds=2,
            )
            _check(
                result["lifecycle_state"] == LIFECYCLE_TERMINATED,
                "terminate_session lands the ledger transition",
            )

            deadline = time.monotonic() + 5.0
            dead = False
            while time.monotonic() < deadline:
                if not real_driver.alive(host_ref):
                    dead = True
                    break
                time.sleep(0.1)
            _check(
                dead,
                "terminate_session actually killed the real OS process, not just the ledger row",
            )
        finally:
            if prior is not None:
                session_hosts._REGISTRY["headless"] = prior  # noqa: SLF001
            else:
                session_hosts._REGISTRY.pop("headless", None)  # noqa: SLF001


def test_retire_idempotent_and_redrivable() -> None:
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-r",
            host="operator",
        ),
    )
    # ManagedSessionSpec has no agent_session_id field (it arrives later, at
    # the registration hook, §3.2) — set it directly to simulate that.
    state.update_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {"table": "managed_session", "filters": {"agent_instance_id": "agi-r"}},
        {"agent_session_id": "ases-r"},
    )
    win_cardinality_gate(
        state,
        CardinalityGatedClaim(
            agent_session_id="ases-r",
            requested_role="Some-Lane",
            agent_instance_id="agi-r",
        ),
    )
    _check(
        read_session_role_claim(state, "ases-r") is not None,
        "precondition: the session_role_claim row exists before retire",
    )
    first = retire_session(state, agent_instance_id="agi-r", directed_by="operator:none")
    _check(first["already_retired"] is False, "first retire_session lands terminated -> retired")
    _check(
        read_managed_session(state, "agi-r")["lifecycle_state"] == LIFECYCLE_RETIRED,
        "the ledger row is now 'retired'",
    )
    _check(
        read_session_role_claim(state, "ases-r") is None,
        "retire_session's step 2 (populated agent_session_id path) actually prunes the session_role_claim cardinality row -- this path is only reachable with agent_session_id populated, which is exactly what the registration hook now does for every real spawn",
    )
    second = retire_session(state, agent_instance_id="agi-r", directed_by="operator:none")
    _check(
        second["already_retired"] is True,
        "re-running retire_session on an already-retired row is a no-op (re-drivable by construction, never wedged)",
    )


def test_report_alive() -> None:
    state = _state()

    def _report(status: str) -> dict[str, object]:
        return report_alive(
            state,
            agent_instance_id="agi-a",
            status=status,
            directed_by="operator:none",
        )

    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-a",
            host="operator",
        ),
    )
    transition_lifecycle_state(
        state,
        agent_instance_id="agi-a",
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE,
        directed_by="operator:none",
    )
    before = read_managed_session(state, "agi-a").get("report_by")
    result = _report("idle")
    _check(result["lifecycle_state"] == "idle", "report_alive(idle) transitions live -> idle")
    after = read_managed_session(state, "agi-a")
    _check(after["report_by"] != before, "report_alive re-arms report_by even ON a transition")

    same_status = _report("idle")
    _check(
        same_status["recovered"] is False,
        "reporting the SAME status again is a no-op transition (still re-arms, see below)",
    )
    after2 = read_managed_session(state, "agi-a")
    _check(
        after2["report_by"] != after["report_by"],
        "report_alive re-arms report_by even when NO transition occurs (the same-status branch)",
    )
    heartbeat = _report("heartbeat")
    after_heartbeat = read_managed_session(state, "agi-a")
    _check(
        heartbeat["lifecycle_state"] == LIFECYCLE_IDLE and after_heartbeat["lifecycle_state"] == LIFECYCLE_IDLE,
        "a passive heartbeat preserves an explicit idle status",
    )
    _check(
        after_heartbeat.get("last_heartbeat_at") is not None and after_heartbeat.get("report_by") == after2.get("report_by"),
        "a passive heartbeat records liveness without rearming report_by",
    )
    later = _report("working")
    _check(
        later["lifecycle_state"] == LIFECYCLE_LIVE and read_managed_session(state, "agi-a").get("status_source") == "explicit_self_report",
        "a later explicit working report supersedes idle with explicit provenance",
    )

    # overdue recovery
    transition_lifecycle_state(
        state,
        agent_instance_id="agi-a",
        from_state=LIFECYCLE_LIVE,
        to_state=LIFECYCLE_OVERDUE,
        directed_by="sweep:none",
    )
    recovered = _report("working")
    _check(recovered["recovered"] is True, "a late report from 'overdue' is flagged recovered=True")
    _check(recovered["lifecycle_state"] == LIFECYCLE_LIVE, "overdue -> live on a 'working' report")

    # parked/terminal conflict
    transition_lifecycle_state(
        state,
        agent_instance_id="agi-a",
        from_state=LIFECYCLE_LIVE,
        to_state="parked",
        directed_by="steward:none",
    )
    conflict = None
    try:
        _report("working")
    except VerbError as exc:
        conflict = exc.code
    _check(
        conflict == "lifecycle_state_conflict",
        "a report on a 'parked' row -> lifecycle_state_conflict",
    )

    unknown_status = None
    try:
        _report("bogus")
    except VerbError as exc:
        unknown_status = exc.code
    _check(unknown_status == "unknown_status", "report_alive(status='bogus') -> unknown_status")


def test_report_alive_parked_refusal_is_distinct_from_the_terminal_one() -> None:
    """LIF-04 — a PARKED row and a TERMINAL row both refuse ``report_alive``
    with the same code, and until now with the same SENTENCE ("state skew ...
    parked/terminal rows never self-report back to life"). They are not the
    same fact, and the shared sentence taught the wrong one.

    Measured 2026-08-19 on ``lane-seed-remint``: a park suppresses the
    HEARTBEAT CONTRACT ONLY. The pane, the stop-hook wake path and the
    messaging verbs stay live, so a parked session keeps waking, keeps reading
    its inbox and can still send — and a session in exactly that position is
    the caller this refusal answers. "State skew" invites it to conclude its
    own row is wrong; the truth is that its row is right, park is a steward's
    deliberate state, and only ``drive_session`` takes ``parked -> live``
    back. A terminal row is the opposite fact: it IS finished.

    The error TOKEN must not move (callers key on it) — so the discriminator
    this pins is that the two messages differ and that the parked one carries
    the measured scope. Collapsing them back into one sentence reds this.
    """
    state = _state()

    def _refusal(agent_instance_id: str) -> VerbError | None:
        try:
            report_alive(
                state,
                agent_instance_id=agent_instance_id,
                status="working",
                directed_by="operator:none",
            )
        except VerbError as exc:
            return exc
        return None

    for instance_id, terminal in (("agi-lif04-parked", False), ("agi-lif04-dead", True)):
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id=instance_id,
                host="operator",
            ),
        )
        transition_lifecycle_state(
            state,
            agent_instance_id=instance_id,
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="operator:none",
        )
        transition_lifecycle_state(
            state,
            agent_instance_id=instance_id,
            from_state=LIFECYCLE_LIVE,
            to_state=LIFECYCLE_TERMINATED if terminal else LIFECYCLE_PARKED,
            directed_by="steward:none",
        )

    parked = _refusal("agi-lif04-parked")
    dead = _refusal("agi-lif04-dead")
    _check(
        parked is not None and dead is not None,
        "both a parked row and a terminal row still refuse report_alive",
    )
    _check(
        parked is not None and dead is not None and parked.code == "lifecycle_state_conflict" == dead.code,
        "the error TOKEN is unchanged on both — LIF-04 moves the message, not the code",
    )
    parked_message = parked.message if parked is not None else ""
    dead_message = dead.message if dead is not None else ""
    _check(
        parked_message != dead_message,
        "the two refusals are not the same sentence",
    )
    lowered = parked_message.lower()
    _check(
        "state skew" not in lowered,
        "the parked refusal drops the 'state skew' framing — a parked lane's row is RIGHT, and telling it otherwise is what sends it retrying",
    )
    _check(
        "not evidence that you are dead" in lowered,
        "the parked refusal says outright that it is NOT a death notice",
    )
    _check(
        "report_by" in lowered and "heartbeat contract only" in lowered,
        "the parked refusal names the MEASURED scope: the heartbeat contract, and only that",
    )
    _check(
        "wake" in lowered and "inbox" in lowered,
        "...and that the pane/wake/inbox stay live through a park (LIF-04's whole point)",
    )
    _check(
        "drive_session" in lowered,
        "the parked refusal names who owns the way back: a steward's drive_session",
    )
    _check(
        "drive_session" not in dead_message.lower(),
        "the terminal refusal does NOT offer the parked row's way back — different fact",
    )


def _report_by_delta_seconds(row: dict[str, object]) -> float:
    report_by = datetime.fromisoformat(str(row["report_by"]))
    if report_by.tzinfo is None:
        report_by = report_by.replace(tzinfo=UTC)
    return (report_by - datetime.now(UTC)).total_seconds()


def test_rearm_report_by_honors_spawn_window() -> None:
    """D2-lane-tail fix: _rearm_report_by must re-arm from the SPAWN's own
    report_by_seconds, not the hardcoded DEFAULT_REPORT_BY_SECONDS (300s).
    RED-FIRST shape: a row spawned with report_by_seconds=900 that re-arms
    to ~300s (the pre-fix bug) fails the >800s assertion below -- this is
    the exact live-measured regression (a worker's deadline silently
    shortened on its first report/drive)."""
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-window",
            host="operator",
            report_by_seconds=900,
        ),
    )
    transition_lifecycle_state(
        state,
        agent_instance_id="agi-window",
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE,
        directed_by="operator:none",
    )
    report_alive(state, agent_instance_id="agi-window", status="idle", directed_by="operator:none")
    delta = _report_by_delta_seconds(read_managed_session(state, "agi-window"))
    _check(
        delta > DEFAULT_REPORT_BY_SECONDS + 100,
        f"RED-vs-GREEN: report_alive re-arms a 900s-window row to ~900s out (got {delta:.0f}s), not silently shortened to the {DEFAULT_REPORT_BY_SECONDS}s default",
    )

    # Fallback case: a row that never requested a custom window (0/absent,
    # e.g. a legacy row) must still re-arm correctly via the default -- the
    # fix must not break the no-custom-window path.
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-nowindow",
            host="operator",
        ),
    )
    transition_lifecycle_state(
        state,
        agent_instance_id="agi-nowindow",
        from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE,
        directed_by="operator:none",
    )
    report_alive(state, agent_instance_id="agi-nowindow", status="idle", directed_by="operator:none")
    fallback_delta = _report_by_delta_seconds(read_managed_session(state, "agi-nowindow"))
    _check(
        DEFAULT_REPORT_BY_SECONDS - 30 < fallback_delta <= DEFAULT_REPORT_BY_SECONDS + 5,
        f"GREEN: a row with no custom window still falls back to the {DEFAULT_REPORT_BY_SECONDS}s default (got {fallback_delta:.0f}s)",
    )


def test_insert_managed_session_arms_report_by_for_non_operator_hosts() -> None:
    """A4 Slice 0 (measured gap, not assumed): a caller-omitted
    report_by_seconds (0) used to leave the row with NO report_by at all
    until the first report_alive/drive_session call -- invisible to
    sweep_overdue_sessions the whole spawn-to-first-report window, for any
    non-operator host. RED-FIRST shape: reverting the insert-time default
    makes the headless/tmux assertions below fail (report_by stays None)
    while the operator assertion keeps passing -- the fix must touch
    exactly the non-operator path, never the operator carve-out."""
    state = _state()

    # Operator host, no report_by_seconds: unchanged -- no contract by
    # design (sweep_overdue_sessions' own documented carve-out).
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-op-nowindow",
            host="operator",
        ),
    )
    op_row = read_managed_session(state, "agi-op-nowindow")
    _check(
        op_row.get("report_by") is None,
        "operator host + report_by_seconds=0 -> report_by stays None (no contract by design)",
    )

    # Headless host, no report_by_seconds: must be armed immediately at
    # insert, not left null until a later report/drive call.
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-headless-nowindow",
            host="headless",
        ),
    )
    headless_row = read_managed_session(state, "agi-headless-nowindow")
    _check(
        headless_row.get("report_by") is not None,
        "headless host + report_by_seconds=0 -> report_by armed at insert, not left null",
    )
    headless_delta = _report_by_delta_seconds(headless_row)
    _check(
        DEFAULT_REPORT_BY_SECONDS - 30 < headless_delta <= DEFAULT_REPORT_BY_SECONDS + 5,
        f"headless default-armed report_by lands ~{DEFAULT_REPORT_BY_SECONDS}s out (got {headless_delta:.0f}s)",
    )
    _check(
        headless_row.get("report_by_seconds") == DEFAULT_REPORT_BY_SECONDS,
        "the resolved default is written back into report_by_seconds -- the row stays self-describing for the next _rearm_report_by call",
    )

    # Tmux host, no report_by_seconds: same coverage, different host string.
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-tmux-nowindow",
            host="tmux",
        ),
    )
    tmux_row = read_managed_session(state, "agi-tmux-nowindow")
    _check(
        tmux_row.get("report_by") is not None,
        "tmux host + report_by_seconds=0 -> report_by armed at insert, not left null",
    )

    # A caller-supplied window is never overridden by the default.
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-headless-window",
            host="headless",
            report_by_seconds=900,
        ),
    )
    explicit_row = read_managed_session(state, "agi-headless-window")
    explicit_delta = _report_by_delta_seconds(explicit_row)
    _check(
        explicit_delta > DEFAULT_REPORT_BY_SECONDS + 100,
        f"an explicit report_by_seconds=900 is honored, not overridden by the {DEFAULT_REPORT_BY_SECONDS}s default (got {explicit_delta:.0f}s)",
    )
    _check(
        explicit_row.get("report_by_seconds") == 900,
        "an explicit report_by_seconds survives untouched in the row",
    )


def test_drive_session_rearm_honors_spawn_window() -> None:
    """Same fix, the drive_session call site."""
    state = _state()
    session_hosts._REGISTRY[_TEST_HOST] = _FakeDriverWithChannel()  # noqa: SLF001 -- test-only monkeypatch  # pyright: ignore[reportArgumentType]
    try:
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-drive-window",
                host=_TEST_HOST,
                session_origin="spawned",
                report_by_seconds=900,
            ),
        )
        transition_lifecycle_state(
            state,
            agent_instance_id="agi-drive-window",
            from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE,
            directed_by="operator:none",
        )
        drive_session(
            state,
            agent_instance_id="agi-drive-window",
            text="hello",
            directed_by="steward:none",
        )
        delta = _report_by_delta_seconds(read_managed_session(state, "agi-drive-window"))
        _check(
            delta > DEFAULT_REPORT_BY_SECONDS + 100,
            f"drive_session re-arms a 900s-window row to ~900s out (got {delta:.0f}s), not the {DEFAULT_REPORT_BY_SECONDS}s default",
        )
    finally:
        session_hosts._REGISTRY.pop(_TEST_HOST, None)  # noqa: SLF001


def main() -> int:
    test_spawn_errors()
    test_omitted_host_resolves_to_tmux_before_spawn()
    test_visibility_host_consistency()
    test_local_name_defaults_to_the_label_then_the_instance_id()
    test_spawn_refuses_second_session_under_a_live_local_name()
    test_spawn_allows_replacement_after_incumbent_terminated()
    test_spawn_does_not_claim_the_role_binding()
    test_list_and_status()
    test_clear_session_sends_and_can_park()
    test_compact_session_sends_no_park()
    test_drive_session_dispatches_and_unparks()
    test_drive_session_errors()
    test_clear_and_compact_errors()
    test_terminate_idempotent()
    test_terminate_session_kills_the_real_headless_process()
    test_retire_idempotent_and_redrivable()
    test_report_alive()
    test_report_alive_parked_refusal_is_distinct_from_the_terminal_one()
    test_rearm_report_by_honors_spawn_window()
    test_insert_managed_session_arms_report_by_for_non_operator_hosts()
    test_drive_session_rearm_honors_spawn_window()
    test_terminate_fires_and_delivers_session_terminal_edge()
    test_retire_composes_terminate_no_double_delivery()
    test_terminate_delivery_fault_is_contained()
    test_already_terminal_catches_orphaned_edge()
    test_spawn_session_first_turn_preserves_agent_runtime()
    test_spawn_session_first_turn_failure_is_visible_not_blocking()
    test_spawn_session_first_turn_send_raising_is_contained()

    print()
    print(f"PASSED: {_passed}")
    print(f"FAILED: {len(_failed)}")
    for label in _failed:
        print(f"  - {label}")
    return 1 if _failed else 0


if __name__ == "__main__":
    sys.exit(main())
