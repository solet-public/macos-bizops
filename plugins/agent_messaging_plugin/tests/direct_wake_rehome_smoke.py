#!/usr/bin/env python3
"""Fork-1a (REL-01) direct-wake RE-HOME on reconnect — no DB.

Exercises ``rehome_owed_direct_wakes`` + the ``_direct_wake_self_refresh``
peer/register hook against the real-shape state fake. The RED-FIRST anchors are
the pre-fix ``recipient_gone`` orphan (the live 2026-07-11 16:32/16:35 telemetry
in the deaf-wake RCA): an owed row fenced to a DEAD instance is invisible to the
session's SUCCESSOR instance — the wake strands until the row is re-homed.

  * R1 orphan → re-home (the acceptance replay): an owed row to the OLD instance;
    the successor instance (same agent_session_id) cannot drain it (RED); after
    re-home it can (GREEN); the OLD instance no longer owns it.
  * R2 recipient_gone REACTIVATION: an already-escalated recipient_gone row is
    terminal / not owed (RED); re-home re-enters it (escalated marks cleared, now
    drainable by the successor) — its terminality WAS the orphan bug.
  * R3 cap_reached STAYS terminal: an escalated cap_reached row is NOT revived by
    re-home (its emissions were genuinely spent against a live-but-deaf recipient
    = Root B, not an orphan).
  * R4 negative-adoption + fail-closed: a DIFFERENT agent_session_id adopts
    nothing; an empty session id touches nothing.
  * R5 the register hook: ``_direct_wake_self_refresh`` token contract
    (rehomed:<n> / no_owed / no_session_key / no_service).

Run:
    HOMUNCULUS_NAME=<name>-test .venv/bin/python3 \
        plugins/agent_messaging_plugin/tests/direct_wake_rehome_smoke.py
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
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
from ananta.llm.agent_messaging.models import TextPart  # noqa: E402
from ananta.llm.agent_messaging.schema import (  # noqa: E402
    ESCALATION_REASON_CAP,
    ESCALATION_REASON_GONE,
    NAMESPACE,
    TABLE_AGENT_DIRECT_WAKE,
)
from ananta.llm.agent_messaging.service import AgentMessagingService  # noqa: E402

from agent_messaging_plugin.http_routes import (  # noqa: E402
    _direct_wake_self_refresh,
)

T0 = datetime(2026, 7, 11, 16, 32, 0, tzinfo=UTC)
_OLD = "agi-97f34a"  # Dawn's pre-restart instance (the dead one)
_NEW = "agi-256bac8e"  # her post-restart successor
_SESS = "ases-47659-8254"  # the stable session id that survived the restart

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


class _Clock:
    def __init__(self, start: datetime) -> None:
        self.now = start

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


class _EnabledConfig:
    enabled = True
    allowed_backends: tuple[str, ...] = ()


def _service(state: RealShapeState, clock: _Clock) -> AgentMessagingService:
    return AgentMessagingService(
        repository=cast(Any, object()),
        state_service=cast(StateManagementInterface, state),
        backend_router=cast(Any, object()),
        flow_manager=cast(Any, object()),
        action_factory=cast(Any, object()),
        compilation_context_builder=cast(Any, object()),
        bridge_delivery=cast(Any, object()),
        config=cast(Any, _EnabledConfig()),
        clock=clock,
    )


def _persist(
    svc: AgentMessagingService,
    *,
    message_id: str,
    recipient_instance: str = _OLD,
    recipient_session: str = _SESS,
) -> None:
    svc.persist_direct_wake(
        message_id=message_id,
        thread_id="agt-1",
        recipient_agent_id="claude_code",
        recipient_agent_instance_id=recipient_instance,
        recipient_agent_session_id=recipient_session,
        sender_agent_id="claude_code",
        sender_agent_instance_id="agi-A",
        sender_session_label="Claude-A",
        sender_bridge_id="agc-s",
        content=[TextPart(type="text", text="IMPORTANT: Lane C report")],
        activity_at_emission=None,
    )


def _row(state: RealShapeState, message_id: str) -> dict[str, Any]:
    return next(
        r
        for r in state.rows(NAMESPACE, TABLE_AGENT_DIRECT_WAKE)
        if r["message_id"] == message_id and not r.get("is_deleted")
    )


def _owed_ids(svc: AgentMessagingService, *, instance: str, now: datetime) -> list[str]:
    return [
        str(r["message_id"])
        for r in svc.list_owed_direct_for_instance(
            agent_instance_id=instance, limit=50, now=now,
        )
    ]


# ---------------------------------------------------------------------------
# R1 — orphan → re-home (the 16:32/16:35 acceptance replay)
# ---------------------------------------------------------------------------


def test_r1_orphan_rehome() -> None:
    state = RealShapeState()
    clock = _Clock(T0)
    svc = _service(state, clock)
    _persist(svc, message_id="agm-report")
    clock.advance(301)  # past the re-emit window
    # RED anchor — the orphan: the row is owed to the DEAD OLD instance, so the
    # session's SUCCESSOR instance cannot drain it (this is recipient_gone).
    _check(
        _owed_ids(svc, instance=_OLD, now=clock.now) == ["agm-report"]
        and _owed_ids(svc, instance=_NEW, now=clock.now) == [],
        "R1 red: an owed row to the dead OLD instance is invisible to the successor",
    )
    moved = svc.rehome_owed_direct_wakes(
        agent_session_id=_SESS, new_agent_instance_id=_NEW,
    )
    # GREEN — re-home makes it deliverable to the successor, and only there.
    _check(moved == 1, "R1: re-home moves the one owed row (rows-affected=1)")
    _check(
        _owed_ids(svc, instance=_NEW, now=clock.now) == ["agm-report"]
        and _owed_ids(svc, instance=_OLD, now=clock.now) == [],
        "R1 green: after re-home the row is drainable by the successor, NOT the old instance",
    )
    _check(
        _row(state, "agm-report")["recipient_agent_instance_id"] == _NEW,
        "R1: the row's instance fence now points at the successor",
    )


# ---------------------------------------------------------------------------
# R2 — recipient_gone reactivation (already-escalated → re-entered)
# ---------------------------------------------------------------------------


def test_r2_recipient_gone_reactivation() -> None:
    state = RealShapeState()
    clock = _Clock(T0)
    svc = _service(state, clock)
    _persist(svc, message_id="agm-gone")
    # The reconciler already escalated it recipient_gone (bridge died before drain).
    svc.mark_direct_escalated(message_id="agm-gone", reason=ESCALATION_REASON_GONE)
    clock.advance(301)
    # RED anchor: a terminal (escalated) row is owed to NOBODY — it stranded.
    _check(
        _owed_ids(svc, instance=_OLD, now=clock.now) == []
        and _owed_ids(svc, instance=_NEW, now=clock.now) == [],
        "R2 red: an escalated recipient_gone row is terminal (stranded, un-drainable)",
    )
    revived = svc.rehome_owed_direct_wakes(
        agent_session_id=_SESS, new_agent_instance_id=_NEW,
    )
    row = _row(state, "agm-gone")
    _check(
        revived == 1
        and row["escalated"] is False
        and row["recipient_agent_instance_id"] == _NEW
        and not row["escalation_reason"],
        "R2: re-home REACTIVATES the recipient_gone row (escalated cleared, re-pointed)",
    )
    _check(
        _owed_ids(svc, instance=_NEW, now=clock.now) == ["agm-gone"],
        "R2 green: the reactivated row is drainable by the successor",
    )


# ---------------------------------------------------------------------------
# R3 — cap_reached stays terminal (NOT revived)
# ---------------------------------------------------------------------------


def test_r3_cap_reached_stays_terminal() -> None:
    state = RealShapeState()
    clock = _Clock(T0)
    svc = _service(state, clock)
    _persist(svc, message_id="agm-cap")
    svc.mark_direct_escalated(message_id="agm-cap", reason=ESCALATION_REASON_CAP)
    clock.advance(301)
    moved = svc.rehome_owed_direct_wakes(
        agent_session_id=_SESS, new_agent_instance_id=_NEW,
    )
    row = _row(state, "agm-cap")
    _check(
        moved == 0
        and row["escalated"] is True
        and row["escalation_reason"] == ESCALATION_REASON_CAP,
        "R3: a cap_reached row is NOT revived by re-home (emissions spent = Root B)",
    )
    _check(
        _owed_ids(svc, instance=_NEW, now=clock.now) == []
        and _owed_ids(svc, instance=_OLD, now=clock.now) == [],
        "R3: the cap_reached row stays terminal (un-drainable) after re-home",
    )


# ---------------------------------------------------------------------------
# R4 — negative-adoption + fail-closed
# ---------------------------------------------------------------------------


def test_r4_negative_adoption_and_fail_closed() -> None:
    state = RealShapeState()
    clock = _Clock(T0)
    svc = _service(state, clock)
    _persist(svc, message_id="agm-x")
    # A DIFFERENT session id must NOT adopt this row.
    other = svc.rehome_owed_direct_wakes(
        agent_session_id="ases-someone-else", new_agent_instance_id="agi-intruder",
    )
    _check(
        other == 0 and _row(state, "agm-x")["recipient_agent_instance_id"] == _OLD,
        "R4: a DIFFERENT agent_session_id adopts nothing (negative-adoption guard)",
    )
    # An empty session id is not an identity — fail closed, touch nothing.
    empty = svc.rehome_owed_direct_wakes(
        agent_session_id="", new_agent_instance_id="agi-nobody",
    )
    _check(
        empty == 0 and _row(state, "agm-x")["recipient_agent_instance_id"] == _OLD,
        "R4: an empty session id fails closed (touches nothing)",
    )


# ---------------------------------------------------------------------------
# R5 — the peer/register hook token contract
# ---------------------------------------------------------------------------


def test_r5_register_hook_tokens() -> None:
    state = RealShapeState()
    clock = _Clock(T0)
    svc = _service(state, clock)
    _persist(svc, message_id="agm-hook")
    rehomed = _direct_wake_self_refresh(
        svc, agent_session_id=_SESS, new_agent_instance_id=_NEW,
    )
    _check(rehomed == "rehomed:1", "R5: hook returns rehomed:<n> when rows moved")
    # no_owed: a session that is owed nothing (re-running the SAME session re-
    # matches the still-owed row and idempotently re-points it — same semantics
    # as the role refresh_role_binding_cas sibling — so use a fresh session here).
    empty = _direct_wake_self_refresh(
        svc, agent_session_id="ases-owed-nothing", new_agent_instance_id=_NEW,
    )
    _check(empty == "no_owed", "R5: hook returns no_owed for a session owed nothing")
    _check(
        _direct_wake_self_refresh(svc, agent_session_id="", new_agent_instance_id=_NEW)
        == "no_session_key",
        "R5: hook returns no_session_key for an empty session id",
    )
    _check(
        _direct_wake_self_refresh(None, agent_session_id=_SESS, new_agent_instance_id=_NEW)
        == "no_service",
        "R5: hook returns no_service when the service is unbound",
    )


def main() -> None:
    print("=== Fork-1a direct-wake re-home (REL-01 recipient_gone cure) smoke ===")
    test_r1_orphan_rehome()
    test_r2_recipient_gone_reactivation()
    test_r3_cap_reached_stays_terminal()
    test_r4_negative_adoption_and_fail_closed()
    test_r5_register_hook_tokens()
    print(f"\n{_passed} passed, {len(_failed)} failed")
    if _failed:
        for label in _failed:
            print(f"  FAILED: {label}")
        sys.exit(1)


if __name__ == "__main__":
    main()
