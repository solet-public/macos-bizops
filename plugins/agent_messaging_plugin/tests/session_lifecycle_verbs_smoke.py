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
    ROLE_CLASS_PRINCIPAL,
    ROLE_CLASS_PROJECT,
    TABLE_ROLE,
    role_binding_external_id,
)

import agent_messaging_plugin.session_hosts as session_hosts  # noqa: E402
from agent_messaging_plugin.headless_adapter import (  # noqa: E402
    _WORKER_INJECTED_HOOK_FILENAMES,
    HeadlessHostDriver,
)
from agent_messaging_plugin.schema import (  # noqa: E402
    CONDITION_SESSION_TERMINAL,
    LIFECYCLE_LIVE,
    LIFECYCLE_OVERDUE,
    LIFECYCLE_PARKED,
    LIFECYCLE_RETIRED,
    LIFECYCLE_SPAWNING,
    LIFECYCLE_TERMINATED,
    WORK_CLASS_ANALYSIS_DELIVERABLE,
)
from agent_messaging_plugin.session_lifecycle_store import (  # noqa: E402
    ManagedSessionSpec,
    insert_managed_session,
    read_managed_session,
    resolve_lane_charter,
    set_host_ref,
    transition_lifecycle_state,
)
from agent_messaging_plugin.session_lifecycle_verbs import (  # noqa: E402
    DEFAULT_REPORT_BY_SECONDS,
    FALLBACK_FIRST_TURN_TEXT,
    FIRST_TURN_SOURCE_CHARTER,
    FIRST_TURN_SOURCE_FALLBACK,
    ArmSessionDependencyRequest,
    CaptureLaneCharterRequest,
    SpawnSessionRequest,
    VerbError,
    arm_session_dependency,
    capture_lane_charter,
    clear_session,
    compact_session,
    drive_session,
    list_sessions,
    report_alive,
    retire_session,
    session_status,
    spawn_session,
    terminate_session,
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


def _spawn_req(**overrides: object) -> SpawnSessionRequest:
    base: dict[str, object] = {
        "role_class": "ephemeral",
        "lane_id": "lane-1",
        "brief_ref": "workbench/brief.md",
        "work_class": WORK_CLASS_ANALYSIS_DELIVERABLE,
        "budget_line": "budget-1",
        "host": "operator",
        "directed_by": "operator:none",
    }
    base.update(overrides)
    return SpawnSessionRequest(**base)  # type: ignore[arg-type]


def test_spawn_errors() -> None:
    state = _state()
    for kwargs, code in [
        ({"role_class": "bogus"}, "unknown_role_class"),
        ({"work_class": "bogus"}, "unknown_work_class"),
        ({"budget_line": ""}, "budget_line_required"),
        ({"role_class": ROLE_CLASS_PRINCIPAL, "role_name": "Some-Office"}, "role_not_legislated"),
        ({"role_name": "Coordinator-Main"}, "reserved_role_name"),
    ]:
        raised_code = None
        try:
            spawn_session(state, _spawn_req(**kwargs))
        except VerbError as exc:
            raised_code = exc.code
        _check(raised_code == code, f"spawn_session({kwargs}) -> {code} (got {raised_code!r})")

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
    # never a silent bypass-permissions default. Own lane_id each so
    # neither perturbs the operator row-count assertion below.
    headless_cannot_spawn = None
    os.environ.pop("FLEET_HEADLESS_PERMISSION_MODE", None)
    try:
        spawn_session(state, _spawn_req(host="headless", lane_id="lane-headless-cfg"))
    except VerbError as exc:
        headless_cannot_spawn = exc.code
    _check(
        headless_cannot_spawn == "host_cannot_spawn",
        "spawn_session(host='headless') in an unconfigured environment -> "
        f"host_cannot_spawn (got {headless_cannot_spawn!r})",
    )

    tmux_cannot_spawn = None
    try:
        spawn_session(state, _spawn_req(host="tmux", lane_id="lane-tmux-cfg"))
    except VerbError as exc:
        tmux_cannot_spawn = exc.code
    _check(
        tmux_cannot_spawn == "host_cannot_spawn",
        "spawn_session(host='tmux') in an unconfigured environment -> "
        f"host_cannot_spawn (got {tmux_cannot_spawn!r})",
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
    rows = list_sessions(state, {"lane_id": "lane-1"})["sessions"]
    _check(
        len(rows) == 1 and rows[0]["lifecycle_state"] == LIFECYCLE_TERMINATED,
        "a known-doomed dispatch (host_cannot_spawn) transitions the ledger "
        "row straight to terminated, not left stuck in 'spawning'",
    )


def test_spawn_role_class_conflict() -> None:
    state = _state()
    # Legislate 'Some-Office' as principal directly (simulating the D4
    # governance act, out of scope here).
    state.write_state(
        AGENT_ROLE_BINDING_NAMESPACE,
        {
            "table": TABLE_ROLE,
            "record": {
                "external_id": role_binding_external_id("Some-Office"),
                "role": "Some-Office",
                "role_class": ROLE_CLASS_PRINCIPAL,
            },
        },
    )
    conflict = None
    try:
        spawn_session(
            state, _spawn_req(role_class=ROLE_CLASS_PROJECT, role_name="Some-Office"),
        )
    except VerbError as exc:
        conflict = exc.code
    _check(
        conflict == "role_class_conflict",
        "spawning a 'project' claim against an already-principal-legislated "
        "name -> role_class_conflict",
    )


def test_list_and_status() -> None:
    state = _state()
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-x", lane_id="lane-x", brief_ref="", work_class="read_only",
            budget_line="b1", host="operator",
        ),
    )
    row = session_status(state, "agi-x")
    _check(row["lifecycle_state"] == LIFECYCLE_SPAWNING, "session_status returns the ledger row")

    not_found = None
    try:
        session_status(state, "agi-nonexistent")
    except VerbError as exc:
        not_found = exc.code
    _check(not_found == "session_not_found", "session_status(unknown) -> session_not_found")

    _check(
        len(list_sessions(state, {"lane_id": "lane-x"})["sessions"]) == 1,
        "list_sessions filters by lane_id",
    )


_TEST_HOST = "test-driver-host"
_TEST_HOST_NO_CHANNEL = "test-driver-host-no-channel"


class _FakeChannel:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send(self, text: str) -> None:
        self.sent.append(text)


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
    session_hosts._REGISTRY[_TEST_HOST] = _FakeDriverWithChannel()  # noqa: SLF001 -- test-only monkeypatch
    session_hosts._REGISTRY[_TEST_HOST_NO_CHANNEL] = _FakeDriverNoChannel()  # noqa: SLF001


def _remove_fake_hosts() -> None:
    session_hosts._REGISTRY.pop(_TEST_HOST, None)  # noqa: SLF001
    session_hosts._REGISTRY.pop(_TEST_HOST_NO_CHANNEL, None)  # noqa: SLF001


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
    state: StateManagementInterface, *, waiter_instance_id: str, dying_instance_id: str,
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
                agent_instance_id="agi-dying-1", lane_id="lane-term", brief_ref="",
                work_class="read_only", budget_line="b1", host="operator",
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-dying-1", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-waiter-1", lane_id="lane-term", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST,
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-waiter-1", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        _arm_session_terminal(
            state, waiter_instance_id="agi-waiter-1", dying_instance_id="agi-dying-1",
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
                agent_instance_id="agi-dying-2", lane_id="lane-retire", brief_ref="",
                work_class="read_only", budget_line="b1", host="operator",
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-dying-2", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-waiter-2", lane_id="lane-retire", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST,
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-waiter-2", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        _arm_session_terminal(
            state, waiter_instance_id="agi-waiter-2", dying_instance_id="agi-dying-2",
        )
        driver = session_hosts._REGISTRY[_TEST_HOST]  # noqa: SLF001

        result = retire_session(state, agent_instance_id="agi-dying-2", directed_by="operator:none")
        _check(
            result == {"already_retired": False, "dependencies_fired": 1},
            f"retire_session reports exactly 1 edge fired via composition (got {result})",
        )
        _check(
            len(driver.channel.sent) == 1,
            f"the waiter's channel receives exactly ONE notice, not two, under composition "
            f"(got {driver.channel.sent})",
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
    session_hosts._REGISTRY[_TEST_HOST_RAISING] = _FakeDriverRaisingChannel()  # noqa: SLF001
    try:
        state = _state()
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-dying-3", lane_id="lane-fault", brief_ref="",
                work_class="read_only", budget_line="b1", host="operator",
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-dying-3", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-waiter-3", lane_id="lane-fault", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST_RAISING,
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-waiter-3", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        _arm_session_terminal(
            state, waiter_instance_id="agi-waiter-3", dying_instance_id="agi-dying-3",
        )
        raised = False
        result: dict[str, Any] = {}
        try:
            result = terminate_session(state, agent_instance_id="agi-dying-3", directed_by="operator:none")
        except Exception:  # noqa: BLE001 -- the whole point is it must NOT raise
            raised = True
        _check(not raised, "a raising waiter driver channel does not propagate out of terminate_session")
        _check(
            result.get("already_terminal") is False
            and result.get("session_terminal_edges_fired") == 1,
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
                agent_instance_id="agi-dying-4", lane_id="lane-orphan", brief_ref="",
                work_class="read_only", budget_line="b1", host="operator",
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-dying-4", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
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
                agent_instance_id="agi-waiter-4", lane_id="lane-orphan", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST,
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-waiter-4", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        # Arm the edge AFTER the target already died -- the orphan case.
        _arm_session_terminal(
            state, waiter_instance_id="agi-waiter-4", dying_instance_id="agi-dying-4",
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


# -- lane_charter capture + spawn_session first-turn (phase 2 slice 6) --


def test_capture_lane_charter_validation_errors() -> None:
    state = _state()
    for kwargs, code in [
        ({"lane_id": "", "charter_text": "text", "captured_at": "2026-08-06T00:00:00Z"},
         "missing_lane_id"),
        ({"lane_id": "lane-1", "charter_text": "", "captured_at": "2026-08-06T00:00:00Z"},
         "missing_charter_text"),
        ({"lane_id": "lane-1", "charter_text": "text", "captured_at": ""},
         "missing_captured_at"),
    ]:
        raised_code = None
        try:
            capture_lane_charter(state, CaptureLaneCharterRequest(**kwargs))  # type: ignore[arg-type]
        except VerbError as exc:
            raised_code = exc.code
        _check(raised_code == code, f"capture_lane_charter({kwargs}) -> {code} (got {raised_code!r})")


def test_capture_lane_charter_is_insert_only_and_supersedes_by_recency() -> None:
    """RED-FIRST intent: a naive 'update in place' implementation would make
    resolve_lane_charter return whichever row was written LAST regardless of
    captured_at ordering AND would leave only one row on record; this test
    pins BOTH the supersede-by-recency read and (indirectly, since a second
    capture never errors as a duplicate) the insert-only write."""
    state = _state()
    first = capture_lane_charter(
        state,
        CaptureLaneCharterRequest(
            lane_id="lane-charter-supersede",
            charter_text="First charter text.",
            captured_at="2026-08-06T00:00:00+00:00",
            brief_ref="workbench/first.md",
            directed_by="operator:none",
        ),
    )
    _check(first["charter_text"] == "First charter text.", "the first capture stores its own text")
    resolved_after_first = resolve_lane_charter(state, "lane-charter-supersede")
    _check(
        resolved_after_first is not None
        and resolved_after_first.charter_text == "First charter text."
        and resolved_after_first.brief_ref == "workbench/first.md"
        and resolved_after_first.captured_at == "2026-08-06T00:00:00+00:00",
        f"resolve_lane_charter returns the only row on file, full record "
        f"(got {resolved_after_first!r})",
    )

    second = capture_lane_charter(
        state,
        CaptureLaneCharterRequest(
            lane_id="lane-charter-supersede",
            charter_text="Second, superseding charter text.",
            captured_at="2026-08-06T00:01:00+00:00",
            brief_ref="workbench/second.md",
            directed_by="operator:none",
        ),
    )
    _check(
        second["charter_text"] == "Second, superseding charter text.",
        "the second capture stores its own text",
    )
    resolved_after_second = resolve_lane_charter(state, "lane-charter-supersede")
    _check(
        resolved_after_second is not None
        and resolved_after_second.charter_text == "Second, superseding charter text.",
        f"RED-vs-GREEN: resolve_lane_charter follows the LATEST captured_at, superseding "
        f"the first row rather than editing it or picking arbitrarily (got "
        f"{resolved_after_second!r})",
    )


def test_resolve_lane_charter_empty_for_unknown_lane() -> None:
    state = _state()
    _check(
        resolve_lane_charter(state, "lane-with-no-charter-ever-captured") is None,
        "a lane with no captured charter resolves to None, not a fault",
    )


def test_spawn_session_drives_charter_as_first_turn_byte_exact() -> None:
    """RED-FIRST (phase 2 slice 6, Finding 0's fix): before this slice,
    spawn_session drove NO first turn at all — on the CLAUDE watch path, a
    watch-armed worker's Stop-hook wake loop, which only arms once the first
    turn completes, would never arm for a pristine spawn (a managed CODEX
    worker has no such loop to arm — codex-0147-dead-spool-retirement,
    2026-08-13 — but still needs its first turn driven the same way). Also
    pins the design-check-in
    ruling's byte-exact fidelity requirement end to end: capture -> resolve
    -> drive must never re-serialize or mangle the operator's words — now
    scoped to the charter BODY specifically, since phase 3's provenance
    frame (2026-08-06 incident finding, coordinator-seat ruling) wraps it in
    non-charter framing text; the frame must never touch the body itself."""
    state = _state()
    _install_fake_hosts()
    try:
        charter_text = "Operator's verbatim founding words — apostrophes and em—dashes intact."
        captured = capture_lane_charter(
            state,
            CaptureLaneCharterRequest(
                lane_id="lane-charter-spawn",
                charter_text=charter_text,
                captured_at="2026-08-06T00:00:00+00:00",
                brief_ref="workbench/brief.md",
                directed_by="operator:none",
            ),
        )
        result = spawn_session(
            state,
            _spawn_req(
                host=_TEST_HOST, lane_id="lane-charter-spawn",
                spawned_by_role="watch-migration-impl",
            ),
        )
        _check(
            result["first_turn_source"] == FIRST_TURN_SOURCE_CHARTER,
            f"spawn_session reports first_turn_source='charter' when a lane_charter row "
            f"is on file (got {result['first_turn_source']!r})",
        )
        _check(result["first_turn_delivered"] is True, "the first turn was delivered")
        _check(result["first_turn_error"] == "", "no error on a successful delivery")
        driver = cast("_FakeDriverWithChannel", session_hosts._REGISTRY[_TEST_HOST])  # noqa: SLF001
        _check(len(driver.channel.sent) == 1, "exactly one first-turn send")
        driven_text = driver.channel.sent[0] if driver.channel.sent else ""
        _check(
            driven_text.endswith(captured["charter_text"]) and driven_text.endswith(charter_text),
            f"RED-vs-GREEN: the driven text ENDS WITH the exact stored charter_text, "
            f"byte-exact — no mangling anywhere in capture -> resolve -> drive (got "
            f"{driven_text!r})",
        )
        _check(
            "NOT a live conversation" in driven_text
            and "not yet registered" in driven_text
            and result["agent_instance_id"] in driven_text
            and "watch-migration-impl" in driven_text
            and "workbench/brief.md" in driven_text
            and "2026-08-06T00:00:00+00:00" in driven_text,
            f"the provenance frame (not-live-conversation + not-yet-registered + agent_instance_id "
            f"+ spawned_by_role + brief_ref + captured_at) wraps the charter body (got "
            f"{driven_text!r})",
        )
    finally:
        _remove_fake_hosts()


def test_spawn_session_drives_fallback_when_no_charter_on_file() -> None:
    """Ordering-ruling guard (a): the no-charter fallback must be a small,
    single-shot, terminating turn — never a question, never a wait."""
    state = _state()
    _install_fake_hosts()
    try:
        result = spawn_session(
            state, _spawn_req(host=_TEST_HOST, lane_id="lane-charter-none-spawn"),
        )
        _check(
            result["first_turn_source"] == FIRST_TURN_SOURCE_FALLBACK,
            f"spawn_session reports first_turn_source='fallback' when no lane_charter row "
            f"is on file (got {result['first_turn_source']!r})",
        )
        _check(result["first_turn_delivered"] is True, "the fallback turn was delivered")
        driver = cast("_FakeDriverWithChannel", session_hosts._REGISTRY[_TEST_HOST])  # noqa: SLF001
        _check(
            driver.channel.sent == [FALLBACK_FIRST_TURN_TEXT],
            f"the exact fallback text is driven, unmodified (got {driver.channel.sent!r})",
        )
    finally:
        _remove_fake_hosts()


def test_spawn_session_first_turn_failure_is_visible_not_blocking() -> None:
    """Ordering-ruling guard (b): a first-turn delivery failure must be
    VISIBLE (surfaced in the spawn result) and must NEVER block the spawn
    itself — the row still lands, just with first_turn_delivered=False."""
    state = _state()
    _install_fake_hosts()
    try:
        result = spawn_session(
            state, _spawn_req(host=_TEST_HOST_NO_CHANNEL, lane_id="lane-charter-no-channel"),
        )
        _check(
            result["first_turn_delivered"] is False,
            "a host with no driver channel reports first_turn_delivered=False",
        )
        _check(
            result["first_turn_error"] != "",
            f"RED-vs-GREEN: the failure is surfaced in the spawn result, never silent "
            f"(got first_turn_error={result['first_turn_error']!r})",
        )
        _check(
            result["lifecycle_state"] == LIFECYCLE_SPAWNING,
            "the spawn itself is NOT blocked by a first-turn delivery failure",
        )
    finally:
        _remove_fake_hosts()


def test_spawn_session_first_turn_send_raising_is_contained() -> None:
    state = _state()
    session_hosts._REGISTRY[_TEST_HOST_RAISING] = _FakeDriverRaisingChannel()  # noqa: SLF001
    try:
        result = spawn_session(
            state, _spawn_req(host=_TEST_HOST_RAISING, lane_id="lane-charter-raising"),
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
                agent_instance_id="agi-clear-1", lane_id="lane-clear", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST,
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-clear-1", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        driver = session_hosts._REGISTRY[_TEST_HOST]  # noqa: SLF001

        result = clear_session(
            state, agent_instance_id="agi-clear-1", park=False, directed_by="operator:none",
        )
        _check(driver.channel.sent == ["/clear"], "clear_session sends '/clear' over the channel")
        _check(
            result == {"lifecycle_state": LIFECYCLE_LIVE, "parked": False},
            "clear_session(park=False) does not transition lifecycle_state",
        )
        _check(
            read_managed_session(state, "agi-clear-1")["lifecycle_state"] == LIFECYCLE_LIVE,
            "the ledger row is still 'live' after a non-parking clear",
        )

        parked_result = clear_session(
            state, agent_instance_id="agi-clear-1", park=True, directed_by="operator:none",
        )
        _check(
            driver.channel.sent == ["/clear", "/clear"],
            "clear_session(park=True) still sends '/clear' before parking",
        )
        _check(
            parked_result == {"lifecycle_state": LIFECYCLE_PARKED, "parked": True},
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
                agent_instance_id="agi-compact-1", lane_id="lane-compact", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST,
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-compact-1", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
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
    ``_rearm_report_by`` call reds "re-arms report_by"."""
    _install_fake_hosts()
    try:
        state = _state()
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-drive-1", lane_id="lane-drive", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST,
            ),
        )
        driver = session_hosts._REGISTRY[_TEST_HOST]  # noqa: SLF001

        # Dispatch-at-spawn: a 'spawning' row is drivable (the brief can be
        # sent the moment spawn_session returns); the registration hook
        # still owns the spawning->live edge, so no transition here.
        spawning_result = drive_session(
            state, agent_instance_id="agi-drive-1", text="Brief: build the tmux driver.",
            directed_by="steward:none",
        )
        _check(
            driver.channel.sent == ["Brief: build the tmux driver."],
            "drive_session sends the text over the channel",
        )
        _check(
            spawning_result == {"lifecycle_state": LIFECYCLE_SPAWNING, "unparked": False},
            "drive_session on a 'spawning' row dispatches without a transition",
        )
        report_by_after_first = read_managed_session(state, "agi-drive-1").get("report_by")
        _check(bool(report_by_after_first), "drive_session re-arms report_by on dispatch")

        # parked -> live: the §3.2 "new dispatch through the driver channel"
        # edge, owned by this verb alone.
        transition_lifecycle_state(
            state, agent_instance_id="agi-drive-1", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-drive-1", from_state=LIFECYCLE_LIVE,
            to_state=LIFECYCLE_PARKED, directed_by="steward:none",
        )
        unpark_result = drive_session(
            state, agent_instance_id="agi-drive-1", text="Follow-up: land it.",
            directed_by="steward:none",
        )
        _check(
            driver.channel.sent
            == ["Brief: build the tmux driver.", "Follow-up: land it."],
            "a second drive_session sends the follow-up text",
        )
        _check(
            unpark_result == {"lifecycle_state": LIFECYCLE_LIVE, "unparked": True},
            "drive_session drives parked -> live (unparked=True)",
        )
        _check(
            read_managed_session(state, "agi-drive-1")["lifecycle_state"] == LIFECYCLE_LIVE,
            "the ledger row is 'live' after driving a parked session",
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
                state, agent_instance_id="agi-nonexistent", text="work", directed_by="x",
            )
        except VerbError as exc:
            not_found = exc.code
        _check(not_found == "session_not_found", "drive_session(unknown) -> session_not_found")

        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-drive-err", lane_id="lane-drive-err", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST,
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
            state, agent_instance_id="agi-drive-err", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_TERMINATED, directed_by="operator:none",
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
                agent_instance_id="agi-drive-nochan", lane_id="lane-drive-nochan", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST_NO_CHANNEL,
            ),
        )
        unsupported = None
        try:
            drive_session(
                state, agent_instance_id="agi-drive-nochan", text="work", directed_by="x",
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
                agent_instance_id="agi-terminal", lane_id="lane-terminal", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST,
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-terminal", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_TERMINATED, directed_by="operator:none",
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
                agent_instance_id="agi-no-channel", lane_id="lane-no-channel", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST_NO_CHANNEL,
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-no-channel", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
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
                agent_instance_id="agi-operator", lane_id="lane-operator", brief_ref="",
                work_class="read_only", budget_line="b1", host="operator",
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-operator", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
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
            agent_instance_id="agi-t", lane_id="lane-t", brief_ref="", work_class="read_only",
            budget_line="b1", host="operator",
        ),
    )
    transition_lifecycle_state(
        state, agent_instance_id="agi-t", from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE, directed_by="operator:none",
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
        "an operator-hosted row still reaches 'terminated' without the verb "
        "raising -- the degenerate driver's HostCannotSpawnError is "
        "tolerated, not propagated",
    )


def _real_short_lived_popen_fn(*_a: object, **_k: object) -> subprocess.Popen[str]:
    """Ignores the incoming cmd/env entirely -- spawns a real, harmless,
    short-lived child so the test exercises actual OS-level kill mechanics
    without ever invoking the real ``claude`` binary (mirrors
    ``headless_adapter_smoke.py``'s pattern)."""
    return subprocess.Popen(  # noqa: S603 -- fixed harmless argv, test-only
        [sys.executable, "-c", "import time; time.sleep(30)"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
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
            claude_bin=sys.executable, solet_name="testhom",
            permission_mode="bypassPermissions", mcp_config_path=mcp_config,
            cwd=Path(tmp), popen_fn=_real_short_lived_popen_fn,
        )
        prior = session_hosts._REGISTRY.get("headless")  # noqa: SLF001 -- test-only monkeypatch
        session_hosts._REGISTRY["headless"] = real_driver  # noqa: SLF001
        try:
            state = _state()
            agent_instance_id = "agi-real-reap"
            insert_managed_session(
                state,
                ManagedSessionSpec(
                    agent_instance_id=agent_instance_id, lane_id="lane-reap", brief_ref="",
                    work_class="read_only", budget_line="b1", host="headless",
                ),
            )
            transition_lifecycle_state(
                state, agent_instance_id=agent_instance_id, from_state=LIFECYCLE_SPAWNING,
                to_state=LIFECYCLE_LIVE, directed_by="operator:none",
            )
            host_ref = real_driver.spawn(
                {"agent_instance_id": agent_instance_id, "lane_id": "lane-reap"},
            )
            set_host_ref(state, agent_instance_id=agent_instance_id, host_ref=host_ref)
            _check(
                real_driver.alive(host_ref),
                "precondition: the real spawned process is alive before terminate_session",
            )

            result = terminate_session(
                state, agent_instance_id=agent_instance_id, directed_by="operator:none",
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
                "terminate_session actually killed the real OS process, not "
                "just the ledger row",
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
            agent_instance_id="agi-r", lane_id="lane-r", brief_ref="",
            work_class="read_only", budget_line="b1", host="operator",
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
        state, CardinalityGatedClaim(
            agent_session_id="ases-r", requested_role="Some-Lane", agent_instance_id="agi-r",
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
        "retire_session's step 2 (populated agent_session_id path) actually "
        "prunes the session_role_claim cardinality row -- this path is only "
        "reachable with agent_session_id populated, which is exactly what "
        "the registration hook now does for every real spawn",
    )
    second = retire_session(state, agent_instance_id="agi-r", directed_by="operator:none")
    _check(
        second["already_retired"] is True,
        "re-running retire_session on an already-retired row is a no-op "
        "(re-drivable by construction, never wedged)",
    )


def test_report_alive() -> None:
    state = _state()

    def _report(status: str) -> dict[str, object]:
        return report_alive(
            state, agent_instance_id="agi-a", status=status, directed_by="operator:none",
        )

    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-a", lane_id="lane-a", brief_ref="", work_class="read_only",
            budget_line="b1", host="operator",
        ),
    )
    transition_lifecycle_state(
        state, agent_instance_id="agi-a", from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE, directed_by="operator:none",
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
        "report_alive re-arms report_by even when NO transition occurs "
        "(the same-status branch)",
    )

    # overdue recovery
    transition_lifecycle_state(
        state, agent_instance_id="agi-a", from_state="idle",
        to_state=LIFECYCLE_OVERDUE, directed_by="sweep:none",
    )
    recovered = _report("working")
    _check(recovered["recovered"] is True, "a late report from 'overdue' is flagged recovered=True")
    _check(recovered["lifecycle_state"] == LIFECYCLE_LIVE, "overdue -> live on a 'working' report")

    # parked/terminal conflict
    transition_lifecycle_state(
        state, agent_instance_id="agi-a", from_state=LIFECYCLE_LIVE,
        to_state="parked", directed_by="steward:none",
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
            agent_instance_id="agi-window", lane_id="lane-window", brief_ref="",
            work_class="read_only", budget_line="b1", host="operator",
            report_by_seconds=900,
        ),
    )
    transition_lifecycle_state(
        state, agent_instance_id="agi-window", from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE, directed_by="operator:none",
    )
    report_alive(state, agent_instance_id="agi-window", status="idle", directed_by="operator:none")
    delta = _report_by_delta_seconds(read_managed_session(state, "agi-window"))
    _check(
        delta > DEFAULT_REPORT_BY_SECONDS + 100,
        f"RED-vs-GREEN: report_alive re-arms a 900s-window row to ~900s out "
        f"(got {delta:.0f}s), not silently shortened to the {DEFAULT_REPORT_BY_SECONDS}s "
        "default",
    )

    # Fallback case: a row that never requested a custom window (0/absent,
    # e.g. a legacy row) must still re-arm correctly via the default -- the
    # fix must not break the no-custom-window path.
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-nowindow", lane_id="lane-nowindow", brief_ref="",
            work_class="read_only", budget_line="b1", host="operator",
        ),
    )
    transition_lifecycle_state(
        state, agent_instance_id="agi-nowindow", from_state=LIFECYCLE_SPAWNING,
        to_state=LIFECYCLE_LIVE, directed_by="operator:none",
    )
    report_alive(state, agent_instance_id="agi-nowindow", status="idle", directed_by="operator:none")
    fallback_delta = _report_by_delta_seconds(read_managed_session(state, "agi-nowindow"))
    _check(
        DEFAULT_REPORT_BY_SECONDS - 30 < fallback_delta <= DEFAULT_REPORT_BY_SECONDS + 5,
        f"GREEN: a row with no custom window still falls back to the "
        f"{DEFAULT_REPORT_BY_SECONDS}s default (got {fallback_delta:.0f}s)",
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
            agent_instance_id="agi-op-nowindow", lane_id="lane-op", brief_ref="",
            work_class="read_only", budget_line="b1", host="operator",
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
            agent_instance_id="agi-headless-nowindow", lane_id="lane-headless", brief_ref="",
            work_class="read_only", budget_line="b1", host="headless",
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
        f"headless default-armed report_by lands ~{DEFAULT_REPORT_BY_SECONDS}s out "
        f"(got {headless_delta:.0f}s)",
    )
    _check(
        headless_row.get("report_by_seconds") == DEFAULT_REPORT_BY_SECONDS,
        "the resolved default is written back into report_by_seconds -- the row "
        "stays self-describing for the next _rearm_report_by call",
    )

    # Tmux host, no report_by_seconds: same coverage, different host string.
    insert_managed_session(
        state,
        ManagedSessionSpec(
            agent_instance_id="agi-tmux-nowindow", lane_id="lane-tmux", brief_ref="",
            work_class="read_only", budget_line="b1", host="tmux",
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
            agent_instance_id="agi-headless-window", lane_id="lane-headless-w", brief_ref="",
            work_class="read_only", budget_line="b1", host="headless",
            report_by_seconds=900,
        ),
    )
    explicit_row = read_managed_session(state, "agi-headless-window")
    explicit_delta = _report_by_delta_seconds(explicit_row)
    _check(
        explicit_delta > DEFAULT_REPORT_BY_SECONDS + 100,
        f"an explicit report_by_seconds=900 is honored, not overridden by the "
        f"{DEFAULT_REPORT_BY_SECONDS}s default (got {explicit_delta:.0f}s)",
    )
    _check(
        explicit_row.get("report_by_seconds") == 900,
        "an explicit report_by_seconds survives untouched in the row",
    )


def test_drive_session_rearm_honors_spawn_window() -> None:
    """Same fix, the drive_session call site."""
    state = _state()
    session_hosts._REGISTRY[_TEST_HOST] = _FakeDriverWithChannel()  # noqa: SLF001 -- test-only monkeypatch
    try:
        insert_managed_session(
            state,
            ManagedSessionSpec(
                agent_instance_id="agi-drive-window", lane_id="lane-drive-window", brief_ref="",
                work_class="read_only", budget_line="b1", host=_TEST_HOST,
                report_by_seconds=900,
            ),
        )
        transition_lifecycle_state(
            state, agent_instance_id="agi-drive-window", from_state=LIFECYCLE_SPAWNING,
            to_state=LIFECYCLE_LIVE, directed_by="operator:none",
        )
        drive_session(
            state, agent_instance_id="agi-drive-window", text="hello",
            directed_by="steward:none",
        )
        delta = _report_by_delta_seconds(read_managed_session(state, "agi-drive-window"))
        _check(
            delta > DEFAULT_REPORT_BY_SECONDS + 100,
            f"drive_session re-arms a 900s-window row to ~900s out (got "
            f"{delta:.0f}s), not the {DEFAULT_REPORT_BY_SECONDS}s default",
        )
    finally:
        session_hosts._REGISTRY.pop(_TEST_HOST, None)  # noqa: SLF001


def main() -> int:
    test_spawn_errors()
    test_spawn_role_class_conflict()
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
    test_rearm_report_by_honors_spawn_window()
    test_insert_managed_session_arms_report_by_for_non_operator_hosts()
    test_drive_session_rearm_honors_spawn_window()
    test_terminate_fires_and_delivers_session_terminal_edge()
    test_retire_composes_terminate_no_double_delivery()
    test_terminate_delivery_fault_is_contained()
    test_already_terminal_catches_orphaned_edge()
    test_capture_lane_charter_validation_errors()
    test_capture_lane_charter_is_insert_only_and_supersedes_by_recency()
    test_resolve_lane_charter_empty_for_unknown_lane()
    test_spawn_session_drives_charter_as_first_turn_byte_exact()
    test_spawn_session_drives_fallback_when_no_charter_on_file()
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
