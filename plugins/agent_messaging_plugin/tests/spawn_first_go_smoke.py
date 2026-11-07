#!/usr/bin/env python3
"""Offline native-spawn first-GO ordering and receipt smoke."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
sys.path[:0] = [
    str(REPO_ROOT),
    str(REPO_ROOT / "ananta" / "src"),
    str(REPO_ROOT / "plugins" / "agent_messaging_plugin" / "src"),
    str(Path(__file__).parent),
]

import session_lifecycle_verbs_smoke as base  # noqa: E402

_passed = 0
_failed: list[str] = []


def _check(value: object, label: str) -> None:
    global _passed
    if value:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failed.append(label)
        print(f"  FAIL  {label}")


class _OrderedChannel(base._FakeChannel):  # noqa: SLF001
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events = events

    def send(self, text: str) -> None:
        self.events.append("driver.send")
        super().send(text)


class _FirstGoPort:
    def __init__(
        self,
        events: list[str],
        *,
        fail_once: bool = False,
        fail_receipt_write_state: Any | None = None,
    ) -> None:
        self.events, self.fail_once, self.calls = events, fail_once, 0
        self.fail_receipt_write_state = fail_receipt_write_state

    def peer_send(self, request: object) -> object:
        del request
        self.calls += 1
        self.events.append("first_go")
        if self.fail_receipt_write_state is not None and self.calls == 1:
            self.fail_receipt_write_state.fail_receipt_update = True
        if self.fail_once and self.calls == 1:
            raise RuntimeError("injected first_go enqueue failure")
        return type("PeerSendReceipt", (), {"message_id": f"agm-first-go-{self.calls}"})()


class _CountingDriver(base._FakeDriverWithChannel):  # noqa: SLF001
    def __init__(self, events: list[str]) -> None:
        super().__init__()
        self.events, self.spawns = events, 0
        self.channel = _OrderedChannel(events)

    def spawn(self, spec: object) -> str:
        self.spawns += 1
        return super().spawn(spec)


class _ReceiptWriteFailureState(base.RealShapeState):  # noqa: SLF001
    """Return a real provider error envelope for exactly one receipt write."""

    fail_receipt_update = False

    def update_state(self, namespace: str, query: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
        if self.fail_receipt_update:
            self.fail_receipt_update = False
            return {
                "action_status": "error",
                "error": {"message": "injected receipt write failure"},
                "data": {},
            }
        return super().update_state(namespace, query, updates)


def _ordered_charter_and_fallback() -> None:
    for charter, label in ((True, "charter"), (False, "fallback")):
        state = base._state()
        events: list[str] = []
        driver = _CountingDriver(events)
        port = _FirstGoPort(events)
        base.session_hosts._REGISTRY[base._TEST_HOST] = driver  # noqa: SLF001
        try:
            if charter:
                base.capture_lane_charter(
                    state,
                    base.CaptureLaneCharterRequest(
                        lane_id=f"lane-go-{label}",
                        charter_text="charter",
                        captured_at="2026-08-06T00:00:00+00:00",
                        unit_id=base.FIXTURE_UNIT_ID,
                        revision_id="drv_fixture-revision",
                        directed_by="operator:none",
                    ),
                )
            req = base._spawn_req(host=base._TEST_HOST, lane_id=f"lane-go-{label}", first_go=f"GO {label}")
            register = base.RegisterUnitDouble()
            register.add_unit()
            actor = base.RegisterActor("agi-main", "ases-main", "", req.lane_id)
            result = base.native_spawn_session(state, req, register=register, actor=actor, messaging=port)
            _check(
                events == ["first_go", "driver.send"],
                f"{label}: first_go precedes first-turn release",
            )
            _check(
                result["first_go_status"] == "accepted" and result["first_go_receipt"]["recipient_agent_instance_id"] == result["agent_instance_id"],
                f"{label}: receipt is terminal and recipient-bound",
            )
            persisted = base.read_managed_session(state, result["agent_instance_id"])
            _check(
                "native_spawn_first_go" in persisted["capability_report"],
                f"{label}: receipt persists on owner state",
            )
        finally:
            base._remove_fake_hosts()  # noqa: SLF001


def _recovery_and_conflict() -> None:
    state = base._state()
    events: list[str] = []
    driver = _CountingDriver(events)
    port = _FirstGoPort(events, fail_once=True)
    base.session_hosts._REGISTRY[base._TEST_HOST] = driver  # noqa: SLF001
    try:
        req = base._spawn_req(host=base._TEST_HOST, lane_id="lane-go-retry", first_go="GO retry")
        register = base.RegisterUnitDouble()
        register.add_unit()
        actor = base.RegisterActor("agi-main", "ases-main", "", req.lane_id)
        first = base.native_spawn_session(state, req, register=register, actor=actor, messaging=port)
        second = base.native_spawn_session(state, req, register=register, actor=actor, messaging=port)
        _check(
            first["first_go_status"] == "incomplete" and not first["first_turn_delivered"],
            "enqueue failure retains host without first turn",
        )
        _check(
            second["first_go_status"] == "accepted" and driver.spawns == 1 and port.calls == 2,
            "recovery reuses host and retries exactly once",
        )

        conflict_state = base._state()
        conflict_events: list[str] = []
        conflict_driver = _CountingDriver(conflict_events)
        conflict_driver.channel = base._FakeChannelRaising()  # noqa: SLF001
        conflict_port = _FirstGoPort(conflict_events)
        base.session_hosts._REGISTRY[base._TEST_HOST] = conflict_driver  # noqa: SLF001
        conflict_req = base._spawn_req(host=base._TEST_HOST, lane_id="lane-go-conflict", first_go="GO accepted")
        conflict_register = base.RegisterUnitDouble()
        conflict_register.add_unit()
        conflict_actor = base.RegisterActor("agi-main", "ases-main", "", conflict_req.lane_id)
        accepted = base.native_spawn_session(
            conflict_state,
            conflict_req,
            register=conflict_register,
            actor=conflict_actor,
            messaging=conflict_port,
        )
        row = base.read_managed_session(conflict_state, accepted["agent_instance_id"])
        conflict_state.update_state(
            base.AGENT_ROLE_BINDING_NAMESPACE,
            {
                "table": base.TABLE_MANAGED_SESSION,
                "filters": {"agent_instance_id": accepted["agent_instance_id"], "is_deleted": 0},
            },
            {"first_turn_at": None},
        )
        try:
            base.native_spawn_session(
                conflict_state,
                base.replace(conflict_req, first_go="GO changed"),
                register=conflict_register,
                actor=conflict_actor,
                messaging=conflict_port,
            )
        except base.VerbError as exc:
            _check(
                exc.code == "first_go_payload_conflict" and row["agent_instance_id"] == accepted["agent_instance_id"],
                "retained recovery refuses changed first_go",
            )
        else:
            _check(False, "retained recovery must refuse changed first_go")
    finally:
        base._remove_fake_hosts()  # noqa: SLF001


def _omitted_is_unchanged() -> None:
    events: list[str] = []
    driver = _CountingDriver(events)
    port = _FirstGoPort(events)
    base.session_hosts._REGISTRY[base._TEST_HOST] = driver  # noqa: SLF001
    try:
        req = base._spawn_req(host=base._TEST_HOST, lane_id="lane-go-omitted")
        register = base.RegisterUnitDouble()
        register.add_unit()
        state = base._state()
        result = base.native_spawn_session(
            state,
            req,
            register=register,
            actor=base.RegisterActor("agi-main", "ases-main", "", req.lane_id),
            messaging=port,
        )
        _check(
            result["first_go_status"] == "omitted" and port.calls == 0 and events == ["driver.send"],
            "omitted first_go preserves existing release behavior",
        )
    finally:
        base._remove_fake_hosts()  # noqa: SLF001


def _receipt_write_failure_retains_host_and_recovers() -> None:
    state = _ReceiptWriteFailureState()
    events: list[str] = []
    driver = _CountingDriver(events)
    port = _FirstGoPort(events, fail_receipt_write_state=state)
    base.session_hosts._REGISTRY[base._TEST_HOST] = driver  # noqa: SLF001
    try:
        req = base._spawn_req(host=base._TEST_HOST, lane_id="lane-go-receipt-failure", first_go="GO receipt failure")
        register = base.RegisterUnitDouble()
        register.add_unit()
        actor = base.RegisterActor("agi-main", "ases-main", "", req.lane_id)
        first = base.native_spawn_session(state, req, register=register, actor=actor, messaging=port)
        row = base.read_managed_session(state, first["agent_instance_id"])
        _check(events == ["first_go"], "receipt write failure sends no first turn")
        _check(
            first["first_go_status"] == "incomplete" and not first["first_turn_delivered"],
            "receipt write failure reports incomplete first_go",
        )
        _check(
            first["host_ref"] == row["host_ref"] and row["host_ref"] and driver.spawns == 1,
            "receipt write failure retains the launched host",
        )
        _check(
            "native_spawn_first_go" not in row["capability_report"],
            "receipt write failure leaves no phantom receipt",
        )
        recovered = base.native_spawn_session(state, req, register=register, actor=actor, messaging=port)
        _check(
            recovered["first_go_status"] == "accepted" and events == ["first_go", "first_go", "driver.send"] and driver.spawns == 1 and port.calls == 2,
            "receipt write recovery re-enqueues once on the retained host",
        )
    finally:
        base._remove_fake_hosts()  # noqa: SLF001


def main() -> int:
    def provision(*_: object, agent_instance_id: str, role_name: str, **__: object) -> Any:
        return base.lifecycle_verbs.LaneWorktree(REPO_ROOT, REPO_ROOT, REPO_ROOT, f"fixture/{role_name}/{agent_instance_id}")

    with patch.object(base.lifecycle_verbs, "_provision_spawn_worktree", side_effect=provision):
        _ordered_charter_and_fallback()
        _omitted_is_unchanged()
        _receipt_write_failure_retains_host_and_recovers()
        _recovery_and_conflict()
    print(f"\nPASSED: {_passed}\nFAILED: {len(_failed)}")
    return int(bool(_failed))


if __name__ == "__main__":
    raise SystemExit(main())
